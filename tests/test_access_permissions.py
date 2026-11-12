"""按域验证登录、模型证据、真实流水线、SQLite 执行与会话隔离。"""
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import main
from access_control import COOKIE, Grant, Principal, access_store
from metadata.importer import load_mart
from metadata.models import Mart, TableMeta
from metadata.store import MetadataStore, VectorBundle, VectorSet
from nl2sql import service
from nl2sql.access_scope import AccessDenied, AccessScope, DENIED, scoped_metadata, sql_tables
from nl2sql.executor import SQLiteExecutor
from tests.access_fixture import FUND, PROJECT, UNCLASSIFIED, PilotLLM, build_access_mart, pilot_pipeline
from tests.test_api import sse_events

HEADERS = {"X-AgentFlow-Request": "1"}
PASSWORD = "test-only-Passphrase-12"


@pytest.fixture
def pilot(tmp_path: Path, monkeypatch):
    """本地合成库、三个不同身份和真正的 HTTP 路由。"""
    monkeypatch.setattr(config, "MART_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ACCESS_DB", "")
    monkeypatch.setattr(config, "ACCESS_CONTROL_ENABLED", True)
    monkeypatch.setattr(config, "ACCESS_MODEL_IDS", frozenset({"deepseek-flash"}))
    monkeypatch.setattr(config, "LLM_PROVIDER", "deepseek")
    monkeypatch.setattr(config, "LLM_MODEL", "deepseek-flash")
    monkeypatch.setattr(config, "EXECUTOR", "sqlite")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-model-only")
    db = build_access_mart(tmp_path)
    access = access_store()
    admin = access.save_user(actor=None, username="pilot-admin", name="权限管理员", active=True, admin=True,
                             grants=[], password=PASSWORD)
    access.save_domain(admin["id"], "research", "项目域")
    access.save_domain(admin["id"], "finance", "经费域")
    users = {}
    for name, code in [("project-user", "research"), ("fund-user", "finance")]:
        users[name] = access.save_user(actor=admin["id"], username=name, name=name, active=True,
                                       admin=False, grants=[Grant(code)], password=PASSWORD)
    model = PilotLLM()
    store, graph = service.get_metadata()
    pipeline = pilot_pipeline(store, graph, db, model)
    monkeypatch.setattr(service, "get_pipeline", lambda: pipeline)
    yield db, model, users
    service.reset_pipeline()


def client(username: str | None = None) -> TestClient:
    """使用浏览器同样的会话 Cookie 登录。"""
    result = TestClient(main.app, base_url="http://127.0.0.1", headers=HEADERS)
    if username:
        logged_in = result.post("/auth/login", json={"username": username, "password": PASSWORD})
        assert logged_in.status_code == 200, logged_in.text
        assert "httponly" in logged_in.headers["set-cookie"].lower()
    return result


def test_domain_field_import_and_legacy_default(tiny_mart, tiny_sheets, tmp_path):
    """新清单导入归属，旧格式继续读取，非法代码被拦截。"""
    from tests.helpers import write_mart

    mart, _ = load_mart(tiny_mart)
    assert all(table.domain_code == "" for table in mart.tables)
    rows = tiny_sheets["表清单"]
    for row in rows:
        row.extend(["", "research"])
    mart, _ = load_mart(write_mart(tmp_path / "with-domain", tiny_sheets))
    assert {table.domain_code for table in mart.tables} == {"research"}
    rows[0][-1] = "INVALID CODE"
    mart, issues = load_mart(write_mart(tmp_path / "bad-domain", tiny_sheets))
    assert mart is None and any("业务域代码" in issue.message for issue in issues)


