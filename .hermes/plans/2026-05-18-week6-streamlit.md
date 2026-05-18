# Week 6 Day 26-27: Streamlit WebUI 实施计划

> **For Hermes:** Use claude-code-coding skill — delegate to Claude Code, verify, explain.
> **For Claude Code:** This is your complete task spec. Follow exactly.

**Goal:** 为 AgentFlow 创建 Streamlit WebUI，实现聊天式交互、文档上传、数据库查询展示、Agent 思考过程可视化

**Architecture:** 单文件 `app.py`，三模块（Chat/Upload/Query）通过侧边栏切换，直接导入 `graph/`、`rag/`、`sql/` 模块（不通过 FastAPI），使用 `st.session_state` 管理状态

**Tech Stack:** Streamlit 1.34+, Python, matplotlib (Agg backend), base64

---

## 背景（Claude Code 不需要了解，Hermes 使用）

AgentFlow 现有模块：
- `graph/` — LangGraph 多Agent工作流（supervisor路由 → RAG节点/SQL节点 → finalize）
- `rag/` — RAG知识库（loader + vectorstore + chain）
- `sql/` — Text-to-SQL（agent + visualizer + tools）
- `main.py` — FastAPI（7端点，不用于 Streamlit）
- `config.py` — 配置（get_llm_kwargs, get_db_uri, UPLOAD_DIR, CHROMA_PERSIST_DIR）

Streamlit 直接导入上述模块，不经过 FastAPI。

## 技术约束

1. 单文件 `app.py`（YAGNI，后续可拆分）
2. matplotlib 后端必须在 import pyplot 之前设为 `Agg`
3. 直接 `from graph import build_workflow, make_config, clear_memory`
4. 直接 `from rag.loader import load_single_file, split_documents`
5. 直接 `from rag.vectorstore import create_vectorstore, load_vectorstore`
6. 直接 `from sql.agent import SQLQueryAgent`
7. 直接 `from sql.visualizer import QueryVisualizer`
8. 使用 `st.session_state` 管理聊天历史、模式状态
9. 所有 LLM 调用通过 `config.get_llm_kwargs()` 自动获取 DeepSeek 配置
10. BGE 模型首次加载需 15s+，app.py 需要 import 时自动加载，用户等待属正常现象

---

## Task 1: 创建 app.py 基础框架

**Objective:** 搭建 Streamlit 页面框架：标题、侧边栏模式选择器、三模块占位

**Files:**
- Create: `C:\Users\DELL\agentflow\app.py`

**Requirements:**

### 页面配置
```python
st.set_page_config(
    page_title="AgentFlow - AI 智能助手",
    page_icon="🤖",
    layout="wide",
)
```

### 侧边栏
- 标题 "AgentFlow 🤖"
- 副标题 "企业级 AI 智能助手"
- 分隔线
- 模式选择 radio: "💬 智能对话" / "📄 文档管理" / "📊 数据查询"
- 分隔线
- 显示已加载状态（向量库文档数、数据库表数）
- 底部版本号 "v2.0.0"

### 主区域
- 根据 mode 显示不同内容：
  - `chat` → Chat 界面
  - `upload` → 文档管理界面
  - `query` → 数据查询界面

**Verification:** 运行 `streamlit run app.py`，页面加载，侧边栏可切换模式

---

## Task 2: 实现 Chat 模块（核心）

**Objective:** 实现类 ChatGPT 对话界面，后端调用 LangGraph 工作流

**Requirements:**

### 显示对话历史（st.session_state.messages）
```python
if "messages" not in st.session_state:
    st.session_state.messages = []  # [{"role": "user/assistant", "content": "...", "thinking": "..."}]
if "thread_id" not in st.session_state:
    st.session_state.thread_id = f"streamlit-{uuid.uuid4().hex[:8]}"
```

### 消息渲染
- 用 `st.chat_message("user")` 和 `st.chat_message("assistant")` 渲染历史
- assistant 消息下方用 `st.expander("🧠 Agent 思考过程")` 展示路由过程

### 输入框
- `st.chat_input("输入你的问题...")`
- 用户输入后：
  1. 添加到 messages
  2. 调用 `graph/build_workflow().invoke({"question": user_input}, config)`
  3. 从结果提取 `final_answer`、`intent`、`rag_result`、`sql_result`
  4. 构造思考过程文本
  5. 添加到 messages 并 rerun

