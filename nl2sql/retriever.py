"""
候选召回（流水线第 ② 步）
对每个指标、维度、过滤要素，在当前范围内的表里用向量相似度和中文关键字两路找字段，
合并后按表归组，保留最多 MAX_TABLES 张候选表；同时找最相似的样例，样例用到的表直接加入候选。

候选范围由上溯层数决定：
    0  专题层全部表，加整合层的维表
    1  再加入候选事实表的上游表中属于整合层的
    2  再加入属于贴源层的
上溯加入的上游表不占 MAX_TABLES 的名额，命中字段在这些表自己的范围里单独找，不与专题层的表争名次；
但数量有上限：只留有命中字段的，按表得分最多 MAX_UPSTREAM_TABLES 张。一张也没命中时无从比较，取最近的前 MAX_UPSTREAM_TABLES 张，
免得上溯一张表也不给。没留下的维表回到第 0 层，和专题层的表一起排名。

打分：
    关键字路  汉字二元组和英文单词的覆盖率：字段中文名占 0.7，表中文名占 0.3，检索词是字段中文名的子串再加 0.2
    向量路    字段向量与检索词向量的余弦相似度
    每个检索词两路各取前 PER_TERM_K 名，按名次做 RRF 合并；
    表得分 = 各检索词在该表命中字段里的最高分，乘以角色权重后相加。
通用字段（表的数据日期、行编号一类的通用键名、同一层里半数以上的表都有的样板字段）只在检索词本身提到它们的名字时才计分：
真实库上检索词恰好是表名的一部分时，表名会给这张表的每个字段都加分，它们就会占满命中字段的前几位。
加工脚本里恒为空、取加工时间的字段不参与召回。
加工脚本里多条语句写同一个字段时，只有每一条都是恒为空、取加工时间才排除：只要有一条是真正的加工，字段就还能用。
对照实验的 A 组（仅模型结构）没有上溯机制：all_layers 时全部层级的表一起排名，范围就是全部表。
向量器与建库时用的不是同一个、库里没有字段向量、没有传向量器时，只走关键字路，原因写进 notes：
同为 512 维的两种向量器混用不会报错，只会悄悄返回没有意义的结果。
"""
import unicodedata
from collections import defaultdict
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field

import numpy as np

from lineage.graph import LineageGraph
from metadata.index import EXAMPLE_KIND, FIELD_KIND, TOKEN_PATTERN, Embedder, VectorIndex, field_ref_id
from metadata.models import LAYERS
from metadata.relations import GENERIC_KEY_NAMES
from metadata.store import MetadataStore

MAX_TABLES = 8
MAX_UPSTREAM_TABLES = 8                       # 上溯加入的上游表最多留几张，真实库上第 2 层的上游闭包有几十张
MISSING_TABLES_PER_TERM = 2                   # ④ 说缺的每一样单独召回时取前几张
MAX_MISSING_TABLES = 6                        # 单独召回补进来的表最多几张
MAX_EXAMPLES = 3
PER_TERM_K = 30
RRF_K = 60
ROLE_WEIGHTS = {"metric": 1.0, "dimension": 0.7, "filter": 0.5}

MAX_HITS_PER_TABLE = 5                       # 每张候选表最多列出的命中字段
COLUMN_WEIGHT = 0.7                          # 关键字路：字段中文名覆盖率的权重
TABLE_WEIGHT = 0.3                           # 关键字路：表中文名覆盖率的权重
SUBSTRING_BONUS = 0.2                        # 检索词是字段中文名的子串时的加分
EXCLUDED_KINDS = frozenset({"null", "etl_time"})    # 加工脚本里恒为空、取加工时间的字段，不参与召回
DATA_DATE_CN = "数据日期"                      # 中文名是它的字段是数据日期，即使表清单里没把它登记成数据日期字段
BOILERPLATE_SHARE = 0.5                      # 同一层里超过这个比例的表都有的字段名（创建时间、年月日一类），按通用字段处理
BOILERPLATE_MIN_TABLES = 10                  # 样本太小时比例没有意义：至少出现在这么多张表里才算
SCORE_DIGITS = 10                            # 比较得分时保留的小数位：浮点误差不能打乱同分的先后
UPSTREAM_LAYERS = {1: ("整合层",), 2: ("整合层", "贴源层")}    # 上溯层数 → 上游表的层级
REASON_RECALL, REASON_UPSTREAM, REASON_EXAMPLE = "recall", "upstream", "example"
REASON_PREVIOUS, REASON_MISSING = "previous", "missing"
VIA_VECTOR, VIA_KEYWORD = "vector", "keyword"


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
    score: float                         # 两路 RRF 得分乘以要素的角色权重，一张表里每个要素最高的那个就是它对表得分的贡献
    via: tuple[str, ...]                 # 命中的路：vector、keyword


