"""验证发布边界：拒绝错版/越界构建包，在线备份确实能隔离恢复。"""
import hashlib
import io
import json
import sqlite3
import tarfile
import subprocess
import sys
from pathlib import Path

import pytest

from deploy.backup_history import backup_history
from deploy.release import load_site, render_nginx, unpack_frontend
from deploy.runtime_profile import make_profile, metadata_contract, read_environment, write_profile
from deploy.dataset import package_dataset, unpack_dataset
from deploy import release, runtime_profile
from metadata.versions import inputs_fingerprint


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


_SITE = {"public_url": "https://example.test:8447", "server_name": "example.test",
         "cert_dir": "/etc/letsencrypt/live/example", "uv": "/opt/tools/uv", "watch_units": ["other-site"]}


def test_site_config_must_exist_and_be_complete(tmp_path: Path) -> None:
    """服务器上没有站点配置或缺键时，发布在动手前就停下。"""
    path = tmp_path / "site.json"
    with pytest.raises(RuntimeError, match="缺少"):
        load_site(path)
    path.write_text(json.dumps({k: v for k, v in _SITE.items() if k != "cert_dir"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="cert_dir"):
        load_site(path)
    path.write_text(json.dumps(_SITE), encoding="utf-8")
    assert load_site(path)["watch_units"] == ["other-site"]
    path.write_text(json.dumps({**_SITE, "nginx_template": str(tmp_path / "missing.conf")}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="nginx_template"):
        load_site(path)


def test_server_nginx_template_overrides_release(tmp_path: Path) -> None:
    """站点配置写了服务器上的模板就用它（回退旧版本也一样），没写时用该版本仓库里的模板。"""
    code = tmp_path / "release"
    (code / "deploy").mkdir(parents=True)
    (code / "deploy/nginx.conf").write_bytes(b"listen 8447 ssl;\n")
    assert release.nginx_template(code, _SITE) == b"listen 8447 ssl;\n"
    custom = tmp_path / "server-nginx.conf"
    custom.write_bytes(b"listen 127.0.0.1:9443 ssl;\n")
    assert release.nginx_template(code, {**_SITE, "nginx_template": str(custom)}) == custom.read_bytes()


def test_nginx_template_is_filled_from_site_config() -> None:
    """仓库里的模板只有变量，填好后不留变量；旧版本写死的配置原样通过，没填的变量拦下。"""
    template = (Path(__file__).parent.parent / "deploy/nginx.conf").read_bytes()
    rendered = render_nginx(template, _SITE)
    assert b"server_name example.test;" in rendered
    assert b"ssl_certificate /etc/letsencrypt/live/example/fullchain.pem;" in rendered
    assert b"error_page 497 =308 https://example.test:8447$request_uri;" in rendered
    assert b"__" not in rendered
    literal = b"server {\n    server_name 192.0.2.1;\n}\n"
    assert render_nginx(literal, _SITE) == literal
    with pytest.raises(ValueError, match="__UNKNOWN__"):
        render_nginx(b"server_name __UNKNOWN__;", _SITE)


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


def _metadata(path: Path, version_id: str | None = None) -> None:
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
        if version_id is not None:
            connection.execute("INSERT INTO meta_info VALUES ('version_id', ?)", (version_id,))
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
    _metadata(source / "build/metadata.db", "test-v1")
    (source / "build/history.db").write_text("local-only-history")
    (source / ".env").write_text("SECRET=local-only")
    (source / "eval_questions.yaml").write_text("private-local-test")
    (source / "表清单.csv").write_text("table\nexample\n")
    (source / "customer_rows.csv").write_text("must-not-upload-business-rows")
    (source / "scripts").mkdir()
    (source / "scripts/example.sql").write_text("SELECT 1")
    _version_state(source)
    archive = tmp_path / "dataset.tar.gz"
    result = package_dataset(source, archive)
    assert result["files"] == 4
    assert result["version_id"] == "test-v1"
    target = tmp_path / "restored"
    manifest = unpack_dataset(archive, target)
    assert manifest["counts"]["字段"] == 1
    assert not (target / "build/history.db").exists()
    assert not (target / ".env").exists()
    assert not (target / "customer_rows.csv").exists()
    assert (target / "scripts/example.sql").read_text() == "SELECT 1"
    version = json.loads((target / "version.json").read_text(encoding="utf-8"))
    assert version["version_id"] == manifest["version_id"] == "test-v1"
    assert "inputs" not in version and "confirm_items" not in version
    assert "private-confirmation" not in json.dumps(version)
    assert version["confirm_count"] == 1
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


def _version_state(mart: Path, version_id: str = "test-v1") -> Path:
    """手工登记虚构资料版本，使用导入器同一套输入指纹。"""
    inputs, digest = inputs_fingerprint(mart)
    path = mart / "build/versions" / version_id / "version.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "version_id": version_id, "created_at": "2026-10-23T15:00:00+08:00", "baseline": None,
        "inputs": inputs, "inputs_digest": digest, "code_digest": "test-code", "git_commit": None,
        "embedder": "bge-small-zh-v1.5", "dim": 512, "counts": {"表": 1, "字段": 1},
        "gates": {"清单校验": "通过"}, "accepted_at": None,
        "confirm_items": ["private-confirmation"]}), encoding="utf-8")
    (mart / "build/current.json").write_text(json.dumps({"active": version_id}), encoding="utf-8")
    return path


