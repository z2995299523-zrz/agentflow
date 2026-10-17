"""⑦ 输出工具的测试：SQL 排版（含美化失败、美化后错误变多时用原文）、校验结果和执行结果的摘要、假设合并、两个失败提示语

排版的往返检查用模拟题库的 35 条标准 SQL：排版之后仍然通过校验，并且在模拟库上查出同样的数。
"""
import json
from datetime import date
from decimal import Decimal

import pytest

from eval.questions import load_questions
from metadata.checks import ERROR, WARNING
from nl2sql.executor import DuckDBExecutor, ExecResult, NullExecutor
from nl2sql.output import (
    format_sql,
    merge_assumptions,
    no_candidate_message,
    summarize_exec,
    summarize_validation,
    upstream_failure_message,
)
from nl2sql.retriever import RecallQuery
from nl2sql.schemas import ExecSummary, QueryPlan, ValidationSummary
from nl2sql.validator import ValidationIssue, ValidationReport

SQL_QUESTIONS = [question for question in load_questions() if question.expect == "sql"]

MESSY = ("select o.org_nm,sum(b.dep_bal_rmb) as dep_bal from ads.org_corp_dep_bal_d b join dim.org o "
         "on b.org_no=o.org_no where b.data_dt=date '2026-09-30' group by o.org_nm order by dep_bal desc limit 10;  ")


class FakeValidator:
    """按 SQL 文本返回预先写好的错误数，并记下被校验过的文本"""

    def __init__(self, on_original: int, on_formatted: int, original: str, extra_warning: bool = False) -> None:
        self.counts = (on_original, on_formatted)
        self.original = original
        self.extra_warning = extra_warning
        self.calls: list[str] = []

    def validate(self, sql: str, expected_tables=None, expected_columns=None) -> ValidationReport:
        self.calls.append(sql)
        count = self.counts[0] if sql == self.original else self.counts[1]
        issues = [ValidationIssue("R4", ERROR, f"错误 {number}") for number in range(count)]
        if self.extra_warning and sql != self.original:
            issues.append(ValidationIssue("R6", WARNING, "警告"))
        return ValidationReport(issues=issues)


# ── 排版 ──────────────────────────────────────

def test_format_sql_breaks_clauses_onto_their_own_lines():
    formatted = format_sql(MESSY)

    lines = formatted.splitlines()
    assert lines[0] == "SELECT" and "FROM ads.org_corp_dep_bal_d AS b" in lines
    assert lines[-1] == "LIMIT 10"
    assert "2026-09-30" in formatted
    assert formatted == formatted.strip() and not formatted.endswith(";")


def test_format_sql_is_idempotent():
    once = format_sql(MESSY)

    assert format_sql(once) == once


def test_format_sql_returns_the_trimmed_original_when_it_cannot_be_parsed():
    assert format_sql("  SELEC FROM ((  ") == "SELEC FROM (("
    assert format_sql("   ") == ""


def test_format_sql_leaves_anything_but_a_single_statement_alone():
    assert format_sql(" SELECT 1; SELECT 2 ") == "SELECT 1; SELECT 2"      # 多条语句留给校验器去报 R1
    assert format_sql(" ; ") == ";"


@pytest.mark.parametrize("on_original, on_formatted, expect_formatted", [
    (0, 0, True), (0, 1, False), (1, 1, True), (2, 1, True), (1, 3, False)])
def test_format_sql_keeps_the_original_when_formatting_adds_errors(on_original, on_formatted, expect_formatted):
    validator = FakeValidator(on_original, on_formatted, MESSY.strip())

    result = format_sql(MESSY, validator)

    assert (result == MESSY.strip()) is (not expect_formatted)
    assert result == (format_sql(MESSY) if expect_formatted else MESSY.strip())
    assert validator.calls == [MESSY.strip(), format_sql(MESSY)]               # 两份文本都校验过，原文在前


def test_format_sql_only_counts_errors_not_warnings():
    validator = FakeValidator(0, 0, MESSY.strip(), extra_warning=True)

    assert format_sql(MESSY, validator) == format_sql(MESSY)


def test_format_sql_does_not_validate_when_nothing_was_formatted():
    validator = FakeValidator(0, 0, "SELEC FROM ((")

    assert format_sql("SELEC FROM ((", validator) == "SELEC FROM (("
    assert validator.calls == []


@pytest.mark.parametrize("question", SQL_QUESTIONS, ids=lambda question: question.id)
def test_formatted_gold_sql_still_passes_validation_and_returns_the_same_rows(question, mock_validator, mock_db):
    formatted = format_sql(question.gold_sql, mock_validator)

    report = mock_validator.validate(formatted, question.gold_tables, question.gold_columns)
    executor = DuckDBExecutor(mock_db)
    before, after = executor.execute(question.gold_sql, max_rows=None), executor.execute(formatted, max_rows=None)

    assert formatted != question.gold_sql.strip() and "\n" in formatted         # 真的排过版，不是退回了原文
    assert [(issue.rule, issue.message) for issue in report.issues] == []
    assert after.executed, after.error
    assert sorted(after.rows, key=repr) == sorted(before.rows, key=repr)


# ── 摘要 ──────────────────────────────────────

