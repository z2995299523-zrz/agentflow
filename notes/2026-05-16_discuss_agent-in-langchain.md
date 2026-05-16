# Agent 在 LangChain 中的位置 + 多 Agent 协作

> 2026-05-16 | discuss | 对话讲解

---

## 一、LangChain 组件层级

```
┌─────────────────────────────────────────────┐
│              LangGraph（多Agent 编排）        │  ← AgentFlow Week 5
│  ┌───────────────────────────────────────┐  │
│  │         Agent（自主决策循环）           │  │  ← sql/agent.py
│  │  ┌─────────────────────────────────┐  │  │
│  │  │     Chain（固定流程管道）         │  │  │  ← rag/chain.py
│  │  │  ┌───────────────────────────┐  │  │  │
│  │  │  │   Tool（单个能力单元）      │  │  │  │  ← sql/tools.py
│  │  │  │   LLM（大脑）              │  │  │  │
│  │  │  └───────────────────────────┘  │  │  │
│  │  └─────────────────────────────────┘  │  │
│  └───────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
```

---

## 二、各层定义

| 层级 | 决策方式 | AgentFlow 中的例子 |
|------|---------|-------------------|
| LLM | 没有决策，你问它答 | `ChatOpenAI` |
| Tool | 被调用就执行，不决策 | `sql_query(question)` |
| Chain | 固定顺序 A→B→C | RAG 链：检索→格式化→LLM→输出 |
| Agent | LLM 自主决策：调哪个工具、调几次、何时停 | `SQLQueryAgent` |
| LangGraph | Supervisor 路由 + 条件分支 | Week 5：Supervisor→RAG/SQL |

---

## 三、Chain vs Agent（核心区别）

### ETL 类比

```
Chain = DataStage Job Sequence（固定流程）
  Step1 → Step2 → Step3，顺序写死，永不跳过

Agent = 有经验的 DBA（自主决策）
  "查销售额Top5"
  → 自己判断先看哪些表
  → 决定需要 JOIN
  → 结果为空中换写法重试
  → 结果太多加 LIMIT
```

### 一句话

Chain = 流水线工人（按工序走），Agent = 老师傅（自己判断怎么做）。

---

## 四、多 Agent 协作：LangChain 不行，LangGraph 行

| | LangChain | LangGraph |
|---|----------|-----------|
| 单 Agent + 多 Tool | ✅ | ✅ |
| 多 Agent 互相协作 | ❌ | ✅ |
| 条件路由 | ❌ | ✅ |
| 跨 Agent 状态共享 | ❌ | ✅ |

### LangGraph 多 Agent 结构

```python
graph = StateGraph(AgentState)
graph.add_node("supervisor", supervisor)  # 路由器
graph.add_node("rag", rag_node)           # RAG Agent
graph.add_node("sql", sql_node)           # SQL Agent

# Supervisor 判断意图 → 路由到 RAG 或 SQL
graph.add_conditional_edges("supervisor", route_fn, {"rag":"rag", "sql":"sql"})
```

---

## 五、ETL 类比多 Agent 调度

```
调度系统（DolphinScheduler）判断：
├── 需要客户数据 → Job A
├── 需要交易流水 → Job B
└── 都跑完 → 汇总 → 报表

LangGraph Supervisor 判断：
├── 文档类问题 → RAG Agent
├── 数据类问题 → SQL Agent
└── 都完成 → 汇总 → 对比分析
```

**调度逻辑、条件路由、状态传递——和 ETL 依赖解析一模一样的思维模型。**

---

## 六、面试话术

"Chain 是固定流程，像 ETL 的 Job Sequence。Agent 是自主决策，LLM 自己判断该调用哪个工具。我项目里两个都有：RAG 用 Chain，SQL 用 Agent。多 Agent 协作用 LangGraph 编排——Supervisor 路由，和调度系统的依赖解析一个道理。"

---

## 相关笔记

- [[2026-05-16_tech_langchain详解]]
- [[2026-05-16_tech_function-calling详解]]
