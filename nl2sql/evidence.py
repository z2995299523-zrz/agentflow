"""
③ 证据组装
把候选召回的结果展开成给模型看的证据：候选表的结构、指标字段的加工链路、同源字段对比、表之间的上下游、
关联路径、还可以上溯到的表、相似样例。全部是确定性代码，同样的输入必然得到同样的 Evidence。

对照实验的三组：A 组（仅模型结构）没有血缘、脚本、样例，关联路径只来自表结构推得出的关联；
B 组加血缘；C 组再加样例。

做法：
1. 候选表：按召回的先后，列出全部字段；命中的字段、粒度字段、数据日期字段、主键保留较长的说明，其余字段说明截短
2. 指标命中字段：命中词是指标检索词的字段，按得分从高到低排，只展开前 MAX_LINEAGE_FIELDS 个
3. 加工链路：沿血缘往上走 LINEAGE_DEPTH 跳，同一个目标字段的几个来源并成一步，带上脚本表达式和该字段经过的加工条件
4. 同源对比：同一个指标检索词命中的字段两两比较，是上下游关系、同源口径相同还是同源口径不同；
   口径不同的字段即使没排进名额也补上链路
5. 表的上下游：候选表之间谁由谁加工而来
6. 关联路径：候选表两两求最短路径（直连优先，最多经过一张中间表），中间表补进证据
7. 上溯选项：往下一层上溯能到的表
8. 样例：带上样例的需求、口径说明和 SQL 原文
所有排序都有确定的次序：得分相同的先比表在候选里的先后，再比字段名。
"""
from collections.abc import Iterable
from dataclasses import dataclass, replace
from itertools import combinations

from lineage.graph import Hop, LineageGraph, SourceDiff
from metadata.models import FieldRef, Mart, TableMeta
from metadata.relations import structural_relations
from metadata.store import MetadataStore
from nl2sql.retriever import (
    EXCLUDED_KINDS,
    REASON_EXAMPLE,
    SCORE_DIGITS,
    FieldHit,
    RecallQuery,
    RecallResult,
    TableCandidate,
)
from nl2sql.schemas import (
    Evidence,
    EvidenceColumn,
    EvidenceExample,
    EvidenceTable,
    FieldComparison,
    FieldLineage,
    JoinHint,
    JoinHop,
    LineageStep,
    TableLink,
    UpstreamOption,
)

LINEAGE_DEPTH = 3
MAX_LINEAGE_FIELDS = 12          # 有同源不同口径的字段时可以超出，但总数不超过 MAX_LINEAGE_TOTAL
MAX_LINEAGE_TOTAL = 20
MAX_STEPS_PER_FIELD = 12
MAX_COMPARE_FIELDS = 6           # 每个指标检索词参与两两比较的字段数
MAX_JOIN_HINTS = 20
MAX_JOIN_TABLES = 3              # 关联路径经过的中间表最多补几张
MAX_UPSTREAM_OPTIONS = 10
FULL_DESC_CHARS = 200
SHORT_DESC_CHARS = 60
ENUM_CHARS = 200
EXPR_CHARS = 800
COND_CHARS = 300
MAX_CONDS = 6

ELLIPSIS = "…"
REASON_JOIN = "join"
NEXT_LAYER = {0: "整合层", 1: "贴源层"}        # 上溯一层能到的层级，按召回时已经上溯的层数
# 脚本里关联的写法：类型 → 前缀。IMPLICIT 是写在 WHERE 里的等值关联；不认识的类型照写
JOIN_PREFIXES = {"LEFT": "LEFT JOIN", "INNER": "INNER JOIN", "RIGHT": "RIGHT JOIN", "FULL": "FULL JOIN",
                 "JOIN": "JOIN", "IMPLICIT": "WHERE 等值关联"}


# ── 小工具 ──────────────────────────────────────

def _cut(text: str, limit: int) -> str:
    """超过 limit 个字的部分用省略号代替"""
    return text if len(text) <= limit else text[:limit] + ELLIPSIS


def _unique(items: Iterable[str]) -> list[str]:
    """去重，保持第一次出现的先后"""
    return list(dict.fromkeys(items))


def _field_id(table: str, column: str) -> str:
    """字段的写法：模式.表.字段"""
    return f"{table}.{column}"