@pytest.mark.parametrize("version_id", [None, "test-v1"])
def test_metadata_contract_includes_version(tmp_path: Path, version_id: str | None) -> None:
    """发布契约携带库内版本号，兼容未登记版本的旧库。"""
    path = tmp_path / "metadata.db"
    _metadata(path, version_id)
    assert metadata_contract(path)["version_id"] == version_id


@pytest.mark.parametrize("failure, message", [
    ("missing", "先运行 python -m metadata.importer 导入，生成版本"),
    ("legacy", "活动版本是旧库登记的，没有输入记录"),
    ("changed", "和活动版本 test-v1 登记的不一致"),
    ("database", "version_id 和活动版本号不一致")])
def test_package_rejects_unimported_state(tmp_path: Path, failure: str, message: str) -> None:
    """四种版本状态错误均在创建输出目录前拒绝，并给出重新导入提示。"""
    mart = tmp_path / "mart"
    _metadata(mart / "build/metadata.db", "other" if failure == "database" else "test-v1")
    (mart / "scripts").mkdir()
    script = mart / "scripts/test.sql"
    script.write_text("SELECT 1")
    if failure != "missing":
        path = _version_state(mart)
        if failure == "legacy":
            value = json.loads(path.read_text(encoding="utf-8"))
            value["inputs_digest"] = None
            path.write_text(json.dumps(value), encoding="utf-8")
    if failure == "changed":
        script.write_text("SELECT 2")
    output = tmp_path / "output/archive.tar.gz"
    with pytest.raises(ValueError, match=message):
        package_dataset(mart, output)
    assert not output.parent.exists()


def _dataset_bundle(archive: Path, database: Path, version: dict | None, manifest_version: dict) -> None:
    """制作摘要正确、版本信息可指定的新旧格式资料包。"""
    files = {"build/metadata.db": database.read_bytes()}
    if version is not None:
        files["version.json"] = json.dumps(version).encode()
    contract = metadata_contract(database)
    contract.pop("version_id")
    manifest = {"format": 1, **contract, **manifest_version,
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    files["dataset-manifest.json"] = json.dumps(manifest).encode()
    with tarfile.open(archive, "w") as bundle:
        for name, data in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            bundle.addfile(entry, io.BytesIO(data))


@pytest.mark.parametrize("version, manifest_version", [
    ({"version_id": "wrong"}, {}),
    ({"version_id": "test-v1"}, {"version_id": "wrong"}),
    (None, {"version_id": "wrong"})])
def test_unpack_rejects_version_mismatch(tmp_path: Path, version: dict | None, manifest_version: dict) -> None:
    """即使摘要正确，版本清单或包契约与数据库错版也不能安装。"""
    database = tmp_path / "metadata.db"
    _metadata(database, "test-v1")
    archive = tmp_path / "dataset.tar"
    _dataset_bundle(archive, database, version, manifest_version)
    with pytest.raises(ValueError, match="version_id.*不一致"):
        unpack_dataset(archive, tmp_path / "unpack")


@pytest.mark.parametrize("version_id", [None, "test-v1"])
def test_old_dataset_works_without_project_dependencies(tmp_path: Path, version_id: str | None) -> None:
    """隔离系统 Python 的第三方依赖和仓库路径，旧资料包仍可解包。"""
    database = tmp_path / "metadata.db"
    _metadata(database, version_id)
    archive = tmp_path / "dataset.tar"
    _dataset_bundle(archive, database, None, {})
    script = Path(__file__).resolve().parents[1] / "deploy/dataset.py"
    code = (
        "import sys, runpy; from pathlib import Path; "
        "sys.path.insert(0, str(Path(sys.argv[1]).parent)); "
        "module = runpy.run_path(sys.argv[1]); "
        "result = module['unpack_dataset'](Path(sys.argv[2]), Path(sys.argv[3])); "
        "assert not any(n == 'metadata' or n.startswith('metadata.') for n in sys.modules); "
        "print(result['version_id'])")
    result = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(script), str(archive),
                             str(tmp_path / "unpack")], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == str(version_id)


