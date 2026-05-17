# BGE 模型冷启动 + 多Agent 协作顺序

> 日期: 2026-05-17 | 类别: discuss | 关联: [[2026-05-17_tech_langgraph-多agent工作流]], [[2026-05-17_tech_langgraph概念讲解]]

---

## 问题 1：BGE 模型在项目启动时冷加载

### 当前：懒加载（第一次 RAG 请求才加载模型，15秒+）

```python
def rag_node(state):
    from rag.vectorstore import load_vectorstore
    vectorstore = load_vectorstore()  # ← 15秒
```

### 改进：FastAPI lifespan 预热

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时预热
    app.state.vectorstore = load_vectorstore()
    app.state.sql_agent = SQLQueryAgent()
    yield
```

**效果：** 启动多等 20 秒 vs 第一个用户等 15 秒 → 选启动多等。

**面试话术：** "把冷启动成本从用户体验转移到运维启动，是服务端优化的经典 tradeoff。
就像数据库连接池在启动时建立连接而不是等第一次 SQL。"

### 注意：预热后节点函数要改

- graph/nodes.py 需要支持外部注入 vectorstore/agent，或者改为模块级单例
- 和 SQL Agent 的 `_sql_agent` 模式统一
- 建议 Day 23-25 一起做

---

## 问题 2：多 Agent 协作先后顺序

### 核心原则：看数据依赖

B 的输入需要 A 的输出 → A 必须在 B 前面（串行）。
没有依赖 → 可以并行。

### 三种协作模式

| 模式 | 图结构 | 场景 |
|------|--------|------|
| 串行 | A → B → C | RAG 结果作为 SQL 条件 |
| 并行 | A ↘ C ↙ B | 同时查文档和数据库，汇总 |
| 条件串行 | A → [条件] → B or C | mixed：RAG → SQL → 汇总 |

### LangGraph 实现

边控制顺序，State 传递数据：

```python
graph.add_edge("rag", "sql")          # 保证 RAG 先于 SQL
graph.add_edge("sql", "finalize")     # SQL 完了才汇总
```

### ETL 类比

DAG 拓扑排序 = 图的边定义执行顺序。
DolphinScheduler 依赖配置 = `graph.add_edge("A", "B")`。
临时表 = State。

### 判断方法

1. 列出所有 Agent 的输入和输出
2. 画依赖图
3. 拓扑排序
4. 按排序结果连边

---

## 相关笔记
- [[2026-05-17_tech_langgraph-多agent工作流]]
- [[2026-05-17_tech_langgraph概念讲解]]
- [[2026-05-16_tech_fastapi详解]]
