"""
专题库 NL2SQL 接口（FastAPI），前端（web/）通过它查数、看元数据
查数（请求里可以带 model 换模型，不带用默认模型；结果里的 model 是本轮用的模型）：
- POST /nl2sql/query        自然语言查数，一次返回。流水线无状态：追问、反问的上下文由调用方放在请求里
- POST /nl2sql/stream       同上，按 SSE 逐步返回：每走完一步一条 step 事件，最后一条 result
- GET  /nl2sql/models       能选的模型（只列配了 Key 的）和默认模型
会话（历史存在 资料目录/build/history.db；追问、反问的上下文由服务端从存下的轮次里算出来）：
- GET  /conversations                 最近的会话；POST 新建会话
- GET  /conversations/{id}            会话的全部轮次和当前上下文；PATCH 改标题；DELETE 删除
- POST /conversations/{id}/turns      在会话里提问，SSE：每步一条 step，最后一条 turn（存下的这一轮和新的上下文）
元数据：
- GET  /metadata/tables     按层级列出表
- GET  /metadata/columns    一张表的字段清单，带每个字段的直接来源、直接去向数
- GET  /metadata/search     按关键字找表和字段
- GET  /metadata/lineage    一个字段的上游、下游链路
- GET  /metadata/examples   历史查数样例，界面上给用户参考怎么问
- GET  /health              资料目录、各类元数据的数量、执行层、模型名、方言；元数据库没导入时提示先导入

流水线、元数据、会话历史一律通过 nl2sql.service 模块取（get_pipeline_dep、get_metadata_dep、get_history_dep），
测试里用 app.dependency_overrides 换成假流水线、测试用的元数据和临时目录里的历史库。
元数据库没导入、没配模型 Key 时，接口回 503，信息里写明原因和处理办法，不返回堆栈。
"""
import asyncio
import json
import logging
import os
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, SecretStr, field_validator
from starlette.concurrency import run_in_threadpool

import access_api
import config
from access_control import COOKIE, access_store
from lineage.graph import LineageGraph
from metadata.models import LAYERS
from metadata.store import MetadataStore
from nl2sql import service, model_settings
from nl2sql.access_scope import AccessDenied, AccessScope, DENIED, scoped_indexes, scoped_metadata, sql_tables, table_ids
from nl2sql.executor import NullExecutor, SQLiteExecutor
from nl2sql.history import ConversationDetail, ConversationStore, ConversationSummary
from nl2sql.model_connection import normalize_api_key, normalize_base_url, probe_connection
from nl2sql.schemas import Clarify, QueryRequest, QueryResult, TraceStep
from nl2sql.workflow import Pipeline

logger = logging.getLogger(__name__)

MAX_LINEAGE_DEPTH = 10
UNAVAILABLE_ERRORS = (FileNotFoundError, RuntimeError, ValueError, OSError)     # 取不到流水线、元数据时可能抛的错
SSE_HEARTBEAT_SECONDS = 15.0          # 这么久没有新事件就发一行注释保活：一次模型调用可能几十秒，nginx 默认 60 秒没数据就断开
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}          # 不缓存；让 nginx 收到就转发，不攒着
SSE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"content": {"text/event-stream": {}}, "description": "SSE 事件流，每条事件的 data 是一行 JSON"}}

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """接口启动时在后台线程里预热流水线（装配、加载向量模型），不耽误接口开始接请求"""
    threading.Thread(target=service.warm_up, name="nl2sql-warm-up", daemon=True).start()
    yield


app = FastAPI(
    title="专题库 NL2SQL",
    description="基于专题库模型结构与血缘的自然语言转 SQL：查数、会话历史、元数据浏览、健康检查",
    version="3.1.0",
    lifespan=lifespan,
)
app.include_router(access_api.router)


