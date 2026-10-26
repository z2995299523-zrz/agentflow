"""版本归档、切换、回退与命令行的离线测试。"""
import hashlib
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from metadata import importer, versions
from metadata.index import HashEmbedder
from metadata.store import MetadataStore, read_version_id
from tests.helpers import write_mart

NOW = datetime(2026, 10, 23, 14, 50, tzinfo=timezone(timedelta(hours=8)))


def read_json(path: Path) -> dict:
    """读取测试中的 JSON 文件。"""
    return json.loads(path.read_text(encoding="utf-8"))


def current(mart_dir: Path) -> dict:
    """读取当前活动记录。"""
    return read_json(mart_dir / "build" / "current.json")


def test_inputs_fingerprint_matches_registered_version(tiny_mart: Path) -> None:
    """公开指纹与实际导入的版本登记一致。"""
    result = versions.import_version(tiny_mart, now=NOW)
    manifest = read_json(result.version_dir / "version.json")
    assert versions.inputs_fingerprint(tiny_mart) == (manifest["inputs"], manifest["inputs_digest"])
    assert versions.active_manifest(tiny_mart / "build") == manifest


def test_active_manifest_without_active_version(tmp_path: Path) -> None:
    """没有指针文件或指针未登记活动版本时返回 None。"""
    assert versions.active_manifest(tmp_path) is None
    for value in ({}, {"active": None}):
        (tmp_path / "current.json").write_text(json.dumps(value), encoding="utf-8")
        assert versions.active_manifest(tmp_path) is None


def active_files(mart_dir: Path) -> dict[str, bytes]:
    """保存活动文件原始字节，用于检查失败或候选导入没有改变它们。"""
    return {name: (mart_dir / "build" / name).read_bytes()
            for name in ("metadata.db", "import_report.md", "current.json")}


def test_first_import_archives_inputs_and_promotes(tiny_mart):
    result = versions.import_version(tiny_mart, now=NOW)

    assert result.version_id == NOW.astimezone().strftime("%Y%m%d-%H%M")
    assert result.promoted and not result.skipped and result.previous is None
    assert result.active == result.version_id
    manifest = read_json(result.version_dir / "version.json")
    assert manifest["status"] == "ready" and manifest["baseline"] is None
    assert manifest["counts"] == result.report.counts == MetadataStore(result.report.db_path).counts()
    assert {name: manifest["counts"][name] for name in ("表", "字段", "字段向量")} == {
        "表": 3, "字段": 9, "字段向量": 9}
    assert manifest["embedder"] == "hash-bigram-512" and manifest["dim"] == 512
    assert manifest["errors"] == manifest["warnings"] == 0 and manifest["error"] is None
    assert datetime.fromisoformat(manifest["created_at"]).tzinfo is not None
    assert len(manifest["inputs_digest"]) == len(manifest["code_digest"]) == 64
    assert set(manifest["inputs"]) == {"表清单.csv", "字段清单.csv", "血缘清单.csv", "关联关系.csv"}
    for name, digest in manifest["inputs"].items():
        raw = (tiny_mart / name).read_bytes()
        assert digest == hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()
        assert (result.version_dir / "inputs" / name).read_bytes() == raw
    assert f"- 版本：{result.version_id}" in (result.version_dir / "import_report.md").read_text(encoding="utf-8")
    assert (tiny_mart / "build" / "metadata.db").read_bytes() == result.report.db_path.read_bytes()
    assert MetadataStore(tiny_mart / "build" / "metadata.db").version_id == result.version_id
    assert current(tiny_mart)["active"] == result.version_id
    assert current(tiny_mart)["previous"] is None
    assert current(tiny_mart)["history"][-1]["how"] == "import"


