"""
向量索引
字段文本（表中文名、字段中文名、口径说明）和样例文本（需求、口径）的向量化与检索。
向量和元数据存在同一个元数据库里，检索时用 numpy 算全量相似度，不用向量数据库。

向量器可以替换：默认用本地的 BGE 模型；环境变量 METADATA_EMBEDDER=hash 时用哈希向量器，
单元测试一律用它，不加载模型。
"""
import math
import os
import re
import threading
import time
import unicodedata
import zlib
from collections.abc import Collection, Iterator, Sequence
from functools import lru_cache
from typing import Any, Protocol

import numpy as np

from metadata.checks import ReportSection
from metadata.models import ColumnMeta, Example, Mart, TableMeta
from metadata.store import VectorBundle, VectorSet

EMBEDDER_ENV = "METADATA_EMBEDDER"     # bge（默认）或 hash
SECTION_VECTORS = "向量"
FIELD_KIND = "field"                   # 向量集的类别：字段
EXAMPLE_KIND = "example"               # 向量集的类别：样例
NORM_TOLERANCE = 1e-3                  # 向量长度与 1 相差不超过这个数，就认为已经归一化

# 哈希向量器
HASH_DIM = 512                         # 与 BGE 的维度相同
CHAR_WEIGHT = 0.5                      # 汉字单字的权重
BIGRAM_WEIGHT = 1.0                    # 相邻两个汉字的权重
WORD_WEIGHT = 1.0                      # ASCII 整词的权重
SIGN_SALT = b"sign:"                   # 算正负号时加在特征前面，让它和桶号用的是两个不同的 crc
CJK_RANGES = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"           # 汉字：扩展 A、基本区、兼容区
TOKEN_PATTERN = re.compile(f"[{CJK_RANGES}]+|[a-z0-9_]+")        # 一段连续的汉字，或者一个 ASCII 整词

# BGE 模型
BGE_MODEL_ID = "BAAI/bge-small-zh-v1.5"
BGE_NAME = "bge-small-zh-v1.5"
BGE_DIM = 512
BGE_ENCODE_BATCH = 128                 # 模型一次前向计算的文本条数
HF_MIRROR = "https://hf-mirror.com"    # huggingface.co 国内连不上，与 config.py 保持一致
NO_INDEX_HINT = "导入时加上 --no-index 可以跳过向量（召回将只能走关键字）"

_BGE_LOCK = threading.Lock()


class Embedder(Protocol):
    """向量器：把一批文本变成 L2 归一化的 float32 向量"""

    name: str
    dim: int

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """返回形状为 (文本条数, dim) 的 float32 矩阵，每行已 L2 归一化"""
        ...


@lru_cache(maxsize=1 << 16)
def _slot(feature: str, dim: int) -> tuple[int, float]:
    """特征落进哪个桶、取正号还是负号

    用 crc32 而不是 Python 的 hash()：后者的种子每个进程都不一样，同一段文本两次运行会得到不同的向量。
    """
    data = feature.encode("utf-8")
    bucket = zlib.crc32(data) % dim
    sign = -1.0 if zlib.crc32(SIGN_SALT + data) >> 31 else 1.0      # 另一个 crc 的最高位决定正负
    return bucket, sign


def _features(text: str) -> Iterator[tuple[str, float]]:
    """把文本拆成带权重的特征：汉字单字、汉字二元组、ASCII 整词

    先做 NFKC 归一（全角转半角）并转小写。二元组只在连续的汉字之间取，标点、空格、英文都会把它隔断。
    """
    for run in TOKEN_PATTERN.findall(unicodedata.normalize("NFKC", text).lower()):
        if run.isascii():
            yield run, WORD_WEIGHT
            continue
        for char in run:
            yield char, CHAR_WEIGHT
        for start in range(len(run) - 1):
            yield run[start:start + 2], BIGRAM_WEIGHT


