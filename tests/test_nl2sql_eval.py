"""评估程序的测试：假流水线返回预制的 QueryResult，不调模型"""
import io
import json
import sys
import threading
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from eval import nl2sql_eval
from eval.nl2sql_eval import (
    CONFIG_NAMES,
    CONFIGS,
    EVAL_MAX_ROWS,
    EVAL_TIMEOUT_S,
    QuestionOutcome,
    StaticQuestion,
    answer_groups,
    best_group_rank,
    compute_metrics,
    evaluate_question,
    load_outcomes,
    load_static_questions,
    main,
    render_report,
    run_eval,
    run_static_eval,
)
from eval.questions import Alternative, EvalQuestion, load_questions
from nl2sql.executor import DuckDBExecutor, ExecResult
from nl2sql.schemas import (
    CallRecord,
    Clarify,
    ClarifyOption,
    ParsedRequest,
    QueryPlan,
    QueryRequest,
    QueryResult,
    ValidationSummary,
)

TODAY = date(2026, 10, 13)
LOAN = "ads.corp_cust_loan_bal_d"
CUST_DEP = "ads.corp_cust_dep_bal_d"
ORG_DEP = "ads.org_corp_dep_bal_d"
GOLD_SQL = "SELECT SUM(loan_bal_rmb) FROM ads.corp_cust_loan_bal_d WHERE data_dt = DATE '2026-09-30'"
OTHER_SQL = "SELECT SUM(npl_bal_rmb) FROM ads.corp_cust_loan_bal_d WHERE data_dt = DATE '2026-09-30'"
ALT_SQL = ("SELECT SUM(margin_bal_rmb) AS margin_bal_rmb FROM ads.corp_cust_dep_bal_d "
           "WHERE data_dt = DATE '2026-09-30'")
GOLD_ROWS = [(Decimal("100.00"),)]
OTHER_ROWS = [(Decimal("7.00"),)]


# ── 假的流水线、假的执行层、造数据的小工具 ──────────────────

class FakePipeline:
    """按请求返回预制的结果，并记下收到的每个请求"""

    def __init__(self, respond, name: str = "fake-model") -> None:
        self.respond = respond
        self.requests: list[QueryRequest] = []
        self._lock = threading.Lock()
        self.deps = SimpleNamespace(store=None, llm=SimpleNamespace(name=name))

    def run(self, request: QueryRequest) -> QueryResult:
        with self._lock:
            self.requests.append(request)
        result = self.respond(request)
        if isinstance(result, BaseException):
            raise result
        return result


def in_order(*results):
    """每次调用依次给出下一个结果"""
    iterator = iter(results)
    lock = threading.Lock()

    def respond(request):
        with lock:
            return next(iterator)
    return respond


def by_text(mapping):
    """按本轮输入的文字找结果；值可以是函数，用请求算出结果"""
    def respond(request):
        entry = mapping[request.question]
        return entry(request) if callable(entry) else entry
    return respond


class FakeExecutor:
    """SQL → 预制行；没有预制的 SQL 报错。记下每次调用的参数"""

    name = "fake"

    def __init__(self, results=None) -> None:
        self.results = {GOLD_SQL: GOLD_ROWS, OTHER_SQL: OTHER_ROWS, ALT_SQL: GOLD_ROWS}
        self.results.update(results or {})
        self.calls: list[tuple] = []
        self._lock = threading.Lock()

    def execute(self, sql, max_rows=200, timeout_s=30):
        with self._lock:
            self.calls.append((sql, max_rows, timeout_s))
        outcome = self.results.get(sql)
        if outcome is None:
            return ExecResult(executed=False, error="BinderException: 没有这条 SQL 的预制结果")
        if isinstance(outcome, ExecResult):
            return outcome
        width = len(outcome[0]) if outcome else 0
        return ExecResult(executed=True, columns=[f"c{i}" for i in range(width)], rows=list(outcome),
                          row_count=len(outcome))


def make_result(*, question="q", status="ok", sql=GOLD_SQL, requirement="完整需求", tables=(LOAN,),
                columns=(f"{LOAN}.loan_bal_rmb",), candidates=None, first_pass_ok=True, validated=True,
                failed_step="", message="", clarify=None, calls=1, elapsed_ms=1000, tokens=(100, 20),
                validation_ok=True) -> QueryResult:
    """造一个 QueryResult。validated 为假时没有校验结果（没走到校验）"""
    has_sql = status in ("ok", "unvalidated")
    return QueryResult(
        status=status, question=question, requirement=requirement, sql=sql if has_sql else "",
        validation=ValidationSummary(ok=validation_ok, tables=list(tables), columns=list(columns)) if validated else None,
        first_pass_ok=first_pass_ok if validated else None, failed_step=failed_step, message=message,
        clarify=clarify, candidate_tables=list(tables if candidates is None else candidates),
        calls=[CallRecord(step="plan", attempt=index + 1, elapsed_ms=10, input_tokens=tokens[0],
                          output_tokens=tokens[1], ok=True) for index in range(calls)],
        elapsed_ms=elapsed_ms)


def make_clarify(*, by_parser: bool = False) -> QueryResult:
    """④ 的反问（带方案）；by_parser 为真时是 ① 的反问（还没有方案）"""
    clarify = Clarify(question="要不要含保证金？", options=[ClarifyOption(label="不含保证金"), ClarifyOption(label="含保证金")])
    result = make_result(status="need_clarify", validated=False, clarify=clarify)
    return result if by_parser else result.model_copy(update={"plan": QueryPlan(decision="need_clarify", clarify=clarify)})


def sql_question(qid="Q1", *, turns=("查贷款余额",), category="基础时点余额", ordered=False, alternatives=(),
                 gold_sql=GOLD_SQL, tables=(LOAN,), columns=(f"{LOAN}.loan_bal_rmb",)) -> EvalQuestion:
    return EvalQuestion(id=qid, category=category, turns=list(turns), expect="sql", gold_sql=gold_sql,
                        gold_tables=list(tables), gold_columns=list(columns), ordered=ordered,
                        alternatives=list(alternatives))


def clarify_question(qid="A1") -> EvalQuestion:
    return EvalQuestion(id=qid, category="反问", turns=["查存款余额"], expect="clarify")


def after_clarify_question(qid="N1", *, expect="sql", first_expect="clarify", turns=None) -> EvalQuestion:
    """「反问之后」的题：第一轮必须反问，第二轮是回答（期望 SQL）或新问题（期望反问）"""
    if expect == "clarify":
        return EvalQuestion(id=qid, category="反问之后", turns=list(turns or ("查存款余额", "查日均余额")),
                            expect="clarify", first_expect=first_expect)
    return EvalQuestion(id=qid, category="反问之后", turns=list(turns or ("查存款余额", "含保证金的")), expect="sql",
                        gold_sql=GOLD_SQL, gold_tables=[LOAN], gold_columns=[f"{LOAN}.loan_bal_rmb"],
                        first_expect=first_expect)


def judged(result: QueryResult, answers_clarify: bool) -> QueryResult:
    """给结果加上 ① 的解析：本轮是不是在回答上一轮的反问"""
    return result.model_copy(update={"parsed": ParsedRequest(is_query=True, answers_clarify=answers_clarify)})


def score(question, *results, config="C", executor=None, store):
    pipeline = FakePipeline(in_order(*results))
    outcome = evaluate_question(pipeline, question, config, executor or FakeExecutor(), TODAY, store=store)
    return outcome, pipeline


def make_outcome(**overrides) -> QuestionOutcome:
    """造一个答对的 SQL 题结果，按需改字段"""
    base = dict(config="C", question_id="Q1", category="基础时点余额", expect="sql", results=[make_result()],
                exec_ok=True, exec_error="", recall_ok=True, table_ok=True, column_ok=True, clarify_ok=True,
                first_pass_ok=True, final_ok=True, elapsed_ms=1000, llm_calls=1, tokens=120, attribution="")
    base.update(overrides)
    return QuestionOutcome(**base)


# ── 计分：执行准确率与三项准确率 ──────────────────────────

def test_correct_answer_scores_everything_right(mock_store):
    outcome, _ = score(sql_question(), make_result(), store=mock_store)

    assert (outcome.exec_ok, outcome.exec_error) == (True, "")
    assert (outcome.recall_ok, outcome.table_ok, outcome.column_ok) == (True, True, True)
    assert (outcome.clarify_ok, outcome.first_pass_ok, outcome.final_ok) == (True, True, True)
    assert outcome.attribution == ""
    assert (outcome.config, outcome.question_id, outcome.category, outcome.expect) == (
        "C", "Q1", "基础时点余额", "sql")


def test_a_wrong_result_is_not_exec_ok(mock_store):
    outcome, _ = score(sql_question(), make_result(sql=OTHER_SQL), store=mock_store)

    assert outcome.exec_ok is False and outcome.exec_error == ""


