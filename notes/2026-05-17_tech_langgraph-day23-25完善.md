# LangGraph Day 23-25：BGE预热 + Mixed双向路由 + MemorySaver + 评估

> 日期: 2026-05-17 | 类别: tech | 关联: [[2026-05-17_tech_langgraph-多agent工作流]], [[2026-05-17_design_mixed双向路由]]

---

## 一、BGE 模型预热：FastAPI lifespan + 依赖注入

### 问题起源

之前的 rag_node 是懒加载——第一个用户调用时才加载 BGE 模型（100MB，15秒），用户体验很差。

### 核心原理：lifespan 异步上下文管理器

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # yield 之前 = 启动阶段
    app.state.vectorstore = load_vectorstore()    # ① 加载模型
    app.state.sql_agent = SQLQueryAgent()          # ② 预热 Agent
    from graph.nodes import init_vectorstore, init_sql_agent
    init_vectorstore(app.state.vectorstore)        # ③ 注入到 graph 节点
    init_sql_agent(app.state.sql_agent)
    yield  # 服务就绪，接收请求
    # yield 之后 = 关闭阶段，清理资源
```

`@asynccontextmanager` 把生成器函数变成异步上下文管理器。yield 前的代码在 `async with` 进入时执行，yield 后的代码在退出时执行。FastAPI 内部用 `async with lifespan(app)` 包裹整个服务生命周期。

**类比 ETL 数据库连接池：** 启动时建 5 个连接（预热），跑任务时直接拿（零等待），完全一样的逻辑。

### 依赖注入 + 懒加载兜底

```python
# graph/nodes.py
_vectorstore = None   # 模块级单例

def init_vectorstore(vs):       # 注入接口（main.py lifespan 调用）
    global _vectorstore
    _vectorstore = vs

def rag_node(state):
    global _vectorstore
    if _vectorstore is None:    # 兜底：未注入时自己懒加载
        from rag.vectorstore import load_vectorstore
        _vectorstore = load_vectorstore()
    result = ask(question, vectorstore=_vectorstore)
```

两条路径：生产走 lifespan 注入（零等待），测试走懒加载兜底（自给自足）。

**面试深讲：** "依赖注入把创建和使用解耦。graph 节点不关心实例从哪来、什么时候创建。这就像 ETL 脚本不关心数据库连接是谁建立的，只管用。懒加载兜底让单元测试可以直接调 rag_node 而不用先启动 FastAPI——关注点分离。"

### 选型对比

| 方案 | 问题 |
|------|------|
| 模块顶层 import 时加载 | 阻塞整个包导入，启动超慢 |
| `@app.on_event("startup")` | FastAPI 已废弃 |
| **lifespan（我们用的）** | 异步、官方推荐、支持清理 |

### 可迁移概念

- **依赖注入**：Spring、Angular、FastAPI Depends() 都是这个模式
- **模块级单例**：Python 的模块天然是单例，import 一次后 `_vectorstore` 全局共享
- **lifespan 模式**：K8s init container、Lambda cold start、Docker ENTRYPOINT vs CMD——本质都是"启动时做一次性准备"

---

## 二、Mixed 双向路由：Post-condition Guard 防死循环

### 问题

之前 mixed 硬编码 `RAG → SQL → finalize`。但真实场景存在反向需求：

> "查一下投诉最多的5个客户，然后从知识库里找对应的投诉处理流程" → 先 SQL 后 RAG

### 核心思路：对称条件路由 + 结果判空

不是让 supervisor 决定"先走哪个"，而是让 **RAG 和 SQL 互为可达节点，用执行结果判断"跑过没"**。

```python
def route_after_rag(state):
    # mixed 且 SQL 还没跑（sql_result 为空）→ 继续到 SQL
    if state.get("intent") == "mixed" and not state.get("sql_result"):
        return "sql"
    return "finalize"

def route_after_sql(state):
    # mixed 且 RAG 还没跑（rag_result 为空）→ 继续到 RAG
    if state.get("intent") == "mixed" and not state.get("rag_result"):
        return "rag"
    return "finalize"