@pytest.mark.parametrize("actual", ["test-v1", "wrong", None, "missing"])
def test_health_checks_version_when_reported(monkeypatch, actual: str | None) -> None:
    """新接口核对资料版本，旧接口没有该键时仍能回退代码。"""
    expected = {"executor": "none", "mart_dir": "/test", "counts": {"字段向量": 1}, "version_id": "test-v1"}
    body = {"status": "ok", "hint": "", **{k: v for k, v in expected.items() if k != "version_id"}}
    if actual != "missing":
        body["metadata_version"] = actual
    monkeypatch.setattr(release.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(json.dumps(body).encode()))
    if actual in ("wrong", None):
        with pytest.raises(AssertionError, match="资料版本不符合发布契约"):
            release.health(expected)
    else:
        assert release.health(expected) == body


@pytest.fixture
def data_server(tmp_path: Path, monkeypatch) -> dict:
    """隔离所有服务器路径和进程操作，构造共享稳定会话库的两个虚构快照。"""
    base, data = tmp_path / "opt", tmp_path / "data"
    commit = "a" * 40
    code = base / "releases" / f"git-{commit}"
    code.mkdir(parents=True)
    current = base / "current"
    current.touch()
    history = data / "history-test.db"
    old, new = data / "datasets/private-old", data / "datasets/private-new"
    for mart, version in ((old, "old-v1"), (new, "new-v2")):
        _metadata(mart / "build/metadata.db", version)
        (mart / "build/history.db").touch()
    history.touch()
    # Windows 普通账户不能创建符号链接，用路径解析替身模拟服务器已有的链接。
    resolve = Path.resolve
    links = {current: code, old / "build/history.db": history, new / "build/history.db": history}
    monkeypatch.setattr(Path, "resolve", lambda path, *a, **kw: resolve(links.get(path, path), *a, **kw))
    access, env = tmp_path / "access.conf", tmp_path / "app.env"
    access.write_text("auth_basic_user_file /test/passwords;")
    env.write_bytes((f'# 原有配置\r\nDEEPSEEK_API_KEY="test secret"\r\nMART_DIR="{old.as_posix()}"\r\n'
                     'EXECUTOR=none\r\n').encode("utf-8"))
    for module in (release, runtime_profile):
        monkeypatch.setattr(module, "ENV_PATH", env)
    monkeypatch.setattr(release, "BASE", base)
    monkeypatch.setattr(release, "DATA", data)
    monkeypatch.setattr(release, "ACCESS_CONF", access)
    calls = []
    monkeypatch.setattr(release, "run", lambda *args, **kw: calls.append(("run", args)))
    monkeypatch.setattr(release, "as_app", lambda *args: calls.append(("as_app", args)))
    monkeypatch.setattr(release.time, "sleep", lambda seconds: None)
    def response(*args, **kwargs):
        profile = make_profile(read_environment(env)["MART_DIR"], "none", code)
        return io.BytesIO(json.dumps({"status": "ok", "hint": "", "executor": "none",
            "mart_dir": profile["mart_dir"], "counts": profile["counts"],
            "metadata_version": profile["version_id"]}).encode())
    monkeypatch.setattr(release.urllib.request, "urlopen", response)
    evidence = base / "deployments" / commit
    release.save(evidence / "previous.json", {"untouched": True})
    return {"commit": commit, "code": code, "base": base, "old": old, "new": new,
            "env": env, "access": access, "history": history, "calls": calls, "evidence": evidence}


def _switch_records(server: dict) -> list[dict]:
    """按时间顺序读本次测试的资料切换记录。"""
    return [json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(server["evidence"].glob("data-switch-*.json"))]