@pytest.mark.parametrize("username,own,other", [("project-user", PROJECT, FUND), ("fund-user", FUND, PROJECT)])
def test_metadata_health_and_admin_follow_login_and_domain(pilot, username, own, other):
    """表、字段、搜索、血缘和样例均应用同一域权限。"""
    assert client().get("/metadata/tables").status_code == 401
    user = client(username)
    assert [item["table_id"] for item in user.get("/metadata/tables").json()] == [own]
    assert user.get("/health").json()["counts"]["表"] == 1
    assert user.get("/metadata/columns", params={"table": own}).status_code == 200
    assert user.get("/metadata/columns", params={"table": other}).status_code == 404
    assert user.get("/metadata/columns", params={"table": UNCLASSIFIED}).status_code == 404
    search = user.get("/metadata/search", params={"q": other.split(".")[1]}).json()
    assert not search["tables"] and not search["columns"]
    lineage = user.get("/metadata/lineage", params={"table": own, "column": "quantity"}).json()
    assert not lineage["upstream"] and not lineage["downstream"] and other not in str(lineage)
    examples = user.get("/metadata/examples").json()
    assert len(examples) == 1 and examples[0]["example_id"] != "pilot-mixed"
    assert user.get("/admin/users").status_code == 403
    assert user.get("/nl2sql/model-configs").status_code == 403


@pytest.mark.parametrize("username,question,other,denied_question,value", [
    ("project-user", "统计项目立项数量", FUND, "统计经费支出合计", 8),
    ("fund-user", "统计经费支出合计", PROJECT, "统计项目立项数量", 30),
])
def test_scoped_query_runs_real_sqlite_and_rejects_out_of_domain(
    pilot, username, question, other, denied_question, value,
):
    """两域各自查数成功，点名另一域时不给出 SQL 和结果。"""
    _, model, _ = pilot
    user = client(username)
    result = user.post("/nl2sql/query", json={"question": question}).json()
    assert result["status"] == "ok", result
    assert result["exec_result"]["rows"] == [[value]]
    assert other not in result["sql"] and all(other not in prompt for prompt in model.evidence_prompts)
    denied = user.post("/nl2sql/query", json={"question": denied_question}).json()
    assert denied["message"] == DENIED and not denied["sql"] and denied["exec_result"] is None
    assert user.post("/nl2sql/query", json={"question": question, "prev_sql": f"SELECT * FROM {other}"}).status_code == 400
    assert user.post("/nl2sql/query", params={"domain": "none"}, json={"question": question}).json()["status"] == "ok"


def test_admin_always_uses_all_tables_even_with_legacy_domain_parameter(pilot):
    """管理员使用全部表，旧版域参数不再缩小查数范围。"""
    admin = client("pilot-admin")
    assert {item["code"] for item in admin.get("/auth/me").json()["user"]["domains"]} == {"research", "finance"}
    assert {item["table_id"] for item in admin.get("/metadata/tables").json()} == {PROJECT, FUND, UNCLASSIFIED}
    assert admin.get("/metadata/columns", params={"table": UNCLASSIFIED}).status_code == 200
    result = admin.post("/nl2sql/query", json={"question": f"统计 {UNCLASSIFIED}"}).json()
    assert result["status"] == "ok" and result["exec_result"]["rows"] == [[189]]
    narrowed = admin.post("/nl2sql/query", params={"domain": "finance"}, json={"question": "统计经费支出合计"}).json()
    assert narrowed["status"] == "ok" and narrowed["exec_result"]["rows"] == [[30]]
    assert admin.post("/nl2sql/query", params={"domain": "finance"}, json={"question": "统计项目立项数量"}).json()["status"] == "ok"


def test_example_suggestions_follow_selected_domain_and_multiple_grants(pilot):
    """多域样例轮流展示，切换域后不返回跨域或未授权样例。"""
    access = access_store()
    actor = next(user["id"] for user in access.users() if user["username"] == "pilot-admin")
    access.save_user(actor=actor, username="multi-user", name="多域用户", active=True, admin=False,
                     grants=[Grant("research"), Grant("finance")], password=PASSWORD)
    store, _ = service.get_metadata()
    store._examples.insert(1, store._examples[0].model_copy(update={"example_id": "pilot-1-extra"}))
    multi = client("multi-user")
    examples = multi.get("/metadata/examples", params={"limit": 2}).json()
    assert {item["example_id"] for item in examples} == {"pilot-1", "pilot-2"}
    for domain, expected in [("research", "pilot-1"), ("finance", "pilot-2")]:
        response = multi.get("/metadata/examples", params={"domain": domain})
        assert response.status_code == 200
        ids = [item["example_id"] for item in response.json()]
        assert ids == ([expected, "pilot-1-extra"] if domain == "research" else [expected])
    assert client("project-user").get("/metadata/examples", params={"domain": "finance"}).status_code == 403
    assert client().get("/metadata/examples").status_code == 401


