# Week 5 Day 21-22: LangGraph 多Agent 工作流（基础框架 + 单Agent 节点化）

> **For Hermes:** 先讲解 LangGraph 核心概念，再委托 Claude Code 实现，最后审查讲解
>
> **关联技能:** claude-code-coding, agentflow-dev

**目标:** 搭建 LangGraph 多Agent 框架基础，将 RAG Agent 和 SQL Agent 改造为 LangGraph 节点，实现 Supervisor 路由器。

**架构:**
```
用户提问 → Supervisor（意图识别）
    ├── rag  → RAG Agent Node → 带引用回答
    ├── sql  → SQL Agent Node → 查询+图表
    └── mixed → RAG → SQL → 汇总
```

**新增文件:** graph/state.py, graph/nodes.py, graph/supervisor.py, graph/workflow.py, graph/__init__.py, test_graph.py

---

## Task 1: 创建 LangGraph 共享状态（State）

**目标:** 定义多Agent 工作流中所有节点共享的状态结构

**文件:**
- 创建: `graph/__init__.py`
- 创建: `graph/state.py`

**要实现的 State 字段:**

```python
from typing import TypedDict, Annotated, Sequence
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

class AgentState(TypedDict):
    # 对话消息历史（自动追加合并）
    messages: Annotated[Sequence[BaseMessage], add_messages]
    
    # 当前用户问题
    question: str
    
    # Supervisor 路由决策: "rag" | "sql" | "mixed" | "unknown"
    intent: str
    
    # RAG 节点输出
    rag_result: str      # RAG 回答文本
    rag_sources: list    # 文档来源列表
    
    # SQL 节点输出
    sql_result: str      # SQL 查询结果（自然语言）
    sql_query: str       # 生成的 SQL
    sql_chart: str       # 图表 base64（如有）
    
    # 最终输出
    final_answer: str
```

**验收标准:**
- State 定义能正常 import 和使用
- 包含所有必要字段
- 使用 LangGraph 的 TypedDict + Annotated 模式

---

## Task 2: RAG Agent 节点化

**目标:** 将现有 `rag/chain.py` 的 ask() 函数包装为 LangGraph 节点

**文件:**
- 创建: `graph/nodes.py`

**节点函数签名:**
```python
def rag_node(state: AgentState) -> dict:
    """
    RAG 问答节点。
    从 state["question"] 读取问题 → 调用 rag.ask() → 写回 state
    返回: {"rag_result": "...", "rag_sources": [...]}
    """
```

**关键设计:**
- 节点只接收 state，返回部分 state update（LangGraph 自动合并）
- 懒加载 vectorstore + chain（避免启动开销）
- 返回 rag_result + rag_sources 而不是 final_answer（由下游节点汇总）

**验收标准:**
- 能独立调用 `rag_node(test_state)` 并得到正确结果
- vectorstore 无文档时返回友好提示而非崩溃

---

## Task 3: SQL Agent 节点化

**目标:** 将现有 `sql/agent.py` 的 SQLQueryAgent.query() 包装为 LangGraph 节点

**文件:**
- 修改: `graph/nodes.py`（追加 sql_node 函数）

**节点函数签名:**
```python
def sql_node(state: AgentState) -> dict:
    """
    SQL 查询节点。
    从 state["question"] 读取问题 → 调用 SQLQueryAgent.query() → 写回 state
    返回: {"sql_result": "...", "sql_query": "SELECT ...", "sql_chart": "base64..."}
    """
```

**关键设计:**
- 懒加载 SQLQueryAgent 单例
- 从 sql_result 中提取自然语言回答
- 失败时返回错误信息而非崩溃

**验收标准:**
- 能独立调用 `sql_node(test_state)` 得到正确结果
- 数据库不存在/连接失败时返回友好提示

---

## Task 4: Supervisor 路由器

**目标:** 创建 Supervisor Agent，判断用户意图并路由

**文件:**
- 创建: `graph/supervisor.py`

**实现方式:** 用 LLM 做意图分类（zero-shot，不用 Few-shot）

**核心 Prompt:**
```
你是一个智能路由器。分析用户问题，返回意图标签：

- "rag": 知识问答类（需要查文档/知识库）
- "sql": 数据查询类（需要查数据库）
- "mixed": 综合分析类（需要先查文档再查数据库）
- "unknown": 无法判断

只返回意图标签，不要其他内容。
```

