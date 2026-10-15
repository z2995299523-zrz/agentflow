"""
候选召回（流水线第 ② 步）
对每个指标、维度、过滤要素，在当前范围内的表里用向量相似度和中文关键字两路找字段，
合并后按表归组，保留最多 MAX_TABLES 张候选表；同时找最相似的样例，样例用到的表直接加入候选。

候选范围由上溯层数决定：
    0  专题层全部表，加整合层的维表
    1  再加入候选事实表的上游表中属于整合层的
    2  再加入属于贴源层的
上溯时加入的上游表无条件成为候选，不占名额。

骨架：接口已定，实现由第二份计划的 F 任务补上
"""
from collections.abc import Sequence
from dataclasses import dataclass, field

from lineage.graph import LineageGraph
from metadata.index import Embedder
from metadata.store import MetadataStore

MAX_TABLES = 8
MAX_EXAMPLES = 3
PER_TERM_K = 30
RRF_K = 60
ROLE_WEIGHTS = {"metric": 1.0, "dimension": 0.7, "filter": 0.5}


@dataclass
class RecallQuery:
    """召回的输入：完整需求和拆出来的要素"""

    requirement: str
    metrics: list[str]
    dimensions: list[str] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)


@dataclass
class FieldHit:
    """一个命中的字段"""

    table_id: str
    column_name: str
    column_cn: str
    term: str                            # 命中的要素
    score: float
    via: tuple[str, ...]                 # 命中的路：vector、keyword


@dataclass
class TableCandidate:
    """一张候选表"""

    table_id: str
    reason: str                          # recall、example 或 upstream
    score: float
    hits: list[FieldHit]


@dataclass
class ExampleHit:
    """一条相似样例"""

    example_id: str
    requirement: str
    score: float
    tables: list[str]


@dataclass
class RecallResult:
    """召回结果"""

    scope_level: int
    scope_size: int                      # 范围内的表数
    tables: list[TableCandidate]
    examples: list[ExampleHit]
    notes: list[str]                     # 说明，例如向量器不一致时只走了关键字路


class Retriever:
    """候选召回"""

    def __init__(self, store: MetadataStore, graph: LineageGraph, embedder: Embedder | None) -> None:
        raise NotImplementedError

    def scope_tables(self, level: int, base_tables: Sequence[str] = ()) -> tuple[list[str], list[str]]:
        """返回 (范围内的表, 无条件加入的上游表)"""
        raise NotImplementedError

    def recall(self, query: RecallQuery, scope_level: int = 0,
               base_tables: Sequence[str] = ()) -> RecallResult:
        """召回候选表和相似样例

        Raises:
            ValueError: scope_level 大于等于 1 而 base_tables 为空
        """
        raise NotImplementedError
