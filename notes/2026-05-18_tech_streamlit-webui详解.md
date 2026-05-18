# Streamlit WebUI 详解 — AgentFlow 前端实现

> 日期: 2026-05-18 | 类别: tech | 模块: app.py

---

## 📦 本模块目标

让 AgentFlow 从"后台 API"变成"可视化的 SaaS 产品"——打开浏览器就能聊天、上传文档、查数据库、看图表。

---

## 📖 讲透：Streamlit 是什么，为什么存在

### 问题起源：Python 开发者做前端的困境

做数据分析/机器学习的 Python 开发者，想把自己的模型或分析结果展示给老板或客户看，面临三个选择：

1. **学 React/Vue 写前端** — 学习成本极高，HTML/CSS/JS/打包/状态管理，最少 3 个月
2. **用 Jupyter Notebook 展示** — 不专业，不能让用户自己交互
3. **用 Flask/FastAPI + 手写 HTML** — 能跑但不美观，每次改界面都要改两套代码

Streamlit 在 2019 年诞生，核心洞察：**数据科学家不应该学前端框架，前端框架应该适应数据科学家的思维模式。**

### 底层原理：三句话讲清

**1. 脚本思维，不是事件驱动**

传统 Web 框架（Flask/FastAPI）是事件驱动模型：定义路由，框架在请求来的时候调用。

Streamlit 是脚本模型：每次用户交互（点按钮、输文字），**从头到尾重新执行整个 Python 脚本**。

**ETL 类比：** Streamlit 的执行模型就像"每次上游数据变了，重新跑一次整个 ETL 流程"。传统 Web 框架像"只跑变了的那个步骤"。

**2. 声明式 UI = SQL 的 SELECT**

你告诉 Streamlit **"我要什么"** 而不是 **"怎么画"**：
```python
st.button("查询")          # 我要一个按钮 — 不用管 HTML/CSS/事件绑定
st.dataframe(df)           # 我要一个表格 — 不用管 DataTable 插件
st.chat_message("user")    # 我要聊天气泡 — 不用管 CSS flexbox
```

这和 SQL 的哲学一样：`SELECT * FROM products WHERE price > 100` — 声明"要什么数据"，不用管"怎么遍历索引"。

**3. 状态管理 = ETL 的增量表/全量表**

Streamlit 用 `st.session_state` 管理跨 rerun 的状态，类似于 ETL 中的全量快照表：每次跑批覆盖全量数据，但 `session_state` 保留"增量"信息。

---

## 🔧 选型理由：Streamlit vs Gradio vs React

| 维度 | Streamlit | Gradio | React + FastAPI |
|------|-----------|--------|-----------------|
| 学习门槛 | 极低，纯 Python | 低，纯 Python | 极高，需 JS/HTML/CSS |
| 聊天界面 | `st.chat_message` 原生 | 需手动实现 | 需自己写组件 |
| 数据表格 | `st.dataframe` 一行 | 有但功能少 | 需 DataTable 库 |
| 图表集成 | 原生 matplotlib/plotly | 原生 | 需 ECharts/Recharts |
| 部署 | `streamlit run` 一条 | `gradio launch` | 需 nginx + 打包 |
| 开发速度 | 🚀 极快 | 🚀 快 | 🐢 慢 |

**选中理由：** 目标是"最短时间做出可演示的完整产品"。Streamlit 的 `st.chat_message` 5 行代码实现 ChatGPT 式聊天，`st.file_uploader` 自带拖拽上传，`st.spinner` 自带 loading。

---

## 💡 可迁移概念

1. **声明式 vs 命令式 UI** — React/Vue/SwiftUI/Flutter 都是同一套思维
2. **状态管理** — `st.session_state` 是现代前端 `useState`/`ref`/`store` 的简化版
3. **缓存策略** — `@st.cache_resource` 类比"数据库连接池"，`@st.cache_data` 类比"物化视图"
4. **组件生命周期** — rerun 模型让你天然理解 React 的 re-render 概念

---

## 👨‍💻 逐段代码详解

### 1. matplotlib Agg 后端设置

```python
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
```

**必须在 import pyplot 之前设定。** matplotlib 有多种后端：`TkAgg` 需要 GUI 窗口（服务器会报错），`Agg`（Anti-Grain Geometry）是纯内存渲染，不需要显示器。后端在首次 import pyplot 时锁定，之后无法改变。

**ETL 类比：** 就像 DataStage 的 job 配置——必须在 job 启动前指定"输出目标"。

### 2. 缓存装饰器

```python
@st.cache_resource
def get_workflow():
    return build_workflow()
```

- `@st.cache_resource`：缓存**不可序列化**对象（DB连接、ML模型、LangGraph workflow）→ 全局共享
- `@st.cache_data`：缓存**可序列化**数据（DataFrame、JSON）→ 类似物化视图

这里用 `cache_resource`，因为 `build_workflow()` 包含 LLM chain、Python 函数引用，不能 pickle。