class HashEmbedder:
    """哈希向量器：中文单字、二元组和英文整词散列到固定维度，结果确定、不需要模型

    只用于测试和没有模型的环境：它度量的是字面上的重叠，不懂同义词，不能拿来评价召回效果。
    """

    def __init__(self, dim: int = HASH_DIM) -> None:
        """
        Args:
            dim: 向量维度，也就是桶的个数

        Raises:
            ValueError: 维度不是正整数
        """
        if dim < 1:
            raise ValueError(f"向量维度必须是正整数，收到 {dim}")
        self.dim = dim
        self.name = f"hash-bigram-{dim}"

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """把文本变成向量，空文本（或只有标点的文本）得到零向量

        Args:
            texts: 文本列表

        Returns:
            形状为 (文本条数, dim) 的 float32 矩阵，非零行已 L2 归一化
        """
        matrix = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            weights: dict[int, float] = {}
            for feature, weight in _features(text):
                bucket, sign = _slot(feature, self.dim)
                weights[bucket] = weights.get(bucket, 0.0) + sign * weight
            norm = math.sqrt(sum(value * value for value in weights.values()))
            if norm > 0.0:
                for bucket, value in weights.items():
                    matrix[row, bucket] = value / norm
        return matrix


def _detect_device() -> str:
    """有 CUDA 就用 CUDA，否则用 CPU"""
    try:
        import torch
    except ImportError:
        return "cpu"
    return "cuda" if torch.cuda.is_available() else "cpu"


def _load_bge_model() -> Any:
    """加载 BGE 模型：先只用本地缓存，缓存里没有再联网下载

    Raises:
        RuntimeError: 没有安装 sentence-transformers，或者缓存里没有、联网也下载不下来
    """
    # 与 config.py 一样：huggingface_hub 在导入时读取这个变量，必须赶在导入之前设置
    if not os.getenv("HF_ENDPOINT"):
        os.environ["HF_ENDPOINT"] = HF_MIRROR
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as error:
        raise RuntimeError(f"没有安装 sentence-transformers，无法加载向量模型。{NO_INDEX_HINT}") from error

    # 必须先导入 sentence_transformers 再探测设备。实测在 Windows 上，进程里已经导入了 openpyxl 时，
    # 若先初始化 CUDA 再导入 sentence_transformers，导入 pyarrow 那一步会内存访问冲突，进程直接崩溃
    device = _detect_device()
    try:
        return SentenceTransformer(BGE_MODEL_ID, device=device, local_files_only=True)
    except Exception as offline_error:       # 缓存里没有（各个版本抛的异常类型不同），再试联网
        try:
            return SentenceTransformer(BGE_MODEL_ID, device=device)
        except Exception as online_error:
            raise RuntimeError(
                f"向量模型 {BGE_MODEL_ID} 加载失败。本地缓存：{offline_error}；联网下载：{online_error}。"
                f"{NO_INDEX_HINT}") from online_error


class BgeEmbedder:
    """本地 BGE 模型（bge-small-zh-v1.5），第一次用到时才加载，进程内只加载一次

    本类本身也是单例。导入 sentence_transformers 要好几秒，所以创建对象、传空列表都不会触发加载。
    """

    name = BGE_NAME
    dim = BGE_DIM
    _instance: "BgeEmbedder | None" = None
    _model: Any = None

    def __new__(cls) -> "BgeEmbedder":
        with _BGE_LOCK:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
            return cls._instance

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """把文本变成向量

        Args:
            texts: 文本列表

        Returns:
            形状为 (文本条数, 512) 的 float32 矩阵，每行已 L2 归一化

        Raises:
            RuntimeError: 模型加载失败，信息里说明可以加 --no-index
        """
        if len(texts) == 0:
            return np.empty((0, self.dim), dtype=np.float32)
        with _BGE_LOCK:
            if self._model is None:
                self._model = _load_bge_model()
            model = self._model
        vectors = model.encode(list(texts), batch_size=BGE_ENCODE_BATCH, normalize_embeddings=True,
                               convert_to_numpy=True, show_progress_bar=False)
        return np.ascontiguousarray(vectors, dtype=np.float32)