def test_changed_inputs_and_two_rollbacks(tiny_mart, tiny_sheets):
    first = versions.import_version(tiny_mart, now=NOW)
    first_bytes = first.report.db_path.read_bytes()
    tiny_sheets["字段清单"][3][2] = "账户余额"
    write_mart(tiny_mart, tiny_sheets)
    second = versions.import_version(tiny_mart, now=NOW + timedelta(minutes=1))

    assert second.previous == first.version_id and second.active == second.version_id
    assert read_json(second.version_dir / "version.json")["baseline"] == first.version_id
    assert versions.rollback(tiny_mart / "build", now=NOW) == first.version_id
    assert (tiny_mart / "build" / "metadata.db").read_bytes() == first_bytes
    assert current(tiny_mart)["previous"] == second.version_id
    assert current(tiny_mart)["history"][-1]["how"] == "rollback"
    assert versions.rollback(tiny_mart / "build") == second.version_id
    assert current(tiny_mart)["previous"] == first.version_id


def test_candidate_does_not_change_active_until_promoted(tiny_mart):
    first = versions.import_version(tiny_mart, now=NOW)
    before = active_files(tiny_mart)
    candidate = versions.import_version(tiny_mart, force=True, promote=False, now=NOW)

    assert candidate.active == first.version_id and not candidate.promoted
    assert active_files(tiny_mart) == before
    rows = versions.list_versions(tiny_mart / "build")
    assert [(row.version_id, row.label) for row in rows] == [
        (candidate.version_id, "候选"), (first.version_id, "活动")]
    assert versions.promote(tiny_mart / "build", candidate.version_id) == first.version_id
    assert (tiny_mart / "build" / "metadata.db").read_bytes() == candidate.report.db_path.read_bytes()
    assert current(tiny_mart)["active"] == candidate.version_id


def test_failed_validation_preserves_all_active_files(tiny_mart, tiny_sheets):
    versions.import_version(tiny_mart, now=NOW)
    before = active_files(tiny_mart)
    tiny_sheets["表清单"][0][0] = "明细层"
    write_mart(tiny_mart, tiny_sheets)
    failed = versions.import_version(tiny_mart, now=NOW)

    assert not failed.promoted and not failed.report.succeeded
    assert [issue.rule for issue in failed.report.errors] == ["M05"]
    manifest = read_json(failed.version_dir / "version.json")
    assert manifest["status"] == "failed" and manifest["errors"] == 1 and manifest["error"] is None
    assert (failed.version_dir / "import_report.md").is_file()
    assert not (failed.version_dir / "metadata.db").exists()
    assert active_files(tiny_mart) == before


def test_unchanged_skips_but_force_and_reindex_create_versions(tiny_mart):
    first = versions.import_version(tiny_mart, now=NOW)
    before = active_files(tiny_mart)
    skipped = versions.import_version(tiny_mart, now=NOW)
    assert skipped.skipped and skipped.report is None and skipped.version_id == first.version_id
    assert active_files(tiny_mart) == before
    assert len(versions.list_versions(tiny_mart / "build")) == 1
    forced = versions.import_version(tiny_mart, force=True, now=NOW)
    reindexed = versions.import_version(tiny_mart, reindex=True, now=NOW)
    assert forced.version_id == first.version_id + "-2"
    assert reindexed.version_id == first.version_id + "-3"
    assert "全部新算（加了 --reindex）" in reindexed.report.to_markdown()


def test_adopts_legacy_with_identical_metadata_and_vectors(tiny_mart):
    old_report = importer.run_import(tiny_mart)
    old = MetadataStore(old_report.db_path)
    result = versions.import_version(tiny_mart, now=NOW)
    legacy = result.previous

    assert legacy.startswith("legacy-")
    manifest = read_json(tiny_mart / "build" / "versions" / legacy / "version.json")
    assert manifest["inputs"] == {} and manifest["inputs_digest"] is None
    assert manifest["code_digest"] is None and manifest["git_commit"] is None
    assert current(tiny_mart)["history"][0]["how"] == "legacy"
    assert versions.rollback(tiny_mart / "build") == legacy
    restored = MetadataStore(old_report.db_path)
    assert restored.version_id == legacy and restored.counts() == old.counts()
    assert restored.tables() == old.tables()
    for table in old.tables():
        assert restored.columns(table.table_id) == old.columns(table.table_id)
    assert np.array_equal(restored.vectors("field").matrix, old.vectors("field").matrix)
    assert restored.vectors("field").texts == old.vectors("field").texts


