"""接口：查数（一次返回、流式）、会话、元数据、健康检查；元数据库没导入、层级不对、字段不存在、上一轮的上下文转交给流水线"""
import json
import time
from pathlib import Path

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


@pytest.fixture(autouse=True)
def isolated_model_settings(tmp_path, monkeypatch):
    """接口测试的模型选择写入临时目录，不触碰本地会话配置。"""
    monkeypatch.setattr(config, "MART_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ACCESS_CONTROL_ENABLED", False)


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


@pytest.fixture
def three_models(monkeypatch):
    """能选的模型固定成三个：默认 DeepSeek、千问、小米 MiMo，不看本机 .env 里配了哪些 Key"""
    choices = [config.ModelChoice("deepseek-flash", "DeepSeek Flash", "deepseek"),
               config.ModelChoice("qwen3.8-flash", "千问 qwen3.8-flash", "dashscope"),
               config.ModelChoice("mimo-v2.6-flash", "小米 mimo-v2.6-flash", "mimo")]
    monkeypatch.setattr(config, "available_models", lambda: choices)
    monkeypatch.setattr(config, "default_model", lambda: "deepseek-flash")
    monkeypatch.setattr(config, "LLM_PROVIDER", "deepseek")
    monkeypatch.setattr(config, "_api_key", lambda provider: "test-key" if provider in {"deepseek", "dashscope", "mimo"} else "")


def test_models_lists_the_models_with_a_key_and_the_default(api, three_models):
    client, _ = api

    body = client.get("/nl2sql/models").json()

    assert body["default"] == "deepseek-flash"
    assert body["models"][1] == {"name": "qwen", "label": "千问", "provider": "dashscope"}
    assert [item["name"] for item in body["models"]] == ["deepseek-flash", "qwen", "mimo-v2.6-flash"]


def test_default_model_changes_new_queries_and_preserves_conversation_selection(api, three_models):
    """修改默认模型即时生效，已有会话仍按保存的模型查询。"""
    client, pipeline = api
    path = f"/conversations/{client.post('/conversations').json()['id']}"
    assert client.put(f"{path}/model", json={"model": "deepseek-flash"}).status_code == 200
    assert client.put("/nl2sql/model-configs/qwen/default").status_code == 204
    configs = client.get("/nl2sql/model-configs").json()
    assert configs["default"] == "qwen" and "qwen" in configs["selectable"]
    listing = client.get("/nl2sql/models").json()
    assert listing["default"] == listing["models"][0]["name"] == "qwen"
    assert client.get("/health").json()["model"] == "qwen3.8-flash"
    assert client.post("/nl2sql/query", json={"question": "查余额"}).status_code == 200
    assert pipeline.requests[-1].model == "@dashscope:qwen3.8-flash"
    assert client.post(f"{path}/turns", json={"question": "查余额"}).status_code == 200
    assert pipeline.requests[-1].model == "deepseek-flash"
    assert client.get(f"{path}/model").json()["model"] == "deepseek-flash"


def test_default_model_requires_available_entry_and_falls_back_if_unavailable(api, three_models):
    """停用、缺少密钥和删除后的模型不能成为当前默认值。"""
    client, _ = api
    config = {"base_url": "https://api.example.com/v1", "model": "demo-model", "api_key": "test-key"}
    item = client.post("/nl2sql/model-configs", json=config).json()
    path = f"/nl2sql/model-configs/{item['id']}"
    assert client.put(f"{path}/default").status_code == 204
    assert client.get("/nl2sql/models").json()["default"] == item["id"]
    assert client.put(path, json=config | {"enabled": False}).status_code == 200
    assert client.get("/nl2sql/models").json()["default"] == "deepseek-flash"
    assert client.put(f"{path}/default").status_code == 400
    assert client.put("/nl2sql/model-configs/glm-4.7-flash/default").status_code == 400
    assert client.put(path, json=config).status_code == 200
    assert client.get("/nl2sql/models").json()["default"] == item["id"]
    assert client.delete(path).status_code == 204
    assert client.put(f"{path}/default").status_code == 400
    assert client.get("/nl2sql/model-configs").json()["default"] == "deepseek-flash"


