"""
关联关系推导
四种来源按优先级合并：手工填写、加工脚本里出现过的等值关联、经直取血缘传递、同名粒度键。
同一对表按「无序表对 + 字段映射」去重，每对最多保留 MAX_MAPPINGS_PER_PAIR 种（手工的不受限），最优的排第一。

做法：
1. 手工：关联关系清单里填写的，原样保留，优先级最高。输入里来源不是 manual 的，是上一次推导的结果，不带进来
2. 脚本：加工脚本里追溯到实体表字段的等值键对，同一处关联里的多个键对按（左表, 右表）合成复合键。
   两张表都要在表清单里、字段都要在字段清单里才计为脚本关联
3. 血缘传递：被直取血缘连在一起的字段取值范围相同，称为一个键域（并查集求连通分量）。
   脚本里出现过「A.a = B.b」，A、B 所在键域里的其它表也能按这对字段关联。
   同一个键域里一张表只有一个字段才算候选；键域超过 MAX_DOMAIN_SIZE 个字段、单次候选超过 MAX_PROPAGATION_PER_JOIN 对
   都不传递；传递出的 N:N 一律丢掉（脚本里实际出现过的 N:N 保留）
4. 同名粒度键：表 T 的粒度字段（不全是 id 这类通用名）在表 S 里全有同名字段，S 的层级不低于 T（同层，或下游表关联上游表），
   S 就能按这些字段关联 T。T 被超过 MAX_GRAIN_FANIN 张表关联时整张跳过

对应关系按粒度判定；推导结果统一定向成 N:1（1:N 翻过来写），1:1、N:N、未知把下游层的表放左边。
遇到字段清单里没有的字段（脚本补出来的血缘边来源字段缺在清单里）、不在表清单里的外部表，都不出错：
它们可以在键域里连接别的字段，但出不了候选，也进不了关联

另有 structural_relations：只用表结构能推出的关联，即手工填写的加上同名粒度键，规则、上限和合并方式与上面完全一致，
不看脚本、不看血缘。对照实验的 A 组（仅模型结构）用它求关联路径。不能直接从元数据库里挑出 manual 和 grain_key 两种来源：
同一种映射只留优先级最高的来源，表结构上推得出的关联可能已经被脚本、血缘传递认领，库里记的就不是 grain_key 了
"""
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from itertools import product

from metadata.checks import ReportSection
from metadata.models import (
    DERIVED_CARDINALITIES,
    LAYERS,
    RELATION_ORIGINS,
    FieldRef,
    JoinKey,
    Mart,
    Relation,
    TableMeta,
)

SECTION_RELATIONS = "关联关系推导"
# 各表自己的行编号常用的名字：两张表的粒度恰好都叫这个名字，并不说明它们能关联。
# 后四个来自真实库的统计：按它们推出的同名粒度键几乎全是 1:1，实际是各表的行编号撞了名
GENERIC_KEY_NAMES = frozenset({"id", "uuid", "guid", "row_id", "rowid", "pk_id",
                               "record_id", "attachment_id", "relation_id", "member_id"})
MAX_DOMAIN_SIZE = 50              # 键域超过这么多字段时不从它传递
MAX_PROPAGATION_PER_JOIN = 400    # 一条脚本关联传递出的候选超过这么多对时整条跳过
MAX_GRAIN_FANIN = 150             # 一张表被超过这么多张表按同名粒度键关联时跳过
MAX_MAPPINGS_PER_PAIR = 3         # 同一对表最多保留几种字段映射

TOP_TABLES = 5                    # 报告里列几张被关联最多的表
MAX_SCRIPTS_IN_NOTE = 3           # 说明里最多写几个脚本名，多出来的写「等 N 个脚本」
ORIGIN_NAMES = {"manual": "手工", "script": "脚本", "lineage": "血缘传递", "grain_key": "同名粒度键"}

# 对应关系的安全度，越小越安全。1:N 是 N:1 反过来写，同样安全；N:N 最容易让行数发散
_SAFETY = {"N:1": 0, "1:1": 0, "1:N": 0, "未知": 1, "N:N": 2}
_SYMMETRIC = ("1:1", "N:N", "未知")           # 左右换位置含义不变，靠层级和表名定方向
_FLIPPED = {"N:1": "1:N", "1:N": "N:1"}


