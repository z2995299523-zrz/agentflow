"""
AgentFlow Streamlit WebUI — AI 智能助手平台
三个模块：智能对话(Chat) / 文档管理(Upload) / 数据查询(Query)
"""
import os
import uuid
import io
from pathlib import Path

# ⚠️ matplotlib Agg 后端必须在 import pyplot 之前设置
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

import streamlit as st
import pandas as pd

from config import get_llm_kwargs, UPLOAD_DIR, CHROMA_PERSIST_DIR, get_db_uri
from graph import build_workflow, make_config, clear_memory
from rag.loader import load_single_file, split_documents
from rag.vectorstore import create_vectorstore, load_vectorstore, get_document_count
from sql.agent import SQLQueryAgent, SafeSQLDatabase
from sql.visualizer import QueryVisualizer

# ============================================
# 页面框架
# ============================================
st.set_page_config(page_title="AgentFlow - AI 智能助手", page_icon="🤖", layout="wide")

# ============================================
# 缓存资源（避免每次 rerun 重建）
# ============================================
@st.cache_resource
def get_workflow():
    """缓存 LangGraph 工作流实例"""
    return build_workflow()


@st.cache_resource
def get_agent():
    """缓存 SQLQueryAgent 实例"""
    return SQLQueryAgent()


# ============================================
# 会话状态初始化
# ============================================
if "messages" not in st.session_state:
    st.session_state.messages = []
if "thread_id" not in st.session_state:
    st.session_state.thread_id = f"streamlit-{uuid.uuid4().hex[:8]}"
if "mode" not in st.session_state:
    st.session_state.mode = "💬 智能对话"


def _get_display_name(filename: str) -> str:
    """去掉随机前缀，返回原始文件名（格式: {prefix}_{original_name.ext}）"""
    parts = filename.split("_", 1)
    return parts[1] if len(parts) > 1 else filename


# ============================================
# 侧边栏
# ============================================
with st.sidebar:
    st.title("🤖 AgentFlow")
    st.caption("企业级 AI 智能助手平台")

    mode = st.radio(
        "模式选择",
        ["💬 智能对话", "📄 文档管理", "📊 数据查询"],
        index=["💬 智能对话", "📄 文档管理", "📊 数据查询"].index(st.session_state.mode),
    )
    st.session_state.mode = mode

    st.divider()

    # 清空对话按钮（仅对话模式显示）
    if st.session_state.mode == "💬 智能对话":
        if st.button("🗑️ 清空对话", use_container_width=True):
            st.session_state.messages = []
            clear_memory(st.session_state.thread_id)
            st.rerun()

    st.divider()
    st.caption("v2.0.0")


# ============================================
# 模块1：智能对话
# ============================================
def render_chat():
    """聊天界面 — 调用 LangGraph 工作流进行多 Agent 协作"""
    st.header("💬 智能对话")

    # 知识库状态提示（文件系统检查不触发 BGE 加载）
    upload_dir = Path(UPLOAD_DIR)
    has_uploads = upload_dir.exists() and any(f.is_file() for f in upload_dir.iterdir())
    
    if not has_uploads:
        st.info("📭 知识库为空，上传文档后可使用知识问答功能。切换到「📄 文档管理」上传文档。")
    else:
        try:
            doc_count = get_document_count()
            st.caption(f"📚 知识库已就绪（{doc_count} 个文本块）")
        except Exception:
            st.caption("📚 知识库已就绪")

    # 欢迎消息
    if not st.session_state.messages:
        with st.chat_message("assistant"):
            st.markdown("""
            ### 👋 欢迎使用 AgentFlow

            我可以帮你完成以下任务：

            - **📚 知识问答** — 上传文档后，向我提问文档内容
            - **📊 数据查询** — 用自然语言查询 SQL 数据库
            - **🤖 综合分析** — 同时查文档和数据库，合并回答

            💡 **试试问我**：「产品表有多少条记录？」「总结一下上传的文档内容」
            """)

    # 渲染历史消息
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg.get("thinking"):
                with st.expander("🧠 Agent 思考过程"):
                    st.text(msg["thinking"])

    # 用户输入
    if prompt := st.chat_input("输入你的问题..."):
        st.session_state.messages.append({"role": "user", "content": prompt})

        with st.spinner("🤔 Agent 正在思考..."):
            try:
                wf = get_workflow()
                result = wf.invoke(
                    {"question": prompt},
                    make_config(st.session_state.thread_id),
                )

                answer = result.get("final_answer", "抱歉，未能获取到有效回答。")
                intent = result.get("intent", "unknown")
                rag_result = result.get("rag_result", "")
                sql_result = result.get("sql_result", "")

                # 构造思考过程
                thinking_lines = [f"🎯 意图识别: {intent}"]
                if intent in ("rag", "mixed"):
                    thinking_lines.append("📚 路由到 RAG 知识库")
                if intent in ("sql", "mixed"):
                    thinking_lines.append("🗄️ 路由到 SQL 数据库")
                if intent == "parallel":
                    thinking_lines.append("⚡ 并行路由: RAG ∥ SQL")
                thinking = "\n".join(thinking_lines)

                st.session_state.messages.append({
                    "role": "assistant",
                    "content": answer,
                    "thinking": thinking,
                })

            except Exception as e:
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": f"❌ 处理请求时出错：{e}",
                    "thinking": "",
                })

        st.rerun()


