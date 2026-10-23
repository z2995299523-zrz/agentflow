"""LangGraph 编排：七个节点、三条回路、失败的各条路径、对照实验的三组、逐步进度

用假模型（ScriptedLLM）和模拟专题库的元数据：回复的条数就是模型被调用的次数，调多了、调少了测试都会发现。
"""
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest

from lineage.graph import LineageGraph
from metadata.checks import ERROR, WARNING
from metadata.importer import load_mart
from metadata.store import MetadataStore, write_store
from nl2sql import workflow
from nl2sql.executor import DuckDBExecutor, ExecResult
from nl2sql.llm import ScriptedLLM
from nl2sql.retriever import Retriever
from nl2sql.schemas import (
    STEPS,
    Clarify,
    ClarifyOption,
    Metric,
    ParsedRequest,
    PlanField,
    PlanJoin,
    PlanTable,
    QueryPlan,
    QueryRequest,
    QueryResult,
    TimeSpec,
    TraceStep,
)
from nl2sql.workflow import Pipeline, PipelineDeps
from nl2sql.validator import SqlValidator, ValidationIssue, ValidationReport

LOAN = "ads.corp_cust_loan_bal_d"              # 对公客户贷款余额日汇总：B01 的标准用表
REQUIREMENT = "查询 2026-09-30 全行对公贷款余额"
GOOD_SQL = f"SELECT SUM(loan_bal_rmb) AS loan_bal_rmb FROM {LOAN} WHERE data_dt = DATE '2026-09-30'"
BAD_SQL = f"SELECT SUM(loan_bal_rmb) AS loan_bal_rmb FROM {LOAN}"        # 快照表没有数据日期条件：R5
QUESTION = "查 2026 年 9 月末全行对公贷款余额"
TODAY = date(2026, 10, 13)


def request(**changes) -> QueryRequest:
    """B01 的首轮请求；changes 覆盖其中的字段"""
    return QueryRequest(question=QUESTION, today=TODAY, **changes)


def parsed_json(**changes) -> str:
    """① 的合格回复（JSON 文本）"""
    parsed = ParsedRequest(is_query=True, requirement=REQUIREMENT, metrics=[Metric(name="对公贷款余额")],
                           time=TimeSpec(kind="point", dates=["2026-09-30"], note="9 月末 → 2026-09-30"))
    return parsed.model_copy(update=changes).model_dump_json()


def plan_json(**changes) -> str:
    """④ 的合格回复（JSON 文本）：用 B01 的标准用表"""
    plan = QueryPlan(
        decision="ok", tables=[PlanTable(table=LOAN, reason="时点余额已在客户汇总表里")],
        fields=[PlanField(table=LOAN, column="loan_bal_rmb", usage="metric", reason="贷款余额"),
                PlanField(table=LOAN, column="data_dt", usage="filter", reason="快照表的数据日期")],
        filters=[f"{LOAN}.data_dt = DATE '2026-09-30'"], caliber="loan_bal_rmb 是全部对公贷款的余额",
        table_basis="只到全行，不需要关联维表", assumptions=["余额按人民币折算"])
    return plan.model_copy(update=changes).model_dump_json()


def upstream_json(*missing: str) -> str:
    """④ 说缺东西、要上溯的回复"""
    return QueryPlan(decision="need_upstream", missing=list(missing), reason="候选表里没有这个字段").model_dump_json()


def clarify_json() -> str:
    """④ 反问的回复"""
    clarify = Clarify(question="贷款余额要不要含票据贴现？", options=[
        ClarifyOption(label="含票据贴现", meaning="用全口径余额"), ClarifyOption(label="不含票据贴现", meaning="用贷款余额")])
    return QueryPlan(decision="need_clarify", clarify=clarify).model_dump_json()


def sql_reply(sql: str) -> str:
    """⑤ 的回复：放在 sql 代码块里"""
    return f"```sql\n{sql}\n```"


class SpyRetriever:
    """包住真的召回，记下每次调用的参数和结果"""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[dict] = []

    def recall(self, query, scope_level=0, base_tables=(), *, all_layers=False, carry_tables=(), missing_terms=()):
        result = self._inner.recall(query, scope_level, base_tables, all_layers=all_layers, carry_tables=carry_tables,
                                    missing_terms=missing_terms)
        self.calls.append({"query": query, "scope_level": scope_level, "base_tables": list(base_tables),
                           "all_layers": all_layers, "carry_tables": list(carry_tables),
                           "missing_terms": list(missing_terms), "result": result})
        return result


@pytest.fixture
def make(mock_store, mock_graph, mock_retriever, mock_validator):
    """造流水线：make(replies, executor=None) → (流水线, 假模型, 召回的记录)"""

    def build(replies, executor=None):
        llm = ScriptedLLM(replies)
        spy = SpyRetriever(mock_retriever)
        deps = PipelineDeps(store=mock_store, graph=mock_graph, retriever=spy, validator=mock_validator, llm=llm)
        if executor is not None:
            deps.executor = executor
        return Pipeline(deps), llm, spy

    return build