def test_generated_sql_and_gold_sql_are_both_executed_with_the_evaluation_limits(mock_store):
    executor = FakeExecutor()

    score(sql_question(), make_result(), executor=executor, store=mock_store)

    assert sorted(executor.calls) == sorted([(GOLD_SQL, EVAL_MAX_ROWS, EVAL_TIMEOUT_S)] * 2)
    assert (EVAL_MAX_ROWS, EVAL_TIMEOUT_S) == (100_000, 30)


def test_unvalidated_sql_is_executed_and_scored_too(mock_store):
    outcome, _ = score(sql_question(), make_result(status="unvalidated", first_pass_ok=False, validation_ok=False),
                       store=mock_store)

    assert outcome.exec_ok is True
    assert (outcome.first_pass_ok, outcome.final_ok) == (False, False)


@pytest.mark.parametrize("result", [
    make_clarify(),
    make_result(status="failed", failed_step="plan", message="出错了", validated=False),
])
def test_clarify_and_failed_results_are_never_executed(result, mock_store):
    executor = FakeExecutor()

    outcome, _ = score(sql_question(), result, executor=executor, store=mock_store)

    assert outcome.exec_ok is False and outcome.exec_error == ""
    assert outcome.final_ok is None and executor.calls == []


def test_execution_error_is_recorded_and_counts_as_wrong(mock_store):
    outcome, _ = score(sql_question(), make_result(sql="SELECT 1 FROM nowhere"), store=mock_store)

    assert outcome.exec_ok is False
    assert "BinderException" in outcome.exec_error


