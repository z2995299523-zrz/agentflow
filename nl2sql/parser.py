"""
① 需求解析
把本轮输入（连同上一轮的完整需求、上一轮的反问）解析成结构化的需求：是不是查数需求、完整需求复述、
指标（计数类带被计数的实体）、维度、过滤、排序与条数、时间（相对日期按「今天」换算）、口径线索、子问题、假设。
是查数需求、但缺了没法默认的要素（要看的单个对象没说是哪个、没说看哪些指标）时，① 自己反问一次；
本轮已经是对反问的回答时不能再问。
"""
import re
from collections.abc import Iterable, Sequence
from datetime import date

from nl2sql.llm import LLMClient, structured_call
from nl2sql.retriever import RecallQuery
from nl2sql.schemas import CallRecord, ParsedRequest, QueryRequest, TimeSpec
from prompts.nl2sql import parser_messages

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# 每种时间类型要几个日期，和写给模型看的说法
_DATE_COUNTS = {"none": 0, "point": 1, "daily_avg": 1, "range": 2}
_DATE_MEANINGS = {
    "point": "时点日期",
    "daily_avg": "日均的截止日期",
    "range": "起、止两个日期 [起, 止]（含两端）",
}
_COMPARE_HINTS = {
    "prev_year_end": "较年初要写当期所在年份的上一年 12 月 31 日",
    "prev_month_end": "较上月末要写当期日期的上一个月的最后一天",
    "other": "要写对比的日期",
}


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
    allow_clarify = request.prev_clarify is None
    return structured_call(llm, "parse", parser_messages(request, today), ParsedRequest,
                           lambda parsed: check_parsed(parsed, allow_clarify=allow_clarify))


def _valid_date(text: str) -> bool:
    """是不是合法的 YYYY-MM-DD：格式严格（月、日两位），日期要真实存在"""
    if _ISO_DATE.fullmatch(text) is None:
        return False
    try:
        date(int(text[:4]), int(text[5:7]), int(text[8:]))
    except ValueError:
        return False
    return True


def _check_time(time: TimeSpec) -> list[str]:
    """核对时间要素：日期格式、日期个数、区间的起止顺序、对比日期"""
    problems = []
    for label, values in (("time.dates", time.dates), ("time.compare_dates", time.compare_dates)):
        for index, value in enumerate(values):
            if not _valid_date(value):
                problems.append(f"{label}[{index}] 的值「{value}」不是合法的 YYYY-MM-DD 日期，"
                                "请按「今天」换算成具体日期，例如 2026-09-30")
    expected = _DATE_COUNTS[time.kind]
    if len(time.dates) != expected:
        if time.kind == "none":
            problems.append(f"time.kind 是 none（不限定日期）时 time.dates 必须为空，现在有 {len(time.dates)} 个；"
                            "需求里有具体日期的话，请把 kind 改成 point（时点）或 range（区间）")
        else:
            problems.append(f"time.kind 是 {time.kind} 时 time.dates 必须正好 {expected} 个日期（{_DATE_MEANINGS[time.kind]}），"
                            f"现在有 {len(time.dates)} 个；请按「今天」把相对日期换算成 YYYY-MM-DD")
    elif time.kind == "range" and all(_valid_date(value) for value in time.dates) and time.dates[0] > time.dates[1]:
        problems.append(f"time.dates 的起 {time.dates[0]} 晚于止 {time.dates[1]}，请调换顺序，写成 [起, 止]")
    if time.compare != "none" and not time.compare_dates:
        problems.append(f"time.compare 是 {time.compare} 时必须在 time.compare_dates 里写对比日期（{_COMPARE_HINTS[time.compare]}）；"
                        "没有对比就把 compare 改成 none")
    return problems


def _check_clarify(parsed: ParsedRequest, allow_clarify: bool) -> list[str]:
    """① 的反问：本轮能反问；有问题；至少两个带 label 的选项"""
    if not allow_clarify:
        return ["本轮是对反问的回答，不能再反问：clarify 写 null，按用户的回答补全需求；"
                "回答没说清的（例如「按最常用的口径处理」）按上一轮反问的第一个选项理解，写进 assumptions"]
    problems = []
    if not parsed.clarify.question.strip():
        problems.append("clarify 不是 null 时必须在 clarify.question 里写一个问题")
    labelled = [option for option in parsed.clarify.options if option.label.strip()]
    if len(labelled) < 2:
        problems.append(f"clarify.options 至少要有 2 个带 label 的选项（第一个是最常见的理解，另一个请用户指定），"
                        f"现在只有 {len(labelled)} 个")
    return problems


def check_parsed(parsed: ParsedRequest, *, allow_clarify: bool = True) -> list[str]:
    """核对解析结果，返回能直接告诉模型怎么改的问题；合格时返回空列表

    是查数需求时：完整需求非空，日期合法、个数与时间类型相符，有对比时对比日期非空；
    没有反问时至少一个指标，有反问时要能反问、有问题、至少两个带 label 的选项（指标可以还没说清）。
    不是查数需求时：回复非空。

    Args:
        parsed: ① 的结果
        allow_clarify: 本轮能不能反问（本轮已经是对反问的回答时不能）
    """
    if not parsed.is_query:
        if parsed.reply.strip():
            return []
        return ["is_query 是 false 时必须在 reply 里写给用户的回复，说明这个工具能做什么、请用户怎么提问"]
    problems = []
    if not parsed.requirement.strip():
        problems.append("requirement 是空的，请用一句自包含的话复述完整需求"
                        "（日期写成具体日期，口径限定词和要列出的列都保留）")
    if parsed.clarify is not None:
        return problems + _check_clarify(parsed, allow_clarify) + _check_time(parsed.time)
    if not any(metric.name.strip() for metric in parsed.metrics):
        problems.append("metrics 里没有指标，至少要写一个指标（业务叫法，如「对公贷款余额」；明细查询写最主要的度量字段，"
                        "只列某类对象的信息时写这类对象的叫法，如「客户经理」）；"
                        "如果用户既没说指标、也没说要看哪类对象，请把 is_query 改成 false，在 reply 里请用户补充")
    return problems + _check_time(parsed.time)


def _dedupe(terms: Iterable[str]) -> list[str]:
    """去首尾空白、去掉空串、保序去重"""
    seen: dict[str, None] = {}
    for term in terms:
        text = term.strip()
        if text:
            seen.setdefault(text)
    return list(seen)


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
    metrics = _dedupe([m.name for m in parsed.metrics] + [m.entity for m in parsed.metrics] + list(extra_terms))
    return RecallQuery(requirement=parsed.requirement, metrics=metrics, dimensions=_dedupe(parsed.dimensions),
                       filters=_dedupe(item.field for item in parsed.filters))
