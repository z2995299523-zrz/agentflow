"""
向量索引
字段文本（表中文名、字段中文名、口径说明）和样例文本（需求、口径）的向量化与检索。
向量和元数据存在同一个元数据库里，检索时用 numpy 算全量相似度。

向量器可以替换：默认用本地的 BGE 模型；环境变量 METADATA_EMBEDDER=hash 时用哈希向量器，
单元测试一律用它，不加载模型。

骨架：接口已定，实现由第二份计划的 C 任务补上
"""
from collections.abc import Collection, Sequence
from typing import Protocol

import numpy as np

from metadata.checks import ReportSection
from metadata.models import ColumnMeta, Example, Mart, TableMeta
from metadata.store import VectorBundle, VectorSet

EMBEDDER_ENV = "METADATA_EMBEDDER"     # bge（默认）或 hash
SECTION_VECTORS = "向量"


class Embedder(Protocol):
    """向量器：把一批文本变成 L2 归一化的 float32 向量"""

    name: str
    dim: int

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """返回形状为 (文本条数, dim) 的 float32 矩阵，每行已 L2 归一化"""
        ...


class HashEmbedder:
    """哈希向量器：中文单字、二元组和英文整词散列到固定维度，结果确定、不需要模型"""

    def __init__(self, dim: int = 512) -> None:
        raise NotImplementedError


class BgeEmbedder:
    """本地 BGE 模型（bge-small-zh-v1.5），第一次用到时才加载，进程内只加载一次"""

    def __init__(self) -> None:
        raise NotImplementedError


class _PendingEmbedder:
    """骨架阶段的占位向量器，C 任务实现后删除"""

    name = "pending"
    dim = 0

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        raise NotImplementedError


def default_embedder() -> Embedder:
    """按环境变量 METADATA_EMBEDDER 取默认向量器，每次调用都重新读环境变量"""
    return _PendingEmbedder()


def field_ref_id(table_id: str, column_name: str) -> str:
    """字段向量的编号：模式.表.字段"""
    raise NotImplementedError


def field_text(table: TableMeta, column: ColumnMeta) -> str:
    """字段向量化用的文本：表中文名、字段中文名、口径说明"""
    raise NotImplementedError


def example_text(example: Example) -> str:
    """样例向量化用的文本：需求、口径"""
    raise NotImplementedError


def build_vectors(mart: Mart, embedder: Embedder, batch_size: int = 256) -> tuple[VectorBundle, ReportSection]:
    """为全部字段和样例算向量

    Args:
        mart: 已经补全的资料
        embedder: 向量器
        batch_size: 每批向量化的文本条数

    Returns:
        (向量, 报告节)
    """
    return VectorBundle(embedder=embedder.name, dim=embedder.dim, sets={}), ReportSection(SECTION_VECTORS, ())


class VectorIndex:
    """一类文档向量上的全量相似度检索"""

    def __init__(self, vector_set: VectorSet) -> None:
        raise NotImplementedError

    def search(self, query: np.ndarray, k: int,
               allowed: Collection[str] | None = None) -> list[tuple[str, float]]:
        """返回最相似的 k 条：(文档编号, 余弦相似度)，按相似度降序，同分按编号

        Args:
            query: 查询向量，已 L2 归一化
            k: 返回条数
            allowed: 只在这些编号里找；为 None 时不限
        """
        raise NotImplementedError