# ── 对应关系 ──────────────────────────────────────────────

def _covers(table: TableMeta, columns: list[str]) -> bool | None:
    """键是否覆盖了表的全部粒度字段；表没有粒度字段时判断不了，返回 None"""
    if not table.grain_columns:
        return None
    return set(table.grain_columns) <= set(columns)


def cardinality(left: TableMeta, left_cols: list[str], right: TableMeta, right_cols: list[str]) -> str:
    """按粒度判断对应关系

    关联键覆盖了某一侧表的全部粒度字段，这一侧就唯一；表没有粒度字段时这一侧记为未知。
    两侧都唯一是 1:1；只有右侧唯一是 N:1（左侧有没有粒度都一样：右侧唯一，左侧每行最多对上一行）；
    只有左侧唯一是 1:N；两侧都有粒度而都没覆盖是 N:N；其余判断不了，记为「未知」。

    Args:
        left: 左表
        left_cols: 左侧的关联字段
        right: 右表
        right_cols: 右侧的关联字段，与左侧按位置一一对应

    Returns:
        N:1、1:1、1:N、N:N 或「未知」
    """
    left_unique, right_unique = _covers(left, left_cols), _covers(right, right_cols)
    if left_unique and right_unique:
        return "1:1"
    if right_unique:
        return "N:1"
    if left_unique:
        return "1:N"
    if left_unique is False and right_unique is False:
        return "N:N"
    return "未知"


# ── 键域 ──────────────────────────────────────────────────

def _ref_key(ref: FieldRef) -> tuple[str, str]:
    """字段引用的排序键"""
    return ref.table, ref.column


class _UnionFind:
    """并查集，节点是字段引用"""

    def __init__(self) -> None:
        self._parent: dict[FieldRef, FieldRef] = {}

    def add(self, item: FieldRef) -> None:
        """登记一个节点，已有则不变"""
        self._parent.setdefault(item, item)

    def find(self, item: FieldRef) -> FieldRef:
        """所在连通分量的代表，顺带压缩路径"""
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: FieldRef, b: FieldRef) -> None:
        """合并两个节点所在的连通分量"""
        self.add(a)
        self.add(b)
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self._parent[root_b] = root_a

    def nodes(self) -> list[FieldRef]:
        """全部节点"""
        return list(self._parent)


def _direct_pairs(mart: Mart) -> Iterator[tuple[FieldRef, FieldRef]]:
    """直取对（来源, 目标）：合并后血缘里标为直取的边，以及脚本里类别为直取的目标字段

    清单与脚本不一致时两边都算：清单说是直取的，或者脚本说是直取的，取值范围都相同。
    """
    for edge in mart.lineage:
        if edge.is_passthrough:
            yield (FieldRef(table=edge.source_table, column=edge.source_column),
                   FieldRef(table=edge.target_table, column=edge.target_column))
    for column in mart.script_columns:
        if column.kind == "passthrough":
            target = FieldRef(table=column.target_table, column=column.target_column)
            for source in column.sources:
                yield source, target


def key_domains(mart: Mart) -> dict[FieldRef, int]:
    """求键域：被直取血缘连在一起的字段取值范围相同，属于同一个键域

    类比：同一个客户号从贴源层一路直取到专题层，这些字段说的是同一批值。
    节点是直取对的两端，加上出现在脚本关联键里的全部字段（没有直取边的单独成一个键域）。
    字段清单里没有的字段、外部表的字段也照常入键域，只是后面出不了关联。

    Args:
        mart: 已经解析过加工脚本、合并过血缘的资料

    Returns:
        字段 → 键域编号。编号从 0 开始，按每个键域最小的字段排序，与输入的顺序无关
    """
    forest = _UnionFind()
    for source, target in _direct_pairs(mart):
        forest.union(source, target)
    for statement in mart.scripts:
        for join in statement.joins:
            for key in join.keys:
                forest.add(key.left)
                forest.add(key.right)
    numbers: dict[FieldRef, int] = {}
    domains: dict[FieldRef, int] = {}
    for node in sorted(forest.nodes(), key=_ref_key):
        root = forest.find(node)
        if root not in numbers:
            numbers[root] = len(numbers)
        domains[node] = numbers[root]
    return domains


