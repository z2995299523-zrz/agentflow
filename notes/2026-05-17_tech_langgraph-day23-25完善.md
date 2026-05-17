# LangGraph Day 23-25：BGE预热 + Mixed双路 + 记忆 + 评估

> 日期: 2026-05-17 | 类别: tech | 关联: [[2026-05-17_tech_langgraph-多agent工作流]]

---

## 📦 完成内容

### 1. BGE 模型预热（FastAPI lifespan）

**问题：** 懒加载导致第一个 RAG 请求等 15 秒（BGE 模型 100MB）。

**方案：** FastAPI `lifespan` 事件在启动时预热模型，注入到 graph 节点。

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.vectorstore = load_vectorstore()  # 预热
    init_vectorstore(app.state.vectorstore)     # 注入
    yield
```

nodes.py 改为模块级单例 + fallback 懒加载：
- 优先用 `init_vectorstore()` 注入的实例
- 未注入时自动懒加载（测试/直接调用场景）

**面试话术：** "预热 vs 懒加载的 tradeoff。就像数据库连接池——启动时建立连接，请求时零等待。"

### 2. Mixed 意图双路并行

**图结构调整：**
```
supervisor → rag → [mixed→sql → finalize]
                   [其他→finalize]
```

新增 `route_after_rag()` — 看 intent 决定 RAG 后是否继续走到 SQL。

**Agent 间信息传递：** sql_node 读取 `state["rag_result"]`，注入到问题上下文：
```python
if rag_context and state.get("intent") == "mixed":
    question = f"参考信息：{rag_context[:500]}\n\n问题：{question}"
```

### 3. 多轮对话记忆（MemorySaver）

**graph/memory.py：** 全局 MemorySaver 单例 + make_config(thread_id)

```python
wf = build_workflow()  # 默认 enable_memory=True
config = make_config("user-123")
wf.invoke({"question": "产品表有多少条"}, config)  # 第一轮
wf.invoke({"question": "客户表呢"}, config)         # 第二轮
```

thread_id 隔离不同会话，messages 用 add_messages reducer 自动累积。

### 4. 评估基线（eval/ 模块）

**3 项指标：**
| 指标 | 方法 | 目标 |
|------|------|------|
| 路由准确率 | 10题 × supervisor_node | ≥90% |
| 端到端成功率 | 10题 × workflow.invoke | ≥80% |
| LLM-as-Judge | DeepSeek 打分 1-5 | ≥3.5 |

**评估脚本：** `python eval/langgraph_eval.py`

---

## ⚠️ 踩坑

1. Mixed 路由：`route_after_rag` 写好了但忘记接入图，手动补了 `add_conditional_edges`
2. 页面文件不足：`WinError 1455` — PyTorch + LangChain 同时加载时 Windows 虚拟内存不足
3. 评估脚本运行慢：10 个 LLM 调用串行，后续可改成并行 + 缓存结果

---

## 相关笔记
- [[2026-05-17_tech_langgraph-多agent工作流]]
- [[2026-05-17_tech_langgraph概念讲解]]
- [[2026-05-17_discuss_bge冷启动与多agent顺序]]
