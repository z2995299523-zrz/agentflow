"""流水线装配：向量器还原、按资料目录建流水线、进程内缓存、字段链路的描述"""
import json
import os
import threading
from datetime import date
from pathlib import Path

import pytest

import config
from metadata import index
from metadata.importer import default_db_path, run_import
from metadata.index import BgeEmbedder, HashEmbedder
from metadata.models import Mart
from metadata.store import MetadataStore, VectorBundle
from nl2sql import service
from nl2sql.llm import ScriptedLLM
from nl2sql.schemas import Metric, ParsedRequest, PlanField, PlanTable, QueryPlan, QueryRequest, TimeSpec

BAL = "ads.cust_bal"                           # tiny 资料里的专题层汇总表
TINY_SQL = f"SELECT SUM(bal) AS bal FROM {BAL} WHERE data_dt = DATE '2026-09-30'"


def store_with_vectors(embedder: str, dim: int) -> MetadataStore:
    """只有向量器信息、没有任何表的元数据库：embedder_for 只看向量器的名字"""
    return MetadataStore.from_mart(Mart(), VectorBundle(embedder=embedder, dim=dim, sets={}))


@pytest.fixture
def imported_mart(tiny_mart):
    """用哈希向量器导入 tiny 资料，返回资料目录"""
    report = run_import(tiny_mart, embedder=HashEmbedder())
    assert report.succeeded
    return tiny_mart


@pytest.fixture(autouse=True)
def clean_caches():
    """每个测试前后都清缓存，免得进程内的缓存带到别的测试里"""
    service.reset_pipeline()
    yield
    service.reset_pipeline()


@pytest.fixture
def reload_control(monkeypatch: pytest.MonkeyPatch):
    """在第二次读库时用事件卡住后台线程，结束后确保线程退出"""
    load = service._load_metadata
    started, release = threading.Event(), threading.Event()
    calls = []

    def controlled_load(mart_dir: Path) -> tuple[MetadataStore, service.LineageGraph]:
        calls.append(mart_dir)
        if len(calls) == 2:
            started.set()
            assert release.wait(10), "测试没有放行后台读库"
        return load(mart_dir)

    monkeypatch.setattr(service, "_load_metadata", controlled_load)
    monkeypatch.setattr(service, "ChatModelClient", lambda **kwargs: ScriptedLLM([]))
    yield started, release, calls
    release.set()
    assert service.wait_for_reload(10), "后台换版本线程没有退出"


def advance_metadata_mtime(mart_dir: Path) -> None:
    """把活动元数据库的修改时间往后拨，避免文件系统时间精度影响测试"""
    db = default_db_path(mart_dir)
    stat = db.stat()
    os.utime(db, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))


def tiny_replies() -> list[str]:
    """tiny 资料上一轮查询的三个回复：解析、方案、SQL"""
    parsed = ParsedRequest(is_query=True, requirement="查询 2026-09-30 客户余额", metrics=[Metric(name="余额")],
                           time=TimeSpec(kind="point", dates=["2026-09-30"]))
    plan = QueryPlan(decision="ok", tables=[PlanTable(table=BAL)],
                     fields=[PlanField(table=BAL, column="bal", usage="metric"),
                             PlanField(table=BAL, column="data_dt", usage="filter")])
    return [parsed.model_dump_json(), plan.model_dump_json(), f"```sql\n{TINY_SQL}\n```"]


# ── embedder_for ──────────────────────────────────

def test_embedder_for_restores_the_embedder_the_store_was_built_with(mock_store, monkeypatch):
    def not_allowed():
        raise AssertionError("还原向量器时不能加载 BGE 模型")

    monkeypatch.setattr(index, "_load_bge_model", not_allowed)

    hashed = service.embedder_for(store_with_vectors("hash-bigram-256", 256))
    assert isinstance(hashed, HashEmbedder) and hashed.dim == 256 and hashed.name == "hash-bigram-256"
    assert isinstance(service.embedder_for(store_with_vectors("bge-small-zh-v1.5", 512)), BgeEmbedder)    # 不触发加载
    assert service.embedder_for(mock_store) is None                                  # 没有向量
    assert service.embedder_for(store_with_vectors("some-other-model", 8)) is None   # 不认识：召回自己会写明只走关键字路
    assert service.embedder_for(store_with_vectors("hash-bigram-xx", 8)) is None


# ── build_pipeline ────────────────────────────────

