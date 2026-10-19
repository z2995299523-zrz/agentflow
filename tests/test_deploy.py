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
from deploy.runtime_profile import make_profile, metadata_contract, read_environment, write_profile
from deploy.dataset import package_dataset, unpack_dataset


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


def _metadata(path: Path) -> None:
    """构造只用于发布契约检查的最小元数据库，不包含真实资料。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        for table in ("meta_table", "meta_column", "meta_lineage", "meta_relation", "meta_script", "meta_example"):
            connection.execute(f"CREATE TABLE {table} (name TEXT)")
        connection.execute("INSERT INTO meta_table VALUES ('test_table')")
        connection.execute("INSERT INTO meta_column VALUES ('test_column')")
        connection.execute("CREATE TABLE meta_vector (kind TEXT)")
        connection.execute("INSERT INTO meta_vector VALUES ('field')")
        connection.execute("CREATE TABLE meta_info (key TEXT, value TEXT)")
        connection.execute("INSERT INTO meta_info VALUES ('embedder','bge-small-zh-v1.5')")
        connection.commit()
    finally:
        connection.close()


def test_runtime_profile_preserves_secret_quoting_and_permissions(tmp_path: Path) -> None:
    """切换资料只改三项运行配置，密钥原文、引号和文件权限必须保留。"""
    path = tmp_path / "app.env"
    secret_line = 'DEEPSEEK_API_KEY="test key#with hash"'
    path.write_text(secret_line + '\nMART_DIR="./data/mock_mart"\nEXECUTOR=duckdb\n', encoding="utf-8")
    permissions = path.stat().st_mode & 0o777
    write_profile(path, {"mart_dir": "/var/lib/agentflow/datasets/private-test", "executor": "none",
                         "history_path": "/var/lib/agentflow/history-real.db"})
    values = read_environment(path)
    assert values["DEEPSEEK_API_KEY"] == "test key#with hash"
    assert secret_line in path.read_text()
    assert values["EXECUTOR"] == "none"
    assert values["AGENTFLOW_HISTORY_DB"].endswith("history-real.db")
    assert path.stat().st_mode & 0o777 == permissions


def test_external_profile_cannot_execute_in_mock_database(tmp_path: Path) -> None:
    """真实资料搭配模拟执行器必须在发布前被拒绝。"""
    _metadata(tmp_path / "build/metadata.db")
    with pytest.raises(ValueError, match="EXECUTOR=none"):
        make_profile(str(tmp_path), "duckdb", tmp_path)
    profile = make_profile(str(tmp_path), "none", tmp_path)
    assert profile["counts"]["表"] == 1 and profile["external"]


def test_metadata_contract_rejects_incomplete_vectors(tmp_path: Path) -> None:
    """缺少字段向量不能作为完整线上检索资料发布。"""
    path = tmp_path / "metadata.db"
    _metadata(path)
    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM meta_vector")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="完整 BGE"):
        metadata_contract(path)


def test_dataset_snapshot_excludes_history_and_round_trips(tmp_path: Path) -> None:
    """资料包只能带模型资料，不能顺带上传本地会话、环境文件和评估文件。"""
    source = tmp_path / "source"
    _metadata(source / "build/metadata.db")
    (source / "build/history.db").write_text("local-only-history")
    (source / ".env").write_text("SECRET=local-only")
    (source / "eval_questions.yaml").write_text("private-local-test")
    (source / "表清单.csv").write_text("table\nexample\n")
    (source / "customer_rows.csv").write_text("must-not-upload-business-rows")
    (source / "scripts").mkdir()
    (source / "scripts/example.sql").write_text("SELECT 1")
    archive = tmp_path / "dataset.tar.gz"
    result = package_dataset(source, archive)
    assert result["files"] == 3
    target = tmp_path / "restored"
    manifest = unpack_dataset(archive, target)
    assert manifest["counts"]["字段"] == 1
    assert not (target / "build/history.db").exists()
    assert not (target / ".env").exists()
    assert not (target / "customer_rows.csv").exists()
    assert (target / "scripts/example.sql").read_text() == "SELECT 1"
    with pytest.raises(FileExistsError):
        package_dataset(source, archive)


def test_dataset_rejects_tampered_file(tmp_path: Path) -> None:
    """传输包中的文件被改动时，不能进入正式资料目录。"""
    archive = tmp_path / "tampered.tar"
    manifest = {"format": 1, "files": {"表清单.csv": "0" * 64}}
    with tarfile.open(archive, "w") as bundle:
        for name, data in (("表清单.csv", b"changed"), ("dataset-manifest.json", json.dumps(manifest).encode())):
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            bundle.addfile(entry, io.BytesIO(data))
    with pytest.raises(ValueError, match="摘要不一致"):
        unpack_dataset(archive, tmp_path / "staging")


def test_dataset_rejects_history_even_when_manifest_lists_it(tmp_path: Path) -> None:
    """清单里也不能授权把本地会话库作为模型资料发布。"""
    archive = tmp_path / "history.tar"
    content = b"local history"
    manifest = {"format": 1, "files": {"build/history.db": hashlib.sha256(content).hexdigest()}}
    with tarfile.open(archive, "w") as bundle:
        for name, data in (("build/history.db", content), ("dataset-manifest.json", json.dumps(manifest).encode())):
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            bundle.addfile(entry, io.BytesIO(data))
    with pytest.raises(ValueError, match="只能包含"):
        unpack_dataset(archive, tmp_path / "staging")
