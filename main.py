"""
专题库 NL2SQL 接口（FastAPI），前端（web/）通过它查数、看元数据
查数：
- POST /nl2sql/query        自然语言查数，一次返回。流水线无状态：追问、反问的上下文由调用方放在请求里
- POST /nl2sql/stream       同上，按 SSE 逐步返回：每走完一步一条 step 事件，最后一条 result
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
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

import config
from lineage.graph import LineageGraph
from metadata.models import LAYERS
from metadata.store import MetadataStore
from nl2sql import service
from nl2sql.history import ConversationDetail, ConversationStore, ConversationSummary
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


def get_pipeline_dep() -> Pipeline:
    """依赖：查询流水线（nl2sql.service.get_pipeline）。取不到时回 503"""
    try:
        return service.get_pipeline()
    except UNAVAILABLE_ERRORS as error:
        raise _unavailable(error) from error


def get_metadata_dep() -> tuple[MetadataStore, LineageGraph]:
    """依赖：元数据和血缘图（nl2sql.service.get_metadata），不需要模型配置。取不到时回 503"""
    try:
        return service.get_metadata()
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
                        finish: Callable[[QueryResult], tuple[str, Any]]) -> AsyncIterator[str]:
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
        except Exception as error:  # noqa: BLE001 流式接口不能把堆栈发给浏览器
            logger.exception("流式查数出错")
            put(_sse("error", {"detail": f"内部错误：{type(error).__name__}: {error}"}))
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
        yield message


def _streaming(produce: Callable[[], Iterator[TraceStep | QueryResult]],
               finish: Callable[[QueryResult], tuple[str, Any]]) -> StreamingResponse:
    """SSE 响应：text/event-stream，不缓存、不让代理攒着"""
    return StreamingResponse(_event_stream(produce, finish), media_type="text/event-stream", headers=SSE_HEADERS)


# ── 查数 ──────────────────────────────────────────

class QuestionBody(BaseModel):
    """本轮输入"""

    question: str = Field(description="本轮输入", min_length=1, max_length=2000)

    @field_validator("question")
    @classmethod
    def question_not_blank(cls, value: str) -> str:
        """本轮输入去掉首尾空白后不能是空的"""
        value = value.strip()
        if not value:
            raise ValueError("question 不能是空白")
        return value


class QueryBody(QuestionBody):
    """查数请求。追问、反问的上下文由调用方带来：把上一轮结果里的 requirement、clarify 原样放回来"""

    prev_requirement: str = Field("", description="上一轮的完整需求（上一轮结果里的 requirement）；追问时带上", max_length=4000)
    prev_clarify: Clarify | None = Field(None, description="上一轮提出、还没回答的反问；本轮的 question 是在回答它"
                                                           "还是问了新问题，由流水线判断")

    def to_request(self) -> QueryRequest:
        """转成流水线的请求；今天按系统日期，请求体里不收"""
        return QueryRequest(question=self.question, prev_requirement=self.prev_requirement,
                            prev_clarify=self.prev_clarify)


@app.post("/nl2sql/query", summary="自然语言查数")
def query(body: QueryBody, pipeline: Pipeline = Depends(get_pipeline_dep)) -> dict[str, Any]:
    """跑一轮查询，返回 QueryResult（不含提示词和模型原始回复）

    status 是 ok、unvalidated（SQL 有校验错误，需人工检查）、need_clarify（要回答 clarify 里的问题）或 failed。
    今天按系统日期，请求体里不收。
    """
    return pipeline.run(body.to_request()).public()


@app.post("/nl2sql/stream", summary="自然语言查数（流式）", response_class=StreamingResponse, responses=SSE_RESPONSES)
def query_stream(body: QueryBody, pipeline: Pipeline = Depends(get_pipeline_dep)) -> StreamingResponse:
    """和 /nl2sql/query 一样跑一轮查询，按 SSE 逐步返回

    事件：每走完一步一条 step（data 是 TraceStep），最后一条 result（data 同 /nl2sql/query 的返回）；
    出了没预料到的错时最后一条是 error（data 是 {"detail": 原因}）。
    """
    request = body.to_request()
    return _streaming(lambda: pipeline.stream(request), lambda result: ("result", result.public()))


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


@app.get("/conversations", response_model=list[ConversationSummary], summary="最近的会话")
def conversations(limit: int = Query(50, ge=1, le=200, description="最多列几个"),
                  history: ConversationStore = Depends(get_history_dep)) -> list[ConversationSummary]:
    """最近的会话，按最后一轮的时间从新到旧"""
    return history.list_recent(limit)


@app.post("/conversations", response_model=ConversationSummary, status_code=201, summary="新建会话")
def create_conversation(body: ConversationBody | None = None,
                        history: ConversationStore = Depends(get_history_dep)) -> ConversationSummary:
    """新建一个空会话，返回它的标识；之后用 POST /conversations/{id}/turns 提问"""
    return history.create(body.title if body is not None else "")


@app.get("/conversations/{conversation_id}", response_model=ConversationDetail, summary="会话详情")
def conversation(conversation_id: str, history: ConversationStore = Depends(get_history_dep)) -> ConversationDetail:
    """会话的全部轮次（从早到晚，每轮带流水线结果）和下一轮要用的上下文（上一轮的完整需求、还没回答的反问）"""
    detail = history.get(conversation_id)
    if detail is None:
        raise _conversation_missing(conversation_id)
    return detail


@app.patch("/conversations/{conversation_id}", response_model=ConversationSummary, summary="改会话标题")
def rename_conversation(conversation_id: str, body: RenameBody,
                        history: ConversationStore = Depends(get_history_dep)) -> ConversationSummary:
    """改会话标题"""
    summary = history.rename(conversation_id, body.title)
    if summary is None:
        raise _conversation_missing(conversation_id)
    return summary


@app.delete("/conversations/{conversation_id}", status_code=204, response_class=Response, summary="删除会话")
def delete_conversation(conversation_id: str, history: ConversationStore = Depends(get_history_dep)) -> Response:
    """删掉会话和它的全部轮次"""
    if not history.delete(conversation_id):
        raise _conversation_missing(conversation_id)
    return Response(status_code=204)


@app.post("/conversations/{conversation_id}/turns", summary="在会话里提问（流式）", response_class=StreamingResponse,
          responses=SSE_RESPONSES)
def ask_in_conversation(conversation_id: str, body: QuestionBody, pipeline: Pipeline = Depends(get_pipeline_dep),
                        history: ConversationStore = Depends(get_history_dep)) -> StreamingResponse:
    """在会话里问一轮：上一轮的完整需求和还没回答的反问由服务端从会话里取，跑完把这一轮存进会话

    事件：每走完一步一条 step（data 是 TraceStep），最后一条 turn，data 是
    {"turn": 存下的这一轮（id、question、created_at、result）, "context": 下一轮的上下文}；
    出了没预料到的错时最后一条是 error（data 是 {"detail": 原因}）。
    """
    try:
        context = history.context(conversation_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    request = QueryRequest(question=body.question, prev_requirement=context.prev_requirement,
                           prev_clarify=context.prev_clarify)

    def finish(result: QueryResult) -> tuple[str, Any]:
        turn = history.add_turn(conversation_id, body.question, result)
        return "turn", {"turn": turn.model_dump(mode="json"),
                        "context": history.context(conversation_id).model_dump(mode="json")}

    return _streaming(lambda: pipeline.stream(request), finish)


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
    limit: int = Query(8, ge=1, le=100, description="最多返回几条"),
    metadata: tuple[MetadataStore, LineageGraph] = Depends(get_metadata_dep),
) -> list[ExampleInfo]:
    """样例库里的需求，按样例库里的顺序；界面上给用户参考怎么问。资料里没有样例时是空列表"""
    store, _ = metadata
    return [ExampleInfo(example_id=example.example_id, requirement=example.requirement)
            for example in store.examples()[:limit]]


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


def _model_info() -> tuple[str, str]:
    """(模型名, 提示)：没配模型 Key 时提示查数接口暂时不能用，元数据接口不受影响"""
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
    base = {"mart_dir": str(config.MART_DIR), "executor": config.EXECUTOR, "model": model,
            "dialect": config.SQL_DIALECT}
    provider = request.app.dependency_overrides.get(get_metadata_dep, get_metadata_dep)     # 测试里换掉的依赖也要认
    try:
        store, _ = provider()
    except Exception as error:  # noqa: BLE001 健康检查要如实报告，不能自己先崩
        problem = error if isinstance(error, ServiceUnavailable) else _unavailable(error)
        return HealthInfo(status=problem.status, counts={}, hint=problem.message, **base)
    return HealthInfo(status="ok", counts=store.counts(), hint=model_hint, **base)
