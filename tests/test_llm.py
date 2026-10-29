"""模型调用层：假模型、结构化调用的重试、文本调用、OpenAI 兼容接口的包装，以及结果的对外字典"""
import json
import sys
import types

import pytest
from pydantic import BaseModel

from nl2sql.llm import (
    ChatModelClient,
    LLMError,
    LLMReply,
    ScriptedLLM,
    StepError,
    parse_json_reply,
    structured_call,
    text_call,
)
from nl2sql.schemas import CallRecord, QueryResult
from prompts.nl2sql import RETRY_PROMPT

MESSAGES = [{"role": "system", "content": "只输出 json"}, {"role": "user", "content": "查贷款余额"}]
RETRY_HEAD = RETRY_PROMPT.split("{problems}")[0]


class Answer(BaseModel):
    name: str
    count: int


def no_negative(answer: Answer) -> list[str]:
    return ["count 不能是负数"] if answer.count < 0 else []


# ── 假模型 ────────────────────────────────────

def test_scripted_llm_returns_replies_in_order_and_records_calls():
    llm = ScriptedLLM(["第一", "第二"])

    first = llm.complete(MESSAGES, json_mode=True)
    second = llm.complete(MESSAGES)

    assert (first.text, second.text) == ("第一", "第二")
    assert [call["json_mode"] for call in llm.calls] == [True, False]
    assert llm.calls[0]["messages"] == MESSAGES
    assert llm.calls[0]["messages"] is not MESSAGES           # 存的是副本
    assert llm.remaining == 0


def test_scripted_llm_raises_exceptions_as_llm_errors():
    llm = ScriptedLLM([TimeoutError("连接超时"), LLMError("接口报错")])

    with pytest.raises(LLMError, match="TimeoutError: 连接超时"):
        llm.complete(MESSAGES)
    with pytest.raises(LLMError, match="接口报错"):
        llm.complete(MESSAGES)


def test_scripted_llm_calls_functions_with_the_messages():
    llm = ScriptedLLM([lambda messages: f"收到 {len(messages)} 条"])

    assert llm.complete(MESSAGES).text == "收到 2 条"


def test_scripted_llm_complains_when_replies_run_out():
    llm = ScriptedLLM([])

    with pytest.raises(AssertionError, match="假模型的回复用完了"):
        llm.complete(MESSAGES)
    assert len(llm.calls) == 1                                  # 用完了也记下这次调用，方便看发了什么


# ── 解析 JSON ─────────────────────────────────

@pytest.mark.parametrize("text", [
    '{"name": "a", "count": 1}',
    '  {"name": "a", "count": 1}\n',
    '```json\n{"name": "a", "count": 1}\n```',
    '```\n{"name": "a", "count": 1}\n```',
    '\n```JSON\n{"name": "a", "count": 1}```  ',
])
def test_parse_json_reply_strips_fences_and_whitespace(text):
    assert parse_json_reply(text) == {"name": "a", "count": 1}


def test_parse_json_reply_raises_on_bad_json():
    with pytest.raises(json.JSONDecodeError):
        parse_json_reply("```json\n{name: a}\n```")


# ── 结构化调用 ────────────────────────────────

def test_structured_call_passes_on_the_first_try():
    llm = ScriptedLLM(['{"name": "贷款余额", "count": 2}'])

    answer, records = structured_call(llm, "parse", MESSAGES, Answer, no_negative)

    assert answer == Answer(name="贷款余额", count=2)
    assert [(r.step, r.attempt, r.ok) for r in records] == [("parse", 1, True)]
    assert records[0].messages == MESSAGES
    assert records[0].reply == '{"name": "贷款余额", "count": 2}'
    assert llm.calls[0]["json_mode"] is True