def test_build_pipeline_assembles_a_pipeline_that_answers_a_query(imported_mart):
    llm = ScriptedLLM(tiny_replies())

    pipeline = service.build_pipeline(imported_mart, llm=llm, executor_kind="none")
    result = pipeline.run(QueryRequest(question="查 9 月末的客户余额", today=date(2026, 10, 13)))

    assert result.status == "ok" and result.candidate_tables == [BAL]
    assert result.recall_notes == []                         # 向量器还原对了：向量路在工作，没有「只走关键字路」的说明
    assert pipeline.deps.mart_dir == imported_mart and pipeline.deps.executor.name == "none"
    assert pipeline.deps.llm is llm and llm.remaining == 0


def test_build_pipeline_with_the_duckdb_executor_uses_the_mock_mart(imported_mart, mock_db):
    pipeline = service.build_pipeline(imported_mart, llm=ScriptedLLM([]), executor_kind="duckdb")

    assert pipeline.deps.executor.name == "duckdb" and pipeline.deps.executor.db_path == mock_db
    with pytest.raises(ValueError, match="oracle"):
        service.build_pipeline(imported_mart, llm=ScriptedLLM([]), executor_kind="oracle")


def test_missing_metadata_db_tells_how_to_import_and_recovers_after_importing(tiny_mart, monkeypatch):
    with pytest.raises(FileNotFoundError, match="python -m metadata.importer") as error:
        service.build_pipeline(tiny_mart, llm=ScriptedLLM([]))
    assert str(tiny_mart) in str(error.value)

    monkeypatch.setattr(config, "MART_DIR", str(tiny_mart))
    with pytest.raises(FileNotFoundError, match="python -m metadata.importer"):
        service.get_metadata()
    run_import(tiny_mart, embedder=HashEmbedder())
    store, _ = service.get_metadata()                        # 失败不进缓存：导入之后不用重启就能用
    assert store.counts()["表"] == 3


# ── 进程内缓存 ────────────────────────────────────

def test_get_pipeline_is_cached_shared_across_threads_and_cleared_by_reset(imported_mart, monkeypatch):
    monkeypatch.setattr(config, "MART_DIR", str(imported_mart))
    created = []

    def fake_client(**kwargs):
        created.append(kwargs)
        return ScriptedLLM([])

    monkeypatch.setattr(service, "ChatModelClient", fake_client)
    found, barrier = [], threading.Barrier(8)

    def grab():
        barrier.wait(10)
        found.append(service.get_pipeline())

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    pipeline = found[0]
    assert all(item is pipeline for item in found) and len(created) == 1          # 并发时也只建一份
    assert created[0] == {"timeout": config.LLM_TIMEOUT}
    store, graph = service.get_metadata()
    assert service.get_pipeline() is pipeline and service.get_metadata()[0] is store
    assert pipeline.deps.store is store and pipeline.deps.graph is graph          # 流水线复用元数据的缓存

    service.reset_pipeline()
    assert service.get_metadata()[0] is not store
    assert service.get_pipeline() is not pipeline and len(created) == 2


def test_metadata_and_history_stay_available_while_the_pipeline_is_assembling(imported_mart, monkeypatch):
    monkeypatch.setattr(config, "MART_DIR", str(imported_mart))
    assembling, release = threading.Event(), threading.Event()

    def slow_client(**kwargs):                     # 第一次导入模型库很慢：装配卡在这里
        assembling.set()
        release.wait(10)
        return ScriptedLLM([])

    monkeypatch.setattr(service, "ChatModelClient", slow_client)
    found = []
    worker = threading.Thread(target=lambda: found.append(service.get_pipeline()))
    worker.start()
    assert assembling.wait(10)

    answered = []
    reader = threading.Thread(target=lambda: answered.append((service.get_metadata(), service.get_history())))
    reader.start()
    reader.join(5)
    answered_while_assembling = bool(answered)      # 放行之前看：装配期间元数据、会话历史照常能取，不排队
    release.set()
    worker.join(10)

    assert answered_while_assembling
    assert found and service.get_pipeline() is found[0]


def test_warm_up_assembles_the_pipeline_and_never_raises(imported_mart, monkeypatch):
    monkeypatch.setattr(config, "MART_DIR", str(imported_mart))
    monkeypatch.setattr(service, "ChatModelClient", lambda **kwargs: ScriptedLLM([]))

    service.warm_up()
    assert service._pipeline is not None                                             # 第一问直接用缓存

    service.reset_pipeline()

    def no_key(**kwargs):
        raise ValueError("未找到 deepseek 的 API Key")

    monkeypatch.setattr(service, "ChatModelClient", no_key)
    service.warm_up()                                                                 # 没配 Key：只记日志，不抛
    assert service._pipeline is None


