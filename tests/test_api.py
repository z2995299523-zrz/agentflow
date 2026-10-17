"""接口：四个接口的正常情况、元数据库没导入、层级不对、字段不存在、上一轮的上下文原样转交给流水线"""
import pytest
from fastapi.testclient import TestClient

import config
import main
from metadata.models import LAYERS
from nl2sql import service
from nl2sql.schemas import CallRecord, Clarify, ClarifyOption, ExecSummary, QueryResult, TraceStep

NO_METADATA = "元数据库不存在: x。请先运行 python -m metadata.importer 导入资料"


def sample_result() -> QueryResult:
    """一个带提示词和模型原始回复的结果：接口返回时这两样要去掉"""
    return QueryResult(
        status="ok", question="查 9 月末的对公贷款余额", requirement="查询 2026-09-30 全行对公贷款余额",
        sql="SELECT 1", trace=[TraceStep(step="parse", label="① 需求解析", elapsed_ms=5, summary="完整需求：…")],
        calls=[CallRecord(step="parse", attempt=1, elapsed_ms=5, ok=True, input_tokens=10, output_tokens=3,
                          messages=[{"role": "user", "content": "这是提示词"}], reply="这是模型的原始回复")],
        exec_result=ExecSummary(executed=True, columns=["a"], rows=[[1]], row_count=1), elapsed_ms=12)


class FakePipeline:
    """假流水线：记下收到的请求，返回预先给定的结果"""

    def __init__(self, result: QueryResult) -> None:
        self.result = result
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return self.result


@pytest.fixture
def api(mock_store, mock_graph):
    """TestClient 和假流水线；元数据换成模拟专题库的"""
    pipeline = FakePipeline(sample_result())
    main.app.dependency_overrides[main.get_pipeline_dep] = lambda: pipeline
    main.app.dependency_overrides[main.get_metadata_dep] = lambda: (mock_store, mock_graph)
    yield TestClient(main.app), pipeline
    main.app.dependency_overrides.clear()


# ── POST /nl2sql/query ────────────────────────────

def test_query_returns_the_public_result_without_prompts(api):
    client, pipeline = api

    response = client.post("/nl2sql/query", json={"question": "查 9 月末的对公贷款余额"})

    assert response.status_code == 200
    assert response.json() == pipeline.result.public()
    body = response.json()
    assert body["status"] == "ok" and body["sql"] == "SELECT 1" and body["requirement"].startswith("查询 2026-09-30")
    call = body["calls"][0]
    assert "messages" not in call and "reply" not in call and call["input_tokens"] == 10      # 调用记录留着，提示词去掉
    assert "这是提示词" not in response.text and "这是模型的原始回复" not in response.text
    assert body["exec_result"]["rows"] == [[1]]


def test_query_hands_the_previous_turn_to_the_pipeline(api):
    client, pipeline = api
    clarify = {"question": "要不要含票据贴现？", "options": [{"label": "含票据贴现", "meaning": "全口径"},
                                                        {"label": "不含票据贴现"}]}

    response = client.post("/nl2sql/query", json={
        "question": " 不含票据贴现 ", "prev_requirement": "查询 2026-09-30 全行对公贷款余额", "prev_clarify": clarify,
        "today": "2020-01-01"})                              # 今天用系统日期，请求体里的 today 不收

    assert response.status_code == 200
    sent = pipeline.requests[0]
    assert sent.question == "不含票据贴现"                    # 首尾空白去掉了
    assert sent.prev_requirement == "查询 2026-09-30 全行对公贷款余额"
    assert sent.prev_clarify == Clarify(question="要不要含票据贴现？", options=[
        ClarifyOption(label="含票据贴现", meaning="全口径"), ClarifyOption(label="不含票据贴现")])
    assert sent.today is None and sent.use_lineage and sent.use_examples
    assert client.post("/nl2sql/query", json={"question": "  "}).status_code == 422       # 空白问题不放进流水线
    assert len(pipeline.requests) == 1


# ── GET /metadata/tables ──────────────────────────

def test_tables_are_listed_by_layer_and_a_wrong_layer_is_a_400(api, mock_store):
    client, _ = api

    response = client.get("/metadata/tables", params={"layer": "专题层"})

    assert response.status_code == 200
    tables = response.json()
    expected = mock_store.tables("专题层")
    assert [item["table_id"] for item in tables] == [table.table_id for table in expected] and tables
    first = tables[0]
    assert set(first) == {"table_id", "table_cn", "layer", "table_type", "grain_columns", "date_column", "column_count"}
    assert first["layer"] == "专题层" and first["column_count"] == len(mock_store.columns(first["table_id"]))
    assert len(client.get("/metadata/tables").json()) == len(mock_store.tables())          # 不给层级：全部表

    wrong = client.get("/metadata/tables", params={"layer": "不存在的层"})
    assert wrong.status_code == 400 and all(layer in wrong.json()["detail"] for layer in LAYERS)