def test_structured_call_retries_after_bad_json_with_the_problem():
    llm = ScriptedLLM(['{"name": "贷款余额", "count": ', '{"name": "贷款余额", "count": 2}'])

    answer, records = structured_call(llm, "parse", MESSAGES, Answer)

    assert answer.count == 2
    assert [(r.attempt, r.ok) for r in records] == [(1, False), (2, True)]
    assert "不是合法的 JSON" in records[0].error
    retry = records[1].messages
    assert retry[:2] == MESSAGES
    assert retry[2] == {"role": "assistant", "content": '{"name": "贷款余额", "count": '}
    assert retry[3]["role"] == "user" and retry[3]["content"].startswith(RETRY_HEAD)
    assert "不是合法的 JSON" in retry[3]["content"]
    assert MESSAGES == [{"role": "system", "content": "只输出 json"}, {"role": "user", "content": "查贷款余额"}]


def test_structured_call_reports_pydantic_errors_as_field_paths():
    llm = ScriptedLLM(['{"name": "贷款余额", "count": "很多"}', '{"name": "贷款余额", "count": 3}'])

    _, records = structured_call(llm, "plan", MESSAGES, Answer)

    assert records[0].error.startswith("JSON 的结构不对：count: ")


def test_structured_call_retries_when_the_check_finds_problems():
    llm = ScriptedLLM(['{"name": "a", "count": -1}', '{"name": "a", "count": 1}'])

    answer, records = structured_call(llm, "plan", MESSAGES, Answer, no_negative)

    assert answer.count == 1
    assert records[0].error == "count 不能是负数"
    assert "- count 不能是负数" in records[1].messages[-1]["content"]


def test_structured_call_gives_up_after_two_bad_replies():
    llm = ScriptedLLM(['{"name": "a", "count": -1}', '{"name": "a", "count": -2}'])

    with pytest.raises(StepError) as caught:
        structured_call(llm, "plan", MESSAGES, Answer, no_negative)

    assert caught.value.step == "plan"
    assert caught.value.message == "输出两次都不合格：count 不能是负数"
    assert [(r.attempt, r.ok) for r in caught.value.calls] == [(1, False), (2, False)]


def test_structured_call_turns_llm_errors_into_step_errors_at_once():
    llm = ScriptedLLM([TimeoutError("连接超时"), '{"name": "a", "count": 1}'])

    with pytest.raises(StepError) as caught:
        structured_call(llm, "parse", MESSAGES, Answer)

    assert caught.value.step == "parse"
    assert caught.value.message.startswith("模型调用失败：")
    assert [(r.attempt, r.ok) for r in caught.value.calls] == [(1, False)]
    assert llm.remaining == 1                                   # 不再重试


def test_structured_call_keeps_the_records_of_earlier_attempts_on_llm_error():
    llm = ScriptedLLM(['不是 JSON', LLMError("接口报错")])

    with pytest.raises(StepError) as caught:
        structured_call(llm, "parse", MESSAGES, Answer)

    assert [(r.attempt, r.ok) for r in caught.value.calls] == [(1, False), (2, False)]


# ── 文本调用 ──────────────────────────────────

def test_text_call_returns_the_reply_and_one_record():
    llm = ScriptedLLM(["```sql\nSELECT 1\n```"])

    text, record = text_call(llm, "generate", MESSAGES)

    assert text == "```sql\nSELECT 1\n```"
    assert (record.step, record.attempt, record.ok, record.reply) == ("generate", 1, True, text)
    assert llm.calls[0]["json_mode"] is False


def test_text_call_raises_step_error_on_llm_error():
    with pytest.raises(StepError) as caught:
        text_call(ScriptedLLM([ConnectionError("断网")]), "generate", MESSAGES)

    assert caught.value.step == "generate"
    assert [r.ok for r in caught.value.calls] == [False]


# ── OpenAI 兼容接口的包装 ─────────────────────
# 不联网，也不导入真的 langchain_openai：它的 azure 模块会连带导入 transformers 和 torch，冷启动要几十秒。
# 换成假的模块，构造参数照记，模型对象换成能指定回复的假对象

class FakeMessage:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage_metadata = usage


