"""
⑤ 生成 SQL
按查询方案写一条 SQL。修正轮带上一次的 SQL 和校验错误（只带错误，不带警告）。
"""
from collections.abc import Sequence

from nl2sql.llm import LLMClient
from nl2sql.schemas import CallRecord, Evidence, ParsedRequest, QueryPlan


def generate_sql(llm: LLMClient, parsed: ParsedRequest, plan: QueryPlan, evidence: Evidence, *,
                 previous_sql: str = "", errors: Sequence[str] = ()) -> tuple[str, CallRecord]:
    """调模型生成一条 SQL

    Args:
        llm: 模型
        parsed: ① 的结果
        plan: ④ 的方案
        evidence: ③ 的结果
        previous_sql: 修正时上一次生成的 SQL
        errors: 修正时上一次的校验错误（「R4：…」）

    Returns:
        (SQL，已用 extract_sql 取出, 调用记录)

    Raises:
        StepError: 模型调用失败
    """
    raise NotImplementedError("由 W1-B 实现")


def extract_sql(text: str) -> str:
    """从模型回复里取出 SQL

    有 ```sql 代码块取第一个；否则取第一个无语言标记的代码块；都没有取全文。去首尾空白和末尾分号。
    """
    raise NotImplementedError("由 W1-B 实现")
