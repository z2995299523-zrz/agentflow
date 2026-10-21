"""
会话历史：每个会话的每一轮（提问和流水线结果）存进 SQLite，界面左侧的会话列表、刷新后恢复对话都靠它
- 流水线本身无状态；追问、反问要的上下文（上一轮的完整需求、还没回答的反问，上一轮校验通过时它的 SQL 和用到的表）
  由 context 从存下的轮次里算出来
- 每套资料一个库文件：资料目录/build/history.db，和元数据库放在一起，不入库
- 每个操作单独开一个连接，多线程同时用没有问题；只用标准 SQL，以后换别的数据库只换这个类
"""
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from nl2sql.schemas import Clarify, QueryResult

HISTORY_FILE_NAME = "history.db"
SCHEMA_VERSION = 1
TITLE_CHARS = 40                 # 会话标题默认取第一轮提问的前多少个字
CLARIFY_STATUS = "need_clarify"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  TEXT NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    question         TEXT NOT NULL,
    status           TEXT NOT NULL,
    requirement      TEXT NOT NULL DEFAULT '',
    clarify          TEXT,
    result           TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_conversation ON turns (conversation_id, id);
"""
# 会话列表里的一项：会话加上它有几轮；{where} 填过滤条件
_SUMMARY = ("SELECT c.id, c.title, c.created_at, c.updated_at, COUNT(t.id) AS turn_count"
            " FROM conversations AS c LEFT JOIN turns AS t ON t.conversation_id = c.id {where}"
            " GROUP BY c.id, c.title, c.created_at, c.updated_at")


class ConversationSummary(BaseModel):
    """会话列表里的一项"""

    id: str
    title: str
    created_at: str                           # UTC 时间，ISO 格式，精确到毫秒
    updated_at: str                           # 最后一轮的时间；还没有轮次时是创建时间
    turn_count: int = 0


class Turn(BaseModel):
    """会话里的一轮"""

    id: int
    question: str
    created_at: str
    result: dict[str, Any]                    # QueryResult.public()：不含提示词和模型原始回复


class ConversationContext(BaseModel):
    """下一轮要带给流水线的上下文"""

    prev_requirement: str = ""
    prev_clarify: Clarify | None = None
    clarify_turn_id: int | None = None        # 还没回答的反问出自哪一轮；界面上只有这一轮的选项能点
    prev_sql: str = ""                        # 那一轮校验通过的 SQL；下一轮是追问时以它为底
    prev_tables: list[str] = Field(default_factory=list)      # 那条 SQL 用到的表


class ConversationDetail(ConversationSummary):
    """一个会话的全部轮次和当前的上下文"""

    turns: list[Turn] = Field(default_factory=list)
    context: ConversationContext = Field(default_factory=ConversationContext)


def _now() -> str:
    """当前的 UTC 时间：ISO 格式，精确到毫秒，按字符串排序就是按时间排序"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _title_of(question: str) -> str:
    """由提问得出默认标题：空白压成一个空格，取前 TITLE_CHARS 个字"""
    return " ".join(question.split())[:TITLE_CHARS]


def _context_from(row: sqlite3.Row | None) -> ConversationContext:
    """由最近一个有完整需求的轮次得出上下文

    和原来界面的规则一样：解析成功（结果里有完整需求）的轮次才算数，解析失败、不是查数需求的轮次不动上下文，
    用户可以接着上一个话题问，也可以重新点上一次反问的选项；这一轮是反问时带上它的反问，
    下一轮是在回答它还是问了新问题，由流水线的 ① 判断。这一轮校验通过时带上它的 SQL 和用到的表
    （规则同 QueryResult.next_context），下一轮是不是在它上面追问，也由 ① 判断。
    """
    if row is None:
        return ConversationContext()
    if row["status"] == CLARIFY_STATUS and row["clarify"]:
        return ConversationContext(prev_requirement=row["requirement"],
                                   prev_clarify=Clarify.model_validate_json(row["clarify"]),
                                   clarify_turn_id=row["id"])
    carried = QueryResult.model_validate_json(row["result"]).next_context()
    return ConversationContext(prev_requirement=row["requirement"], prev_sql=carried.get("prev_sql", ""),
                               prev_tables=carried.get("prev_tables", []))


