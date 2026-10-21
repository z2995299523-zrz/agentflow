"""接口：查数（一次返回、流式）、会话、元数据、健康检查；元数据库没导入、层级不对、字段不存在、上一轮的上下文转交给流水线"""
import json
import time

import pytest
from fastapi.testclient import TestClient

import config
import main
from metadata.models import LAYERS
from nl2sql import service
from nl2sql.history import ConversationStore
from nl2sql.schemas import CallRecord, Clarify, ClarifyOption, ExecSummary, QueryResult, TraceStep

NO_METADATA = "元数据库不存在: x。请先运行 python -m metadata.importer 导入资料"
CLARIFY = Clarify(question="要不要含票据贴现？", options=[ClarifyOption(label="含票据贴现", meaning="全口径"),
                                                     ClarifyOption(label="不含票据贴现")])


def sample_result() -> QueryResult:
    """一个带提示词和模型原始回复的结果：接口返回时这两样要去掉"""
    return QueryResult(
        status="ok", question="查 9 月末的对公贷款余额", requirement="查询 2026-09-30 全行对公贷款余额",
        sql="SELECT 1", trace=[TraceStep(step="parse", label="① 需求解析", elapsed_ms=5, summary="完整需求：…")],
        calls=[CallRecord(step="parse", attempt=1, elapsed_ms=5, ok=True, input_tokens=10, output_tokens=3,
                          messages=[{"role": "user", "content": "这是提示词"}], reply="这是模型的原始回复")],
        exec_result=ExecSummary(executed=True, columns=["a"], rows=[[1]], row_count=1), elapsed_ms=12)


def clarify_result() -> QueryResult:
    """一个反问的结果"""
    return QueryResult(status="need_clarify", question="查 9 月末的对公贷款余额",
                       requirement="查询 2026-09-30 全行对公贷款余额", clarify=CLARIFY,
                       trace=[TraceStep(step="parse", label="① 需求解析", elapsed_ms=5),
                              TraceStep(step="plan", label="④ 选表定口径", elapsed_ms=7, summary="需要反问")])


class FakePipeline:
    """假流水线：记下收到的请求，返回预先给定的结果；stream 先逐步给出结果里的轨迹，再给出结果"""

    def __init__(self, result: QueryResult) -> None:
        self.result = result
        self.requests = []
        self.delay = 0.0                         # stream 给出结果前停多久，测保活用

    def run(self, request):
        self.requests.append(request)
        return self.result

    def stream(self, request):
        self.requests.append(request)
        yield from self.result.trace
        time.sleep(self.delay)
        yield self.result


def sse_events(text: str) -> list[tuple[str, dict]]:
    """把 SSE 响应体拆成（事件名, 数据）；冒号开头的保活注释跳过"""
    events = []
    for block in text.split("\n\n"):
        lines = [line for line in block.splitlines() if line and not line.startswith(":")]
        if lines:
            fields = dict(line.split(": ", 1) for line in lines)
            events.append((fields["event"], json.loads(fields["data"])))
    return events


@pytest.fixture
def history(tmp_path):
    """临时目录里的会话历史库"""
    return ConversationStore(tmp_path / "history.db")


@pytest.fixture
def api(mock_store, mock_graph, history):
    """TestClient 和假流水线；元数据换成模拟专题库的，会话历史放进临时目录"""
    pipeline = FakePipeline(sample_result())
    main.app.dependency_overrides[main.get_pipeline_dep] = lambda: pipeline
    main.app.dependency_overrides[main.get_metadata_dep] = lambda: (mock_store, mock_graph)
    main.app.dependency_overrides[main.get_history_dep] = lambda: history
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
        "prev_sql": "SELECT 1", "prev_tables": ["ads.corp_cust_loan_bal_d"],
        "today": "2020-01-01"})                              # 今天用系统日期，请求体里的 today 不收

    assert response.status_code == 200
    sent = pipeline.requests[0]
    assert sent.question == "不含票据贴现"                    # 首尾空白去掉了
    assert sent.prev_requirement == "查询 2026-09-30 全行对公贷款余额"
    assert sent.prev_sql == "SELECT 1" and sent.prev_tables == ["ads.corp_cust_loan_bal_d"]
    assert sent.prev_clarify == Clarify(question="要不要含票据贴现？", options=[
        ClarifyOption(label="含票据贴现", meaning="全口径"), ClarifyOption(label="不含票据贴现")])
    assert sent.today is None and sent.use_lineage and sent.use_examples
    assert client.post("/nl2sql/query", json={"question": "  "}).status_code == 422       # 空白问题不放进流水线
    assert len(pipeline.requests) == 1