**函数签名:**
```python
def supervisor_node(state: AgentState) -> dict:
    """意图识别节点。分析 question → 决定路由"""
    # 调用 LLM 判断意图
    # 返回: {"intent": "rag"|"sql"|"mixed"|"unknown"}
```

**路由函数:**
```python
def route_by_intent(state: AgentState) -> str:
    """条件边：根据 intent 路由到不同节点"""
    intent = state["intent"]
    if intent == "rag": return "rag_node"
    elif intent == "sql": return "sql_node"
    elif intent == "mixed": return "rag_node"  # 先 RAG
    else: return "fallback_node"
```

**验收标准:**
- "今年销售额最高的产品是哪些" → sql
- "如何配置数据库连接" → rag  
- "根据产品文档分析销售趋势" → mixed
- 纯闲聊 → unknown

---

## Task 5: 工作流图组装

**目标:** 将 Supervisor + RAG Node + SQL Node 组合为完整 StateGraph

**文件:**
- 创建: `graph/workflow.py`

**图结构:**

```
START
  ↓
supervisor_node (意图识别)
  ├── rag → rag_node → finalize_node → END
  ├── sql → sql_node → finalize_node → END  
  ├── mixed → rag_node → sql_node → finalize_node → END
  └── unknown → fallback_node → END
```

**关键代码结构:**
```python
from langgraph.graph import StateGraph, START, END

def build_workflow() -> StateGraph:
    graph = StateGraph(AgentState)
    
    # 添加节点
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("rag", rag_node)
    graph.add_node("sql", sql_node)
    graph.add_node("finalize", finalize_node)
    graph.add_node("fallback", fallback_node)
    
    # 边
    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges("supervisor", route_by_intent, {...})
    graph.add_edge("rag", "finalize")
    graph.add_edge("sql", "finalize")
    graph.add_edge("finalize", END)
    graph.add_edge("fallback", END)
    
    return graph.compile()
```

**finalize_node**: 将各节点输出汇总为 `final_answer`
**fallback_node**: 当意图无法识别时返回友好提示

**验收标准:**
- `workflow.invoke({"question": "测试问题"})` 能跑通
- 路由正确，各节点输出汇总为 final_answer

---

## Task 6: 端到端测试

**文件:**
- 创建: `test_graph.py`

**测试用例（10 项）:**

| # | 问题 | 预期意图 | 验收点 |
|---|------|---------|--------|
| 1 | "产品表有多少条记录" | sql | sql_result 非空 |
| 2 | "销售额最高的5个产品是哪些" | sql | sql_result + sql_query 非空 |
| 3 | "如何配置数据库连接" | rag | rag_result 非空 |
| 4 | "项目有哪些技术栈" | rag | rag_result 非空 + sources |
| 5 | "分析产品销售趋势并给出建议" | mixed | rag_result + sql_result 都非空 |
| 6 | "你好" | unknown | final_answer 非空（友好回复） |
| 7 | supervisor 路由测试 | — | 连续5问路由正确 |
| 8 | state 传递测试 | — | rag 结果能传入 sql |
| 9 | 错误处理测试 | — | 无文档时 rag 不崩溃 |
| 10 | workflow.invoke 完整性 | — | final_answer 非空 |

**验收标准:**
- 至少 8/10 通过（网络/LLM 非确定性可能导致偶发失败）
- 路由准确率 80%+

---

## 实施顺序（强制）

```
Task 1 (state.py) → Task 2 (RAG node) → Task 3 (SQL node)
    → Task 4 (supervisor) → Task 5 (workflow.py) → Task 6 (test_graph.py)
```

每个 Task 完成后必须验证 import 正常 + 基础功能可用，再进入下一 Task。

---

## 注意事项

1. **LangGraph 版本:** 项目已有 langgraph 1.1.3，StateGraph API 稳定
2. **懒加载:** RAG vectorstore 和 SQL agent 在函数内懒加载，不在模块 import 时初始化
3. **错误处理:** 每个节点必须 try-except，失败时返回有意义的状态更新
4. **不使用 checkpointer:** Day 21-22 先不加入持久化（Day 25 再加）
5. **CLAUDE.md 更新:** 完成后需更新 CLAUDE.md 反映新模块