@app.middleware("http")
async def protect_api(request: Request, call_next: Callable) -> Response:
    """默认认证全部 API，管理和模型配置要求管理员身份。"""
    if not config.ACCESS_CONTROL_ENABLED:
        return await call_next(request)
    path = request.url.path
    if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("X-AgentFlow-Request") != "1":
        return JSONResponse({"detail": "请从应用页面发起请求"}, status_code=403)
    token = request.cookies.get(COOKIE, "")
    user = await run_in_threadpool(lambda: access_store().resolve(token)) if token else None
    request.state.principal = user
    if user is None and path not in {"/auth/me", "/auth/login", "/auth/logout"}:
        return JSONResponse({"detail": "请登录后继续"}, status_code=401)
    if user and not user.admin and path.startswith(("/admin/", "/nl2sql/model-configs", "/nl2sql/model-settings")):
        await run_in_threadpool(access_store().denied, user.id, "admin")
        return JSONResponse({"detail": "需要管理员权限"}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(AccessDenied)
async def handle_access_denied(request: Request, error: AccessDenied) -> JSONResponse:
    """返回统一的授权错误，省略隐藏表名和资源细节。"""
    if user := access_api.principal(request):
        await run_in_threadpool(access_store().denied, user.id, "query")
    return JSONResponse({"detail": DENIED}, status_code=403)


# ── 取不到服务时的统一回应 ──────────────────────────

class ServiceUnavailable(Exception):
    """服务暂时用不了：元数据库没导入、没配模型 Key 等。接口回 503，信息里带处理办法"""

    def __init__(self, message: str, status: str = "error") -> None:
        """
        Args:
            message: 原因和处理办法
            status: no_metadata（元数据库没导入）或 error（其它）
        """
        super().__init__(message)
        self.message = message
        self.status = status


def _unavailable(error: Exception) -> ServiceUnavailable:
    """把取流水线、元数据时抛的错转成 ServiceUnavailable；元数据库不存在记为 no_metadata

    UNAVAILABLE_ERRORS 里的错误信息本来就是写给用户看的，原样用；其它错误带上类型名。
    """
    status = "no_metadata" if isinstance(error, FileNotFoundError) else "error"
    detail = str(error)
    if detail and isinstance(error, UNAVAILABLE_ERRORS):
        return ServiceUnavailable(detail, status)
    return ServiceUnavailable(f"{type(error).__name__}: {detail}".rstrip(": "), status)


@app.exception_handler(ServiceUnavailable)
async def handle_unavailable(request: Request, error: ServiceUnavailable) -> JSONResponse:
    """服务不可用：503，detail 是原因和处理办法"""
    return JSONResponse(status_code=503, content={"detail": error.message, "status": error.status})


def _active_store() -> MetadataStore:
    return service.get_metadata()[0]


def _access_scope(request: Request, store: MetadataStore, *, narrow: bool = False) -> AccessScope | None:
    user = access_api.principal(request)
    if user is None:
        return None
    if narrow and (domain := request.query_params.get("domain")):
        domains = frozenset(item["code"] for item in access_store().domains()) if user.admin else user.domains
        if domain not in domains:
            raise AccessDenied()
        tables = table_ids(store, frozenset({domain}))
    elif user.admin:
        tables = frozenset(table.table_id for table in store.tables())
    else:
        tables = table_ids(store, user.domains)
    return AccessScope(request.cookies[COOKIE], user, store, tables, _active_store)


def get_pipeline_dep(request: Request) -> Pipeline:
    """依赖：查询流水线（nl2sql.service.get_pipeline）。取不到时回 503"""
    try:
        user = access_api.principal(request)
        if user is None:
            return service.get_pipeline()
        scope = _access_scope(request, _active_store(), narrow=True)
        if not scope.tables:
            raise AccessDenied()
        base = service.get_pipeline().deps
        if base.store is not scope.store:
            raise AccessDenied()
        store, graph = scoped_metadata(scope.store, scope.tables)
        retriever, validator = scoped_indexes(store, graph, config.SQL_DIALECT)
        executor = base.executor
        if isinstance(executor, SQLiteExecutor):
            executor = SQLiteExecutor(executor.db_path, executor.source_dialect, allowed_tables=scope.tables)
        elif not isinstance(executor, NullExecutor):
            executor = NullExecutor()
        return Pipeline(replace(base, store=store, graph=graph, retriever=retriever, validator=validator,
                                executor=executor, structure_graph=None, access=scope))
    except AccessDenied:
        raise
    except UNAVAILABLE_ERRORS as error:
        raise _unavailable(error) from error


def get_metadata_dep(request: Request) -> tuple[MetadataStore, LineageGraph]:
    """依赖：元数据和血缘图（nl2sql.service.get_metadata），不需要模型配置。取不到时回 503"""
    try:
        store, graph = service.get_metadata()
        scope = _access_scope(request, store)
        return scoped_metadata(store, scope.tables) if scope else (store, graph)
    except UNAVAILABLE_ERRORS as error:
        raise _unavailable(error) from error


def get_history_dep() -> ConversationStore:
    """依赖：当前资料目录的会话历史库（nl2sql.service.get_history）。目录写不了时回 503"""
    try:
        return service.get_history()
    except UNAVAILABLE_ERRORS as error:
        raise _unavailable(error) from error


# ── 流式输出（SSE） ────────────────────────────────

def _sse(event: str, data: Any) -> str:
    """一条 SSE 消息：事件名一行，数据一行 JSON（json.dumps 不输出换行），空行结束"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _event_stream(produce: Callable[[], Iterator[TraceStep | QueryResult]],
                        finish: Callable[[QueryResult], tuple[str, Any]],
                        scope: AccessScope | None = None) -> AsyncIterator[str]:
    """在后台线程里跑流水线：每走完一步发一条 step 事件，拿到结果后发 finish 给出的事件

    流水线是同步代码，一次模型调用可能要几十秒，所以放进后台线程，事件循环这边等队列；
    超过 SSE_HEARTBEAT_SECONDS 没有新消息就发一行注释（浏览器忽略），免得代理把连接当成空闲断掉。
    浏览器中途断开时这里不再往外发，后台线程照样跑完、finish 照样执行（会话里这一轮照样存下）。
    出了没预料到的错（流水线按约定不抛异常，finish 存历史时可能出错）发一条 error 事件：{"detail": 原因}。

    Args:
        produce: 开始跑流水线，返回 Pipeline.stream 那样的迭代器：若干 TraceStep，最后一个 QueryResult
        finish: 拿到 QueryResult 之后做的事（存历史等），返回最后一条事件的（事件名, 数据）
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    def put(message: str | None) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, message)
        except RuntimeError:                      # 事件循环已经关了（服务在停），没人收了
            pass

    def work() -> None:
        try:
            for item in produce():
                if isinstance(item, QueryResult):
                    put(_sse(*finish(item)))
                else:
                    put(_sse("step", item.model_dump(mode="json")))
        except AccessDenied:
            put(_sse("error", {"detail": DENIED}))
        except Exception as error:  # noqa: BLE001 流式接口不能把堆栈发给浏览器
            logger.exception("流式查数出错")
            detail = "查询处理失败，请重试" if config.ACCESS_CONTROL_ENABLED else f"内部错误：{type(error).__name__}: {error}"
            put(_sse("error", {"detail": detail}))
        finally:
            put(None)

    threading.Thread(target=work, name="nl2sql-stream", daemon=True).start()
    while True:
        try:
            message = await asyncio.wait_for(queue.get(), SSE_HEARTBEAT_SECONDS)
        except TimeoutError:
            yield ": ping\n\n"
            continue
        if message is None:
            return
        if scope:
            try:
                await run_in_threadpool(scope.check_current)
            except AccessDenied:
                yield _sse("error", {"detail": DENIED})
                return
        yield message