@pytest.fixture
def evidence_spy(monkeypatch):
    """包住 build_evidence，记下每次的参数和证据"""
    seen: list[dict] = []
    original = workflow.build_evidence

    def spy(*args, **kwargs):
        evidence = original(*args, **kwargs)
        seen.append({"kwargs": kwargs, "evidence": evidence})
        return evidence

    monkeypatch.setattr(workflow, "build_evidence", spy)
    return seen


def labels(result: QueryResult) -> list[str]:
    return [step.label for step in result.trace]


# ── 正常路径 ──────────────────────────────────────

def test_normal_path_runs_the_seven_steps_in_order(make):
    pipeline, llm, _ = make([parsed_json(), plan_json(), sql_reply(GOOD_SQL)])

    result = pipeline.run(request())

    assert result.status == "ok" and result.failed_step == "" and result.message == ""
    assert [step.step for step in result.trace] == list(STEPS)
    assert all(step.summary for step in result.trace)
    assert result.llm_calls == 3 and llm.remaining == 0
    assert [call["json_mode"] for call in llm.calls] == [True, True, False]       # ① ④ 用 JSON 模式，⑤ 不用
    assert result.requirement == REQUIREMENT
    assert LOAN in result.candidate_tables                                         # 标准用表进了候选
    assert result.validation.ok and result.first_pass_ok is True and result.repair_rounds == 0
    assert result.sql.startswith("SELECT") and "\n" in result.sql                   # 排过版
    assert result.assumptions == ["余额按人民币折算"]
    assert result.exec_result is None                                               # 执行层是 none，不执行
    assert result.metadata_version is None and result.public()["metadata_version"] is None
    assert "messages" not in result.public()["calls"][0]                           # 给界面和接口的版本没有提示词


def test_result_records_the_metadata_version_loaded_from_the_store(tmp_path: Path) -> None:
    """写库后读出的元数据版本进入结果和对外字典。"""
    mart, _ = load_mart(Path(__file__).resolve().parents[1] / "data" / "mock_mart")
    db_path = tmp_path / "metadata.db"
    version = "20261023-followup-test"
    write_store(db_path, mart, info={"version_id": version})
    store = MetadataStore(db_path)
    graph = LineageGraph(store)
    llm = ScriptedLLM([parsed_json(), plan_json(), sql_reply(GOOD_SQL)])
    pipeline = Pipeline(PipelineDeps(store=store, graph=graph, retriever=Retriever(store, graph, None),
                                     validator=SqlValidator(store), llm=llm))

    result = pipeline.run(request())

    assert result.status == "ok" and result.llm_calls == 3
    assert result.metadata_version == version and result.public()["metadata_version"] == version


def test_duckdb_executor_runs_the_sql_and_the_summary_has_no_data(make, mock_db):
    pipeline, _, _ = make([parsed_json(), plan_json(), sql_reply(GOOD_SQL)], executor=DuckDBExecutor(mock_db))

    result = pipeline.run(request())

    assert result.status == "ok" and result.exec_result.executed and result.exec_result.row_count > 0
    summary = result.trace[-1].summary
    assert f"已执行，返回 {result.exec_result.row_count} 行" in summary
    assert str(result.exec_result.rows[0][0]) not in summary                        # 轨迹里只有元数据，没有数据


# ── 反问、上溯、做不到 ─────────────────────────────

def test_clarify_stops_before_generating_sql(make):
    pipeline, llm, _ = make([parsed_json(), clarify_json()])

    result = pipeline.run(request())

    assert result.status == "need_clarify" and result.sql == ""
    assert [option.label for option in result.clarify.options] == ["含票据贴现", "不含票据贴现"]
    assert result.requirement == REQUIREMENT                    # 调用方把它作为下一轮的 prev_requirement
    assert result.llm_calls == 2 and llm.remaining == 0         # 没有调用 ⑤
    assert [step.step for step in result.trace] == ["parse", "recall", "evidence", "plan", "output"]


def test_upstream_one_level_then_ok(make):
    pipeline, llm, spy = make([parsed_json(), upstream_json("还款方式"), plan_json(), sql_reply(GOOD_SQL)])

    result = pipeline.run(request())

    assert result.status == "ok" and result.scope_level == 1 and result.llm_calls == 4
    assert [call["scope_level"] for call in spy.calls] == [0, 1]
    first, second = spy.calls
    assert "还款方式" in second["query"].metrics and "还款方式" not in first["query"].metrics
    assert first["missing_terms"] == [] and second["missing_terms"] == ["还款方式"]     # 缺的东西也单独补召回
    assert second["base_tables"] == [table.table_id for table in first["result"].tables]    # 以第一次的候选为起点
    assert "② 候选召回（上溯第 1 层）" in labels(result) and "④ 选表定口径（上溯第 1 层）" in labels(result)
    assert labels(result)[:4] == ["① 需求解析", "② 候选召回", "③ 血缘展开", "④ 选表定口径"]    # 第 0 层的标签不带后缀