def test_switch_data_success(data_server: dict, capsys) -> None:
    """资料切换备份当前会话，更新配置和契约证据，代码与上一版发布记录保持原样。"""
    s = data_server
    old_env = s["env"].read_bytes()
    release.switch_data(s["commit"], str(s["new"]), "none")
    assert Path(read_environment(s["env"])["MART_DIR"]) == s["new"]
    assert (s["base"] / "current").resolve() == s["code"]
    assert json.loads((s["evidence"] / "previous.json").read_text()) == {"untouched": True}
    record, = _switch_records(s)
    assert record["commit"] == s["commit"] and record["how"] == "switch"
    assert record["from"] == make_profile(s["old"].as_posix(), "none", s["code"])
    assert record["to"] == make_profile(str(s["new"]), "none", s["code"])
    assert record["health"]["metadata_version"] == "new-v2" and record["at"]
    backup = Path(record["runtime_backup"])
    assert backup.read_bytes() == old_env
    if sys.platform != "win32":
        assert backup.stat().st_mode & 0o777 == 0o600
    assert s["calls"] == [
        ("as_app", (s["code"], str(s["code"] / "deploy/backup_history.py"), "--source", str(s["history"]))),
        ("run", ("systemctl", "restart", "agentflow"))]
    assert json.loads(capsys.readouterr().out)["version_id"] == "new-v2"


def test_switch_data_health_failure_restores_exact_environment(data_server: dict, monkeypatch) -> None:
    """健康检查重试三十次仍失败时恢复原文，二次重启并抛出原错误。"""
    s = data_server
    original = s["env"].read_bytes()
    error = RuntimeError("test health failure")
    attempts = []
    def fail(*args, **kwargs):
        attempts.append(1)
        raise error
    monkeypatch.setattr(release.urllib.request, "urlopen", fail)
    with pytest.raises(RuntimeError) as caught:
        release.switch_data(s["commit"], str(s["new"]))
    assert caught.value is error and len(attempts) == 30
    assert s["env"].read_bytes() == original
    assert s["calls"].count(("run", ("systemctl", "restart", "agentflow"))) == 2
    assert _switch_records(s) == []


@pytest.mark.parametrize("failure, message", [
    ("current", "当前指针不是"), ("outside", "datasets"), ("executor", "EXECUTOR=none"),
    ("missing_access", "密码保护"), ("unprotected", "密码保护"),
    ("same", "同一个目录"), ("history", "稳定会话库"), ("vectors", "完整 BGE")])
def test_switch_data_preconditions_have_no_effects(data_server: dict, failure: str, message: str) -> None:
    """前置条件不满足时不备份、不改运行配置、不重启应用。"""
    s = data_server
    original = s["env"].read_bytes()
    target, executor = str(s["new"]), "none"
    if failure == "current":
        (s["base"] / "current").unlink()
    elif failure == "outside":
        target = str(s["base"])
    elif failure == "executor":
        executor = "duckdb"
    elif failure == "missing_access":
        s["access"].unlink()
    elif failure == "unprotected":
        s["access"].write_text("auth_basic off;")
    elif failure == "same":
        target = str(s["old"])
    elif failure == "history":
        s["history"].unlink()
    elif failure == "vectors":
        with sqlite3.connect(s["new"] / "build/metadata.db") as db:
            db.execute("DELETE FROM meta_vector")
    with pytest.raises((ValueError, RuntimeError), match=message):
        release.switch_data(s["commit"], target, executor)
    assert s["env"].read_bytes() == original and s["calls"] == []
    assert _switch_records(s) == []
    assert list(s["evidence"].glob("runtime-before-*.env")) == []


def test_rollback_data_toggles_between_snapshots(data_server: dict) -> None:
    """回退以最新资料切换记录为准，连续两次回退能来回切换。"""
    s = data_server
    release.switch_data(s["commit"], str(s["new"]))
    release.rollback_data(s["commit"])
    assert Path(read_environment(s["env"])["MART_DIR"]) == s["old"]
    assert _switch_records(s)[-1]["how"] == "rollback"
    release.rollback_data(s["commit"])
    assert Path(read_environment(s["env"])["MART_DIR"]) == s["new"]
    assert [record["how"] for record in _switch_records(s)] == ["switch", "rollback", "rollback"]


def test_rollback_data_requires_a_record(data_server: dict) -> None:
    """没有换过资料时明确拒绝回退，不修改服务器状态。"""
    s = data_server
    with pytest.raises(ValueError, match="这个提交上没有换过资料，没有可回退的"):
        release.rollback_data(s["commit"])
    assert s["calls"] == []