@dataclass
class TableCandidate:
    """一张候选表"""

    table_id: str
    reason: str                          # recall、previous、missing、upstream 或 example
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


@dataclass(frozen=True)
class _Field:
    """参与召回的一个字段"""

    index: int                           # 在全部字段里的序号
    ref: str                             # 向量集里的编号：模式.表.字段
    table_id: str
    column_name: str
    column_cn: str
    squashed_cn: str                     # 归一化并去掉空白的中文名，用来判断检索词是不是它的子串
    generic: bool                        # 通用字段：数据日期、通用键名、样板字段名，只有检索词提到它的名字才计分


@dataclass(frozen=True)
class _Term:
    """一个检索词和它的预处理结果"""

    text: str
    weight: float                        # 角色权重
    squashed: str
    features: frozenset[str]


@dataclass(frozen=True)
class _TableScore:
    """一张表在一个范围里的得分"""

    score: float
    terms: int                           # 命中的检索词个数
    hits: list[FieldHit]


def _normalize(text: str) -> str:
    """NFKC 归一（全角转半角）并转小写"""
    return unicodedata.normalize("NFKC", text).lower()


def _squash(text: str) -> str:
    """归一化并去掉全部空白"""
    return "".join(_normalize(text).split())


def _features(text: str) -> frozenset[str]:
    """文本的特征：连续汉字切成字二元组（单个孤立的汉字就是它自己），英文整词按下划线切开

    纯数字的词不要：它是取值（年份、编号），不是字段名的一部分，留着只会把真正的名字对上的比例拉低。
    """
    found: set[str] = set()
    for run in TOKEN_PATTERN.findall(_normalize(text)):
        if run.isascii():
            found.update(word for word in run.split("_") if word and not word.isdigit())
        elif len(run) == 1:
            found.add(run)
        else:
            found.update(run[start:start + 2] for start in range(len(run) - 1))
    return frozenset(found)


def _prepare_terms(query: RecallQuery) -> list[_Term]:
    """把三类要素摊平成检索词，空的和同一角色下重复的丢掉，顺序为指标、维度、过滤"""
    terms: list[_Term] = []
    seen: set[tuple[str, str]] = set()
    for role, texts in (("metric", query.metrics), ("dimension", query.dimensions), ("filter", query.filters)):
        for text in texts:
            squashed = _squash(text)
            if squashed and (role, squashed) not in seen:
                seen.add((role, squashed))
                terms.append(_Term(text.strip(), ROLE_WEIGHTS[role], squashed, _features(text)))
    return terms


def _rrf(rank: int) -> float:
    """名次（从 1 开始）对应的 RRF 得分"""
    return 1.0 / (RRF_K + rank)


def _round(score: float) -> float:
    """比较用的得分：去掉浮点误差，同分才会走到后面的先后规则"""
    return round(score, SCORE_DIGITS)