@dataclass(frozen=True)
class _MetricHit:
    """一个指标命中字段：命中词是指标检索词的字段"""

    table_id: str
    column: str
    term: str
    rank: tuple                          # 排序键：得分从高到低，同分先比表在候选里的先后，再比字段名

    @property
    def field(self) -> str:
        """模式.表.字段"""
        return _field_id(self.table_id, self.column)

    @property
    def ref(self) -> FieldRef:
        """字段引用，血缘图的查询用"""
        return FieldRef(table=self.table_id, column=self.column)


# ── 候选表 ──────────────────────────────────────

def _column_kinds(store: MetadataStore, table_id: str) -> dict[str, str]:
    """一张表每个字段在加工脚本里的表达式类别

    多条语句写同一个字段时，取第一条真正加工的（不是恒为空、取加工时间）；都不是时取第一条。
    与召回排除字段的规则一致：召回时留下的字段，这里不会标成恒为空。
    """
    written: dict[str, list[str]] = {}
    for script_column in store.script_columns(table_id):
        written.setdefault(script_column.target_column, []).append(script_column.kind)
    return {column: next((kind for kind in kinds if kind not in EXCLUDED_KINDS), kinds[0])
            for column, kinds in written.items()}


def _evidence_table(store: MetadataStore, meta: TableMeta, hits: list[FieldHit], reason: str, score: float,
                    use_lineage: bool) -> EvidenceTable:
    """由一张表的元数据和它的命中字段组装证据里的表

    说明的截断：命中的字段、粒度字段、数据日期字段、主键保留 FULL_DESC_CHARS 个字，其余字段只留 SHORT_DESC_CHARS 个字；
    码值一律截到 ENUM_CHARS。
    """
    hit_terms: dict[str, list[str]] = {}
    for item in hits:
        terms = hit_terms.setdefault(item.column_name, [])
        if item.term not in terms:
            terms.append(item.term)
    important = {*meta.grain_columns, *([meta.date_column] if meta.date_column else [])}
    kinds = _column_kinds(store, meta.table_id) if use_lineage else {}
    columns = []
    for column in store.columns(meta.table_id):
        terms = hit_terms.get(column.column_name, [])
        full = bool(terms) or column.is_pk or column.column_name in important
        columns.append(EvidenceColumn(
            name=column.column_name, cn=column.column_cn, data_type=column.data_type, is_pk=column.is_pk,
            description=_cut(column.description, FULL_DESC_CHARS if full else SHORT_DESC_CHARS),
            enum_values=_cut(column.enum_values, ENUM_CHARS), kind=kinds.get(column.column_name, ""),
            hit_terms=list(terms)))
    return EvidenceTable(
        table_id=meta.table_id, table_cn=meta.table_cn, layer=meta.layer, table_type=meta.table_type,
        grain_columns=list(meta.grain_columns), date_column=meta.date_column,
        description=_cut(meta.description, FULL_DESC_CHARS), reason=reason, score=score, columns=columns)


# ── 加工链路 ──────────────────────────────────────

class _MemoGraph(LineageGraph):
    """组装一份证据期间用的血缘图：同一个字段的上游链路只走一遍

    加工链路要走一遍，同源对比里每个字段又要和其余五个各比一次，每次比较都重新走两条上游链路。
    与原来的图共享全部内部索引，不重建。
    """

    def __init__(self, base: LineageGraph) -> None:
        self.__dict__.update(base.__dict__)
        self._upstream_memo: dict[tuple[str, str, int], list[Hop]] = {}

    def upstream(self, table: str, column: str, depth: int = 3) -> list[Hop]:
        """同 LineageGraph.upstream；返回的列表是缓存里的，调用方不要修改"""
        key = (table, column, depth)
        if key not in self._upstream_memo:
            self._upstream_memo[key] = super().upstream(table, column, depth)
        return self._upstream_memo[key]


def _metric_terms(query: RecallQuery) -> list[str]:
    """指标检索词：指标那一项里每一项去掉首尾空白、去重，与召回里命中字段的 term 写法一致"""
    return _unique(text.strip() for text in query.metrics if text.strip())