def test_reimporting_the_metadata_is_picked_up_without_a_restart(imported_mart, monkeypatch):
    monkeypatch.setattr(config, "MART_DIR", str(imported_mart))
    monkeypatch.setattr(service, "ChatModelClient", lambda **kwargs: ScriptedLLM([]))
    store, _ = service.get_metadata()
    pipeline = service.get_pipeline()
    assert service.get_metadata()[0] is store and service.get_pipeline() is pipeline    # 文件没变：用缓存

    run_import(imported_mart, embedder=HashEmbedder())                                    # 重新导入
    db = default_db_path(imported_mart)
    stat = db.stat()                     # 两次导入可能落在同一个时间刻度里：把修改时间往后拨，测的是「变了就重读」
    os.utime(db, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))

    assert service.get_metadata()[0] is store
    assert service.wait_for_reload(10)
    new_store, _ = service.get_metadata()
    assert new_store is not store
    assert service._pipeline is not None and service._pipeline is not pipeline
    assert service._pipeline.deps.store is new_store
    assert service.get_pipeline() is service._pipeline


def test_metadata_and_history_stay_available_while_metadata_is_reloading(
        imported_mart, monkeypatch, reload_control) -> None:
    """后台读库很慢时，请求仍能拿旧元数据、会话历史和换版本状态"""
    monkeypatch.setattr(config, "MART_DIR", str(imported_mart))
    started, release, _ = reload_control
    store, _ = service.get_metadata()
    run_import(imported_mart, embedder=HashEmbedder(), version_id="new-version")
    advance_metadata_mtime(imported_mart)

    assert service.get_metadata()[0] is store
    assert started.wait(10)
    answered = []
    reader = threading.Thread(target=lambda: answered.append(
        (service.get_metadata(), service.get_history(), service.metadata_status())))
    reader.start()
    try:
        reader.join(5)
        assert not reader.is_alive()
        metadata, history, state = answered[0]
        assert metadata[0] is store and history.path.exists()
        assert state == {"reloading": True, "reload_error": ""}
        assert not service.wait_for_reload(0)
    finally:
        release.set()
        reader.join(10)
        assert service.wait_for_reload(10)
    assert service.get_metadata()[0].version_id == "new-version"
    assert service.get_metadata()[0] is not store
    assert service.metadata_status() == {"reloading": False, "reload_error": ""}


def test_concurrent_metadata_requests_start_only_one_reload(
        imported_mart, monkeypatch, reload_control) -> None:
    """版本键变化后，八个并发请求只启动一次后台读库"""
    monkeypatch.setattr(config, "MART_DIR", str(imported_mart))
    started, release, calls = reload_control
    store, _ = service.get_metadata()
    advance_metadata_mtime(imported_mart)
    ready, found, errors = threading.Event(), [], []

    def grab() -> None:
        try:
            assert ready.wait(10)
            found.append(service.get_metadata()[0])
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for thread in threads:
        thread.start()
    ready.set()
    try:
        assert started.wait(10)
        for thread in threads:
            thread.join(5)
        assert all(not thread.is_alive() for thread in threads)
        assert not errors and len(found) == 8 and all(item is store for item in found)
        assert len(calls) == 2
    finally:
        release.set()
        for thread in threads:
            thread.join(10)
        assert service.wait_for_reload(10)
    assert len(calls) == 2 and service.get_metadata()[0] is not store


def test_failed_metadata_reload_keeps_old_cache_and_retries_only_a_new_key(
        imported_mart, monkeypatch, caplog) -> None:
    """坏文件只试一次；文件再次变化后能恢复，失败原因也随之清掉"""
    monkeypatch.setattr(config, "MART_DIR", str(imported_mart))
    monkeypatch.setattr(service, "ChatModelClient", lambda **kwargs: ScriptedLLM([]))
    load, calls = service._load_metadata, []

    def counted_load(mart_dir: Path) -> tuple[MetadataStore, service.LineageGraph]:
        calls.append(mart_dir)
        return load(mart_dir)

    monkeypatch.setattr(service, "_load_metadata", counted_load)
    store, _ = service.get_metadata()
    pipeline = service.get_pipeline()
    db = default_db_path(imported_mart)
    db.write_bytes(b"broken metadata database")
    advance_metadata_mtime(imported_mart)
    assert service.get_metadata()[0] is store
    assert service.wait_for_reload(10)
    state = service.metadata_status()
    assert not state["reloading"]
    assert state["reload_error"].startswith("新的元数据库读不了，继续用原来的版本：")
    assert state["reload_error"] in caplog.text
    assert service.get_metadata()[0] is store and service.get_pipeline() is pipeline
    assert len(calls) == 2

    assert run_import(imported_mart, embedder=HashEmbedder(), version_id="recovered").succeeded
    advance_metadata_mtime(imported_mart)
    assert service.get_metadata()[0] is store
    assert service.wait_for_reload(10)
    assert service.get_metadata()[0].version_id == "recovered"
    assert len(calls) == 3
    assert service.metadata_status() == {"reloading": False, "reload_error": ""}


