"""
查询流水线的 LangGraph 编排
七个节点：parse → recall → evidence → plan → generate → validate → output，三条回路：
- ④ 说缺东西（need_upstream）时上溯一层，带着缺的东西回到 ②，最多上溯 MAX_SCOPE_LEVEL 层
- ⑥ 校验有错误时带着错误回到 ⑤，最多修正 MAX_REPAIRS 次
- ① 反问（缺了没法默认的要素）、④ 反问（口径分岔）、④ 做不到（cannot_answer）时直接去 ⑦
- ④ 结论是 ok 时先由 prune_plan 去掉用不到的表，再去 ⑤

流水线本身无状态，不带检查点：追问、反问的上下文由调用方放进 QueryRequest。
Pipeline.run 不向外抛异常，节点里没接住的程序错误也包成 failed（停在 internal）。

对照实验的开关在这里生效（use_lineage 为假就是 A 组）：
- A 组：召回在全部层级的表里排名（all_layers），证据没有血缘，关联路径来自只按表结构推导的图，不允许上溯
- use_examples 为假：召回结果先去掉样例和样例带入的表
"""
import logging
import operator
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from lineage.graph import LineageGraph
from metadata.store import MetadataStore
from nl2sql.evidence import build_evidence, drop_examples, structure_store
from nl2sql.executor import Executor, NullExecutor
from nl2sql.generator import generate_sql
from nl2sql.llm import LLMClient, StepError
from nl2sql.output import (
    format_sql,
    merge_assumptions,
    no_candidate_message,
    summarize_exec,
    summarize_validation,
    upstream_failure_message,
)
from nl2sql.parser import parse_request, to_recall_query
from nl2sql.planner import clarify_daily_average, plan_query, prune_plan
from nl2sql.retriever import RecallResult, Retriever
from nl2sql.schemas import (
    INTERNAL_STEP,
    STEP_LABELS,
    CallRecord,
    Evidence,
    ExecSummary,
    ParsedRequest,
    QueryPlan,
    QueryRequest,
    QueryResult,
    TimeSpec,
    TraceStep,
    ValidationSummary,
)
from nl2sql.validator import SqlValidator

MAX_SCOPE_LEVEL = 2
MAX_REPAIRS = 2
RECURSION_LIMIT = 40
EXEC_MAX_ROWS = 200                       # 配置了执行层时最多取回几行
SUMMARY_TABLES = 8                        # 轨迹摘要里最多列几张表
SUMMARY_ERROR_CHARS = 40                  # 轨迹摘要里每条校验错误最多留几个字

STATUS_OK, STATUS_UNVALIDATED, STATUS_CLARIFY, STATUS_FAILED = "ok", "unvalidated", "need_clarify", "failed"

logger = logging.getLogger(__name__)

_STRUCTURE_LOCK = threading.Lock()


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


# ── 小工具 ──────────────────────────────────────

def _node_name(step: str) -> str:
    """图里的节点名。不直接用步骤名：recall、evidence、plan 同时是状态里的字段名"""
    return f"{step}_node"


def _unique(items: Iterable[str]) -> list[str]:
    """去掉首尾空白和空串，保序去重"""
    return list(dict.fromkeys(text for text in (item.strip() for item in items) if text))


def _short(text: str, limit: int) -> str:
    """折叠空白后截到 limit 个字，超出的部分用省略号"""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def _names(items: Iterable[str], limit: int = SUMMARY_TABLES) -> str:
    """名字列表写成「a、b、c…」，超过 limit 个的部分用省略号"""
    names = list(items)
    text = "、".join(names[:limit])
    return text + "…" if len(names) > limit else text


def _time_text(time_spec: TimeSpec) -> str:
    """时间要素写成一句话：时点 2026-09-30、区间 a 至 b、日均 …、不限定日期，有对比时接上对比日期"""
    dates = time_spec.dates
    if time_spec.kind == "point":
        text = f"时点 {'、'.join(dates)}"
    elif time_spec.kind == "range":
        text = f"区间 {' 至 '.join(dates)}"
    elif time_spec.kind == "daily_avg":
        text = f"日均，截止 {'、'.join(dates)}"
    else:
        text = "不限定日期"
    if time_spec.compare != "none" and time_spec.compare_dates:
        text += f"，对比 {'、'.join(time_spec.compare_dates)}"
    return text


