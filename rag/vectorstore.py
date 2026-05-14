"""
向量存储 — ChromaDB 集成
文档向量化（本地模型，免费）、存储、相似度检索
"""
import os
from typing import List, Optional

from langchain_community.vectorstores import Chroma
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_core.documents import Document

from config import CHROMA_PERSIST_DIR


# 中文友好的本地 Embedding 模型（首次自动下载，约 100MB）
LOCAL_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"

_embeddings = None


def get_embeddings():
    """
    获取本地 Embedding 模型（单例，避免重复加载）
    使用中文优化模型，免费、离线可用
    """
    global _embeddings
    if _embeddings is None:
        print(f"⏳ 加载本地 Embedding 模型: {LOCAL_EMBEDDING_MODEL} ...")
        _embeddings = HuggingFaceEmbeddings(
            model_name=LOCAL_EMBEDDING_MODEL,
            model_kwargs={"device": "cpu"},
            encode_kwargs={"normalize_embeddings": True},
        )
        print("✅ Embedding 模型加载完成")
    return _embeddings


def create_vectorstore(
    documents: List[Document],
    persist_dir: Optional[str] = None,
) -> Chroma:
    """
    从文档创建向量数据库

    参数:
        documents: 分块后的文档列表
        persist_dir: 持久化目录（默认从配置读取）

    返回:
        Chroma: 向量数据库实例
    """
    if persist_dir is None:
        persist_dir = CHROMA_PERSIST_DIR

    embeddings = get_embeddings()

    vectorstore = Chroma.from_documents(
        documents=documents,
        embedding=embeddings,
        persist_directory=persist_dir,
        collection_name="agentflow_docs",
    )

    print(f"✅ 向量库已创建: {len(documents)} 个文本块 → {persist_dir}")
    return vectorstore


def load_vectorstore(
    persist_dir: Optional[str] = None,
) -> Chroma:
    """
    加载已有的向量数据库

    参数:
        persist_dir: 持久化目录

    返回:
        Chroma: 向量数据库实例
    """
    if persist_dir is None:
        persist_dir = CHROMA_PERSIST_DIR

    embeddings = get_embeddings()

    vectorstore = Chroma(
        embedding_function=embeddings,
        persist_directory=persist_dir,
        collection_name="agentflow_docs",
    )

    return vectorstore


def similarity_search(
    query: str,
    vectorstore: Optional[Chroma] = None,
    top_k: int = 5,
) -> List[Document]:
    """
    相似度检索

    参数:
        query: 查询文本
        vectorstore: 向量库实例
        top_k: 返回 K 个结果

    返回:
        List[Document]: 相关文档片段，按相似度降序
    """
    if vectorstore is None:
        vectorstore = load_vectorstore()

    return vectorstore.similarity_search(query, k=top_k)


def similarity_search_with_score(
    query: str,
    vectorstore: Optional[Chroma] = None,
    top_k: int = 5,
) -> List[tuple]:
    """相似度检索（带分数）"""
    if vectorstore is None:
        vectorstore = load_vectorstore()

    return vectorstore.similarity_search_with_relevance_scores(query, k=top_k)


def delete_collection(persist_dir: Optional[str] = None):
    """删除向量库集合（重置知识库）"""
    if persist_dir is None:
        persist_dir = CHROMA_PERSIST_DIR

    embeddings = get_embeddings()
    vectorstore = Chroma(
        embedding_function=embeddings,
        persist_directory=persist_dir,
        collection_name="agentflow_docs",
    )
    vectorstore.delete_collection()
    print(f"✅ 向量库已清空: {persist_dir}")


def get_document_count(vectorstore: Optional[Chroma] = None) -> int:
    """获取向量库中的文档数量"""
    if vectorstore is None:
        vectorstore = load_vectorstore()

    try:
        return vectorstore._collection.count()
    except Exception:
        return 0
