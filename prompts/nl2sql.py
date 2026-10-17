"""
查询流水线三个模型步骤的提示词
- ① 需求解析、④ 选表定口径用 JSON 模式输出；⑤ 生成 SQL 输出一个 sql 代码块
- 固定的规则放在系统提示词里，这次的内容放在用户消息里：前缀不变，DeepSeek 的前缀缓存才能命中
- render_evidence 把证据写成给模型看的文本，render_tables 只写给定表的结构

RETRY_PROMPT 由 nl2sql.llm.structured_call 使用：输出不合格时带着问题再试一次。
"""
from collections.abc import Sequence
from datetime import date

from nl2sql.schemas import Evidence, ParsedRequest, QueryPlan, QueryRequest

EVIDENCE_CHAR_LIMIT = 40000                   # 证据文本超过这个字数时按固定顺序收缩

RETRY_PROMPT = """你上一次的输出有下面这些问题：
{problems}

请逐条改正，重新输出完整的 JSON。只输出 JSON，不要写解释，不要用代码块包起来。"""

# 以下由 W1-B 写实
PARSER_SYSTEM = ""
PLANNER_SYSTEM = ""
GENERATOR_SYSTEM = ""
DIALECT_RULES = ""


def parser_messages(request: QueryRequest, today: date) -> list[dict[str, str]]:
    """① 需求解析的消息：系统提示词是固定规则，用户消息带今天、上一轮需求、上一轮反问和本轮输入

    Args:
        request: 本轮请求
        today: 换算相对日期用的「今天」

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    raise NotImplementedError("由 W1-B 实现")


def planner_messages(parsed: ParsedRequest, evidence: Evidence, *, allow_clarify: bool,
                     allow_upstream: bool) -> list[dict[str, str]]:
    """④ 选表定口径的消息：用户消息带需求解析结果、证据文本，以及能不能反问、能不能上溯的说明

    Args:
        parsed: ① 的结果
        evidence: ③ 的结果
        allow_clarify: 本轮能不能反问（本轮已经是对反问的回答时不能）
        allow_upstream: 能不能要求上溯（A 组不能）

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    raise NotImplementedError("由 W1-B 实现")


def generator_messages(parsed: ParsedRequest, plan: QueryPlan, evidence: Evidence, *, previous_sql: str = "",
                       errors: Sequence[str] = ()) -> list[dict[str, str]]:
    """⑤ 生成 SQL 的消息：只列方案里的表的结构；C 组带参考样例的 SQL；修正时带上一次的 SQL 和校验错误

    Args:
        parsed: ① 的结果
        plan: ④ 的方案
        evidence: ③ 的结果
        previous_sql: 修正时上一次生成的 SQL
        errors: 修正时上一次的校验错误（「R4：…」），不含警告

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    raise NotImplementedError("由 W1-B 实现")


def render_evidence(evidence: Evidence) -> str:
    """把证据写成给模型看的文本

    分节：候选表；B、C 组的指标字段加工链路、同源字段对比、表之间的上下游、还可以上溯到的表；
    关联路径；C 组的相似样例；最后是「说明」。超过 EVIDENCE_CHAR_LIMIT 时按固定顺序收缩，收缩了什么写进「说明」。
    同样的证据必然得到同样的文本。

    Args:
        evidence: ③ 的结果

    Returns:
        证据文本
    """
    raise NotImplementedError("由 W1-B 实现")


def render_tables(evidence: Evidence, table_ids: Sequence[str]) -> str:
    """只写给定表的结构，格式同 render_evidence 的候选表一节，供 ⑤ 使用

    Args:
        evidence: ③ 的结果
        table_ids: 要写的表，按这个顺序；不在证据里的跳过

    Returns:
        表结构文本
    """
    raise NotImplementedError("由 W1-B 实现")