def _streaming(produce: Callable[[], Iterator[TraceStep | QueryResult]],
               finish: Callable[[QueryResult], tuple[str, Any]],
               scope: AccessScope | None = None) -> StreamingResponse:
    """SSE 响应：text/event-stream，不缓存、不让代理攒着"""
    return StreamingResponse(_event_stream(produce, finish, scope), media_type="text/event-stream", headers=SSE_HEADERS)


# ── 查数 ──────────────────────────────────────────

class QuestionBody(BaseModel):
    """本轮输入和用哪个模型"""

    question: str = Field(description="本轮输入", min_length=1, max_length=2000)
    model: str | None = Field(None, description="本轮用的模型；千问入口为 qwen，不写用默认模型",
                              max_length=100)
    execute: bool = Field(True, description="查询结果并生成看板；false 时只生成 SQL，配了执行层也不执行")

    @field_validator("question")
    @classmethod
    def question_not_blank(cls, value: str) -> str:
        """本轮输入去掉首尾空白后不能是空的"""
        value = value.strip()
        if not value:
            raise ValueError("question 不能是空白")
        return value

    @field_validator("model")
    @classmethod
    def model_or_none(cls, value: str | None) -> str | None:
        """模型名去掉首尾空白，空的当作没写"""
        if value is None:
            return None
        return value.strip() or None


class QueryBody(QuestionBody):
    """查数请求。追问、反问的上下文由调用方带来：把上一轮结果里的 requirement、clarify 原样放回来，
    上一轮 status 是 ok 时再带上它的 sql 和 validation.tables"""

    prev_requirement: str = Field("", description="上一轮的完整需求（上一轮结果里的 requirement）；追问时带上", max_length=4000)
    prev_clarify: Clarify | None = Field(None, description="上一轮提出、还没回答的反问；本轮的 question 是在回答它"
                                                           "还是问了新问题，由流水线判断")
    prev_sql: str = Field("", description="上一轮校验通过的 SQL（上一轮结果里的 sql，status 是 ok 时）；"
                                          "本轮是在它上面追问时，流水线以它为底增量修改", max_length=20000)
    prev_tables: list[str] = Field(default_factory=list, description="上一轮 SQL 用到的表（上一轮结果里的 validation.tables）",
                                   max_length=50)

    def to_request(self) -> QueryRequest:
        """转成流水线的请求；今天按系统日期，请求体里不收"""
        return QueryRequest(question=self.question, prev_requirement=self.prev_requirement,
                            prev_clarify=self.prev_clarify, prev_sql=self.prev_sql, prev_tables=self.prev_tables,
                            model=self.model, execute=self.execute)


class ModelInfo(BaseModel):
    """能选的一个模型"""

    name: str = Field(description="模型名，查数请求里的 model 填它")
    label: str = Field(description="界面上显示的名字")
    provider: str = Field(description="服务商：deepseek、dashscope 等")


class ModelList(BaseModel):
    """能选的模型"""

    default: str = Field(description="请求里不写 model 时用的模型")
    models: list[ModelInfo] = Field(description="配了 Key 的模型，默认模型排第一")


def _check_model(model: str | None) -> None:
    """请求里指定的模型不在能选的模型里（不认识或者没配 Key）时回 400"""
    if model is None:
        return
    items = model_settings.query_models()
    names = [item['id'] for item in items]
    if not config.ACCESS_CONTROL_ENABLED and any(item['id'] == 'qwen' and item.get('original') for item in items):
        names.extend(choice.name for choice in config.available_models() if choice.provider == 'dashscope')
    if model not in names:
        raise HTTPException(400, f"模型「{model}」用不了（不认识或者没配 Key），可选：{'、'.join(names) or '无'}")