# ── POST /nl2sql/stream ───────────────────────────

def test_stream_sends_each_step_then_the_public_result(api):
    client, pipeline = api

    response = client.post("/nl2sql/stream", json={"question": "查 9 月末的对公贷款余额",
                                                    "prev_requirement": "上一轮", "prev_clarify": CLARIFY.model_dump()})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache" and response.headers["x-accel-buffering"] == "no"
    events = sse_events(response.text)
    assert events == [("step", pipeline.result.trace[0].model_dump(mode="json")), ("result", pipeline.result.public())]
    assert "这是提示词" not in response.text and "这是模型的原始回复" not in response.text
    sent = pipeline.requests[0]
    assert sent.prev_requirement == "上一轮" and sent.prev_clarify == CLARIFY
    assert client.post("/nl2sql/stream", json={"question": " "}).status_code == 422


def test_stream_keeps_the_connection_alive_and_reports_unexpected_errors(api, monkeypatch):
    client, pipeline = api
    monkeypatch.setattr(main, "SSE_HEARTBEAT_SECONDS", 0.05)
    pipeline.delay = 0.3                                          # 一步很慢：中间要有保活注释

    slow = client.post("/nl2sql/stream", json={"question": "查余额"})

    assert ": ping\n\n" in slow.text and sse_events(slow.text)[-1][0] == "result"

    def broken(request):
        yield TraceStep(step="parse", label="① 需求解析", elapsed_ms=1)
        raise RuntimeError("坏了")

    pipeline.stream = broken
    events = sse_events(client.post("/nl2sql/stream", json={"question": "查余额"}).text)
    assert [name for name, _ in events] == ["step", "error"] and "RuntimeError: 坏了" in events[-1][1]["detail"]


# ── 会话 ──────────────────────────────────────────

def test_conversations_can_be_created_listed_renamed_and_deleted(api):
    client, _ = api

    created = client.post("/conversations")
    named = client.post("/conversations", json={"title": "九月末贷款"})

    assert created.status_code == 201 and named.status_code == 201
    first, second = created.json(), named.json()
    assert first["title"] == "" and first["turn_count"] == 0 and len(first["id"]) == 32
    assert second["title"] == "九月末贷款"
    assert {item["id"] for item in client.get("/conversations").json()} == {first["id"], second["id"]}
    assert len(client.get("/conversations", params={"limit": 1}).json()) == 1

    detail = client.get(f"/conversations/{first['id']}").json()
    assert detail["turns"] == [] and detail["context"] == {"prev_requirement": "", "prev_clarify": None,
                                                           "clarify_turn_id": None, "prev_sql": "", "prev_tables": []}
    renamed = client.patch(f"/conversations/{first['id']}", json={"title": " 存款 "})
    assert renamed.status_code == 200 and renamed.json()["title"] == "存款"
    assert client.patch(f"/conversations/{first['id']}", json={"title": " "}).status_code == 422
    assert client.patch("/conversations/nope", json={"title": "x"}).status_code == 404

    assert client.delete(f"/conversations/{first['id']}").status_code == 204
    assert client.get(f"/conversations/{first['id']}").status_code == 404
    assert client.delete(f"/conversations/{first['id']}").status_code == 404
    assert [item["id"] for item in client.get("/conversations").json()] == [second["id"]]


