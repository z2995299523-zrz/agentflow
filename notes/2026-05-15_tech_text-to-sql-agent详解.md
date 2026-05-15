# Text-to-SQL Agent 代码详解

> 日期：2026-05-15 | 类别：tech | 源文件：`sql/agent.py`

---

## 一、整体架构

```
用户问题: "销售额最高的5个产品"
    │
    ▼
SQLQueryAgent.query()
    │
    ├─→ 创建 _SQLCaptureCallback (监控探头)
    │
    ├─→ agent.invoke({"input": question})
    │       │
    │       ├─→ LLM 读 Schema → 生成 SQL
    │       ├─→ callback.on_tool_start 捕获 SQL ← 路径1
    │       ├─→ SafeSQLDatabase.run() 安检 → 执行
    │       └─→ LLM 翻译结果 → 自然语言
    │
    ├─→ 从 callback 取 SQL (路径1)
    ├─→ 如为空，从 intermediate_steps 取 (路径2)
    │
    └─→ 返回 {"question", "sql", "answer", "success", "error"}
```

四个核心设计：
1. **SafeSQLDatabase** — 安全层，继承式权限控制
2. **_SQLCaptureCallback** — SQL 捕获，双路径保证
3. **create_sql_agent** — Agent 创建，三步自动化
4. **query() 方法** — 结构化返回，封装不确定性

---

## 二、SafeSQLDatabase — 安全层

```python
class SafeSQLDatabase(SQLDatabase):
    def run(self, command, ..., **kwargs):
        self._check_safe(command)      # ← 先安检
        return super().run(...)         # ← 再放行
```

### 解决什么问题？

LangChain 的 `SQLDatabase` 本身**没有任何权限控制**。把数据库连接给它，它能执行任何 SQL——包括 `DROP TABLE products`。这在生产环境不可接受。

用户的银行数据开发背景最能理解：**生产库的权限分级是命根子**。在大规模数据治理改造中做到零数据事故，靠的就是分层权限。这段代码就是 Agent 的"只读账号"。

### 为什么用继承而不是装饰器？

```python
# 方案A：继承（我们的选择）
class SafeSQLDatabase(SQLDatabase):
    def run(self, ...):
        self._check_safe(command)
        return super().run(...)

# 方案B：装饰器/包装
class DatabaseWrapper:
    def __init__(self, db):
        self.db = db
    def run(self, command):
        check_safe(command)
        return self.db.run(command)
```

选继承的理由：`SQLDatabaseToolkit` 内部会检查 `isinstance(db, SQLDatabase)`。如果用包装器，它不认，工具注册会失败。继承保持了类型兼容性。

### 安全检查的算法细节

```python
command_upper = command.upper().strip()
tokens = set(command_upper.split())   # ← 关键：按空白分词
for kw in FORBIDDEN_KEYWORDS:
    if kw in tokens:                   # ← 集合查找 O(1)
        raise ValueError(...)
```

**为什么不用简单的 `if 'DROP' in command_upper`？**

因为那会误判。假如表里有个字段叫 `drop_reason`（退货原因），简单子串匹配就误杀了。分词后 `DROP` 必须是独立单词才拦截，`DROP_REASON` 不会命中。

### ETL 类比

这就是 **DataStage 的数据源权限控制**。以前连生产库时，调度系统给的账号只有 `SELECT` 权限，这就是同一条防线——只不过现在这条防线在代码层面而非数据库账号层面实现。

### 面试话术

> "在实现 Text-to-SQL 时，我特别关注了安全性。LangChain 原生的 SQLDatabase 没有权限控制，我通过继承它并重写 `run()` 方法，在执行前加了一层安全校验。校验逻辑是分词后集合匹配，避免字段名中含有关键词时的误判。为什么用继承而不是包装器？因为 LangChain 的 SQLDatabaseToolkit 内部有 `isinstance` 检查，包装器会导致工具注册失败。这对应我在银行做数据治理时的权限管控思维——任何数据操作都必须经过安全闸门。"

