"""
模型调用
- LLMClient：流水线只依赖这个接口。真实环境用 ChatModelClient（OpenAI 兼容接口，默认 DeepSeek，可换千问），测试用 ScriptedLLM
- structured_call：JSON 模式调用 → 解析 → Pydantic 校验 → 代码核对，不合格时把问题告诉模型再试一次，① ④ 用
- text_call：只调一次，⑤ 生成 SQL 用

每次调用记一条 CallRecord：耗时、token、发出去的消息（副本）和回复。
模型调用失败或者输出两次都不合格时抛 StepError，由编排把整轮结果置为 failed 并写明停在哪一步。
"""
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from nl2sql.schemas import CallRecord
from prompts.nl2sql import RETRY_PROMPT

T = TypeVar("T", bound=BaseModel)

PYDANTIC_PROBLEM_CHARS = 500                  # Pydantic 校验错误压成一句话后最多留多少字
_FENCE = re.compile(r"```[\w-]*[ \t]*\n?(.*?)\n?[ \t]*```", re.S)


class LLMError(RuntimeError):
    """模型调用失败：网络、超时、接口报错、空回复"""


@dataclass
class LLMReply:
    """模型的一次回复"""

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_ms: int = 0


class LLMClient(Protocol):
    """模型调用接口"""

    name: str

    def complete(self, messages: Sequence[Mapping[str, str]], *, json_mode: bool = False) -> LLMReply:
        """发一组消息，拿回一条回复。失败时抛 LLMError"""
        ...


class ChatModelClient:
    """OpenAI 兼容接口的模型（默认 DeepSeek，也可以是千问等 config.MODEL_CHOICES 里的），连接参数来自 config.get_llm_kwargs

    json_mode 时要求接口按 JSON 对象输出（response_format=json_object）；token 数取回复的 usage_metadata。
    任何异常都包成 LLMError。
    """

    def __init__(self, *, temperature: float = 0.0, timeout: float | None = None, max_retries: int = 2,
                 model: str | None = None) -> None:
        """
        Args:
            temperature: 温度，流水线一律用 0
            timeout: 单次调用的超时秒数，None 时用 config.LLM_TIMEOUT
            max_retries: 网络错误、限流时接口客户端自己重试的次数
            model: 模型名，None 时用默认模型（config.default_model）

        Raises:
            ValueError: 模型不认识，或者没有配置模型的 Key
        """
        import config  # 延迟导入：测试和元数据页不需要模型配置
        from langchain_openai import ChatOpenAI

        kwargs = config.get_llm_kwargs(temperature, model)
        self.name: str = kwargs["model"]
        self._model = ChatOpenAI(**kwargs, timeout=timeout if timeout is not None else config.LLM_TIMEOUT,
                                 max_retries=max_retries)
        self._json_model = self._model.bind(response_format={"type": "json_object"})

    def complete(self, messages: Sequence[Mapping[str, str]], *, json_mode: bool = False) -> LLMReply:
        """调用一次模型

        Args:
            messages: [{"role": "system" | "user" | "assistant", "content": ...}]
            json_mode: 是否要求输出 JSON 对象

        Returns:
            回复的文本、token 数和耗时

        Raises:
            LLMError: 网络、超时、接口报错，或者回复是空的
        """
        model = self._json_model if json_mode else self._model
        started = time.perf_counter()
        try:
            message = model.invoke([dict(item) for item in messages])
        except Exception as error:  # noqa: BLE001 接口客户端的异常种类很多，一律包成 LLMError
            raise LLMError(f"{type(error).__name__}: {error}") from error
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        content = message.content
        text = content if isinstance(content, str) else "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
        if not text.strip():
            raise LLMError("模型返回了空回复")
        usage = getattr(message, "usage_metadata", None) or {}
        return LLMReply(text=text, input_tokens=int(usage.get("input_tokens", 0) or 0),
                        output_tokens=int(usage.get("output_tokens", 0) or 0), elapsed_ms=elapsed_ms)


ReplyItem = str | Exception | Callable[[list[dict[str, str]]], str]


class ScriptedLLM:
    """假模型：按顺序返回预先写好的回复，测试用

    回复是字符串就原样返回；是异常实例就抛出（不是 LLMError 的包成 LLMError）；
    是函数就用这次的消息调用它，返回值作为回复。回复用完了再调用抛 AssertionError。
    """

    def __init__(self, replies: Sequence[ReplyItem], name: str = "scripted") -> None:
        """
        Args:
            replies: 依次返回的回复
            name: 模型名
        """
        self.name = name
        self._replies: list[ReplyItem] = list(replies)
        self.calls: list[dict] = []           # 每次调用：{"messages": [...副本], "json_mode": bool}

    @property
    def remaining(self) -> int:
        """还剩几条回复没用"""
        return len(self._replies)

    def complete(self, messages: Sequence[Mapping[str, str]], *, json_mode: bool = False) -> LLMReply:
        """记下这次调用，返回下一条预制的回复

        Raises:
            LLMError: 下一条回复是异常
            AssertionError: 回复用完了
        """
        copied = [dict(item) for item in messages]
        self.calls.append({"messages": copied, "json_mode": json_mode})
        if not self._replies:
            raise AssertionError("假模型的回复用完了")
        item = self._replies.pop(0)
        if isinstance(item, LLMError):
            raise item
        if isinstance(item, Exception):
            raise LLMError(f"{type(item).__name__}: {item}") from item
        text = item(copied) if callable(item) else item
        return LLMReply(text=text)