def test_comparison_exception_counts_as_mismatch_and_is_recorded(mock_store, monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("列对应的搜索量超过上限")

    monkeypatch.setattr(nl2sql_eval, "compare_results", boom)

    outcome, _ = score(sql_question(), make_result(), store=mock_store)

    assert outcome.exec_ok is False
    assert outcome.exec_error.startswith("比对出错：ValueError") and "搜索量" in outcome.exec_error


def test_a_result_cut_off_by_the_row_limit_is_a_mismatch_but_not_an_error(mock_store):
    cut = ExecResult(executed=True, columns=["c0"], rows=list(GOLD_ROWS), row_count=1, truncated=True)

    outcome, _ = score(sql_question(), make_result(), executor=FakeExecutor({GOLD_SQL: cut}), store=mock_store)

    assert outcome.exec_ok is False and outcome.exec_error == ""


def test_failure_to_run_the_gold_sql_is_reported_as_such(mock_store):
    broken = FakeExecutor({GOLD_SQL: ExecResult(executed=False, error="CatalogException: 表不存在")})

    outcome, _ = score(sql_question(), make_result(), executor=broken, store=mock_store)

    assert outcome.exec_ok is False
    assert "标准 SQL 执行失败" in outcome.exec_error and "CatalogException" in outcome.exec_error


def test_row_order_counts_only_for_ordered_questions(mock_store):
    gold = [(1, "a"), (2, "b")]
    executor = FakeExecutor({GOLD_SQL: gold, OTHER_SQL: gold[::-1]})
    result = make_result(sql=OTHER_SQL)

    loose, _ = score(sql_question(), result, executor=executor, store=mock_store)
    strict, _ = score(sql_question(ordered=True), result, executor=executor, store=mock_store)

    assert (loose.exec_ok, strict.exec_ok) == (True, False)


# ── 计分：其它正确答案 ────────────────────────────────────

def alt_question() -> EvalQuestion:
    """标准答案用贷款汇总表，另一种写法用存款汇总表（只是为了凑出第二组表）"""
    return sql_question(alternatives=[Alternative(sql=ALT_SQL, columns=[f"{CUST_DEP}.margin_bal_rmb"])])


def test_alternative_answer_group_is_accepted_for_the_tables_and_columns(mock_store):
    result = make_result(sql=ALT_SQL, tables=(CUST_DEP,), columns=(f"{CUST_DEP}.margin_bal_rmb", f"{CUST_DEP}.data_dt"))

    outcome, _ = score(alt_question(), result, store=mock_store)

    assert (outcome.exec_ok, outcome.recall_ok, outcome.table_ok, outcome.column_ok) == (True, True, True, True)


def test_each_metric_picks_its_own_group_but_never_mixes_two_groups(mock_store):
    question = sql_question(columns=(f"{LOAN}.loan_bal_rmb", f"{LOAN}.npl_bal_rmb"),
                            alternatives=[Alternative(sql=ALT_SQL, columns=[f"{CUST_DEP}.margin_bal_rmb",
                                                                           f"{CUST_DEP}.dep_bal_rmb"])])
    half_and_half = make_result(tables=(LOAN,), columns=(f"{LOAN}.loan_bal_rmb", f"{CUST_DEP}.margin_bal_rmb"))
    second_group = make_result(sql=ALT_SQL, tables=(LOAN,),
                               columns=(f"{CUST_DEP}.margin_bal_rmb", f"{CUST_DEP}.dep_bal_rmb"))

    mixed, _ = score(question, half_and_half, store=mock_store)
    other, _ = score(question, second_group, store=mock_store)

    assert mixed.column_ok is False                 # 两组各凑一半，哪一组都没有凑齐
    assert (other.table_ok, other.column_ok) == (True, True)       # 表按第一组、字段按第二组，各项指标各自判断


def test_alternative_without_columns_falls_back_to_the_gold_columns(mock_store):
    question = sql_question(alternatives=[Alternative(sql=ALT_SQL)], columns=(f"{CUST_DEP}.margin_bal_rmb",),
                            tables=(LOAN,))
    result = make_result(sql=ALT_SQL, tables=(CUST_DEP,), columns=(f"{CUST_DEP}.margin_bal_rmb",))

    outcome, _ = score(question, result, store=mock_store)

    assert (outcome.table_ok, outcome.column_ok) == (True, True)


def test_answer_groups_take_the_tables_of_each_alternative_from_the_validator(mock_store):
    from nl2sql.validator import SqlValidator

    groups = answer_groups(alt_question(), SqlValidator(mock_store))

    assert [sorted(group.tables) for group in groups] == [[LOAN], [CUST_DEP]]
    assert groups[0].columns == {f"{LOAN}.loan_bal_rmb"} and groups[1].columns == {f"{CUST_DEP}.margin_bal_rmb"}


def test_answer_groups_skip_alternatives_the_validator_cannot_read(mock_store):
    from nl2sql.validator import SqlValidator

    question = sql_question(alternatives=[Alternative(sql="SELECT FROM WHERE")])

    groups = answer_groups(question, SqlValidator(mock_store))

    assert len(groups) == 1             # 读不出表的写法不能成为一个空组：空集合是任何集合的子集，会把召回判成全对


def test_recall_needs_every_table_of_one_group_in_the_candidates(mock_store):
    question = sql_question(tables=(LOAN, "dim.org"), columns=(f"{LOAN}.loan_bal_rmb",),
                            alternatives=[Alternative(sql=ALT_SQL, columns=[f"{CUST_DEP}.margin_bal_rmb"])])

    only_one, _ = score(question, make_result(candidates=[LOAN, ORG_DEP]), store=mock_store)
    both, _ = score(question, make_result(candidates=[ORG_DEP, "dim.org", LOAN]), store=mock_store)
    other_group, _ = score(question, make_result(candidates=[CUST_DEP]), store=mock_store)

    assert (only_one.recall_ok, both.recall_ok, other_group.recall_ok) == (False, True, True)


def test_table_accuracy_means_the_used_tables_equal_a_group(mock_store):
    extra, _ = score(sql_question(), make_result(tables=(LOAN, "dim.org")), store=mock_store)
    fewer, _ = score(sql_question(tables=(LOAN, "dim.org")), make_result(tables=(LOAN,)), store=mock_store)

    assert extra.table_ok is False and fewer.table_ok is False


def test_without_a_validation_result_the_table_and_column_checks_fail(mock_store):
    outcome, _ = score(sql_question(), make_result(status="failed", failed_step="plan", message="x",
                                                   validated=False, candidates=[LOAN]), store=mock_store)

    assert (outcome.recall_ok, outcome.table_ok, outcome.column_ok) == (True, False, False)


# ── 计分：反问准确率 ──────────────────────────────────────

@pytest.mark.parametrize("question, result, expected", [
    (clarify_question(), make_clarify(), True),                      # 该问的问了
    (clarify_question(), make_result(), False),                      # 该问的没问
    (clarify_question(), make_result(status="failed", failed_step="parse", message="x", validated=False), False),
    (sql_question(), make_result(), True),                           # 不该问的没问
    (sql_question(), make_clarify(), False),                         # 不该问的问了
])
def test_clarify_accuracy_covers_both_expectations(question, result, expected, mock_store):
    outcome, _ = score(question, result, store=mock_store)

    assert outcome.clarify_ok is expected


def test_clarify_question_has_no_execution_or_table_scores(mock_store):
    outcome, _ = score(clarify_question(), make_clarify(), store=mock_store)

    assert outcome.exec_ok is None and outcome.exec_error == ""
    assert (outcome.recall_ok, outcome.table_ok, outcome.column_ok) == (None, None, None)
    assert outcome.attribution == ""


# ── 追问：多轮 ───────────────────────────────────────────

def test_second_turn_receives_the_requirement_of_the_first(mock_store):
    question = sql_question("F1", turns=("查贷款余额", "换成上月末的"), category="追问")
    first = make_result(requirement="查 2026 年 9 月末贷款余额", sql=OTHER_SQL)
    second = make_result(requirement="查 2026 年 8 月末贷款余额")

    outcome, pipeline = score(question, first, second, config="B", store=mock_store)

    one, two = pipeline.requests
    assert (one.question, one.prev_requirement, one.prev_clarify) == ("查贷款余额", "", None)
    assert (two.question, two.prev_requirement, two.prev_clarify) == ("换成上月末的", "查 2026 年 9 月末贷款余额", None)
    assert all(request.today == TODAY for request in pipeline.requests)
    assert CONFIGS["B"] == (True, False)
    assert all((request.use_lineage, request.use_examples) == (True, False) for request in pipeline.requests)
    assert [item.requirement for item in outcome.results] == [first.requirement, second.requirement]


def test_second_turn_after_a_clarify_carries_the_clarify_too(mock_store):
    question = sql_question("F1", turns=("查存款余额", "不含保证金"), category="追问")
    asked = make_clarify()

    _, pipeline = score(question, asked, make_result(), store=mock_store)

    assert pipeline.requests[1].prev_clarify == asked.clarify
    assert pipeline.requests[1].prev_requirement == asked.requirement


def test_the_last_turn_is_the_one_that_is_scored(mock_store):
    question = sql_question("F1", turns=("甲", "乙"), category="追问")
    first = make_result(status="failed", failed_step="parse", message="没听懂", validated=False)

    outcome, _ = score(question, first, make_result(), store=mock_store)

    assert outcome.exec_ok is True and outcome.attribution == ""
    assert outcome.premise_ok is None                               # 追问题的第一轮不计分


def test_time_calls_and_tokens_add_up_over_the_turns(mock_store):
    question = sql_question("F1", turns=("甲", "乙"), category="追问")
    first = make_result(elapsed_ms=1500, calls=2, tokens=(100, 50))
    second = make_result(elapsed_ms=2500, calls=3, tokens=(10, 5))

    outcome, _ = score(question, first, second, store=mock_store)

    assert (outcome.elapsed_ms, outcome.llm_calls, outcome.tokens) == (4000, 5, 2 * 150 + 3 * 15)


# ── 反问之后：第一轮必须反问 ─────────────────────────────

def test_after_a_clarify_the_answer_is_scored_on_the_second_turn(mock_store):
    outcome, pipeline = score(after_clarify_question(), make_clarify(), make_result(), store=mock_store)

    assert pipeline.requests[1].prev_clarify is not None
    assert (outcome.premise_ok, outcome.exec_ok, outcome.clarify_ok, outcome.attribution) == (True, True, True, "")


def test_a_first_turn_that_does_not_ask_breaks_the_premise_and_counts_as_wrong(mock_store):
    executor = FakeExecutor()

    # 第二轮的 SQL 和标准答案一样，但第一轮没有反问，第二轮测的就不是「回答反问」
    outcome, _ = score(after_clarify_question(), make_result(), make_result(), executor=executor, store=mock_store)

    assert (outcome.premise_ok, outcome.exec_ok, outcome.clarify_ok) == (False, False, False)
    assert outcome.attribution == "第一轮没有反问（状态 ok），题目前提不成立"
    assert executor.calls == [] and outcome.exec_error == ""              # 不算数的结果不再执行
    assert (outcome.recall_ok, outcome.table_ok, outcome.column_ok) == (True, True, True)   # 选表、字段照常诊断


@pytest.mark.parametrize("first, second, clarify_ok, attribution", [
    (make_clarify(), make_clarify(), True, ""),                                     # 新问题照常反问
    (make_result(), make_clarify(), False, "第一轮没有反问（状态 ok），题目前提不成立"),
    (make_result(status="failed", failed_step="parse", message="x", validated=False), make_clarify(), False,
     "第一轮没有反问（状态 failed），题目前提不成立"),
    (make_clarify(), make_result(), False, "④ 该反问没有反问"),
])
def test_a_new_question_after_a_clarify_must_be_asked_in_both_turns(first, second, clarify_ok, attribution,
                                                                     mock_store):
    outcome, _ = score(after_clarify_question(expect="clarify"), first, second, store=mock_store)

    assert (outcome.clarify_ok, outcome.attribution) == (clarify_ok, attribution)
    assert outcome.exec_ok is None


def test_a_first_turn_expected_to_give_sql_breaks_the_premise_when_it_asks(mock_store):
    outcome, _ = score(after_clarify_question(first_expect="sql"), make_clarify(), make_result(), store=mock_store)

    assert (outcome.premise_ok, outcome.exec_ok) == (False, False)
    assert outcome.attribution == "第一轮没有给出 SQL（状态 need_clarify），题目前提不成立"


@pytest.mark.parametrize("question, second, expected", [
    # 新问题被当成了回答：① 不许再问，④ 也就没问
    (after_clarify_question(expect="clarify"), judged(make_result(), True), "① 把新问题当成了对上一轮反问的回答"),
    (after_clarify_question(expect="clarify"), judged(make_result(), False), "④ 该反问没有反问"),
    # 回答没被认出来，又反问了一次
    (after_clarify_question(), judged(make_clarify(), False), "① 没认出本轮是在回答上一轮的反问"),
    (after_clarify_question(), judged(make_clarify(by_parser=True), False), "① 没认出本轮是在回答上一轮的反问"),
    (after_clarify_question(), judged(make_clarify(), True), "④ 多余的反问"),
    (after_clarify_question(), make_clarify(), "④ 多余的反问"),                    # 没有解析结果时按原来的规则
    # 追问题不看 ① 的这项判断
    (sql_question("F1", turns=("甲", "乙"), category="追问"), judged(make_clarify(), False), "④ 多余的反问"),
], ids=["新问题当成回答", "新问题没反问", "回答没认出", "回答没认出-①问的", "认出了还问", "没有解析结果", "追问题"])
def test_attribution_points_at_the_parser_when_it_misjudges_an_answer(question, second, expected, mock_store):
    outcome, _ = score(question, make_clarify(), second, store=mock_store)

    assert outcome.attribution == expected


# ── 归因 ─────────────────────────────────────────────────

def attribution_cases():
    """每条规则一个例子：(题, 结果, 期望的归因)。执行层预制：GOLD_SQL 的结果是标准答案，OTHER_SQL 不一致"""
    return {
        "1-失败": (sql_question(), make_result(status="failed", failed_step="plan", message="方案两次不合格",
                                              validated=False, candidates=[ORG_DEP]),
                  "停在 plan：方案两次不合格"),
        "2-多余的反问": (sql_question(), make_clarify(), "④ 多余的反问"),
        "2-① 多余的反问": (sql_question(), make_clarify(by_parser=True), "① 多余的反问"),
        "2-该问没问": (clarify_question(), make_result(), "④ 该反问没有反问"),
        "3-召回漏表": (sql_question(), make_result(sql=OTHER_SQL, candidates=[ORG_DEP]), "② 召回漏表"),
        "4-选表不对": (sql_question(), make_result(sql=OTHER_SQL, tables=(LOAN, CUST_DEP)), "④ 选表不对"),
        "5-字段不对": (sql_question(), make_result(sql=OTHER_SQL, columns=(f"{LOAN}.npl_bal_rmb",)),
                      "④ 字段或口径不对"),
        "6-执行失败": (sql_question(), make_result(sql="SELECT oops"), "执行失败（方言转换或 SQL 报错）"),
        "7-没通过校验": (sql_question(), make_result(sql=OTHER_SQL, status="unvalidated", first_pass_ok=False,
                                                  validation_ok=False), "⑥ 修正两次仍未通过校验"),
        "8-结果不一致": (sql_question(), make_result(sql=OTHER_SQL), "⑤ SQL 与标准答案的结果不一致"),
        "对的题没有归因": (sql_question(), make_result(), ""),
        "反问对了没有归因": (clarify_question(), make_clarify(), ""),
    }


@pytest.mark.parametrize("name", list(attribution_cases()))
def test_attribution_rules(name, mock_store):
    question, result, expected = attribution_cases()[name]

    outcome, _ = score(question, result, store=mock_store)

    assert outcome.attribution == expected


def test_failure_message_in_the_attribution_is_cut_to_sixty_characters(mock_store):
    message = "很长的失败原因" * 20 + "\n换行"
    result = make_result(status="failed", failed_step="parse", message=message, validated=False)

    outcome, _ = score(sql_question(), result, store=mock_store)

    assert outcome.attribution.startswith("停在 parse：")
    assert len(outcome.attribution) == len("停在 parse：") + 60 and "\n" not in outcome.attribution


def test_only_the_first_matching_rule_is_reported(mock_store):
    # 召回漏表、选表不对、执行失败同时成立时，只报排在最前面的召回漏表
    result = make_result(sql="SELECT oops", candidates=[ORG_DEP], tables=(ORG_DEP,))

    outcome, _ = score(sql_question(), result, store=mock_store)

    assert outcome.attribution == "② 召回漏表" and outcome.exec_error


def test_a_right_result_has_no_attribution_even_if_the_tables_differ(mock_store):
    # 执行准确率是主指标：结果一致就算对，选表、字段只是诊断
    outcome, _ = score(sql_question(), make_result(tables=(LOAN, CUST_DEP), candidates=[ORG_DEP]),
                       store=mock_store)

    assert outcome.exec_ok is True and outcome.attribution == ""
    assert (outcome.recall_ok, outcome.table_ok) == (False, False)


# ── 流水线出错 ────────────────────────────────────────────

def test_a_pipeline_that_raises_is_recorded_as_failed_and_does_not_crash(mock_store):
    pipeline = FakePipeline(lambda request: RuntimeError("连接断了"))

    outcome = evaluate_question(pipeline, sql_question(), "A", FakeExecutor(), TODAY, store=mock_store)

    final = outcome.results[-1]
    assert (final.status, final.failed_step) == ("failed", "internal")
    assert final.message == "内部错误：RuntimeError: 连接断了"
    assert outcome.exec_ok is False
    assert outcome.attribution == "停在 internal：内部错误：RuntimeError: 连接断了"


def test_a_crash_in_the_second_turn_keeps_the_first_result(mock_store):
    question = sql_question("F1", turns=("甲", "乙"), category="追问")
    pipeline = FakePipeline(in_order(make_result(), RuntimeError("超时了")))

    outcome = evaluate_question(pipeline, question, "A", FakeExecutor(), TODAY, store=mock_store)

    assert [item.status for item in outcome.results] == ["ok", "failed"]
    assert outcome.exec_ok is False


def test_an_unknown_config_is_a_programming_error(mock_store):
    with pytest.raises(ValueError, match="配置"):
        evaluate_question(FakePipeline(in_order()), sql_question(), "D", FakeExecutor(), TODAY, store=mock_store)


# ── 汇总指标 ─────────────────────────────────────────────

def test_metrics_on_a_constructed_set_of_outcomes():
    outcomes = [
        make_outcome(question_id="Q1"),
        make_outcome(question_id="Q2", exec_ok=False, recall_ok=True, table_ok=False, column_ok=True,
                     first_pass_ok=False, final_ok=True, elapsed_ms=3000, llm_calls=3, tokens=300,
                     attribution="④ 选表不对"),
        make_outcome(question_id="Q3", exec_ok=False, recall_ok=False, table_ok=False, column_ok=False,
                     first_pass_ok=None, final_ok=None, elapsed_ms=2000, llm_calls=2, tokens=240,
                     attribution="停在 parse：x"),
        make_outcome(question_id="Q4", exec_ok=True, first_pass_ok=False, final_ok=False),
        make_outcome(question_id="F1", category="追问", exec_ok=True),
        make_outcome(question_id="F2", category="追问", exec_ok=False, attribution="⑤ x"),
        make_outcome(question_id="A1", category="反问", expect="clarify", exec_ok=None, recall_ok=None,
                     table_ok=None, column_ok=None, clarify_ok=True, first_pass_ok=None, final_ok=None),
        make_outcome(question_id="A2", category="反问", expect="clarify", exec_ok=None, recall_ok=None,
                     table_ok=None, column_ok=None, clarify_ok=False, first_pass_ok=True, final_ok=True,
                     attribution="④ 该反问没有反问"),
    ]

    metrics = compute_metrics(outcomes)

    assert (metrics.questions, metrics.sql_questions) == (8, 6)
    assert (metrics.exec_ok, metrics.recall_ok, metrics.table_ok, metrics.column_ok) == (3, 5, 4, 5)
    # 有 SQL 的题：Q1、Q2、Q4、F1、F2 和 A2（没有反问却给了 SQL）；Q3 没走到校验，A1 反问了
    assert (metrics.with_sql, metrics.first_pass_ok, metrics.passed) == (6, 4, 5)
    assert (metrics.follow_up, metrics.follow_up_ok) == (2, 1)
    assert metrics.clarify_ok == 7
    assert round(metrics.avg_seconds, 4) == round((1 * 6 + 3 + 2) / 8, 4)
    assert round(metrics.avg_calls, 4) == round((1 * 6 + 3 + 2) / 8, 4)
    assert round(metrics.avg_tokens, 4) == round((120 * 6 + 300 + 240) / 8, 4)
    assert round(metrics.exec_rate, 4) == round(3 / 6, 4)
    assert round(metrics.follow_up_rate, 4) == 0.5
    assert round(metrics.clarify_rate, 4) == round(7 / 8, 4)


def test_metrics_of_nothing_have_no_rates():
    metrics = compute_metrics([])

    assert metrics.questions == 0
    assert metrics.exec_rate is None and metrics.clarify_rate is None and metrics.follow_up_rate is None
    assert metrics.avg_seconds == 0


# ── 并发运行与落盘 ────────────────────────────────────────

def small_bank():
    return [
        sql_question("Q1", turns=("甲",), category="基础时点余额"),
        sql_question("Q2", turns=("乙",), category="口径区分"),
        sql_question("F1", turns=("丙", "丙二"), category="追问"),
        clarify_question("A1"),
        clarify_question("A2"),
    ]


def small_bank_responses():
    """A 组：Q1 结果不对、Q2 失败、其余对，A2 该问没问。B 组：Q2 结果不对。C 组：全对"""
    def result_for(text, request):
        config = (request.use_lineage, request.use_examples)
        if text == "甲":
            return make_result(sql=OTHER_SQL if config == (False, False) else GOLD_SQL)
        if text == "乙":
            if config == (False, False):
                return make_result(status="failed", failed_step="plan", message="方案不合格", validated=False)
            return make_result(sql=OTHER_SQL if config == (True, False) else GOLD_SQL)
        if text == "丙":
            return make_result(requirement="丙的完整需求")
        if text == "丙二":
            return make_result()
        if text == "查存款余额":
            return make_clarify()
        raise AssertionError(text)

    def respond(request):
        if request.question == "查存款余额" and request.use_lineage is False:
            return make_result()            # A 组：该问没问
        return result_for(request.question, request)
    return respond


def test_run_eval_scores_every_question_of_every_config_and_writes_one_line_each(tmp_path, mock_store):
    questions = small_bank()
    pipeline = FakePipeline(small_bank_responses())

    summary = run_eval(pipeline, questions, ["A", "B", "C"], store=mock_store, executor=FakeExecutor(), today=TODAY,
                       workers=4, run_dir=tmp_path / "run", model="fake-model")

    assert [(o.config, o.question_id) for o in summary.outcomes] == [
        (config, question.id) for config in "ABC" for question in questions]       # 按配置、题库顺序
    assert (summary.model, summary.today) == ("fake-model", "2026-10-13")
    assert summary.started <= summary.finished and summary.started[:4] == str(date.today().year)
    assert len(pipeline.requests) == 3 * 6                    # 5 题，追问 2 轮
    by_config = {config: compute_metrics([o for o in summary.outcomes if o.config == config]) for config in "ABC"}
    assert [by_config[c].exec_ok for c in "ABC"] == [1, 2, 3]            # A：只有追问题对；B：缺 Q2；C 全对
    assert [by_config[c].clarify_ok for c in "ABC"] == [3, 5, 5]         # A：Q2 失败不算问、两道反问题只对…

    for config in "ABC":
        lines = (tmp_path / "run" / f"{config}.jsonl").read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines]
        assert sorted(record["question_id"] for record in records) == sorted(q.id for q in questions)
        assert all(record["config"] == config and record["results"] for record in records)
        assert all("calls" in record["results"][0] for record in records)