---

## 三、_SQLCaptureCallback — SQL 捕获

```python
class _SQLCaptureCallback(BaseCallbackHandler):
    sql_queries: list[str] = []

    def on_tool_start(self, serialized, input_str, **kwargs):
        tool_name = serialized.get("name", "")
        if "sql" in tool_name.lower() and "query" in tool_name.lower():
            self.sql_queries.append(input_str)
```

### 解决什么问题？

`create_sql_agent` 执行完后，只返回最终的自然语言回答——**不会告诉你它到底跑了什么 SQL**。但面试展示时需要让面试官看到 "Agent 自动生成了这个 SQL"，这是 Text-to-SQL 的核心卖点。

回调就是 LangChain 的"监控探头"——在每个工具调用之前插入我们的逻辑。

### 底层原理：LangChain Callback 系统

```
Agent 执行流程：
  LLM 生成 Action → [Callback.on_tool_start 触发！] → 执行工具 → [Callback.on_tool_end] → 返回结果
                         ↑
                    我们在这里拦截，记录 SQL
```

`BaseCallbackHandler` 是 LangChain 的事件钩子系统，类似数据库的 Trigger。它提供了一系列钩子：

| 钩子 | 触发时机 | 类比 |
|------|---------|------|
| `on_llm_start` | LLM 被调用前 | SQL 执行前 |
| `on_tool_start` | 工具被调用前 | 存储过程被调用前 |
| `on_chain_start` | Chain 被调用前 | ETL Job 启动前 |

我们只用 `on_tool_start`——当 Agent 决定调用 `sql_db_query` 工具时，`input_str` 就是生成的 SQL。

### 双路径捕获设计（关键坑）

```python
# 路径1：回调捕获
sql = callback.sql_queries[-1] if callback.sql_queries else ""

# 路径2：intermediate_steps 兜底
if not sql:
    for step in result.get("intermediate_steps", []):
        action = step[0]
        if "SELECT" in str(action.tool_input).upper():
            sql = str(action.tool_input)
```

**为什么需要双路径？** DeepSeek 有时不走 `sql_db_query` 工具，而是直接输出答案（不走工具调用路径），回调就不会触发。`intermediate_steps` 是 Agent 执行器中**强制保存**的每一步记录，不走回调也能拿到。双路径 = 回调优先（干净）+ intermediate_steps 兜底（可靠）。

### 面试话术

> "我实现了一个 SQL 捕获的回调机制。LangChain 的 Agent 默认只返回最终答案，不暴露中间生成的 SQL。我通过 BaseCallbackHandler 的 on_tool_start 钩子拦截工具调用，记录生成的 SQL 语句。但实践中发现某些 LLM 的行为不稳定——有时回调触发不了。所以我设计了双路径捕获：回调优先 + Agent 的 intermediate_steps 作为兜底。这样既满足展示需求，又保证了稳健性。"

---

## 四、create_sql_agent — Agent 创建

```python
self.agent = create_sql_agent(
    llm=self.llm,
    toolkit=toolkit,          # ← SQLDatabaseToolkit 提供工具
    verbose=False,
    prefix=prefix,            # ← 系统提示词
    handle_parsing_errors=True,       # ← 输出解析容错
    return_intermediate_steps=True,   # ← SQL 双路径捕获的关键
)
```

### create_sql_agent 底层做了什么？

```
Step 1: SQLDatabaseToolkit 从数据库读取 Schema
         ↓
        products(id, product_name, category, unit_price)
        sales(id, product_id, quantity, sale_date, region)
        customers(id, customer_name, region, level)

Step 2: 把 Schema + 问题 拼成 Prompt，注入到 LLM
         ↓
        "你是一个SQL查询助手。以下是数据库表结构：
         [Schema]
         用户问题：销售额最高的5个产品
         请生成 SQLite 语法的 SELECT 查询..."

Step 3: LLM 生成 SQL → Agent 执行 → 拿到结果 → LLM 翻译成自然语言
```