class RecordingHash(HashEmbedder):
    """记录文本，验证候选版本能补足活动库里没有的向量。"""

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    def embed(self, texts: list[str]) -> np.ndarray:
        """记录后调用纯哈希向量器。"""
        self.texts.extend(texts)
        return super().embed(texts)


def test_reuses_latest_candidate_vectors(tiny_mart, tiny_sheets):
    first = versions.import_version(tiny_mart, now=NOW)
    tiny_sheets["字段清单"][3][2] = "账户余额"
    write_mart(tiny_mart, tiny_sheets)
    recording = RecordingHash()
    candidate = versions.import_version(tiny_mart, promote=False, embedder=recording, now=NOW)
    assert any(text.endswith("账户余额") for text in recording.texts)
    assert candidate.active == first.version_id
    tiny_sheets["表清单"][0][7] = "补充表说明，不影响向量文本"
    write_mart(tiny_mart, tiny_sheets)
    recording.texts.clear()

    result = versions.import_version(tiny_mart, embedder=recording, now=NOW)

    assert result.promoted and recording.texts == []
    assert "全部复用，没有调用向量器" in result.report.to_markdown()


def test_prune_protects_active_previous_and_retains_latest_failed(tiny_mart, tiny_sheets):
    ready = [versions.import_version(tiny_mart, build_index=False, force=True, now=NOW + timedelta(minutes=i))
             for i in range(8)]
    tiny_sheets["表清单"][0][0] = "明细层"
    write_mart(tiny_mart, tiny_sheets)
    failed = [versions.import_version(tiny_mart, build_index=False, force=True,
                                     now=NOW + timedelta(minutes=8+i)) for i in range(4)]
    build = tiny_mart / "build"
    versions.promote(build, ready[0].version_id)
    expected = {ready[1].version_id, ready[2].version_id, failed[0].version_id}

    assert set(versions.prune(build, keep=5)) == expected
    assert len(versions.list_versions(build)) == 12
    assert set(versions.prune(build, keep=5, apply=True)) == expected
    rows = versions.list_versions(build)
    assert len(rows) == 9 and sum(row.status == "failed" for row in rows) == 3
    assert {row.version_id for row in rows if row.label in ("活动", "上一个活动")} == {
        ready[0].version_id, ready[-1].version_id}
    assert any(row.label == "用过" for row in rows)


def test_promote_exclusive_lock_and_invalid_versions(tiny_mart, tiny_sheets):
    first = versions.import_version(tiny_mart, now=NOW)
    candidate = versions.import_version(tiny_mart, force=True, promote=False, now=NOW)
    before = active_files(tiny_mart)
    build = tiny_mart / "build"
    lock = build / ".promote.lock"
    lock.write_text("another process", encoding="utf-8")
    with pytest.raises(RuntimeError, match="另一个切换正在进行"):
        versions.promote(build, candidate.version_id)
    assert lock.read_text(encoding="utf-8") == "another process"
    assert active_files(tiny_mart) == before
    lock.unlink()
    assert versions.promote(build, candidate.version_id) == first.version_id
    assert not lock.exists()
    with pytest.raises(ValueError, match="版本目录不存在"):
        versions.promote(build, "missing")
    tiny_sheets["表清单"][0][0] = "明细层"
    write_mart(tiny_mart, tiny_sheets)
    failed = versions.import_version(tiny_mart, now=NOW)
    with pytest.raises(ValueError, match="不是 ready"):
        versions.promote(build, failed.version_id)


def test_promoting_same_version_preserves_previous_and_limits_history(tiny_mart, monkeypatch):
    first = versions.import_version(tiny_mart, now=NOW)
    second = versions.import_version(tiny_mart, force=True, now=NOW)
    monkeypatch.setattr(versions, "HISTORY_LIMIT", 3)
    for _ in range(5):
        assert versions.promote(tiny_mart / "build", second.version_id) == second.version_id
    assert current(tiny_mart)["previous"] == first.version_id
    assert len(current(tiny_mart)["history"]) == 3


