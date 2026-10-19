"""在线备份会话库，并通过隔离恢复核对表行数；不复制密钥和模型缓存。"""
import hashlib
import json
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """读取业务表行数，用于备份和恢复之间的对平检核。"""
    names = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )]
    return {name: connection.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
            for name in names}


def backup_history(source: Path, destination: Path) -> dict:
    """生成独立 SQLite 快照并恢复验证；保留所有历史备份，不自动清理。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as origin:
        with closing(sqlite3.connect(destination)) as backup:
            origin.backup(backup)
            expected = table_counts(backup)
            assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        with closing(sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)) as backup:
            with closing(sqlite3.connect(Path(temporary) / "restore.db")) as restored:
                backup.backup(restored)
                assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                assert table_counts(restored) == expected
    evidence = {"file": str(destination), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "counts": expected, "restore_verified": True,
                "release": str(Path("/opt/agentflow/current").resolve())}
    destination.with_suffix(".json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    return evidence


def main() -> None:
    """备份服务器上的稳定会话库，并输出不含秘密的验收证据。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    print(json.dumps(backup_history(Path("/var/lib/agentflow/history.db"),
                                   Path("/var/backups/agentflow") / f"history-{stamp}.db"),
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
