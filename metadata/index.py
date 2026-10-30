"""
向量索引
字段文本（表中文名、字段中文名）、表文本（表中文名、说明、字段中文名清单）和样例文本（需求、口径）的向量化与检索。
向量和元数据存在同一个元数据库里，检索时用 numpy 算全量相似度，不用向量数据库。
重新导入时按文本复用上一次的向量，只算新出现的文本，全部能复用时不加载模型。

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
from collections.abc import Callable, Collection, Iterator, Sequence
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
TABLE_KIND = "table"                   # 向量集的类别：表
TABLE_TEXT_FIELD_CHARS = 300           # 表文本里字段中文名清单最多留多少字
NORM_TOLERANCE = 1e-3                  # 向量长度与 1 相差不超过这个数，就认为已经归一化
REUSE_CHECK_SAMPLES = 32               # 这次要调向量器时，顺带重算几条复用的文本，核对向量模型没有换过
REUSE_MIN_COSINE = 0.9999              # 复用的向量与重算的余弦低于这个数，就认为向量模型换过，全部重算

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
_BGE_ENCODE_LOCK = threading.Lock()


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
        # sentence-transformers 的 encode 不保证线程安全，评估多线程、接口并发时会同时调到，所以串行；检索词很短，不影响速度
        with _BGE_ENCODE_LOCK:
            vectors = model.encode(list(texts), batch_size=BGE_ENCODE_BATCH, normalize_embeddings=True,
                                   convert_to_numpy=True, show_progress_bar=False)
        return np.ascontiguousarray(vectors, dtype=np.float32)


def bge_loaded() -> bool:
    """查询进程内 BGE 是否已经加载；不创建单例，也不触发模型加载。"""
    instance = BgeEmbedder._instance
    return instance is not None and instance._model is not None


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
    """字段向量化用的文本：表中文名、字段中文名，用空格连接

    字段名在很多表里都叫「余额」「客户号」，带上表中文名才分得出是哪张表的字段。

    口径说明有意不放进来（设计文档原文还带着它）：真实库里的口径说明多是加工过程的描述，属于血缘信息，
    不是字段的含义，混进向量文本会稀释相似度。在真实库专题层的手写查询上对比过，去掉口径说明后第一名的
    得分普遍高 0.07 到 0.10，只在说明里提到相关词的干扰字段也明显减少。口径说明仍然留在元数据库里，
    后面的证据环节交给模型读。
    """
    return " ".join(part for part in (table.table_cn, column.column_cn) if part)


def table_text(table: TableMeta, columns: Sequence[ColumnMeta]) -> str:
    """表向量化用的文本：表中文名、说明、字段中文名清单（截到 TABLE_TEXT_FIELD_CHARS 字），用空格连接

    字段向量回答「哪个字段像检索词」，表向量回答「哪张表讲的是这件事」：说明里的业务域、粒度和整张表有哪些字段，
    能把表名里没有检索词、却正是在讲这件事的表找出来。在真实库 215 道题上离线比过：只用中文名和说明时，
    说明里碰巧提到检索词的表会被抬上来，变差的比变好的多；加上字段中文名清单后变好的多。
    """
    names = "、".join(column.column_cn for column in columns if column.column_cn)[:TABLE_TEXT_FIELD_CHARS]
    return " ".join(part for part in (table.table_cn, table.description, f"字段：{names}" if names else "") if part)


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


def _reusable_vectors(previous: VectorBundle | None, previous_note: str, embedder: Embedder,
                      texts: Sequence[str]) -> tuple[dict[str, np.ndarray], str]:
    """上一版向量里能复用的部分：文本 → 向量，只收这次用得到的文本

    Returns:
        (能复用的向量, 不能复用的原因)；能复用时原因为空串
    """
    if previous is None:
        return {}, previous_note or "没有上一版的向量"
    if (previous.embedder, previous.dim) != (embedder.name, embedder.dim):
        return {}, (f"上一版的向量器是 {previous.embedder}（{previous.dim} 维），"
                    f"这次是 {embedder.name}（{embedder.dim} 维）")
    wanted = set(texts)
    reusable: dict[str, np.ndarray] = {}
    for vector_set in previous.sets.values():                # 字段、样例用的是同一个向量器，文本相同向量就相同
        for text, row in zip(vector_set.texts, vector_set.matrix):
            if text in wanted:
                reusable.setdefault(text, row)
    return reusable, ""


def _spread(items: Sequence[str], count: int) -> list[str]:
    """从列表里均匀地挑 count 个，不足 count 个就全要；同样的输入挑出同样的结果"""
    if len(items) <= count:
        return list(items)
    return [items[i * len(items) // count] for i in range(count)]


def _cosine(first: np.ndarray, second: np.ndarray) -> float:
    """两个已归一化向量的余弦；两个都是零向量（文本里没有任何特征）时算作 1"""
    if not first.any() and not second.any():
        return 1.0
    return float(np.dot(first.astype(np.float64), second.astype(np.float64)))


def build_vectors(mart: Mart, embedder: Embedder, batch_size: int = 256, *,
                  previous: VectorBundle | None = None, previous_note: str = "",
                  before_embed: Callable[[int], None] | None = None) -> tuple[VectorBundle, ReportSection]:
    """为全部字段、表和样例算向量；给了上一版的向量时，文本相同的直接复用，只算新出现的文本

    字段向量的顺序与 mart.columns 一致，表向量与 mart.tables 一致，样例向量与 mart.examples 一致，
    同样的资料每次得到同样的结果。
    同样的文本只算一次。上一版的向量器名称或维度与这次的不同时，一条也不复用。
    全部能复用时不调用向量器，BGE 也就不用加载。复用了一部分、又要调向量器算新文本时，顺带从复用的文本里
    均匀抽 REUSE_CHECK_SAMPLES 条重算核对：余弦低于 REUSE_MIN_COSINE 说明向量模型换过（名字没变），全部重算；
    核对通过的仍用上一版的向量，前后两次导入逐位一致。
    没有样例时不生成样例集。表清单里没有的表下的字段（正常导入时不会出现）没法拼字段文本，直接跳过。

    Args:
        mart: 已经补全的资料
        embedder: 向量器
        batch_size: 每批向量化的文本条数
        previous: 上一版的向量，按文本复用；为 None 时全部重算
        previous_note: previous 为 None 时写进报告的原因，例如「元数据库还不存在」
        before_embed: 第一次调用向量器之前调它一次，参数是要算的文本条数；全部复用时不调。
            命令行用它在加载模型之前提示一声

    Returns:
        (向量, 报告节)

    Raises:
        ValueError: batch_size 不是正整数，或向量器返回的结果不合格
    """
    if batch_size < 1:
        raise ValueError(f"batch_size 必须是正整数，收到 {batch_size}")
    started = time.perf_counter()

    tables = {table.table_id: table for table in mart.tables}
    columns_of: dict[str, list[ColumnMeta]] = {table_id: [] for table_id in tables}
    field_ids: list[str] = []
    field_texts: list[str] = []
    for column in mart.columns:
        table = tables.get(column.table_id)
        if table is not None:
            columns_of[column.table_id].append(column)
            field_ids.append(field_ref_id(column.table_id, column.column_name))
            field_texts.append(field_text(table, column))
    table_texts = [table_text(table, columns_of[table.table_id]) for table in mart.tables]
    example_texts = [example_text(example) for example in mart.examples]

    unique = list(dict.fromkeys(field_texts + table_texts + example_texts))   # 去重，保持首次出现的顺序
    reusable, reason = _reusable_vectors(previous, previous_note, embedder, unique)
    missing = [text for text in unique if text not in reusable]
    samples = _spread([text for text in unique if text in reusable], REUSE_CHECK_SAMPLES) if missing else []
    computed: dict[str, np.ndarray] = {}
    if missing:
        if before_embed is not None:
            before_embed(len(missing) + len(samples))
        computed = dict(zip(missing + samples, _embed_all(embedder, missing + samples, batch_size)))
    lowest = min((_cosine(reusable[text], computed[text]) for text in samples), default=None)
    if lowest is not None and lowest < REUSE_MIN_COSINE:
        rest = [text for text in unique if text not in computed]
        computed.update(zip(rest, _embed_all(embedder, rest, batch_size)))
        reason = (f"抽查复用的 {len(samples)} 条，与重算的余弦最低 {lowest:.6f}，低于 {REUSE_MIN_COSINE}，"
                  "向量模型可能换过")
        reusable = {}
    vectors = {**computed, **reusable}                                # 复用的优先：抽查过的文本仍用上一版的向量

    def matrix_of(texts: list[str]) -> np.ndarray:
        return np.array([vectors[text] for text in texts], dtype=np.float32).reshape(len(texts), embedder.dim)

    sets = {FIELD_KIND: VectorSet(kind=FIELD_KIND, ref_ids=field_ids, texts=field_texts,
                                  matrix=matrix_of(field_texts)),
            TABLE_KIND: VectorSet(kind=TABLE_KIND, ref_ids=[table.table_id for table in mart.tables],
                                  texts=table_texts, matrix=matrix_of(table_texts))}
    if mart.examples:
        sets[EXAMPLE_KIND] = VectorSet(kind=EXAMPLE_KIND, ref_ids=[example.example_id for example in mart.examples],
                                       texts=example_texts, matrix=matrix_of(example_texts))

    if reason:
        reuse_lines = [f"- 不同的文本 {len(unique)} 条：全部新算（{reason}）"]
    else:
        reuse_lines = [f"- 不同的文本 {len(unique)} 条：复用上一版 {len(reusable)} 条，新算 {len(missing)} 条"]
        if not missing:
            reuse_lines.append("- 全部复用，没有调用向量器")
        elif samples:
            reuse_lines.append(f"- 抽查复用的 {len(samples)} 条，与重算的余弦最低 {lowest:.6f}")
    elapsed = time.perf_counter() - started
    section = ReportSection(SECTION_VECTORS, (
        f"- 向量器：{embedder.name}",
        f"- 维度：{embedder.dim}",
        f"- 字段向量：{len(field_ids)} 条",
        f"- 表向量：{len(mart.tables)} 条",
        f"- 样例向量：{len(mart.examples)} 条",
        *reuse_lines,
        f"- 耗时：{elapsed:.2f} 秒",
    ))
    return VectorBundle(embedder=embedder.name, dim=embedder.dim, sets=sets), section


class VectorIndex:
    """一类文档向量上的全量相似度检索

    不建近似索引：两万多条 512 维的向量，扫一遍只要几毫秒，近似索引省不下什么，还会让结果不确定。
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
