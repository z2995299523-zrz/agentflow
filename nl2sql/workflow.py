"""
查询流水线的 LangGraph 编排
七个节点：parse → recall → evidence → plan → generate → validate → output，三条回路：
- ④ 说缺东西（need_upstream）时上溯一层，带着缺的东西回到 ②，最多上溯 MAX_SCOPE_LEVEL 层
- ⑥ 校验有错误时带着错误回到 ⑤，最多修正 MAX_REPAIRS 次
- ④ 反问（need_clarify）、做不到（cannot_answer）时直接去 ⑦

流水线本身无状态，不带检查点：追问、反问的上下文由调用方放进 QueryRequest。
Pipeline.run 不向外抛异常，节点里没接住的程序错误也包成 failed（停在 internal）。
"""
import operator
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Annotated, Any, TypedDict

from lineage.graph import LineageGraph
from metadata.store import MetadataStore
from nl2sql.executor import Executor, NullExecutor
from nl2sql.llm import LLMClient
from nl2sql.retriever import RecallResult, Retriever
from nl2sql.schemas import (
    CallRecord,
    Evidence,
    ParsedRequest,
    QueryPlan,
    QueryRequest,
    QueryResult,
    TraceStep,
    ValidationSummary,
)
from nl2sql.validator import SqlValidator

MAX_SCOPE_LEVEL = 2
MAX_REPAIRS = 2
RECURSION_LIMIT = 40


@dataclass
class PipelineDeps:
    """流水线的依赖，由 nl2sql.service 按资料目录装配，测试里手工组装"""

    store: MetadataStore
    graph: LineageGraph
    retriever: Retriever
    validator: SqlValidator
    llm: LLMClient
    executor: Executor = field(default_factory=NullExecutor)
    structure_graph: LineageGraph | None = None       # A 组求关联路径用；None 时第一次跑 A 组再建，建好留着
    mart_dir: Path | None = None


class PipelineState(TypedDict, total=False):
    """流水线的状态。节点只返回自己改动的字段；trace、calls 追加合并"""

    request: QueryRequest
    today: date
    parsed: ParsedRequest
    scope_level: int
    base_tables: list[str]
    extra_terms: list[str]
    recall: RecallResult
    evidence: Evidence
    plan: QueryPlan
    sql: str
    repair_rounds: int
    validation: ValidationSummary
    first_pass_ok: bool
    status: str
    failed_step: str
    message: str
    result: QueryResult
    trace: Annotated[list[TraceStep], operator.add]
    calls: Annotated[list[CallRecord], operator.add]
    started: float


def build_workflow(deps: PipelineDeps) -> Any:
    """建 LangGraph 图并编译，不带检查点

    Args:
        deps: 流水线的依赖

    Returns:
        编译好的图
    """
    raise NotImplementedError("由 W2-D 实现")


class Pipeline:
    """查询流水线：界面、接口、评估共用"""

    def __init__(self, deps: PipelineDeps) -> None:
        """
        Args:
            deps: 流水线的依赖
        """
        raise NotImplementedError("由 W2-D 实现")

    @property
    def deps(self) -> PipelineDeps:
        """流水线的依赖"""
        raise NotImplementedError("由 W2-D 实现")

    def run(self, request: QueryRequest) -> QueryResult:
        """跑一轮查询，不抛异常：任何错误都包成 status=failed 的结果"""
        raise NotImplementedError("由 W2-D 实现")

    def stream(self, request: QueryRequest) -> Iterator[TraceStep | QueryResult]:
        """跑一轮查询，逐步给出 TraceStep，最后一个是 QueryResult；不抛异常"""
        raise NotImplementedError("由 W2-D 实现")