class ConversationStore:
    """会话历史库：建会话、列会话、取会话、改标题、删会话、记一轮、算上下文"""

    def __init__(self, db_path: Path) -> None:
        """
        Args:
            db_path: 库文件路径；目录和表不存在时自动建
        """
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @property
    def path(self) -> Path:
        """库文件路径"""
        return self._path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """开一个连接，块内是一个事务：正常结束提交，出错回滚；最后关掉连接"""
        conn = sqlite3.connect(self._path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create(self, title: str = "") -> ConversationSummary:
        """新建一个空会话；标题不写时，第一轮提问时用提问的前几个字"""
        now = _now()
        summary = ConversationSummary(id=uuid.uuid4().hex, title=title.strip(), created_at=now, updated_at=now)
        with self._connect() as conn:
            conn.execute("INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                         (summary.id, summary.title, now, now))
        return summary

    def list_recent(self, limit: int = 50) -> list[ConversationSummary]:
        """最近的会话，按最后一轮的时间从新到旧"""
        with self._connect() as conn:
            rows = conn.execute(_SUMMARY.format(where="") + " ORDER BY c.updated_at DESC, c.created_at DESC LIMIT ?",
                                (limit,)).fetchall()
        return [ConversationSummary(**dict(row)) for row in rows]

    def get(self, conversation_id: str) -> ConversationDetail | None:
        """一个会话的全部轮次（从早到晚）和当前的上下文；会话不存在时返回 None"""
        with self._connect() as conn:
            head = conn.execute("SELECT id, title, created_at, updated_at FROM conversations WHERE id = ?",
                                (conversation_id,)).fetchone()
            if head is None:
                return None
            rows = conn.execute("SELECT id, question, status, requirement, clarify, result, created_at FROM turns"
                                " WHERE conversation_id = ? ORDER BY id", (conversation_id,)).fetchall()
        turns = [Turn(id=row["id"], question=row["question"], created_at=row["created_at"],
                      result=json.loads(row["result"])) for row in rows]
        latest = next((row for row in reversed(rows) if row["requirement"]), None)
        return ConversationDetail(**dict(head), turn_count=len(turns), turns=turns, context=_context_from(latest))

    def rename(self, conversation_id: str, title: str) -> ConversationSummary | None:
        """改标题；会话不存在时返回 None"""
        with self._connect() as conn:
            conn.execute("UPDATE conversations SET title = ? WHERE id = ?", (title.strip(), conversation_id))
            row = conn.execute(_SUMMARY.format(where="WHERE c.id = ?"), (conversation_id,)).fetchone()
        return ConversationSummary(**dict(row)) if row is not None else None

    def delete(self, conversation_id: str) -> bool:
        """删掉会话和它的全部轮次；会话不存在时返回 False"""
        with self._connect() as conn:
            conn.execute("DELETE FROM turns WHERE conversation_id = ?", (conversation_id,))
            return conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,)).rowcount > 0

    def add_turn(self, conversation_id: str, question: str, result: QueryResult) -> Turn:
        """记下一轮：提问和流水线结果；会话还没有标题时用这一轮的提问

        Raises:
            LookupError: 会话不存在（比如跑的时候被删了）
        """
        now = _now()
        public = result.public()
        clarify = result.clarify.model_dump_json() if result.status == CLARIFY_STATUS and result.clarify else None
        with self._connect() as conn:
            head = conn.execute("SELECT title FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            if head is None:
                raise LookupError(f"会话不存在：{conversation_id}")
            cursor = conn.execute(
                "INSERT INTO turns (conversation_id, question, status, requirement, clarify, result, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (conversation_id, question, result.status, result.requirement, clarify,
                 json.dumps(public, ensure_ascii=False), now))
            conn.execute("UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                         (head["title"] or _title_of(question), now, conversation_id))
        return Turn(id=cursor.lastrowid, question=question, created_at=now, result=public)

    def context(self, conversation_id: str) -> ConversationContext:
        """下一轮要带给流水线的上下文：最近一个有完整需求的轮次的需求，以及它还没被回答的反问；
        那一轮校验通过时，还有它的 SQL 和用到的表

        Raises:
            LookupError: 会话不存在
        """
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone() is None:
                raise LookupError(f"会话不存在：{conversation_id}")
            row = conn.execute("SELECT id, status, requirement, clarify, result FROM turns"
                               " WHERE conversation_id = ? AND requirement <> '' ORDER BY id DESC LIMIT 1",
                               (conversation_id,)).fetchone()
        return _context_from(row)
