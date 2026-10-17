"""④ 选表定口径：核对规则（每条有正例和反例）、带重试的方案生成"""
import pytest

from nl2sql.llm import ScriptedLLM, StepError
from nl2sql.planner import check_plan, plan_query
from nl2sql.schemas import Clarify, ClarifyOption, Evidence, PlanField, PlanJoin, PlanTable
from prompts.nl2sql import render_evidence
from tests.step_fixtures import CUST_BAL, DIM_ORG, EXAMPLE_ID, ORG_BAL, make_evidence, make_parsed, make_plan


@pytest.fixture(scope="module")
def evidence(mock_store):
    return make_evidence(mock_store)


def check(plan, evidence, *, clarify=True, upstream=True):
    return check_plan(plan, evidence, allow_clarify=clarify, allow_upstream=upstream)


def two_options():
    return [ClarifyOption(label="不含保证金", meaning=f"用 {ORG_BAL}.dep_bal_rmb，加工时剔除了保证金账户"),
            ClarifyOption(label="含保证金", meaning=f"用 {ORG_BAL}.dep_bal_tot_rmb，是两者之和")]


# ── decision = ok ─────────────────────────────

def test_a_complete_plan_passes(evidence):
    assert check(make_plan(), evidence) == []
    assert check(make_plan(), evidence, clarify=False, upstream=False) == []     # 开关只管反问和上溯


def test_an_ok_plan_needs_tables_and_fields(evidence):
    problems = check(make_plan(tables=[], fields=[], joins=[]), evidence)

    assert len(problems) == 2
    assert "tables" in problems[0] and "fields" in problems[1]


def test_a_table_outside_the_evidence_is_reported_with_the_available_tables(evidence):
    plan = make_plan(tables=make_plan().tables + [PlanTable(table="ads.no_such_table")])

    problems = check(plan, evidence)

    assert len(problems) == 1
    assert "ads.no_such_table" in problems[0] and "不在候选里" in problems[0]
    assert ORG_BAL in problems[0] and CUST_BAL in problems[0] and DIM_ORG in problems[0]     # 可用的表全列出来


@pytest.mark.parametrize("written, hint", [
    ("ADS.ORG_CORP_DEP_BAL_D", ORG_BAL),                  # 大小写
    ("org_corp_dep_bal_d", ORG_BAL),                      # 没带模式名
])
def test_a_table_written_loosely_gets_the_exact_name_as_a_hint(evidence, written, hint):
    plan = make_plan(tables=[PlanTable(table=written), PlanTable(table=DIM_ORG)],
                     fields=[PlanField(table=written, column="dep_bal_rmb", usage="metric")], joins=[])

    problems = check(plan, evidence)

    assert any(written in p and f"应写成 {hint}" in p for p in problems)


def test_a_field_whose_table_is_not_in_the_plan_tables_is_reported(evidence):
    plan = make_plan(fields=make_plan().fields + [PlanField(table=CUST_BAL, column="dep_bal_rmb", usage="metric")])

    problems = check(plan, evidence)

    assert len(problems) == 1
    assert f"{CUST_BAL}.dep_bal_rmb" in problems[0] and "tables" in problems[0]


def test_a_field_whose_table_is_unknown_is_reported_once(evidence):
    plan = make_plan(fields=[PlanField(table="ads.no_such_table", column="x", usage="metric")])

    problems = check(plan, evidence)

    assert len(problems) == 1 and "ads.no_such_table" in problems[0] and "不在候选里" in problems[0]


def test_a_field_missing_from_its_table_is_reported_with_close_names(evidence):
    plan = make_plan(fields=[PlanField(table=ORG_BAL, column="dep_bal_rm", usage="metric")])

    problems = check(plan, evidence)

    assert len(problems) == 1
    assert f"表 {ORG_BAL} 没有字段 dep_bal_rm" in problems[0] and "dep_bal_rmb" in problems[0]


def test_a_field_written_in_the_wrong_case_gets_the_exact_name_as_a_hint(evidence):
    plan = make_plan(fields=[PlanField(table=ORG_BAL, column="DEP_BAL_RMB", usage="metric")])

    problems = check(plan, evidence)

    assert len(problems) == 1 and "应写成 dep_bal_rmb" in problems[0]


def test_the_same_problem_is_not_repeated(evidence):
    plan = make_plan(fields=[PlanField(table=ORG_BAL, column="bad_col", usage="metric"),
                             PlanField(table=ORG_BAL, column="bad_col", usage="filter")])

    assert len(check(plan, evidence)) == 1


