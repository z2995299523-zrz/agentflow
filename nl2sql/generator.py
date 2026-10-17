"""
⑤ 生成 SQL
按查询方案写一条 SQL。修正轮带上一次的 SQL 和校验错误（只带错误，不带警告）。
"""
import re
from collections.abc import Sequence

from nl2sql.llm import LLMClient, text_call
from nl2sql.schemas import CallRecord, Evidence, ParsedRequest, QueryPlan
from prompts.nl2sql import generator_messages

# 代码块：```语言标记 换行 内容 ```。语言标记可以为空
_FENCED = re.compile(r"```([\w-]*)[ \t]*\n(.*?)```", re.S)
_OPEN_FENCE = re.compile(r"```([\w-]*)[ \t]*\n(.*)\Z", re.S)        # 没有结束围栏（输出被截断）
SQL_LABELS = frozenset({"sql", "postgresql", "postgres", "pgsql", "psql"})


def generate_sql(llm: LLMClient, parsed: ParsedRequest, plan: QueryPlan, evidence: Evidence, *,
                 previous_sql: str = "", errors: Sequence[str] = ()) -> tuple[str, CallRecord]:
    """调模型生成一条 SQL

    回复里取不出 SQL 不算调用失败：返回空串，交给静态校验报错、进入修正回路。

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
    messages = generator_messages(parsed, plan, evidence, previous_sql=previous_sql, errors=errors)
    text, record = text_call(llm, "generate", messages)
    return extract_sql(text), record


def _tidy(sql: str) -> str:
    """去首尾空白和末尾的分号"""
    return re.sub(r"[;\s]+\Z", "", sql.strip())


def extract_sql(text: str) -> str:
    """从模型回复里取出 SQL

    有 ```sql 代码块取第一个；否则取第一个无语言标记的代码块；都没有取全文。去首尾空白和末尾分号。
    sql 代码块的语言标记不分大小写，postgresql 这类写法也算；只有开头的围栏、没有结束围栏时，取围栏后面的全部。
    """
    blocks = [(label.lower(), body) for label, body in _FENCED.findall(text)]
    if not blocks:
        opened = _OPEN_FENCE.search(text)
        if opened is not None:
            blocks = [(opened.group(1).lower(), opened.group(2))]
    for wanted in (SQL_LABELS, frozenset({""})):
        for label, body in blocks:
            if label in wanted:
                return _tidy(body)
    return _tidy(text)