@pytest.mark.parametrize("missing", ["version.json", "metadata.db"])
def test_promote_reports_missing_files(tiny_mart, missing):
    result = versions.import_version(tiny_mart, now=NOW)
    (result.version_dir / missing).unlink()
    with pytest.raises(ValueError, match="不存在"):
        versions.promote(tiny_mart / "build", result.version_id)


def test_permission_error_keeps_old_database_and_releases_lock(tiny_mart, monkeypatch):
    versions.import_version(tiny_mart, now=NOW)
    candidate = versions.import_version(tiny_mart, force=True, promote=False, now=NOW)
    before = active_files(tiny_mart)
    original = versions.os.replace

    def occupied(source, target):
        if Path(target).name == "metadata.db":
            raise PermissionError("占用")
        original(source, target)

    monkeypatch.setattr(versions.os, "replace", occupied)
    with pytest.raises(RuntimeError, match="正被其它程序占用"):
        versions.promote(tiny_mart / "build", candidate.version_id)
    assert active_files(tiny_mart) == before
    assert not (tiny_mart / "build" / ".promote.lock").exists()
    assert not (tiny_mart / "build" / "metadata.db.next").exists()


def test_adopt_repairs_current_from_database(tiny_mart):
    first = versions.import_version(tiny_mart, now=NOW)
    second = versions.import_version(tiny_mart, force=True, now=NOW)
    build = tiny_mart / "build"
    shutil.copyfile(first.report.db_path, build / "metadata.db")
    versions.adopt_active(build, NOW)
    record = current(tiny_mart)
    assert record["active"] == first.version_id and record["previous"] == first.version_id
    assert record["history"][-1]["how"] == "fix"
    assert len(versions.list_versions(build)) == 2
    (build / "current.json").unlink()
    versions.adopt_active(build, NOW)
    assert current(tiny_mart)["active"] == first.version_id
    assert second.report.db_path.is_file()


def test_direct_overwrite_is_adopted_as_new_legacy(tiny_mart):
    first = versions.import_version(tiny_mart, now=NOW)
    importer.run_import(tiny_mart)
    versions.adopt_active(tiny_mart / "build", NOW)
    assert current(tiny_mart)["active"].startswith("legacy-")
    assert current(tiny_mart)["previous"] == first.version_id


def test_bad_active_database_is_left_for_import_to_replace(tiny_mart):
    build = tiny_mart / "build"
    build.mkdir()
    db = build / "metadata.db"
    db.write_bytes(b"broken database")
    versions.adopt_active(build, NOW)
    assert db.read_bytes() == b"broken database"
    assert not (build / "versions").exists()
    result = versions.import_version(tiny_mart, now=NOW)
    assert result.promoted and result.previous is None


@pytest.mark.parametrize("exception", [FileNotFoundError, ValueError, RuntimeError])
def test_import_exceptions_are_recorded_and_reraised(tiny_mart, monkeypatch, exception):
    versions.import_version(tiny_mart, now=NOW)
    before = active_files(tiny_mart)
    failure = exception("测试导入异常")

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(importer, "run_import", fail)
    with pytest.raises(exception, match="测试导入异常") as caught:
        versions.import_version(tiny_mart, force=True, now=NOW)
    assert caught.value is failure and active_files(tiny_mart) == before
    manifest = read_json(versions.list_versions(tiny_mart / "build")[0].version_dir / "version.json")
    assert manifest["status"] == "failed" and manifest["error"] == "测试导入异常"


def test_missing_mart_does_not_create_directories(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(FileNotFoundError, match="资料目录不存在"):
        versions.import_version(missing)
    assert not missing.exists()
    assert versions.list_versions(tmp_path / "build") == []
    with pytest.raises(ValueError, match="没有上一个活动版本"):
        versions.rollback(tmp_path / "build")


def test_input_selection_snapshots_sql_and_normalizes_line_endings(tiny_mart):
    for name in ("scripts/a.sql", "examples/b.sql", "转换报告.md"):
        path = tiny_mart / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"-- comment\r\n")
    (tiny_mart / "scripts" / "ignore.txt").write_text("ignored", encoding="utf-8")
    names = versions.input_files(tiny_mart)
    assert names == sorted(names) and len(names) == 7
    first = versions.import_version(tiny_mart, build_index=False, now=NOW)
    for name in names:
        assert (first.version_dir / "inputs" / name).read_bytes() == (tiny_mart / name).read_bytes()
        path = tiny_mart / name
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
    assert versions.import_version(tiny_mart, build_index=False, now=NOW).skipped
    (tiny_mart / "metadata.xlsx").write_bytes(b"only testing file selection")
    assert versions.input_files(tiny_mart) == sorted([
        "metadata.xlsx", "scripts/a.sql", "examples/b.sql", "转换报告.md"])