@app.get("/nl2sql/models", response_model=ModelList, summary="能选的模型")
def list_models() -> ModelList:
    """能选的模型：只列配了 Key 的，默认模型排第一；界面右上角的模型下拉框用它"""
    items = model_settings.query_models()
    default = model_settings.default_choice(items)
    if default:
        items = [default, *(item for item in items if item['id'] != default['id'])]
    models = [ModelInfo(name=item['id'], label=item['label'], provider=item['provider']) for item in items]
    return ModelList(default=default['id'] if default else model_settings.default_model_id(), models=models)


class QwenSettings(BaseModel):
    """内置百炼入口实际调用的模型代码。"""
    model: str = Field(min_length=1, max_length=100)


class ModelConfigBody(BaseModel):
    """页面维护的模型名称、服务商和实际调用代码。"""
    label: str = Field(default="", max_length=60)
    provider: str = Field(default="custom", min_length=1, max_length=30)
    model: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    base_url: str | None = Field(default=None, max_length=2048)
    api_key: SecretStr | None = None


class ModelConnectionTestBody(ModelConfigBody):
    """带可选已保存模型 ID 的连接测试请求。"""
    id: str | None = Field(default=None, max_length=128)


@app.exception_handler(RequestValidationError)
async def redact_model_config_validation(request: Request, error: RequestValidationError) -> Response:
    """配置请求校验失败时不返回提交的字段值。"""
    if request.url.path.startswith(('/nl2sql/model-configs', '/auth/', '/admin/')):
        return JSONResponse(status_code=422, content={'detail': [
            {'loc': issue['loc'], 'msg': issue['msg'], 'type': issue['type']} for issue in error.errors()
        ]})
    return await request_validation_exception_handler(request, error)


@app.get("/nl2sql/model-configs")
def list_model_configs() -> dict[str, Any]:
    """列出可配置模型、内置模型和服务商密钥状态，不返回密钥。"""
    providers = [{"name": name, "configured": bool(config._api_key(name))} for name in config.PROVIDERS]
    models = []
    for item in model_settings.model_catalog():
        public = {key: value for key, value in item.items() if key not in {'revision', 'original'}}
        public['key_configured'] = bool(item.get('revision') and model_settings.model_route(item | {'enabled': True}))
        models.append(public)
    selectable = model_settings.query_models()
    default = model_settings.default_choice(selectable)
    return {'models': models, 'providers': providers, 'default': default['id'] if default else None,
            'selectable': [item['id'] for item in selectable]}


@app.post("/nl2sql/model-configs", status_code=201)
def create_model_config(body: ModelConfigBody) -> dict[str, Any]:
    """新增模型配置，自定义密钥由服务端加密保存。"""
    try:
        return model_settings.save_custom_model(body.label, body.provider, body.model, body.enabled,
                                                base_url=body.base_url,
                                                api_key=body.api_key.get_secret_value() if body.api_key else None)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.post("/nl2sql/model-configs/test")
def test_model_connection(body: ModelConnectionTestBody) -> dict[str, Any]:
    """使用表单里的连接发一次短请求，不写入模型配置。"""
    model = body.model.strip()
    if not config.valid_model_code(model):
        raise HTTPException(400, "请填写服务商提供的有效模型名称")
    key = body.api_key.get_secret_value() if body.api_key else ''
    try:
        if body.provider == 'custom':
            base_url = normalize_base_url(body.base_url or '')
            key = normalize_api_key(key) or model_settings.saved_test_api_key(body.id, base_url)
            return probe_connection(base_url, model, key, custom=True)
        if body.base_url or key:
            raise ValueError("请选择自定义接口以填写 Base URL 和 API Key")
        connection = config.get_llm_kwargs(model=f'@{body.provider}:{model}')
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    base_url = (connection.get('base_url') or os.getenv('OPENAI_API_BASE') or os.getenv('OPENAI_BASE_URL')
                or 'https://api.openai.com/v1')
    return probe_connection(base_url, model, connection['api_key'], custom=False,
                            extra_body=connection.get('extra_body'))


@app.put("/nl2sql/model-configs/{model_id}")
def update_model_config(model_id: str, body: ModelConfigBody) -> dict[str, Any]:
    """修改自定义或内置模型。"""
    try:
        return model_settings.save_custom_model(body.label, body.provider, body.model, body.enabled, model_id,
                                                base_url=body.base_url,
                                                api_key=body.api_key.get_secret_value() if body.api_key else None)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