def test_qwen_entry_requires_dashscope_key(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(config, "available_models", lambda: [config.ModelChoice("qwen3.8-flash", "千问", "dashscope")])
    main._check_model("qwen")
    with pytest.raises(HTTPException):
        main._check_model("qwen bad")
    monkeypatch.setattr(config, "available_models", lambda: [config.ModelChoice("deepseek-flash", "DeepSeek", "deepseek")])
    with pytest.raises(HTTPException):
        main._check_model("qwen")


def test_conversation_model_selection_is_independent(api, three_models):
    client, _ = api
    first = client.post("/conversations").json()["id"]
    second = client.post("/conversations").json()["id"]
    path1 = f"/conversations/{first}/model"
    path2 = f"/conversations/{second}/model"
    assert client.get(path1).json()["model"] is None
    assert client.put(path1, json={"model": "qwen"}).status_code == 200
    assert client.put(path2, json={"model": "mimo-v2.6-flash"}).status_code == 200
    assert client.get(path1).json()["model"] == "qwen"
    assert client.get(path2).json()["model"] == "mimo-v2.6-flash"
    assert client.put(path1, json={"model": "gpt-9"}).status_code == 400
    assert client.get(path1).json()["model"] == "qwen"
    assert client.delete(f"/conversations/{first}").status_code == 204
    assert client.get(path1).status_code == 404


def test_custom_endpoint_credentials_are_write_only_and_bound_to_url(api, monkeypatch):
    client, pipeline = api
    monkeypatch.setattr(config, 'LLM_PROVIDER', 'deepseek')
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    monkeypatch.delenv(config.PROVIDERS[config.LLM_PROVIDER][0], raising=False)
    secret = 'test-connection-key'
    body = {'base_url': 'https://api.example.com/v1/chat/completions', 'model': 'demo-model:free', 'api_key': secret}
    created = client.post('/nl2sql/model-configs', json=body)
    assert created.status_code == 201 and secret not in created.text
    ident = created.json()['id']
    assert created.json()['base_url'] == 'https://api.example.com/v1'
    assert created.json()['label'] == 'demo-model:free'
    listing = client.get('/nl2sql/model-configs')
    assert secret not in listing.text and next(m for m in listing.json()['models'] if m['id'] == ident)['key_configured']
    assert client.get('/nl2sql/models').json()['default'] == ident
    assert client.post('/nl2sql/query', json={'question': '查余额', 'model': ident}).status_code == 200
    route = pipeline.requests[-1].model
    kwargs = config.get_llm_kwargs(model=route)
    assert kwargs['api_key'] == secret and kwargs['model'] == 'demo-model:free'
    assert secret not in (Path(config.MART_DIR) / 'build' / 'history.db').read_bytes().decode(errors='ignore')
    path = f'/nl2sql/model-configs/{ident}'
    assert client.put(path, json=body | {'api_key': '', 'base_url': 'https://other.example.com/v1'}).status_code == 400
    assert config.get_llm_kwargs(model=route)['base_url'] == 'https://api.example.com/v1'
    assert client.put(path, json=body | {'api_key': '', 'label': '改名'}).status_code == 200
    assert config.get_llm_kwargs(model=main._resolved_model(ident))['api_key'] == secret
    with pytest.raises(ValueError, match='配置已变化'):
        config.get_llm_kwargs(model=route)
    invalid = client.post('/nl2sql/model-configs', json=body | {'api_key': {'value': secret}})
    assert invalid.status_code == 422 and secret not in invalid.text


def test_model_configuration_add_edit_disable_and_qwen_compatibility(api, three_models, monkeypatch):
    client, pipeline = api
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'test-key')
    initial = client.get('/nl2sql/model-configs').json()
    assert next(item for item in initial['models'] if item['id'] == 'qwen')['model'] == 'qwen3.8-flash'
    body = {'label': '百炼模型', 'provider': 'dashscope', 'model': 'kimi-k3', 'enabled': True}
    created = client.post('/nl2sql/model-configs', json=body)
    assert created.status_code == 201
    ident = created.json()['id']
    assert any(item['name'] == ident for item in client.get('/nl2sql/models').json()['models'])
    assert main._resolved_model(ident) == '@dashscope:kimi-k3'
    assert client.post('/nl2sql/query', json={'question': '查余额', 'model': ident}).status_code == 200
    assert pipeline.requests[-1].model == '@dashscope:kimi-k3'
    assert client.put(f'/nl2sql/model-configs/{ident}', json=body | {'model': 'kimi-k3.1-pro'}).status_code == 200
    assert main._resolved_model(ident) == '@dashscope:kimi-k3.1-pro'
    assert client.put(f'/nl2sql/model-configs/{ident}', json=body | {'enabled': False}).status_code == 200
    assert ident not in [item['name'] for item in client.get('/nl2sql/models').json()['models']]
    assert client.post('/nl2sql/query', json={'question': '查余额', 'model': ident}).status_code == 400
    assert client.put('/nl2sql/model-configs/qwen', json={'label': '千问', 'provider': 'dashscope', 'model': 'qwen3.8-27b', 'enabled': True}).status_code == 200
    assert main._resolved_model('qwen') == '@dashscope:qwen3.8-27b'
    assert client.post('/nl2sql/model-configs', json=body | {'model': 'invalid model'}).status_code == 400