class Retriever:
    """候选召回"""

    def __init__(self, store: MetadataStore, graph: LineageGraph, embedder: Embedder | None) -> None:
        """建好关键字倒排索引和向量索引；之后每次召回只做查询

        Args:
            store: 元数据库的只读入口
            graph: 血缘图，上溯时求上游表用
            embedder: 检索词的向量器，必须与建库时用的是同一个；为 None 时只走关键字路
        """
        self._store = store
        self._graph = graph
        self._embedder = embedder
        self._tables = {table.table_id: table for table in store.tables()}
        self._level0 = sorted(table.table_id for table in store.tables()
                              if table.layer == "专题层" or (table.layer == "整合层" and table.table_type == "维表"))

        self._boilerplate = self._find_boilerplate()
        self._fields: list[_Field] = []
        self._by_ref: dict[str, int] = {}
        self._table_fields: dict[str, list[int]] = {}
        self._column_postings: dict[str, list[int]] = defaultdict(list)     # 特征 → 字段名里有它的字段
        self._table_postings: dict[str, list[str]] = defaultdict(list)      # 特征 → 表名里有它的表
        for table in store.tables():
            self._add_table(table.table_id)

        self._field_index, self._example_index, self._vector_note = self._open_vectors()
        self._examples = store.examples()
        self._example_features = {example.example_id: _features(example.requirement) for example in self._examples}

    def _find_boilerplate(self) -> set[tuple[str, str]]:
        """找出每一层里的「样板字段名」：超过一半的表都有的字段名，例如创建时间、年月日

        返回 (层级, 小写字段名) 的集合。像停用词一样，它们出现得太普遍，说明不了这张表讲的是什么。
        """
        tables_in_layer: dict[str, int] = defaultdict(int)
        holders: dict[tuple[str, str], int] = defaultdict(int)
        for table in self._store.tables():
            tables_in_layer[table.layer] += 1
            for name in {column.column_name.lower() for column in self._store.columns(table.table_id)}:
                holders[(table.layer, name)] += 1
        return {key for key, count in holders.items()
                if count >= max(BOILERPLATE_MIN_TABLES, BOILERPLATE_SHARE * tables_in_layer[key[0]])}

    def _add_table(self, table_id: str) -> None:
        """登记一张表和它的字段：排除加工脚本里恒为空、取加工时间的字段，建倒排索引

        多条语句写同一个字段时，每一条的类别都在排除之列才排除；先按字段归好类，不必对每个字段重新扫一遍脚本字段。
        """
        table = self._tables[table_id]
        for feature in _features(f"{table.table_cn} {table.table_name}"):
            self._table_postings[feature].append(table_id)
        written: dict[str, list[str]] = defaultdict(list)           # 字段 → 写它的各条语句里的表达式类别
        for script_column in self._store.script_columns(table_id):
            written[script_column.target_column].append(script_column.kind)
        indices: list[int] = []
        for column in self._store.columns(table_id):
            kinds = written.get(column.column_name)
            if kinds and all(kind in EXCLUDED_KINDS for kind in kinds):
                continue
            name = column.column_name.lower()
            generic = (name in GENERIC_KEY_NAMES or column.column_name == table.date_column
                       or column.column_cn.strip() == DATA_DATE_CN or (table.layer, name) in self._boilerplate)
            item = _Field(index=len(self._fields), ref=field_ref_id(table_id, column.column_name), table_id=table_id,
                          column_name=column.column_name, column_cn=column.column_cn,
                          squashed_cn=_squash(column.column_cn), generic=generic)
            self._fields.append(item)
            self._by_ref[item.ref] = item.index
            indices.append(item.index)
            for feature in _features(f"{column.column_cn} {column.column_name}"):
                self._column_postings[feature].append(item.index)
        self._table_fields[table_id] = indices

    def _open_vectors(self) -> tuple[VectorIndex | None, VectorIndex | None, str | None]:
        """核对向量器后打开字段、样例的向量索引；用不了时返回原因

        Returns:
            (字段向量索引, 样例向量索引, 向量路用不了的原因)。原因不为 None 时两个索引都是 None
        """
        field_vectors = self._store.vectors(FIELD_KIND)
        if self._embedder is None:
            reason = "没有传入向量器"
        elif field_vectors is None or not field_vectors.ref_ids:
            reason = "元数据库里没有字段向量（导入时可能加了 --no-index）"
        elif self._store.embedder_name != self._embedder.name:
            reason = (f"元数据库的向量由 {self._store.embedder_name} 算出，当前向量器是 {self._embedder.name}，"
                      "两者的向量不能比较")
        elif field_vectors.matrix.shape[1] != self._embedder.dim:
            reason = f"元数据库的向量是 {field_vectors.matrix.shape[1]} 维，当前向量器是 {self._embedder.dim} 维"
        else:
            example_vectors = self._store.vectors(EXAMPLE_KIND)
            return VectorIndex(field_vectors), VectorIndex(example_vectors) if example_vectors else None, None
        return None, None, f"{reason}，字段只走关键字路，样例只按文字相似度排序"

    # ── 范围 ──

    def scope_tables(self, level: int, base_tables: Sequence[str] = ()) -> tuple[list[str], list[str]]:
        """返回 (范围内的表, 上溯加入的上游表)

        第 0 层是专题层全部表加整合层的维表。第 1、2 层在此之上，对 base_tables 里不是维表的每张表，
        加入它的上游表闭包里属于整合层（第 2 层再加贴源层）的表。后者是返回值的第二项，
        也包含在第一项里（维表本来就在第 0 层里，只出现一次）。

        Args:
            level: 上溯层数，0、1 或 2
            base_tables: 上溯的起点，通常是上一轮召回的候选表

        Returns:
            (范围内的表, 上游表)。上游表是完整的闭包，近的在前、不重复，recall 再从中按命中字段留下最多 MAX_UPSTREAM_TABLES 张；
            范围内的表先是第 0 层（按表标识排序），再是其余的上游表

        Raises:
            ValueError: level 不是 0、1、2，或 level 大于 0 而 base_tables 为空
        """
        if level not in (0, 1, 2):
            raise ValueError(f"上溯层数只能是 0、1、2，收到 {level}")
        if level == 0:
            return list(self._level0), []
        if not base_tables:
            raise ValueError(f"上溯 {level} 层需要给出起点表 base_tables")
        upstream: list[str] = []
        seen: set[str] = set()
        for table_id in base_tables:
            table = self._tables.get(table_id)
            if table is None or table.table_type == "维表":
                continue
            for source in self._graph.upstream_tables(table_id, layers=UPSTREAM_LAYERS[level]):
                if source not in seen:
                    seen.add(source)
                    upstream.append(source)
        in_level0 = set(self._level0)
        return self._level0 + [table_id for table_id in upstream if table_id not in in_level0], upstream

    # ── 召回 ──

    def recall(self, query: RecallQuery, scope_level: int = 0, base_tables: Sequence[str] = (), *,
               all_layers: bool = False, carry_tables: Sequence[str] = (),
               missing_terms: Sequence[str] = ()) -> RecallResult:
        """召回候选表和相似样例

        候选表分五类，按 recall、previous、missing、upstream、example 的顺序排列：
        recall 是范围内（第 0 层）得分最高的前 MAX_TABLES 张；
        previous 是追问时上一轮 SQL 用到的表（carry_tables），原样带进来；
        missing 是 ④ 最近一次说缺的东西（missing_terms）逐条单独在第 0 层排名，轮流各取一张，每条最多
        MISSING_TABLES_PER_TERM 张、总共最多 MAX_MISSING_TABLES 张：缺的东西混在全部检索词里一起加总得分时，
        会被原来的检索词冲淡；
        upstream 是上溯加入的表，只留有命中字段的前 MAX_UPSTREAM_TABLES 张，都没命中时取最近的前 MAX_UPSTREAM_TABLES 张；
        example 是相似样例用到的表。除 recall 以外都不占名额，前面几类里已经有的表不重复。

        all_layers 是对照实验 A 组用的：排名池是全部层级的全部表，没有上溯，所以没有 upstream、missing 两类，
        scope_size 是全部表数，scope_level 只能是 0。

        Args:
            query: 完整需求和拆出来的指标、维度、过滤
            scope_level: 上溯层数，0、1 或 2
            base_tables: 上溯的起点，scope_level 大于 0 时必须给出
            all_layers: 全部表一起排名，不上溯
            carry_tables: 追问时上一轮 SQL 用到的表；元数据里没有的跳过
            missing_terms: ④ 说缺的东西（上溯时才有）

        Returns:
            召回结果，说明里记着走了哪几路

        Raises:
            ValueError: scope_level 大于等于 1 而 base_tables 为空；all_layers 时 scope_level 不是 0
        """
        if all_layers:
            if scope_level != 0:
                raise ValueError(f"all_layers 时全部表一起排名，没有上溯，scope_level 只能是 0，收到 {scope_level}")
            scope, upstream = sorted(self._tables), []
        else:
            scope, upstream = self.scope_tables(scope_level, base_tables)
        terms = _prepare_terms(query)
        notes = [self._vector_note] if self._vector_note else []
        if not terms:
            notes.append("没有检索词，候选表只来自上溯和相似样例")

        vectors, requirement_vector = self._embed(terms, query.requirement)
        upstream_scored, upstream_matched = self._score_tables(terms, vectors, upstream)
        kept = self._keep_upstream(upstream, upstream_scored)
        taken = set(kept)       # 上游表单独排名；没留下的维表仍在第 0 层里，回到池子里和专题层的表一起排
        pool = [table_id for table_id in (scope if all_layers else self._level0) if table_id not in taken]
        scored, matched = self._score_tables(terms, vectors, pool)
        notes += [f"检索词「{term.text}」没有命中任何字段"
                  for position, term in enumerate(terms) if position not in matched | upstream_matched]

        ranked = sorted((table_id for table_id, item in scored.items() if item.score > 0),
                        key=lambda table_id: self._order_key(table_id, scored.get(table_id)))
        tables = [self._candidate(table_id, REASON_RECALL, scored) for table_id in ranked[:MAX_TABLES]]
        have = {candidate.table_id for candidate in tables}
        for table_id in carry_tables:
            if table_id in self._tables and table_id not in have:
                have.add(table_id)
                tables.append(self._candidate(table_id, REASON_PREVIOUS, {**upstream_scored, **scored}))
        if not all_layers:
            tables += self._missing_candidates(missing_terms, have)
        tables += [self._candidate(table_id, REASON_UPSTREAM, upstream_scored) for table_id in kept if table_id not in have]
        have.update(kept)

        examples = self._similar_examples(query.requirement, requirement_vector)
        if self._examples and self._vector_note is None and self._example_index is None:
            notes.append("元数据库里没有样例向量，样例只按文字相似度排序")
        for example in examples:
            for table_id in example.tables:
                if table_id in self._tables and table_id not in have:
                    have.add(table_id)
                    tables.append(self._candidate(table_id, REASON_EXAMPLE, {**upstream_scored, **scored}))
        return RecallResult(scope_level=scope_level, scope_size=len(scope), tables=tables, examples=examples,
                            notes=notes)

    def _missing_candidates(self, missing_terms: Sequence[str], have: set[str]) -> list[TableCandidate]:
        """④ 说缺的东西逐条单独在第 0 层排名，每条最多补 MISSING_TABLES_PER_TERM 张还不在候选里的表

        轮流取：先给每一条补它排第一的表，再补排第二的，免得前面几条把名额用光；总共最多 MAX_MISSING_TABLES 张。
        补进来的表登记进 have。没有缺的东西时返回空列表。
        """
        terms = _prepare_terms(RecallQuery(requirement="", metrics=list(missing_terms)))
        if not terms:
            return []
        vectors, _ = self._embed(terms, "")
        pool = [table_id for table_id in self._level0 if table_id not in have]
        rankings = []                                           # 每条缺的东西：(按得分排好的表, 得分)
        for position, term in enumerate(terms):
            scored, _ = self._score_tables([term], vectors[position:position + 1] if vectors is not None else None, pool)
            rankings.append((sorted((table_id for table_id, item in scored.items() if item.score > 0),
                                    key=lambda table_id: self._order_key(table_id, scored[table_id])), scored))
        picked: list[TableCandidate] = []
        taken = [0] * len(rankings)                             # 每条已经补了几张
        for _ in range(MISSING_TABLES_PER_TERM):
            for index, (ranked, scored) in enumerate(rankings):
                table_id = next((table_id for table_id in ranked if table_id not in have), None)
                if table_id is None or taken[index] == MISSING_TABLES_PER_TERM:
                    continue
                if len(picked) == MAX_MISSING_TABLES:
                    return picked
                have.add(table_id)
                taken[index] += 1
                picked.append(self._candidate(table_id, REASON_MISSING, scored))
        return picked

    def _keep_upstream(self, upstream: list[str], scored: dict[str, _TableScore]) -> list[str]:
        """上溯加入的表里留下的，已排好先后

        有命中字段的按表得分排（先后规则同 _order_key），最多 MAX_UPSTREAM_TABLES 张，没有命中字段的不要。
        一张也没命中时无从比较（没有检索词、检索词都对不上），取闭包里最近的前 MAX_UPSTREAM_TABLES 张，
        upstream 里近的在前、起点表的先后由调用方决定，所以结果是确定的。
        """
        hit = sorted((table_id for table_id in upstream if table_id in scored),
                     key=lambda table_id: self._order_key(table_id, scored[table_id]))
        return hit[:MAX_UPSTREAM_TABLES] if hit else upstream[:MAX_UPSTREAM_TABLES]

    def _embed(self, terms: list[_Term], requirement: str) -> tuple[np.ndarray | None, np.ndarray | None]:
        """一次算出全部检索词和需求文本的向量；向量路用不了时都是 None"""
        if self._vector_note is not None or self._embedder is None:
            return None, None
        texts = [term.text for term in terms]
        with_requirement = bool(requirement.strip()) and self._example_index is not None
        if with_requirement:
            texts.append(requirement)
        if not texts:
            return None, None
        matrix = np.asarray(self._embedder.embed(texts), dtype=np.float32)
        return (matrix[:len(terms)] if terms else None), (matrix[-1] if with_requirement else None)

    def _order_key(self, table_id: str, item: _TableScore | None) -> tuple:
        """表的先后：得分高的在前；同分依次是命中的检索词多的、层级高的、表标识小的"""
        layer = LAYERS.index(self._tables[table_id].layer) if self._tables[table_id].layer in LAYERS else -1
        return (-_round(item.score) if item else 0.0, -item.terms if item else 0, -layer, table_id)

    @staticmethod
    def _candidate(table_id: str, reason: str, scored: dict[str, _TableScore]) -> TableCandidate:
        """组装一张候选表；这张表在范围里没有命中字段时得分为 0、命中字段为空"""
        item = scored.get(table_id)
        return TableCandidate(table_id=table_id, reason=reason, score=item.score if item else 0.0,
                              hits=list(item.hits) if item else [])

    # ── 字段两路召回 ──

    def _score_tables(self, terms: list[_Term], vectors: np.ndarray | None,
                      tables: Collection[str]) -> tuple[dict[str, _TableScore], set[int]]:
        """在给定的这些表里，为每个检索词两路找字段、RRF 合并，再按表归组

        Returns:
            (有命中的表 → 得分, 有命中的检索词序号)
        """
        scope = set(tables)
        if not scope or not terms:
            return {}, set()
        allowed = self._allowed_refs(scope) if vectors is not None else set()
        best: dict[str, list[float]] = {}                       # 表 → 每个检索词在这张表里的最高得分
        hits: dict[int, tuple[float, int, FieldHit]] = {}       # 字段 → (得分, 检索词序号, 命中)，每个字段留得分最高的检索词
        matched: set[int] = set()
        for position, term in enumerate(terms):
            column_hits, table_hits = self._count_matches(term)
            fused: dict[int, list] = {}                         # 字段 → [RRF 得分, 向量路命中, 关键字路命中]
            if vectors is not None:
                for rank, index in enumerate(self._vector_ranking(vectors[position], column_hits, scope, allowed), 1):
                    entry = fused.setdefault(index, [0.0, False, False])
                    entry[0] += _rrf(rank)
                    entry[1] = True
            for rank, index in enumerate(self._keyword_ranking(term, column_hits, table_hits, scope), 1):
                entry = fused.setdefault(index, [0.0, False, False])
                entry[0] += _rrf(rank)
                entry[2] = True
            if fused:
                matched.add(position)
            for index, (score, by_vector, by_keyword) in fused.items():
                item = self._fields[index]
                weighted = term.weight * score
                row = best.setdefault(item.table_id, [0.0] * len(terms))
                row[position] = max(row[position], weighted)
                known = hits.get(index)
                if known is None or weighted > known[0]:
                    via = tuple(name for name, flag in ((VIA_VECTOR, by_vector), (VIA_KEYWORD, by_keyword)) if flag)
                    hits[index] = (weighted, position, FieldHit(
                        table_id=item.table_id, column_name=item.column_name, column_cn=item.column_cn,
                        term=term.text, score=weighted, via=via))

        by_table: dict[str, list[tuple[float, int, str, FieldHit]]] = defaultdict(list)
        for index, (weighted, position, hit) in hits.items():
            by_table[hit.table_id].append((-_round(weighted), position, self._fields[index].ref, hit))
        scored = {}
        for table_id, row in best.items():
            ordered = sorted(by_table[table_id], key=lambda entry: entry[:3])
            scored[table_id] = _TableScore(score=sum(row), terms=sum(1 for value in row if value > 0),
                                           hits=[entry[3] for entry in ordered[:MAX_HITS_PER_TABLE]])
        return scored, matched

    def _allowed_refs(self, scope: Collection[str]) -> set[str]:
        """向量路可以检索的字段编号：范围内的非通用字段"""
        return {self._fields[index].ref for table_id in scope for index in self._table_fields.get(table_id, ())
                if not self._fields[index].generic}

    def _count_matches(self, term: _Term) -> tuple[dict[int, int], dict[str, int]]:
        """检索词的特征在哪些字段名、哪些表名里出现，各出现了几个"""
        column_hits: dict[int, int] = defaultdict(int)
        table_hits: dict[str, int] = defaultdict(int)
        for feature in term.features:
            for index in self._column_postings.get(feature, ()):
                column_hits[index] += 1
            for table_id in self._table_postings.get(feature, ()):
                table_hits[table_id] += 1
        return column_hits, table_hits

    def _keyword_ranking(self, term: _Term, column_hits: dict[int, int], table_hits: dict[str, int],
                         scope: Collection[str]) -> list[int]:
        """关键字路：范围内得分最高的前 PER_TERM_K 个字段

        得分 = 0.7 × 检索词的特征在字段名里的覆盖率 + 0.3 × 在表名里的覆盖率，检索词是字段中文名的子串再加 0.2。
        通用字段只算自己名字的部分，表名对上了不算。得分相同时，中文名长度与检索词最接近的在前，再按字段编号。
        """
        total = len(term.features)
        scores: dict[int, float] = {}
        for index, count in column_hits.items():
            item = self._fields[index]
            if item.table_id in scope:
                bonus = SUBSTRING_BONUS if term.squashed in item.squashed_cn else 0.0
                scores[index] = COLUMN_WEIGHT * count / total + bonus
        for table_id, count in table_hits.items():
            if table_id in scope:
                for index in self._table_fields[table_id]:
                    if not self._fields[index].generic:
                        scores[index] = scores.get(index, 0.0) + TABLE_WEIGHT * count / total
        order = sorted(scores, key=lambda index: (-_round(scores[index]),
                                                  abs(len(self._fields[index].squashed_cn) - len(term.squashed)),
                                                  self._fields[index].ref))
        return order[:PER_TERM_K]

    def _vector_ranking(self, vector: np.ndarray, column_hits: dict[int, int], scope: Collection[str],
                        allowed: set[str]) -> list[int]:
        """向量路：范围内与检索词最相似的前 PER_TERM_K 个字段

        通用字段不参与检索，除非检索词本身对上了它的名字（column_hits 里有它）。
        """
        if not np.any(vector):
            return []
        named = [self._fields[index].ref for index in column_hits
                 if self._fields[index].generic and self._fields[index].table_id in scope]
        found = self._field_index.search(vector, PER_TERM_K, allowed.union(named) if named else allowed)
        return [self._by_ref[ref] for ref, _ in found]

    # ── 样例 ──

    def _similar_examples(self, requirement: str, vector: np.ndarray | None) -> list[ExampleHit]:
        """最相似的前 MAX_EXAMPLES 条样例：需求文本的余弦名次与二元组 Jaccard 名次做 RRF

        没有样例向量（或向量路用不了）时只用 Jaccard。两路各自只收有相似度的样例：一点重叠也没有的不入榜。
        """
        features = _features(requirement)
        if not self._examples or not features:
            return []
        overlap: dict[str, float] = {}
        for example in self._examples:
            other = self._example_features[example.example_id]
            common = len(features & other)
            if common:
                overlap[example.example_id] = common / len(features | other)
        scores: dict[str, float] = defaultdict(float)
        for rank, example_id in enumerate(sorted(overlap, key=lambda i: (-_round(overlap[i]), i)), 1):
            scores[example_id] += _rrf(rank)
        if vector is not None and self._example_index is not None and np.any(vector):
            nearest = self._example_index.search(vector, len(self._examples))
            for rank, (example_id, _) in enumerate([pair for pair in nearest if pair[1] > 0], 1):
                scores[example_id] += _rrf(rank)
        by_id = {example.example_id: example for example in self._examples}
        top = sorted(scores, key=lambda i: (-_round(scores[i]), i))[:MAX_EXAMPLES]
        return [ExampleHit(example_id=i, requirement=by_id[i].requirement, score=scores[i],
                           tables=list(by_id[i].tables)) for i in top]