@pytest.mark.parametrize("stage", ["backup", "write", "restart"])
def test_switch_data_step_failures_restore_environment(data_server: dict, monkeypatch, stage: str) -> None:
    """备份、配置写入或首次重启异常，运行配置都是原文、保留原始异常；改过配置的才重启恢复，备份失败不碰服务。"""
    s = data_server
    original = s["env"].read_bytes()
    error = RuntimeError("test step failure")
    def fail(*args, **kwargs):
        raise error
    if stage == "backup":
        monkeypatch.setattr(release, "as_app", fail)
    elif stage == "write":
        def partial_write(*args, **kwargs):
            s["env"].write_text("partial write")
            raise error
        monkeypatch.setattr(release, "write_profile", partial_write)
    else:
        def restart(*args, **kwargs):
            s["calls"].append(("run", args))
            if s["calls"].count(("run", args)) == 1:
                raise error
        monkeypatch.setattr(release, "run", restart)
    with pytest.raises(RuntimeError) as caught:
        release.switch_data(s["commit"], str(s["new"]))
    assert caught.value is error and s["env"].read_bytes() == original
    restarted = ("run", ("systemctl", "restart", "agentflow")) in s["calls"]
    assert restarted == (stage != "backup")
    assert _switch_records(s) == []


def test_switch_data_retries_then_succeeds(data_server: dict, monkeypatch) -> None:
    """模型预热期间健康检查暂时失败时，两秒后重试即可成功。"""
    s = data_server
    health = release.health
    attempts, sleeps = [], []
    def delayed(expected: dict) -> dict:
        attempts.append(expected)
        if len(attempts) == 1:
            raise RuntimeError("warming up")
        return health(expected)
    monkeypatch.setattr(release, "health", delayed)
    monkeypatch.setattr(release.time, "sleep", sleeps.append)
    release.switch_data(s["commit"], str(s["new"]))
    assert len(attempts) == 2 and sleeps == [2]
    assert len(_switch_records(s)) == 1


@pytest.mark.parametrize("stage", ["switch-data", "rollback-data"])
def test_data_commands_dispatch(data_server: dict, monkeypatch, stage: str) -> None:
    """命令行把资料目录和执行层传给新阶段，回退仅使用当前提交。"""
    s = data_server
    calls = []
    monkeypatch.setattr(release.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(release.os, "umask", lambda mask: 0)
    monkeypatch.setattr(release, "switch_data", lambda *args: calls.append(args))
    monkeypatch.setattr(release, "rollback_data", lambda *args: calls.append(args))
    arguments = ["release.py", s["commit"], stage]
    if stage == "switch-data":
        arguments += ["--mart-dir", str(s["new"]), "--executor", "none"]
    monkeypatch.setattr(sys, "argv", arguments)
    release.main()
    assert calls == ([(s["commit"], str(s["new"]), "none")] if stage == "switch-data" else [(s["commit"],)])


def test_switch_data_command_requires_mart(data_server: dict, monkeypatch, capsys) -> None:
    """缺少资料目录的命令在解析阶段退出。"""
    monkeypatch.setattr(release.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(release.os, "umask", lambda mask: 0)
    monkeypatch.setattr(sys, "argv", ["release.py", data_server["commit"], "switch-data"])
    with pytest.raises(SystemExit) as caught:
        release.main()
    assert caught.value.code == 2
    assert "switch-data 必须提供 --mart-dir" in capsys.readouterr().err


@pytest.mark.parametrize("modern", [False, True])
def test_install_uses_only_standard_library(tmp_path: Path, modern: bool) -> None:
    """系统 Python 隔离进程可安装新旧包，系统权限与链接操作用替身代替。"""
    database = tmp_path / "metadata.db"
    _metadata(database, "test-v1")
    archive = tmp_path / "dataset.tar"
    _dataset_bundle(archive, database, {"version_id": "test-v1"} if modern else None,
                    {"version_id": "test-v1"} if modern else {})
    script = Path(__file__).resolve().parents[1] / "deploy/dataset.py"
    code = """
import sys, runpy, os
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(sys.argv[1]).parent))
module = runpy.run_path(sys.argv[1])
root = Path(sys.argv[3]) / 'datasets'
root.parent.mkdir()
install = module['install_dataset']
install.__globals__['Path'] = lambda value: root if value == '/var/lib/agentflow/datasets' else Path(value)
os.geteuid = lambda: 0
os.chown = lambda *args: None
sys.modules['grp'] = SimpleNamespace(getgrnam=lambda name: SimpleNamespace(gr_gid=0))
Path.symlink_to = lambda self, target: self.write_text(str(target))
result = install(Path(sys.argv[2]), 'private-test', root.parent / 'history-test.db')
assert result['version_id'] == 'test-v1'
assert (root / 'private-test/build/metadata.db').is_file()
assert not any(n == 'metadata' or n.startswith('metadata.') for n in sys.modules)
print(result['version_id'])
"""
    result = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(script), str(archive),
                             str(tmp_path / "server")], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "test-v1"