### 思考过程展示
```python
thinking_parts = []
thinking_parts.append(f"🎯 意图识别: **{intent}**")
if intent == "rag":
    thinking_parts.append(f"📚 路由到 **RAG 知识库**")
elif intent == "sql":
    thinking_parts.append(f"🗄️ 路由到 **SQL 数据库**")
elif intent == "mixed":
    thinking_parts.append("🔀 混合路由: RAG + SQL 串联")
elif intent == "parallel":
    thinking_parts.append("⚡ 并行路由: RAG ∥ SQL")
thinking = "\n\n".join(thinking_parts)
```

### 清空对话按钮
- 侧边栏底部 "🗑️ 清空对话" 按钮
- 点击后 `st.session_state.messages = []` + `clear_memory(thread_id)`

### 工作流初始化
- 延迟加载 `build_workflow()`（避免启动时阻塞）
- 用 `st.cache_resource` 装饰器缓存 workflow 实例

```python
@st.cache_resource
def get_workflow():
    return build_workflow()
```

**Verification:** 提问"产品表有多少条记录"，看到回答 + 思考过程（意图: sql, 路由: SQL数据库）

---

## Task 3: 实现文档管理模块

**Objective:** 实现文档上传、列表展示、删除功能

**Requirements:**

### 上传区域
- `st.file_uploader("上传文档到知识库", type=["pdf", "docx", "txt", "md"])`
- 上传后：
  1. 保存到 `config.UPLOAD_DIR`
  2. 调用 `load_single_file()` + `split_documents()`
  3. 调用 `create_vectorstore()`
  4. 显示成功提示（文件名 + 块数）

### 文档列表
- 扫描 `config.UPLOAD_DIR` 显示已上传文档
- 每行：文件名（去掉随机前缀） + 删除按钮
- 用 `st.dataframe` 或表格展示

### 删除功能
- 每行一个 🗑️ 按钮
- 点击后删除文件 + 提示（向量库需重建才能真正清除）

### 状态刷新
- 上传/删除后刷新列表

**Verification:** 上传一个 TXT 文件 → 列表显示 → 删除后消失

---

## Task 4: 实现数据查询模块

**Objective:** 实现自然语言查数据库 + 结果表格 + 图表展示

**Requirements:**

### 输入区
- `st.text_input("输入数据查询问题...")` + 查询按钮
- placeholder: "例如：销售额最高的10个产品"

### 查询执行
```python
from sql.agent import SQLQueryAgent
agent = SQLQueryAgent()
result = agent.query(question)
```

### 结果展示
- 如果成功：
  - **生成的 SQL**：`st.code(sql, language="sql")`
  - **查询结果**：`st.write(answer)`
  
### 图表生成
- 查询成功且有 SQL 后，自动执行 SQL 获取 DataFrame
- 用 `QueryVisualizer.visualize(df)` 生成图表
- 显示 `st.image(base64)` 展示图表

### 错误处理
- HTTPException → `st.error()`
- SQL 执行失败 → `st.warning()`
- 空结果 → `st.info()`

### 数据库信息
- 侧边栏显示数据库表结构（调用 `agent.get_schema()`）

**Verification:** 查询"产品表有多少条记录" → 显示 SQL + 答案 + 图表

---

## Task 5: 启动说明 + 用户体验打磨

**Objective:** 完善用户体验细节

**Requirements:**

### 首页（默认 Chat 模式）
- 如果没有消息历史，显示欢迎语：
  ```
  👋 你好！我是 AgentFlow 智能助手
  
  我可以：
  - 📚 回答知识库中的问题（需先在「文档管理」上传文档）
  - 📊 查询分析数据库（切换到「数据查询」或直接问数据问题）
  - 🔀 同时查文档和数据库进行综合分析
  
  直接在下方输入你的问题开始吧！
  ```

### 加载状态
- 调用 LangGraph / SQL Agent 时用 `st.spinner("Agent 思考中...")` 包裹
- 或 `st.status()` 显示中间步骤

### 错误容错
- 向量库为空时 RAG 查询给出友好提示
- SQL 生成失败时显示原始错误
- 所有 try/except 包裹

### 移动端响应
- `layout="wide"` 足够好，不做额外适配

**Verification:** 首次打开显示欢迎语，提问时显示 spinner，空库查询有友好提示

---

## 验证清单（Hermes 验收用）

- [ ] `streamlit run app.py` 能启动
- [ ] Chat 模式：提问数据问题 → 返回答案 + 思考过程
- [ ] Chat 模式：提问文档问题（需先上传文档）→ 返回答案
- [ ] 文档管理：上传文件 → 列表显示
- [ ] 数据查询：输入问题 → 显示 SQL + 答案
- [ ] 三模式切换正常
- [ ] 清空对话正常
- [ ] 无 import 错误
- [ ] matplotlib Agg 后端正确设置