def test_each_record_carries_the_full_results_with_their_model_calls(tmp_path, mock_store):
    pipeline = FakePipeline(lambda request: make_result(requirement="完整需求一"))

    run_eval(pipeline, [sql_question("Q1")], ["C"], store=mock_store, executor=FakeExecutor(), today=TODAY,
             run_dir=tmp_path)

    record = json.loads((tmp_path / "C.jsonl").read_text(encoding="utf-8"))
    assert record["exec_ok"] is True and record["attribution"] == ""
    assert record["results"][0]["requirement"] == "完整需求一"
    assert record["results"][0]["calls"][0]["input_tokens"] == 100


def test_resume_skips_what_is_already_done_and_keeps_the_old_outcomes(tmp_path, mock_store):
    questions = small_bank()
    responses = small_bank_responses()
    first = FakePipeline(responses)
    run_eval(first, questions[:2], ["B"], store=mock_store, executor=FakeExecutor(), today=TODAY, run_dir=tmp_path)

    second = FakePipeline(responses)
    summary = run_eval(second, questions, ["B"], store=mock_store, executor=FakeExecutor(), today=TODAY,
                       run_dir=tmp_path, resume=True)

    assert [request.question for request in second.requests if request.question in ("甲", "乙")] == []
    assert sorted(request.question for request in second.requests) == ["丙", "丙二", "查存款余额", "查存款余额"]
    assert [o.question_id for o in summary.outcomes] == [q.id for q in questions]      # 新旧合在一起
    assert len((tmp_path / "B.jsonl").read_text(encoding="utf-8").splitlines()) == 5