def test_turns_stream_the_steps_save_the_turn_and_carry_the_context(api):
    client, pipeline = api
    conversation_id = client.post("/conversations").json()["id"]
    pipeline.result = clarify_result()

    asked = client.post(f"/conversations/{conversation_id}/turns", json={"question": " 查 9 月末的对公贷款余额 "})

    assert asked.status_code == 200 and asked.headers["content-type"].startswith("text/event-stream")
    events = sse_events(asked.text)
    assert [name for name, _ in events] == ["step", "step", "turn"]
    turn, context = events[-1][1]["turn"], events[-1][1]["context"]
    assert turn["question"] == "查 9 月末的对公贷款余额" and turn["result"] == pipeline.result.public()
    assert context == {"prev_requirement": "查询 2026-09-30 全行对公贷款余额", "prev_clarify": CLARIFY.model_dump(),
                       "clarify_turn_id": turn["id"], "prev_sql": "", "prev_tables": []}
    assert pipeline.requests[0].prev_requirement == "" and pipeline.requests[0].prev_clarify is None

    pipeline.result = sample_result()                             # 点了反问的选项：服务端带上反问和上一轮的需求
    answered = sse_events(client.post(f"/conversations/{conversation_id}/turns",
                                      json={"question": "不含票据贴现"}).text)
    assert pipeline.requests[1].prev_clarify == CLARIFY
    assert pipeline.requests[1].prev_requirement == "查询 2026-09-30 全行对公贷款余额"
    assert pipeline.requests[1].prev_sql == "" and pipeline.requests[1].prev_tables == []    # 反问那一轮没有 SQL
    assert answered[-1][1]["context"]["prev_clarify"] is None
    assert answered[-1][1]["context"]["prev_sql"] == "SELECT 1"            # 这一轮校验通过：下一轮追问以它为底

    detail = client.get(f"/conversations/{conversation_id}").json()
    assert detail["title"] == "查 9 月末的对公贷款余额" and detail["turn_count"] == 2
    assert [item["question"] for item in detail["turns"]] == ["查 9 月末的对公贷款余额", "不含票据贴现"]
    assert detail["turns"][1]["result"]["sql"] == "SELECT 1" and "这是提示词" not in json.dumps(detail, ensure_ascii=False)

    assert client.post("/conversations/nope/turns", json={"question": "查余额"}).status_code == 404
    assert client.post(f"/conversations/{conversation_id}/turns", json={"question": ""}).status_code == 422
    assert len(pipeline.requests) == 2


# ── GET /metadata/tables ──────────────────────────

def test_tables_are_listed_by_layer_and_a_wrong_layer_is_a_400(api, mock_store):
    client, _ = api

    response = client.get("/metadata/tables", params={"layer": "专题层"})

    assert response.status_code == 200
    tables = response.json()
    expected = mock_store.tables("专题层")
    assert [item["table_id"] for item in tables] == [table.table_id for table in expected] and tables
    first = tables[0]
    assert set(first) == {"table_id", "table_cn", "layer", "table_type", "grain_columns", "date_column", "description",
                          "column_count"}
    assert first["layer"] == "专题层" and first["column_count"] == len(mock_store.columns(first["table_id"]))
    assert first["description"] == expected[0].description
    assert len(client.get("/metadata/tables").json()) == len(mock_store.tables())          # 不给层级：全部表

    wrong = client.get("/metadata/tables", params={"layer": "不存在的层"})
    assert wrong.status_code == 400 and all(layer in wrong.json()["detail"] for layer in LAYERS)


# ── GET /metadata/columns、/metadata/search、/metadata/examples ──

def test_columns_come_in_table_order_with_lineage_counts(api, mock_store):
    client, _ = api

    response = client.get("/metadata/columns", params={"table": "ads.corp_cust_dep_bal_d"})

    assert response.status_code == 200
    columns = response.json()
    assert [item["column_name"] for item in columns] == [
        column.column_name for column in mock_store.columns("ads.corp_cust_dep_bal_d")]
    balance = next(item for item in columns if item["column_name"] == "dep_bal_rmb")
    assert balance["column_cn"] == "存款余额" and balance["upstream_count"] >= 1 and balance["downstream_count"] >= 1
    assert set(balance) == {"column_name", "column_cn", "ordinal", "data_type", "is_pk", "description", "enum_values",
                            "upstream_count", "downstream_count"}
    assert client.get("/metadata/columns", params={"table": "ads.no_such"}).status_code == 404
    assert client.get("/metadata/columns").status_code == 422


def test_search_ranks_exact_names_first_and_ignores_case(api):
    client, _ = api

    body = client.get("/metadata/search", params={"q": "存款余额"}).json()

    assert [item["table_id"] for item in body["tables"]] == ["ads.corp_cust_dep_bal_d", "ads.org_corp_dep_bal_d"]
    assert [(item["table_id"], item["column_name"]) for item in body["columns"][:2]] == [
        ("ads.corp_cust_dep_bal_d", "dep_bal_rmb"), ("ads.org_corp_dep_bal_d", "dep_bal_rmb")]   # 中文名完全一样的排前面
    assert body["column_total"] > 2 and body["table_total"] == 2
    upper = client.get("/metadata/search", params={"q": "DEP_BAL", "limit": 2}).json()
    assert len(upper["columns"]) == 2 and upper["column_total"] > 2
    assert all("dep_bal" in item["column_name"] for item in upper["columns"])
    assert client.get("/metadata/search", params={"q": ""}).status_code == 422