def test_summarize_validation_splits_errors_from_warnings_and_prefixes_the_rule():
    report = ValidationReport(
        issues=[ValidationIssue("R4", ERROR, "表 ads.a 没有字段 x"), ValidationIssue("R6", WARNING, "关联不在已知关联里"),
                ValidationIssue("R5", ERROR, "快照表缺少数据日期条件")],
        tables=["ads.a", "dim.b"], columns=["ads.a.y", "dim.b.k"])

    summary = summarize_validation(report)

    assert summary == ValidationSummary(
        ok=False, errors=["R4：表 ads.a 没有字段 x", "R5：快照表缺少数据日期条件"], warnings=["R6：关联不在已知关联里"],
        tables=["ads.a", "dim.b"], columns=["ads.a.y", "dim.b.k"])
    assert summary.tables is not report.tables and summary.columns is not report.columns


def test_summarize_validation_passes_with_warnings_only():
    summary = summarize_validation(ValidationReport(issues=[ValidationIssue("R7", WARNING, "没有约束粒度字段")]))

    assert summary.ok and summary.errors == [] and summary.warnings == ["R7：没有约束粒度字段"]
    assert summarize_validation(ValidationReport()) == ValidationSummary(ok=True)


def test_summarize_exec_turns_rows_into_lists():
    result = ExecResult(executed=True, columns=["org", "bal"], rows=[("A", Decimal("1.50")), ("B", Decimal("2"))],
                        row_count=2, truncated=True, elapsed_ms=12)

    summary = summarize_exec(result)

    assert summary == ExecSummary(executed=True, columns=["org", "bal"],
                                  rows=[["A", Decimal("1.50")], ["B", Decimal("2")]],
                                  row_count=2, truncated=True, elapsed_ms=12)
    assert all(isinstance(row, list) for row in summary.rows)
    result.rows.append(("C", Decimal("3")))
    assert len(summary.rows) == 2                                           # 摘要不跟着原结果变


def test_summarize_exec_keeps_errors_and_notes_and_stays_json_friendly():
    failed = summarize_exec(ExecResult(executed=False, error="ValueError: 只能执行单条语句", elapsed_ms=3))
    skipped = summarize_exec(NullExecutor().execute("SELECT 1"))
    with_data = summarize_exec(ExecResult(executed=True, columns=["d", "v"], rows=[(date(2026, 9, 30), Decimal("1.5"))],
                                          row_count=1))

    assert (failed.executed, failed.error, failed.elapsed_ms) == (False, "ValueError: 只能执行单条语句", 3)
    assert (skipped.executed, skipped.rows) == (False, [])
    assert skipped.note == NullExecutor().execute("SELECT 1").note != ""
    assert json.loads(json.dumps(with_data.model_dump(mode="json"))) == {
        "executed": True, "columns": ["d", "v"], "rows": [["2026-09-30", "1.5"]], "row_count": 1,
        "truncated": False, "elapsed_ms": 0, "error": "", "note": ""}


# ── 假设合并 ──────────────────────────────────────

def test_merge_assumptions_trims_drops_empty_and_dedupes_in_order():
    merged = merge_assumptions(["「最新」按上一自然日 2026-10-12", " 币种按人民币 "], ["币种按人民币", "", "   "],
                               ("新增的假设", "「最新」按上一自然日 2026-10-12"))

    assert merged == ["「最新」按上一自然日 2026-10-12", "币种按人民币", "新增的假设"]
    assert merge_assumptions() == [] and merge_assumptions([], [""]) == []


# ── 提示语 ──────────────────────────────────────

def test_upstream_failure_message_names_the_level_and_what_is_missing():
    plan = QueryPlan(decision="need_upstream", missing=["币种", " 产品 ", "", "币种"], reason="整合层和贴源层都没有")

    assert upstream_failure_message(plan, 2) == "上溯 2 层仍找不到：币种、产品。整合层和贴源层都没有"
    assert upstream_failure_message(plan.model_copy(update={"reason": ""}), 1) == "上溯 1 层仍找不到：币种、产品"


def test_upstream_failure_message_without_a_list_of_what_is_missing():
    plan = QueryPlan(decision="need_upstream")

    assert upstream_failure_message(plan, 2) == "上溯 2 层仍找不到：所需的表或字段"


def test_no_candidate_message_names_the_terms_and_appends_the_recall_notes():
    query = RecallQuery("查各部门立项项目数", ["项目数", "立项项目数"], ["部门"], ["项目数", " 年度 "])

    assert no_candidate_message(query, []) == "元数据里没有找到与「项目数、立项项目数、部门、年度」相关的字段"
    assert no_candidate_message(query, ["检索词「项目数」没有命中任何字段", "元数据库里没有字段向量"]) == (
        "元数据里没有找到与「项目数、立项项目数、部门、年度」相关的字段。"
        "检索词「项目数」没有命中任何字段；元数据库里没有字段向量")


def test_no_candidate_message_falls_back_to_the_requirement_when_there_are_no_terms():
    assert no_candidate_message(RecallQuery("查项目数", []), []) == "元数据里没有找到与「查项目数」相关的字段"
    assert no_candidate_message(RecallQuery("  ", []), []) == "元数据里没有找到与需求相关的字段"
