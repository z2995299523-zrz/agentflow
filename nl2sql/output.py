"""
⑦ 输出组装用到的工具：SQL 排版、校验结果和执行结果的摘要、假设合并、失败时给用户的提示语
"""
from collections.abc import Sequence

from nl2sql.executor import ExecResult
from nl2sql.retriever import RecallQuery
from nl2sql.schemas import ExecSummary, QueryPlan, ValidationSummary
from nl2sql.validator import SqlValidator, ValidationReport


def format_sql(sql: str, validator: SqlValidator | None = None) -> str:
    """用 sqlglot 按 postgres 美化 SQL

    美化失败，或者传了校验器而美化后的错误比原文多，就返回原文（去首尾空白）。

    Args:
        sql: 生成的 SQL
        validator: 静态校验器，传了就核对美化没有引入新错误

    Returns:
        排版后的 SQL
    """
    raise NotImplementedError("由 W1-A 实现")


def summarize_validation(report: ValidationReport) -> ValidationSummary:
    """校验报告的摘要，每条问题写成「R4：消息」，错误和警告分开"""
    raise NotImplementedError("由 W1-A 实现")


def summarize_exec(result: ExecResult) -> ExecSummary:
    """执行结果的摘要，rows 转成 list[list]"""
    raise NotImplementedError("由 W1-A 实现")


def merge_assumptions(*groups: Sequence[str]) -> list[str]:
    """合并几组假设：去首尾空白，去掉空的，保序去重"""
    raise NotImplementedError("由 W1-A 实现")


def upstream_failure_message(plan: QueryPlan, scope_level: int) -> str:
    """上溯到顶仍缺东西时给用户的说明：「上溯 2 层仍找不到：…」"""
    raise NotImplementedError("由 W1-A 实现")


def no_candidate_message(query: RecallQuery, notes: Sequence[str]) -> str:
    """一张候选表也没有时给用户的说明：「元数据里没有找到与「…」相关的字段」，加上召回的说明"""
    raise NotImplementedError("由 W1-A 实现")