def test_each_lift_recalls_only_what_the_planner_says_is_missing_this_time(make):
    pipeline, _, spy = make([parsed_json(), upstream_json("还款方式"), upstream_json("担保方式"), plan_json(),
                             sql_reply(GOOD_SQL)])

    result = pipeline.run(request())

    assert result.status == "ok" and result.scope_level == 2
    third = spy.calls[2]
    assert third["missing_terms"] == ["担保方式"]                                  # 单独补召回只用最近一次说缺的
    assert {"还款方式", "担保方式"} <= set(third["query"].metrics)                   # 整体排名用累积的


def test_upstream_to_the_top_still_missing_fails(make):
    pipeline, llm, spy = make([parsed_json(), upstream_json("还款方式"), upstream_json("还款方式"),
                               upstream_json("还款方式")])

    result = pipeline.run(request())

    assert result.status == "failed" and result.failed_step == "plan"
    assert "上溯 2 层仍找不到" in result.message and "还款方式" in result.message
    assert [call["scope_level"] for call in spy.calls] == [0, 1, 2]
    assert result.scope_level == 2 and result.llm_calls == 4 and result.sql == ""


def test_cannot_answer_fails_with_the_reason(make):
    reason = "元数据里没有还款计划，做不到"
    pipeline, llm, _ = make([parsed_json(), QueryPlan(decision="cannot_answer", reason=reason).model_dump_json()])

    result = pipeline.run(request())

    assert result.status == "failed" and result.failed_step == "plan" and result.message == reason
    assert result.llm_calls == 2 and llm.remaining == 0


def test_no_candidate_table_fails_at_recall(make):
    parsed = parsed_json(requirement="qwertyuiop", metrics=[Metric(name="qwertyuiop")])
    pipeline, llm, _ = make([parsed])

    result = pipeline.run(request())

    assert result.status == "failed" and result.failed_step == "recall"
    assert "元数据里没有找到" in result.message and "qwertyuiop" in result.message
    assert result.llm_calls == 1 and result.candidate_tables == []
    assert [step.step for step in result.trace] == ["parse", "recall", "output"]


# ── 修正回路 ──────────────────────────────────────

def test_repair_twice_and_still_failing_is_unvalidated(make):
    pipeline, llm, _ = make([parsed_json(), plan_json(), sql_reply(BAD_SQL), sql_reply(BAD_SQL), sql_reply(BAD_SQL)])

    result = pipeline.run(request())

    assert result.status == "unvalidated" and result.repair_rounds == 2 and result.first_pass_ok is False
    assert result.llm_calls == 5 and llm.remaining == 0
    assert not result.validation.ok and result.validation.errors[0].startswith("R5：")
    assert result.sql                                            # 仍然给出 SQL，由人判断
    assert "⑤ 生成 SQL（第 2 次）" in labels(result) and "⑥ 静态校验（第 3 次）" in labels(result)
    second = llm.calls[3]["messages"]                            # 修正那次发给模型的消息：上一次的 SQL 和错误
    assert BAD_SQL in second[-1]["content"] and "R5" in second[-1]["content"]
    assert "第 2 次：修正 R5" in [step.summary for step in result.trace if step.step == "generate"][1]


def test_one_repair_is_enough(make):
    pipeline, _, _ = make([parsed_json(), plan_json(), sql_reply(BAD_SQL), sql_reply(GOOD_SQL)])

    result = pipeline.run(request())

    assert result.status == "ok" and result.repair_rounds == 1 and result.first_pass_ok is False
    assert result.validation.ok and result.llm_calls == 4


def test_validation_compares_the_sql_with_the_plans_tables_and_metric_columns(make):
    fields = [PlanField(table=LOAN, column="npl_bal_rmb", usage="metric"),         # SQL 没用到：该警告
              PlanField(table=LOAN, column="cust_no", usage="filter"),             # SQL 没用到，但过滤字段不在期望里
              PlanField(table=LOAN, column="data_dt", usage="filter")]
    pipeline, _, _ = make([parsed_json(), plan_json(fields=fields), sql_reply(GOOD_SQL)])

    result = pipeline.run(request())

    assert result.status == "ok"                                 # R8 只是警告，不触发修正
    assert len(result.validation.warnings) == 1 and result.validation.warnings[0].startswith("R8：")
    assert "npl_bal_rmb" in result.validation.warnings[0] and "cust_no" not in result.validation.warnings[0]
    assert [step.summary for step in result.trace if step.step == "validate"] == ["通过，警告 1 条"]