def test_changing_mart_dir_loads_new_metadata_synchronously(
        imported_mart, tiny_sheets, tmp_path, monkeypatch) -> None:
    """换资料路径后同步读取新资料，不能用旧路径的缓存顶上"""
    from tests.helpers import write_mart

    monkeypatch.setattr(config, "MART_DIR", str(imported_mart))
    old_store, _ = service.get_metadata()
    other = write_mart(tmp_path / "other_mart", tiny_sheets)
    assert run_import(other, embedder=HashEmbedder(), version_id="other").succeeded

    monkeypatch.setattr(config, "MART_DIR", str(other))
    store, _ = service.get_metadata()
    assert store is not old_store and store.version_id == "other"
    assert service.metadata_status() == {"reloading": False, "reload_error": ""}


def test_reset_discards_an_in_progress_metadata_reload(
        imported_mart, monkeypatch, reload_control) -> None:
    """后台读库期间重置缓存，旧代数的读库结果不会写回"""
    monkeypatch.setattr(config, "MART_DIR", str(imported_mart))
    started, release, calls = reload_control
    store, _ = service.get_metadata()
    advance_metadata_mtime(imported_mart)
    assert service.get_metadata()[0] is store
    assert started.wait(10)

    service.reset_pipeline()
    release.set()
    assert service.wait_for_reload(10)
    assert service._metadata is None and service._pipeline is None
    assert service.metadata_status() == {"reloading": False, "reload_error": ""}
    assert len(calls) == 2


def test_first_metadata_load_is_serialized_without_blocking_history(imported_mart, monkeypatch) -> None:
    """首次并发读取只读一次库，读库期间会话历史仍能取"""
    monkeypatch.setattr(config, "MART_DIR", str(imported_mart))
    load, calls = service._load_metadata, []
    ready, started, release = threading.Event(), threading.Event(), threading.Event()
    found, errors, histories = [], [], []

    def slow_load(mart_dir: Path) -> tuple[MetadataStore, service.LineageGraph]:
        calls.append(mart_dir)
        started.set()
        assert release.wait(10)
        return load(mart_dir)

    def grab() -> None:
        try:
            assert ready.wait(10)
            found.append(service.get_metadata())
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(service, "_load_metadata", slow_load)
    threads = [threading.Thread(target=grab) for _ in range(8)]
    for thread in threads:
        thread.start()
    reader = threading.Thread(target=lambda: histories.append(service.get_history()))
    ready.set()
    try:
        assert started.wait(10)
        reader.start()
        reader.join(5)
        assert not reader.is_alive() and histories
    finally:
        release.set()
        for thread in threads:
            thread.join(10)
        if reader.ident is not None:
            reader.join(10)
    assert all(not thread.is_alive() for thread in threads)
    assert not errors and len(calls) == 1 and len(found) == 8
    assert all(item is found[0] for item in found)