@app.delete("/nl2sql/model-configs/{model_id}", status_code=204)
def delete_model_config(model_id: str) -> Response:
    """删除模型配置和随配置保存的密钥。"""
    try:
        model_settings.delete_model(model_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    return Response(status_code=204)


@app.put("/nl2sql/model-configs/{model_id}/default", status_code=204)
def update_default_model(model_id: str) -> Response:
    """保存新会话与未指定模型的查数请求所使用的默认入口。"""
    try:
        model_settings.set_default_model(model_id)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return Response(status_code=204)


@app.get("/nl2sql/model-settings/qwen", response_model=QwenSettings)
def read_qwen_settings() -> QwenSettings:
    """读取内置百炼入口的当前型号。"""
    item = next((item for item in model_settings.model_catalog() if item['id'] == 'qwen'), None)
    if item is None or item['provider'] != 'dashscope':
        raise HTTPException(404, "百炼入口已删除或更换连接方式")
    return QwenSettings(model=item['model'])


@app.put("/nl2sql/model-settings/qwen", response_model=QwenSettings)
def update_qwen_settings(body: QwenSettings) -> QwenSettings:
    """保存内置百炼入口的型号，下一轮查数立即使用。"""
    _check_model("qwen")
    item = next(item for item in model_settings.model_catalog() if item['id'] == 'qwen')
    if item['provider'] != 'dashscope':
        raise HTTPException(400, "当前入口未使用百炼连接")
    try:
        if not item.get('original'):
            saved = model_settings.save_custom_model(item['label'], 'dashscope', body.model, item['enabled'], 'qwen')
            return QwenSettings(model=saved['model'])
        return QwenSettings(model=model_settings.set_qwen(body.model))
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


def _resolved_model(model: str | None) -> str | None:
    """将会话选择解析为本轮实际服务商与型号。"""
    items = model_settings.query_models()
    if config.ACCESS_CONTROL_ENABLED:
        item = next((item for item in items if item["id"] == model), None) if model else model_settings.default_choice(items)
        if item is None or not (route := model_settings.model_route(item)):
            raise HTTPException(400, "请选择已批准用于查数的模型")
        return route
    if model is None:
        item = model_settings.default_choice(items)
        if item is None:
            default = next((item for item in model_settings.model_catalog()
                            if item['id'] == model_settings.default_model_id()), None)
            if default is None or not default.get('original'):
                raise HTTPException(400, "没有可用模型，请在模型配置中新增或启用一个模型")
            return None
        if item['id'] == model_settings.default_model_id() and item.get('original') and item['id'] != 'qwen':
            return None
    else:
        item = next((item for item in items if item['id'] == model), None)
        if item is None:
            _check_model(model)
            return model
    if not item.get('original') or item['id'] == 'qwen':
        return model_settings.model_route(item) or f"@{item['provider']}:{item['model']}"
    if model is None:
        return f"@{item['provider']}:{item['model']}"
    return model


@app.post("/nl2sql/query", summary="自然语言查数")
def query(body: QueryBody, pipeline: Pipeline = Depends(get_pipeline_dep)) -> dict[str, Any]:
    """跑一轮查询，返回 QueryResult（不含提示词和模型原始回复）

    status 是 ok、unvalidated（SQL 有校验错误，需人工检查）、need_clarify（要回答 clarify 里的问题）、explained（在问上一轮的结果，看 explanation）或 failed。
    今天按系统日期，请求体里不收。model 不在能选的模型里时回 400。
    """
    result = pipeline.run(_query_request(body))
    if config.ACCESS_CONTROL_ENABLED and pipeline.deps.access:
        pipeline.deps.access.check_current()
        if result.message == DENIED:
            access_store().denied(pipeline.deps.access.user.id, "query")
    return result.public()


@app.post("/nl2sql/stream", summary="自然语言查数（流式）", response_class=StreamingResponse, responses=SSE_RESPONSES)
def query_stream(body: QueryBody, pipeline: Pipeline = Depends(get_pipeline_dep)) -> StreamingResponse:
    """和 /nl2sql/query 一样跑一轮查询，按 SSE 逐步返回

    事件：每走完一步一条 step（data 是 TraceStep），最后一条 result（data 同 /nl2sql/query 的返回）；
    出了没预料到的错时最后一条是 error（data 是 {"detail": 原因}）。model 不在能选的模型里时回 400。
    """
    request = _query_request(body)
    scope = pipeline.deps.access if config.ACCESS_CONTROL_ENABLED else None

    def finish(result: QueryResult) -> tuple[str, Any]:
        if scope and result.message == DENIED:
            access_store().denied(scope.user.id, "query")
        return "result", result.public()

    return _streaming(lambda: pipeline.stream(request), finish, scope)


def _query_request(body: QueryBody) -> QueryRequest:
    if config.ACCESS_CONTROL_ENABLED and (body.prev_sql or body.prev_requirement or body.prev_tables or body.prev_clarify):
        raise HTTPException(400, "请在已登录的会话中继续追问")
    request = body.to_request()
    request.model = _resolved_model(body.model)
    return request


# ── 会话 ──────────────────────────────────────────

class ConversationBody(BaseModel):
    """新建会话；标题不写时用第一轮提问的前几个字"""

    title: str = Field("", max_length=200)


class RenameBody(BaseModel):
    """改会话标题"""

    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str) -> str:
        """标题去掉首尾空白后不能是空的"""
        value = value.strip()
        if not value:
            raise ValueError("title 不能是空白")
        return value


def _conversation_missing(conversation_id: str) -> HTTPException:
    """会话不存在：404"""
    return HTTPException(404, f"会话不存在：{conversation_id}")


def _history_scope(request: Request) -> dict:
    if access_api.principal(request) is None:
        return {}
    scope = _access_scope(request, _active_store())
    scope.check_current()
    return {"owner_id": scope.user.id, "allowed": scope.tables}


