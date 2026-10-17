"""
④ 选表定口径
模型按证据给出四种结论之一：ok（查询方案）、need_clarify（反问）、need_upstream（缺什么，要上溯）、
cannot_answer（元数据里做不到）。代码核对方案里的表、字段、关联都在证据里之后才往下走。
"""
from nl2sql.llm import LLMClient
from nl2sql.schemas import CallRecord, Evidence, ParsedRequest, QueryPlan


def plan_query(llm: LLMClient, parsed: ParsedRequest, evidence: Evidence, *, allow_clarify: bool,
               allow_upstream: bool) -> tuple[QueryPlan, list[CallRecord]]:
    """调模型定查询方案，输出经 check_plan 核对，不合格带着问题重试一次

    Args:
        llm: 模型
        parsed: ① 的结果
        evidence: ③ 的结果
        allow_clarify: 本轮能不能反问（本轮已经是对反问的回答时不能）
        allow_upstream: 能不能要求上溯（A 组不能）

    Returns:
        (查询方案, 调用记录)

    Raises:
        StepError: 模型调用失败，或者两次输出都不合格
    """
    raise NotImplementedError("由 W1-B 实现")


def check_plan(plan: QueryPlan, evidence: Evidence, *, allow_clarify: bool, allow_upstream: bool) -> list[str]:
    """核对查询方案，返回能直接告诉模型怎么改的问题；合格时返回空列表"""
    raise NotImplementedError("由 W1-B 实现")