**类比 ETL 经验：** 这就是 DataStage 的 **Schema 自动发现 + 动态 SQL 生成**。以前写 ETL 时需要先了解源表结构（desc table），然后手写 SQL。Agent 把这个过程自动化了——读 Schema、写 SQL、执行、解释结果，四步合成一步。

### 关键参数解释

| 参数 | 作用 | 为什么这么设 |
|------|------|------------|
| `handle_parsing_errors=True` | LLM 返回格式不对时自动重试 | DeepSeek 有时不按 Agent 的 JSON 格式返回，不加这个直接崩溃 |
| `return_intermediate_steps=True` | 返回每一步的 Action+Observation | SQL 双路径捕获的兜底方案 |
| `verbose=False` | 不打印调试日志 | 生产环境不污染输出 |
| `prefix=中文提示词` | 约束 Agent 行为 | 告诉 Agent 只生成 SELECT、日期格式、诚实回答 |

### 面试话术

> "底层用的是 LangChain 的 create_sql_agent，它本质上是三件事：自动获取数据库 Schema、注入 Prompt、用 Function Calling 让 LLM 生成并执行 SQL。我加了 handle_parsing_errors 来处理 LLM 输出的不稳定性——因为国产模型 DeepSeek 在严格 JSON 格式输出上不如 GPT-4 稳定，需要容错重试机制。prefix 提示词用中文写了行为约束：只允许 SELECT、日期格式规范、空结果诚实告知。"

---

## 五、query() 方法 — 结构化返回

```python
def query(self, question: str) -> dict:
    callback = _SQLCaptureCallback()
    result = self.agent.invoke(
        {"input": question},
        config={"callbacks": [callback]},
    )
    # ... SQL 提取 ...
    return {
        "question": question,
        "sql": "...",
        "answer": "...",
        "success": True/False,
        "error": None/"...",
    }
```

### 返回格式为什么是这五个字段？

```python
{"question", "sql", "answer", "success", "error"}
```

这是为后续的 FastAPI 接口和 Streamlit 前端准备的。前端需要：
- `sql` → 展示在"生成的 SQL"卡片里（让面试官看到）
- `answer` → 展示在聊天框里
- `success/error` → 控制 UI 显示正常结果还是错误提示

### 异常处理原则

```python
except Exception as e:
    return {
        "success": False,
        "error": str(e),
    }
```

不抛异常，而是返回结构化错误。类比 ETL 经验：DataStage Job 失败时不会让整个调度崩掉，而是记录错误日志并继续下一个 Job。

### 面试话术

> "query 方法的设计遵循一个原则：把 LLM 的不确定性封装在一个结构化的返回格式里。不管 Agent 内部成功还是失败，外部拿到的永远是一个包含 question、sql、answer、success、error 五个字段的字典。这样做的好处是后续接 FastAPI 和 Streamlit 时，前端不需要判断异常，只需要看 success 字段。这是我做 ETL 时的习惯——数据管道每一段的输出格式必须稳定，上游的异常不能污染下游。"

---

## 六、四个设计决策总结

| 设计 | 决策 | 面试话术关键词 |
|------|------|--------------|
| SafeSQLDatabase | 继承而非包装 | "类型兼容性、权限分级、零信任原则" |
| 双路径 SQL 捕获 | 回调 + intermediate_steps | "防御性编程、LLM 行为不确定性、稳健性" |
| create_sql_agent | 开箱即用 + 参数调优 | "Schema 自动发现、Function Calling、容错重试" |
| 结构化返回 | 五个字段统一格式 | "封装复杂度、上下游解耦、ETL 管道的稳定性思维" |

---

## 相关笔记

- [[README|笔记索引]]
- 源文件：`sql/agent.py`
- 升级计划：`.hermes/upgrade-plan.md`
