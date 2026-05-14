"""
文档加载器 — 支持 PDF、Word(DOCX)、TXT、Markdown
自动中文友好的文本分块，为向量化做准备
"""
import os
from typing import List

from langchain_community.document_loaders import (
    PyPDFLoader,
    Docx2txtLoader,
    TextLoader,
    UnstructuredMarkdownLoader,
)
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document


# 支持的文件类型
SUPPORTED_EXTENSIONS = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".txt": "txt",
    ".md": "markdown",
}

# 中文友好的分隔符优先级
CHINESE_SEPARATORS = [
    "\n\n",    # 段落
    "\n",      # 换行
    "。",      # 中文句号
    "；",      # 中文分号
    "，",      # 中文逗号
    ".",       # 英文句号
    ";",       # 英文分号
    ",",       # 英文逗号
    " ",       # 空格
    "",        # 字符级别
]


def detect_file_type(file_path: str) -> str:
    """根据扩展名检测文件类型"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"不支持的文件类型: {ext}。支持: {list(SUPPORTED_EXTENSIONS.keys())}")
    return SUPPORTED_EXTENSIONS[ext]


def load_single_file(file_path: str) -> List[Document]:
    """
    加载单个文件，返回 LangChain Document 列表
    
    参数:
        file_path: 文件路径
    
    返回:
        List[Document]: 每页/每段作为一个 Document
    """
    file_type = detect_file_type(file_path)
    file_path = os.path.abspath(file_path)
    
    if file_type == "pdf":
        # PyPDFLoader 按页加载
        loader = PyPDFLoader(file_path)
        documents = loader.load()
        # 过滤掉空白页
        documents = [doc for doc in documents if doc.page_content.strip()]
        
    elif file_type == "docx":
        loader = Docx2txtLoader(file_path)
        documents = loader.load()
        
    elif file_type == "txt":
        # TextLoader 默认 UTF-8，自动检测编码
        loader = TextLoader(file_path, encoding="utf-8")
        try:
            documents = loader.load()
        except UnicodeDecodeError:
            # UTF-8 失败则尝试 GBK（中文 Windows 常见编码）
            loader = TextLoader(file_path, encoding="gbk")
            documents = loader.load()
        
    elif file_type == "markdown":
        loader = UnstructuredMarkdownLoader(file_path)
        documents = loader.load()
        
    else:
        raise ValueError(f"未知文件类型: {file_type}")
    
    # 给每个 Document 添加来源元数据
    file_name = os.path.basename(file_path)
    for doc in documents:
        doc.metadata["source"] = file_name
        doc.metadata["file_path"] = file_path
        doc.metadata["file_type"] = file_type
    
    return documents


def load_documents(file_paths: List[str]) -> List[Document]:
    """
    批量加载多个文件
    
    参数:
        file_paths: 文件路径列表
    
    返回:
        List[Document]: 所有文件的 Document 合并列表
    """
    all_docs = []
    for fp in file_paths:
        try:
            docs = load_single_file(fp)
            all_docs.extend(docs)
        except Exception as e:
            print(f"⚠️ 加载文件失败 [{fp}]: {e}")
            continue
    return all_docs


def split_documents(
    documents: List[Document],
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
) -> List[Document]:
    """
    将文档切分为适合向量化的文本块（中文友好）

    参数:
        documents: 原始文档列表
        chunk_size: 每个文本块的最大字符数（默认 1000）
        chunk_overlap: 相邻文本块的重叠字符数（默认 200）

    返回:
        List[Document]: 切分后的文本块列表
    """
    splitter = RecursiveCharacterTextSplitter(
        separators=CHINESE_SEPARATORS,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        length_function=len,
        is_separator_regex=False,
    )
    
    chunks = splitter.split_documents(documents)
    
    # 过滤掉太短的碎片（纯空白或仅有标点）
    chunks = [c for c in chunks if len(c.page_content.strip()) >= 20]
    
    return chunks


def load_and_split(
    file_paths: List[str],
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
) -> List[Document]:
    """
    一站式：加载文件 + 分块
    
    参数:
        file_paths: 文件路径列表
        chunk_size: 分块大小
        chunk_overlap: 重叠大小
    
    返回:
        List[Document]: 分块后的文档列表
    """
    documents = load_documents(file_paths)
    if not documents:
        print("⚠️ 没有成功加载任何文档")
        return []
    
    chunks = split_documents(documents, chunk_size, chunk_overlap)
    print(f"✅ 加载了 {len(documents)} 个文档片段 → 切分为 {len(chunks)} 个文本块")
    return chunks