# ── GET /metadata/lineage ─────────────────────────

def test_lineage_gives_upstream_and_downstream_and_unknown_fields_are_404(api):
    client, _ = api
    params = {"table": "ads.corp_cust_dep_bal_d", "column": "dep_bal_rmb"}

    response = client.get("/metadata/lineage", params=params)

    assert response.status_code == 200
    body = response.json()
    assert body["field"] == "ads.corp_cust_dep_bal_d.dep_bal_rmb"
    assert body["upstream"][0]["source"] == "dwd.corp_dep_acct_d.bal_rmb" and body["upstream"][0]["depth"] == 1
    assert body["upstream"][0]["filters"] and body["upstream"][0]["script_expression"].startswith("SUM(")
    assert body["downstream"][0]["target"] == "ads.org_corp_dep_bal_d.dep_bal_rmb"
    assert max(hop["depth"] for hop in body["upstream"]) > 1
    shallow = client.get("/metadata/lineage", params={**params, "depth": 1}).json()
    assert {hop["depth"] for hop in shallow["upstream"]} == {1}

    assert client.get("/metadata/lineage", params={**params, "column": "no_such_column"}).status_code == 404
    assert client.get("/metadata/lineage", params={"table": "ads.no_such", "column": "x"}).status_code == 404
    assert client.get("/metadata/lineage", params={**params, "depth": 0}).status_code == 422
    assert client.get("/metadata/lineage", params={"table": "ads.corp_cust_dep_bal_d"}).status_code == 422


def test_openapi_schema_lists_the_four_endpoints(api):
    client, _ = api

    response = client.get("/openapi.json")                      # /docs 靠它生成：模型、参数写错了这里就会报错

    assert response.status_code == 200
    assert set(response.json()["paths"]) == {"/nl2sql/query", "/metadata/tables", "/metadata/lineage", "/health"}


# ── GET /health ───────────────────────────────────

def test_health_reports_counts_executor_and_model(api, mock_store, monkeypatch):
    client, _ = api
    monkeypatch.setattr(config, "get_llm_kwargs", lambda temperature=None: {"model": "test-model"})

    body = client.get("/health").json()

    assert body == {"status": "ok", "mart_dir": str(config.MART_DIR), "counts": mock_store.counts(),
                    "executor": config.EXECUTOR, "model": "test-model", "hint": ""}

    def no_key(temperature=None):
        raise ValueError("未找到 deepseek 的 API Key")

    monkeypatch.setattr(config, "get_llm_kwargs", no_key)
    body = client.get("/health").json()
    assert body["status"] == "ok" and "API Key" in body["hint"]          # 元数据照常能用，只提示查数接口不能用


def test_health_without_metadata_says_so_and_how_to_import(api):
    client, _ = api

    def missing():
        raise FileNotFoundError(NO_METADATA)

    main.app.dependency_overrides[main.get_metadata_dep] = missing
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "no_metadata" and body["counts"] == {} and "python -m metadata.importer" in body["hint"]

    def broken():
        raise RuntimeError("读不了元数据库")

    main.app.dependency_overrides[main.get_metadata_dep] = broken
    body = client.get("/health").json()
    assert body["status"] == "error" and "读不了元数据库" in body["hint"]


# ── 服务取不到时 ──────────────────────────────────

def test_endpoints_answer_503_with_the_reason_when_the_service_is_unavailable(monkeypatch):
    def missing():
        raise FileNotFoundError(NO_METADATA)

    def no_key():
        raise ValueError("未找到 deepseek 的 API Key")

    monkeypatch.setattr(service, "get_metadata", missing)
    monkeypatch.setattr(service, "get_pipeline", no_key)
    client = TestClient(main.app)                          # 不换依赖：走真的 get_*_dep

    tables = client.get("/metadata/tables")
    assert tables.status_code == 503
    assert tables.json() == {"detail": NO_METADATA, "status": "no_metadata"}
    query = client.post("/nl2sql/query", json={"question": "查余额"})
    assert query.status_code == 503 and "API Key" in query.json()["detail"] and query.json()["status"] == "error"
    health = client.get("/health").json()
    assert health["status"] == "no_metadata" and "python -m metadata.importer" in health["hint"]