def test_code_change_and_embedder_change_prevent_skip(tiny_mart, monkeypatch):
    versions.import_version(tiny_mart, now=NOW)
    changed = versions.import_version(tiny_mart, embedder=HashEmbedder(256), now=NOW)
    assert not changed.skipped
    monkeypatch.setattr(versions, "_code_fingerprint", lambda: ("changed-code", None))
    code = versions.import_version(tiny_mart, embedder=HashEmbedder(256), now=NOW)
    assert not code.skipped
    assert read_json(code.version_dir / "version.json")["git_commit"] is None


def test_no_index_never_resolves_embedder_and_git_failure_is_optional(tiny_mart, monkeypatch):
    def forbidden():
        raise AssertionError("不该解析向量器")

    def no_git(*args, **kwargs):
        raise FileNotFoundError("没有 Git")

    monkeypatch.setattr(versions, "default_embedder", forbidden)
    monkeypatch.setattr(versions.subprocess, "run", no_git)
    result = versions.import_version(tiny_mart, build_index=False, now=NOW)
    manifest = read_json(result.version_dir / "version.json")
    assert manifest["embedder"] is manifest["dim"] is manifest["git_commit"] is None
    assert versions.import_version(tiny_mart, build_index=False).skipped


def test_default_embedder_is_resolved_once_and_reused_by_importer(tiny_mart, monkeypatch):
    resolved = []

    def default():
        selected = HashEmbedder()
        resolved.append(selected)
        return selected

    def forbidden():
        raise AssertionError("run_import 不该再次解析向量器")

    monkeypatch.setattr(versions, "default_embedder", default)
    monkeypatch.setattr(importer, "default_embedder", forbidden)
    assert versions.import_version(tiny_mart, now=NOW).promoted
    assert len(resolved) == 1


def test_mismatching_archive_version_cannot_replace_active(tiny_mart):
    first = versions.import_version(tiny_mart, now=NOW)
    candidate = versions.import_version(tiny_mart, force=True, promote=False, now=NOW)
    before = active_files(tiny_mart)
    shutil.copyfile(first.report.db_path, candidate.report.db_path)
    with pytest.raises(RuntimeError, match="版本号与.*不一致"):
        versions.promote(tiny_mart / "build", candidate.version_id)
    assert active_files(tiny_mart) == before
    assert not (tiny_mart / "build" / ".promote.lock").exists()


def test_commands_and_output(tiny_mart, capsys, monkeypatch):
    mart = ["--mart", str(tiny_mart)]
    assert versions.main(["list", *mart]) == 0
    assert "还没有版本" in capsys.readouterr().out
    assert versions.main(["rollback", *mart]) == 1
    assert "失败: 没有上一个活动版本" in capsys.readouterr().out
    first = versions.import_version(tiny_mart, now=NOW)
    second = versions.import_version(tiny_mart, force=True, now=NOW)
    versions.import_version(tiny_mart, force=True, promote=False, now=NOW)
    assert versions.main(["list", *mart]) == 0
    output = capsys.readouterr().out
    assert "版本 | 导入时间 | 状态 | 表 | 字段 | 字段向量" in output
    assert "候选" in output and "活动" in output and "上一个活动" in output
    assert versions.main(["promote", first.version_id, *mart]) == 0
    assert f"已切换到 {first.version_id}（上一个：{second.version_id}）" in capsys.readouterr().out
    assert versions.main(["rollback", *mart]) == 0
    assert f"已回退到 {second.version_id}" in capsys.readouterr().out
    assert versions.main(["prune", "--keep", "0", *mart]) == 0
    assert "加 --yes 才会删除" in capsys.readouterr().out
    assert len(versions.list_versions(tiny_mart / "build")) == 3
    assert versions.main(["prune", "--keep", "0", "--yes", *mart]) == 0
    assert "已删除 1 个版本" in capsys.readouterr().out
    assert versions.main(["promote", "missing", *mart]) == 1
    assert "失败:" in capsys.readouterr().out
    assert versions.main(["prune", "--keep", "-1", *mart]) == 1
    assert "不能为负数" in capsys.readouterr().out
    import config

    monkeypatch.setattr(config, "MART_DIR", str(tiny_mart))
    assert versions.main(["list"]) == 0
    assert versions.main([*mart, "list"]) == 0
    assert versions.main(["unknown"]) == 2