@app.get("/conversations", response_model=list[ConversationSummary], summary="最近的会话")
def conversations(request: Request, limit: int = Query(50, ge=1, le=200, description="最多列几个"),
                  history: ConversationStore = Depends(get_history_dep)) -> list[ConversationSummary]:
    """最近的会话，按最后一轮的时间从新到旧"""
    return history.list_recent(limit, **_history_scope(request))


@app.post("/conversations", response_model=ConversationSummary, status_code=201, summary="新建会话")
def create_conversation(request: Request, body: ConversationBody | None = None,
                        history: ConversationStore = Depends(get_history_dep)) -> ConversationSummary:
    """新建一个空会话，返回它的标识；之后用 POST /conversations/{id}/turns 提问"""
    user = access_api.principal(request)
    return history.create(body.title if body is not None else "", owner_id=user.id if user else None)


@app.get("/conversations/{conversation_id}", response_model=ConversationDetail, summary="会话详情")
def conversation(request: Request, conversation_id: str,
                 history: ConversationStore = Depends(get_history_dep)) -> ConversationDetail:
    """会话的全部轮次（从早到晚，每轮带流水线结果）和下一轮要用的上下文（上一轮的完整需求、还没回答的反问）"""
    detail = history.get(conversation_id, **_history_scope(request))
    if detail is None:
        raise _conversation_missing(conversation_id)
    return detail


@app.patch("/conversations/{conversation_id}", response_model=ConversationSummary, summary="改会话标题")
def rename_conversation(request: Request, conversation_id: str, body: RenameBody,
                        history: ConversationStore = Depends(get_history_dep)) -> ConversationSummary:
    """改会话标题"""
    summary = history.rename(conversation_id, body.title, **_history_scope(request))
    if summary is None:
        raise _conversation_missing(conversation_id)
    return summary


@app.delete("/conversations/{conversation_id}", status_code=204, response_class=Response, summary="删除会话")
def delete_conversation(request: Request, conversation_id: str,
                        history: ConversationStore = Depends(get_history_dep)) -> Response:
    """删掉会话和它的全部轮次"""
    if not history.delete(conversation_id, **_history_scope(request)):
        raise _conversation_missing(conversation_id)
    model_settings.delete_conversation_model(conversation_id)
    return Response(status_code=204)


class ConversationModel(BaseModel):
    """会话选择的模型入口。"""
    model: str | None = Field(None, max_length=100)


@app.get("/conversations/{conversation_id}/model", response_model=ConversationModel)
def get_conversation_model(request: Request, conversation_id: str,
                           history: ConversationStore = Depends(get_history_dep)) -> ConversationModel:
    """读取会话保存的模型入口。"""
    if history.get(conversation_id, **_history_scope(request)) is None:
        raise _conversation_missing(conversation_id)
    return ConversationModel(model=model_settings.get_conversation_model(conversation_id))


@app.put("/conversations/{conversation_id}/model", response_model=ConversationModel)
def set_conversation_model(request: Request, conversation_id: str, body: ConversationModel,
                           history: ConversationStore = Depends(get_history_dep)) -> ConversationModel:
    """保存会话模型入口，下一次提问生效。"""
    if history.get(conversation_id, **_history_scope(request)) is None:
        raise _conversation_missing(conversation_id)
    if not body.model:
        raise HTTPException(400, "请选择模型")
    _check_model(body.model)
    model_settings.set_conversation_model(conversation_id, body.model)
    return body


@app.post("/conversations/{conversation_id}/turns", summary="在会话里提问（流式）", response_class=StreamingResponse,
          responses=SSE_RESPONSES)
