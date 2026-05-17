# LangGraph 多Agent 工作流详解

> 日期: 2026-05-17 | 类别: tech | 关联: [[2026-05-17_tech_langgraph概念讲解]]
> 实现阶段: Week 5 Day 21-22

---

## 📦 模块目标

把 RAG Agent 和 SQL Agent 两个独立模块，通过 LangGraph 编排成一个协作工作流。
用户问一个问题 → Supervisor 自动判断意图 → 路由到对应 Agent → 汇总回答。

---

## 📖 讲透：每个文件的设计原理

### 1. state.py — 为什么需要显式定义 State？

**问题起源：** 多个 Agent 之间怎么传数据？全局变量有致命问题：不知道谁在什么时候改了什么、并发时互相覆盖、调试找不到数据流向。

**LangGraph 方案：** 把共享数据定义为 `TypedDict`，每个节点只返回它修改的字段，图引擎负责合并。
就像数据库的 MERGE 操作——只 UPDATE 负责的列，其他列原封不动。

```python
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]  # ← 特殊！
    question: str
    intent: str
    rag_result: str
    rag_sources: list
    sql_result: str
    sql_query: str
    sql_chart: str
    final_answer: str
```

**关键理解：`Annotated[Sequence[BaseMessage], add_messages]`**

普通的 list 字段，节点 B 返回消息会覆盖节点 A 的消息。`add_messages` 是一个 **reducer 函数**，
告诉 LangGraph：这个字段的更新方式是"追加"，不是"替换"。

**ETL 类比：** messages = 增量日志表（insert-only），其他字段 = 全量快照表（overwrite）。

---

### 2. nodes.py — 懒加载模式

**为什么 import 写在函数里而不是文件顶部？**

`load_vectorstore()` 触发 BGE 模型加载（100MB+）。模块级 import 会阻塞整个包的加载。
懒加载推迟到第一次实际调用，类比数据库的延迟物化视图。

```python
def rag_node(state: AgentState) -> dict:
    from rag.chain import ask          # 函数内懒加载
    from rag.vectorstore import load_vectorstore
    ...
```

**SQL Agent 单例：**

```python
_sql_agent = None  # 模块级缓存

def sql_node(state):
    global _sql_agent
    if _sql_agent is None:
        _sql_agent = SQLQueryAgent()  # 首次创建，后续复用
```

每次调用不需要重新连接数据库。

---

### 3. supervisor.py — 零样本意图分类

**核心思路：** LLM 做路由器，zero-shot（不给示例），只给规则。

**为什么 temperature=0？** 路由器需要确定性——同样的问题每次必须走到同样的 Agent。
temperature=0 关闭随机采样，LLM 永远选最高概率 token。

**防御性编程：**
```python
valid_intents = {"rag", "sql", "mixed", "unknown"}
if intent not in valid_intents:
    intent = "unknown"  # LLM 万一输出奇怪内容，兜底
```

类比 ETL 数据质量校验：输入不可信，必须清洗。

---

### 4. workflow.py — 图的组装

**图结构：**
```
START → supervisor → [rag | sql | fallback]
rag → finalize → END
sql → finalize → END
fallback → END
```

**add_conditional_edges 三个参数：**
1. 从哪个节点出发
2. 决策函数（看 state 决定下一步）
3. 返回值 → 目标节点的映射表

类比 DolphinScheduler 的条件分支。

**finalize_node：**
只看 state 里有没有结果，不管是 RAG 还是 SQL——这就是 State 作为共享内存的好处。

---

## 🔧 选型理由（面试版）

| 决策 | 选择 | 为什么 |
|------|------|--------|
| 路由方式 | LLM 零样本分类 | 简单、可扩展、不需要维护规则 |
| 路由温度 | temperature=0 | 路由器需要确定性 |
| 节点加载 | 懒加载 | 避免 BGE 模型加载阻塞 |
| mixed 处理 | 先 RAG 后 SQL | 串行最简单，可优化为并行 |
| State 定义 | TypedDict | 类型安全，LangGraph 原生 |

---

## 💡 可迁移概念

学会了 LangGraph StateGraph = 理解了：
- AWS Step Functions（云端的 StateGraph）
- Temporal/Cadence（分布式工作流）
- Airflow DAG（ETL 的 StateGraph）
- 所有工作流引擎的核心概念：State + Node + Edge + Conditional

---

## 👨‍💻 面试话术

### 30秒版
"我用 LangGraph 的 StateGraph 把 RAG 和 SQL 两个 Agent 串成协作工作流。
Supervisor 节点用 LLM 做零样本意图分类，条件边实现分支路由。
State 机制类似 ETL 临时表，节点间通过共享状态传递数据。"

### 3分钟深度版
"AgentFlow 的核心是多 Agent 协作。单 Agent 只能做一件事，真实业务需要多个 Agent 配合。
LangGraph 的 StateGraph 解决了这个问题——

第一，显式 State 定义。用 TypedDict 声明所有节点共享的字段，
messages 用 add_messages reducer 做增量追加，其他字段做覆盖更新。
类似数据库的增量日志表 + 全量快照表在同一张 State 里共存。

第二，条件路由。Supervisor 节点用 LLM 做零样本意图分类，
temperature=0 确保确定性。LLM 的输出经过白名单校验兜底。
条件边根据分类结果路由到 RAG 或 SQL 节点。

第三，解耦设计。每个节点只知道自己从 state 读什么、写回什么，
不知道其他节点的存在。finalize 节点只看 state 里有没有结果，
不管是哪个上游节点产生的。这种松耦合让后续加新 Agent 只需加一个节点。"

---

## ⚠️ 常见坑

1. **模块顶层编译图** — 永远在函数内 `graph.compile()`，否则 import 就触发 LLM 调用
2. **忘记 add_messages reducer** — messages 字段不加 reducer，每次消息覆盖历史
3. **节点返回不是 dict** — 必须返回 dict（部分 state 更新），不能返回完整 state
4. **BGE 模型首次加载慢** — 15s+，懒加载 + 面试前预热

---

## 相关笔记
- [[2026-05-15_tech_text-to-sql-agent详解]]
- [[2026-05-15_tech_visualizer详解]]
- [[2026-05-14_tech_rag实现详解]]