class FakeChatOpenAI:
    def __init__(self, outcome=None, **kwargs):
        self.kwargs = kwargs
        self.bound = {}
        self.outcome = outcome
        self.received = None

    def bind(self, **kwargs):
        bound = FakeChatOpenAI(**self.kwargs)
        bound.bound = kwargs
        return bound

    def invoke(self, messages):
        self.received = messages
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def fake_openai(monkeypatch):
    seen = {}

    def kwargs(temperature=None, model=None):
        seen.update(temperature=temperature, model=model)
        return {"model": model or "fake-chat", "api_key": "sk-test", "base_url": "http://127.0.0.1:9",
                "temperature": temperature}

    monkeypatch.setattr("config.get_llm_kwargs", kwargs)
    monkeypatch.setitem(sys.modules, "langchain_openai", types.SimpleNamespace(ChatOpenAI=FakeChatOpenAI))
    return seen


def test_chat_client_builds_the_model_with_temperature_zero_and_json_mode(fake_openai):
    client = ChatModelClient(timeout=5, max_retries=1)

    assert client.name == "fake-chat"
    assert fake_openai["temperature"] == 0.0
    assert client._model.kwargs == {"model": "fake-chat", "api_key": "sk-test", "base_url": "http://127.0.0.1:9",
                                    "temperature": 0.0, "timeout": 5, "max_retries": 1}
    assert client._json_model.bound == {"response_format": {"type": "json_object"}}


def test_chat_client_defaults_the_timeout_from_config(fake_openai, monkeypatch):
    monkeypatch.setattr("config.LLM_TIMEOUT", 77.0)

    assert ChatModelClient()._model.kwargs["timeout"] == 77.0


def test_chat_client_asks_config_for_the_chosen_model(fake_openai):
    assert ChatModelClient().name == "fake-chat" and fake_openai["model"] is None      # 不指定：默认模型

    client = ChatModelClient(model="qwen3.8-flash")

    assert fake_openai["model"] == "qwen3.8-flash"
    assert client.name == "qwen3.8-flash" and client._model.kwargs["model"] == "qwen3.8-flash"


def test_chat_client_reads_text_and_tokens(fake_openai):
    client = ChatModelClient()
    client._json_model = FakeChatOpenAI(FakeMessage('{"a": 1}', {"input_tokens": 120, "output_tokens": 8}))

    reply = client.complete(MESSAGES, json_mode=True)

    assert (reply.text, reply.input_tokens, reply.output_tokens) == ('{"a": 1}', 120, 8)
    assert client._json_model.received == MESSAGES


def test_chat_client_wraps_errors_and_empty_replies(fake_openai):
    client = ChatModelClient()
    client._model = FakeChatOpenAI(RuntimeError("401 Unauthorized"))
    with pytest.raises(LLMError, match="RuntimeError: 401 Unauthorized"):
        client.complete(MESSAGES)

    client._model = FakeChatOpenAI(FakeMessage("   "))
    with pytest.raises(LLMError, match="空回复"):
        client.complete(MESSAGES)