def ask_in_conversation(conversation_id: str, body: QuestionBody, pipeline: Pipeline = Depends(get_pipeline_dep),
                        history: ConversationStore = Depends(get_history_dep)) -> StreamingResponse:
    """在会话里问一轮：上一轮的完整需求和还没回答的反问由服务端从会话里取，跑完把这一轮存进会话

    事件：每走完一步一条 step（data 是 TraceStep），最后一条 turn，data 是
    {"turn": 存下的这一轮（id、question、created_at、result）, "context": 下一轮的上下文}；
    出了没预料到的错时最后一条是 error（data 是 {"detail": 原因}）。
    model 换模型，存下的这一轮的 result.model 是本轮用的模型；不在能选的模型里时回 400。
    """
    scope = pipeline.deps.access if config.ACCESS_CONTROL_ENABLED else None
    history_scope = {"owner_id": scope.user.id, "allowed": scope.tables} if scope else {}
    if scope:
        scope.check_current()
        detail = history.get(conversation_id, **history_scope)
        if detail is None:
            raise _conversation_missing(conversation_id)
        context = detail.context
        prior = next((turn.result for turn in reversed(detail.turns) if turn.result.get("requirement")), None)
        basis = QueryResult.model_validate(prior).basis() if prior else None
    else:
        try:
            context = history.context(conversation_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        basis = history.basis(conversation_id)
    resolved_model = _resolved_model(body.model or model_settings.get_conversation_model(conversation_id))
    request = QueryRequest(question=body.question, prev_requirement=context.prev_requirement,
                           prev_clarify=context.prev_clarify, prev_sql=context.prev_sql, prev_tables=context.prev_tables,
                           prev_basis=basis,
                           model=resolved_model, execute=body.execute)

    def finish(result: QueryResult) -> tuple[str, Any]:
        if scope:
            scope.check_current()
            if result.message == DENIED:
                access_store().denied(scope.user.id, "query")
        turn = history.add_turn(conversation_id, body.question, result, **history_scope)
        latest = history.get(conversation_id, **history_scope).context if scope else history.context(conversation_id)
        return "turn", {"turn": turn.model_dump(mode="json"),
                        "context": latest.model_dump(mode="json")}

    return _streaming(lambda: pipeline.stream(request), finish, scope)


# ── 元数据 ────────────────────────────────────────

class TableInfo(BaseModel):
    """表清单里的一项"""

    table_id: str
    table_cn: str
    layer: str
    table_type: str
    grain_columns: list[str]
    date_column: str | None
    description: str
    column_count: int
    domain_code: str = ""


class ColumnInfo(BaseModel):
    """字段清单里的一项"""

    column_name: str
    column_cn: str
    ordinal: int
    data_type: str
    is_pk: bool
    description: str
    enum_values: str
    upstream_count: int = Field(description="直接来源有几条血缘")
    downstream_count: int = Field(description="直接加工出几个字段")


class ColumnHit(BaseModel):
    """搜索命中的一个字段"""

    table_id: str
    table_cn: str
    layer: str
    column_name: str
    column_cn: str
    data_type: str


class SearchResult(BaseModel):
    """按关键字找到的表和字段"""

    tables: list[TableInfo]
    columns: list[ColumnHit]
    table_total: int
    column_total: int


class HopInfo(BaseModel):
    """血缘的一跳：target 由 source 加工而来"""

    depth: int
    target: str
    source: str
    transform: str
    script_expression: str
    is_passthrough: bool
    origin: str
    script_name: str
    source_external: bool
    source_known: bool
    filters: list[str]
    joins: list[str]


class FieldBrief(BaseModel):
    """血缘图上的一个字段"""

    table: str
    column: str
    table_cn: str
    layer: str
    column_cn: str
    known: bool = Field(description="在不在字段清单里；外部表的字段不在")


class LineageInfo(BaseModel):
    """一个字段的上下游链路"""

    field: str
    upstream: list[HopInfo]
    downstream: list[HopInfo]
    fields: dict[str, FieldBrief]


class ExampleInfo(BaseModel):
    """一条历史查数样例"""

    example_id: str
    requirement: str


def _check_layer(layer: str | None) -> None:
    """层级不对时回 400"""
    if layer is not None and layer not in LAYERS:
        raise HTTPException(400, f"层级「{layer}」不对，可选：{'、'.join(LAYERS)}")


@app.get("/metadata/tables", response_model=list[TableInfo], summary="按层级列出表")
def metadata_tables(
    layer: str | None = Query(None, description=f"层级，可选：{'、'.join(LAYERS)}；不写就列出全部表"),
    metadata: tuple[MetadataStore, LineageGraph] = Depends(get_metadata_dep),
) -> list[dict[str, Any]]:
    """列出表：表标识、中文名、层级、类型、粒度字段、数据日期字段、说明、字段数"""
    _check_layer(layer)
    store, _ = metadata
    return [service.table_summary(store, table) for table in store.tables(layer)]


@app.get("/metadata/columns", response_model=list[ColumnInfo], summary="一张表的字段清单")
def metadata_columns(
    table: str = Query(description="表标识：模式.表"),
    metadata: tuple[MetadataStore, LineageGraph] = Depends(get_metadata_dep),
) -> list[dict[str, Any]]:
    """表的字段，按表内顺序：字段名、中文名、类型、主键、口径说明、码值，以及直接来源、直接去向各有几条血缘"""
    store, graph = metadata
    try:
        return service.column_summaries(store, graph, table)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


@app.get("/metadata/search", response_model=SearchResult, summary="按关键字找表和字段")
def metadata_search(
    q: str = Query(min_length=1, max_length=100, description="关键字：表名、字段名或中文名的一部分，不区分大小写"),
    limit: int = Query(50, ge=1, le=200, description="表和字段各自最多返回几个"),
    metadata: tuple[MetadataStore, LineageGraph] = Depends(get_metadata_dep),
) -> dict[str, Any]:
    """找表名或中文名含关键字的表、字段名或中文名含关键字的字段；完全一样的排前面，同一档里专题层排前面"""
    store, _ = metadata
    return service.search_metadata(store, q, limit)


@app.get("/metadata/lineage", response_model=LineageInfo, summary="一个字段的上游、下游链路")
def metadata_lineage(
    table: str = Query(description="表标识：模式.表"),
    column: str = Query(description="字段名"),
    depth: int = Query(3, ge=1, le=MAX_LINEAGE_DEPTH, description="最多走几跳"),
    metadata: tuple[MetadataStore, LineageGraph] = Depends(get_metadata_dep),
) -> dict[str, Any]:
    """字段的上游（由哪些字段加工而来）和下游（加工出了哪些字段），每一跳带加工表达式、过滤条件、关联条件；

    fields 是链路上每个字段的中文名、所在表和层级，画血缘图用。
    """
    store, graph = metadata
    try:
        return service.describe_lineage(store, graph, table, column, depth)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


@app.get("/metadata/examples", response_model=list[ExampleInfo], summary="历史查数样例")
def metadata_examples(
    request: Request,
    limit: int = Query(8, ge=1, le=100, description="最多返回几条"),
    domain: str | None = Query(None, description="只展示指定业务域可用的样例"),
    metadata: tuple[MetadataStore, LineageGraph] = Depends(get_metadata_dep),
) -> list[ExampleInfo]:
    """按授权表筛选样例，并在多个业务域之间轮流选取。"""
    store, _ = metadata
    user = access_api.principal(request)
    allowed = None
    examples = store.examples()
    if domain:
        domains = ({item["code"] for item in access_store().domains()} if user and user.admin
                   else user.domains if user else {table.domain_code for table in store.tables()})
        if domain not in domains:
            raise AccessDenied()
        allowed = table_ids(store, frozenset({domain}))
        examples = []
        for example in store.examples():
            if example.tables and allowed.issuperset(example.tables):
                try:
                    if sql_tables(example.sql_text) == frozenset(example.tables):
                        examples.append(example)
                except AccessDenied:
                    pass
    from nl2sql.suggestions import question_suggestions
    suggestions = question_suggestions(config.MART_DIR, store, allowed)
    if user is not None or suggestions:
        table_domains = {table.table_id: table.domain_code for table in store.tables()}
        groups: dict[str, list[ExampleInfo]] = {}
        for example in examples:
            codes = sorted({table_domains.get(table, "") for table in example.tables})
            groups.setdefault(codes[0] if codes else "", []).append(
                ExampleInfo(example_id=example.example_id, requirement=example.requirement))
        for item in suggestions:
            groups.setdefault(item['domain'], []).append(
                ExampleInfo(example_id=item['example_id'], requirement=item['requirement']))
        selected = []
        while groups and len(selected) < limit:
            for code in list(groups):
                selected.append(groups[code].pop(0))
                if not groups[code]:
                    del groups[code]
                if len(selected) == limit:
                    break
        return selected
    return [ExampleInfo(example_id=example.example_id, requirement=example.requirement)
            for example in examples[:limit]]


# ── 健康检查 ──────────────────────────────────────

class HealthInfo(BaseModel):
    """服务状态"""

    status: Literal["ok", "no_metadata", "error"]
    mart_dir: str
    counts: dict[str, int]
    executor: str
    model: str
    dialect: str
    hint: str = ""
    metadata_version: str | None = None
    metadata_reloading: bool = False
    reload_error: str = ""


def _model_info() -> tuple[str, str]:
    """(模型名, 提示)：没配模型 Key 时提示查数接口暂时不能用，元数据接口不受影响"""
    choice = model_settings.default_choice(model_settings.query_models())
    if config.ACCESS_CONTROL_ENABLED:
        return (choice["model"], "") if choice else ("", "查数接口暂时不能用：没有已批准的模型")
    default_id = model_settings.default_model_id()
    if choice and (choice['id'] != default_id or not choice.get('original') or default_id == 'qwen'):
        return choice['model'], ""
    if not choice and not any(item['id'] == default_id and item.get('original')
                              for item in model_settings.model_catalog()):
        return config.LLM_MODEL, "查数接口暂时不能用：请在模型配置中新增或启用一个模型"
    try:
        return config.get_llm_kwargs()["model"], ""
    except ValueError as error:
        return config.LLM_MODEL, f"查数接口暂时不能用：{error}"


@app.get("/health", response_model=HealthInfo, summary="健康检查")
def health(request: Request) -> HealthInfo:
    """服务状态：资料目录、各类元数据的数量、执行层、模型名、SQL 方言

    status 为 ok；no_metadata 表示元数据库还没导入，hint 里是导入命令；error 表示元数据读不出来。
    """
    model, model_hint = _model_info()
    user = access_api.principal(request)
    hide_internal = user is not None and not user.admin
    base = {"mart_dir": "" if hide_internal else str(config.MART_DIR), "executor": config.EXECUTOR, "model": model,
            "dialect": config.SQL_DIALECT}
    provider = request.app.dependency_overrides.get(get_metadata_dep, get_metadata_dep)     # 测试里换掉的依赖也要认
    try:
        store, _ = provider(request) if provider is get_metadata_dep else provider()
    except Exception as error:  # noqa: BLE001 健康检查要如实报告，不能自己先崩
        problem = error if isinstance(error, ServiceUnavailable) else _unavailable(error)
        state = service.metadata_status()
        return HealthInfo(status=problem.status, counts={}, hint="元数据暂不可用" if hide_internal else problem.message,
                          metadata_reloading=state["reloading"],
                          reload_error="" if hide_internal else state["reload_error"], **base)
    state = service.metadata_status()
    return HealthInfo(status="ok", counts=store.counts(), hint=model_hint,
                      metadata_version=store.version_id, metadata_reloading=state["reloading"],
                      reload_error="" if hide_internal else state["reload_error"], **base)