def default_embedder() -> Embedder:
    """按环境变量 METADATA_EMBEDDER 取默认向量器，每次调用都重新读环境变量

    Returns:
        值为 hash 时是新建的哈希向量器；值为 bge 或没设置时是 BGE 单例（此时还没有加载模型）

    Raises:
        ValueError: 环境变量的值不认识
    """
    choice = os.environ.get(EMBEDDER_ENV, "").strip().lower()
    if choice == "hash":
        return HashEmbedder()
    if choice in ("", "bge"):
        return BgeEmbedder()
    raise ValueError(f"环境变量 {EMBEDDER_ENV} 的值「{os.environ[EMBEDDER_ENV]}」不认识，可选 bge 或 hash")


def field_ref_id(table_id: str, column_name: str) -> str:
    """字段向量的编号：模式.表.字段"""
    return f"{table_id}.{column_name}"


def field_text(table: TableMeta, column: ColumnMeta) -> str:
    """字段向量化用的文本：表中文名、字段中文名、口径说明，用空格连接，没有口径说明就省去

    字段名在很多表里都叫「余额」「客户号」，带上表中文名才分得出是哪张表的字段。
    """
    return " ".join(part for part in (table.table_cn, column.column_cn, column.description) if part)


def example_text(example: Example) -> str:
    """样例向量化用的文本：需求、口径，用空格连接，没有口径就省去"""
    return " ".join(part for part in (example.requirement, example.caliber_note) if part)


def _unit_rows(matrix: np.ndarray) -> np.ndarray:
    """保证每一行长度为 1

    向量器按约定已经归一化，长度在容差内的行原样保留（逐位不变）；偏离明显的才重新归一化。零向量保持为零。
    """
    norms = np.linalg.norm(matrix, axis=1)
    loose = (norms > 0) & (np.abs(norms - 1.0) > NORM_TOLERANCE)
    if loose.any():
        matrix = matrix.copy()
        matrix[loose] /= norms[loose, None]
    return matrix


def _embed_all(embedder: Embedder, texts: Sequence[str], batch_size: int) -> np.ndarray:
    """分批向量化，拼成一个 float32 矩阵，行的顺序与 texts 一致

    Raises:
        ValueError: 向量器返回的形状与它声明的维度对不上，或者含有 NaN、无穷大
    """
    if not texts:
        return np.empty((0, embedder.dim), dtype=np.float32)
    parts = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]
        part = np.asarray(embedder.embed(batch), dtype=np.float32)
        if part.shape != (len(batch), embedder.dim):
            raise ValueError(
                f"向量器 {embedder.name} 声明维度是 {embedder.dim}，{len(batch)} 条文本却得到形状 {part.shape}，"
                "维度对不上")
        if not np.isfinite(part).all():
            raise ValueError(f"向量器 {embedder.name} 返回了 NaN 或无穷大")
        parts.append(_unit_rows(part))
    return np.concatenate(parts)


def build_vectors(mart: Mart, embedder: Embedder, batch_size: int = 256) -> tuple[VectorBundle, ReportSection]:
    """为全部字段和样例算向量

    字段向量的顺序与 mart.columns 一致，样例向量的顺序与 mart.examples 一致，同样的资料每次得到同样的结果。
    没有样例时不生成样例集。表清单里没有的表下的字段（正常导入时不会出现）没法拼字段文本，直接跳过。

    Args:
        mart: 已经补全的资料
        embedder: 向量器
        batch_size: 每批向量化的文本条数

    Returns:
        (向量, 报告节)

    Raises:
        ValueError: batch_size 不是正整数，或向量器返回的结果不合格
    """
    if batch_size < 1:
        raise ValueError(f"batch_size 必须是正整数，收到 {batch_size}")
    started = time.perf_counter()

    tables = {table.table_id: table for table in mart.tables}
    field_ids: list[str] = []
    field_texts: list[str] = []
    for column in mart.columns:
        table = tables.get(column.table_id)
        if table is not None:
            field_ids.append(field_ref_id(column.table_id, column.column_name))
            field_texts.append(field_text(table, column))
    sets = {FIELD_KIND: VectorSet(kind=FIELD_KIND, ref_ids=field_ids, texts=field_texts,
                                  matrix=_embed_all(embedder, field_texts, batch_size))}
    if mart.examples:
        example_texts = [example_text(example) for example in mart.examples]
        sets[EXAMPLE_KIND] = VectorSet(kind=EXAMPLE_KIND, ref_ids=[example.example_id for example in mart.examples],
                                       texts=example_texts, matrix=_embed_all(embedder, example_texts, batch_size))

    elapsed = time.perf_counter() - started
    section = ReportSection(SECTION_VECTORS, (
        f"- 向量器：{embedder.name}",
        f"- 维度：{embedder.dim}",
        f"- 字段向量：{len(field_ids)} 条",
        f"- 样例向量：{len(mart.examples)} 条",
        f"- 耗时：{elapsed:.2f} 秒",
    ))
    return VectorBundle(embedder=embedder.name, dim=embedder.dim, sets=sets), section


