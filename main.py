"""
专题库 NL2SQL 接口（FastAPI）
- POST /nl2sql/query      自然语言查数。流水线无状态：追问、反问的上下文由调用方放在请求里
- GET  /metadata/tables   按层级列出表
- GET  /metadata/lineage  一个字段的上游、下游链路
- GET  /health            资料目录、各类元数据的数量、执行层、模型名；元数据库没导入时提示先导入

流水线和元数据一律通过 nl2sql.service 模块取（get_pipeline_dep、get_metadata_dep 两个依赖函数），
测试里用 app.dependency_overrides 换成假流水线和测试用的元数据。
元数据库没导入、没配模型 Key 时，接口回 503，信息里写明原因和处理办法，不返回堆栈。
"""
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

import config
from lineage.graph import LineageGraph
from metadata.models import LAYERS
from metadata.store import MetadataStore
from nl2sql import service
from nl2sql.schemas import Clarify, QueryRequest
from nl2sql.workflow import Pipeline

MAX_LINEAGE_DEPTH = 10
UNAVAILABLE_ERRORS = (FileNotFoundError, RuntimeError, ValueError, OSError)     # 取不到流水线、元数据时可能抛的错

app = FastAPI(
    title="专题库 NL2SQL",
    description="基于专题库模型结构与血缘的自然语言转 SQL：查数、元数据浏览、健康检查",
    version="3.0.0",
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


# ── 查数 ──────────────────────────────────────────

class QueryBody(BaseModel):
    """查数请求。追问、反问的上下文由调用方带来：把上一轮结果里的 requirement、clarify 原样放回来"""

    question: str = Field(description="本轮输入", min_length=1, max_length=2000)
    prev_requirement: str = Field("", description="上一轮的完整需求（上一轮结果里的 requirement）；追问时带上", max_length=4000)
    prev_clarify: Clarify | None = Field(None, description="上一轮提出的反问；有值表示本轮的 question 是对它的回答")

    @field_validator("question")
    @classmethod
    def question_not_blank(cls, value: str) -> str:
        """本轮输入去掉首尾空白后不能是空的"""
        value = value.strip()
        if not value:
            raise ValueError("question 不能是空白")
        return value


@app.post("/nl2sql/query", summary="自然语言查数")
def query(body: QueryBody, pipeline: Pipeline = Depends(get_pipeline_dep)) -> dict[str, Any]:
    """跑一轮查询，返回 QueryResult（不含提示词和模型原始回复）

    status 是 ok、unvalidated（SQL 有校验错误，需人工检查）、need_clarify（要回答 clarify 里的问题）或 failed。
    今天按系统日期，请求体里不收。
    """
    request = QueryRequest(question=body.question, prev_requirement=body.prev_requirement,
                           prev_clarify=body.prev_clarify)
    return pipeline.run(request).public()


# ── 元数据 ────────────────────────────────────────

class TableInfo(BaseModel):
    """表清单里的一项"""

    table_id: str
    table_cn: str
    layer: str
    table_type: str
    grain_columns: list[str]
    date_column: str | None
    column_count: int


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


class LineageInfo(BaseModel):
    """一个字段的上下游链路"""

    field: str
    upstream: list[HopInfo]
    downstream: list[HopInfo]


@app.get("/metadata/tables", response_model=list[TableInfo], summary="按层级列出表")
def metadata_tables(
    layer: str | None = Query(None, description=f"层级，可选：{'、'.join(LAYERS)}；不写就列出全部表"),
    metadata: tuple[MetadataStore, LineageGraph] = Depends(get_metadata_dep),
) -> list[TableInfo]:
    """列出表：表标识、中文名、层级、类型、粒度字段、数据日期字段、字段数"""
    if layer is not None and layer not in LAYERS:
        raise HTTPException(400, f"层级「{layer}」不对，可选：{'、'.join(LAYERS)}")
    store, _ = metadata
    return [TableInfo(table_id=table.table_id, table_cn=table.table_cn, layer=table.layer, table_type=table.table_type,
                      grain_columns=table.grain_columns, date_column=table.date_column,
                      column_count=len(store.columns(table.table_id)))
            for table in store.tables(layer)]


@app.get("/metadata/lineage", response_model=LineageInfo, summary="一个字段的上游、下游链路")
def metadata_lineage(
    table: str = Query(description="表标识：模式.表"),
    column: str = Query(description="字段名"),
    depth: int = Query(3, ge=1, le=MAX_LINEAGE_DEPTH, description="最多走几跳"),
    metadata: tuple[MetadataStore, LineageGraph] = Depends(get_metadata_dep),
) -> dict[str, Any]:
    """字段的上游（由哪些字段加工而来）和下游（加工出了哪些字段），每一跳带加工表达式、过滤条件、关联条件"""
    store, graph = metadata
    try:
        return service.describe_lineage(store, graph, table, column, depth)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


# ── 健康检查 ──────────────────────────────────────

class HealthInfo(BaseModel):
    """服务状态"""

    status: Literal["ok", "no_metadata", "error"]
    mart_dir: str
    counts: dict[str, int]
    executor: str
    model: str
    hint: str = ""


def _model_info() -> tuple[str, str]:
    """(模型名, 提示)：没配模型 Key 时提示查数接口暂时不能用，元数据接口不受影响"""
    try:
        return config.get_llm_kwargs()["model"], ""
    except ValueError as error:
        return config.LLM_MODEL, f"查数接口暂时不能用：{error}"


@app.get("/health", response_model=HealthInfo, summary="健康检查")
def health(request: Request) -> HealthInfo:
    """服务状态：资料目录、各类元数据的数量、执行层、模型名

    status 为 ok；no_metadata 表示元数据库还没导入，hint 里是导入命令；error 表示元数据读不出来。
    """
    model, model_hint = _model_info()
    base = {"mart_dir": str(config.MART_DIR), "executor": config.EXECUTOR, "model": model}
    provider = request.app.dependency_overrides.get(get_metadata_dep, get_metadata_dep)     # 测试里换掉的依赖也要认
    try:
        store, _ = provider()
    except Exception as error:  # noqa: BLE001 健康检查要如实报告，不能自己先崩
        problem = error if isinstance(error, ServiceUnavailable) else _unavailable(error)
        return HealthInfo(status=problem.status, counts={}, hint=problem.message, **base)
    return HealthInfo(status="ok", counts=store.counts(), hint=model_hint, **base)