def test_a_dimension_joined_only_by_its_key_is_dropped_before_generating(make):
    cust = "dim.corp_cust"
    plan = plan_json(
        tables=[PlanTable(table=LOAN), PlanTable(table=cust)],
        fields=[PlanField(table=LOAN, column="loan_bal_rmb", usage="metric"),
                PlanField(table=LOAN, column="data_dt", usage="filter"),
                PlanField(table=LOAN, column="cust_no", usage="join"), PlanField(table=cust, column="cust_no", usage="join")],
        joins=[PlanJoin(left_table=LOAN, left_columns=["cust_no"], right_table=cust, right_columns=["cust_no"])])
    pipeline, llm, _ = make([parsed_json(), plan, sql_reply(GOOD_SQL)])

    result = pipeline.run(request())

    assert result.status == "ok" and result.validation.warnings == []           # 精简后的方案和 SQL 一致，没有 R8
    assert [item.table for item in result.plan.tables] == [LOAN] and result.plan.joins == []
    assert [step.summary for step in result.trace if step.step == "plan"] == \
        [f"结论 ok：用表 {LOAN}；去掉用不到的表 {cust}"]
    generate = llm.calls[2]["messages"][1]["content"]
    structure = generate.split("## 表结构", 1)[1].split("\n## ", 1)[0]
    assert f"### {cust} " not in structure and cust not in structure          # ⑤ 的表结构里没有这张维表，也不指向它


def test_sql_that_joins_a_dropped_table_again_is_sent_back_for_repair(make):
    cust = "dim.corp_cust"
    plan = plan_json(
        tables=[PlanTable(table=LOAN), PlanTable(table=cust)],
        fields=[PlanField(table=LOAN, column="loan_bal_rmb", usage="metric"),
                PlanField(table=LOAN, column="data_dt", usage="filter"), PlanField(table=cust, column="cust_no", usage="join")],
        joins=[PlanJoin(left_table=LOAN, left_columns=["cust_no"], right_table=cust, right_columns=["cust_no"])])
    rejoined = (f"SELECT SUM(l.loan_bal_rmb) AS loan_bal_rmb FROM {LOAN} l JOIN {cust} c ON l.cust_no = c.cust_no "
                f"WHERE l.data_dt = DATE '2026-09-30'")
    pipeline, llm, _ = make([parsed_json(), plan, sql_reply(rejoined), sql_reply(GOOD_SQL)])

    result = pipeline.run(request())

    assert result.status == "ok" and result.repair_rounds == 1 and result.first_pass_ok is False
    assert result.plan.dropped_tables == [cust]
    assert "不要用的表：dim.corp_cust" in llm.calls[2]["messages"][1]["content"]
    assert "已经去掉了 dim.corp_cust" in llm.calls[3]["messages"][1]["content"]       # 修正轮带着 R8 错误


AVG_REQUIREMENT = "查询 2026-09-30 全行不含保证金的对公存款日均余额"
ORG_DEP = "ads.org_corp_dep_bal_d"


def avg_replies(answers_clarify: bool = False) -> list[str]:
    """① 解析出没说统计区间的日均，④ 挑了机构汇总表的年日均；answers_clarify 是 ① 判断的本轮是不是在回答反问"""
    parsed = parsed_json(requirement=AVG_REQUIREMENT, metrics=[Metric(name="对公存款日均余额")],
                         caliber_hints=["不含保证金"], answers_clarify=answers_clarify)
    plan = plan_json(tables=[PlanTable(table=ORG_DEP)],
                     fields=[PlanField(table=ORG_DEP, column="ytd_avg_bal_rmb", usage="metric"),
                             PlanField(table=ORG_DEP, column="data_dt", usage="filter")],
                     filters=[f"{ORG_DEP}.data_dt = DATE '2026-09-30'"])
    return [parsed, plan]


def test_an_unqualified_daily_average_is_asked_back_instead_of_guessed(make):
    pipeline, llm, _ = make(avg_replies())

    result = pipeline.run(QueryRequest(question="查 2026 年 9 月末全行不含保证金的对公存款日均余额", today=TODAY))

    assert result.status == "need_clarify" and result.llm_calls == 2 and result.sql == ""
    assert [option.label for option in result.clarify.options] == ["年日均", "月日均"]
    assert [step.summary for step in result.trace if step.step == "plan"][0].startswith("需要反问：需求里的「日均」")