class VectorIndex:
    """一类文档向量上的全量相似度检索

    不建近似索引：一两万条 512 维的向量，扫一遍只要几毫秒，近似索引省不下什么，还会让结果不确定。
    """

    def __init__(self, vector_set: VectorSet) -> None:
        """
        Args:
            vector_set: 一类文档的向量，矩阵已 L2 归一化

        Raises:
            ValueError: 矩阵不是二维的，或者行数与编号个数对不上
        """
        matrix = np.ascontiguousarray(vector_set.matrix, dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[0] != len(vector_set.ref_ids):
            raise ValueError(f"向量矩阵的形状 {matrix.shape} 与 {len(vector_set.ref_ids)} 个编号对不上")
        self._matrix = matrix
        self._ref_ids = list(vector_set.ref_ids)
        self._row_of = {ref_id: row for row, ref_id in enumerate(self._ref_ids)}
        # 每个编号在升序排列中的名次，同分时拿它定先后
        self._rank = np.empty(len(self._ref_ids), dtype=np.int64)
        self._rank[sorted(range(len(self._ref_ids)), key=self._ref_ids.__getitem__)] = np.arange(len(self._ref_ids))

    def search(self, query: np.ndarray, k: int,
               allowed: Collection[str] | None = None) -> list[tuple[str, float]]:
        """返回最相似的 k 条：(文档编号, 余弦相似度)，按相似度降序，同分按编号

        Args:
            query: 查询向量，一维，已 L2 归一化
            k: 返回条数，大于文档总数时全部返回，不大于 0 时返回空列表
            allowed: 只在这些编号里找，先过滤再取前 k 条，不认识的编号忽略；为 None 时不限

        Returns:
            (文档编号, 余弦相似度) 的列表

        Raises:
            ValueError: 查询向量不是一维的，或者维度与索引对不上（多半是换了向量器）
        """
        vector = np.asarray(query, dtype=np.float32)
        if vector.shape != (self._matrix.shape[1],):
            raise ValueError(
                f"查询向量的形状是 {vector.shape}，索引的维度是 {self._matrix.shape[1]}，"
                "维度对不上，可能不是同一个向量器算出来的")
        if k <= 0 or not self._ref_ids:
            return []

        # 不直接用 @：BLAS 对不同位置的行，累加顺序可能不同，两行完全相同的向量得分会差最后一位，
        # 「同分按编号」就乱了。einsum 对每一行都按同一种顺序累加，相同的行得分逐位相同
        scores = np.einsum("ij,j->i", self._matrix, vector)
        ranks = self._rank
        rows = None
        if allowed is not None:
            rows = np.array(sorted({self._row_of[ref_id] for ref_id in allowed if ref_id in self._row_of}),
                            dtype=np.intp)
            if rows.size == 0:
                return []
            scores, ranks = scores[rows], ranks[rows]

        count = len(scores)
        if k < count:
            # 粗筛：第 k 名的得分以上都留下（包括与第 k 名同分的），再精排，这样平局压在边界上也能按编号取
            threshold = np.partition(scores, count - k)[count - k]
            candidates = np.flatnonzero(scores >= threshold)
        else:
            candidates = np.arange(count)
        chosen = candidates[np.lexsort((ranks[candidates], -scores[candidates]))][:k]    # 先按得分降序，再按名次升序
        matrix_rows = chosen if rows is None else rows[chosen]
        return [(self._ref_ids[row], float(scores[position])) for row, position in zip(matrix_rows, chosen)]