# ── 排序 ──────────────────────────────────────────────────

def _layer_number(table: TableMeta | None) -> int:
    """层级的序号：贴源层 0、整合层 1、专题层 2，越大越下游；不认识的记 -1"""
    return LAYERS.index(table.layer) if table is not None and table.layer in LAYERS else -1


def relation_rank(rel: Relation, tables: dict[str, TableMeta]) -> tuple:
    """关联关系的排序键：来源优先级、对应关系安全度、是否同名键、字典序，越小越优

    依次比较：
    1. 来源优先级：手工、脚本、血缘传递、同名粒度键
    2. 对应关系安全度：N:1、1:1（1:N 是 N:1 反过来写，同样安全）优先，其次「未知」，N:N 最后
    3. 两侧字段是否同名，同名的优先
    4. 左表的层级，越下游越靠前
    5. 表名、字段名的字典序，最后是对应关系和说明，保证是全序，输出顺序确定

    Args:
        rel: 关联关系
        tables: 表标识 → 表，用来查左表的层级；查不到的表按层级未知处理

    Returns:
        可以直接比较大小的元组
    """
    origin = RELATION_ORIGINS.index(rel.origin) if rel.origin in RELATION_ORIGINS else len(RELATION_ORIGINS)
    safety = _SAFETY.get(rel.cardinality, len(_SAFETY))
    same_name = 0 if list(rel.left_columns) == list(rel.right_columns) else 1
    return (origin, safety, same_name, -_layer_number(tables.get(rel.left_table)),
            rel.left_table, tuple(rel.left_columns), rel.right_table, tuple(rel.right_columns),
            rel.cardinality, rel.note)


# ── 映射 ──────────────────────────────────────────────────

def _mapping_key(left: str, left_cols: list[str] | tuple[str, ...], right: str,
                 right_cols: list[str] | tuple[str, ...]) -> frozenset:
    """字段映射的规范写法，与左右顺序、键对顺序无关；用来去重"""
    return frozenset(tuple(sorted(((left, a), (right, b)))) for a, b in zip(left_cols, right_cols))


def _relation_key(rel: Relation) -> frozenset:
    """一条关联的字段映射"""
    return _mapping_key(rel.left_table, rel.left_columns, rel.right_table, rel.right_columns)


def _pair_key(rel: Relation) -> tuple[str, str]:
    """一条关联所属的无序表对"""
    a, b = rel.left_table, rel.right_table
    return (a, b) if a <= b else (b, a)


@dataclass
class _ScriptMapping:
    """加工脚本里出现过的一种字段映射，两侧的写法取第一次出现时的"""

    left: str
    right: str
    pairs: list[tuple[str, str]]              # (左字段, 右字段)，复合键有几个字段就有几对
    scripts: list[str] = field(default_factory=list)      # 出现过的脚本文件名，按首次出现的顺序

    @property
    def left_columns(self) -> list[str]:
        """左侧的关联字段"""
        return [a for a, _ in self.pairs]

    @property
    def right_columns(self) -> list[str]:
        """右侧的关联字段"""
        return [b for _, b in self.pairs]

    @property
    def key(self) -> frozenset:
        """规范写法，用来去重"""
        return _mapping_key(self.left, self.left_columns, self.right, self.right_columns)

    def condition(self) -> str:
        """写成「表.字段 = 表.字段 AND …」"""
        return " AND ".join(f"{self.left}.{a} = {self.right}.{b}" for a, b in self.pairs)


def _group_keys(keys: list[JoinKey]) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """一处关联的键对按（左表, 右表）分组，每组是一种字段映射（复合键）

    两侧表相反的键对翻过来并入同一组。一组里同一侧出现重复字段时（同一个条件追溯出几个来源，是几选一而不是复合），
    拆成单个键对各算一种映射。
    """
    groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for key in keys:
        left, right = key.left, key.right
        if (right.table, left.table) in groups and (left.table, right.table) not in groups:
            left, right = right, left
        groups.setdefault((left.table, right.table), []).append((left.column, right.column))
    result: list[tuple[str, str, list[tuple[str, str]]]] = []
    for (left, right), pairs in groups.items():
        pairs = list(dict.fromkeys(pairs))
        if len({a for a, _ in pairs}) == len(pairs) and len({b for _, b in pairs}) == len(pairs):
            result.append((left, right, pairs))
        else:
            result.extend((left, right, [pair]) for pair in pairs)
    return result