def test_when_answering_a_clarify_the_daily_average_guard_stays_quiet(make):
    prev = Clarify(question="要不要含保证金？", options=[ClarifyOption(label="含"), ClarifyOption(label="不含")])
    pipeline, _, _ = make([*avg_replies(answers_clarify=True),
                           sql_reply(f"SELECT SUM(ytd_avg_bal_rmb) AS ytd_avg_bal_rmb FROM {ORG_DEP} "
                                     "WHERE data_dt = DATE '2026-09-30'")])

    result = pipeline.run(QueryRequest(question="不含", prev_clarify=prev, today=TODAY))

    assert result.status == "ok"


def test_a_new_question_after_an_unanswered_clarify_still_gets_the_daily_average_question(make):
    prev = Clarify(question="要不要含保证金？", options=[ClarifyOption(label="含"), ClarifyOption(label="不含")])
    pipeline, _, _ = make(avg_replies(answers_clarify=False))

    result = pipeline.run(QueryRequest(question="查 2026 年 9 月末全行不含保证金的对公存款日均余额",
                                       prev_clarify=prev, today=TODAY))

    assert result.status == "need_clarify" and result.llm_calls == 2
    assert [option.label for option in result.clarify.options] == ["年日均", "月日均"]


# ── 失败 ──────────────────────────────────────────

def test_requirement_unparseable_twice_fails_at_parse(make):
    pipeline, llm, _ = make(["这不是 JSON", "还是不是 JSON"])

    result = pipeline.run(request())

    assert result.status == "failed" and result.failed_step == "parse" and "输出两次都不合格" in result.message
    assert result.llm_calls == 2 and result.requirement == ""
    assert [step.step for step in result.trace] == ["parse", "output"]


def test_model_error_names_the_step_that_failed(make):
    pipeline, _, _ = make([parsed_json(), RuntimeError("连接中断")])

    result = pipeline.run(request())

    assert result.status == "failed" and result.failed_step == "plan"
    assert "模型调用失败" in result.message and "连接中断" in result.message
    assert result.calls[-1].ok is False and result.requirement == REQUIREMENT      # 失败前的结果还在


def test_not_a_query_fails_with_the_models_reply(make):
    reply = "我是专题库查数工具，请告诉我要查什么指标"
    pipeline, llm, _ = make([ParsedRequest(is_query=False, reply=reply).model_dump_json()])

    result = pipeline.run(request())

    assert result.status == "failed" and result.failed_step == "parse" and result.message == reply
    assert result.llm_calls == 1 and result.requirement == ""        # 不是需求，调用方不该把它当作上一轮需求


def test_the_parser_asks_back_before_recall_when_the_object_is_missing(make):
    clarify = Clarify(question="要看哪个客户的贷款情况？", options=[
        ClarifyOption(label="列出全部客户"), ClarifyOption(label="指定一个客户", meaning="请回复客户名称或客户号")])
    asking = ParsedRequest(is_query=True, requirement="查询某个对公客户的贷款情况", clarify=clarify)
    pipeline, llm, spy = make([asking.model_dump_json()])

    result = pipeline.run(QueryRequest(question="看一下某个客户的贷款情况", today=TODAY))

    assert result.status == "need_clarify" and result.clarify == clarify and result.plan is None
    assert result.requirement == "查询某个对公客户的贷款情况"          # 调用方拿它和 clarify 作为下一轮的上下文
    assert [step.step for step in result.trace] == ["parse", "output"] and spy.calls == [] and result.llm_calls == 1
    assert result.trace[0].summary == "需要反问：要看哪个客户的贷款情况？"


# ── 追问与反问的回答 ──────────────────────────────

def test_follow_up_sends_the_previous_requirement_to_the_parser(make):
    pipeline, llm, _ = make([parsed_json(requirement=REQUIREMENT + "，按机构分组"), plan_json(), sql_reply(GOOD_SQL)])

    result = pipeline.run(request(prev_requirement=REQUIREMENT))

    assert REQUIREMENT in llm.calls[0]["messages"][-1]["content"]
    assert result.status == "ok" and result.requirement == REQUIREMENT + "，按机构分组"


PREV_SQL = f"SELECT SUM(loan_bal_rmb) AS loan_bal_rmb FROM {LOAN} WHERE data_dt = DATE '2026-08-31'"
DUEBILL = "dwd.corp_loan_duebill_d"            # 整合层的表：第 0 层召回不到，只能靠上一轮带进来


def test_a_follow_up_builds_on_the_sql_and_tables_of_the_previous_round(make):
    pipeline, llm, spy = make([parsed_json(follows_up=True), plan_json(), sql_reply(GOOD_SQL)])

    result = pipeline.run(request(prev_requirement=REQUIREMENT, prev_sql=PREV_SQL, prev_tables=[DUEBILL]))

    assert result.status == "ok" and result.llm_calls == 3                          # 不多调模型
    assert spy.calls[0]["carry_tables"] == [DUEBILL]
    assert [table.reason for table in spy.calls[0]["result"].tables if table.table_id == DUEBILL] == ["previous"]
    planner, generator = llm.calls[1]["messages"][-1]["content"], llm.calls[2]["messages"][-1]["content"]
    assert "# 上一轮的 SQL" in planner and PREV_SQL in planner
    assert "## 上一轮的 SQL" in generator and PREV_SQL in generator
    assert "入选原因：上一轮用过" in planner
    assert result.trace[0].summary.startswith("在上一轮的基础上追问，以上一轮的 SQL 为底；完整需求：")
    assert all("元数据已更新" not in item for item in result.assumptions)