def test_resume_ignores_a_line_cut_off_by_a_crash(tmp_path, mock_store):
    pipeline = FakePipeline(lambda request: make_result())
    run_eval(pipeline, [sql_question("Q1")], ["C"], store=mock_store, executor=FakeExecutor(), today=TODAY,
             run_dir=tmp_path)
    with (tmp_path / "C.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"config": "C", "question_id": "Q2", "categ')          # 写到一半，没有换行

    again = FakePipeline(lambda request: make_result())
    summary = run_eval(again, [sql_question("Q1"), sql_question("Q2")], ["C"], store=mock_store,
                       executor=FakeExecutor(), today=TODAY, run_dir=tmp_path, resume=True)

    assert len(again.requests) == 1                                         # 只重跑 Q2
    assert [o.question_id for o in summary.outcomes] == ["Q1", "Q2"]
    done = load_outcomes(tmp_path, ["C"])
    assert sorted(done) == [("C", "Q1"), ("C", "Q2")]                       # 新的一行没有被粘在残缺行后面


def test_resume_only_counts_the_requested_configs_and_questions(tmp_path, mock_store):
    pipeline = FakePipeline(lambda request: make_result())
    run_eval(pipeline, [sql_question("Q1"), sql_question("Q2")], ["A", "C"], store=mock_store,
             executor=FakeExecutor(), today=TODAY, run_dir=tmp_path)

    summary = run_eval(pipeline, [sql_question("Q2")], ["C"], store=mock_store, executor=FakeExecutor(),
                       today=TODAY, run_dir=tmp_path, resume=True)

    assert [(o.config, o.question_id) for o in summary.outcomes] == [("C", "Q2")]


def test_a_run_directory_with_records_needs_resume(tmp_path, mock_store):
    pipeline = FakePipeline(lambda request: make_result())
    run_eval(pipeline, [sql_question("Q1")], ["C"], store=mock_store, executor=FakeExecutor(), today=TODAY,
             run_dir=tmp_path)

    with pytest.raises(ValueError, match="resume"):
        run_eval(pipeline, [sql_question("Q1")], ["C"], store=mock_store, executor=FakeExecutor(), today=TODAY,
                 run_dir=tmp_path)


def test_unknown_config_is_rejected_before_anything_runs(tmp_path, mock_store):
    pipeline = FakePipeline(lambda request: make_result())

    with pytest.raises(ValueError, match="配置"):
        run_eval(pipeline, [sql_question("Q1")], ["A", "X"], store=mock_store, executor=FakeExecutor(), today=TODAY,
                 run_dir=tmp_path)

    assert pipeline.requests == []


def test_gold_sql_is_executed_once_per_question_however_many_configs(tmp_path, mock_store):
    executor = FakeExecutor()
    pipeline = FakePipeline(lambda request: make_result())

    run_eval(pipeline, [sql_question("Q1")], ["A", "B", "C"], store=mock_store, executor=executor, today=TODAY,
             workers=3, run_dir=tmp_path)

    sqls = [call[0] for call in executor.calls]
    assert sqls.count(GOLD_SQL) == 1 + 3          # 标准 SQL 一次，三组生成的 SQL 恰好也是它，各一次


def test_progress_callback_is_called_once_per_finished_question(tmp_path, mock_store):
    seen = []
    pipeline = FakePipeline(lambda request: make_result())

    run_eval(pipeline, [sql_question("Q1"), sql_question("Q2")], ["A", "C"], store=mock_store,
             executor=FakeExecutor(), today=TODAY, workers=2, run_dir=tmp_path, on_outcome=seen.append)

    assert sorted((o.config, o.question_id) for o in seen) == [("A", "Q1"), ("A", "Q2"), ("C", "Q1"), ("C", "Q2")]


def test_many_workers_give_the_same_outcomes_as_one(tmp_path, mock_store):
    questions = [sql_question(f"Q{index:02d}") for index in range(30)]
    pipeline = FakePipeline(lambda request: make_result())

    serial = run_eval(pipeline, questions, ["B", "C"], store=mock_store, executor=FakeExecutor(), today=TODAY,
                      workers=1, run_dir=tmp_path / "one")
    parallel = run_eval(pipeline, questions, ["B", "C"], store=mock_store, executor=FakeExecutor(), today=TODAY,
                        workers=8, run_dir=tmp_path / "eight")

    assert [(o.config, o.question_id, o.exec_ok) for o in serial.outcomes] == \
           [(o.config, o.question_id, o.exec_ok) for o in parallel.outcomes]
    assert len((tmp_path / "eight" / "C.jsonl").read_text(encoding="utf-8").splitlines()) == 30


# ── 报告 ─────────────────────────────────────────────────

@pytest.fixture
def small_summary(tmp_path, mock_store):
    questions = small_bank()
    summary = run_eval(FakePipeline(small_bank_responses()), questions, ["A", "B", "C"], store=mock_store,
                       executor=FakeExecutor(), today=TODAY, workers=2, run_dir=tmp_path, model="fake-model")
    return summary, questions


def test_report_has_every_section(small_summary):
    summary, questions = small_summary

    report = render_report(summary, questions)

    for heading in ("# NL2SQL 评估报告", "## 一、三组对比", "## 二、血缘与样例的贡献", "## 三、分类别的执行准确率",
                    "## 四、执行失败", "## 五、错题明细", "## 六、局限"):
        assert heading in report, heading
    head = report.split("## 一、")[0]
    assert "fake-model" in head and "2026-10-13" in head and "5 道" in head and "并发" in head and "用时" in head
    for config in "ABC":
        assert f"{config} {CONFIG_NAMES[config]}" in report


def test_report_comparison_table_has_the_numbers_of_each_config(small_summary):
    summary, questions = small_summary

    report = render_report(summary, questions)

    row = next(line for line in report.splitlines() if line.startswith("| 执行准确率"))
    assert row.split("|")[2:5] == [" 1/3（33.3%） ", " 2/3（66.7%） ", " 3/3（100.0%） "]
    clarify_row = next(line for line in report.splitlines() if line.startswith("| 反问准确率"))
    assert clarify_row.split("|")[2:5] == [" 3/5（60.0%） ", " 5/5（100.0%） ", " 5/5（100.0%） "]
    follow_up_row = next(line for line in report.splitlines() if line.startswith("| 追问准确率"))
    assert follow_up_row.split("|")[2:5] == [" 1/1（100.0%） "] * 3


def test_report_contribution_is_in_percentage_points(small_summary):
    summary, questions = small_summary

    report = render_report(summary, questions)

    section = report.split("## 二、")[1].split("## 三、")[0]
    assert "B−A" in section and "C−B" in section
    exec_row = next(line for line in section.splitlines() if line.startswith("| 执行准确率"))
    assert [cell.strip() for cell in exec_row.split("|")[2:4]] == ["+33.3", "+33.3"]
    clarify_row = next(line for line in section.splitlines() if line.startswith("| 反问准确率"))
    assert [cell.strip() for cell in clarify_row.split("|")[2:4]] == ["+40.0", "+0.0"]


def test_report_by_category_marks_the_clarify_category(small_summary):
    summary, questions = small_summary

    report = render_report(summary, questions)

    section = report.split("## 三、")[1].split("## 四、")[0]
    assert "| 基础时点余额 | 0/1（0.0%） | 1/1（100.0%） | 1/1（100.0%） |" in section
    assert "| 口径区分 | 0/1（0.0%） | 0/1（0.0%） | 1/1（100.0%） |" in section
    assert "| 追问 | 1/1（100.0%） | 1/1（100.0%） | 1/1（100.0%） |" in section
    assert "| 反问（按反问准确率） | 0/2（0.0%） | 2/2（100.0%） | 2/2（100.0%） |" in section


def test_report_counts_a_mixed_category_by_what_each_question_expects(tmp_path, mock_store):
    questions = [after_clarify_question("N1", expect="clarify", turns=("甲", "甲二")),
                 after_clarify_question("N2", turns=("乙", "乙二")),
                 after_clarify_question("N3", turns=("丙", "丙二")),
                 after_clarify_question("N4", turns=("丁", "丁二"))]
    pipeline = FakePipeline(by_text({
        "甲": make_clarify(), "甲二": make_clarify(),                            # 新问题照常反问：对
        "乙": make_clarify(), "乙二": make_result(),                             # 回答之后给出 SQL：对
        "丙": make_result(requirement="丙的完整需求"), "丙二": make_result(),      # 第一轮没反问：前提不成立
        "丁": make_clarify(), "丁二": make_result(sql=OTHER_SQL),                # 回答之后 SQL 不对
    }))
    summary = run_eval(pipeline, questions, ["C"], store=mock_store, executor=FakeExecutor(), today=TODAY,
                       run_dir=tmp_path)

    report = render_report(summary, questions)

    assert "题目前提不成立" in report.split("## 一、")[1].split("## 二、")[0]       # 分母的说明里写了这条规则
    section = report.split("## 三、")[1].split("## 四、")[0]
    assert "| 反问之后（期望反问的题按反问准确率） | 2/4（50.0%） |" in section
    wrong = report.split("## 五、")[1].split("## 六、")[0]
    premise = wrong.split("#### N3")[1].split("####")[0]
    assert "- 归因：第一轮没有反问（状态 ok），题目前提不成立" in premise
    assert "- 第一轮：给出了 SQL，没有反问（完整需求：丙的完整需求）" in premise
    answered = wrong.split("#### N4")[1].split("####")[0]
    assert "- 第一轮：反问「要不要含保证金？」（不含保证金；含保证金）" in answered


def test_report_details_for_wrong_answers_have_requirement_sql_gold_and_attribution(small_summary):
    summary, questions = small_summary

    report = render_report(summary, questions)

    wrong = report.split("## 五、")[1].split("## 六、")[0]
    assert "### A 仅模型结构（错 4 题）" in wrong and "### B 加血缘（错 1 题）" in wrong
    assert "### C 加血缘和样例（错 0 题）" in wrong and "没有错题" in wrong
    block = wrong.split("#### Q1")[1].split("####")[0]
    assert "⑤ SQL 与标准答案的结果不一致" in block and "完整需求" in block
    assert OTHER_SQL in block and GOLD_SQL in block
    failed = wrong.split("#### Q2")[1].split("####")[0]
    assert "停在 plan：方案不合格" in failed
    missed = wrong.split("#### A2")[1].split("####")[0]
    assert "④ 该反问没有反问" in missed and "期望被反问" in missed


def test_report_lists_execution_failures_in_their_own_section(tmp_path, mock_store):
    bad = make_result(sql="SELECT oops FROM nowhere")
    pipeline = FakePipeline(by_text({"甲": bad, "乙": make_result()}))
    questions = [sql_question("Q1", turns=("甲",)), sql_question("Q2", turns=("乙",))]
    summary = run_eval(pipeline, questions, ["C"], store=mock_store, executor=FakeExecutor(), today=TODAY,
                       run_dir=tmp_path)

    report = render_report(summary, questions)

    section = report.split("## 四、")[1].split("## 五、")[0]
    assert "Q1" in section and "BinderException" in section and "SELECT oops FROM nowhere" in section
    assert "Q2" not in section


def test_report_says_there_are_no_execution_failures_when_there_are_none(small_summary):
    summary, questions = small_summary

    section = render_report(summary, questions).split("## 四、")[1].split("## 五、")[0]

    assert "没有" in section


def test_report_with_a_single_config_has_no_contribution_numbers(tmp_path, mock_store):
    pipeline = FakePipeline(lambda request: make_result())
    questions = [sql_question("Q1")]
    summary = run_eval(pipeline, questions, ["C"], store=mock_store, executor=FakeExecutor(), today=TODAY,
                       run_dir=tmp_path)

    report = render_report(summary, questions)

    section = report.split("## 二、")[1].split("## 三、")[0]
    assert "A、B、C 三组都跑过才能" in section
    assert "| 执行准确率" not in section


def test_report_with_two_adjacent_configs_has_only_the_contribution_it_can_compute(tmp_path, mock_store):
    pipeline = FakePipeline(small_bank_responses())
    questions = small_bank()
    summary = run_eval(pipeline, questions, ["B", "C"], store=mock_store, executor=FakeExecutor(), today=TODAY,
                       run_dir=tmp_path)

    section = render_report(summary, questions).split("## 二、")[1].split("## 三、")[0]

    assert "样例贡献（C−B）" in section and "血缘贡献（B−A）" not in section
    assert "另一项需要 A、B、C 三组都跑过才能给出" in section
    exec_row = next(line for line in section.splitlines() if line.startswith("| 执行准确率"))
    assert [cell.strip() for cell in exec_row.split("|")[2:3]] == ["+33.3"]


def test_report_limitations_mention_the_known_problems(small_summary):
    summary, questions = small_summary

    limits = render_report(summary, questions).split("## 六、")[1]

    assert "专门构造" in limits and "静态评估" in limits           # 设计文档 §13.6
    assert "U05" in limits and "cust_type" in limits               # 条件在数据里不起作用，没有修
    assert "K06" not in limits                                     # 已经修了
    assert "其它正确写法" in limits or "alternatives" in limits


# ── 真实库静态评估 ────────────────────────────────────────

def static_question(qid="R01", turns=("问题一",), groups=(("ads.x1",), ("ads.y1", "ads.y2")), answer="按含保证金的口径",
                    note="") -> StaticQuestion:
    return StaticQuestion(id=qid, turns=list(turns), today=date(2026, 10, 13), gold_table_sets=[list(g) for g in groups],
                          clarify_answer=answer, note=note)


def static_ok(tables=("ads.x1",), candidates=("ads.z", "ads.x1", "ads.w"), validation_ok=True,
              requirement="静态评估的完整需求") -> QueryResult:
    return make_result(requirement=requirement, tables=tables, candidates=candidates, validation_ok=validation_ok,
                       sql="SELECT 1 FROM ads.x1")


def test_best_group_rank_is_the_latest_position_needed_to_complete_the_best_group():
    groups = [["a"], ["b", "c"], ["d"]]

    assert best_group_rank([["a"]], ["z", "a"]) == 2
    assert best_group_rank(groups, ["x", "c", "y", "b", "a"]) == 4        # b、c 要取到第 4 张；a 要取到第 5 张；取小的
    assert best_group_rank(groups, ["a", "x", "b", "c"]) == 1             # a 单独一组，第 1 张就够
    assert best_group_rank([["b", "c"]], ["x", "b", "y"]) is None         # 组里有一张不在候选里，这组没有名次
    assert best_group_rank(groups, ["x", "y"]) is None
    assert best_group_rank([], ["a"]) is None
    assert best_group_rank([["a", "a"]], ["a"]) == 1


def test_static_eval_answers_one_clarify_with_the_given_caliber(tmp_path):
    asked = make_clarify()
    asked.requirement = "第一轮的需求"
    pipeline = FakePipeline(in_order(asked, static_ok()))

    run_static_eval(pipeline, [static_question()], tmp_path)

    first, second = pipeline.requests
    assert (first.question, first.prev_requirement, first.prev_clarify) == ("问题一", "", None)
    assert (second.question, second.prev_requirement, second.prev_clarify) == ("按含保证金的口径", "第一轮的需求", asked.clarify)
    assert second.today == date(2026, 10, 13)
    assert (first.use_lineage, first.use_examples) == (True, True)         # 只跑 C 组


def test_static_eval_counts_a_second_clarify_as_asking_twice(tmp_path):
    pipeline = FakePipeline(in_order(make_clarify(), make_clarify()))

    report = run_static_eval(pipeline, [static_question()], tmp_path)

    text = report.read_text(encoding="utf-8")
    assert "反问两次" in text
    assert len(pipeline.requests) == 2                                      # 不会问第三遍
    record = json.loads(report.with_suffix(".jsonl").read_text(encoding="utf-8"))
    assert record["clarify_count"] == 2 and record["status"] == "need_clarify"


def test_static_eval_without_a_clarify_answer_does_not_answer(tmp_path):
    pipeline = FakePipeline(in_order(make_clarify()))

    report = run_static_eval(pipeline, [static_question(answer="")], tmp_path)

    assert len(pipeline.requests) == 1
    record = json.loads(report.with_suffix(".jsonl").read_text(encoding="utf-8"))
    assert record["clarify_count"] == 1 and record["status"] == "need_clarify"


def test_static_eval_judges_containment_of_the_gold_tables(tmp_path):
    questions = [
        static_question("R01", turns=("一",)),           # 用了第一组 {x1}，还多用了别的：包含
        static_question("R02", turns=("二",)),           # 用了 {y1} 缺 y2：不包含任何一组
        static_question("R03", turns=("三",)),           # 用了 {y1, y2}：包含第二组
    ]
    pipeline = FakePipeline(by_text({
        "一": static_ok(tables=("ads.x1", "ads.extra")),
        "二": static_ok(tables=("ads.y1",), candidates=("ads.y1",)),
        "三": static_ok(tables=("ads.y2", "ads.y1"), candidates=("ads.y2", "ads.y1")),
    }))

    report = run_static_eval(pipeline, questions, tmp_path)

    records = [json.loads(line) for line in report.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(r["question_id"], r["tables_ok"], r["recall_ok"], r["best_rank"]) for r in records] == [
        ("R01", True, True, 2), ("R02", False, False, None), ("R03", True, True, 2)]
    text = report.read_text(encoding="utf-8")
    assert "SQL 用的表包含标准用表" in text and "2/3" in text


def test_static_eval_records_validation_and_the_material_for_manual_judgement(tmp_path):
    result = static_ok(validation_ok=False)
    result.plan = QueryPlan(decision="ok", caliber="口径：取存款余额字段", table_basis="用表依据：粒度够用",
                            join_basis="关联说明：一对一", lineage_note="溯源说明：还原了过滤条件", assumptions=["按上月末"])
    result.assumptions = ["按上月末"]
    pipeline = FakePipeline(in_order(result))

    report = run_static_eval(pipeline, [static_question(note="手写检索词时排第 3")], tmp_path)

    text = report.read_text(encoding="utf-8")
    for needle in ("静态评估的完整需求", "口径：取存款余额字段", "用表依据：粒度够用", "关联说明：一对一",
                   "溯源说明：还原了过滤条件", "SELECT 1 FROM ads.x1", "手写检索词时排第 3", "校验未通过"):
        assert needle in text, needle


def test_static_eval_report_explains_how_the_rank_is_counted(tmp_path):
    pipeline = FakePipeline(in_order(static_ok()))

    text = run_static_eval(pipeline, [static_question()], tmp_path).read_text(encoding="utf-8")

    assert "名次" in text and "最靠后" in text and "没有名次" in text


def test_static_eval_survives_a_pipeline_that_raises(tmp_path):
    pipeline = FakePipeline(lambda request: RuntimeError("断网"))

    report = run_static_eval(pipeline, [static_question()], tmp_path)

    record = json.loads(report.with_suffix(".jsonl").read_text(encoding="utf-8"))
    assert record["status"] == "failed" and record["results"][0]["failed_step"] == "internal"


def test_static_eval_files_get_a_sequence_number_when_the_name_is_taken(tmp_path):
    pipeline = FakePipeline(lambda request: static_ok())

    first = run_static_eval(pipeline, [static_question()], tmp_path)
    second = run_static_eval(pipeline, [static_question()], tmp_path)

    assert first.name.endswith("_static_eval.md") and second.name.endswith("_static_eval_2.md")
    assert first.with_suffix(".jsonl").exists() and second.with_suffix(".jsonl").exists()


def test_load_static_questions(tmp_path):
    path = tmp_path / "static.yaml"
    path.write_text(yaml.safe_dump([
        {"id": "R01", "turns": ["问题一"], "today": date(2026, 10, 13), "gold_table_sets": [["ads.x1"], ["ads.y1", "ads.y2"]],
         "clarify_answer": "含保证金", "note": "备注"},
        {"id": "R02", "turns": ["问题二", "再看一下"], "today": "2026-10-14", "gold_table_sets": [["ads.x2"]]},
    ], allow_unicode=True), encoding="utf-8")

    questions = load_static_questions(path)

    assert [q.id for q in questions] == ["R01", "R02"]
    assert questions[0].gold_table_sets == [["ads.x1"], ["ads.y1", "ads.y2"]]
    assert (questions[0].clarify_answer, questions[1].clarify_answer) == ("含保证金", "")
    assert questions[1].today == date(2026, 10, 14) and questions[1].turns == ["问题二", "再看一下"]


@pytest.mark.parametrize("content, message", [
    ("", "空的"),
    ("id: R01\n", "列表"),
    ("- {id: R01, turns: [一], today: 2026-10-13, gold_table_sets: [[a.b]]}\n"
     "- {id: R01, turns: [二], today: 2026-10-13, gold_table_sets: [[a.b]]}\n", "重复"),
    ("- {id: R01, turns: [], today: 2026-10-13, gold_table_sets: [[a.b]]}\n", "turns"),
    ("- {id: R01, turns: [一], today: 2026-10-13, gold_table_sets: []}\n", "gold_table_sets"),
    ("- {id: R01, turns: [一], today: 2026-10-13, gold_table_sets: [[]]}\n", "标准用表"),
    ("- {id: R01, turns: [一], today: 2026-10-13, gold_table_sets: [[a.b]], clarify_anwser: x}\n", "clarify_anwser"),
])
def test_unusable_static_question_files_are_rejected(tmp_path, content, message):
    path = tmp_path / "static.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_static_questions(path)


# ── 命令行 ───────────────────────────────────────────────

def test_static_mode_requires_out(tmp_path, capsys, monkeypatch):
    called = []
    monkeypatch.setattr(nl2sql_eval, "_build_pipeline", lambda mart_dir: called.append(mart_dir))
    path = tmp_path / "static.yaml"
    path.write_text("- {id: R01, turns: [一], today: 2026-10-13, gold_table_sets: [[a.b]]}\n", encoding="utf-8")

    code = main(["--static", str(path), "--mart", str(tmp_path)])

    assert code == 2
    assert "--out" in capsys.readouterr().err
    assert called == []                              # 没有建流水线，更没有跑题


def test_static_mode_runs_and_writes_into_out(tmp_path, monkeypatch, capsys):
    pipeline = FakePipeline(lambda request: static_ok())
    seen = []
    monkeypatch.setattr(nl2sql_eval, "_build_pipeline", lambda mart_dir: seen.append(mart_dir) or pipeline)
    path = tmp_path / "static.yaml"
    path.write_text("- {id: R01, turns: [一], today: 2026-10-13, gold_table_sets: [[ads.x1]]}\n", encoding="utf-8")
    out = tmp_path / "out"

    code = main(["--static", str(path), "--mart", str(tmp_path / "mart"), "--out", str(out)])

    assert code == 0
    assert seen == [tmp_path / "mart"]
    reports = list(out.glob("*_static_eval.md"))
    assert len(reports) == 1 and (out / reports[0].name.replace(".md", ".jsonl")).exists()
    assert str(reports[0]) in capsys.readouterr().out


def test_mock_mode_rejects_bad_arguments(capsys):
    assert main(["--configs", "A,X"]) == 2
    assert "X" in capsys.readouterr().err
    assert main(["--ids", "NOPE"]) == 2
    assert "NOPE" in capsys.readouterr().err
    assert main(["--today", "昨天"]) == 2
    assert main(["--workers", "0"]) == 2
    assert main(["--resume", "不存在的目录"]) == 2


def test_help_exits_cleanly(capsys):
    assert main(["--help"]) == 0
    assert "--static" in capsys.readouterr().out


def test_unique_path_counts_up_until_no_listed_suffix_is_taken(tmp_path):
    (tmp_path / "r.md").write_text("", encoding="utf-8")
    (tmp_path / "r_2.jsonl").write_text("", encoding="utf-8")

    assert nl2sql_eval._unique_path(tmp_path, "fresh") == tmp_path / "fresh.md"
    assert nl2sql_eval._unique_path(tmp_path, "r") == tmp_path / "r_2.md"
    assert nl2sql_eval._unique_path(tmp_path, "r", (".md", ".jsonl")) == tmp_path / "r_3.md"


def test_load_outcomes_skips_records_it_cannot_read_and_keeps_the_last_of_duplicates(tmp_path, mock_store):
    pipeline = FakePipeline(lambda request: make_result())
    run_eval(pipeline, [sql_question("Q1")], ["C"], store=mock_store, executor=FakeExecutor(), today=TODAY,
             run_dir=tmp_path)
    good = json.loads((tmp_path / "C.jsonl").read_text(encoding="utf-8"))
    newer = dict(good, attribution="后来重跑的")
    extra_field = dict(good, question_id="Q2", unknown_field=1)
    with (tmp_path / "C.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(newer, ensure_ascii=False) + "\n" + json.dumps(extra_field) + "\n\n[1, 2]\n")

    found = load_outcomes(tmp_path, ["C", "A"])

    assert sorted(found) == [("C", "Q1")]
    assert found[("C", "Q1")].attribution == "后来重跑的"


def test_load_outcomes_reads_old_records_without_the_premise(tmp_path):
    record = nl2sql_eval.outcome_to_record(make_outcome())
    del record["premise_ok"]                                        # 加这一项之前写的记录
    (tmp_path / "C.jsonl").write_text(json.dumps(record, ensure_ascii=False, default=str) + "\n", encoding="utf-8")

    done = load_outcomes(tmp_path, ["C"])

    assert done[("C", "Q1")].premise_ok is None


# ── 用真实题库、真实库和校验器走一遍 ──────────────────────

def perfect_responses(questions, validator, use_alternatives: bool):
    """一个完美的流水线：每道题都给出标准 SQL（或登记的另一种写法），反问题都反问"""
    mapping = {}
    for question in questions:                                      # 两轮的题的第一轮先放一个占位结果
        if len(question.turns) == 2:
            first = make_clarify() if question.first_expect == "clarify" else make_result()
            mapping[question.turns[0]] = first.model_copy(update={"requirement": f"{question.id} 的第一轮"})
    for question in questions:
        text = question.turns[-1]
        if question.expect == "clarify":
            mapping[text] = make_clarify()
            continue
        sql = question.alternatives[0].sql if use_alternatives and question.alternatives else question.gold_sql
        report = validator.validate(sql)
        mapping[text] = make_result(sql=sql, tables=report.tables, columns=report.columns, candidates=report.tables,
                                    requirement=f"{question.id} 的完整需求")
    return by_text(mapping)


@pytest.mark.parametrize("use_alternatives", [False, True], ids=["gold", "alternatives"])
def test_a_perfect_pipeline_gets_every_metric_right_on_the_real_question_bank(tmp_path, mock_db, mock_store,
                                                                              mock_validator, use_alternatives):
    questions = load_questions()
    pipeline = FakePipeline(perfect_responses(questions, mock_validator, use_alternatives))

    summary = run_eval(pipeline, questions, ["A", "B", "C"], store=mock_store, executor=DuckDBExecutor(mock_db),
                       today=TODAY, workers=4, run_dir=tmp_path)

    for config in "ABC":
        metrics = compute_metrics([o for o in summary.outcomes if o.config == config])
        assert (metrics.questions, metrics.sql_questions) == (43, 37)
        assert (metrics.exec_ok, metrics.recall_ok, metrics.table_ok, metrics.column_ok) == (37, 37, 37, 37), config
        assert (metrics.clarify_ok, metrics.follow_up_ok, metrics.follow_up) == (43, 5, 5)
        premises = {o.question_id: o.premise_ok for o in summary.outcomes if o.config == config and o.premise_ok}
        assert premises == {"N01": True, "N02": True, "N03": True}
        wrong = [(o.question_id, o.attribution, o.exec_error) for o in summary.outcomes
                 if o.config == config and o.attribution]
        assert wrong == [], wrong


def test_the_wrong_answers_of_the_question_bank_are_scored_wrong(tmp_path, mock_db, mock_store, mock_validator):
    questions = [q for q in load_questions() if q.id in ("K06", "D05", "T02", "T04")]
    wrong_sql = {
        "K06": "SELECT COUNT(DISTINCT cust_no) FROM ads.corp_cust_dep_bal_d WHERE data_dt = DATE '2026-09-30'",
        "D05": ("SELECT o.lvl1_org_nm, SUM(l.loan_bal_rmb) FROM ads.corp_cust_loan_bal_d l JOIN dim.org o "
                "ON l.org_no = o.org_no WHERE l.data_dt = DATE '2026-09-30' GROUP BY o.lvl1_org_nm"),
        "T02": ("SELECT SUM(CASE WHEN data_dt = DATE '2026-09-30' THEN dep_bal_rmb ELSE 0 END) - "
                "SUM(CASE WHEN data_dt = DATE '2026-01-01' THEN dep_bal_rmb ELSE 0 END) FROM ads.org_corp_dep_bal_d "
                "WHERE data_dt IN (DATE '2026-09-30', DATE '2026-01-01')"),
        "T04": ("SELECT AVG(dep_bal_rmb) FROM ads.corp_cust_dep_bal_d "
                "WHERE data_dt BETWEEN DATE '2026-09-01' AND DATE '2026-09-30'"),
    }
    mapping = {}
    for question in questions:
        report = mock_validator.validate(wrong_sql[question.id])
        mapping[question.turns[0]] = make_result(sql=wrong_sql[question.id], tables=report.tables,
                                                 columns=report.columns, candidates=report.tables)
    pipeline = FakePipeline(by_text(mapping))

    summary = run_eval(pipeline, questions, ["C"], store=mock_store, executor=DuckDBExecutor(mock_db), today=TODAY,
                       run_dir=tmp_path)

    assert [o.exec_ok for o in summary.outcomes] == [False] * 4


def test_main_runs_the_mock_evaluation_and_resumes(tmp_path, mock_db, mock_store, mock_validator, monkeypatch, capsys):
    questions = load_questions()
    pipeline = FakePipeline(perfect_responses(questions, mock_validator, False))
    pipeline.deps.store = mock_store
    monkeypatch.setattr(nl2sql_eval, "_build_pipeline", lambda mart_dir: pipeline)
    out = tmp_path / "reports"

    code = main(["--configs", "A,C", "--ids", "B01,K02,A01", "--workers", "2", "--out", str(out)])

    assert code == 0
    reports = list(out.glob("*_nl2sql_eval.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "fake-model" in text and "2/2（100.0%）" in text          # 两道 SQL 题全对
    run_dir = next((out / "runs").iterdir())
    assert sorted(path.name for path in run_dir.glob("*.jsonl")) == ["A.jsonl", "C.jsonl"]
    assert len(pipeline.requests) == 6
    shown = capsys.readouterr().out
    assert str(reports[0]) in shown and "执行准确率" in shown

    code = main(["--configs", "A,C", "--ids", "B01,K02,A01", "--resume", str(run_dir), "--out", str(out)])

    assert code == 0
    assert len(pipeline.requests) == 6                                # 都已经做过，没有再调流水线
    assert len(list(out.glob("*_nl2sql_eval*.md"))) == 2              # 同一天第二份报告加了序号
    assert any(path.name.endswith("_nl2sql_eval_2.md") for path in out.glob("*.md"))


def test_main_limit_and_ids(tmp_path, mock_db, mock_store, mock_validator, monkeypatch):
    questions = load_questions()
    pipeline = FakePipeline(perfect_responses(questions, mock_validator, False))
    pipeline.deps.store = mock_store
    monkeypatch.setattr(nl2sql_eval, "_build_pipeline", lambda mart_dir: pipeline)

    main(["--configs", "C", "--limit", "3", "--out", str(tmp_path / "limit")])
    asked = sorted(request.question for request in pipeline.requests)
    assert asked == sorted([questions[0].turns[0], questions[1].turns[0], questions[2].turns[0]])

    pipeline.requests.clear()
    main(["--configs", "C", "--ids", "K02,B01", "--limit", "1", "--out", str(tmp_path / "ids")])
    assert [request.question for request in pipeline.requests] == [questions[0].turns[0]]    # 先按题号选，再取前 N，保持题库顺序


def test_main_reports_a_missing_metadata_database_without_a_traceback(monkeypatch, capsys, tmp_path):
    def missing(mart_dir):
        raise FileNotFoundError("元数据库不存在，请先运行 python -m metadata.importer")

    monkeypatch.setattr(nl2sql_eval, "_build_pipeline", missing)

    code = main(["--configs", "C", "--out", str(tmp_path)])

    assert code == 1
    assert "metadata.importer" in capsys.readouterr().err


# ── 终审补的几处：放宽的选表口径、按字节读逐题记录、控制台编码、--mart 要配 --out ──────────

def test_an_extra_dimension_table_fails_the_strict_table_check_but_passes_containment(mock_store):
    outcome, _ = score(sql_question(), make_result(tables=(LOAN, "dim.org")), store=mock_store)

    assert (outcome.exec_ok, outcome.table_ok, outcome.table_contains_ok) == (True, False, True)
    metrics = compute_metrics([outcome])
    assert (metrics.table_ok, metrics.table_contains_ok, metrics.table_contains_rate) == (0, 1, 1.0)


def test_report_shows_both_table_checks(small_summary):
    summary, questions = small_summary
    text = render_report(summary, questions)

    assert "| 选表准确率 |" in text and "| 选表准确率（包含标准用表） |" in text
    assert "两个数一起看" in text


def test_load_outcomes_reads_bytes_so_a_cut_character_or_a_line_separator_loses_nothing(tmp_path):
    first = make_outcome(question_id="Q1", attribution="说明里有 行分隔符和\u0085")
    lines = [json.dumps(nl2sql_eval.outcome_to_record(item), ensure_ascii=False, default=str)
             for item in (first, make_outcome(question_id="Q2"))]
    cut = json.dumps(nl2sql_eval.outcome_to_record(make_outcome(question_id="Q3", attribution="中文")),
                     ensure_ascii=False, default=str).encode("utf-8")
    cut = cut[:cut.index("中".encode("utf-8")) + 1]                    # 截在一个汉字的三个字节中间
    (tmp_path / "C.jsonl").write_bytes(("\n".join(lines) + "\n").encode("utf-8") + cut)

    done = load_outcomes(tmp_path, ["C"])

    assert sorted(done) == [("C", "Q1"), ("C", "Q2")]
    assert done[("C", "Q1")].attribution == first.attribution


def test_main_survives_characters_the_console_cannot_encode(tmp_path, mock_db, mock_store, monkeypatch):
    failed = make_result(status="failed", validated=False, failed_step="plan",
                         message="关联（ads.x ↔ dim.y）的左表不在 tables 里")
    pipeline = FakePipeline(lambda request: failed)
    pipeline.deps.store = mock_store
    monkeypatch.setattr(nl2sql_eval, "_build_pipeline", lambda mart_dir: pipeline)
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="gbk"))   # 「↔」不在 GBK 里

    code = main(["--configs", "C", "--ids", "B01", "--out", str(tmp_path)])

    assert code == 0 and len(list(tmp_path.glob("*_nl2sql_eval.md"))) == 1


def test_mock_mode_with_a_mart_needs_an_out_directory(tmp_path, capsys):
    code = main(["--configs", "C", "--mart", str(tmp_path)])

    assert code == 2 and "--out" in capsys.readouterr().err
