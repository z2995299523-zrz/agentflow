# LangGraph 核心概念讲解

> 日期: 2026-05-17 | 类别: tech | 关联: [[2026-05-17_tech_langgraph-多agent工作流]]
> 讲解阶段: Week 5 Day 21-22 前置知识

---

## 一、问题起源：一个 Agent 搞不定的事

你有两个独立工作的 Agent：RAG（查文档）和 SQL（查数据库）。

但真实场景用户不会说"用 RAG"或"用 SQL"，只会说：
**"根据产品手册，分析一下今年哪些产品卖得好，给我建议。"**

这个问题需要：
1. 先查文档（RAG）→ 了解"好产品"的标准
2. 再查数据库（SQL）→ 算出销售额排行
3. 最后汇总 → 结合标准和数据给出建议

**LangGraph 解决的问题：把多个 Agent 串成一条流水线。**

---

## 二、ETL 类比

| ETL 概念 | LangGraph 概念 |
|----------|---------------|
| DAG（有向无环图） | StateGraph |
| ETL 步骤（抽取→清洗→加载） | Node（节点） |
| 数据管道流向 | Edge（边） |
| 条件分支（if 质量>阈值→主表 else→异常表） | Conditional Edge（条件边） |
| 数据缓冲区/临时表 | State（状态） |
| DolphinScheduler 任务编排 | LangGraph 工作流引擎 |

本质上：你以前用 DolphinScheduler 编排 1500+ ETL 脚本的依赖关系，
现在用 LangGraph 编排 3-5 个 Agent 的协作关系。原理一样，规模更小。

---

## 三、核心概念

### State（状态）—— 所有节点的"共享内存"

```python
class AgentState(TypedDict):
    question: str       # 用户问题
    intent: str         # 路由决策
    rag_result: str     # RAG 输出
    sql_result: str     # SQL 输出
    final_answer: str   # 最终回答
```

节点 A 写入 `{"rag_result": "查到3条文档"}`，节点 B 读取 `state["rag_result"]`。
类比 ETL：State = 临时表，所有人看同一张表。

### Node（节点）—— 可复用的处理单元

每个节点 = 一个纯函数：`(state) → partial_state_update`

节点不直接调用下一个节点，只返回部分 state 更新，引擎负责传递和合并。
类比：ETL 脚本不硬编码"接下来跑脚本 B"，DolphinScheduler 负责调度。

### Edge（边）

- 普通边：固定流向，A → B
- 条件边：根据 state 决定流向

```python
graph.add_conditional_edges("supervisor", route_by_intent, {
    "rag": "rag_node",
    "sql": "sql_node",
    "fallback": "fallback",
})
```

---

## 四、选型对比

| 方案 | 优点 | 缺点 | 适用 |
|------|------|------|------|
| **LangGraph** | 有状态、条件路由、检查点 | 学习曲线中等 | ✅ 多Agent 协作 |
| LangChain Chain | 简单 | 无法分支 | 线性流水线 |
| 手写 if-else | 零依赖 | 不可维护 | Demo |
| CrewAI | 高层抽象 | 黑盒 | 快速原型 |
| AutoGen | 微软出品 | 太重 | 企业级大项目 |

---

## 五、面试话术

**30秒版：**
"LangGraph 是 LangChain 生态的有状态图编排框架。我用它把 RAG 和 SQL 两个 Agent 串成协作工作流，
Supervisor 节点做意图路由，条件边实现分支决策。State 机制类似 ETL 临时表。"

**3分钟版：**
"选 LangGraph 有三个原因。第一，它解决了单 Agent 的天花板问题。
第二，Conditional Edge 机制特别适合意图路由场景。
第三，State 是显式的——你能看到每一步的状态变化，调试和展示都非常友好。
对比 CrewAI 和 AutoGen，它们抽象层级太高，出了问题你不知道是 prompt 的问题还是编排的问题。"

---

## 相关笔记
- [[2026-05-17_tech_langgraph-多agent工作流]]
- [[2026-05-15_tech_text-to-sql-agent详解]]
- [[2026-05-16_tech_langchain详解]]