def test_deepseek_defaults_to_flash_with_thinking_off(monkeypatch):
    import config

    monkeypatch.setattr(config, "LLM_PROVIDER", "deepseek")
    monkeypatch.setattr(config, "LLM_MODEL", "")
    monkeypatch.setattr(config, "LLM_API_BASE", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    kwargs = config.get_llm_kwargs(0.0)

    assert kwargs["model"] == "deepseek-flash"
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert kwargs["temperature"] == 0.0


def test_qwen_on_dashscope_turns_thinking_off_and_openai_sends_no_switch(monkeypatch):
    import config

    monkeypatch.setattr(config, "LLM_PROVIDER", "dashscope")
    monkeypatch.setattr(config, "LLM_MODEL", "")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    assert config.get_llm_kwargs(0.0)["extra_body"] == {"enable_thinking": False}

    monkeypatch.setattr(config, "LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert "extra_body" not in config.get_llm_kwargs(0.0)


@pytest.fixture
def deepseek_default(monkeypatch):
    """默认模型是 DeepSeek，三家的 Key 都配了；LLM_API_BASE 指向一个代理地址"""
    import config

    monkeypatch.setattr(config, "LLM_PROVIDER", "deepseek")
    monkeypatch.setattr(config, "LLM_MODEL", "deepseek-flash")
    monkeypatch.setattr(config, "LLM_API_BASE", "http://proxy.local/v1")
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dashscope")
    monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
    return config


def test_choosing_qwen_uses_the_dashscope_key_and_address_with_thinking_off(deepseek_default):
    config = deepseek_default

    kwargs = config.get_llm_kwargs(0.0, "qwen3.8-flash")

    assert kwargs == {"model": "qwen3.8-flash", "api_key": "sk-dashscope", "temperature": 0.0,
                      "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",   # LLM_API_BASE 只给 DeepSeek 用
                      "extra_body": {"enable_thinking": False}}


def test_choosing_mimo_uses_the_mimo_key_and_turns_thinking_off_like_deepseek(deepseek_default):
    config = deepseek_default

    kwargs = config.get_llm_kwargs(0.0, "mimo-v2.6-flash")

    assert kwargs == {"model": "mimo-v2.6-flash", "api_key": "sk-mimo", "temperature": 0.0,
                      "base_url": "https://api.xiaomimimo.com/v1",
                      "extra_body": {"thinking": {"type": "disabled"}}}                 # v2.6 默认开着思考


def test_naming_the_default_model_is_the_same_as_naming_none(deepseek_default):
    config = deepseek_default

    kwargs = config.get_llm_kwargs(0.0)

    assert config.get_llm_kwargs(0.0, "deepseek-flash") == kwargs
    assert kwargs == {"model": "deepseek-flash", "api_key": "sk-deepseek", "temperature": 0.0,
                      "base_url": "http://proxy.local/v1", "extra_body": {"thinking": {"type": "disabled"}}}


def test_unknown_models_and_missing_keys_are_refused(deepseek_default, monkeypatch):
    config = deepseek_default

    with pytest.raises(ValueError, match="不认识的模型：gpt-9"):
        config.get_llm_kwargs(0.0, "gpt-9")
    monkeypatch.delenv("DASHSCOPE_API_KEY")
    monkeypatch.setattr(config, "LLM_API_KEY", "sk-generic")       # LLM_API_KEY 只给 LLM_PROVIDER 兜底
    with pytest.raises(ValueError, match="dashscope 的 API Key"):
        config.get_llm_kwargs(0.0, "qwen3.8-flash")


def test_available_models_lists_the_default_first_and_only_those_with_a_key(deepseek_default, monkeypatch):
    config = deepseek_default

    assert [(item.name, item.label) for item in config.available_models()] == [
        ("deepseek-flash", "DeepSeek Flash"), ("qwen3.8-flash", "千问 qwen3.8-flash"),
        ("mimo-v2.6-flash", "小米 mimo-v2.6-flash")]

    monkeypatch.delenv("DASHSCOPE_API_KEY")
    monkeypatch.delenv("MIMO_API_KEY")
    assert [item.name for item in config.available_models()] == ["deepseek-flash"]

    monkeypatch.setattr(config, "LLM_PROVIDER", "dashscope")       # 默认换成千问：千问排第一，DeepSeek 照样能选
    monkeypatch.setattr(config, "LLM_MODEL", "qwen3.8-flash")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dashscope")
    assert [item.name for item in config.available_models()] == ["qwen3.8-flash", "deepseek-flash"]
    assert config.default_model() == "qwen3.8-flash"


# ── 结果的对外字典 ────────────────────────────

def test_public_drops_prompts_and_replies():
    record = CallRecord(step="parse", attempt=1, elapsed_ms=12, input_tokens=100, output_tokens=20, ok=True,
                        messages=[{"role": "user", "content": "机密提示词"}], reply="机密回复")
    result = QueryResult(status="ok", question="查贷款余额", sql="SELECT 1", calls=[record, record])

    public = result.public()

    assert result.llm_calls == 2
    assert public["calls"] == [{"step": "parse", "attempt": 1, "elapsed_ms": 12, "input_tokens": 100,
                                "output_tokens": 20, "ok": True, "error": ""}] * 2
    assert public["sql"] == "SELECT 1"
    assert result.calls[0].messages                              # 原对象不受影响
    assert "机密" not in json.dumps(public, ensure_ascii=False)


def test_llm_reply_defaults():
    assert LLMReply(text="x") == LLMReply(text="x", input_tokens=0, output_tokens=0, elapsed_ms=0)