def _metric_hits(candidates: list[TableCandidate], terms: list[str]) -> list[_MetricHit]:
    """命中词是指标检索词的字段，按得分从高到低排，同分先比表在候选里的先后，再比字段名"""
    wanted = set(terms)
    found = []
    for position, candidate in enumerate(candidates):
        for item in candidate.hits:
            if item.term in wanted:
                rank = (-round(item.score, SCORE_DIGITS), position, item.column_name)
                found.append(_MetricHit(item.table_id, item.column_name, item.term, rank))
    return sorted(found, key=lambda metric: metric.rank)


def _distinct_fields(hits: Iterable[_MetricHit]) -> list[_MetricHit]:
    """同一个字段只留排在最前的那一条命中"""
    seen: dict[str, _MetricHit] = {}
    for item in hits:
        seen.setdefault(item.field, item)
    return list(seen.values())


def _join_text(join_type: str, table: str, condition: str) -> str:
    """脚本里一处关联写成「LEFT JOIN 表 ON 条件」，写在 WHERE 里的等值关联写成「WHERE 等值关联 表 ON 条件」"""
    head = f"{JOIN_PREFIXES.get(join_type, join_type)} {table}"
    return f"{head} ON {condition}" if condition else head


def _lineage_step(hops: list[Hop]) -> tuple[LineageStep, bool]:
    """同一个目标字段的几跳并成一步

    Returns:
        (一步, 过滤条件或关联条件是不是因为超过 MAX_CONDS 条而被截掉了)
    """
    first = hops[0]
    expression = next((text for text in (hop.script_expression or hop.transform for hop in hops) if text), "")
    filters = _unique(f"{item.clause} {item.condition}" for hop in hops for item in hop.filters)
    joins = _unique(_join_text(item.join_type, item.table, item.condition) for hop in hops for item in hop.joins)
    step = LineageStep(
        depth=first.depth, target=_field_id(first.target.table, first.target.column),
        sources=_unique(_field_id(hop.source.table, hop.source.column) for hop in hops),
        unknown_sources=_unique(_field_id(hop.source.table, hop.source.column) for hop in hops if not hop.source_known),
        expression=_cut(expression, EXPR_CHARS),
        filters=[_cut(text, COND_CHARS) for text in filters[:MAX_CONDS]],
        joins=[_cut(text, COND_CHARS) for text in joins[:MAX_CONDS]],
        script_name=next((hop.script_name for hop in hops if hop.script_name), ""), origin=first.origin)
    return step, len(filters) > MAX_CONDS or len(joins) > MAX_CONDS


def _field_lineage(graph: LineageGraph, table_id: str, column: str, term: str,
                   notes: list[str]) -> tuple[FieldLineage, int]:
    """一个指标命中字段的上游链路

    Returns:
        (链路, 条件被截掉的步数)；链路步数超过 MAX_STEPS_PER_FIELD 时截断并在 notes 里写明
    """
    groups: dict[tuple[int, str], list[Hop]] = {}
    for hop in graph.upstream(table_id, column, LINEAGE_DEPTH):
        groups.setdefault((hop.depth, _field_id(hop.target.table, hop.target.column)), []).append(hop)
    steps, cut = [], 0
    for hops in groups.values():
        step, was_cut = _lineage_step(hops)
        steps.append(step)
        cut += was_cut
    field = _field_id(table_id, column)
    truncated = len(steps) > MAX_STEPS_PER_FIELD
    if truncated:
        notes.append(f"字段 {field} 的加工链路超过 {MAX_STEPS_PER_FIELD} 步，只列前 {MAX_STEPS_PER_FIELD} 步")
        steps = steps[:MAX_STEPS_PER_FIELD]
    return FieldLineage(field=field, term=term, steps=steps, truncated=truncated), cut


# ── 同源对比 ──────────────────────────────────────

def _differences(diff: SourceDiff) -> str:
    """两条链路在哪些方面不同：来源字段、加工表达式、过滤条件、关联条件"""
    aspects = (
        ("来源字段", lambda hop: hop.source),
        ("加工表达式", lambda hop: hop.script_expression or hop.transform),
        ("过滤条件", lambda hop: tuple((item.scope, item.clause, item.condition) for item in hop.filters)),
        ("关联条件", lambda hop: tuple((item.scope, item.join_type, item.table, item.condition) for item in hop.joins)),
    )
    differing = [name for name, pick in aspects
                 if {pick(hop) for hop in diff.path_a} != {pick(hop) for hop in diff.path_b}]
    return "、".join(differing) + "不同" if differing else "加工口径不同"