def test_importer_flags_skipped_and_candidate_message(tiny_mart, capsys):
    assert importer.main([str(tiny_mart)]) == 0
    assert "这是第一个版本" in capsys.readouterr().out
    assert importer.main([str(tiny_mart)]) == 0
    output = capsys.readouterr().out
    assert "资料和代码都没变" in output and "没有生成新版本" in output and "元数据导入报告" not in output
    assert importer.main([str(tiny_mart), "--force", "--no-promote"]) == 0
    output = capsys.readouterr().out
    assert "是候选版本，没有切换" in output and "python -m metadata.versions promote" in output
    assert importer.main([str(tiny_mart), "--force"]) == 0
    assert "上一个：" in capsys.readouterr().out


def test_list_skips_unregistered_directories_and_prune_rejects_escape(tiny_mart):
    result = versions.import_version(tiny_mart, now=NOW)
    (result.version_dir.parent / "partial-import").mkdir()
    assert len(versions.list_versions(tiny_mart / "build")) == 1
    with pytest.raises(ValueError, match="版本号无效"):
        versions.promote(tiny_mart / "build", "../outside")


def test_interrupted_import_is_listed_as_failed_and_can_be_pruned(tiny_mart, monkeypatch):
    first = versions.import_version(tiny_mart, now=NOW)
    before = active_files(tiny_mart)

    class Interrupted(BaseException):
        """模拟导入中途按了 Ctrl+C：不是普通异常，import_version 接不住"""

    def interrupt(*args, **kwargs):
        raise Interrupted

    monkeypatch.setattr(importer, "run_import", interrupt)
    with pytest.raises(Interrupted):
        versions.import_version(tiny_mart, force=True, now=NOW + timedelta(minutes=1))

    assert active_files(tiny_mart) == before
    rows = versions.list_versions(tiny_mart / "build")
    assert [row.label for row in rows] == ["失败", "活动"]                  # 没跑完的这一版看得见
    manifest = read_json(rows[0].version_dir / "version.json")
    assert manifest["status"] == "failed" and manifest["error"] == versions.UNFINISHED
    assert versions.prune(tiny_mart / "build", keep_failed=0, apply=True) == [rows[0].version_id]
    assert [row.version_id for row in versions.list_versions(tiny_mart / "build")] == [first.version_id]


def test_sql_files_count_as_inputs_whatever_the_case_of_the_suffix(tiny_mart):
    (tiny_mart / "scripts").mkdir()
    (tiny_mart / "scripts" / "LOAD_CUST.SQL").write_text("-- 后缀大写\n", encoding="utf-8")

    assert "scripts/LOAD_CUST.SQL" in versions.input_files(tiny_mart)


def remove_customer(tiny_mart: Path, tiny_sheets: dict) -> None:
    """删除无血缘依赖的客户维表及其手工关联，保留其余资料的合法性。"""
    tiny_sheets["表清单"] = [r for r in tiny_sheets["表清单"] if r[1:3] != ["ods", "cust"]]
    tiny_sheets["字段清单"] = [r for r in tiny_sheets["字段清单"] if r[0] != "ods.cust"]
    tiny_sheets["关联关系"].clear()
    write_mart(tiny_mart, tiny_sheets)


