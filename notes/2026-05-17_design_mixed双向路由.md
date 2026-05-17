# Mixed 双向路由：RAG↔SQL 对称设计

> 日期: 2026-05-17 | 类别: design | 关联: [[2026-05-17_tech_langgraph-day23-25完善]]

---

## 问题

Mixed 意图（综合分析类问题）之前硬编码为"先 RAG 后 SQL"：

```
supervisor(mixed) → RAG → SQL → finalize
```

但真实场景存在反向需求：

> "查一下投诉最多的5个客户，然后从知识库里找对应的投诉处理流程"

这是**先 SQL 后 RAG**——先查数据发现问题，再查文档找解决方案。

**架构缺陷：** SQL 节点后是固定边 `sql → finalize`，没有回 RAG 的路径。

---

## 方案：对称条件路由 + 防死循环

### 核心思路

不是让 supervisor 决定"先 RAG 还是先 SQL"，而是让 RAG 和 SQL **互相可达**。
无论 supervisor 先把 mixed 路由到哪个节点，最终两个都会执行。

### 防死循环机制

不用 intent 判断，而用**结果是否为空**——这是唯一可靠的"跑过没"信号：

```python
def route_after_rag(state):
    # mixed 且 SQL 还没跑 → 继续到 SQL
    if state.get("intent") == "mixed" and not state.get("sql_result"):
        return "sql"
    return "finalize"

def route_after_sql(state):
    # mixed 且 RAG 还没跑 → 继续到 RAG
    if state.get("intent") == "mixed" and not state.get("rag_result"):
        return "rag"
    return "finalize"
```

### 图结构

```
supervisor → mixed
    ├── 先 RAG → route_after_rag: sql_result为空 → SQL
    │                                            ↓
    │                                       route_after_sql: rag_result非空 → finalize ✅
    │
    └── 先 SQL → route_after_sql: rag_result为空 → RAG
                                                     ↓
                                                route_after_rag: sql_result非空 → finalize ✅
```

### 为什么不会死循环？

第一次通过 RAG：`rag_result` 为空 → 走 RAG → `rag_result` 被填充。
第二次回到 route_after_rag：`rag_result` 非空 → 走 finalize，不再回到 RAG。

同理 SQL 方向。两个节点各跑一次，永远凑不满第三次条件。

---

## 改动量

| 文件 | 改动 | 说明 |
|------|------|------|
| `graph/workflow.py` | +20 行 | route_after_sql 新增 + route_after_rag 加判空 + SQL 边改为条件路由 |
| `test_graph.py` | +30 行 | 6 项路由测试（含防死循环验证） |

不涉及 state.py、nodes.py、supervisor.py——改动完全局限在路由层。

---

## 可迁移概念

这个模式叫 **"Post-condition Guard"**——用执行结果（而非意图标签）判断下一步。比硬编码路径更灵活、更健壮。

类比 ETL：不是写死"先跑脚本 A 再跑脚本 B"，而是"跑完 A 后检查 B 的依赖数据是否就位，没就位就跑 B"。这就是拓扑排序的思路。

---

## 面试话术

> "Mixed 意图的路径不是硬编码的。我用对称的条件路由——RAG 和 SQL 互为可达节点，
> 通过 state 里的结果字段判断'跑过没'来做防死循环。
> 这样无论 supervisor 先把请求路由到哪个节点，最终两个 Agent 都会执行。
> 改动完全在路由层，不影响节点本身的实现——这是关注点分离。"

---

## 相关笔记
- [[2026-05-17_tech_langgraph-day23-25完善]]
- [[2026-05-17_tech_langgraph-多agent工作流]]
- [[2026-05-17_discuss_bge冷启动与多agent顺序]]
