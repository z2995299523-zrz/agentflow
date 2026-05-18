# SQL Agent agent_scratchpad 错误修复 — LangChain 1.2.13 兼容问题

> 日期: 2026-05-18 | 类别: pitfall | 模块: sql/agent.py

---

## 问题现象

Streamlit 中查询"销售额最高的5个产品是什么"，SQL Agent 报错：

```
Prompt missing required variables: {'agent_scratchpad'}
```

SQL 查询完全失败，返回空结果。

---

## 根因分析

### 调用链路

```
create_sql_agent(llm, toolkit, prefix=..., suffix=...)
  → agent_type 默认 = ZERO_SHOT_REACT_DESCRIPTION
  → 构建 prompt 模板: "\n\n".join([prefix, "{tools}", format_instructions, suffix])
  → PromptTemplate.from_template(template)
  → create_react_agent(llm, tools, prompt)
  → ❌ 检查 prompt.input_variables 缺少 agent_scratchpad → 抛异常
```

### 为什么缺少 agent_scratchpad

LangChain 1.2.13 的 `create_react_agent` 强制要求 prompt 模板必须包含 4 个变量：

```python
required_vars = {"agent_scratchpad", "tools", "tool_names", "input"}
```

但 `create_sql_agent` 构建的模板是：

```
{prefix 内容}
{tools}
{format_instructions}
{suffix 内容}
```

`format_instructions` 和 `suffix` 来自我们的自定义 `SQL_AGENT_SUFFIX`，其中不包含 `{agent_scratchpad}` 占位符。模板解析后只有 `tools` / `input` 等变量，缺少 `agent_scratchpad`。

**本质：** `create_sql_agent` 在 LangChain 旧版（0.x）中使用不同的 agent 类型（`ZeroShotAgent`），那个版本不需要显式的 `agent_scratchpad` 变量。1.x 重构后用了 `create_react_agent`，校验变严格了。

---

## 修复方案

### 方案对比

| 方案 | 优点 | 缺点 |
|------|------|------|
| 在 suffix 中手动添加 `{agent_scratchpad}` | 改动最小 | 破坏 suffix 语义，suffix 不应该包含内部占位符 |
| 用 `prompt=` 参数传入完整模板 | 完全控制 | 需要手动构建 React prompt，复杂 |
| **改用 `agent_type="tool-calling"`** | 内置处理 agent_scratchpad，DeepSeek 支持 | suffix 参数不再适用（改为 AIMessage 注入） |

### 最终方案：`agent_type="tool-calling"`

```python
self.agent = create_sql_agent(
    llm=self.llm,
    toolkit=toolkit,
    agent_type="tool-calling",        # ← 关键改动
    prefix=SQL_AGENT_PREFIX,
    # suffix 移除 — tool-calling 模式用 AIMessage 处理
    handle_parsing_errors=True,
    return_intermediate_steps=True,
    max_iterations=10,
)
```

**原理：** `tool-calling` 模式内部用 `ChatPromptTemplate.from_messages()` 构建 prompt，自动插入 `MessagesPlaceholder(variable_name="agent_scratchpad")`——这个占位符被 LLM 原生 tool calling 机制正确处理，不需要我们在 prompt 模板里手动写。

**DeepSeek 兼容性：** DeepSeek API 支持 OpenAI 兼容的 tool calling（`tools` 参数 + `tool_calls` 响应），所以 `agent_type="tool-calling"` 完全可用。

---

## 连带修复：SQL 提取格式变化

切换到 tool-calling 模式后，`on_tool_start` 回调收到的 `input_str` 格式变了：

```
旧版 (React): "SELECT COUNT(*) FROM products"
新版 (tool-calling): "{'query': 'SELECT COUNT(*) FROM products'}"
```

注意单引号——这是 Python dict 的 `repr()` 格式，**不是 JSON**（JSON 必须双引号）。

### 错误做法

```python
json.loads("{'query': 'SELECT ...'}")  # ❌ JSONDecodeError: 单引号不是合法 JSON
```

### 正确做法

```python
import ast
ast.literal_eval("{'query': 'SELECT ...'}")  # ✅ 返回 {'query': 'SELECT ...'}
```

`ast.literal_eval()` 是 Python 的安全求值器，只解析字面量（dict/list/str/int），不会执行任意代码，比 `eval()` 安全。

### 回调改造

```python
def on_tool_start(self, serialized, input_str, **kwargs):
    if serialized.get("name") == "sql_db_query":  # 精确匹配，不误匹配 checker/schema
        import ast
        try:
            parsed = ast.literal_eval(input_str)
            sql = parsed.get("query", "") if isinstance(parsed, dict) else input_str
        except (ValueError, SyntaxError):
            sql = input_str
        self.sql_queries.append(sql)
```

**为什么用 `==` 而不是 `"sql" in name`：** tool-calling 模式下有 4 个 SQL 相关工具：
- `sql_db_list_tables` — 列出表名
- `sql_db_schema` — 获取表结构
- `sql_db_query_checker` — 检查 SQL 语法
- `sql_db_query` — 真正执行查询 ← **只关心这个**

用模糊匹配会误捕获 checker 的输入（也是 SQL 查询语句），导致展示错误的 SQL。

---

## 验证结果

```
查询: "销售额最高的3个产品"
sql: SELECT p.product_name, SUM(s.quantity * p.unit_price) AS total_sales_amount
     FROM sales s JOIN products p ON s.product_id
     GROUP BY p.product_name ORDER BY total_sales_amount DESC LIMIT 3
answer: 销售额最高的3个产品如下：
        | 产品名称 | 总销售额 |
        | MacBook Pro 14 | 974,935.0 |
        | iPad Air | 139,972.0 |
        | iPhone 15 | 118,983.0 |
```

---

## 面试考点

**问：** 你在项目中遇到过 LangChain 版本兼容问题吗？怎么解决的？

**答：** "AgentFlow 从 LangChain 0.x 升级到 1.2.13 后，`create_sql_agent` 报 `agent_scratchpad` 缺失。原因是 1.x 重构了 agent 底层，`create_react_agent` 强制要求 prompt 模板包含内部工作变量，而 `create_sql_agent` 的自定义 prefix/suffix 破坏了模板结构。我的解决方案是把 agent 类型从默认的 `zero-shot-react-description` 切换到 `tool-calling`——这个模式用 `MessagesPlaceholder` 自动处理 `agent_scratchpad`，DeepSeek 的 OpenAI 兼容接口完全支持 tool calling。同时修复了 SQL 提取逻辑——tool-calling 模式下的 tool_input 是 Python dict repr 而非纯字符串，需要用 `ast.literal_eval()` 解析。"

---

## 相关笔记

- [[2026-05-15_tech_text-to-sql-agent详解|Text-to-SQL Agent 代码详解]]
- [[2026-05-16_pitfall_fastapi-启动踩坑|FastAPI 启动踩坑]]
- [[2026-05-18_tech_streamlit-webui详解|Streamlit WebUI 详解]]