@pytest.mark.parametrize("side", ["left", "right"])
def test_a_join_side_outside_the_plan_tables_is_reported(evidence, side):
    join = make_plan().joins[0]
    other = join.model_copy(update={f"{side}_table": CUST_BAL})
    plan = make_plan(joins=[other])

    problems = check(plan, evidence)

    assert len(problems) == 1
    assert CUST_BAL in problems[0] and "tables" in problems[0] and ("左" if side == "left" else "右") in problems[0]


@pytest.mark.parametrize("side, column", [("left", "no_such"), ("right", "no_such")])
def test_a_join_column_missing_from_its_table_is_reported(evidence, side, column):
    join = make_plan().joins[0]
    other = join.model_copy(update={f"{side}_columns": [column]})
    plan = make_plan(joins=[other])

    problems = check(plan, evidence)

    assert len(problems) == 1 and "没有字段 no_such" in problems[0]


@pytest.mark.parametrize("left, right", [(["org_no", "data_dt"], ["org_no"]), (["org_no"], ["org_no", "org_nm"]), ([], [])])
def test_join_sides_need_the_same_non_zero_number_of_columns(evidence, left, right):
    join = PlanJoin(left_table=ORG_BAL, left_columns=left, right_table=DIM_ORG, right_columns=right)

    problems = check(make_plan(joins=[join]), evidence)

    assert len(problems) == 1 and "个数" in problems[0] and "至少" in problems[0]


def test_a_multi_column_join_with_matching_counts_passes(evidence):
    join = PlanJoin(left_table=ORG_BAL, left_columns=["org_no", "data_dt"], right_table=CUST_BAL,
                    right_columns=["org_no", "data_dt"], join_type="LEFT")
    plan = make_plan(tables=make_plan().tables + [PlanTable(table=CUST_BAL)],
                     joins=make_plan().joins + [join])

    assert check(plan, evidence) == []


def test_examples_used_must_be_in_the_evidence(evidence):
    problems = check(make_plan(examples_used=["不存在的样例#9"]), evidence)

    assert len(problems) == 1 and "不存在的样例#9" in problems[0] and EXAMPLE_ID in problems[0]
    assert check(make_plan(examples_used=[EXAMPLE_ID]), evidence) == []


def test_examples_used_must_be_empty_when_the_evidence_has_no_examples(mock_store):
    evidence = make_evidence(mock_store, use_examples=False)

    problems = check(make_plan(examples_used=[EXAMPLE_ID]), evidence)

    assert len(problems) == 1 and "留空" in problems[0]


def test_a_table_added_by_a_join_path_counts_as_a_candidate(mock_store):
    evidence = make_evidence(mock_store)
    evidence.tables[2].reason = "join"

    assert check(make_plan(), evidence) == []


# ── decision = need_clarify ───────────────────

def test_a_clarify_with_a_question_and_two_options_passes(evidence):
    plan = make_plan(decision="need_clarify", clarify=Clarify(question="存款余额要不要包含保证金？", options=two_options()),
                     tables=[], fields=[], joins=[])

    assert check(plan, evidence) == []                       # 反问时不核对表和字段


def test_a_clarify_is_refused_when_the_round_answers_a_clarify(evidence):
    plan = make_plan(decision="need_clarify", clarify=Clarify(question="要不要含保证金？", options=two_options()))

    problems = check(plan, evidence, clarify=False)

    assert len(problems) == 1
    assert "不能再反问" in problems[0] and "assumptions" in problems[0] and "ok" in problems[0]


@pytest.mark.parametrize("clarify, expected", [
    (None, "clarify.question"),
    (Clarify(question="  ", options=[]), "clarify.question"),
])
def test_a_clarify_needs_a_question(evidence, clarify, expected):
    problems = check(make_plan(decision="need_clarify", clarify=clarify), evidence)

    assert any(expected in p for p in problems)


@pytest.mark.parametrize("options", [
    [],
    [ClarifyOption(label="不含保证金")],
    [ClarifyOption(label="不含保证金"), ClarifyOption(label="  ", meaning="没有选项文字")],
])
def test_a_clarify_needs_two_labelled_options(evidence, options):
    plan = make_plan(decision="need_clarify", clarify=Clarify(question="要不要含保证金？", options=options))

    problems = check(plan, evidence)

    assert len(problems) == 1 and "clarify.options" in problems[0] and "label" in problems[0]


# ── decision = need_upstream ──────────────────

def test_an_upstream_request_with_missing_items_passes(evidence):
    plan = make_plan(decision="need_upstream", missing=["币种"], reason="专题层没有币种")

    assert check(plan, evidence) == []


