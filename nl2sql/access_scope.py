"""按业务域限制模型证据、SQL 和执行范围。"""
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from access_control import Principal, access_store
from lineage.graph import LineageGraph
from metadata.models import Mart
from metadata.sqlscope import parse_statements, table_id, unwrap
from metadata.store import MetadataStore, VectorBundle, VectorSet
from nl2sql.retriever import Retriever
from nl2sql.validator import SqlValidator

DENIED = "当前授权范围无法完成该请求"


class AccessDenied(PermissionError):
    """查询命中域外资源或在执行中失去权限。"""

    def __init__(self) -> None:
        super().__init__(DENIED)


def sql_tables(sql: str) -> frozenset[str]:
    """枚举单条查询的底层表，要求实体表全限定且可解析。"""
    try:
        statements = parse_statements(sql)
        if len(statements) != 1 or not isinstance(unwrap(statements[0]), (exp.Select, exp.SetOperation)):
            raise AccessDenied()
        tree = statements[0]
        cte_refs = {id(node) for scope in traverse_scope(tree)
                    for node, source in scope.selected_sources.values()
                    if isinstance(node, exp.Table) and isinstance(source, Scope) and not node.db and not node.catalog}
        identifiers = set()
        for table in tree.find_all(exp.Table):
            if id(table) in cte_refs:
                continue
            if table.catalog or not isinstance(table.this, exp.Identifier) or not isinstance(table.args.get("db"), exp.Identifier):
                raise AccessDenied()
            identifiers.add(table_id(table))
        return frozenset(identifiers)
    except Exception:
        # 解析异常可能附带原 SQL，受控接口只返回固定错误。
        raise AccessDenied() from None


def table_ids(store: MetadataStore, domains: frozenset[str]) -> frozenset[str]:
    """取得明确归属于已授权业务域的表。"""
    return frozenset(table.table_id for table in store.tables() if table.domain_code in domains)


@lru_cache(maxsize=2)
def scoped_metadata(store: MetadataStore, allowed: frozenset[str]) -> tuple[MetadataStore, LineageGraph]:
    """构造同一元数据版本下的授权视图，范围外证据不参与排序。"""
    tables = [table for table in store.tables() if table.table_id in allowed]
    examples = []
    for example in store.examples():
        if example.tables and allowed.issuperset(example.tables):
            try:
                if sql_tables(example.sql_text) == frozenset(example.tables):
                    examples.append(example)
            except AccessDenied:
                pass
    mart = Mart(
        tables=tables,
        columns=[column for table in tables for column in store.columns(table.table_id)],
        # 文本加工表达式可能引用其它表，保留经检查的结构化关系。
        lineage=[edge.model_copy(update={"transform": "", "script_name": ""})
                 for edge in store.lineage_edges()
                 if edge.source_table in allowed and edge.target_table in allowed and not edge.source_external],
        relations=[relation.model_copy(update={"note": ""}) for relation in store.relations()
                   if relation.left_table in allowed and relation.right_table in allowed],
        examples=examples,
    )
    refs = {"table": allowed,
            "field": {f"{column.table_id}.{column.column_name}" for column in mart.columns},
            "example": {example.example_id for example in examples}}
    sets = {}
    for kind, ids in refs.items():
        vectors = store.vectors(kind)
        if vectors is not None:
            indices = [index for index, ident in enumerate(vectors.ref_ids) if ident in ids]
            if indices:
                # 全部可见时共享只读向量，避免管理员范围复制整个向量矩阵。
                sets[kind] = vectors if len(indices) == len(vectors.ref_ids) else VectorSet(
                    kind, [vectors.ref_ids[i] for i in indices],
                    [vectors.texts[i] for i in indices], vectors.matrix[indices])
    bundle = VectorBundle(store.embedder_name, next(iter(sets.values())).matrix.shape[1], sets) if sets else None
    scoped = MetadataStore.from_mart(mart, bundle, version_id=store.version_id)
    return scoped, LineageGraph(scoped)


@dataclass(frozen=True)
class AccessScope:
    """绑定单轮查询的身份、元数据版本和授权范围。"""

    token: str
    user: Principal
    store: MetadataStore
    tables: frozenset[str]
    current_store: Callable[[], MetadataStore]

    def check_current(self) -> None:
        """会话失效、授权撤回或元数据变化时停止本轮。"""
        current = access_store().resolve(self.token)
        if current is None or current.id != self.user.id or self.current_store() is not self.store:
            raise AccessDenied()
        if self.user.admin and not current.admin:
            raise AccessDenied()
        if not self.user.admin and not current.domains.issuperset(self.user.domains):
            raise AccessDenied()

    def check_sql(self, sql: str) -> None:
        """禁止访问未授权表，并限制查询为可确定来源的 SQL。"""
        if not self.tables.issuperset(sql_tables(sql)):
            raise AccessDenied()

    def check_names(self, names: Iterable[str], *, exact: bool = True) -> None:
        """处理明确点名的域外表，避免按相似名称改查其它表。"""
        texts = [value.strip().casefold() for value in names]
        tables = self.store.tables()
        visible = [table for table in tables if table.table_id in self.tables]
        visible_bare = {table.table_name.casefold() for table in visible}
        visible_cn = {table.table_cn.casefold() for table in visible if table.table_cn}
        spans = [] if exact else [
            (value, [match.span() for name in visible_cn if name in value
                     for match in re.finditer(re.escape(name), value)])
            for value in texts
        ]
        for table in tables:
            if table.table_id in self.tables:
                continue
            ident, bare = table.table_id.casefold(), table.table_name.casefold()
            cn = table.table_cn.casefold()
            names_to_check = [ident]
            if bare not in visible_bare:
                names_to_check.append(bare)
            if exact:
                if any(value in names_to_check or (cn and value == cn and cn not in visible_cn)
                       for value in texts):
                    raise AccessDenied()
            elif any(re.search(rf"(?<![\w.]){re.escape(name)}(?![\w])", value, flags=re.ASCII)
                     for name in names_to_check for value in texts):
                raise AccessDenied()
            elif len(cn) >= 4 and cn not in visible_cn:
                # 已授权全名里的短名称不单独算点名，单独或更完整的域外名称仍拦截。
                for value, visible_spans in spans:
                    if cn in value and any(
                        not any(start <= match.start() and match.end() <= end for start, end in visible_spans)
                        for match in re.finditer(re.escape(cn), value)
                    ):
                        raise AccessDenied()


@lru_cache(maxsize=1)
def scoped_indexes(store: MetadataStore, graph: LineageGraph, dialect: str) -> tuple[Retriever, SqlValidator]:
    """复用相同权限范围的索引与校验目录。"""
    from nl2sql.service import embedder_for

    return Retriever(store, graph, embedder_for(store)), SqlValidator(store, dialect=dialect)