### 3. 会话状态初始化

```python
if "messages" not in st.session_state:
    st.session_state.messages = []
```

Streamlit 每次 rerun 重新执行整个脚本。`if not in` 确保首次初始化，后续保留。**类比 ETL 增量表：** 首次建表 + 全量，后续只 INSERT。

```python
st.session_state.thread_id = f"streamlit-{uuid.uuid4().hex[:8]}"
```

`thread_id` 是 LangGraph 的会话隔离标识——每个浏览器 tab 一个 unique ID，多用户对话不串。

### 4. 聊天模块核心

```python
result = wf.invoke({"question": prompt}, make_config(thread_id))
answer = result.get("final_answer", "...")
intent = result.get("intent", "unknown")
```

`wf.invoke()` 是 LangGraph 入口——传入初始 state，自动走 supervisor 路由 → 节点 → finalize。`make_config(thread_id)` 指定会话记忆。

**思考过程可视化（面试亮点）：**
```python
thinking_lines = [f"🎯 意图识别: {intent}"]
if intent in ("rag", "mixed"):
    thinking_lines.append("📚 路由到 RAG 知识库")
```

用户看到的不只是最终答案，还有 Agent 的"思考过程"——这是展示 LangGraph 多Agent 协作的关键。

### 5. 文档管理四步流水线

```python
safe_name = f"{uuid.uuid4().hex[:8]}_{uf.name}"
docs = load_single_file(save_path)      # Extract
chunks = split_documents(docs)           # Transform
create_vectorstore(chunks)               # Load
```

类比 ETL 的 E-T-L 流程，全部在 `st.spinner()` 包裹中。

### 6. 数据查询 + 图表

```python
df = pd.read_sql_query(sql, db._engine)
```

**为什么不用 `db.run()`：** `db.run()` 返回的是 LangChain 格式化后的字符串（如 `"[(1, '产品A'), ...]"`），不能直接转 DataFrame。`pd.read_sql_query` 直接通过 SQLAlchemy engine 获取结构化数据。

```python
st.image(f"data:image/png;base64,{viz_result['image_base64']}")
```

Streamlit 的 `st.image()` 支持 data URI 格式，不需要写临时文件。

### 7. 模式路由（策略模式）

```python
mode_renderers = {
    "💬 智能对话": render_chat,
    "📄 文档管理": render_upload,
    "📊 数据查询": render_query,
}
render_fn = mode_renderers.get(st.session_state.mode, render_chat)
render_fn()
```

字典做路由比 if-elif 链更清晰、更易扩展（开闭原则）。

---

## ⚠️ 常见坑

### 坑1：`st.rerun()` 后代码不执行

`st.rerun()` 立即停止当前执行 + 重新运行整个脚本。后面的代码永远不会执行。必须在 `st.rerun()` 前完成所有状态修改。

### 坑2：`cache_resource` 是全局共享的

多个浏览器 tab 共享同一个 `@st.cache_resource` 实例。必须用 `thread_id` 做会话隔离，否则对话历史会串。

### 坑3：streamlit run 启动慢（BGE 15s+）

`build_workflow()` 内部调用 `load_vectorstore()` 加载 BGE 模型，首次调用 15-30s。面试演示时提前启动服务。

### 坑4：`db.run()` 返回字符串不是 DataFrame

`SafeSQLDatabase.run()` 返回 LangChain 格式化字符串。正确的做法是用 `pd.read_sql_query(sql, db._engine)` 直接获取 DataFrame。

---

## 🎯 面试话术

**30秒版：** "Streamlit 是 Python 的 Web 框架，写纯 Python 代码就能生成网页，不需要学 HTML/JS/CSS。我用它给 AgentFlow 做了聊天界面、文档管理、数据查询三个模块，全部在一个 300 行的 app.py 里完成。"

**3分钟版：** "Streamlit 的核心创新是脚本执行模型——每次用户交互，整个 Python 脚本重新执行一遍。这和传统 Web 框架的事件驱动完全不同。好处是开发者不用管理前端状态、不用写 AJAX 回调。代价是性能——所以提供了 `st.cache_resource` 缓存重资源。我的 AgentFlow 用 `st.chat_message` 实现 ChatGPT 式对话，`st.file_uploader` 处理文档上传，`st.image` 展示 matplotlib 图表。最有价值的设计是'Agent 思考过程可视化'——用户看到的不仅是最终回答，还能看到 LangGraph 的 supervisor 路由决策（intent → rag/sql → mixed/parallel），这直接展示了多 Agent 协作的架构。"

---

## 相关笔记

- [[2026-05-17_tech_langgraph概念讲解]] — LangGraph StateGraph + supervisor 路由
- [[2026-05-17_tech_langgraph-day23-25完善]] — BGE预热 + Mixed双路 + MemorySaver
- [[2026-05-16_tech_prompt-engineering体系]] — Prompt 体系
- [[2026-05-15_tech_text-to-sql-agent详解]] — SQL Agent 实现
