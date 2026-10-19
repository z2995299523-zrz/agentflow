"""验证发布边界：拒绝错版/越界构建包，在线备份确实能隔离恢复。"""
import hashlib
import io
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from deploy.backup_history import backup_history
from deploy.release import unpack_frontend


def _bundle(path: Path, commit: str, name: str = "web/dist/index.html") -> None:
    """制作包含一个页面和清单的测试发布包。"""
    content = b"test page"
    manifest = json.dumps({"commit": commit, "files": {name: hashlib.sha256(content).hexdigest()}}).encode()
    with tarfile.open(path, "w") as bundle:
        for filename, data in ((name, content), ("frontend-manifest.json", manifest)):
            entry = tarfile.TarInfo(filename)
            entry.size = len(data)
            bundle.addfile(entry, io.BytesIO(data))


def test_frontend_must_match_source_commit(tmp_path: Path) -> None:
    """前端构建提交与服务端源码不同时必须阻止发布。"""
    archive = tmp_path / "frontend.tar"
    _bundle(archive, "a" * 40)
    with pytest.raises(AssertionError, match="提交不匹配"):
        unpack_frontend(archive, tmp_path / "release", "b" * 40)
    assert not (tmp_path / "release").exists()


@pytest.mark.parametrize("name", ["../outside", "/etc/cron.d/test", "web/dist/../../../outside"])
def test_frontend_rejects_path_escape(tmp_path: Path, name: str) -> None:
    """发布包不得写入版本目录以外的位置。"""
    archive = tmp_path / "frontend.tar"
    _bundle(archive, "a" * 40, name)
    with pytest.raises(ValueError, match="不允许"):
        unpack_frontend(archive, tmp_path / "release", "a" * 40)


def test_backup_preserves_rows_and_verifies_restore(tmp_path: Path) -> None:
    """备份必须保留会话及中文内容，生成摘要与实际文件一致。"""
    source, destination = tmp_path / "history.db", tmp_path / "backup/history.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE turns (question TEXT)")
        connection.execute("INSERT INTO turns VALUES (?)", ("统计九月份存款余额",))
    result = backup_history(source, destination)
    assert result["restore_verified"] is True
    assert result["counts"] == {"turns": 1}
    assert result["sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()
    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT question FROM turns").fetchone()[0] == "统计九月份存款余额"
    with pytest.raises(FileExistsError):
        backup_history(source, destination)


def test_backup_missing_source_is_not_silently_created(tmp_path: Path) -> None:
    """源库不存在时必须失败，不能把自动新建的空库当成有效备份。"""
    source = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        backup_history(source, tmp_path / "backup/history.db")
    assert not source.exists()
