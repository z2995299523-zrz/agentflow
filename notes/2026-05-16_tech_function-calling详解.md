# Function Calling 工具封装详解

> 2026-05-16 | tech | Hermes Agent 讲解

---

## 一、问题起源：LLM 只能"说"，不能"做"

2023年的 ChatGPT 发布时，模型只能根据训练数据生成文本。如果你问它"数据库里有多少个产品？"，它只能瞎编，因为它根本看不到你的数据库。

就像你有一个只会背书的实习生——他能把教科书倒背如流，但你让他查一下今天的销售数据，他做不到，因为他没有"查数据库"这个能力。

---

## 二、底层原理

Function Calling 让 LLM 不仅能"说话"，还能"动手"——调用外部函数来获取实时数据，然后基于真实数据回答。

### 三步流程

```
用户: "销售额最高的3个产品？"
  ↓
① LLM 看到问题 + 可用工具列表（含 sql_query 的 JSON Schema）
  ↓
② LLM 决定: 需要调用 sql_query(question="销售额最高的3个产品？")
  返回的不是文本，而是一个 tool_call 对象
  ↓
③ 程序执行 sql_query → 拿到真实数据库结果 → 追加到对话 → LLM 用自然语言总结
```

### 三层架构（面试考点）

| 层 | 职责 | 代码体现 |
|----|------|---------|
| Tool Definition | 定义工具的接口契约（JSON Schema） | `@tool(description=...)` |
| Tool Binding | 把工具注册给 LLM | `llm.bind_tools([tool])` |
| Tool Execution | 执行真实的函数调用，返回结果 | `sql_query.invoke({"question": "..."})` |

---

## 三、ETL 类比

| 数据库概念 | Function Calling 概念 |
|-----------|----------------------|
| 存储过程 `sp_query_top_products` | Tool / Function |
| 存储过程参数 `@question NVARCHAR` | JSON Schema `{"question": "string"}` |
| 调用存储过程拿到结果集 | Tool 执行返回字符串 |
| 把结果集返回给报表工具 | 把 Tool 结果返回给 LLM 总结 |

---

## 四、选型对比

| 方案 | 优点 | 缺点 | 选用 |
|------|------|------|------|
| `@tool` 装饰器 | 自动从函数签名生成 Schema | 灵活性略低 | ✅ |
| `StructuredTool.from_function()` | 手动控制 Schema | 代码更啰嗦 | ❌ |
| `BaseTool` 子类 | 完全自定义 | 过度设计 | ❌ YAGNI |

---

## 五、关键代码

```python
@tool(description=SQL_TOOL_DESCRIPTION)
def sql_query(question: str) -> str:
    # @tool 在背后做了三件事：
    # 1. 读函数签名 → 生成 JSON Schema
    # 2. 读 docstring → 生成工具描述发给 LLM
    # 3. 包装成 StructuredTool 对象
    result = agent.query(question)
    if result["success"]:
        return f"[SQL: {result['sql']}]\n\n{result['answer']}"
    else:
        return f"查询失败：{result['error']}"
```

---

## 六、常见坑

1. **temperature 冲突** — `get_llm_kwargs()` 已含 temperature，再传会报错
2. **LLM 判断不稳** — DeepSeek 在工具选择上不如 GPT-4，需要写好工具描述
3. **工具内部异常** — 必须 try/except 返回错误字符串，不要让异常污染 LLM 上下文
4. **延迟初始化** — SQLQueryAgent 创建成本高，在 create_sql_tool() 中做，不在模块导入时做

---

## 七、面试话术

### 30秒版
"Function Calling 让 LLM 从纯文本生成变成能调用外部系统。我项目中用 LangChain 的 @tool 装饰器把 SQL 查询封装成工具，LLM 自己判断用户问题是否需要查库，需要就自动调用，拿到真实数据再回答。"

### 3分钟版
"传统聊天机器人只能基于训练数据回答，看不到企业内部数据。Function Calling 的本质是给 LLM 一个'手'——它不再只是生成文本，而是能调用外部函数获取实时信息。技术上分三层：Tool Definition 定义接口契约，Tool Binding 注册给 LLM，Tool Execution 执行真实查询。我做了一个关键设计：工具内部封装了完整的 SQLQueryAgent，包括安全校验（只允许 SELECT）、错误处理、SQL 语句捕获。这样 LLM 只看到工具接口，内部复杂性对它透明——这和数据库存储过程一个道理。"

---

## 相关笔记

- [[2026-05-15_tech_text-to-sql-agent详解]]
- [[2026-05-15_tech_visualizer详解]]
- [[2026-05-16_tech_prompt-engineering详解]]
- [[2026-05-16_tech_fastapi详解]]