def _structure_graph(deps: PipelineDeps) -> LineageGraph:
    """A 组求关联路径用的图：只有表结构推得出的关联。第一次用到时建，加锁只建一次，建好留在 deps 里"""
    graph = deps.structure_graph
    if graph is None:
        with _STRUCTURE_LOCK:
            graph = deps.structure_graph
            if graph is None:
                graph = deps.structure_graph = LineageGraph(structure_store(deps.store))
    return graph


def _step_label(step: str, state: PipelineState) -> str:
    """轨迹里的标签：上溯时加「（上溯第 N 层）」，修正时加「（第 N 次）」"""
    label = STEP_LABELS[step]
    if step in ("recall", "evidence", "plan") and state.get("scope_level", 0) > 0:
        return f"{label}（上溯第 {state['scope_level']} 层）"
    if step in ("generate", "validate") and state.get("repair_rounds", 0) > 0:
        return f"{label}（第 {state['repair_rounds'] + 1} 次）"
    return label


def _internal_message(error: BaseException) -> str:
    """没接住的程序错误写成「内部错误：类型: 信息」"""
    return f"内部错误：{type(error).__name__}: {error}"


def _guarded(step: str, run: Callable[[PipelineState], tuple[dict[str, Any], str]]
             ) -> Callable[[PipelineState], dict[str, Any]]:
    """把一个步骤的实现包成图里的节点

    - 接住 StepError：状态置为 failed，记下停在哪一步、原因，并追加它带的模型调用记录
    - 接住其它异常：状态置为 failed，停在 internal；记日志，轨迹和已有的调用记录都留着
    - 最后追加一条 TraceStep
    """

    def node(state: PipelineState) -> dict[str, Any]:
        started = time.perf_counter()
        label = _step_label(step, state)
        try:
            updates, summary = run(state)
        except StepError as error:
            updates = {"status": STATUS_FAILED, "failed_step": error.step or step, "message": error.message,
                       "calls": list(error.calls)}
            summary = f"失败：{error.message}"
        except Exception as error:  # noqa: BLE001 流水线不向外抛异常，没料到的错误也要变成 failed
            logger.exception("流水线的 %s 步出现没有预料到的错误", step)
            message = _internal_message(error)
            updates = {"status": STATUS_FAILED, "failed_step": INTERNAL_STEP, "message": message}
            summary = f"失败：{message}"
        updates["trace"] = [TraceStep(step=step, label=label, elapsed_ms=int((time.perf_counter() - started) * 1000),
                                      summary=summary)]
        return updates

    return node


# ── 七个步骤的实现 ──────────────────────────────────

