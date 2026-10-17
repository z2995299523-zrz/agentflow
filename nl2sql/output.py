"""
⑦ 输出组装用到的工具：SQL 排版、校验结果和执行结果的摘要、假设合并、失败时给用户的提示语
"""
from collections.abc import Sequence

import sqlglot

from metadata.sqlscope import DIALECT, quiet_sqlglot
from nl2sql.executor import ExecResult
from nl2sql.retriever import RecallQuery
from nl2sql.schemas import ExecSummary, QueryPlan, ValidationSummary
from nl2sql.validator import SqlValidator, ValidationIssue, ValidationReport


def format_sql(sql: str, validator: SqlValidator | None = None) -> str:
    """用 sqlglot 按 postgres 美化 SQL

    美化失败（解析不了、不是恰好一条语句），或者传了校验器而美化后的错误比原文多，就返回原文（去首尾空白）。
    美化只改排版，语义不变，但 sqlglot 会顺手改写个别写法（例如 DATE '2026-09-30' 写成 CAST('2026-09-30' AS DATE)），
    所以交给校验器核对一遍：错误数多了说明改写踩到了校验器不认的写法，宁可给原文。

    Args:
        sql: 生成的 SQL
        validator: 静态校验器，传了就核对美化没有引入新错误

    Returns:
        排版后的 SQL
    """
    text = sql.strip()
    try:
        with quiet_sqlglot():
            statements = sqlglot.transpile(text, read=DIALECT, write=DIALECT, pretty=True)
    except Exception:  # noqa: BLE001 ParseError、TokenError 以及 sqlglot 内部异常都算美化失败，给原文
        return text
    if len(statements) != 1 or not statements[0].strip():
        return text
    formatted = statements[0].strip()
    if validator is not None:
        before = len(validator.validate(text).errors)
        if len(validator.validate(formatted).errors) > before:
            return text
    return formatted


def _issue_text(issue: ValidationIssue) -> str:
    """一条校验问题写成「R4：消息」"""
    return f"{issue.rule}：{issue.message}"


def summarize_validation(report: ValidationReport) -> ValidationSummary:
    """校验报告的摘要，每条问题写成「R4：消息」，错误和警告分开"""
    return ValidationSummary(ok=report.ok, errors=[_issue_text(issue) for issue in report.errors],
                             warnings=[_issue_text(issue) for issue in report.warnings],
                             tables=list(report.tables), columns=list(report.columns))


def summarize_exec(result: ExecResult) -> ExecSummary:
    """执行结果的摘要，rows 转成 list[list]"""
    return ExecSummary(executed=result.executed, columns=list(result.columns), rows=[list(row) for row in result.rows],
                       row_count=result.row_count, truncated=result.truncated, elapsed_ms=result.elapsed_ms,
                       error=result.error, note=result.note)


def merge_assumptions(*groups: Sequence[str]) -> list[str]:
    """合并几组假设：去首尾空白，去掉空的，保序去重"""
    merged: dict[str, None] = {}
    for group in groups:
        for item in group:
            text = item.strip()
            if text:
                merged.setdefault(text)
    return list(merged)


def upstream_failure_message(plan: QueryPlan, scope_level: int) -> str:
    """上溯到顶仍缺东西时给用户的说明：「上溯 2 层仍找不到：…」

    Args:
        plan: 最后一次 ④ 的方案，missing 是缺的东西，reason 是补充说明
        scope_level: 已经上溯的层数
    """
    missing = "、".join(dict.fromkeys(item.strip() for item in plan.missing if item.strip())) or "所需的表或字段"
    message = f"上溯 {scope_level} 层仍找不到：{missing}"
    reason = plan.reason.strip()
    return f"{message}。{reason}" if reason else message


def no_candidate_message(query: RecallQuery, notes: Sequence[str]) -> str:
    """一张候选表也没有时给用户的说明：「元数据里没有找到与「…」相关的字段」，加上召回的说明

    「…」是指标、维度、过滤的叫法；一个也没有时用完整需求。

    Args:
        query: ② 用的检索词
        notes: 召回结果里的说明，写明为什么没找到（例如检索词没有命中任何字段、只走了关键字路）
    """
    terms = dict.fromkeys(term.strip() for term in (*query.metrics, *query.dimensions, *query.filters) if term.strip())
    named = "、".join(terms) or query.requirement.strip()
    message = f"元数据里没有找到与「{named}」相关的字段" if named else "元数据里没有找到与需求相关的字段"
    return f"{message}。{'；'.join(notes)}" if notes else message
