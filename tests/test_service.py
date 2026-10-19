"""流水线装配：向量器还原、按资料目录建流水线、进程内缓存、字段链路的描述"""
import json
import os
import threading
from datetime import date

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
        barrier.wait()
        found.append(service.get_pipeline())

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

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

    assert service.get_metadata()[0] is not store
    assert service.get_pipeline() is not pipeline and service.get_pipeline().deps.store is service.get_metadata()[0]


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