class _Steps:
    """七个节点的实现。除 output 外，每个方法返回（状态更新，一句话摘要），由 _guarded 包装成节点"""

    def __init__(self, deps: PipelineDeps) -> None:
        self._deps = deps

    # ① 需求解析
    def parse(self, state: PipelineState) -> tuple[dict[str, Any], str]:
        """调模型解析需求；不是查数需求时流程结束，把模型给用户的回复作为原因；缺了没法默认的要素时反问"""
        parsed, calls = parse_request(self._deps.llm, state["request"], state["today"])
        updates: dict[str, Any] = {"parsed": parsed, "calls": calls}
        if not parsed.is_query:
            updates.update(status=STATUS_FAILED, failed_step="parse", message=parsed.reply)
            return updates, "不是查数需求"
        if parsed.clarify is not None and state["request"].prev_clarify is None:
            updates["status"] = STATUS_CLARIFY
            return updates, f"需要反问：{parsed.clarify.question}"
        summary = (f"完整需求：{parsed.requirement}；指标 {len(parsed.metrics)} 个、维度 {len(parsed.dimensions)} 个、"
                   f"过滤 {len(parsed.filters)} 个；时间：{_time_text(parsed.time)}")
        return updates, summary

    # ② 候选召回
    def recall(self, state: PipelineState) -> tuple[dict[str, Any], str]:
        """召回候选表；A 组在全部层级里排名；不用样例时先去掉样例；一张候选也没有时流程结束"""
        request = state["request"]
        query = to_recall_query(state["parsed"], state["extra_terms"])
        all_layers = not request.use_lineage
        recall = self._deps.retriever.recall(query, state["scope_level"], state["base_tables"], all_layers=all_layers)
        if not request.use_examples:
            recall = drop_examples(recall)
        updates: dict[str, Any] = {"recall": recall}
        if not recall.tables:
            updates.update(status=STATUS_FAILED, failed_step="recall",
                           message=no_candidate_message(query, recall.notes))
        scope = "全部层级" if all_layers else f"第 {recall.scope_level} 层"
        summary = (f"{scope}，范围 {recall.scope_size} 张表，候选 {len(recall.tables)} 张："
                   f"{_names(table.table_id for table in recall.tables)}；样例 {len(recall.examples)} 条")
        return updates, summary

    # ③ 证据组装
    def evidence(self, state: PipelineState) -> tuple[dict[str, Any], str]:
        """由召回结果组装证据；A 组的关联路径来自只按表结构推导的图"""
        request = state["request"]
        query = to_recall_query(state["parsed"], state["extra_terms"])
        join_graph = None if request.use_lineage else _structure_graph(self._deps)
        evidence = build_evidence(self._deps.store, self._deps.graph, state["recall"], query,
                                  use_lineage=request.use_lineage, use_examples=request.use_examples,
                                  all_layers=not request.use_lineage, join_graph=join_graph)
        summary = (f"表 {len(evidence.tables)} 张、血缘字段 {len(evidence.lineage)} 个、"
                   f"同源对比 {len(evidence.comparisons)} 组、关联路径 {len(evidence.joins)} 条")
        return {"evidence": evidence}, summary

    # ④ 选表定口径
    def plan(self, state: PipelineState) -> tuple[dict[str, Any], str]:
        """调模型定方案，按结论转向：反问、做不到直接结束；缺东西且没到顶就上溯一层，到顶了就失败；
        能反问而需求只说「日均」时改成反问；结论是 ok 时先去掉用不到的表"""
        request = state["request"]
        allow_clarify = request.prev_clarify is None
        plan, calls = plan_query(self._deps.llm, state["parsed"], state["evidence"],
                                 allow_clarify=allow_clarify, allow_upstream=request.use_lineage)
        if allow_clarify:
            plan = clarify_daily_average(state["parsed"], state["evidence"], plan)
        plan, removed = prune_plan(plan, state["evidence"])
        updates: dict[str, Any] = {"plan": plan, "calls": calls}
        level = state["scope_level"]
        if plan.decision == "need_clarify":
            updates["status"] = STATUS_CLARIFY
            return updates, f"需要反问：{plan.clarify.question if plan.clarify else ''}"
        if plan.decision == "cannot_answer":
            updates.update(status=STATUS_FAILED, failed_step="plan", message=plan.reason)
            return updates, f"做不到：{plan.reason}"
        if plan.decision == "need_upstream":
            missing = "、".join(_unique(plan.missing))
            if level >= MAX_SCOPE_LEVEL:
                updates.update(status=STATUS_FAILED, failed_step="plan", message=upstream_failure_message(plan, level))
                return updates, f"需要上溯，但已经上溯到第 {level} 层：缺 {missing}"
            updates.update(scope_level=level + 1, base_tables=[table.table_id for table in state["recall"].tables],
                           extra_terms=_unique([*state["extra_terms"], *plan.missing]))
            return updates, f"需要上溯：缺 {missing}"
        summary = f"结论 ok：用表 {_names(item.table for item in plan.tables)}"
        return updates, summary + (f"；去掉用不到的表 {_names(removed)}" if removed else "")

    # ⑤ 生成 SQL
    def generate(self, state: PipelineState) -> tuple[dict[str, Any], str]:
        """调模型写 SQL；修正轮带上一次的 SQL 和校验错误（只带错误，不带警告）"""
        rounds = state.get("repair_rounds", 0)
        previous = state.get("sql", "") if rounds else ""
        errors = state["validation"].errors if rounds and "validation" in state else []
        sql, record = generate_sql(self._deps.llm, state["parsed"], state["plan"], state["evidence"],
                                   previous_sql=previous, errors=errors)
        if rounds == 0:
            summary = "第 1 次生成"
        else:
            rules = _unique(error.split("：", 1)[0] for error in errors)
            summary = f"第 {rounds + 1} 次：修正 {'、'.join(rules)}"
        if not sql:
            summary += "（回复里没有取到 SQL）"
        return {"sql": sql, "calls": [record]}, summary

    # ⑥ 静态校验
    def validate(self, state: PipelineState) -> tuple[dict[str, Any], str]:
        """校验 SQL：期望的表和字段取自方案，方案去掉的表不能再用；
        没有错误为 ok，有错误且没修满就回去修，修满了为 unvalidated"""
        plan = state["plan"]
        expected_tables = [item.table for item in plan.tables]
        expected_columns = [f"{item.table}.{item.column}" for item in plan.fields
                            if item.usage in ("metric", "dimension")]
        summary = summarize_validation(self._deps.validator.validate(state["sql"], expected_tables, expected_columns,
                                                                     excluded_tables=plan.dropped_tables))
        updates: dict[str, Any] = {"validation": summary}
        if "first_pass_ok" not in state:
            updates["first_pass_ok"] = summary.ok
        rounds = state.get("repair_rounds", 0)
        if summary.ok:
            updates["status"] = STATUS_OK
            text = "通过" + (f"，警告 {len(summary.warnings)} 条" if summary.warnings else "")
        else:
            if rounds < MAX_REPAIRS:
                updates["repair_rounds"] = rounds + 1
            else:
                updates["status"] = STATUS_UNVALIDATED
            shown = "；".join(_short(error, SUMMARY_ERROR_CHARS) for error in summary.errors[:3])
            text = f"错误 {len(summary.errors)} 条：{shown}" + ("…" if len(summary.errors) > 3 else "")
        return updates, text

    # ⑦ 输出
    def output(self, state: PipelineState) -> dict[str, Any]:
        """组装最终结果：SQL 排版，ok 时按需执行；轨迹里带上本步，本步出错也要给出一个 failed 的结果"""
        started = time.perf_counter()
        overrides: dict[str, Any] = {}
        try:
            sql, exec_result, summary = self._finalize(state)
        except Exception as error:  # noqa: BLE001 输出是最后一步，出错也要有结果
            logger.exception("流水线的 output 步出现没有预料到的错误")
            sql, exec_result = "", None
            overrides = {"status": STATUS_FAILED, "failed_step": INTERNAL_STEP, "message": _internal_message(error)}
            summary = f"失败：{overrides['message']}"
        step = TraceStep(step="output", label=STEP_LABELS["output"],
                         elapsed_ms=int((time.perf_counter() - started) * 1000), summary=summary)
        result = _build_result(state, sql=sql, exec_result=exec_result, trace=[*state.get("trace", []), step],
                               overrides=overrides)
        return {"result": result, "trace": [step]}

    def _finalize(self, state: PipelineState) -> tuple[str, ExecSummary | None, str]:
        """SQL 排版、执行，返回（排版后的 SQL，执行结果摘要，轨迹里的一句话）

        ok 时把校验器传给 format_sql，核对排版没有引入新错误；unvalidated 本来就有错误，不传。
        只有 ok 并且配置了执行层才执行。摘要里只写状态、行数这类元数据，不写数据。
        """
        status = state.get("status")
        sql = ""
        exec_result: ExecSummary | None = None
        if status in (STATUS_OK, STATUS_UNVALIDATED):
            sql = format_sql(state.get("sql", ""), self._deps.validator if status == STATUS_OK else None)
        if status == STATUS_OK and getattr(self._deps.executor, "name", "none") != "none":
            exec_result = self._execute(sql)
        if status == STATUS_OK:
            if exec_result is None:
                summary = "状态 ok；仅生成，未执行"
            elif exec_result.executed:
                summary = f"状态 ok；已执行，返回 {exec_result.row_count} 行" + ("（结果被截断）" if exec_result.truncated else "")
            else:
                summary = "状态 ok；执行失败"
        elif status == STATUS_UNVALIDATED:
            errors = len(state["validation"].errors) if "validation" in state else 0
            summary = f"状态 unvalidated；修正 {state.get('repair_rounds', 0)} 次后仍有 {errors} 条校验错误"
        elif status == STATUS_CLARIFY:
            summary = "状态 need_clarify；等待用户回答反问"
        else:
            summary = f"状态 failed；停在 {state.get('failed_step') or INTERNAL_STEP}"
        return sql, exec_result, summary

    def _execute(self, sql: str) -> ExecSummary:
        """交给执行层执行；执行层按约定不抛异常，真抛了也变成一条执行失败的结果"""
        try:
            return summarize_exec(self._deps.executor.execute(sql, max_rows=EXEC_MAX_ROWS))
        except Exception as error:  # noqa: BLE001 自定义的执行层不一定守约定
            logger.exception("执行层抛出了异常")
            return ExecSummary(executed=False, error=f"{type(error).__name__}: {error}")