def test_examples_list_the_example_requirements(api, mock_store):
    client, _ = api

    body = client.get("/metadata/examples", params={"limit": 2}).json()

    assert body == [{"example_id": example.example_id, "requirement": example.requirement}
                    for example in mock_store.examples()[:2]]


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
    on_chain = {body["field"]} | {hop[end] for hop in body["upstream"] + body["downstream"] for end in ("source", "target")}
    assert set(body["fields"]) == on_chain                         # 画图用：链路上每个字段都有说明
    assert body["fields"][body["field"]] == {"table": "ads.corp_cust_dep_bal_d", "column": "dep_bal_rmb",
                                             "table_cn": "对公客户存款余额日汇总", "layer": "专题层",
                                             "column_cn": "存款余额", "known": True}
    shallow = client.get("/metadata/lineage", params={**params, "depth": 1}).json()
    assert {hop["depth"] for hop in shallow["upstream"]} == {1}

    assert client.get("/metadata/lineage", params={**params, "column": "no_such_column"}).status_code == 404
    assert client.get("/metadata/lineage", params={"table": "ads.no_such", "column": "x"}).status_code == 404
    assert client.get("/metadata/lineage", params={**params, "depth": 0}).status_code == 422
    assert client.get("/metadata/lineage", params={"table": "ads.corp_cust_dep_bal_d"}).status_code == 422


def test_openapi_schema_lists_every_endpoint(api):
    client, _ = api

    response = client.get("/openapi.json")                      # /docs 靠它生成：模型、参数写错了这里就会报错

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert set(paths) == {"/nl2sql/query", "/nl2sql/stream", "/conversations", "/conversations/{conversation_id}",
                          "/conversations/{conversation_id}/turns", "/metadata/tables", "/metadata/columns",
                          "/metadata/search", "/metadata/lineage", "/metadata/examples", "/health"}
    assert set(paths["/conversations/{conversation_id}"]) == {"get", "patch", "delete"}
    assert "text/event-stream" in paths["/nl2sql/stream"]["post"]["responses"]["200"]["content"]


# ── GET /health ───────────────────────────────────

def test_health_reports_counts_executor_and_model(api, mock_store, monkeypatch):
    client, _ = api
    monkeypatch.setattr(config, "get_llm_kwargs", lambda temperature=None: {"model": "test-model"})

    body = client.get("/health").json()

    assert body == {"status": "ok", "mart_dir": str(config.MART_DIR), "counts": mock_store.counts(),
                    "executor": config.EXECUTOR, "model": "test-model", "dialect": config.SQL_DIALECT, "hint": ""}

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

    def read_only():
        raise PermissionError("会话历史库写不了")

    monkeypatch.setattr(service, "get_metadata", missing)
    monkeypatch.setattr(service, "get_pipeline", no_key)
    monkeypatch.setattr(service, "get_history", read_only)
    client = TestClient(main.app)                          # 不换依赖：走真的 get_*_dep

    tables = client.get("/metadata/tables")
    assert tables.status_code == 503
    assert tables.json() == {"detail": NO_METADATA, "status": "no_metadata"}
    for path in ("/metadata/columns?table=x", "/metadata/search?q=x", "/metadata/examples"):
        assert client.get(path).status_code == 503
    query = client.post("/nl2sql/query", json={"question": "查余额"})
    assert query.status_code == 503 and "API Key" in query.json()["detail"] and query.json()["status"] == "error"
    stream = client.post("/nl2sql/stream", json={"question": "查余额"})
    assert stream.status_code == 503 and "API Key" in stream.json()["detail"]      # 还没开始流就失败：普通的 503
    conversations = client.get("/conversations")
    assert conversations.status_code == 503 and conversations.json()["detail"] == "会话历史库写不了"
    health = client.get("/health").json()
    assert health["status"] == "no_metadata" and "python -m metadata.importer" in health["hint"]
