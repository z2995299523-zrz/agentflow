"""
① 需求解析
把本轮输入（连同上一轮的完整需求、上一轮的反问）解析成结构化的需求：是不是查数需求、完整需求复述、
指标（计数类带被计数的实体）、维度、过滤、排序与条数、时间（相对日期按「今天」换算）、口径线索、子问题、假设。
"""
from collections.abc import Sequence
from datetime import date

from nl2sql.llm import LLMClient
from nl2sql.retriever import RecallQuery
from nl2sql.schemas import CallRecord, ParsedRequest, QueryRequest


def parse_request(llm: LLMClient, request: QueryRequest, today: date) -> tuple[ParsedRequest, list[CallRecord]]:
    """调模型解析需求，输出经 check_parsed 核对，不合格带着问题重试一次

    Args:
        llm: 模型
        request: 本轮请求
        today: 换算相对日期用的「今天」

    Returns:
        (解析结果, 调用记录)

    Raises:
        StepError: 模型调用失败，或者两次输出都不合格
    """
    raise NotImplementedError("由 W1-B 实现")


def check_parsed(parsed: ParsedRequest) -> list[str]:
    """核对解析结果，返回能直接告诉模型怎么改的问题；合格时返回空列表"""
    raise NotImplementedError("由 W1-B 实现")


def to_recall_query(parsed: ParsedRequest, extra_terms: Sequence[str] = ()) -> RecallQuery:
    """解析结果转成召回的检索词

    metrics 是指标叫法、被计数的实体和 extra_terms（上溯时缺的东西）；dimensions 是维度；
    filters 是过滤要素的字段叫法。各自保序去重。

    Args:
        parsed: ① 的结果
        extra_terms: 追加的指标检索词

    Returns:
        召回的输入
    """
    raise NotImplementedError("由 W1-B 实现")