def _build_result(state: PipelineState, *, sql: str, exec_result: ExecSummary | None, trace: list[TraceStep],
                  overrides: dict[str, Any] | None = None) -> QueryResult:
    """由流水线的最终状态组装 QueryResult

    Args:
        state: 走到 output 时的状态
        sql: 排版后的 SQL；只有 ok、unvalidated 时有
        exec_result: 执行结果摘要
        trace: 全部轨迹，含 output 这一步
        overrides: 覆盖状态里的 status、failed_step、message（output 自己出错时用）
    """
    values = {"status": state.get("status"), "failed_step": state.get("failed_step", ""),
              "message": state.get("message", ""), **(overrides or {})}
    if values["status"] is None:
        values.update(status=STATUS_FAILED, failed_step=INTERNAL_STEP, message="内部错误：流程结束时没有得出结论")
    request = state["request"]
    parsed = state.get("parsed")
    plan = state.get("plan")
    evidence = state.get("evidence")
    recall = state.get("recall")
    if evidence is not None:
        candidates = evidence.table_ids()
    else:                       # 证据还没组出来（召回之后、证据之前出错）时，退回召回的候选
        candidates = [table.table_id for table in recall.tables] if recall is not None else []
    started = state.get("started", time.perf_counter())
    clarify = None
    if values["status"] == STATUS_CLARIFY:                  # ④ 的反问；还没到 ④ 就是 ① 的反问
        clarify = plan.clarify if plan is not None else parsed.clarify if parsed is not None else None
    return QueryResult(
        status=values["status"], question=request.question,
        requirement=parsed.requirement.strip() if parsed is not None and parsed.is_query else "",
        parsed=parsed, sql=sql, plan=plan, clarify=clarify,
        message=values["message"] if values["status"] == STATUS_FAILED else "",
        failed_step=values["failed_step"] if values["status"] == STATUS_FAILED else "",
        assumptions=merge_assumptions(parsed.assumptions if parsed is not None else [],
                                      plan.assumptions if plan is not None else []),
        validation=state.get("validation"), first_pass_ok=state.get("first_pass_ok"),
        repair_rounds=state.get("repair_rounds", 0), scope_level=state.get("scope_level", 0),
        candidate_tables=candidates, recall_notes=list(recall.notes) if recall is not None else [],
        exec_result=exec_result, trace=trace, calls=list(state.get("calls", [])),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


# ── 图 ──────────────────────────────────────

def _go_unless_failed(next_step: str) -> Callable[[PipelineState], str]:
    """路由：状态是 failed（或者 ① 要反问，need_clarify）就去输出，否则去下一步"""
    target, output = _node_name(next_step), _node_name("output")

    def route(state: PipelineState) -> str:
        return output if state.get("status") in (STATUS_FAILED, STATUS_CLARIFY) else target

    return route


def _after_plan(state: PipelineState) -> str:
    """路由：failed、need_clarify 去输出；还需要上溯（状态没置位，方案的结论是 need_upstream）回到召回；否则去生成"""
    if state.get("status") in (STATUS_FAILED, STATUS_CLARIFY):
        return _node_name("output")
    if state["plan"].decision == "need_upstream":
        return _node_name("recall")
    return _node_name("generate")


def _after_validate(state: PipelineState) -> str:
    """路由：状态已经定了（ok、unvalidated、failed）去输出；没定说明校验有错误且还能修，回到生成"""
    return _node_name("output") if state.get("status") else _node_name("generate")


def build_workflow(deps: PipelineDeps) -> Any:
    """建 LangGraph 图并编译，不带检查点

    START → parse → recall → evidence → plan → generate → validate → output → END。
    每个节点出来后先看状态是不是 failed（parse 之后还有反问），是就直接去 output；
    plan 之后反问、做不到去 output，需要上溯回到 recall；validate 之后要修正回到 generate。

    Args:
        deps: 流水线的依赖

    Returns:
        编译好的图
    """
    steps = _Steps(deps)
    graph = StateGraph(PipelineState)
    runs = {"parse": steps.parse, "recall": steps.recall, "evidence": steps.evidence, "plan": steps.plan,
            "generate": steps.generate, "validate": steps.validate}
    for step, run in runs.items():
        graph.add_node(_node_name(step), _guarded(step, run))
    graph.add_node(_node_name("output"), steps.output)

    def destinations(*names: str) -> dict[str, str]:
        return {_node_name(name): _node_name(name) for name in names}

    graph.add_edge(START, _node_name("parse"))
    for here, there in (("parse", "recall"), ("recall", "evidence"), ("evidence", "plan"), ("generate", "validate")):
        graph.add_conditional_edges(_node_name(here), _go_unless_failed(there), destinations(there, "output"))
    graph.add_conditional_edges(_node_name("plan"), _after_plan, destinations("recall", "generate", "output"))
    graph.add_conditional_edges(_node_name("validate"), _after_validate, destinations("generate", "output"))
    graph.add_edge(_node_name("output"), END)
    return graph.compile()


# ── 对外的流水线 ──────────────────────────────────

def _failure(request: QueryRequest, error: BaseException | str, trace: list[TraceStep], calls: list[CallRecord],
             started: float) -> QueryResult:
    """图本身出了错（节点里的错误已经在节点里接住了）时给出的 failed 结果，带上已经走过的轨迹和调用记录"""
    message = _internal_message(error) if isinstance(error, BaseException) else f"内部错误：{error}"
    return QueryResult(status=STATUS_FAILED, question=request.question, message=message, failed_step=INTERNAL_STEP,
                       trace=trace, calls=calls, elapsed_ms=int((time.perf_counter() - started) * 1000))


class Pipeline:
    """查询流水线：界面、接口、评估共用。无状态，可以多线程同时用"""

    def __init__(self, deps: PipelineDeps) -> None:
        """
        Args:
            deps: 流水线的依赖
        """
        self._deps = deps
        self._app = build_workflow(deps)

    @property
    def deps(self) -> PipelineDeps:
        """流水线的依赖"""
        return self._deps

    def run(self, request: QueryRequest) -> QueryResult:
        """跑一轮查询，不抛异常：任何错误都包成 status=failed 的结果"""
        *_, result = self.stream(request)               # stream 保证最后一个是 QueryResult
        return result

    def stream(self, request: QueryRequest) -> Iterator[TraceStep | QueryResult]:
        """跑一轮查询，每走完一步给出一个 TraceStep，最后一个是 QueryResult；不抛异常

        Args:
            request: 本轮请求；追问、反问的上下文由调用方放在里面
        """
        started = time.perf_counter()
        state: PipelineState = {
            "request": request, "today": request.today or date.today(), "scope_level": 0, "base_tables": [],
            "extra_terms": [], "repair_rounds": 0, "trace": [], "calls": [], "started": started,
        }
        trace: list[TraceStep] = []
        calls: list[CallRecord] = []
        result: QueryResult | None = None
        failure: BaseException | str = "流程结束时没有产生结果"
        try:
            for chunk in self._app.stream(state, {"recursion_limit": RECURSION_LIMIT}, stream_mode="updates"):
                for update in chunk.values():
                    if not isinstance(update, dict):
                        continue
                    calls.extend(update.get("calls", ()))
                    for step in update.get("trace", ()):
                        trace.append(step)
                        yield step
                    if "result" in update:
                        result = update["result"]
        except Exception as error:  # noqa: BLE001 run、stream 不向外抛异常
            logger.exception("流水线运行出错")
            result, failure = None, error
        if result is None:
            result = _failure(request, failure, trace, calls, started)
        yield result