def test_scoped_vectors_share_full_scope_and_filter_partial_scope():
    """全表授权复用向量，单域范围只保留已授权向量。"""
    import numpy as np

    tables = [TableMeta(table_id=ident, schema_name=ident.split('.')[0], table_name=ident.split('.')[1],
                        table_cn='测试表', layer='专题层', table_type='全量表', grain_columns=[], domain_code=domain)
              for ident, domain in [(PROJECT, 'research'), (FUND, 'finance')]]
    vectors = VectorSet('table', [PROJECT, FUND], ['项目', '经费'], np.eye(2, dtype=np.float32))
    store = MetadataStore.from_mart(Mart(tables=tables), VectorBundle('test', 2, {'table': vectors}))
    full, _ = scoped_metadata(store, frozenset({PROJECT, FUND}))
    assert full.vectors('table') is vectors
    partial, _ = scoped_metadata(store, frozenset({PROJECT}))
    assert partial.vectors('table').ref_ids == [PROJECT]
    assert not np.shares_memory(partial.vectors('table').matrix, vectors.matrix)


def test_reference_questions_respect_domains_and_do_not_become_sql_examples(pilot, monkeypatch):
    """参考问法只用于展示，不能泄露跨域表或作为 SQL 证据。"""
    path = Path(config.MART_DIR) / 'build' / 'suggested_questions.json'
    path.write_text(json.dumps([
        {'id': 'research', 'question': '参考项目问法', 'tables': [PROJECT]},
        {'id': 'finance', 'question': '参考经费问法', 'tables': [FUND]},
        {'id': 'mixed', 'question': '不应展示的跨域问法', 'tables': [PROJECT, FUND]},
        {'id': 'unknown', 'question': '不应展示的未知表问法', 'tables': ['other.missing']},
        {'id': 'none', 'question': '不应展示的空表问法', 'tables': []},
    ], ensure_ascii=False), encoding='utf-8')
    research = client('project-user').get('/metadata/examples').json()
    assert {item['example_id'] for item in research} == {'pilot-1', 'suggestion:research'}
    finance = client('fund-user').get('/metadata/examples').json()
    assert {item['example_id'] for item in finance} == {'pilot-2', 'suggestion:finance'}
    admin = client('pilot-admin')
    examples = admin.get('/metadata/examples', params={'limit': 2}).json()
    assert {item['example_id'] for item in examples} == {'pilot-1', 'pilot-2'}
    selected = admin.get('/metadata/examples', params={'domain': 'research'}).json()
    assert {item['example_id'] for item in selected} == {'pilot-1', 'suggestion:research'}
    calls = []
    original = main.scoped_metadata
    def observe(store, allowed):
        calls.append(allowed)
        return original(store, allowed)
    monkeypatch.setattr(main, 'scoped_metadata', observe)
    assert admin.get('/metadata/examples', params={'domain': 'finance'}).status_code == 200
    assert len(calls) == 1  # 已授权视图只取一次，选域不再复制另一份向量。
    store, _ = service.get_metadata()
    assert {item.example_id for item in store.examples()} == {'pilot-1', 'pilot-2', 'pilot-mixed'}


def test_generated_cross_domain_sql_is_hidden_in_sync_and_stream(pilot):
    """模型生成越权 SQL 时，普通接口和流式接口都丢弃 SQL。"""
    db, model, _ = pilot
    model.generated_sql = f"WITH t AS (SELECT * FROM {FUND}) SELECT * FROM t"
    user = client("project-user")
    body = {"question": "统计项目立项数量"}
    result = user.post("/nl2sql/query", json=body)
    assert result.json()["message"] == DENIED and FUND not in result.text
    stream = user.post("/nl2sql/stream", json=body)
    assert FUND not in stream.text and sse_events(stream.text)[-1][1]["message"] == DENIED
    with pytest.raises(AccessDenied):
        sql_tables("SELECT * FROM unqualified")
    assert sql_tables(model.generated_sql) == {FUND}
    assert not SQLiteExecutor(db, allowed_tables=frozenset({PROJECT})).execute(model.generated_sql).executed