def test_pending_import_accept_and_rollback(tiny_mart, tiny_sheets):
    first = versions.import_version(tiny_mart, now=NOW)
    before = active_files(tiny_mart)
    remove_customer(tiny_mart, tiny_sheets)
    candidate = versions.import_version(tiny_mart, now=NOW)
    assert not candidate.promoted and candidate.active == first.version_id
    assert active_files(tiny_mart) == before
    assert candidate.confirm_items[0] == "删了 1 张表、2 个字段"
    assert candidate.diff_report == candidate.version_dir / "diff_report.md"
    text = candidate.diff_report.read_text(encoding="utf-8")
    assert f"- 基线：{first.version_id}" in text and "## 影响面（零成本回归）" in text
    assert "输入文件内容变了：" in text and "代码：没有变化" in text
    manifest = read_json(candidate.version_dir / "version.json")
    assert manifest["status"] == "ready" and manifest["accepted_at"] is None
    assert manifest["confirm_items"] == candidate.confirm_items
    assert set(manifest["gates"]) == {"清单校验", "样例", "差异", "题库静态核对", "召回重放", "会话影响"}
    with pytest.raises(ValueError, match="有 .* 项需要确认.*--accept"):
        versions.promote(tiny_mart / "build", candidate.version_id)
    assert active_files(tiny_mart) == before
    assert versions.promote(tiny_mart / "build", candidate.version_id, accept=True, now=NOW) == first.version_id
    accepted = read_json(candidate.version_dir / "version.json")["accepted_at"]
    assert datetime.fromisoformat(accepted).tzinfo is not None
    assert versions.rollback(tiny_mart / "build") == first.version_id
    assert versions.rollback(tiny_mart / "build") == candidate.version_id
    assert read_json(candidate.version_dir / "version.json")["accepted_at"] == accepted


def test_description_change_still_promotes_and_first_import_gates(tiny_mart, tiny_sheets):
    first = versions.import_version(tiny_mart, now=NOW)
    manifest = read_json(first.version_dir / "version.json")
    assert manifest["confirm_items"] == [] and manifest["accepted_at"] is None
    assert set(manifest["gates"]) == {"清单校验", "样例"} and first.diff_report is None
    tiny_sheets["表清单"][0][-1] = "补充说明"
    write_mart(tiny_mart, tiny_sheets)
    second = versions.import_version(tiny_mart, now=NOW)
    assert second.promoted and not second.confirm_items
    assert "description：账户每日快照 → 补充说明" in second.diff_report.read_text(encoding="utf-8")


def test_regression_failure_does_not_fail_import(tiny_mart):
    versions.import_version(tiny_mart, now=NOW)
    before = active_files(tiny_mart)
    (tiny_mart / "eval_questions.yaml").write_text("invalid question format", encoding="utf-8")
    candidate = versions.import_version(tiny_mart, force=True, now=NOW)
    assert candidate.report.succeeded and not candidate.promoted
    assert candidate.confirm_items[0].startswith("零成本回归没跑完：")
    assert active_files(tiny_mart) == before
    assert read_json(candidate.version_dir / "version.json")["status"] == "ready"


def test_regression_unexpected_failure_is_also_pending(tiny_mart, monkeypatch):
    from eval import regression

    versions.import_version(tiny_mart, now=NOW)
    def fail(*args, **kwargs):
        raise RuntimeError("回归入口故障")
    monkeypatch.setattr(regression, "run_regression", fail)
    candidate = versions.import_version(tiny_mart, force=True, now=NOW)
    assert candidate.confirm_items == ["零成本回归没跑完：回归入口故障"]
    assert candidate.report.succeeded and not candidate.promoted and candidate.diff_report.is_file()


def test_legacy_manifest_missing_gates_remains_compatible(tiny_mart):
    result = versions.import_version(tiny_mart, now=NOW)
    path = result.version_dir / "version.json"
    manifest = read_json(path)
    for name in ("confirm_items", "accepted_at", "gates"):
        manifest.pop(name)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert versions.promote(tiny_mart / "build", result.version_id) == result.version_id
    assert versions.import_version(tiny_mart).skipped