def test_metadata_size_change_triggers_reload_even_with_the_same_mtime(imported_mart, monkeypatch) -> None:
    """修改时间相同但文件大小变化，也能检测到新版本"""
    monkeypatch.setattr(config, "MART_DIR", str(imported_mart))
    monkeypatch.setattr(service, "ChatModelClient", lambda **kwargs: ScriptedLLM([]))
    store, _ = service.get_metadata()
    key = service._db_key(imported_mart)
    db = default_db_path(imported_mart)
    stat = db.stat()
    with db.open("ab") as stream:
        stream.write(b"\0")                          # SQLite 忽略完整数据库末尾的额外字节
    os.utime(db, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert service._db_key(imported_mart) == (key[0], key[1], key[2] + 1)
    assert service.get_metadata()[0] is store
    assert service.wait_for_reload(10)
    assert service.get_metadata()[0] is not store


def test_reload_assembly_failure_only_logs_a_warning(imported_mart, monkeypatch, caplog) -> None:
    """没有模型 Key 时，新元数据照常换上，预装配失败只记警告"""
    monkeypatch.setattr(config, "MART_DIR", str(imported_mart))

    def no_key(**kwargs):
        raise ValueError("未找到 deepseek 的 API Key")

    monkeypatch.setattr(service, "ChatModelClient", no_key)
    store, _ = service.get_metadata()
    advance_metadata_mtime(imported_mart)
    assert service.get_metadata()[0] is store
    assert service.wait_for_reload(10)
    assert service.get_metadata()[0] is not store and service._pipeline is None
    assert service.metadata_status() == {"reloading": False, "reload_error": ""}
    assert "流水线预装配没有完成" in caplog.text and "API Key" in caplog.text


@pytest.mark.parametrize("reset_during_assembly", [False, True])
def test_reload_stays_active_until_pipeline_assembly_finishes(
        imported_mart, monkeypatch, reset_during_assembly) -> None:
    """换版本状态包含预装配阶段，预装配期间 reset 也不会把缓存写回来"""
    monkeypatch.setattr(config, "MART_DIR", str(imported_mart))
    assembling, release = threading.Event(), threading.Event()

    def slow_client(**kwargs):
        assembling.set()
        assert release.wait(10)
        return ScriptedLLM([])

    monkeypatch.setattr(service, "ChatModelClient", slow_client)
    store, _ = service.get_metadata()
    advance_metadata_mtime(imported_mart)
    assert service.get_metadata()[0] is store
    try:
        assert assembling.wait(10)
        assert service.get_metadata()[0] is not store
        assert service.metadata_status()["reloading"]
        assert not service.wait_for_reload(0)
        if reset_during_assembly:
            service.reset_pipeline()
    finally:
        release.set()
        assert service.wait_for_reload(10)
    if reset_during_assembly:
        assert service._metadata is None and service._pipeline is None
    else:
        assert service._pipeline is not None
        assert service._pipeline.deps.store is service.get_metadata()[0]


def test_metadata_works_without_a_model_key_but_the_pipeline_does_not(imported_mart, monkeypatch):
    monkeypatch.setattr(config, "MART_DIR", str(imported_mart))

    def no_key(**kwargs):
        raise ValueError("未找到 deepseek 的 API Key")

    monkeypatch.setattr(service, "ChatModelClient", no_key)

    store, _ = service.get_metadata()                        # 元数据页、元数据接口照常能用
    assert store.counts()["表"] == 3
    with pytest.raises(ValueError, match="API Key"):
        service.get_pipeline()


def test_history_sits_next_to_the_metadata_db_and_follows_the_mart_dir(tiny_mart, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MART_DIR", str(tiny_mart))

    store = service.get_history()

    assert store.path == default_db_path(tiny_mart).parent / "history.db" and store.path.exists()
    assert service.get_history() is store                     # 进程内缓存
    other = tmp_path / "other_mart"
    monkeypatch.setattr(config, "MART_DIR", str(other))
    assert service.get_history().path == default_db_path(other).parent / "history.db"   # 换了资料目录就换一个库
    service.reset_pipeline()
    monkeypatch.setattr(config, "MART_DIR", str(tiny_mart))
    assert service.get_history() is not store and service.get_history().path == store.path


# ── describe_lineage ──────────────────────────────

def test_describe_lineage_lists_both_directions_in_a_json_friendly_shape(mock_store, mock_graph):
    view = service.describe_lineage(mock_store, mock_graph, "ads.corp_cust_dep_bal_d", "dep_bal_rmb")

    assert view["field"] == "ads.corp_cust_dep_bal_d.dep_bal_rmb"
    first = view["upstream"][0]
    assert first["depth"] == 1 and first["target"] == view["field"] and first["source"] == "dwd.corp_dep_acct_d.bal_rmb"
    assert first["script_expression"].startswith("SUM(CASE WHEN") and first["source_known"] is True
    assert first["filters"][0].startswith("WHERE ") and first["joins"][0].startswith("JOIN ")
    assert " ON " in first["joins"][0]
    assert {"depth", "target", "source", "transform", "script_expression", "is_passthrough", "origin", "script_name",
            "source_external", "source_known", "filters", "joins"} == set(first)
    downstream = view["downstream"][0]                       # 下游：target 是由本字段加工出来的字段
    assert downstream["source"] == view["field"] and downstream["target"] == "ads.org_corp_dep_bal_d.dep_bal_rmb"
    assert json.loads(json.dumps(view)) == view
    assert service.describe_lineage(mock_store, mock_graph, "ads.corp_cust_dep_bal_d", "dep_bal_rmb", depth=1)["upstream"] \
        == [hop for hop in view["upstream"] if hop["depth"] == 1]

    with pytest.raises(LookupError, match="字段不存在"):
        service.describe_lineage(mock_store, mock_graph, "ads.corp_cust_dep_bal_d", "no_such_column")
    with pytest.raises(LookupError, match="表不存在"):
        service.describe_lineage(mock_store, mock_graph, "ads.no_such_table", "x")
