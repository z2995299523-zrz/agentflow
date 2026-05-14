"""
RAG 模块 — 文档知识库问答
"""
from rag.loader import load_and_split, load_single_file, split_documents
from rag.vectorstore import (
    create_vectorstore,
    load_vectorstore,
    similarity_search,
    delete_collection,
    get_document_count,
)
from rag.chain import build_qa_chain, ask, ask_with_detail

__all__ = [
    # loader
    "load_and_split",
    "load_single_file", 
    "split_documents",
    # vectorstore
    "create_vectorstore",
    "load_vectorstore",
    "similarity_search",
    "delete_collection",
    "get_document_count",
    # chain
    "build_qa_chain",
    "ask",
    "ask_with_detail",
]
