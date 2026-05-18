# AgentFlow Streamlit WebUI — 编码任务

创建 `C:\Users\DELL\agentflow\app.py`，实现三个模块的 Streamlit WebUI：

## 技术约束（必须遵守）

1. 单文件 `app.py`，不要拆分多个文件
2. matplotlib 在后端设置必须在 import matplotlib.pyplot 之前：
```python
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
```
3. 所有 LLM 配置通过 `from config import get_llm_kwargs` 获取，不硬编码
4. 直接导入项目模块（不通过 FastAPI）：
   - `from graph import build_workflow, make_config, clear_memory`
   - `from rag.loader import load_single_file, split_documents`
   - `from rag.vectorstore import create_vectorstore, load_vectorstore, get_document_count`
   - `from rag.chain import ask, ask_with_detail`
   - `from sql.agent import SQLQueryAgent`
   - `from sql.visualizer import QueryVisualizer`
   - `from config import UPLOAD_DIR, CHROMA_PERSIST_DIR, get_db_uri`
5. 使用 `@st.cache_resource` 缓存 workflow 和 SQLAgent 实例（避免每次 rerun 重建）
6. 处理向量库为空、SQL 查询失败等边界情况，给用户友好提示

## 功能需求

### 页面框架
- `st.set_page_config(page_title="AgentFlow - AI 智能助手", page_icon="🤖", layout="wide")`
- 侧边栏：模式选择 radio ("💬 智能对话" / "📄 文档管理" / "📊 数据查询")，保存到 `st.session_state.mode`
- 侧边栏底部：版本号 "v2.0.0"

### 模块1：智能对话 (Chat)

聊天界面，调用 LangGraph 工作流：

1. **会话状态**：
   - `st.session_state.messages = []` — [{role, content, thinking}]
   - `st.session_state.thread_id = f"streamlit-{uuid.uuid4().hex[:8]}"`

2. **消息渲染**：
   - 遍历 messages，`st.chat_message(role)` 渲染
   - assistant 消息下方 `st.expander("🧠 Agent 思考过程")` 展示 thinking

3. **用户输入处理** (`st.chat_input`)：
   - 用户输入 → 添加到 messages
   - 调用 `get_workflow().invoke({"question": question}, make_config(thread_id))`
   - 提取 result["final_answer"], result["intent"], result.get("rag_result", ""), result.get("sql_result", "")
   - 构造 thinking 文本：
     ```
     🎯 意图识别: {intent}
     📚 路由到 RAG 知识库  (if rag/mixed)
     🗄️ 路由到 SQL 数据库  (if sql/mixed)
     ⚡ 并行路由: RAG ∥ SQL (if parallel)
     ```
   - 添加到 messages 并 `st.rerun()`

4. **清空对话**：侧边栏底部按钮，清空 messages + 调用 `clear_memory(thread_id)`

5. **Welcome 消息**：如果 messages 为空，显示欢迎语介绍三种能力

### 模块2：文档管理 (Upload)

1. **上传**：`st.file_uploader("上传文档到知识库", type=["pdf","docx","txt","md"])`
   - 上传后保存到 UPLOAD_DIR
   - `load_single_file()` → `split_documents()` → `create_vectorstore()`
   - 显示成功提示 (文件名 + chunks数)

2. **文档列表**：扫描 UPLOAD_DIR，用表格显示，每行有删除按钮
   - 文件名去掉随机前缀（`*_originalname` → `originalname`）
   - 删除后刷新列表

### 模块3：数据查询 (Query)

1. **输入**：`st.text_input` + 查询按钮
2. **执行**：`SQLQueryAgent().query(question)`
3. **结果展示**：
   - SQL: `st.code(sql, language="sql")`
   - 答案: `st.write(answer)`
4. **图表生成**（如果有 SQL）：
   - 执行 SQL 获取 DataFrame
   - `QueryVisualizer.visualize(df)` 生成图表
   - `st.image(base64)` 显示
5. **错误处理**：空结果/失败给出友好提示

## 重要：导入顺序

```python
# 1. stdlib
import os, uuid, base64, io
from pathlib import Path

# 2. matplotlib (Agg must be set FIRST)
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# 3. third-party
import streamlit as st
import pandas as pd

# 4. project modules
from config import get_llm_kwargs, UPLOAD_DIR, CHROMA_PERSIST_DIR, get_db_uri
from graph import build_workflow, make_config, clear_memory
from rag.loader import load_single_file, split_documents
from rag.vectorstore import create_vectorstore, load_vectorstore, get_document_count
from sql.agent import SQLQueryAgent
from sql.visualizer import QueryVisualizer
```

## 文件改动范围

- **新建**: `C:\Users\DELL\agentflow\app.py`
- **不修改**其他任何文件

## 交付标准

1. 代码完整，无语法错误
2. 三个模块的 UI 逻辑正确
3. 错误处理完善（try/except + 友好提示）
4. 导入正确，不引用不存在的函数/变量
5. `streamlit run app.py` 可以启动（即使 BGE 模型加载需要等待）