```

**为什么不会死循环？** 每个节点跑完后，它负责的 `*_result` 字段从空变为非空。字段只写不删，条件最多满足一次。

| 轮次 | rag_result | sql_result | route_after_rag | route_after_sql |
|------|-----------|-----------|----------------|----------------|
| 初始 | 空 | 空 | sql | rag |
| RAG跑完 | 非空 | 空 | sql | finalize |
| SQL跑完 | 非空 | 非空 | finalize | finalize |

**图结构：**

```
supervisor → mixed
    ├── 先 RAG → route_after_rag(sql未跑)→ SQL → route_after_sql(rag已跑)→ finalize
    └── 先 SQL → route_after_sql(rag未跑)→ RAG → route_after_rag(sql已跑)→ finalize
```

### 设计模式：Post-condition Guard

"Post-condition" = 执行后的状态。"Guard" = 门卫检查条件。

类比 ETL：数据清洗脚本跑完后，检查数据质量是否达标。达标 → 加载，不达标 → 重回清洗。这里的 rag_result/sql_result 就是质量检查信号。

### 选型对比

| 方案 | 问题 |
|------|------|
| LLM 判断"先查哪个" | 多一次 LLM 调用，增加延迟和成本 |
| 并行跑 RAG+SQL 再合并 | 有依赖的场景不适用 |
| **Post-condition Guard（我们用的）** | 零额外 LLM 调用、防死循环、支持任意顺序 |

### 可迁移概念

- **拓扑排序**：DAG 中判断"前置节点是否完成"的逻辑
- **乐观锁**：用状态字段判断是否已处理，类似数据库 version 字段
- **状态机**：每个节点跑完改变状态，守卫函数检查状态决定跳转

### 面试话术

> "Mixed 意图的路径不是硬编码的。我用对称的条件路由——RAG 和 SQL 互为可达节点，
> 通过 state 里的结果字段判断'跑过没'来做防死循环。这个模式叫 Post-condition Guard，
> 无论 supervisor 先把请求路由到哪个节点，最终两个 Agent 都会执行。
> 改动完全在路由层，不影响节点本身——关注点分离。"

---

## 三、多轮对话记忆：MemorySaver + Checkpoint

### 核心原理

LangGraph 的 checkpointer 不是简单存对话历史，而是做**状态快照（State Snapshot）**——每次 invoke 结束后把整个 AgentState 序列化存下来，下次同一个 thread_id 调用时从快照恢复。

**类比数据库：** Checkpoint = 事务提交后的快照。thread_id = 事务 ID。不同 thread_id 完全隔离。

```python
from graph.memory import make_config

wf = build_workflow()  # checkpointer=MemorySaver()
config = make_config("user-123")

wf.invoke({"question": "产品表有多少条"}, config)   # 第一轮，存入 checkpoint[user-123]
wf.invoke({"question": "那客户表呢"}, config)        # 第二轮，从 checkpoint[user-123] 恢复
```

**底层：** MemorySaver 内部是 `dict[thread_id, StateSnapshot]`。LangGraph 自动在 invoke 结束时存、开始时取。

### 重要限制

`messages` 用 `add_messages` reducer 决定了追加而非覆盖的合并策略，但**节点目前还没往 messages 追加内容**。节点需要返回 `{"messages": [HumanMessage(...), AIMessage(...)]}` 才能真正累积对话历史。这是后续优化点。

### 踩坑

1. MemorySaver 重启丢失 → 生产换 SqliteSaver/PostgresSaver
2. 忘记传 config → 所有对话混在一起
3. messages 需要节点主动追加 → reducer 只决定合并策略，不自动创建内容

---

## 四、评估基线（eval/ 模块）

3 项指标：
| 指标 | 方法 | 目标 |
|------|------|------|
| 路由准确率 | 10题 × supervisor_node | ≥90% |
| 端到端成功率 | 10题 × workflow.invoke | ≥80% |
| LLM-as-Judge | DeepSeek 打分 1-5 | ≥3.5 |

运行：`python eval/langgraph_eval.py`

---

## 踩坑记录

1. Claude Code 定义了 route_after_rag 但忘记接入图 → 手动补 add_conditional_edges
2. Claude Code 在 lifespan 里用无意义的 global _sql_agent → 删掉
3. 评估脚本 WinError 1455（页面文件太小）→ Windows 虚拟内存不足，非代码问题
4. 本轮 3 次 Claude Code 调用共 $1.60

---

## 相关笔记
- [[2026-05-17_tech_langgraph-多agent工作流]]
- [[2026-05-17_tech_langgraph概念讲解]]
- [[2026-05-17_discuss_bge冷启动与多agent顺序]]
- [[2026-05-17_design_mixed双向路由]]