def test_an_upstream_request_is_refused_when_upstream_is_not_allowed(evidence):
    plan = make_plan(decision="need_upstream", missing=["币种"])

    problems = check(plan, evidence, upstream=False)

    assert len(problems) == 1 and "不能上溯" in problems[0]
    assert "cannot_answer" in problems[0] and "ok" in problems[0]


@pytest.mark.parametrize("missing", [[], ["  "]])
def test_an_upstream_request_needs_to_say_what_is_missing(evidence, missing):
    problems = check(make_plan(decision="need_upstream", missing=missing), evidence)

    assert len(problems) == 1 and "missing" in problems[0]


# ── decision = cannot_answer ──────────────────

def test_cannot_answer_with_a_reason_passes(evidence):
    assert check(make_plan(decision="cannot_answer", reason="元数据里没有客户经理相关的字段"), evidence) == []


@pytest.mark.parametrize("reason", ["", "   "])
def test_cannot_answer_needs_a_reason(evidence, reason):
    problems = check(make_plan(decision="cannot_answer", reason=reason), evidence)

    assert len(problems) == 1 and "reason" in problems[0]


# ── plan_query ────────────────────────────────

def test_plan_query_returns_the_plan_with_one_record(evidence):
    llm = ScriptedLLM([make_plan().model_dump_json()])

    plan, records = plan_query(llm, make_parsed(), evidence, allow_clarify=True, allow_upstream=True)

    assert plan == make_plan()
    assert [(r.step, r.attempt, r.ok) for r in records] == [("plan", 1, True)]
    assert llm.calls[0]["json_mode"] is True
    user = llm.calls[0]["messages"][1]["content"]
    assert make_parsed().requirement in user and render_evidence(evidence) in user


def test_plan_query_retries_with_the_problem_text(evidence):
    bad = make_plan(tables=[PlanTable(table="ads.no_such_table"), PlanTable(table=DIM_ORG)])
    llm = ScriptedLLM([bad.model_dump_json(), make_plan().model_dump_json()])

    plan, records = plan_query(llm, make_parsed(), evidence, allow_clarify=True, allow_upstream=True)

    assert plan == make_plan() and [(r.attempt, r.ok) for r in records] == [(1, False), (2, True)]
    assert "ads.no_such_table" in llm.calls[1]["messages"][-1]["content"]


def test_plan_query_applies_the_switches_when_checking(evidence):
    clarify = make_plan(decision="need_clarify", clarify=Clarify(question="要不要含保证金？", options=two_options()))
    llm = ScriptedLLM([clarify.model_dump_json(), clarify.model_dump_json()])

    with pytest.raises(StepError) as caught:
        plan_query(llm, make_parsed(), evidence, allow_clarify=False, allow_upstream=True)

    assert caught.value.step == "plan"
    assert "输出两次都不合格" in caught.value.message and "不能再反问" in caught.value.message


def test_plan_query_accepts_a_clarify_when_allowed(evidence):
    clarify = make_plan(decision="need_clarify", clarify=Clarify(question="要不要含保证金？", options=two_options()))
    llm = ScriptedLLM([clarify.model_dump_json()])

    plan, records = plan_query(llm, make_parsed(), evidence, allow_clarify=True, allow_upstream=False)

    assert plan.decision == "need_clarify" and len(records) == 1
    assert "能不能反问：能" in llm.calls[0]["messages"][1]["content"]


def test_plan_query_gives_up_after_two_bad_plans(evidence):
    bad = make_plan(tables=[], fields=[])
    llm = ScriptedLLM([bad.model_dump_json(), bad.model_dump_json()])

    with pytest.raises(StepError) as caught:
        plan_query(llm, make_parsed(), evidence, allow_clarify=True, allow_upstream=True)

    assert [(r.attempt, r.ok) for r in caught.value.calls] == [(1, False), (2, False)]


def test_plan_query_turns_model_errors_into_step_errors(evidence):
    llm = ScriptedLLM([ConnectionError("断网")])

    with pytest.raises(StepError) as caught:
        plan_query(llm, make_parsed(), evidence, allow_clarify=True, allow_upstream=True)

    assert caught.value.step == "plan" and caught.value.message.startswith("模型调用失败：")


def test_an_empty_evidence_is_reported_without_a_blank_list():
    problems = check(make_plan(), Evidence(use_lineage=True, use_examples=False))

    assert len(problems) == 2 and all("可用的表：（没有）" in problem for problem in problems)