def test_probe_uses_unsaved_form_or_saved_key_at_the_same_url(api, monkeypatch):
    """测试连接不落库，编辑时只在原地址复用密钥。"""
    client, _ = api
    calls = []

    def probe(url, model, key, **options):
        calls.append((url, model, key, options))
        return {'ok': True, 'status_code': 200, 'elapsed_ms': 12, 'message': '模型调用成功'}

    monkeypatch.setattr(main, 'probe_connection', probe)
    path = '/nl2sql/model-configs/test'
    body = {'base_url': 'https://api.example.com/v1/chat/completions', 'model': 'demo-model',
            'api_key': 'test-connection-key', 'enabled': False}
    before = client.get('/nl2sql/model-configs').json()
    result = client.post(path, json=body)
    assert result.status_code == 200 and result.json()['ok']
    assert body['api_key'] not in result.text
    assert client.get('/nl2sql/model-configs').json() == before
    created = client.post('/nl2sql/model-configs', json=body).json()
    pending = body | {'id': created['id'], 'api_key': '', 'model': 'other-model'}
    assert client.post(path, json=pending).json()['ok']
    assert calls == [('https://api.example.com/v1', 'demo-model', 'test-connection-key', {'custom': True}),
                     ('https://api.example.com/v1', 'other-model', 'test-connection-key', {'custom': True})]
    response = client.post(path, json=pending | {'base_url': 'https://other.example.com/v1'})
    assert response.status_code == 400 and '更换接口地址' in response.json()['detail']
    assert len(calls) == 2


def test_probe_uses_preset_connection_and_actual_model_code(api, three_models, monkeypatch):
    """百炼预设测试使用同一个服务商连接及当前表单里的实际模型。"""
    client, _ = api

    def probe(url, model, key, **options):
        assert url == config.PROVIDERS['dashscope'][1]
        assert model == 'demo-model' and key == 'test-key'
        assert options == {'custom': False, 'extra_body': {'enable_thinking': False}}
        return {'ok': False, 'status_code': 404, 'elapsed_ms': 4, 'message': '模型不存在'}

    monkeypatch.setattr(main, 'probe_connection', probe)
    result = client.post('/nl2sql/model-configs/test', json={'provider': 'dashscope', 'model': 'demo-model'})
    assert result.status_code == 200 and result.json()['message'] == '模型不存在'


def test_builtin_can_be_edited_and_deleted_without_losing_history(api, three_models):
    """内置项编辑沿用原 ID，删除后查询回退和历史记录各自保留。"""
    client, pipeline = api
    ident = 'deepseek-flash'
    path = f'/nl2sql/model-configs/{ident}'
    conversation = client.post('/conversations').json()['id']
    conversation_path = f'/conversations/{conversation}'
    assert client.put(f'{conversation_path}/model', json={'model': ident}).status_code == 200
    body = {'label': '新模型', 'model': 'other-model', 'provider': 'mimo'}
    edited = client.put(path, json=body)
    assert edited.status_code == 200 and edited.json()['builtin'] is True
    assert client.post(f'{conversation_path}/turns', json={'question': '查余额'}).status_code == 200
    assert pipeline.requests[-1].model == '@mimo:other-model'
    assert client.get('/health').json()['model'] == 'other-model'
    assert client.delete(path).status_code == 204
    assert client.delete(path).status_code == 404
    assert client.put(path, json=body).status_code == 404
    assert ident not in {item['id'] for item in client.get('/nl2sql/model-configs').json()['models']}
    assert client.get('/nl2sql/models').json()['default'] == 'qwen'
    assert client.get(f'{conversation_path}/model').json()['model'] == ident
    assert client.get(conversation_path).json()['turn_count'] == 1
    assert client.post(f'{conversation_path}/turns', json={'question': '继续查'}).status_code == 400
    assert client.post('/nl2sql/query', json={'question': '查余额'}).status_code == 200
    assert pipeline.requests[-1].model == '@dashscope:qwen3.8-flash'


