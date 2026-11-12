"""用户、域授权和登录会话的 SQLite 存储。"""
import argparse
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from getpass import getpass
from pathlib import Path

import config
from metadata.models import DOMAIN_CODE

COOKIE = "agentflow_session"
SESSION_TTL = 8 * 3600
LOGIN_WINDOW = 5 * 60
LOGIN_LIMIT = 12
_LOGIN_SLOTS = threading.BoundedSemaphore(2)
_USER = re.compile(r"[a-z0-9][a-z0-9_.-]{2,39}")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    password BLOB NOT NULL, salt BLOB NOT NULL, admin INTEGER NOT NULL, active INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS domains (code TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS user_domain_grants (
    user_id TEXT NOT NULL REFERENCES users(id), code TEXT NOT NULL REFERENCES domains(code),
    starts_at INTEGER NOT NULL, expires_at INTEGER, PRIMARY KEY (user_id, code)
);
CREATE TABLE IF NOT EXISTS login_sessions (
    digest TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (client TEXT NOT NULL, attempted_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts (attempted_at, client);
CREATE TABLE IF NOT EXISTS access_audit (
    id INTEGER PRIMARY KEY, occurred_at INTEGER NOT NULL, actor TEXT, action TEXT NOT NULL,
    target TEXT NOT NULL, before TEXT, after TEXT
);
"""


@dataclass(frozen=True)
class Principal:
    """由服务端会话取得的用户身份与当前有效业务域。"""

    id: str
    username: str
    name: str
    admin: bool
    domains: frozenset[str]


@dataclass(frozen=True)
class Grant:
    """一条域授权，可立即生效或设定有效期。"""

    code: str
    starts_at: int = 0
    expires_at: int | None = None


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=3, maxmem=64 * 1024**2)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _audit(conn: sqlite3.Connection, actor: str | None, action: str, target: str,
           before: dict | None = None, after: dict | None = None) -> None:
    conn.execute("INSERT INTO access_audit(occurred_at, actor, action, target, before, after) VALUES(?,?,?,?,?,?)",
                 (int(time.time()), actor, action, target, json.dumps(before, ensure_ascii=False),
                  json.dumps(after, ensure_ascii=False)))


class AccessStore:
    """权限库事务与会话校验，每次读取时重新计算有效授权。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def login(self, username: str, password: str, client: str) -> str | None:
        """验证凭据、限制尝试频率，并签发一次新的随机会话。"""
        now = int(time.time())
        if not _LOGIN_SLOTS.acquire(blocking=False):
            raise OverflowError("登录请求过多，请稍后重试")
        try:
            with self._connect() as conn:
                conn.execute("DELETE FROM login_attempts WHERE attempted_at <= ?", (now - LOGIN_WINDOW,))
                attempts = conn.execute("SELECT count(*) FROM login_attempts WHERE client=?", (client,)).fetchone()[0]
                if attempts >= LOGIN_LIMIT:
                    raise OverflowError("登录尝试过多，请稍后重试")
                conn.execute("INSERT INTO login_attempts VALUES (?,?)", (client, now))
                row = conn.execute("SELECT id, password, salt FROM users WHERE username=? AND active=1",
                                   (username.strip().lower(),)).fetchone()
            computed = _hash(password, row["salt"] if row else bytes(16))
            if not row or not hmac.compare_digest(row["password"], computed):
                return None
            token = secrets.token_urlsafe(32)
            with self._connect() as conn:
                conn.execute("DELETE FROM login_sessions WHERE expires_at <= ?", (now,))
                inserted = conn.execute("INSERT INTO login_sessions SELECT ?, id, ? FROM users "
                                        "WHERE id=? AND active=1 AND password=?",
                                        (_digest(token), now + SESSION_TTL, row["id"], row["password"]))
                return token if inserted.rowcount else None
        finally:
            _LOGIN_SLOTS.release()

    def resolve(self, token: str) -> Principal | None:
        """从会话令牌读取有效身份、启停状态与业务域。"""
        if not token or len(token) > 100:
            return None
        now = int(time.time())
        with self._connect() as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT u.id, u.username, u.name, u.admin FROM login_sessions s "
                               "JOIN users u ON u.id=s.user_id WHERE s.digest=? AND s.expires_at>? AND u.active=1",
                               (_digest(token), now)).fetchone()
            if row is None:
                return None
            grants = conn.execute("SELECT code FROM user_domain_grants WHERE user_id=? AND starts_at<=? "
                                  "AND (expires_at IS NULL OR expires_at>?)", (row["id"], now, now))
            return Principal(row["id"], row["username"], row["name"], bool(row["admin"]),
                             frozenset(item[0] for item in grants))

    def logout(self, token: str) -> None:
        """吊销本次登录签发的令牌。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM login_sessions WHERE digest=?", (_digest(token),))

    def domains(self) -> list[dict]:
        """返回域名和当前有效的授权人数。"""
        now = int(time.time())
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT d.code, d.name, count(u.id) AS user_count FROM domains d "
                "LEFT JOIN user_domain_grants g ON d.code=g.code AND g.starts_at<=? "
                "AND (g.expires_at IS NULL OR g.expires_at>?) "
                "LEFT JOIN users u ON g.user_id=u.id AND u.active=1 GROUP BY d.code ORDER BY d.code", (now, now))]

    def save_domain(self, actor: str, code: str, name: str) -> None:
        """登记或更新业务域显示名称，保留业务域代码。"""
        name = name.strip()
        if not DOMAIN_CODE.fullmatch(code) or not 1 <= len(name) <= 60:
            raise ValueError("请输入有效的业务域代码和名称")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT name FROM domains WHERE code=?", (code,)).fetchone()
            conn.execute("INSERT INTO domains VALUES (?,?) ON CONFLICT(code) DO UPDATE SET name=excluded.name",
                         (code, name))
            _audit(conn, actor, "domain.save", code, dict(old) if old else None, {"name": name})

    @staticmethod
    def _user(conn: sqlite3.Connection, user_id: str) -> dict | None:
        row = conn.execute("SELECT id, username, name, admin, active FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            return None
        return dict(row) | {"admin": bool(row["admin"]), "active": bool(row["active"]),
                            "grants": [dict(g) for g in conn.execute(
                                "SELECT code, starts_at, expires_at FROM user_domain_grants WHERE user_id=? ORDER BY code",
                                (user_id,))]}

    def users(self) -> list[dict]:
        """读取账号状态和授权，不返回密码摘要或会话令牌。"""
        with self._connect() as conn:
            conn.execute("BEGIN")
            return [self._user(conn, row[0]) for row in conn.execute("SELECT id FROM users ORDER BY username")]

    def audit(self) -> list[dict]:
        """读取最近的账号、业务域及拒绝记录。"""
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM access_audit ORDER BY id DESC LIMIT 50")]

    def denied(self, actor: str, action: str) -> None:
        """记录越权拒绝，省略用户问题与资源标识。"""
        with self._connect() as conn:
            _audit(conn, actor, "access.denied", action)

    def save_user(self, *, actor: str | None, username: str, name: str, active: bool,
                  admin: bool, grants: Sequence[Grant], password: str = "", user_id: str | None = None) -> dict:
        """新建或修改账号，事务内更新域授权与历史。"""
        username, name = username.strip().lower(), name.strip()
        if not _USER.fullmatch(username) or not 1 <= len(name) <= 60:
            raise ValueError("请填写有效的登录账号和姓名")
        if (password or user_id is None) and not 12 <= len(password) <= 128:
            raise ValueError("密码长度须为 12 到 128 个字符")
        now = int(time.time())
        if len({grant.code for grant in grants}) != len(grants) or any(
            grant.starts_at < 0 or (grant.expires_at is not None
                                   and grant.expires_at <= (grant.starts_at or now)) for grant in grants
        ):
            raise ValueError("请检查业务域重复授权和有效期")
        salt = secrets.token_bytes(16)
        encoded = _hash(password, salt) if password else None
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = self._user(conn, user_id) if user_id else None
            if user_id and old is None:
                raise LookupError("账号不存在")
            if actor is None and (not admin or conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()):
                raise ValueError("权限库已初始化，请通过管理员页面维护")
            if old and old["admin"] and old["active"] and not (admin and active):
                if conn.execute("SELECT count(*) FROM users WHERE admin=1 AND active=1").fetchone()[0] == 1:
                    raise ValueError("至少保留一个启用的管理员")
            if any(not conn.execute("SELECT 1 FROM domains WHERE code=?", (g.code,)).fetchone() for g in grants):
                raise ValueError("存在未登记的业务域")
            if conn.execute("SELECT 1 FROM users WHERE username=? AND id<>?", (username, user_id or "")).fetchone():
                raise ValueError("登录账号已存在")
            if user_id is None:
                user_id = uuid.uuid4().hex
                conn.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?)",
                             (user_id, username, name, encoded, salt, int(admin), int(active)))
            else:
                conn.execute("UPDATE users SET username=?, name=?, admin=?, active=? WHERE id=?",
                             (username, name, int(admin), int(active), user_id))
                if encoded:
                    conn.execute("UPDATE users SET password=?, salt=? WHERE id=?", (encoded, salt, user_id))
                if encoded or not active:
                    conn.execute("DELETE FROM login_sessions WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM user_domain_grants WHERE user_id=?", (user_id,))
            conn.executemany("INSERT INTO user_domain_grants VALUES (?,?,?,?)",
                             [(user_id, g.code, g.starts_at or now, g.expires_at) for g in grants])
            saved = self._user(conn, user_id)
            _audit(conn, actor, "user.save", user_id, old, saved)
            return saved


@lru_cache(maxsize=4)
def _store(path: Path) -> AccessStore:
    return AccessStore(path)


def access_store() -> AccessStore:
    """获取当前运行配置指定的权限库。"""
    path = Path(config.ACCESS_DB) if config.ACCESS_DB else Path(config.MART_DIR) / "build" / "access.db"
    return _store(path.resolve())


def main() -> None:
    """从本机命令行初始化第一个管理员，口令经交互录入。"""
    parser = argparse.ArgumentParser(description="初始化 AgentFlow 管理员")
    parser.add_argument("username")
    parser.add_argument("name")
    args = parser.parse_args()
    password = getpass("密码：")
    if password != getpass("再次输入密码："):
        raise SystemExit("两次密码不一致")
    access_store().save_user(actor=None, username=args.username, name=args.name, admin=True, active=True,
                             grants=[], password=password)
    print("管理员已创建")


if __name__ == "__main__":
    main()