def _script_names(names: list[str]) -> str:
    """说明里写的脚本名：最多写几个，多出来的写「等 N 个脚本」"""
    if len(names) <= MAX_SCRIPTS_IN_NOTE:
        return "、".join(names)
    return "、".join(names[:MAX_SCRIPTS_IN_NOTE]) + f" 等 {len(names)} 个脚本"


# ── 一次推导 ──────────────────────────────────────────────

@dataclass
class _Stats:
    """推导过程中的计数，写进报告"""

    occurrences: int = 0                      # 脚本里的等值关联处数（复合键算一处）
    distinct: int = 0                         # 去重后的映射种数
    external: int = 0                         # 其中一侧不在表清单里
    gap: int = 0                              # 其中用到字段清单里没有的字段
    domain_blocked: int = 0                   # 键域超限，没有传递的映射
    domain_combos: int = 0                    # 它们的候选组合数
    join_blocked: int = 0                     # 单次传递超限，整条跳过的映射
    join_combos: int = 0
    fanin_tables: int = 0                     # 被关联次数超限，整张跳过的表
    fanin_avoided: int = 0                    # 因此少生成的关联
    many_to_many: int = 0                     # 传递出的 N:N，丢掉
    merged: int = 0                           # 与更高优先级的来源映射相同，合并掉
    cut: int = 0                              # 超过每对上限，去掉
    generated: Counter = field(default_factory=Counter)   # 各来源去重之前的候选数