def test_custom_delete_clears_credentials_and_qwen_delete_clears_aliases(api, three_models):
    """删除自定义密钥；被删除的百炼入口不再通过旧名称调用。"""
    client, _ = api
    body = {'base_url': 'https://api.example.com/v1', 'model': 'demo-model', 'api_key': 'test-connection-key'}
    created = client.post('/nl2sql/model-configs', json=body).json()
    route = main._resolved_model(created['id'])
    assert client.delete(f"/nl2sql/model-configs/{created['id']}").status_code == 204
    with pytest.raises(ValueError, match='配置已变化'):
        config.get_llm_kwargs(model=route)
    assert client.delete('/nl2sql/model-configs/qwen').status_code == 204
    assert client.get('/nl2sql/model-settings/qwen').status_code == 404
    assert client.post('/nl2sql/query', json={'question': '查余额', 'model': 'qwen3.8-flash'}).status_code == 400


def test_every_query_endpoint_hands_the_query_switch_to_the_pipeline(api):
    client, pipeline = api
    conversation_id = client.post("/conversations").json()["id"]

    client.post("/nl2sql/query", json={"question": "查余额", "execute": False})
    client.post("/nl2sql/stream", json={"question": "查余额", "execute": False})
    client.post(f"/conversations/{conversation_id}/turns", json={"question": "查余额", "execute": False})
    client.post("/nl2sql/query", json={"question": "查余额"})                     # 不写时默认查询

    assert [request.execute for request in pipeline.requests] == [False, False, False, True]


def test_every_query_endpoint_hands_the_chosen_model_to_the_pipeline(api, three_models):
    client, pipeline = api
    conversation_id = client.post("/conversations").json()["id"]
    pipeline.result = sample_result().model_copy(update={"model": "qwen3.8-flash"})

    assert client.post("/nl2sql/query", json={"question": "查余额", "model": "qwen3.8-flash"}).status_code == 200
    client.post("/nl2sql/stream", json={"question": "查余额", "model": "mimo-v2.6-flash"})
    turn = sse_events(client.post(f"/conversations/{conversation_id}/turns",
                                  json={"question": "查余额", "model": "qwen3.8-flash"}).text)[-1][1]["turn"]
    client.post("/nl2sql/query", json={"question": "查余额", "model": " "})        # 空白当作没写：用默认模型

    assert [request.model for request in pipeline.requests] == ["qwen3.8-flash", "mimo-v2.6-flash", "qwen3.8-flash",
                                                                None]
    assert turn["result"]["model"] == "qwen3.8-flash"                             # 会话历史记下了这一轮用的模型
    saved = client.get(f"/conversations/{conversation_id}").json()["turns"][0]
    assert saved["result"]["model"] == "qwen3.8-flash"


def test_builtin_dashscope_accepts_deepseek_code_and_routes_all_query_endpoints(api, three_models):
    client, pipeline = api
    code = "deepseek-v4-pro-0813"
    response = client.put('/nl2sql/model-configs/qwen', json={
        'label': '千问', 'provider': 'dashscope', 'model': code, 'enabled': True,
    })
    assert response.status_code == 200
    assert response.json()['model'] == code
    assert next(item for item in client.get('/nl2sql/models').json()['models'] if item['name'] == 'qwen')['label'] == '百炼'
    conversation_id = client.post("/conversations").json()["id"]
    assert client.post("/nl2sql/query", json={"question": "查余额", "model": "qwen"}).status_code == 200
    assert client.post("/nl2sql/stream", json={"question": "查余额", "model": "qwen"}).status_code == 200
    assert client.post(f"/conversations/{conversation_id}/turns", json={"question": "查余额", "model": "qwen"}).status_code == 200
    assert [request.model for request in pipeline.requests] == [f"@dashscope:{code}"] * 3


