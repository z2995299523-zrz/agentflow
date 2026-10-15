"""
血缘图与关联图
启动时从元数据库加载两张图，对外只提供确定性查询，不调用模型：
- 字段血缘图：节点是「表.字段」，边从源字段指向目标字段，带加工表达式、是否直取、脚本名
- 表关联图：节点是表，边是关联键和对应关系

骨架：接口已定，实现由第二份计划的 D 任务补上
"""
from collections.abc import Collection
from dataclasses import dataclass

from metadata.models import FieldRef, Relation, ScriptFilter, ScriptJoin
from metadata.store import MetadataStore


@dataclass(frozen=True)
class Hop:
    """上游链路里的一跳：目标字段由源字段加工而来"""

    depth: int                               # 第几跳，从 1 开始
    target: FieldRef
    source: FieldRef
    transform: str                           # 血缘清单里的加工逻辑
    script_expression: str                   # 加工脚本里展开后的表达式
    is_passthrough: bool
    origin: str                              # 血缘来源：list 或 script
    script_name: str
    source_external: bool
    filters: tuple[ScriptFilter, ...]        # 这一跳目标表的加工过滤条件
    joins: tuple[ScriptJoin, ...]            # 这一跳目标表的加工关联条件


@dataclass(frozen=True)
class SourceDiff:
    """两个字段的同源差异"""

    a: FieldRef
    b: FieldRef
    common: tuple[FieldRef, ...]             # 共同的上游字段，没有共同上游时为空
    path_a: tuple[Hop, ...]
    path_b: tuple[Hop, ...]
    differs: bool                            # 同源但加工或过滤不同


@dataclass(frozen=True)
class JoinStep:
    """关联路径里的一步"""

    left_table: str
    left_columns: tuple[str, ...]
    right_table: str
    right_columns: tuple[str, ...]
    cardinality: str
    origin: str
    note: str


@dataclass(frozen=True)
class JoinPath:
    """两张表之间的关联路径"""

    steps: tuple[JoinStep, ...]
    fan_out: bool                            # 路径上有可能让行数变多的一步


class LineageGraph:
    """血缘图与关联图的确定性查询"""

    def __init__(self, store: MetadataStore) -> None:
        raise NotImplementedError

    def upstream(self, table: str, column: str, depth: int = 3) -> list[Hop]:
        """字段的上游链路，广度优先，深度可限，有环时不重复走"""
        raise NotImplementedError

    def downstream(self, table: str, column: str, depth: int = 3) -> list[Hop]:
        """字段的下游链路"""
        raise NotImplementedError

    def compare(self, a: FieldRef, b: FieldRef, depth: int = 3) -> SourceDiff:
        """两个字段的同源差异"""
        raise NotImplementedError

    def upstream_tables(self, table: str, layers: Collection[str] | None = None) -> list[str]:
        """表的全部上游表（血缘来源，加上加工脚本里关联用到的表），可按层级过滤"""
        raise NotImplementedError

    def downstream_tables(self, table: str, layers: Collection[str] | None = None) -> list[str]:
        """表的全部下游表，可按层级过滤"""
        raise NotImplementedError

    def relations_between(self, a: str, b: str) -> list[Relation]:
        """两张表之间的全部关联关系，最优的在前"""
        raise NotImplementedError

    def join_path(self, a: str, b: str) -> JoinPath | None:
        """两张表之间的最短关联路径：直连优先，最多经过一张中间表；找不到时返回 None"""
        raise NotImplementedError

    def is_unique(self, table: str, columns: Collection[str]) -> bool | None:
        """一组字段是否唯一确定表里的一行：覆盖全部粒度字段为 True，否则 False；表没有粒度字段时 None"""
        raise NotImplementedError