def _compare(graph: LineageGraph, a: _MetricHit, b: _MetricHit, term: str) -> FieldComparison | None:
    """两个字段的同源对比；没有共同上游时返回 None

    判定的顺序：b 的上游里有 a（b 由 a 加工而来）、a 的上游里有 b（交换后同上）、有共同上游且口径不同、有共同上游且口径相同。
    """
    diff = graph.compare(a.ref, b.ref, LINEAGE_DEPTH)
    for upstream, downstream, path in ((a, b, diff.path_b), (b, a, diff.path_a)):
        key = (upstream.table_id, upstream.column)
        depths = [hop.depth for hop in path if (hop.source.table, hop.source.column) == key]
        if depths:
            return FieldComparison(term=term, a=upstream.field, b=downstream.field, relation="derived_from",
                                   common=[_field_id(ref.table, ref.column) for ref in diff.common],
                                   note=f"{downstream.field} 由 {upstream.field} 加工而来，相隔 {min(depths)} 跳")
    if not diff.common:
        return None
    common = [_field_id(ref.table, ref.column) for ref in diff.common]
    if diff.differs:
        return FieldComparison(term=term, a=a.field, b=b.field, relation="same_source_diff", common=common,
                               note=f"有共同的上游字段，加工口径不同：{_differences(diff)}")
    return FieldComparison(term=term, a=a.field, b=b.field, relation="same_source_same", common=common,
                           note="有共同的上游字段，来源、表达式和加工条件都一致")


def _lineage_and_comparisons(graph: LineageGraph, metrics: list[_MetricHit], terms: list[str], notes: list[str]
                             ) -> tuple[list[FieldLineage], list[FieldComparison]]:
    """指标命中字段的加工链路，以及同一个指标检索词命中的字段两两之间的同源对比

    同源不同口径的字段即使没排进 MAX_LINEAGE_FIELDS 的名额也补上链路，链路字段总数不超过 MAX_LINEAGE_TOTAL。
    """
    ranked = _distinct_fields(metrics)
    if len(ranked) > MAX_LINEAGE_FIELDS:
        notes.append(f"指标命中字段共 {len(ranked)} 个，只展开前 {MAX_LINEAGE_FIELDS} 个的加工链路")
    lineage: list[FieldLineage] = []
    cut = 0
    for item in ranked[:MAX_LINEAGE_FIELDS]:
        field_lineage, was_cut = _field_lineage(graph, item.table_id, item.column, item.term, notes)
        lineage.append(field_lineage)
        cut += was_cut

    comparisons: list[FieldComparison] = []
    compared: set[frozenset[str]] = set()
    for term in terms:
        hits = _distinct_fields(item for item in metrics if item.term == term)
        if len(hits) > MAX_COMPARE_FIELDS:
            notes.append(f"检索词「{term}」命中的字段共 {len(hits)} 个，只对比得分最高的前 {MAX_COMPARE_FIELDS} 个")
        for a, b in combinations(hits[:MAX_COMPARE_FIELDS], 2):
            pair = frozenset((a.field, b.field))
            if pair in compared:                                    # 同一对字段只记一次，记在第一个检索词下
                continue
            compared.add(pair)
            comparison = _compare(graph, a, b, term)
            if comparison is not None:
                comparisons.append(comparison)

    have = {item.field for item in lineage}
    by_field = {item.field: item for item in ranked}
    left_out: dict[str, None] = {}
    for comparison in comparisons:
        if comparison.relation != "same_source_diff":
            continue
        for field in (comparison.a, comparison.b):
            if field in have:
                continue
            if len(lineage) >= MAX_LINEAGE_TOTAL:
                left_out.setdefault(field)
                continue
            item = by_field[field]
            field_lineage, was_cut = _field_lineage(graph, item.table_id, item.column, comparison.term, notes)
            lineage.append(field_lineage)
            have.add(field)
            cut += was_cut
    if left_out:
        notes.append(f"同源不同口径的字段有 {len(left_out)} 个没有补充加工链路（链路字段总数上限 {MAX_LINEAGE_TOTAL} 个）")
    if cut:
        notes.append(f"{cut} 个加工步骤的过滤或关联条件超过 {MAX_CONDS} 条，只列了前 {MAX_CONDS} 条")
    return lineage, comparisons


