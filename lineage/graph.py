"""
血缘图与关联图
启动时从元数据库加载两张图，对外只提供确定性查询，不调用模型：
- 字段血缘图：节点是「表.字段」，边从源字段指向目标字段，带加工表达式、是否直取、脚本名
- 表关联图：节点是表，边是关联键和对应关系

字段链路广度优先，每条边只报一次、每个字段只展开一次，所以成环也不会死循环，顺序由血缘清单的顺序决定。
上游表、下游表按表一级求闭包，边有两个来源：血缘边（源表 → 目标表），以及「目标表的脚本语句里引用到的实体表 → 目标表」：
维表往往只出现在关联里，不是任何字段的血缘来源，只看血缘边会漏掉它们。
遇到字段清单里没有的字段（脚本补出来的血缘边来源字段缺在清单里）不会出错，Hop.source_known 为 False 标出它们；
不在表清单里的外部表不进表一级的结果。

每一跳带的过滤条件和关联条件只取目标字段在脚本里经过的查询层（最外层、CTE、子查询）的，别的 CTE 的条件与这个字段无关。
脚本字段没有记录经过的查询层时（旧资料、追溯不到），退回这条语句的全部条件。
"""
from collections import defaultdict, deque
from collections.abc import Collection
from dataclasses import dataclass

from metadata.models import FieldRef, LineageEdge, Relation, ScriptFilter, ScriptJoin, ScriptStatement, TableMeta
from metadata.relations import relation_rank
from metadata.script_parser import MAIN_SCOPE
from metadata.sqlscope import parse_statements, physical_tables, table_id
from metadata.store import MetadataStore

# 沿路径走的方向上，这几种对应关系可能让行数变多
_FAN_OUT_CARDINALITIES = ("1:N", "N:N", "未知")
_FLIPPED = {"N:1": "1:N", "1:N": "N:1"}


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
    filters: tuple[ScriptFilter, ...]        # 目标字段经过的查询层的加工过滤条件，含最外层
    joins: tuple[ScriptJoin, ...]            # 目标字段经过的查询层的加工关联条件，含最外层
    # 源字段在字段清单里。脚本补出来的边，来源字段可能不在清单里，外部表的字段一律不在；
    # 为 False 的字段交给模型，静态校验会报「没有这个字段」，所以引用它们之前要先看这个标记
    source_known: bool = True


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


def _pair(a: str, b: str) -> tuple[str, str]:
    """无序表对的规范写法"""
    return (a, b) if a <= b else (b, a)