# ============================================
# 模块2：文档管理
# ============================================
def render_upload():
    """文档管理界面 — 上传文档到知识库、查看/删除已有文档"""
    st.header("📄 文档管理")

    os.makedirs(UPLOAD_DIR, exist_ok=True)

    # 上传区域
    st.subheader("📤 上传文档")
    uploaded_files = st.file_uploader(
        "支持 PDF / DOCX / TXT / Markdown 格式",
        type=["pdf", "docx", "txt", "md"],
        accept_multiple_files=True,
    )

    if uploaded_files:
        for uf in uploaded_files:
            # 保存到 uploads 目录（加随机前缀避免重名）
            safe_name = f"{uuid.uuid4().hex[:8]}_{uf.name}"
            save_path = os.path.join(UPLOAD_DIR, safe_name)

            with open(save_path, "wb") as f:
                f.write(uf.getbuffer())

            # 加载 → 分块 → 写入向量库
            try:
                with st.spinner(f"⏳ 正在处理「{uf.name}」..."):
                    docs = load_single_file(save_path)
                    chunks = split_documents(docs)
                    create_vectorstore(chunks)
                    st.success(f"✅「{uf.name}」— {len(chunks)} 个文本块已写入知识库")
            except Exception as e:
                st.error(f"❌ 处理「{uf.name}」失败：{e}")

    st.divider()

    # 文档列表
    st.subheader("📋 已上传文档")

    files = sorted(
        [f for f in os.listdir(UPLOAD_DIR) if os.path.isfile(os.path.join(UPLOAD_DIR, f))],
        key=lambda x: os.path.getmtime(os.path.join(UPLOAD_DIR, x)),
        reverse=True,
    )

    if not files:
        st.info("暂无已上传文档，请先上传。")
        return

    # 加载向量库获取文档计数
    try:
        doc_count = get_document_count()
        st.caption(f"向量库共 {doc_count} 个文本块")
    except Exception:
        pass

    for i, filename in enumerate(files):
        filepath = os.path.join(UPLOAD_DIR, filename)
        display_name = _get_display_name(filename)
        size_kb = os.path.getsize(filepath) / 1024

        col1, col2, col3 = st.columns([5, 2, 2])
        with col1:
            st.write(f"📎 {display_name}")
        with col2:
            st.caption(f"{size_kb:.1f} KB")
        with col3:
            if st.button("🗑️ 删除", key=f"del_{i}"):
                try:
                    os.remove(filepath)
                    st.success(f"已删除「{display_name}」")
                    st.rerun()
                except Exception as e:
                    st.error(f"删除失败：{e}")


# ============================================
# 模块3：数据查询
# ============================================
def render_query():
    """数据查询界面 — 自然语言 → SQL → 图表"""
    st.header("📊 数据查询")

    question = st.text_input(
        "输入你的数据问题",
        placeholder="例如：产品表有多少条记录？销售额最高的5个产品是哪些？",
    )

    if st.button("🔍 查询", type="primary"):
        if not question.strip():
            st.warning("请输入查询问题")
            return

        with st.spinner("⏳ 正在生成 SQL 并查询..."):
            try:
                agent = get_agent()
                result = agent.query(question)

                if not result.get("success"):
                    st.error(f"❌ 查询失败：{result.get('error', '未知错误')}")
                    return

                sql = result.get("sql", "")
                answer = result.get("answer", "")

                # SQL 展示
                if sql:
                    st.subheader("🔍 生成的 SQL")
                    st.code(sql, language="sql")

                # 答案展示
                st.subheader("💬 回答")
                st.write(answer)

                # 图表生成
                if sql:
                    try:
                        db = SafeSQLDatabase.from_uri(get_db_uri())
                        # ⚠️ db.run() 返回字符串，用 read_sql_query 直接获取 DataFrame
                        df = pd.read_sql_query(sql, db._engine)
                        if not df.empty:
                            st.subheader("📊 数据可视化")

                            chart_type = st.radio(
                                "图表类型",
                                ["auto", "bar", "line", "pie"],
                                horizontal=True,
                                key="chart_type",
                            )

                            viz_result = QueryVisualizer.visualize(
                                df, chart_type=chart_type, title=question
                            )
                            if viz_result.get("success"):
                                st.image(
                                    f"data:image/png;base64,{viz_result['image_base64']}",
                                    caption=viz_result.get("title", ""),
                                )
                            else:
                                st.warning(f"图表生成失败：{viz_result.get('error', '')}")
                        else:
                            st.info("查询无返回数据，无法生成图表。")
                    except Exception as e:
                        st.warning(f"图表生成失败：{e}")

                elif not answer.strip():
                    st.info("查询未返回结果，请换个问题试试。")

            except Exception as e:
                st.error(f"❌ 查询过程出错：{e}")


# ============================================
# 渲染当前模式
# ============================================
mode_renderers = {
    "💬 智能对话": render_chat,
    "📄 文档管理": render_upload,
    "📊 数据查询": render_query,
}

render_fn = mode_renderers.get(st.session_state.mode, render_chat)
render_fn()
