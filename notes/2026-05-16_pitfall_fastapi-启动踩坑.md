# 2026-05-16 踩坑记录

> 2026-05-16 | pitfall | FastAPI 启动 + 代理配置 + DeepSeek 输出格式

---

## 坑1：代理端口记错

**现象：** `HTTPS_PROXY=http://127.0.0.1:7897` 设置后仍 Connection error

**排查：** `curl -x http://127.0.0.1:7897 https://api.deepseek.com` 超时，`curl -x http://127.0.0.1:7890` 通

**根因：** 系统 Clash 代理实际端口是 7890，Hermes memory 里误记成 7897

**修复：** 全局替换代理端口为 7890，更新 memory

**教训：** 不要信任记忆中的端口号，用 `curl -x` 实测验证

---

## 坑2：httpx/openai SDK 不自动继承 HTTPS_PROXY

**现象：** shell 里 `export HTTPS_PROXY=...` 后，Python 的 `ChatOpenAI` 调用仍 Connection error

**根因：** httpx 和 openai SDK 不会自动读取 `HTTPS_PROXY` 环境变量来设置代理

**修复：** 在 `config.py` 的 `get_llm_kwargs()` 里显式传入：
```python
kwargs["http_client"] = httpx.Client(proxy="http://127.0.0.1:7890")
```

**教训：** Python 的 HTTP 库（httpx/requests）对 `HTTPS_PROXY` 的支持不一致。生产环境永远显式配置，不要依赖环境变量。

---

## 坑3：国内/国外 API 混用时代理一刀切

**现象：** 配置代理后关掉 Clash，DeepSeek 也连不上了

**根因：** `config.py` 里所有 provider 都强制走代理，但 DeepSeek（api.deepseek.com）国内直连即可

**修复：** 按 provider 区分——国内直连，国外走代理：
```python
_NEEDS_PROXY_PROVIDERS = frozenset({"openai"})
if provider in _NEEDS_PROXY_PROVIDERS and PROXY_URL:
    kwargs["http_client"] = httpx.Client(proxy=PROXY_URL)
```

**教训：** 代理配置要按 API 域名区分。国内模型（DeepSeek/通义/文心）不走代理，国外模型（OpenAI/Anthropic）才走。

---

## 坑4：uvicorn 端口被旧进程占用，静默失败

**现象：** 启动 `uvicorn` 后 `curl localhost:8000/health` 返回正常，但接口行为不对

**根因：** 之前杀 uvicorn 进程不彻底，旧进程仍占用 8000 端口。新启动的 uvicorn 因端口冲突退出（exit code 1），但旧进程仍在运行。curl 实际打到了旧进程上，旧进程用的是老配置。

**诊断方法：**
```bash
netstat -ano | grep ':8000'          # 查看谁在监听
taskkill //PID <pid> //F             # 杀掉旧进程
```
确认端口完全释放后再启动。

**教训：** Windows 上进程退出不一定释放端口（TIME_WAIT）。每次重启服务前先 `netstat` 确认。

---

## 坑5：DeepSeek 不遵守 Agent 输出格式

**现象：** LLM 查询成功返回数据，但 LangChain 抛出 `OUTPUT_PARSING_FAILURE`：
> Could not parse LLM output: `产品表中共有 12 条记录。`

**根因：** LangChain SQL Agent 要求 LLM 按 ReAct 格式输出（Thought → Action → Observation → Final Answer），DeepSeek 有时跳过格式直接输出自然语言。`handle_parsing_errors=True` 只能重试，不能改变 LLM 的行为模式。

**修复：** 在 Prompt 末尾加 `suffix`，用正确/错误示例强制 LLM 遵守格式：
```
SQL_AGENT_SUFFIX = """
重要格式要求（必须遵守）：
...
正确格式：Final Answer: 客户表中共有 10 条记录...
错误格式：根据查询结果，客户表中有 10 条记录...（缺少 Final Answer: 开头）
"""
```

**教训：** Prompt Engineering 的核心不是"告诉 LLM 做什么"，而是"告诉 LLM 怎么输出"。格式约束要放在 Prompt 末尾（LLM 最难忽略的位置），用正反例对比最有效。

---

## 坑6：Pydantic 和 get_llm_kwargs() 的 temperature 冲突

**现象：** `TypeError: ChatOpenAI() got multiple values for keyword argument 'temperature'`

**根因：** `get_llm_kwargs()` 已经设置了 `temperature=0.7`，调用方又传了 `temperature=0`

**修复：** 先 pop 掉默认值再传自定义：
```python
kwargs = get_llm_kwargs()
kwargs.pop("temperature", None)
llm = ChatOpenAI(**kwargs, temperature=0)
```

**教训：** 工厂函数返回的 kwargs 是"带默认值的"，调用方想覆盖时要先 pop 再传，不能直接叠加。

---

## 相关笔记

- [[2026-05-16_tech_fastapi详解]]
- [[2026-05-16_tech_prompt-engineering详解]]
- [[2026-05-16_discuss_async-vs-thread]]