def test_diff_cli_ready_only_and_accept_cli(tiny_mart, tiny_sheets, capsys):
    first = versions.import_version(tiny_mart, now=NOW)
    remove_customer(tiny_mart, tiny_sheets)
    candidate = versions.import_version(tiny_mart, now=NOW)
    mart = ["--mart", str(tiny_mart)]
    assert versions.main(["diff", first.version_id, candidate.version_id, *mart]) == 0
    text = capsys.readouterr().out
    assert "# 元数据差异报告" in text and "ods.cust" in text and "## 影响面" not in text
    assert versions.main(["promote", candidate.version_id, *mart]) == 1
    assert "--accept" in capsys.readouterr().out
    assert versions.main(["promote", candidate.version_id, "--accept", *mart]) == 0
    assert current(tiny_mart)["active"] == candidate.version_id
    assert read_json(candidate.version_dir / "version.json")["accepted_at"]
    assert versions.main(["diff", "missing", candidate.version_id, *mart]) == 1
    tiny_sheets["表清单"][0][0] = "错误层级"
    write_mart(tiny_mart, tiny_sheets)
    failed = versions.import_version(tiny_mart, now=NOW)
    assert versions.main(["diff", candidate.version_id, failed.version_id, *mart]) == 1
    assert "不是 ready" in capsys.readouterr().out


def test_invalid_example_input_decoding_and_confirmation(tiny_mart, tiny_sheets):
    path = tiny_mart / "examples" / "EXAMPLE.SQL"
    path.parent.mkdir()
    path.write_text("-- 需求: 客户名称\nSELECT cust_nm FROM ods.cust;\n", encoding="gb18030")
    versions.import_version(tiny_mart, now=NOW)
    tiny_sheets["字段清单"] = [r for r in tiny_sheets["字段清单"] if r[:2] != ["ods.cust", "cust_nm"]]
    write_mart(tiny_mart, tiny_sheets)
    candidate = versions.import_version(tiny_mart, now=NOW)
    assert "失效样例 1 条" in candidate.confirm_items
    assert "客户名称：R4" in candidate.diff_report.read_text(encoding="utf-8")


@pytest.mark.parametrize("replay,kind,loaded,expected", [
    (False, "bge", False, False), (True, "bge", False, True),
    (False, "bge", True, True), (False, "hash", False, True), (False, "none", False, True)])
def test_replay_decision_never_loads_bge(tiny_mart, monkeypatch, replay, kind, loaded, expected):
    from eval import regression
    from metadata import index

    versions.import_version(tiny_mart, now=NOW)
    if kind == "bge":
        monkeypatch.setattr(MetadataStore, "embedder_name", property(lambda self: index.BGE_NAME))
    elif kind == "none":
        monkeypatch.setattr(MetadataStore, "embedder_name", property(lambda self: None))
    monkeypatch.setattr(versions, "bge_loaded", lambda: loaded)
    def forbidden(*args):
        raise AssertionError("不允许加载 BGE")
    monkeypatch.setattr(index, "_load_bge_model", forbidden)
    monkeypatch.setattr(regression, "embedder_for", forbidden)
    received = []
    def run(*args, **kwargs):
        received.append(kwargs["replay"])
        return regression.RegressionResult(gates={"召回重放": "测试已替代重放"})
    monkeypatch.setattr(regression, "run_regression", run)
    result = versions.import_version(tiny_mart, force=True, now=NOW, replay=replay)
    assert result.promoted and received == [expected]


def test_header_limits_and_git_commits():
    old = {"version_id": "a", "inputs": {"gone": "old", "changed": "old"}, "code_digest": "a", "git_commit": "123456789"}
    new = {"version_id": "b", "baseline": "a", "inputs": {"changed": "new", **{f"file{i}": "x" for i in range(25)}},
           "code_digest": "b", "git_commit": "987654321"}
    text = "\n".join(versions._diff_header(old, new))
    assert "等共 25 项" in text and "1234567 → 9876543" in text
    assert "输入文件删除：gone" in text and "输入文件内容变了：changed" in text
    assert "基线没有输入记录（旧库登记的版本）" in "\n".join(versions._diff_header({}, new))