# ── 表的上下游、关联路径、上溯选项 ──────────────────────────────────────

def _table_links(table_ids: list[str], closures: dict[str, list[str]]) -> list[TableLink]:
    """候选表两两之间的上下游：a 在 b 的上游表里，就记 a 是 b 的上游

    先后：按下游表在候选里的先后，同一张下游表按上游表在候选里的先后。
    """
    links = []
    for downstream in table_ids:
        upstream_tables = set(closures[downstream])
        links += [TableLink(upstream=upstream, downstream=downstream) for upstream in table_ids
                  if upstream != downstream and upstream in upstream_tables]
    return links


def _join_hints(store: MetadataStore, graph: LineageGraph, table_ids: list[str], notes: list[str]
                ) -> tuple[list[JoinHint], list[str]]:
    """候选表两两的关联路径

    直连的排在经过中间表的前面，同类的按候选表的先后；最多 MAX_JOIN_HINTS 条。
    中间表不在候选里时要补进证据，最多补 MAX_JOIN_TABLES 张，超出名额的那条路径不记。

    Returns:
        (关联路径, 要补进证据的中间表，按补进的先后)
    """
    found = []
    for left, right in combinations(table_ids, 2):
        path = graph.join_path(left, right)
        if path is None or not path.steps:
            continue
        hops = [JoinHop(left_table=step.left_table, left_columns=list(step.left_columns), right_table=step.right_table,
                        right_columns=list(step.right_columns), cardinality=step.cardinality, origin=step.origin,
                        note=step.note) for step in path.steps]
        found.append(JoinHint(left_table=left, right_table=right, hops=hops, fan_out=path.fan_out))
    found.sort(key=lambda hint: len(hint.hops))                     # 稳定排序：直连在前，同类仍按候选表的先后

    present = set(table_ids)
    middles: list[str] = []
    hints: list[JoinHint] = []
    no_room = 0
    for hint in found:
        if len(hints) >= MAX_JOIN_HINTS:
            notes.append(f"关联路径超过 {MAX_JOIN_HINTS} 条，只列了前 {MAX_JOIN_HINTS} 条（直连的排在前面）")
            break
        if len(hint.hops) > 1:
            middle = hint.hops[0].right_table
            if middle not in present and middle not in middles:
                if store.table(middle) is None:                     # 图和元数据库对不上，没法把它补进证据
                    continue
                if len(middles) >= MAX_JOIN_TABLES:
                    no_room += 1
                    continue
                middles.append(middle)
        hints.append(hint)
    if no_room:
        notes.append(f"关联路径要补充的中间表超过 {MAX_JOIN_TABLES} 张，有 {no_room} 条路径没有列出")
    return hints, middles


def _upstream_options(store: MetadataStore, tables: list[EvidenceTable], closures: dict[str, list[str]],
                      scope_level: int, notes: list[str]) -> list[UpstreamOption]:
    """还可以上溯到的表：候选里的非维表，各自在上游表里取下一层的，去重，去掉已在证据里的"""
    layer = NEXT_LAYER.get(scope_level)
    if layer is None:
        return []
    present = {table.table_id for table in tables}
    options: dict[str, UpstreamOption] = {}
    for table in tables:
        if table.reason == REASON_JOIN or table.table_type == "维表":
            continue
        for upstream in closures[table.table_id]:
            meta = store.table(upstream)
            if meta is not None and meta.layer == layer and upstream not in present and upstream not in options:
                options[upstream] = UpstreamOption(table_id=upstream, table_cn=meta.table_cn, layer=meta.layer)
    if len(options) > MAX_UPSTREAM_OPTIONS:
        notes.append(f"还可以上溯的表超过 {MAX_UPSTREAM_OPTIONS} 张，只列了前 {MAX_UPSTREAM_OPTIONS} 张")
    return list(options.values())[:MAX_UPSTREAM_OPTIONS]