def test_a_model_that_is_not_offered_is_a_400_and_never_runs(api, three_models):
    client, pipeline = api
    conversation_id = client.post("/conversations").json()["id"]

    for path in ("/nl2sql/query", "/nl2sql/stream", f"/conversations/{conversation_id}/turns"):
        response = client.post(path, json={"question": "查余额", "model": "gpt-9"})
        assert response.status_code == 400 and "gpt-9" in response.json()["detail"]
        assert "qwen3.8-flash" in response.json()["detail"]                       # 告诉用户能选哪些
    assert pipeline.requests == []
    assert client.get(f"/conversations/{conversation_id}").json()["turn_count"] == 0


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
                          "column_count", "domain_code"}
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
    assert set(paths) == {"/nl2sql/query", "/nl2sql/stream", "/nl2sql/models", "/nl2sql/model-settings/qwen", "/nl2sql/model-configs", "/nl2sql/model-configs/test", "/nl2sql/model-configs/{model_id}",
                          "/nl2sql/model-configs/{model_id}/default", "/conversations", "/conversations/{conversation_id}/model",
                          "/conversations/{conversation_id}", "/conversations/{conversation_id}/turns",
                          "/metadata/tables", "/metadata/columns", "/metadata/search", "/metadata/lineage",
                          "/metadata/examples", "/health", "/auth/me", "/auth/login", "/auth/logout",
                          "/admin/users", "/admin/users/{user_id}", "/admin/domains"}
    assert set(paths["/conversations/{conversation_id}"]) == {"get", "patch", "delete"}
    assert "text/event-stream" in paths["/nl2sql/stream"]["post"]["responses"]["200"]["content"]


# ── GET /health ───────────────────────────────────

def test_health_reports_counts_executor_and_model(api, mock_store, monkeypatch):
    client, _ = api
    monkeypatch.setattr(config, "available_models", lambda: [])
    monkeypatch.setattr(config, "get_llm_kwargs", lambda temperature=None: {"model": "test-model"})

    body = client.get("/health").json()

    assert body == {"status": "ok", "mart_dir": str(config.MART_DIR), "counts": mock_store.counts(),
                    "executor": config.EXECUTOR, "model": "test-model", "dialect": config.SQL_DIALECT,
                    "hint": "", "metadata_version": None,
                    "metadata_reloading": False, "reload_error": ""}

    def no_key(temperature=None):
        raise ValueError("未找到 deepseek 的 API Key")

    monkeypatch.setattr(config, "get_llm_kwargs", no_key)
    body = client.get("/health").json()
    assert body["status"] == "ok" and "API Key" in body["hint"]          # 元数据照常能用，只提示查数接口不能用


@pytest.mark.parametrize("status", ["ok", "no_metadata", "error"])
def test_health_reports_metadata_reload_status(api, monkeypatch, status) -> None:
    """健康检查的每个分支都如实返回后台换版本状态和失败原因"""
    monkeypatch.setattr(service, "metadata_status", lambda: {
        "reloading": True, "reload_error": "新的元数据库读不了，继续用原来的版本：坏文件"})
    if status != "ok":
        def unavailable():
            if status == "no_metadata":
                raise FileNotFoundError(NO_METADATA)
            raise RuntimeError("读不了元数据库")

        main.app.dependency_overrides[main.get_metadata_dep] = unavailable
    client, _ = api
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == status and body["metadata_reloading"] is True
    assert body["reload_error"] == "新的元数据库读不了，继续用原来的版本：坏文件"


def test_health_reports_metadata_version(api, tiny_mart):
    """健康检查暴露实际加载的元数据库版本号。"""
    from metadata.importer import run_import
    from metadata.store import MetadataStore

    report = run_import(tiny_mart, version_id="20261023-1450")
    store = MetadataStore(report.db_path)
    main.app.dependency_overrides[main.get_metadata_dep] = lambda: (store, None)
    client, _ = api
    assert client.get("/health").json()["metadata_version"] == "20261023-1450"


def test_health_without_metadata_says_so_and_how_to_import(api):
    client, _ = api

    def missing():
        raise FileNotFoundError(NO_METADATA)

    main.app.dependency_overrides[main.get_metadata_dep] = missing
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "no_metadata" and body["counts"] == {} and "python -m metadata.importer" in body["hint"]
    assert body["metadata_version"] is None

    def broken():
        raise RuntimeError("读不了元数据库")

    main.app.dependency_overrides[main.get_metadata_dep] = broken
    body = client.get("/health").json()
    assert body["status"] == "error" and "读不了元数据库" in body["hint"]
    assert body["metadata_version"] is None


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


def test_conversation_request_carries_previous_basis(api, history) -> None:
    """会话查询把最近有需求的一轮依据传给流水线，不含执行数据"""
    client, pipeline = api
    conversation = history.create()
    previous = sample_result()
    history.add_turn(conversation.id, previous.question, previous)
    response = client.post(f"/conversations/{conversation.id}/turns", json={"question": "为什么这样选表"})
    assert response.status_code == 200
    assert pipeline.requests[-1].prev_basis == previous.basis()
    assert "exec_result" not in pipeline.requests[-1].prev_basis.model_dump()