@pytest.mark.parametrize("prev_sql, rule", [
    ("SELECT missing_bal FROM ads.removed_loan_bal_d", "R3"),
    (f"SELECT removed_bal FROM {LOAN} WHERE data_dt = DATE '2026-08-31'", "R4"),
])
def test_a_follow_up_rejects_sql_that_no_longer_matches_metadata(make, prev_sql: str, rule: str) -> None:
    """旧表或旧字段失效时，丢弃底稿和携带表，按完整需求重新生成。"""
    requirement = REQUIREMENT + "，仅统计制造业"
    pipeline, llm, spy = make([parsed_json(follows_up=True, requirement=requirement), plan_json(), sql_reply(GOOD_SQL)])
    errors = pipeline.deps.validator.validate(prev_sql).errors
    reasons = [f"{error.rule} {error.message}" for error in errors[:2]]
    shown_reasons = "；".join(reason if len(reason) <= 80 else reason[:79] + "…" for reason in reasons)

    result = pipeline.run(request(prev_requirement=REQUIREMENT, prev_sql=prev_sql, prev_tables=[DUEBILL],
                                  use_examples=False))

    assert result.status == "ok" and result.llm_calls == 3 and llm.remaining == 0
    assert result.requirement == requirement
    assert spy.calls[0]["carry_tables"] == []
    assert DUEBILL not in result.candidate_tables
    assert all(table.reason != "previous" for table in spy.calls[0]["result"].tables)
    assert all("上一轮的 SQL" not in call["messages"][-1]["content"] for call in llm.calls[1:])
    assert all(prev_sql not in call["messages"][-1]["content"] for call in llm.calls[1:])
    assert requirement in llm.calls[1]["messages"][-1]["content"]
    assert requirement in llm.calls[2]["messages"][-1]["content"]
    assert result.trace[0].summary.startswith("在上一轮的基础上追问，但上一轮的 SQL 在当前元数据上校验不过")
    assert rule in result.trace[0].summary
    assert result.assumptions[-1] == ("元数据已更新，上一轮的 SQL 在当前版本上校验不过"
                                       f"（{shown_reasons}），这一轮没有以它为底，按完整需求重新生成")


@pytest.mark.parametrize("length", [79, 80, 81, 160])
def test_rejected_base_sql_keeps_only_two_errors_with_at_most_80_characters(make, monkeypatch, length: int) -> None:
    """拒绝原因保留错误顺序、规则编号，最多两条，每条含省略号最多 80 字。"""
    pipeline, _, _ = make([parsed_json(follows_up=True)])
    reason = "R3 " + "旧" * (length - 3)
    report = ValidationReport(issues=[ValidationIssue("R6", WARNING, "警告不阻止底稿复用"),
                                      ValidationIssue("R3", ERROR, reason[3:]),
                                      ValidationIssue("R4", ERROR, "字段已删除"),
                                      ValidationIssue("R5", ERROR, "第三条错误不保留")])
    validate = Mock(return_value=report)
    monkeypatch.setattr(pipeline.deps.validator, "validate", validate)
    state = workflow.PipelineState(request=request(prev_requirement=REQUIREMENT, prev_sql=PREV_SQL), today=TODAY)

    updates, summary = workflow._Steps(pipeline.deps).parse(state)

    expected = reason if length <= 80 else reason[:79] + "…"
    assert updates["base_sql_rejected"] == [expected, "R4 字段已删除"]
    assert expected in summary and "第三条错误" not in summary and "警告不阻止" not in summary
    assert workflow._base_sql({**state, **updates}) == ""
    validate.assert_called_once_with(PREV_SQL)


@pytest.mark.parametrize("follows_up, prev_sql", [(False, PREV_SQL), (False, "SELECT * FROM ads.removed"),
                                                  (True, ""), (True, "   ")])
