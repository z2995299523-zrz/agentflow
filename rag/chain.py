"""
问答链 — 基于 LangChain 的 RAG 问答
检索增强生成：检索相关文档 → 拼接上下文 → LLM 生成回答（带来源引用）
"""
from typing import List, Optional, Dict, Any

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.documents import Document
from langchain_core.runnables import RunnablePassthrough
from langchain_core.output_parsers import StrOutputParser

from config import get_llm_kwargs
from rag.vectorstore import load_vectorstore, Chroma
from prompts.rag import RAG_SYSTEM_PROMPT


def get_llm():
    """获取 LLM 实例"""
    kwargs = get_llm_kwargs()
    return ChatOpenAI(**kwargs)


def _format_docs(docs: List[Document]) -> str:
    """将检索到的文档格式化为上下文字符串"""
    parts = []
    for i, doc in enumerate(docs):
        source = doc.metadata.get("source", "未知文件")
        page = doc.metadata.get("page", None)
        label = f"[来源{i+1}: {source}"
        if page is not None:
            label += f" 第{page+1}页"
        label += "]"
        parts.append(f"{label}\n{doc.page_content}")
    return "\n\n---\n\n".join(parts)


def build_qa_chain(vectorstore: Optional[Chroma] = None):
    """
    构建 RAG 问答链（LCEL 现代写法，兼容 LangChain 1.x）

    参数:
        vectorstore: 向量数据库实例

    返回:
        可执行的问答链
    """
    if vectorstore is None:
        vectorstore = load_vectorstore()

    llm = get_llm()
    retriever = vectorstore.as_retriever(search_kwargs={"k": 5})

    prompt = ChatPromptTemplate.from_messages([
        ("system", RAG_SYSTEM_PROMPT),
        ("human", "{question}"),
    ])

    # LCEL 链式组合：检索 → 格式化 → LLM → 输出
    chain = (
        {"context": retriever | _format_docs, "question": RunnablePassthrough()}
        | prompt
        | llm
        | StrOutputParser()
    )

    return chain


def ask(
    question: str,
    qa_chain=None,
    vectorstore: Optional[Chroma] = None,
) -> Dict[str, Any]:
    """
    向知识库提问

    返回:
        {"answer": "LLM 回答", "sources": ["文件1 第X页", ...]}
    """
    if qa_chain is None:
        if vectorstore is None:
            vectorstore = load_vectorstore()
        qa_chain = build_qa_chain(vectorstore)

    # 检索 + 生成
    answer = qa_chain.invoke(question)

    # 单独检索一次获取来源信息
    if vectorstore is None:
        vectorstore = load_vectorstore()
    retriever = vectorstore.as_retriever(search_kwargs={"k": 5})
    docs = retriever.invoke(question)

    sources = []
    seen = set()
    for doc in docs:
        source = doc.metadata.get("source", "未知文件")
        page = doc.metadata.get("page", None)
        label = source + (f" 第{page+1}页" if page is not None else "")
        if label not in seen:
            sources.append(label)
            seen.add(label)

    return {"answer": answer, "sources": sources}


def ask_with_detail(
    question: str,
    qa_chain=None,
    vectorstore: Optional[Chroma] = None,
) -> Dict[str, Any]:
    """提问并返回详细信息（含检索到的原文片段）"""
    if vectorstore is None:
        vectorstore = load_vectorstore()

    result = ask(question, qa_chain, vectorstore)

    retriever = vectorstore.as_retriever(search_kwargs={"k": 5})
    docs = retriever.invoke(question)

    context_details = []
    seen = set()
    for doc in docs:
        source = doc.metadata.get("source", "未知文件")
        page = doc.metadata.get("page", None)
        label = source + (f" 第{page+1}页" if page is not None else "")
        if label not in seen:
            context_details.append({
                "source": label,
                "content": doc.page_content[:300] + ("..." if len(doc.page_content) > 300 else ""),
            })
            seen.add(label)

    result["context_details"] = context_details
    return result