# ── 对外接口 ──────────────────────────────────────

def build_evidence(store: MetadataStore, graph: LineageGraph, recall: RecallResult, query: RecallQuery, *,
                   use_lineage: bool, use_examples: bool, all_layers: bool = False,
                   join_graph: LineageGraph | None = None) -> Evidence:
    """由召回结果组装证据

    Args:
        store: 元数据库
        graph: 血缘图
        recall: ② 的结果
        query: ② 用的检索词，指标检索词决定展开哪些字段的血缘
        use_lineage: 是否给血缘、同源对比、表的上下游、上溯选项（B、C 组）
        use_examples: 是否给相似样例（C 组）
        all_layers: 召回是否在全部层级里排名（A 组），此时没有上溯选项
        join_graph: 求关联路径用的图，None 时用 graph。A 组传 structure_store 建出来的图

    Returns:
        证据
    """
    notes = list(recall.notes)
    candidates = []
    for candidate in recall.tables:                                 # 元数据库里没有的表、重复出现的表不进证据
        if store.table(candidate.table_id) is not None and candidate.table_id not in {c.table_id for c in candidates}:
            candidates.append(candidate)
    tables = [_evidence_table(store, store.table(c.table_id), c.hits, c.reason, c.score, use_lineage)
              for c in candidates]
    table_ids = [table.table_id for table in tables]

    lineage: list[FieldLineage] = []
    comparisons: list[FieldComparison] = []
    links: list[TableLink] = []
    closures: dict[str, list[str]] = {}
    if use_lineage:
        terms = _metric_terms(query)
        metrics = _metric_hits(candidates, terms)
        lineage, comparisons = _lineage_and_comparisons(_MemoGraph(graph), metrics, terms, notes)
        closures = {table_id: graph.upstream_tables(table_id) for table_id in table_ids}
        links = _table_links(table_ids, closures)

    hints, middles = _join_hints(store, join_graph if join_graph is not None else graph, table_ids, notes)
    tables += [_evidence_table(store, store.table(middle), [], REASON_JOIN, 0.0, use_lineage) for middle in middles]

    options = []
    if use_lineage and not all_layers:
        options = _upstream_options(store, tables, closures, recall.scope_level, notes)

    examples = []
    if use_examples:
        known = {example.example_id: example for example in store.examples()}
        for item in recall.examples:
            example = known.get(item.example_id)
            if example is None:
                notes.append(f"样例 {item.example_id} 在元数据库里找不到，已跳过")
                continue
            examples.append(EvidenceExample(example_id=example.example_id, requirement=example.requirement,
                                            caliber_note=example.caliber_note, sql_text=example.sql_text,
                                            tables=list(example.tables)))

    return Evidence(use_lineage=use_lineage, use_examples=use_examples, all_layers=all_layers,
                    scope_level=recall.scope_level, tables=tables, lineage=lineage, comparisons=comparisons,
                    table_links=links, joins=hints, upstream_options=options, examples=examples, notes=notes)


def drop_examples(recall: RecallResult) -> RecallResult:
    """A、B 组用：去掉样例和样例带入的表，其余不变

    Args:
        recall: ② 的结果

    Returns:
        新的召回结果，不修改传入的对象
    """
    return replace(recall, tables=[table for table in recall.tables if table.reason != REASON_EXAMPLE], examples=[],
                   notes=list(recall.notes))


def structure_store(store: MetadataStore) -> MetadataStore:
    """只有表结构的元数据：表、字段和 structural_relations 推出的关联，没有血缘、脚本、样例、向量

    A 组的关联路径用它建图。不能从元数据库里按来源筛：导入时同一种映射只留优先级最高的来源，
    表结构推得出的关联可能已经被血缘、脚本推出的同一映射顶掉了。

    Args:
        store: 完整的元数据库

    Returns:
        新的只读入口
    """
    tables = store.tables()
    columns = [column for table in tables for column in store.columns(table.table_id)]
    manual = [relation for relation in store.relations() if relation.origin == "manual"]
    relations = structural_relations(Mart(tables=tables, columns=columns, relations=manual))
    return MetadataStore.from_mart(Mart(tables=tables, columns=columns, relations=relations))