def test_a_new_question_or_a_round_without_sql_carries_nothing(make, monkeypatch, follows_up, prev_sql):
    pipeline, llm, spy = make([parsed_json(follows_up=follows_up), plan_json(), sql_reply(GOOD_SQL)])
    validate = Mock(wraps=pipeline.deps.validator.validate)
    monkeypatch.setattr(pipeline.deps.validator, "validate", validate)

    result = pipeline.run(request(prev_requirement=REQUIREMENT, prev_sql=prev_sql, prev_tables=[DUEBILL]))

    assert result.status == "ok" and spy.calls[0]["carry_tables"] == []
    assert all("上一轮的 SQL" not in call["messages"][-1]["content"] for call in llm.calls[1:])
    assert not result.trace[0].summary.startswith("在上一轮的基础上追问")
    assert all("元数据已更新" not in item for item in result.assumptions)
    assert all(call.args[0] != prev_sql for call in validate.call_args_list)    # 只校验本轮 SQL，不核对底稿


def test_answering_a_clarify_cannot_trigger_another_one(make):
    prev = Clarify(question="贷款余额要不要含票据贴现？", options=[ClarifyOption(label="含票据贴现"),
                                                             ClarifyOption(label="不含票据贴现")])
    pipeline, llm, _ = make([parsed_json(answers_clarify=True), clarify_json(), clarify_json()])

    result = pipeline.run(request(prev_clarify=prev))

    assert "贷款余额要不要含票据贴现" in llm.calls[0]["messages"][-1]["content"]      # 解析时知道上一轮问了什么
    assert "能不能反问：不能" in llm.calls[1]["messages"][-1]["content"]
    assert result.status == "failed" and result.failed_step == "plan" and "输出两次都不合格" in result.message
    assert "不能再反问" in llm.calls[2]["messages"][-1]["content"]                  # 重试时告诉模型为什么不行
    assert result.llm_calls == 3 and result.clarify is None
    assert result.trace[0].summary.startswith("回答上一轮的反问；完整需求：")


SAVINGS_CLARIFY = Clarify(question="存款余额要不要包含保证金？", options=[ClarifyOption(label="不含保证金"),
                                                                    ClarifyOption(label="含保证金")])


def test_a_new_question_after_an_unanswered_clarify_can_be_asked_back_by_the_planner(make):
    pipeline, llm, _ = make([parsed_json(answers_clarify=False), clarify_json()])

    result = pipeline.run(request(prev_requirement="查询 2026-09-30 各开户机构的对公存款余额", prev_clarify=SAVINGS_CLARIFY))

    assert "能不能反问：能" in llm.calls[1]["messages"][-1]["content"]
    assert result.status == "need_clarify" and result.llm_calls == 2
    assert result.clarify.question == "贷款余额要不要含票据贴现？"                  # 新问题自己的反问，下一轮回答它
    assert result.requirement == REQUIREMENT
    assert result.trace[0].summary.startswith("上一轮的反问没有回答，按新问题处理；完整需求：")


def test_the_parser_can_ask_back_on_a_new_question_while_a_clarify_is_pending(make):
    clarify = Clarify(question="要看哪个客户的贷款情况？", options=[
        ClarifyOption(label="列出全部客户"), ClarifyOption(label="指定一个客户", meaning="请回复客户名称或客户号")])
    asking = ParsedRequest(is_query=True, answers_clarify=False, requirement="查询某个对公客户的贷款情况", clarify=clarify)
    pipeline, _, spy = make([asking.model_dump_json()])

    result = pipeline.run(QueryRequest(question="看一下某个客户的贷款情况", today=TODAY,
                                       prev_requirement="查询 2026-09-30 各开户机构的对公存款余额",
                                       prev_clarify=SAVINGS_CLARIFY))

    assert result.status == "need_clarify" and result.clarify == clarify and result.plan is None
    assert spy.calls == [] and result.requirement == "查询某个对公客户的贷款情况"
    assert result.trace[0].summary == "上一轮的反问没有回答，按新问题处理；需要反问：要看哪个客户的贷款情况？"


# ── 对照实验的三组 ─────────────────────────────────

def test_group_a_ranks_all_layers_without_lineage_or_upstream(make, evidence_spy, mock_graph):
    replies = [parsed_json(), upstream_json("还款方式"), plan_json(), sql_reply(GOOD_SQL)]
    pipeline, llm, spy = make(replies + replies)               # 跑两遍：结构图只建一次
    group_a = request(use_lineage=False, use_examples=False)

    result = pipeline.run(group_a)

    assert result.status == "ok" and result.scope_level == 0
    assert result.llm_calls == 4                                # ④ 想上溯被驳回，重试后给出方案
    assert "不能上溯" in llm.calls[2]["messages"][-1]["content"]
    assert len(spy.calls) == 1 and spy.calls[0]["all_layers"] is True and spy.calls[0]["scope_level"] == 0
    assert spy.calls[0]["result"].examples                      # 召回本来有样例，不用样例时被去掉了
    assert any(table.reason == "example" for table in spy.calls[0]["result"].tables)
    evidence = evidence_spy[0]["evidence"]
    assert all(table.reason != "example" for table in evidence.tables)              # 样例带进来的表也去掉了
    assert (evidence.use_lineage, evidence.all_layers) == (False, True)
    assert evidence.lineage == [] and evidence.comparisons == [] and evidence.upstream_options == []
    assert evidence.examples == []
    structure = pipeline.deps.structure_graph                   # 关联路径来自只按表结构推导的图
    assert structure is not None and evidence_spy[0]["kwargs"]["join_graph"] is structure
    assert structure.relations_between("ods.org_info", "ods.org_info") == []
    assert mock_graph.relations_between("ods.org_info", "ods.org_info")        # 完整的图里有这条脚本推出的自关联

    assert pipeline.run(group_a).status == "ok"
    assert pipeline.deps.structure_graph is structure           # 建好留着，没有重建