class StepError(Exception):
    """某一步做不下去：模型调用失败，或者输出两次都不合格"""

    def __init__(self, step: str, message: str, calls: Sequence[CallRecord] = ()) -> None:
        """
        Args:
            step: 停在哪一步（STEPS 之一）
            message: 原因，给用户看
            calls: 这一步已经发生的模型调用
        """
        super().__init__(f"{step}：{message}")
        self.step = step
        self.message = message
        self.calls: list[CallRecord] = list(calls)


def parse_json_reply(text: str) -> Any:
    """解析模型回复里的 JSON：去掉首尾空白和 ```json 围栏后 json.loads

    Raises:
        json.JSONDecodeError: 不是合法的 JSON
    """
    body = text.strip()
    match = _FENCE.fullmatch(body)
    if match is not None:
        body = match.group(1).strip()
    return json.loads(body)


def _pydantic_problem(error: ValidationError) -> str:
    """Pydantic 校验错误压成「字段路径: 原因」，多条用分号连起来，最多 PYDANTIC_PROBLEM_CHARS 字"""
    items = []
    for detail in error.errors():
        path = ".".join(str(part) for part in detail["loc"]) or "（顶层）"
        items.append(f"{path}: {detail['msg']}")
    text = "；".join(items)
    return text if len(text) <= PYDANTIC_PROBLEM_CHARS else text[:PYDANTIC_PROBLEM_CHARS - 1] + "…"


def _review(text: str, model: type[T], check: Callable[[T], list[str]] | None) -> tuple[T | None, list[str]]:
    """解析一条回复，返回结构化结果和问题；有问题时结果为 None"""
    try:
        data = parse_json_reply(text)
    except json.JSONDecodeError as error:
        return None, [f"回复不是合法的 JSON：{error.msg}（第 {error.lineno} 行第 {error.colno} 列）"]
    try:
        value = model.model_validate(data)
    except ValidationError as error:
        return None, [f"JSON 的结构不对：{_pydantic_problem(error)}"]
    problems = list(check(value)) if check is not None else []
    return (None, problems) if problems else (value, [])


def structured_call(llm: LLMClient, step: str, messages: list[dict[str, str]], model: type[T],
                    check: Callable[[T], list[str]] | None = None, *,
                    max_attempts: int = 2) -> tuple[T, list[CallRecord]]:
    """用 JSON 模式调用模型，拿回一个合格的结构化结果

    JSON 解析错误、Pydantic 校验错误、check 返回的问题都算不合格。不合格时在消息后面追加原回复和
    RETRY_PROMPT（带着问题）再试，最多 max_attempts 次。

    Args:
        llm: 模型
        step: 步骤名，写进调用记录和 StepError
        messages: 要发的消息，不会被修改
        model: 结果的 Pydantic 类型
        check: 代码核对，返回问题列表，空列表表示合格
        max_attempts: 最多调用几次

    Returns:
        (合格的结果, 每次调用一条的记录)

    Raises:
        StepError: 模型调用失败，或者每次的输出都不合格；带着已经发生的调用记录
    """
    conversation = [dict(item) for item in messages]
    records: list[CallRecord] = []
    problems: list[str] = []
    for attempt in range(1, max_attempts + 1):
        sent = [dict(item) for item in conversation]
        started = time.perf_counter()
        try:
            reply = llm.complete(sent, json_mode=True)
        except LLMError as error:
            records.append(CallRecord(step=step, attempt=attempt, elapsed_ms=int((time.perf_counter() - started) * 1000),
                                      ok=False, error=f"模型调用失败：{error}", messages=sent))
            raise StepError(step, f"模型调用失败：{error}", records) from error
        elapsed_ms = reply.elapsed_ms or int((time.perf_counter() - started) * 1000)
        value, problems = _review(reply.text, model, check)
        records.append(CallRecord(step=step, attempt=attempt, elapsed_ms=elapsed_ms, input_tokens=reply.input_tokens,
                                  output_tokens=reply.output_tokens, ok=not problems, error="；".join(problems),
                                  messages=sent, reply=reply.text))
        if not problems:
            return value, records
        conversation += [
            {"role": "assistant", "content": reply.text},
            {"role": "user", "content": RETRY_PROMPT.format(problems="\n".join(f"- {p}" for p in problems))},
        ]
    times = "两次" if max_attempts == 2 else f"{max_attempts} 次"
    raise StepError(step, f"输出{times}都不合格：{'；'.join(problems)}", records)


def text_call(llm: LLMClient, step: str, messages: list[dict[str, str]]) -> tuple[str, CallRecord]:
    """普通文本模式调用一次模型

    Args:
        llm: 模型
        step: 步骤名
        messages: 要发的消息，不会被修改

    Returns:
        (回复文本, 调用记录)

    Raises:
        StepError: 模型调用失败
    """
    sent = [dict(item) for item in messages]
    started = time.perf_counter()
    try:
        reply = llm.complete(sent, json_mode=False)
    except LLMError as error:
        record = CallRecord(step=step, attempt=1, elapsed_ms=int((time.perf_counter() - started) * 1000), ok=False,
                            error=f"模型调用失败：{error}", messages=sent)
        raise StepError(step, f"模型调用失败：{error}", [record]) from error
    record = CallRecord(step=step, attempt=1, elapsed_ms=reply.elapsed_ms or int((time.perf_counter() - started) * 1000),
                        input_tokens=reply.input_tokens, output_tokens=reply.output_tokens, ok=True,
                        messages=sent, reply=reply.text)
    return reply.text, record