def test_known_table_name_containing_shorter_out_of_scope_name_is_allowed():
    """授权表完整中文名和域外表的较短名称重叠时，分辨用户实际点名。"""
    tables = [
        TableMeta(table_id=f"pilot.{name}", schema_name="pilot", table_name=name,
                  table_cn=label, layer="专题层", table_type="全量表", grain_columns=[])
        for name, label in [("approved", "创新计划信息表"), ("hidden", "计划信息表"),
                            ("archive", "历年创新计划信息表归档")]
    ]
    store = MetadataStore.from_mart(Mart(tables=tables))
    user = Principal("u1", "demo-user", "测试用户", False, frozenset({"research"}))
    scope = AccessScope("", user, store, frozenset({"pilot.approved"}), lambda: store)
    scope.check_names(["统计创新计划信息表的记录数"], exact=False)
    for text in ["统计计划信息表", "关联创新计划信息表和计划信息表", "查历年创新计划信息表归档"]:
        with pytest.raises(AccessDenied):
            scope.check_names([text], exact=False)
    with pytest.raises(AccessDenied):
        scope.check_sql("SELECT * FROM pilot.hidden")


def test_history_ownership_revoke_expiry_and_session_logout(pilot):
    """他人和旧会话不可读取，撤权后本人也不能打开或续问旧结果。"""
    _, _, users = pilot
    owner = client("project-user")
    other = client("fund-user")
    admin = client("pilot-admin")
    legacy = service.get_history().create("旧共享会话")
    ident = owner.post("/conversations", json={}).json()["id"]
    path = f"/conversations/{ident}"
    answer = owner.post(f"{path}/turns", json={"question": "统计项目立项数量"})
    assert sse_events(answer.text)[-1][1]["turn"]["result"]["status"] == "ok"
    assert owner.get(path).status_code == 200
    assert not other.get("/conversations").json()
    assert other.get(f"/conversations/{legacy.id}").status_code == 404
    assert other.get(path).status_code == admin.get(path).status_code == 404
    assert other.get(f"{path}/model").status_code == 404
    assert other.put(f"{path}/model", json={"model": "deepseek-flash"}).status_code == 404
    assert other.patch(path, json={"title": "越权操作"}).status_code == 404
    assert other.delete(path).status_code == 404
    assert other.post(f"{path}/turns", json={"question": "继续统计"}).status_code == 404
    managed = users["project-user"]
    now = int(time.time())
    changed = admin.put(f"/admin/users/{managed['id']}", json=managed | {
        "grants": [{"code": "research", "starts_at": now - 60, "expires_at": now - 10}],
    })
    assert changed.status_code == 200
    assert owner.get("/metadata/tables").json() == []
    assert owner.get(path).status_code == 404 and not owner.get("/conversations").json()
    assert owner.post(f"{path}/turns", json={"question": "继续统计"}).status_code == 403
    token = other.cookies.get(COOKIE)
    assert other.post("/auth/logout").status_code == 204
    assert access_store().resolve(token) is None
    assert other.get("/metadata/tables").status_code == 401


def test_admin_can_register_domain_grant_and_disable_user(pilot):
    """管理员可维护账号和域，普通用户不能提权或提交跨站请求。"""
    _, _, users = pilot
    admin = client("pilot-admin")
    user = client("fund-user")
    assert admin.get("/admin/users").json()["unclassified"] == 1
    assert admin.put("/admin/domains", json={"code": "shared", "name": "公共域"}).status_code == 204
    assert user.put("/admin/domains", json={"code": "new", "name": "无权"}).status_code == 403
    managed = users["fund-user"]
    assert admin.put(f"/admin/users/{managed['id']}", json=managed | {"active": False}).status_code == 200
    assert user.get("/metadata/tables").status_code == 401
    assert TestClient(main.app).post("/auth/login", json={}).status_code == 403
    assert MetadataStore(Path(config.MART_DIR) / "build/metadata.db").table(FUND).domain_code == "finance"