@pytest.mark.parametrize("use_examples", [False, True])
def test_groups_b_and_c_both_use_lineage_but_only_c_uses_examples(make, evidence_spy, use_examples):
    pipeline, _, spy = make([parsed_json(), plan_json(), sql_reply(GOOD_SQL)])

    result = pipeline.run(request(use_lineage=True, use_examples=use_examples))

    assert result.status == "ok"
    assert spy.calls[0]["all_layers"] is False and spy.calls[0]["result"].examples
    assert any(table.reason == "example" for table in spy.calls[0]["result"].tables)
    evidence = evidence_spy[0]["evidence"]
    assert any(table.reason == "example" for table in evidence.tables) is use_examples     # 样例带进来的表跟着样例走
    assert evidence.use_lineage and evidence.lineage and evidence.upstream_options
    assert bool(evidence.examples) is use_examples
    assert pipeline.deps.structure_graph is None                # B、C 组用不到结构图，不建


# ── 没料到的错误、逐步进度 ─────────────────────────

def test_unexpected_errors_become_a_failed_result_instead_of_being_raised(make, monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("证据组装炸了")

    monkeypatch.setattr(workflow, "build_evidence", boom)
    pipeline, _, _ = make([parsed_json()])

    result = pipeline.run(request())

    assert result.status == "failed" and result.failed_step == "internal"
    assert result.message == "内部错误：ValueError: 证据组装炸了"
    assert result.llm_calls == 1 and result.requirement == REQUIREMENT      # 已经发生的调用和结果都保留
    assert [step.step for step in result.trace] == ["parse", "recall", "evidence", "output"]
    assert result.trace[2].summary.startswith("失败：内部错误")


def test_output_step_failure_still_gives_a_result(make, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("排版炸了")

    monkeypatch.setattr(workflow, "format_sql", boom)
    pipeline, _, _ = make([parsed_json(), plan_json(), sql_reply(GOOD_SQL)])

    result = pipeline.run(request())

    assert result.status == "failed" and result.failed_step == "internal" and "排版炸了" in result.message
    assert result.sql == "" and result.trace[-1].step == "output"


def test_executor_that_raises_becomes_an_execution_error(make):
    class Broken:
        name = "broken"

        def execute(self, sql, max_rows=200, timeout_s=30):
            raise OSError("连不上库")

    pipeline, _, _ = make([parsed_json(), plan_json(), sql_reply(GOOD_SQL)], executor=Broken())

    result = pipeline.run(request())

    assert result.status == "ok" and result.exec_result.executed is False
    assert "连不上库" in result.exec_result.error and result.trace[-1].summary == "状态 ok；执行失败"


def test_executor_gets_the_row_limit(make):
    seen = {}

    class Recording:
        name = "recording"

        def execute(self, sql, max_rows=None, timeout_s=None):
            seen.update(sql=sql, max_rows=max_rows)
            return ExecResult(executed=True, columns=["a"], rows=[(1,)], row_count=1, truncated=True)

    pipeline, _, _ = make([parsed_json(), plan_json(), sql_reply(GOOD_SQL)], executor=Recording())

    result = pipeline.run(request())

    assert seen["max_rows"] == 200 and seen["sql"] == result.sql          # 执行的就是展示给用户的 SQL
    assert result.exec_result.truncated and result.trace[-1].summary == "状态 ok；已执行，返回 1 行（结果被截断）"


def test_stream_gives_each_step_as_it_finishes_then_the_result(make):
    pipeline, llm, _ = make([parsed_json(), plan_json(), sql_reply(GOOD_SQL)])

    stream = pipeline.stream(request())
    first = next(stream)
    assert isinstance(first, TraceStep) and first.step == "parse"
    assert len(llm.calls) == 1                                   # 第一步走完就给出，后面的步骤还没开始
    rest = list(stream)

    assert all(isinstance(item, TraceStep) for item in rest[:-1])
    assert [first.step, *[item.step for item in rest[:-1]]] == list(STEPS)
    result = rest[-1]
    assert isinstance(result, QueryResult) and result.status == "ok"
    assert result.trace == [first, *rest[:-1]]