class LineageGraph:
    """血缘图与关联图的确定性查询"""

    def __init__(self, store: MetadataStore) -> None:
        """从元数据库建图：字段血缘的出入边、每张表的脚本片段、表一级的上下游、按表对归组的关联

        Args:
            store: 元数据库的只读入口，建图之后不再访问它的数据库文件
        """
        self._store = store
        self._tables: dict[str, TableMeta] = {t.table_id: t for t in store.tables()}

        edges = store.lineage_edges()
        self._incoming: dict[tuple[str, str], list[LineageEdge]] = defaultdict(list)
        self._outgoing: dict[tuple[str, str], list[LineageEdge]] = defaultdict(list)
        for edge in edges:
            self._incoming[(edge.target_table, edge.target_column)].append(edge)
            self._outgoing[(edge.source_table, edge.source_column)].append(edge)

        self._known_fields: set[tuple[str, str]] = {
            (column.table_id, column.column_name) for table_id in self._tables for column in store.columns(table_id)}

        self._statements: dict[str, list[ScriptStatement]] = defaultdict(list)
        for statement in store.scripts():
            self._statements[statement.target_table].append(statement)
        # (脚本名, 语句序号, 目标字段) → 这个字段经过的查询层加上最外层；没有记录经过哪些查询层的字段不登记
        self._column_scopes: dict[tuple[str, int, str], frozenset[str]] = {}
        for table in self._statements:
            for script_column in store.script_columns(table):
                if script_column.scopes:
                    self._column_scopes[(script_column.script_name, script_column.stmt_index,
                                         script_column.target_column)] = frozenset(script_column.scopes) | {MAIN_SCOPE}
        self._conditions: dict[tuple[str, str, str],
                               tuple[tuple[ScriptFilter, ...], tuple[ScriptJoin, ...]]] = {}
        self._expressions: dict[tuple[str, str, str], str] = {}

        self._table_up: dict[str, set[str]] = defaultdict(set)
        self._table_down: dict[str, set[str]] = defaultdict(set)
        for edge in edges:
            self._link(edge.source_table, edge.target_table)
        for statement in store.scripts():
            for source in self._script_tables(statement):
                self._link(source, statement.target_table)

        self._by_pair: dict[tuple[str, str], list[Relation]] = defaultdict(list)
        for relation in store.relations():
            self._by_pair[_pair(relation.left_table, relation.right_table)].append(relation)
        self._neighbors: dict[str, set[str]] = defaultdict(set)
        for (a, b), relations in self._by_pair.items():
            relations.sort(key=lambda r: relation_rank(r, self._tables))
            if a != b:
                self._neighbors[a].add(b)
                self._neighbors[b].add(a)

    # ── 建图 ──

    def _link(self, source: str, target: str) -> None:
        """登记一条表一级的依赖边：target 依赖 source。外部表、自己依赖自己的不登记"""
        if source != target and source in self._tables and target in self._tables:
            self._table_up[target].add(source)
            self._table_down[source].add(target)

    @staticmethod
    def _script_tables(statement: ScriptStatement) -> set[str]:
        """一条脚本语句引用到的实体表：关联里的表和键对涉及的表，加上自包含查询里出现的全部实体表

        没有条件的关联（逗号连接）、子查询里的表不在 joins 里，所以还要解析自包含查询。
        查询解析不了时只用 joins，不影响建图。
        """
        found: set[str] = set()
        for join in statement.joins:
            found.add(join.table)
            for key in join.keys:
                found.update((key.left.table, key.right.table))
        if statement.select_sql:
            try:
                for tree in parse_statements(statement.select_sql):
                    found.update(table_id(table) for table in physical_tables(tree))
            except Exception:  # noqa: BLE001 解析失败只是少一些边，不能让建图失败
                pass
        return found

    # ── 字段链路 ──

    def _conditions_of(self, table: str, script_name: str,
                       column: str) -> tuple[tuple[ScriptFilter, ...], tuple[ScriptJoin, ...]]:
        """一个目标字段的加工条件：优先取边上写明的脚本里的语句，对不上脚本名时取这张表的全部语句

        语句里记着这个字段经过哪些查询层的，只取这些查询层和最外层的条件，别的 CTE、子查询的条件与这个字段无关；
        没有记的（旧资料、追溯不到），取这条语句的全部条件。
        """
        key = (table, script_name, column)
        if key not in self._conditions:
            statements = self._statements.get(table, [])
            chosen = [s for s in statements if script_name and s.script_name == script_name] or statements
            filters: list[ScriptFilter] = []
            joins: list[ScriptJoin] = []
            for statement in chosen:
                scopes = self._column_scopes.get((statement.script_name, statement.stmt_index, column))
                filters += [f for f in statement.filters if scopes is None or f.scope in scopes]
                joins += [j for j in statement.joins if scopes is None or j.scope in scopes]
            self._conditions[key] = (tuple(filters), tuple(joins))
        return self._conditions[key]

    def _expression_of(self, table: str, column: str, script_name: str = "") -> str:
        """目标字段在加工脚本里展开后的表达式，没有时为空

        多个脚本写同一个字段时，取血缘边上写明的那个脚本里的；对不上脚本名时取第一条。
        """
        key = (table, column, script_name)
        if key not in self._expressions:
            script_column = self._store.script_column(table, column, script_name)
            self._expressions[key] = script_column.expression if script_column is not None else ""
        return self._expressions[key]

    def _hop(self, depth: int, edge: LineageEdge) -> Hop:
        """由一条血缘边生成一跳，带上目标字段的加工条件，并标出源字段在不在字段清单里"""
        filters, joins = self._conditions_of(edge.target_table, edge.script_name, edge.target_column)
        return Hop(
            depth=depth,
            target=FieldRef(table=edge.target_table, column=edge.target_column),
            source=FieldRef(table=edge.source_table, column=edge.source_column),
            transform=edge.transform,
            script_expression=self._expression_of(edge.target_table, edge.target_column, edge.script_name),
            is_passthrough=edge.is_passthrough, origin=edge.origin, script_name=edge.script_name,
            source_external=edge.source_external, filters=filters, joins=joins,
            source_known=(not edge.source_external
                          and (edge.source_table, edge.source_column) in self._known_fields),
        )

    def _walk(self, start: tuple[str, str], depth: int, forward: bool) -> list[Hop]:
        """从一个字段出发广度优先走血缘边：每条边报一次，每个字段只展开一次，最多走 depth 跳"""
        edges = self._outgoing if forward else self._incoming
        hops: list[Hop] = []
        seen = {start}
        queue = deque([(start, 0)])
        while queue:
            node, level = queue.popleft()
            if level >= depth:
                continue
            for edge in edges.get(node, ()):
                hops.append(self._hop(level + 1, edge))
                following = ((edge.target_table, edge.target_column) if forward
                             else (edge.source_table, edge.source_column))
                if following not in seen:
                    seen.add(following)
                    queue.append((following, level + 1))
        return hops

    def upstream(self, table: str, column: str, depth: int = 3) -> list[Hop]:
        """字段的上游链路，广度优先，深度可限，有环时不重复走

        每一跳带着目标字段经过的查询层的加工过滤条件和关联条件，上溯到更低的层查数时要把它们一并带上，数才对得上。
        Hop.source_known 为 False 的来源字段不在字段清单里，不能写进给模型的 SQL。
        字段没有血缘、不在清单里时返回空列表。

        Args:
            table: 表标识
            column: 字段名
            depth: 最多走几跳，默认 3；0 或负数返回空列表

        Returns:
            各跳，按深度从浅到深，同一深度按血缘清单里的顺序
        """
        return self._walk((table, column), depth, forward=False)

    def downstream(self, table: str, column: str, depth: int = 3) -> list[Hop]:
        """字段的下游链路：哪些字段由它加工而来，规则同 upstream

        Args:
            table: 表标识
            column: 字段名
            depth: 最多走几跳，默认 3

        Returns:
            各跳。Hop.source 是更靠近起点的字段，Hop.target 是由它加工出来的字段
        """
        return self._walk((table, column), depth, forward=True)

    @staticmethod
    def _signature(path: tuple[Hop, ...]) -> frozenset:
        """一条链路的加工口径：每一跳的来源字段、表达式、过滤条件、关联条件；不含目标字段，因为同源比较时目标本来就不同"""
        return frozenset(
            (hop.source, hop.script_expression or hop.transform,
             tuple((f.scope, f.clause, f.condition) for f in hop.filters),
             tuple((j.scope, j.join_type, j.table, j.condition) for j in hop.joins))
            for hop in path)

    def compare(self, a: FieldRef, b: FieldRef, depth: int = 3) -> SourceDiff:
        """两个字段的同源差异

        共同上游：两个字段各自的来源（含字段自己）的交集，所以一个字段是另一个的上游时，它自己也算共同的。
        同源但加工或过滤不同（differs）：有共同上游，并且两条链路上每一跳的来源、表达式、过滤条件、关联条件不完全一致；
        过滤和关联只算各自字段经过的查询层的，所以只差在别的 CTE 里的过滤，不算口径不同。

        Args:
            a: 字段 a
            b: 字段 b
            depth: 各自上溯几跳

        Returns:
            同源差异；没有共同上游时 common 为空、differs 为 False
        """
        path_a = tuple(self.upstream(a.table, a.column, depth))
        path_b = tuple(self.upstream(b.table, b.column, depth))
        origins_b = {b, *(hop.source for hop in path_b)}
        common = tuple(ref for ref in dict.fromkeys([a, *(hop.source for hop in path_a)]) if ref in origins_b)
        differs = bool(common) and self._signature(path_a) != self._signature(path_b)
        return SourceDiff(a=a, b=b, common=common, path_a=path_a, path_b=path_b, differs=differs)

    # ── 表一级的上下游 ──

    def _closure(self, table: str, adjacency: dict[str, set[str]], layers: Collection[str] | None) -> list[str]:
        """沿依赖边广度优先求闭包：近的在前，同一层按表标识排序；不含起点自己"""
        seen = {table}
        order: list[str] = []
        queue = deque([table])
        while queue:
            for following in sorted(adjacency.get(queue.popleft(), ())):
                if following not in seen:
                    seen.add(following)
                    order.append(following)
                    queue.append(following)
        if layers is not None:
            wanted = set(layers)
            order = [t for t in order if self._tables[t].layer in wanted]
        return order

    def upstream_tables(self, table: str, layers: Collection[str] | None = None) -> list[str]:
        """表的全部上游表（血缘来源，加上加工脚本里关联用到的表），可按层级过滤

        求的是闭包：上游表的上游表也在内。按层级过滤只影响结果里留哪些，路过的表不受影响。
        不在表清单里的外部表不进结果。

        Args:
            table: 表标识
            layers: 只留这些层级的表；None 表示不过滤，空集合表示一张也不要

        Returns:
            表标识列表，近的在前，同一层按表标识排序
        """
        return self._closure(table, self._table_up, layers)

    def downstream_tables(self, table: str, layers: Collection[str] | None = None) -> list[str]:
        """表的全部下游表，可按层级过滤；规则同 upstream_tables"""
        return self._closure(table, self._table_down, layers)

    # ── 关联与路径 ──

    def relations_between(self, a: str, b: str) -> list[Relation]:
        """两张表之间的全部关联关系，不分方向，最优的在前

        a、b 相同时返回这张表的自关联。关联的左右方向保持元数据库里的写法。
        """
        return list(self._by_pair.get(_pair(a, b), ()))

    @staticmethod
    def _oriented(relation: Relation, start: str) -> JoinStep:
        """把关联写成从 start 出发的一步：start 在右侧时左右互换，N:1 与 1:N 对调"""
        if relation.left_table == start:
            return JoinStep(left_table=relation.left_table, left_columns=tuple(relation.left_columns),
                            right_table=relation.right_table, right_columns=tuple(relation.right_columns),
                            cardinality=relation.cardinality, origin=relation.origin, note=relation.note)
        return JoinStep(left_table=relation.right_table, left_columns=tuple(relation.right_columns),
                        right_table=relation.left_table, right_columns=tuple(relation.left_columns),
                        cardinality=_FLIPPED.get(relation.cardinality, relation.cardinality),
                        origin=relation.origin, note=relation.note)

    def _best_step(self, start: str, end: str) -> tuple[JoinStep, tuple] | None:
        """start 到 end 之间最优的一种映射，写成从 start 出发的一步，连同它的排序键；没有关联时为 None"""
        relations = self._by_pair.get(_pair(start, end))
        if not relations:
            return None
        return self._oriented(relations[0], start), relation_rank(relations[0], self._tables)

    @staticmethod
    def _path(steps: list[JoinStep]) -> JoinPath:
        """由各步生成路径，任何一步可能让行数变多，路径就标为发散"""
        return JoinPath(steps=tuple(steps), fan_out=any(s.cardinality in _FAN_OUT_CARDINALITIES for s in steps))

    def join_path(self, a: str, b: str) -> JoinPath | None:
        """两张表之间的最短关联路径：直连优先，最多经过一张中间表；找不到时返回 None

        直连时取最优的一种映射。经一张中间表时，在能连通的中间表里先选不发散的，再选两步的来源和对应关系都更优的，
        最后按表标识排序。a、b 是同一张存在的表时返回没有步骤的空路径；有表不在元数据里也返回 None。

        Args:
            a: 起点表
            b: 终点表

        Returns:
            关联路径。每一步的左表是已经在手的表，对应关系按行进的方向写；
            路径上有 1:N、N:N、未知的步骤时 fan_out 为 True
        """
        if a not in self._tables or b not in self._tables:
            return None
        if a == b:
            return JoinPath(steps=(), fan_out=False)
        direct = self._best_step(a, b)
        if direct is not None:
            return self._path([direct[0]])
        best: tuple | None = None
        for middle in sorted(self._neighbors.get(a, set()) & self._neighbors.get(b, set())):
            first, second = self._best_step(a, middle), self._best_step(middle, b)
            if first is None or second is None:
                continue
            path = self._path([first[0], second[0]])
            score = (path.fan_out, max(first[1], second[1]), min(first[1], second[1]), middle)
            if best is None or score < best[0]:
                best = (score, path)
        return best[1] if best is not None else None

    def is_unique(self, table: str, columns: Collection[str]) -> bool | None:
        """一组字段是否唯一确定表里的一行：覆盖全部粒度字段为 True，否则 False；表没有粒度字段时 None"""
        meta = self._tables.get(table)
        if meta is None or not meta.grain_columns:
            return None
        return set(meta.grain_columns) <= set(columns)