def test_default_model_requires_admin_and_approved_connection(pilot, monkeypatch):
    """业务用户不能改全局默认，管理员只能设置获准的模型。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    admin = client("pilot-admin")
    user = client("project-user")
    assert admin.put("/nl2sql/model-configs/qwen/default").status_code == 400
    path = "/nl2sql/model-configs/deepseek-flash/default"
    assert user.put(path).status_code == 403
    assert admin.put(path).status_code == 204
    configs = admin.get("/nl2sql/model-configs").json()
    assert configs["default"] == "deepseek-flash" and configs["selectable"] == ["deepseek-flash"]


def test_query_uses_current_admin_default_and_all_granted_domains(pilot, monkeypatch):
    """旧模型选择及业务域参数不影响新旧会话的模型和授权范围。"""
    from nl2sql import model_settings

    _, model, _ = pilot
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setattr(config, "ACCESS_MODEL_IDS", frozenset({"deepseek-flash", "qwen"}))
    access = access_store()
    actor = next(u["id"] for u in access.users() if u["username"] == "pilot-admin")
    access.save_user(actor=actor, username="multi-user", name="多域用户", active=True, admin=False,
                     grants=[Grant("research"), Grant("finance")], password=PASSWORD)
    calls = []
    pipeline = service.get_pipeline()
    pipeline.deps.llm_for = lambda route: calls.append(route) or model
    admin = client("pilot-admin")
    user = client("multi-user")
    ident = user.post("/conversations", json={}).json()["id"]
    model_settings.set_conversation_model(ident, "deepseek-flash")
    assert admin.put("/nl2sql/model-configs/qwen/default").status_code == 204
    assert user.get(f"/conversations/{ident}/model").json()["model"] == "qwen"
    assert user.put(f"/conversations/{ident}/model", json={"model": "deepseek-flash"}).status_code == 403
    for endpoint in ("/nl2sql/query", "/nl2sql/stream", f"/conversations/{ident}/turns"):
        response = user.post(endpoint, params={"domain": "finance"},
                             json={"question": "统计项目立项数量", "model": "deepseek-flash"})
        assert response.status_code == 200
        result = response.json() if endpoint.endswith("query") else sse_events(response.text)[-1][1]
        if endpoint.endswith("turns"):
            result = result["turn"]["result"]
        assert result["status"] == "ok" and result["exec_result"]["rows"] == [[8]]
    assert len(calls) == 3 and all(route.startswith("@dashscope:") for route in calls)
    calls.clear()
    assert admin.put("/nl2sql/model-configs/deepseek-flash/default").status_code == 204
    response = user.post(f"/conversations/{ident}/turns", json={"question": "统计项目立项数量", "model": "qwen"})
    assert sse_events(response.text)[-1][1]["turn"]["result"]["status"] == "ok"
    assert len(calls) == 1 and calls[0].startswith("@deepseek:")


def test_revoking_domain_during_generation_stops_stream_and_history(pilot, monkeypatch):
    """生成中撤权，流式接口不给出结果，旧授权不能继续写入历史。"""
    _, model, users = pilot
    user = client("project-user")
    admin = client("pilot-admin")
    path = f"/conversations/{user.post('/conversations', json={}).json()['id']}"
    original = model.complete

    def complete(messages, *, json_mode=False):
        reply = original(messages, json_mode=json_mode)
        if not json_mode:
            account = users["project-user"]
            assert admin.put(f"/admin/users/{account['id']}", json=account | {"grants": []}).status_code == 200
        return reply

    monkeypatch.setattr(model, "complete", complete)
    response = user.post(f"{path}/turns", json={"question": "统计项目立项数量"})
    events = sse_events(response.text)
    assert events[-1] == ("error", {"detail": DENIED})
    assert all(event != "turn" for event, _ in events)
    assert user.get(path).json()["turns"] == []