class _Deriver:
    """一次推导：持有表、字段、键域，依次产生四种来源的关联，再合并、定向、去重、排序"""

    def __init__(self, mart: Mart) -> None:
        self.mart = mart
        self.tables: dict[str, TableMeta] = {t.table_id: t for t in mart.tables}
        self.columns: dict[str, set[str]] = defaultdict(set)
        for column in mart.columns:
            self.columns[column.table_id].add(column.column_name)
        self.domains = key_domains(mart)
        self.sizes = Counter(self.domains.values())
        # 键域 → 表 → 该表在键域里的字段。只收表清单里的表、字段清单里有的字段：别的出不了候选
        self.holders: dict[int, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        for ref, number in self.domains.items():
            if ref.table in self.tables and ref.column in self.columns[ref.table]:
                self.holders[number][ref.table].append(ref.column)
        self.stats = _Stats()

    def run(self) -> tuple[list[Relation], ReportSection]:
        """依次产生四种来源的关联并合并，返回（关联关系, 报告节）"""
        manual = self._manual()
        mappings = self._script_mappings()
        scripted = self._script_relations(mappings)
        lineage = self._lineage_relations(mappings)
        grain = self._grain_relations()
        relations = self._merge([*manual, *scripted, *lineage, *grain])
        return relations, self._report(relations)

    def structural(self) -> list[Relation]:
        """只用表结构：手工和同名粒度键两种来源，合并、定向、排序同 run"""
        return self._merge([*self._manual(), *self._grain_relations()])

    def _manual(self) -> list[Relation]:
        """手工填写的关联；输入里来源不是 manual 的是上一次推导的结果，不带进来"""
        return [r for r in self.mart.relations if r.origin == "manual"]

    # ── 通用 ──

    def _real(self, table: str, columns: list[str]) -> bool:
        """表在表清单里，字段都在它的字段清单里"""
        return table in self.tables and all(c in self.columns[table] for c in columns)

    def _side_key(self, table: str, columns: list[str]) -> tuple:
        """对称的对应关系里决定谁放左边：下游层的在前，其次表名、字段名的字典序"""
        return -_layer_number(self.tables.get(table)), table, tuple(sorted(columns))

    def _relation(self, origin: str, a: str, a_cols: list[str], b: str, b_cols: list[str], note: str) -> Relation:
        """由一种映射生成定向后的关联：1:N 翻成 N:1，1:1、N:N、未知把下游层的表放左边，键对按字段名排序"""
        card = cardinality(self.tables[a], a_cols, self.tables[b], b_cols)
        flip = card == "1:N"
        if card in _SYMMETRIC:
            flip = self._side_key(b, b_cols) < self._side_key(a, a_cols)
        if flip:
            a, a_cols, b, b_cols = b, b_cols, a, a_cols
            card = _FLIPPED.get(card, card)
        pairs = sorted(zip(a_cols, b_cols))
        return Relation(left_table=a, left_columns=[p[0] for p in pairs], right_table=b,
                        right_columns=[p[1] for p in pairs], cardinality=card, origin=origin, note=note)

    # ── 脚本 ──

    def _script_mappings(self) -> dict[frozenset, _ScriptMapping]:
        """脚本里出现过的字段映射，按规范写法去重；顺序与脚本里第一次出现的顺序一致"""
        found: dict[frozenset, _ScriptMapping] = {}
        for statement in self.mart.scripts:
            for join in statement.joins:
                for left, right, pairs in _group_keys(join.keys):
                    self.stats.occurrences += 1
                    mapping = _ScriptMapping(left, right, pairs)
                    existing = found.setdefault(mapping.key, mapping)
                    if statement.script_name not in existing.scripts:
                        existing.scripts.append(statement.script_name)
        self.stats.distinct = len(found)
        return found

    def _script_relations(self, mappings: dict[frozenset, _ScriptMapping]) -> list[Relation]:
        """脚本来源的关联。两张表都要在表清单里、字段都要在字段清单里，N:N 照留"""
        relations: list[Relation] = []
        for mapping in mappings.values():
            if mapping.left not in self.tables or mapping.right not in self.tables:
                self.stats.external += 1
            elif not (self._real(mapping.left, mapping.left_columns)
                      and self._real(mapping.right, mapping.right_columns)):
                self.stats.gap += 1
            else:
                note = f"加工脚本 {_script_names(mapping.scripts)} 中的关联：{mapping.condition()}"
                relations.append(self._relation("script", mapping.left, mapping.left_columns, mapping.right,
                                                mapping.right_columns, note))
        self.stats.generated["script"] = len(relations)
        return relations

    # ── 血缘传递 ──

    def _side(self, domains: list[int]) -> list[tuple[str, tuple[str, ...]]]:
        """映射的一侧能换成哪些表：在每个键域里都恰好有一个字段的表（k 个键域取交集）

        Returns:
            (表, 各键域里的那个字段)，按表标识排序。同一个字段占了两个键域的位置时跳过
        """
        common: set[str] | None = None
        for number in domains:
            tables = {t for t, columns in self.holders.get(number, {}).items() if len(columns) == 1}
            common = tables if common is None else common & tables
        result = []
        for table in sorted(common or ()):
            columns = tuple(self.holders[number][table][0] for number in domains)
            if len(set(columns)) == len(columns):
                result.append((table, columns))
        return result

    def _lineage_relations(self, mappings: dict[frozenset, _ScriptMapping]) -> list[Relation]:
        """血缘传递：对每种脚本映射，两侧换成同一个键域里的其它表，生成 origin=lineage 的候选"""
        relations: list[Relation] = []
        for key, mapping in mappings.items():
            left_domains = [self.domains[FieldRef(table=mapping.left, column=c)] for c in mapping.left_columns]
            right_domains = [self.domains[FieldRef(table=mapping.right, column=c)] for c in mapping.right_columns]
            left_side, right_side = self._side(left_domains), self._side(right_domains)
            combos = len(left_side) * len(right_side)
            if any(self.sizes[number] > MAX_DOMAIN_SIZE for number in (*left_domains, *right_domains)):
                self.stats.domain_blocked += 1
                self.stats.domain_combos += combos
                continue
            if combos > MAX_PROPAGATION_PER_JOIN:
                self.stats.join_blocked += 1
                self.stats.join_combos += combos
                continue
            note = f"键域传递：{mapping.scripts[0]} 中 {mapping.condition()}"
            for (a, a_cols), (b, b_cols) in product(left_side, right_side):
                if a == b or _mapping_key(a, a_cols, b, b_cols) == key:
                    continue
                relation = self._relation("lineage", a, list(a_cols), b, list(b_cols), note)
                if relation.cardinality == "N:N":
                    self.stats.many_to_many += 1
                    continue
                self.stats.generated["lineage"] += 1
                relations.append(relation)
        return relations

    # ── 同名粒度键 ──

    def _grain_relations(self) -> list[Relation]:
        """同名粒度键：S 含有 T 的全部粒度字段（按字段名），S 的层级不低于 T，S 就按这些字段关联 T"""
        owners: dict[str, set[str]] = defaultdict(set)
        for table, names in self.columns.items():
            if table in self.tables:
                for name in names:
                    owners[name].add(table)
        relations: list[Relation] = []
        for target_id in sorted(self.tables):
            target = self.tables[target_id]
            grain = list(target.grain_columns)
            if not grain or all(name in GENERIC_KEY_NAMES for name in grain) or not self._real(target_id, grain):
                continue
            holders = set.intersection(*(owners[name] for name in grain)) - {target_id}
            sources = sorted(s for s in holders if _layer_number(self.tables[s]) >= _layer_number(target))
            if len(sources) > MAX_GRAIN_FANIN:
                self.stats.fanin_tables += 1
                self.stats.fanin_avoided += len(sources)
                continue
            for source_id in sources:
                note = f"同名粒度键：{source_id} 含有 {target_id} 的全部粒度字段（{'、'.join(grain)}）"
                self.stats.generated["grain_key"] += 1
                relations.append(self._relation("grain_key", source_id, grain, target_id, grain, note))
        return relations

    # ── 合并 ──

    def _merge(self, candidates: list[Relation]) -> list[Relation]:
        """按优先级去重，每对表只留最优的几种映射，最后排序

        candidates 已按来源优先级排好；同一映射先到的留下。每对表按 relation_rank 排序后留前
        MAX_MAPPINGS_PER_PAIR 种，手工关联不受这个上限限制。
        """
        chosen: dict[frozenset, Relation] = {}
        for relation in candidates:
            if chosen.setdefault(_relation_key(relation), relation) is not relation:
                self.stats.merged += 1
        pairs: dict[tuple[str, str], list[Relation]] = defaultdict(list)
        for relation in chosen.values():
            pairs[_pair_key(relation)].append(relation)
        kept: list[Relation] = []
        for relations in pairs.values():
            relations.sort(key=lambda r: relation_rank(r, self.tables))
            limit = max(MAX_MAPPINGS_PER_PAIR, sum(r.origin == "manual" for r in relations))
            kept.extend(relations[:limit])
            self.stats.cut += max(0, len(relations) - limit)
        kept.sort(key=lambda r: relation_rank(r, self.tables))
        return kept

    # ── 报告 ──

    def _report(self, relations: list[Relation]) -> ReportSection:
        """报告节：各来源条数、对应关系分布、每对表的映射数、各项上限挡掉的情况、被关联最多的表"""
        stats = self.stats
        by_origin = Counter(r.origin for r in relations)
        by_card = Counter(r.cardinality for r in relations)
        per_pair = Counter(_pair_key(r) for r in relations)
        mapping_counts = Counter(per_pair.values())
        top_count = max(MAX_MAPPINGS_PER_PAIR, max(mapping_counts, default=0))
        lines = [
            f"- 关联 {len(relations)} 条，涉及 {len(per_pair)} 对表："
            + "、".join(f"{ORIGIN_NAMES[origin]} {by_origin[origin]}" for origin in RELATION_ORIGINS),
            "- 对应关系：" + "、".join(f"{card} {by_card[card]}" for card in DERIVED_CARDINALITIES),
            "- 每对表的映射数：" + "、".join(f"{n} 种 {mapping_counts[n]} 对" for n in range(1, top_count + 1)),
            f"- 去重和每对上限之前的候选：脚本 {stats.generated['script']}、血缘传递 {stats.generated['lineage']}、"
            f"同名粒度键 {stats.generated['grain_key']}；与更高优先级的来源映射相同、合并掉 {stats.merged} 条",
            f"- 脚本里的等值关联 {stats.occurrences} 处，去重后 {stats.distinct} 种映射；"
            f"其中 {stats.external} 种有一侧不在表清单里、{stats.gap} 种用到字段清单里没有的字段，没有计为脚本关联",
            f"- 键域 {len(self.sizes)} 个，最大 {max(self.sizes.values(), default=0)} 个字段；"
            f"超过 {MAX_DOMAIN_SIZE} 个字段的键域挡掉 {stats.domain_blocked} 种脚本映射的传递"
            f"（这些映射的候选共 {stats.domain_combos} 对）",
            f"- 单次传递的候选超过 {MAX_PROPAGATION_PER_JOIN} 对而整条跳过 {stats.join_blocked} 种脚本映射"
            f"（这些映射的候选共 {stats.join_combos} 对）",
            f"- 同名粒度键：{stats.fanin_tables} 张表被超过 {MAX_GRAIN_FANIN} 张表关联，整张跳过"
            f"（少生成 {stats.fanin_avoided} 条）",
            f"- 传递出的 N:N 丢掉 {stats.many_to_many} 条",
            f"- 每对表超过 {MAX_MAPPINGS_PER_PAIR} 种映射而去掉 {stats.cut} 条",
            self._top_line(relations),
        ]
        return ReportSection(SECTION_RELATIONS, tuple(lines))

    @staticmethod
    def _top_line(relations: list[Relation]) -> str:
        """被关联最多的几张表：按关联的表数排，其次是关联条数，供人工检查有没有被误关联的枢纽表"""
        partners: dict[str, set[str]] = defaultdict(set)
        counts: Counter = Counter()
        for relation in relations:
            left, right = relation.left_table, relation.right_table
            counts[left] += 1
            if left != right:                     # 自关联只算一次，也不算关联了别的表
                counts[right] += 1
                partners[left].add(right)
                partners[right].add(left)
        if not counts:
            return f"- 被关联次数最多的 {TOP_TABLES} 张表：无"
        ranked = sorted(counts, key=lambda t: (-len(partners[t]), -counts[t], t))[:TOP_TABLES]
        listed = "、".join(f"{table}（{len(partners[table])} 张 / {counts[table]} 条）" for table in ranked)
        return f"- 被关联次数最多的 {TOP_TABLES} 张表（涉及的表数 / 关联条数）：{listed}"


def derive_relations(mart: Mart) -> tuple[list[Relation], ReportSection]:
    """推导全部关联关系，包含手工填写的

    按优先级合并四种来源：手工、脚本、血缘传递、同名粒度键。同一对表按字段映射去重，同一映射保留优先级最高的来源，
    每对表最多保留 MAX_MAPPINGS_PER_PAIR 种映射（手工的不受限）。推导结果统一定向：1:N 翻成 N:1，
    1:1、N:N、未知把下游层的表放左边；传递出的 N:N 丢掉，脚本里实际出现过的 N:N 保留。
    手工关联原样保留，不改方向和对应关系。输出按 relation_rank 排序，输入的顺序不影响结果。

    Args:
        mart: 已经解析过加工脚本、合并过血缘的资料

    Returns:
        (已定向、去重、排序的关联关系, 报告节)
    """
    return _Deriver(mart).run()


def structural_relations(mart: Mart) -> list[Relation]:
    """只用表结构能推出的关联关系：手工填写的，加上同名粒度键

    规则、上限（通用键名、层级、被关联次数、每对表的映射数）和合并、定向、排序都与 derive_relations 相同，
    不看加工脚本、不看血缘，输入里上一次推导出的非手工关联也不带进来。结果等于对只留下表、字段、手工关联的同一份资料
    做一次完整推导。

    Args:
        mart: 一套资料，只用到其中的表、字段和手工关联

    Returns:
        已定向、去重、排序的关联关系，来源只有 manual 和 grain_key
    """
    bare = Mart(tables=mart.tables, columns=mart.columns,
                relations=[r for r in mart.relations if r.origin == "manual"])
    return _Deriver(bare).structural()
