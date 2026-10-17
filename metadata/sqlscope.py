"""
SQL 作用域解析
加工脚本解析器和静态校验器共用的底层能力：
- 别名指向实体表还是 CTE、派生表，相关子查询沿外层查找
- 按查询层只给用到的表建结构，再限定字段，顺带找出字段清单的缺口
- 把输出字段一层层追溯到实体表字段，判定直取、加工、空值、常量、加工时间
- 把表达式里的 CTE、派生表展开成实体表全名写法
- 找出 ON、USING、WHERE 里的等值关联键对
"""
import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.schema import MappingSchema

from metadata.models import ColumnMeta, FieldRef, JoinKey, TableMeta

DIALECT = "postgres"
# 写法像字段、其实是系统值的名字。不带别名、又没有任何来源有这个字段时按系统值处理
PSEUDO_COLUMNS = frozenset({"sysdate", "current_user", "session_user", "user", "rownum", "localtimestamp"})
# 取加工时间的函数名。sqlglot 认不出、解析成普通函数调用时按名字识别
ETL_TIME_FUNCTIONS = frozenset({
    "now", "current_timestamp", "current_date", "current_time", "sysdate", "systimestamp",
    "localtimestamp", "localtime", "clock_timestamp", "statement_timestamp", "transaction_timestamp",
})
TIME_PSEUDO_COLUMNS = frozenset({"sysdate", "localtimestamp"})
MAX_KEY_PAIRS = 4             # 一个等值条件两侧来源的笛卡尔积最多取几组（UNION 会产生多个来源）
RENDER_MAX_DEPTH = 8          # 展开表达式文本时最多穿过几层查询
ELLIPSIS = "…"

# 给 qualify 的固定参数：不校验、不加引号，未知的字段留给调用方判断
QUALIFY_OPTIONS = {"validate_qualify_columns": False, "quote_identifiers": False}
# 建小结构时字段类型一律填未知，限定字段用不到类型
UNKNOWN_TYPE = "UNKNOWN"
# 展开时不需要加括号的节点：字段、常量、函数调用、窗口、已有括号
ATOMIC_NODES = (exp.Column, exp.Literal, exp.Null, exp.Boolean, exp.Func, exp.Paren, exp.Window,
                exp.Subquery, exp.Dot, exp.Var, exp.Parameter, exp.Placeholder, exp.Star, exp.Identifier)
TIME_FUNCTION_NODES = (exp.CurrentDate, exp.CurrentDatetime, exp.CurrentTime, exp.CurrentTimestamp)
# 输入全为空时结果也为空的节点：类型转换、COALESCE、NULLIF、四则运算、拼接、忽略空值的汇总
NULL_PRESERVING_NODES = (exp.Column, exp.Identifier, exp.Null, exp.Cast, exp.Paren, exp.Alias, exp.Trim,
                         exp.Coalesce, exp.Nullif, exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Neg, exp.DPipe,
                         exp.Sum, exp.Avg, exp.Min, exp.Max, exp.DataType, exp.DataTypeParam)


class SqlScopeError(Exception):
    """SQL 无法按作用域解析，例如引用了 CTE 里不存在的列、同一个别名用了两次"""


class Catalog(Protocol):
    """元数据目录：按表标识取表和字段。MetadataStore 天然满足"""

    def table(self, table_id: str) -> TableMeta | None:
        """按表标识取一张表，不存在时返回 None"""
        ...

    def columns(self, table_id: str) -> list[ColumnMeta]:
        """返回一张表的字段，表不存在时返回空列表"""
        ...


@dataclass(frozen=True)
class Resolution:
    """一个输出字段或表达式的追溯结果"""

    kind: str                                   # 取值见 metadata.models.COLUMN_KINDS
    sources: frozenset[FieldRef] = frozenset()  # 追溯到的实体表字段；COUNT(*) 这类行计数的加工为空


@dataclass
class ScopeJoin:
    """一个查询层里的一处关联"""

    join_type: str               # LEFT、INNER、RIGHT、FULL、JOIN；写在 WHERE 里的等值为 IMPLICIT
    alias: str                   # 被关联的来源在这一层的别名
    condition: exp.Expression    # 关联条件，可以交给 ScopeResolver.render 展开成实体表写法
    keys: list[JoinKey]          # 两侧都能追溯到实体表字段的等值键对


_UNRESOLVED = Resolution("unresolved")
_NULL = Resolution("null")
_CONSTANT = Resolution("constant")
_ETL_TIME = Resolution("etl_time")
_MEASURE = Resolution("derived")          # 不引用实体字段的加工，例如 COUNT(*)、ROW_NUMBER() OVER ()


# ── 解析与小工具 ──────────────────────────────────────────

@contextmanager
def quiet_sqlglot() -> Iterator[None]:
    """在上下文内把 sqlglot 日志提到 ERROR 级，压掉解析时的警告，出来后恢复原级别"""
    logger = logging.getLogger("sqlglot")
    previous = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(previous)


def parse_statements(sql: str) -> list[exp.Expression]:
    """按 PostgreSQL 方言把一段 SQL 解析成语句列表

    Args:
        sql: 一条或多条用分号隔开的 SQL

    Returns:
        语句的语法树，按原文顺序；空语句和只剩注释的分号节点已去掉

    Raises:
        sqlglot.errors.ParseError: 语法错误，原样抛出
    """
    with quiet_sqlglot():
        statements = sqlglot.parse(sql, read=DIALECT)
    return [s for s in statements if s is not None and not isinstance(s, exp.Semicolon)]


def _ident(node: exp.Expression | str | None) -> str:
    """标识符的规范写法：不带引号的转小写，带引号的保持原样，与 PostgreSQL 一致"""
    if node is None:
        return ""
    if isinstance(node, str):
        return node.lower()
    if isinstance(node, exp.Identifier):
        return node.this if node.quoted else node.this.lower()
    return node.name.lower()


def table_id(t: exp.Table) -> str:
    """表节点的标识：模式.表；没写模式时只有表名

    不带引号的名字转成小写，可以直接拿去元数据目录里查。
    """
    name = _ident(t.this)
    schema = t.args.get("db")
    return f"{_ident(schema)}.{name}" if schema else name


def _cte_names(node: exp.Expression) -> set[str]:
    """一个节点自己的 WITH 子句定义的 CTE 名"""
    with_ = node.args.get("with")
    if not isinstance(with_, exp.With):
        return set()
    return {_ident(cte.args["alias"].this) for cte in with_.expressions if cte.args.get("alias")}


def _is_cte_reference(table: exp.Table) -> bool:
    """不带模式的表名是否引用了外层某个 WITH 定义的 CTE"""
    name = _ident(table.this)
    node = table.parent
    while node is not None:
        if name in _cte_names(node):
            return True
        node = node.parent
    return False


def is_table_function(source: object) -> bool:
    """来源节点是不是表函数，例如 generate_series(...) AS d(dt)

    表函数也是 exp.Table 节点，但它的 this 不是标识符。它的字段由函数产生，不属于任何实体表，
    不能当实体表查元数据、追溯来源或展开。
    """
    return isinstance(source, exp.Table) and not isinstance(source.this, exp.Identifier)


def physical_tables(expr: exp.Expression) -> list[exp.Table]:
    """语法树里引用的实体表节点，不含 CTE 引用和表函数

    包括子查询、CTE 定义里的表。传入整条 INSERT 时也包括写入的目标表，只要来源表就传它的查询部分。

    Args:
        expr: 语法树

    Returns:
        表节点，按语法树深度优先的顺序；同一张表出现几次就有几个节点
    """
    tables: list[exp.Table] = []
    for table in expr.find_all(exp.Table, bfs=False):
        if is_table_function(table):
            continue                          # 表函数，例如 generate_series(...)
        if not table.args.get("db") and _is_cte_reference(table):
            continue
        tables.append(table)
    return tables


def conjuncts(cond: exp.Expression | None) -> list[exp.Expression]:
    """把条件按 AND 拆成子句，只拆最外层的 AND（括号包着的 AND 也拆），不进 OR、NOT、CASE

    Args:
        cond: 条件表达式；也可以直接传 WHERE、HAVING 节点；None 表示没有条件

    Returns:
        子句列表，按原文顺序
    """
    if cond is None:
        return []
    if isinstance(cond, (exp.Where, exp.Having)):
        cond = cond.this
    parts: list[exp.Expression] = []
    stack = [cond]
    while stack:
        node = stack.pop()
        if isinstance(node, exp.Paren) and isinstance(node.unnest(), exp.And):
            node = node.unnest()
        if isinstance(node, exp.And):
            stack.append(node.expression)
            stack.append(node.this)
            continue
        parts.append(node)
    return parts


def _is_temporal(data_type: exp.DataType | None) -> bool:
    """是否日期、时间类型"""
    return data_type is not None and data_type.is_type(*exp.DataType.TEMPORAL_TYPES)


def unwrap(expr: exp.Expression) -> exp.Expression:
    """剥掉不改变取值的外壳：别名、括号、转成非日期时间类型的 CAST、TRIM

    剥完是字段，就说明这个表达式是对该字段的直取。转成日期、时间类型的 CAST 不剥。
    """
    node = expr
    while True:
        if isinstance(node, (exp.Alias, exp.Paren, exp.Trim)):
            node = node.this
        elif isinstance(node, exp.Cast) and not _is_temporal(node.to):      # TryCast 是 Cast 的子类
            node = node.this
        else:
            return node


def _leaf_selects(query: exp.Expression) -> list[exp.Select]:
    """查询里穿过 UNION 和括号之后的每个叶子 SELECT，从左到右"""
    leaves: list[exp.Select] = []
    stack = [query]
    while stack:
        node = stack.pop()
        if isinstance(node, exp.Subquery):
            stack.append(node.this)
        elif isinstance(node, exp.SetOperation):
            stack.append(node.expression)
            stack.append(node.this)
        elif isinstance(node, exp.Select):
            leaves.append(node)
        else:
            raise SqlScopeError(f"不支持的查询结构: {type(node).__name__}")
    return leaves


def rename_outputs(query: exp.Expression, names: Sequence[str]) -> exp.Expression:
    """按位置把查询的输出列改名，用于把 INSERT 的投影对应到目标字段

    穿过 UNION 和括号，把每个叶子 SELECT 的第 i 个投影改成「表达式 AS names[i]」。

    Args:
        query: INSERT 的查询部分，或任意 SELECT / UNION
        names: 目标字段名，按位置对应

    Returns:
        改名后的副本，原树不变

    Raises:
        SqlScopeError: 投影里有 *，或者某个分支的列数与目标字段数不一致
    """
    result = query.copy()
    for select in _leaf_selects(result):
        projections = select.expressions
        if any(projection.is_star for projection in projections):
            raise SqlScopeError("投影里有 *，无法按位置对应目标字段")
        if len(projections) != len(names):
            raise SqlScopeError(f"查询有 {len(projections)} 列，目标字段有 {len(names)} 个，对不上")
        select.set("expressions", [exp.alias_(projection.unalias(), name, copy=False)
                                   for projection, name in zip(projections, names)])
    return result


# ── 按元数据限定 ──────────────────────────────────────────

def _from_order(scope: Scope) -> list[str]:
    """FROM 和各个 JOIN 里来源的别名，按书写顺序"""
    select = scope.expression
    if not isinstance(select, exp.Select):
        return []
    items: list[exp.Expression] = []
    from_ = select.args.get("from")
    if from_ is not None:
        items.append(from_.this)
    items.extend(join.this for join in select.args.get("joins") or [])
    return [item.alias_or_name for item in items]


def _own_sources(scope: Scope) -> dict[str, exp.Table | Scope]:
    """一个查询层自己 FROM、JOIN 里的来源：别名 → 实体表节点或子作用域，按书写顺序"""
    try:
        selected = {name: source for name, (_, source) in scope.selected_sources.items()}
    except OptimizeError:                     # 同一个别名用了两次：取先出现的
        selected = {}
        for name, _ in scope.references:
            if name not in selected and name in scope.sources:
                selected[name] = scope.sources[name]
    ordered = {name: selected[name] for name in _from_order(scope) if name in selected}
    for name, source in selected.items():
        ordered.setdefault(name, source)
    return ordered


def _referenced_tables(tree: exp.Expression, catalog: Catalog) -> tuple[dict[str, list[str]], set[FieldRef]]:
    """找出语句引用的已知实体表，以及「别名.字段」写法引用了、字段清单里却没有的字段

    Returns:
        (表标识 → 字段清单里的字段名, 字段清单缺口)
    """
    known: dict[str, list[str]] = {}
    known_sets: dict[str, set[str]] = {}
    unknown: set[str] = set()
    gaps: set[FieldRef] = set()
    for scope in traverse_scope(tree):
        aliases: dict[str, str] = {}
        for alias, source in _own_sources(scope).items():
            if not isinstance(source, exp.Table) or is_table_function(source):
                continue
            tid = table_id(source)
            if tid in unknown:
                continue
            if tid not in known:
                if catalog.table(tid) is None:
                    unknown.add(tid)
                    continue
                known[tid] = [column.column_name for column in catalog.columns(tid)]
                known_sets[tid] = set(known[tid])
            aliases[alias] = tid
        # scope.columns 也包含相关子查询里引用本层别名的字段
        for column in scope.columns:
            tid = aliases.get(column.table)
            if tid is not None and column.name not in known_sets[tid]:
                gaps.add(FieldRef(table=tid, column=column.name))
    return known, gaps


def _small_schema(known: dict[str, list[str]], gaps: set[FieldRef]) -> MappingSchema:
    """只含这条语句用到的表的结构，字段清单缺口也补进去"""
    extra: dict[str, list[str]] = {}
    for gap in sorted(gaps, key=_ref_key):
        extra.setdefault(gap.table, []).append(gap.column)
    mapping: dict[str, dict[str, dict[str, str]]] = {}
    for tid, columns in known.items():
        names = columns + extra.get(tid, [])
        schema_name, _, table_name = tid.partition(".")
        if names and table_name:
            mapping.setdefault(schema_name, {})[table_name] = dict.fromkeys(names, UNKNOWN_TYPE)
    return MappingSchema(mapping, dialect=DIALECT, normalize=False)


def qualify_with_catalog(expr: exp.Expression, catalog: Catalog) -> tuple[exp.Expression, set[FieldRef]]:
    """按元数据给语句里的表加别名、给每个字段加上所属的别名

    做法：先在没限定的树上按查询层找出「别名 → 已知实体表」；建一个只含这些表的小结构，
    把「别名.字段」写法引用了、字段清单里却没有的字段也补进去，记为字段清单缺口；
    然后调 sqlglot 的 qualify。仍然报错就去掉结构重试一次。

    Args:
        expr: 一条查询（SELECT、UNION）；DML 也可以，按 sqlglot 的规则处理
        catalog: 元数据目录

    Returns:
        (限定后的语法树副本, 字段清单缺口)。标识符已规范成小写，原树不变

    Raises:
        SqlScopeError: 去掉结构之后仍然无法限定，例如引用了 CTE 里不存在的列
    """
    with quiet_sqlglot():
        try:
            tree = normalize_identifiers(expr.copy(), dialect=DIALECT)
            known, gaps = _referenced_tables(tree, catalog)
            schema = _small_schema(known, gaps)
        except Exception as error:
            raise SqlScopeError(f"SQL 无法按作用域解析: {error}") from error
        try:
            return qualify(tree.copy(), dialect=DIALECT, schema=schema, **QUALIFY_OPTIONS), gaps
        except Exception:
            pass                              # 带结构失败，下面去掉结构再试一次
        try:
            return qualify(tree, dialect=DIALECT, **QUALIFY_OPTIONS), gaps
        except Exception as error:
            raise SqlScopeError(f"SQL 无法按作用域解析: {error}") from error


# ── 追溯、展开、关联键 ────────────────────────────────────

def _ref_key(ref: FieldRef) -> tuple[str, str]:
    """字段引用的排序键"""
    return ref.table, ref.column


def _leaf_scopes(scope: Scope) -> list[Scope]:
    """UNION 作用域的各个叶子分支，从左到右；不是 UNION 时就是它自己"""
    leaves: list[Scope] = []
    stack = [scope]
    while stack:
        current = stack.pop()
        if current.union_scopes:
            stack.extend(reversed(current.union_scopes))
        else:
            leaves.append(current)
    return leaves


def _select_at(select: exp.Expression, index: int) -> exp.Expression | None:
    """SELECT 的第 index 个投影；位置之前有 * 或者越界时返回 None"""
    if not isinstance(select, exp.Select):
        return None
    projections = select.selects
    if index >= len(projections) or any(p.is_star for p in projections[: index + 1]):
        return None
    return projections[index]


def _local_nodes(root: exp.Expression) -> Iterator[exp.Expression]:
    """深度优先遍历 root 里属于当前查询层的节点；嵌套查询只返回它本身，不往里走"""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        if node is not root and isinstance(node, exp.Query):
            continue
        stack.extend(reversed(list(node.iter_expressions())))


def _has_time_function(node: exp.Expression) -> bool:
    """表达式里是否有取加工时间的函数或伪列"""
    for item in node.walk():
        if isinstance(item, TIME_FUNCTION_NODES):
            return True
        if isinstance(item, exp.Anonymous) and item.name.lower() in ETL_TIME_FUNCTIONS:
            return True
        if isinstance(item, exp.Column) and not item.table and item.name.lower() in TIME_PSEUDO_COLUMNS:
            return True
    return False


def _stays_null(expr: exp.Expression) -> bool:
    """其中的字段全为空时，表达式是否也恒为空

    只由空值、字段、类型转换、COALESCE、NULLIF、四则运算、拼接、SUM/AVG/MIN/MAX 组成时才算。
    带了常量（类型参数里的数字除外）、COUNT、IS NULL 之类的就不算。
    """
    for node in expr.walk():
        if isinstance(node, exp.Literal) and node.find_ancestor(exp.DataType) is not None:
            continue                          # VARCHAR(1)、DECIMAL(18, 2) 里的类型参数
        if not isinstance(node, NULL_PRESERVING_NODES):
            return False
    return True


def _has_aggregate(node: exp.Expression) -> bool:
    """表达式里（含标量子查询里）是否有聚合函数或窗口函数，例如 COUNT(*)、SUM(1)、ROW_NUMBER() OVER ()"""
    return any(isinstance(item, (exp.AggFunc, exp.Window)) for item in node.walk())


def _without_sources(expr: exp.Expression, kinds: set[str]) -> Resolution:
    """追溯不到任何实体字段的表达式：恒为空、加工时间、行计数这类加工、或者常量

    依次判断：字段全为空且只经过保持空值的运算 → 空值；有取加工时间的函数或字段 → 加工时间；
    引用的字段本身是加工，或者有聚合、窗口函数（由行计数得到的度量）→ 加工，来源为空；其余 → 常量。

    Args:
        expr: 剥壳后的表达式
        kinds: 其中各字段的追溯类别；不引用字段时为空集合
    """
    if kinds <= {"null"} and _stays_null(expr):
        return _NULL
    if "etl_time" in kinds or _has_time_function(expr):
        return _ETL_TIME
    if "derived" in kinds or _has_aggregate(expr):
        return _MEASURE
    return _CONSTANT


def _merge_union(results: list[Resolution]) -> Resolution:
    """合并 UNION 各分支同一位置的追溯结果"""
    kinds = {result.kind for result in results}
    sources = frozenset().union(*(result.sources for result in results))
    if kinds == {"passthrough"}:
        return Resolution("passthrough", sources)
    if kinds == {"null"}:
        return _NULL
    if kinds & {"passthrough", "derived"}:
        return Resolution("derived", sources)
    if "unresolved" in kinds:
        return _UNRESOLVED
    return _ETL_TIME if "etl_time" in kinds else _CONSTANT


def _pseudo(name: str) -> Resolution:
    """伪列的追溯结果"""
    return _ETL_TIME if name.lower() in TIME_PSEUDO_COLUMNS else _CONSTANT


def _equality_columns(condition: exp.Expression) -> tuple[exp.Column, exp.Column] | None:
    """「字段 = 字段」形式的等值条件，剥壳后两侧都是字段时返回这两个字段"""
    condition = condition.unnest()            # 整个条件外面的括号
    if not isinstance(condition, exp.EQ):
        return None
    left, right = unwrap(condition.this), unwrap(condition.expression)
    if (isinstance(left, exp.Column) and isinstance(right, exp.Column)
            and not left.is_star and not right.is_star):
        return left, right
    return None


def _join_type(join: exp.Join) -> str:
    """关联类型：LEFT、RIGHT、FULL、INNER、CROSS，没写类型的为 JOIN"""
    side, kind = join.side.upper(), join.kind.upper()
    if side:
        return side
    if kind and kind != "OUTER":
        return kind
    return "JOIN"


def _entity_column(table: exp.Table, column: exp.Column) -> exp.Column:
    """把字段写成「模式.表.字段」"""
    schema = table.args.get("db")
    return exp.Column(this=column.this.copy(), table=table.this.copy(),
                      db=schema.copy() if schema is not None else None)


def _parenthesize(node: exp.Expression) -> exp.Expression:
    """展开结果嵌进外层表达式时，不是原子节点就加括号，保证运算顺序不变"""
    return node if isinstance(node, ATOMIC_NODES) else exp.Paren(this=node)


class ScopeResolver:
    """在一棵作用域树上做别名查找、字段追溯、表达式展开和关联键提取

    作用域树一般来自 build_scope(qualify_with_catalog(...)[0])。同一个实例按（查询层, 输出名）
    记忆化，适合对一条语句的全部目标字段逐个追溯。
    """

    def __init__(self, root: Scope, max_depth: int = 12, max_chars: int = 1500, *,
                 catalog: Catalog | None = None) -> None:
        """
        Args:
            root: 根作用域
            max_depth: 追溯时最多穿过几层查询，超过的记为追溯不到
            max_chars: 展开文本的最大长度，超出部分截断并以「…」结尾
            catalog: 元数据目录，可选。给了才能知道实体表有哪些字段，
                     用于没带别名的列按来源搜索、owners、外部表之外的 * 展开

        Raises:
            SqlScopeError: root 为 None（语句里没有查询）
        """
        if root is None:
            raise SqlScopeError("没有可以解析的查询")
        self.root = root
        self.max_depth = max_depth
        self.max_chars = max_chars
        self.render_depth = min(RENDER_MAX_DEPTH, max_depth)
        self.catalog = catalog
        # 查询节点 → 它的作用域，用来从表达式里的标量子查询找到对应的作用域
        self._scopes: dict[int, Scope] = {id(scope.expression): scope for scope in root.traverse()}
        self._sources: dict[int, dict[str, exp.Table | Scope]] = {}
        self._table_columns: dict[str, list[str] | None] = {}
        self._memo: dict[tuple[int, str], Resolution] = {}
        self._active: set[tuple[int, str]] = set()
        self._cuts = 0                      # 撞到深度上限或环的次数；中途撞到过的结果不缓存
        self._render_memo: dict[tuple[int, str, int], exp.Expression | None] = {}

    # ── 别名与来源 ──

    def _own(self, scope: Scope) -> dict[str, exp.Table | Scope]:
        """本层来源，带缓存"""
        key = id(scope)
        if key not in self._sources:
            self._sources[key] = _own_sources(scope)
        return self._sources[key]

    def sources(self, scope: Scope) -> dict[str, exp.Table | Scope]:
        """本层 FROM、JOIN 里的来源：别名 → 实体表节点或子作用域，按书写顺序

        不带别名的列要在「所有来源」里搜索时，用它列出候选来源。
        """
        return dict(self._own(scope))

    def source_of(self, scope: Scope, alias: str) -> exp.Table | Scope | None:
        """别名指向的来源：实体表节点，或者 CTE、派生表、UNION 的作用域

        本层找不到时，只要本层可能是相关子查询，就沿 parent 往外层找。

        Returns:
            来源；别名在本层和可见的外层都没有定义时返回 None
        """
        current: Scope | None = scope
        while current is not None:
            source = self._own(current).get(alias)
            if source is not None:
                return source
            if not current.can_be_correlated:
                return None
            current = current.parent
        return None

    def output_names(self, scope: Scope) -> list[str] | None:
        """一个查询层的输出列名，按位置

        UNION 取最左分支的列名。投影里有没展开的 *（例如来自外部表）时不知道有哪些列，返回 None。
        """
        if scope.outer_columns:
            return list(scope.outer_columns)
        select = _leaf_scopes(scope)[0].expression
        if not isinstance(select, exp.Select):
            return None
        if any(projection.is_star for projection in select.selects):
            return None
        return [projection.output_name for projection in select.selects]

    def _columns_of(self, source: exp.Table | Scope) -> list[str] | None:
        """一个来源有哪些字段；不知道时返回 None"""
        if isinstance(source, Scope):
            return self.output_names(source)
        tid = table_id(source)
        if tid not in self._table_columns:
            columns = None
            if self.catalog is not None and self.catalog.table(tid) is not None:
                columns = [column.column_name for column in self.catalog.columns(tid)]
            self._table_columns[tid] = columns
        return self._table_columns[tid]

    def owners(self, scope: Scope, name: str) -> list[str]:
        """本层来源里有字段 name 的那些别名，按书写顺序

        不知道有哪些字段的来源（目录里没有的表、有未展开 * 的查询）不计入。
        恰好一个就是这个字段的来源；零个说明不存在；两个以上说明有歧义。
        """
        return [alias for alias, source in self._own(scope).items()
                if name in (self._columns_of(source) or ())]

    def _star_source(self, scope: Scope, name: str) -> exp.Table | Scope | None:
        """投影里没有 name、但有 * 时，找出 name 来自哪个来源；表函数产生的字段不属于任何来源"""
        own = self._own(scope)
        known: list[exp.Table | Scope] = []
        unknown: list[exp.Table | Scope] = []
        for projection in scope.expression.selects:
            if not projection.is_star:
                continue
            aliases = [projection.table] if isinstance(projection, exp.Column) and projection.table else list(own)
            for alias in aliases:
                source = own.get(alias)
                if source is None or is_table_function(source):
                    continue
                columns = self._columns_of(source)
                if columns is None:
                    unknown.append(source)
                elif name in columns:
                    known.append(source)
        if len(known) == 1:
            return known[0]
        if not known and len(unknown) == 1:
            return unknown[0]
        return None

    def _locate(self, scope: Scope, column: exp.Column) -> exp.Table | Scope | Resolution | None:
        """字段指向的来源；伪列直接给出追溯结果；找不到或有歧义时返回 None"""
        if column.table:
            return self.source_of(scope, column.table)
        name = column.name
        current: Scope | None = scope
        while current is not None:
            owners = self.owners(current, name)
            if len(owners) == 1:
                return self._own(current)[owners[0]]
            if owners:
                return None                   # 有歧义
            current = current.parent if current.can_be_correlated else None
        if name.lower() in PSEUDO_COLUMNS:
            return _pseudo(name)
        own = self._own(scope)
        if len(own) == 1:
            (source,) = own.values()
            if self._columns_of(source) is None:
                return source                 # 只有一个来源且不知道它的字段，就算它的
        return None

    # ── 追溯 ──

    def resolve_column(self, scope: Scope, col: exp.Column) -> Resolution:
        """追溯一个字段：指向实体表是直取；指向 CTE、派生表就到那一层继续追溯；找不到为 unresolved"""
        return self._column(scope, col, 0)

    def resolve_output(self, scope: Scope, name: str) -> Resolution:
        """追溯一个查询层的输出字段

        剥掉别名、括号、非日期时间类型的 CAST、TRIM 之后：是字段就按字段追溯；否则收集表达式里
        （含标量子查询里）的全部字段，有来源为加工；追溯不到实体来源时，按恒为空、加工时间、
        行计数这类加工（COUNT(*)、窗口函数，来源为空）、常量区分。UNION 按位置合并各分支的结果。
        """
        return self._output(scope, name, 0)

    def _column(self, scope: Scope, column: exp.Column, depth: int) -> Resolution:
        """追溯一个字段，depth 是已经穿过的查询层数"""
        located = self._locate(scope, column)
        if isinstance(located, Resolution):
            return located
        if is_table_function(located):
            return _UNRESOLVED                # 函数产生的值，不属于任何实体表字段
        if isinstance(located, exp.Table):
            return Resolution("passthrough", frozenset({FieldRef(table=table_id(located),
                                                                  column=_ident(column.this))}))
        if isinstance(located, Scope):
            return self._output(located, column.name, depth + 1)
        return _UNRESOLVED

    def _output(self, scope: Scope, name: str, depth: int) -> Resolution:
        """追溯一个输出字段：按（查询层, 输出名）记忆化，超过深度上限或遇到环时记为追溯不到"""
        key = (id(scope), name)
        if key in self._memo:
            return self._memo[key]
        if depth > self.max_depth or key in self._active:
            self._cuts += 1
            return _UNRESOLVED
        self._active.add(key)
        cuts = self._cuts
        try:
            result = self._compute_output(scope, name, depth)
        finally:
            self._active.discard(key)
        if self._cuts == cuts:                # 中途没有撞到上限，结果与深度无关，可以缓存
            self._memo[key] = result
        return result

    def _compute_output(self, scope: Scope, name: str, depth: int) -> Resolution:
        """按查询层的类型追溯：UNION 按位置合并各分支；SELECT 找投影，找不到再经 * 找来源；VALUES 是常量"""
        expression = scope.expression
        if isinstance(expression, exp.SetOperation):
            names = self.output_names(scope)
            if names is None or name not in names:
                return _UNRESOLVED
            index = names.index(name)
            results = []
            for leaf in _leaf_scopes(scope):
                projection = _select_at(leaf.expression, index)
                results.append(_UNRESOLVED if projection is None
                               else self._expression(leaf, projection, depth))
            return _merge_union(results)
        if isinstance(expression, exp.Select):
            projection = self._projection(scope, name)
            if projection is not None:
                return self._expression(scope, projection, depth)
            source = self._star_source(scope, name)
            if isinstance(source, exp.Table):
                return Resolution("passthrough", frozenset({FieldRef(table=table_id(source), column=name)}))
            if isinstance(source, Scope):
                return self._output(source, name, depth + 1)
            return _UNRESOLVED
        if isinstance(expression, exp.Values):
            names = self.output_names(scope) or []
            return _CONSTANT if name in names else _UNRESOLVED
        return _UNRESOLVED

    def _projection(self, scope: Scope, name: str) -> exp.Expression | None:
        """查询层里输出名为 name 的投影"""
        if scope.outer_columns:
            if name not in scope.outer_columns:
                return None
            return _select_at(scope.expression, scope.outer_columns.index(name))
        for projection in scope.expression.selects:
            if not projection.is_star and projection.output_name == name:
                return projection
        return None

    def _expression(self, scope: Scope, expr: exp.Expression, depth: int) -> Resolution:
        """判定一个投影表达式的类别：剥壳后是字段就追溯它，否则看其中各字段的追溯结果"""
        core = unwrap(expr)
        if isinstance(core, exp.Column) and not core.is_star:
            return self._column(scope, core, depth)
        results = [self._column(owner, column, depth) for owner, column in self._value_columns(scope, core)]
        sources = frozenset().union(*(result.sources for result in results))
        if sources:
            return Resolution("derived", sources)
        kinds = {result.kind for result in results}
        if "unresolved" in kinds:
            return _UNRESOLVED
        return _without_sources(core, kinds)

    def _value_columns(self, scope: Scope, expr: exp.Expression) -> list[tuple[Scope, exp.Column]]:
        """表达式里本层的字段，以及其中标量子查询各自那一层的字段"""
        references: list[tuple[Scope, exp.Column]] = []
        for node in _local_nodes(expr):
            if isinstance(node, exp.Column):
                if not node.is_star:
                    references.append((scope, node))
            elif isinstance(node, exp.Query) and node is not expr:
                inner = self._scopes.get(id(node.unnest()))
                if inner is None:
                    continue
                for leaf in _leaf_scopes(inner):
                    references.extend((leaf, column) for column in leaf.columns if not column.is_star)
        return references

    def trace_scopes(self, scope: Scope, name: str) -> list[Scope]:
        """追溯一个查询层的输出字段时经过的查询层，按第一次经过的先后，每层只列一次

        包括：这一层自己、UNION 的各个分支、字段穿过的 CTE 和派生表、投影里标量子查询的各层。
        字段指向实体表就到此为止。只在 FROM、JOIN 里出现、却没有被追溯到的来源不算；
        WHERE 里的子查询也不算，它的条件已经写在所在查询层的过滤条件里。
        深度上限和遇到环的处理与 resolve_output 一致。

        Args:
            scope: 查询层，通常是根作用域
            name: 输出字段名

        Returns:
            经过的查询层，第一个是 scope 自己
        """
        visited: dict[int, Scope] = {}
        self._trace_output(scope, name, 0, visited, set())
        return list(visited.values())

    def _trace_output(self, scope: Scope, name: str, depth: int, visited: dict[int, Scope],
                      seen: set[tuple[int, str]]) -> None:
        """沿 _compute_output 走过的路，记下经过的查询层；seen 防止同一个输出重复走和成环"""
        key = (id(scope), name)
        if key in seen or depth > self.max_depth:
            return
        seen.add(key)
        visited.setdefault(id(scope), scope)
        expression = scope.expression
        if isinstance(expression, exp.SetOperation):
            names = self.output_names(scope)
            if names is None or name not in names:
                return
            index = names.index(name)
            for leaf in _leaf_scopes(scope):
                visited.setdefault(id(leaf), leaf)
                projection = _select_at(leaf.expression, index)
                if projection is not None:
                    self._trace_expression(leaf, projection, depth, visited, seen)
        elif isinstance(expression, exp.Select):
            projection = self._projection(scope, name)
            if projection is not None:
                self._trace_expression(scope, projection, depth, visited, seen)
                return
            source = self._star_source(scope, name)
            if isinstance(source, Scope):
                self._trace_output(source, name, depth + 1, visited, seen)

    def _trace_expression(self, scope: Scope, expr: exp.Expression, depth: int, visited: dict[int, Scope],
                          seen: set[tuple[int, str]]) -> None:
        """沿 _expression 走过的路：剥壳后是字段就追溯它，否则追溯其中各字段（含标量子查询里的）"""
        core = unwrap(expr)
        if isinstance(core, exp.Column) and not core.is_star:
            self._trace_column(scope, core, depth, visited, seen)
            return
        for owner, column in self._value_columns(scope, core):
            visited.setdefault(id(owner), owner)
            self._trace_column(owner, column, depth, visited, seen)

    def _trace_column(self, scope: Scope, column: exp.Column, depth: int, visited: dict[int, Scope],
                      seen: set[tuple[int, str]]) -> None:
        """字段指向 CTE、派生表就到那一层继续；指向实体表、伪列或者找不到，到此为止"""
        located = self._locate(scope, column)
        if isinstance(located, Scope):
            self._trace_output(located, column.name, depth + 1, visited, seen)

    # ── 展开表达式文本 ──

    def render(self, scope: Scope, expr: exp.Expression) -> str:
        """把表达式里的字段换成实体表写法，CTE、派生表的字段换成它们展开后的表达式

        指向实体表的字段写成「模式.表.字段」；指向单分支查询的换成那个投影的展开结果，不是原子节点时加括号；
        指向 UNION 的写成 UNION(分支1, 分支2, ...)。嵌套子查询里的字段不动。
        最多展开 8 层，超过 max_chars 的部分截断并以「…」结尾。过滤条件、关联条件也可以用它展开。
        """
        node = expr.unalias() if isinstance(expr, exp.Alias) else expr
        return self._finish(self._expand(scope, node, 0))

    def render_output(self, scope: Scope, name: str) -> str:
        """展开一个查询层的输出字段，UNION 写成 UNION(分支1, 分支2, ...)；没有这个输出字段时返回空串"""
        node = self._expand_output(scope, name, 0)
        return "" if node is None else self._finish(node)

    def _finish(self, node: exp.Expression) -> str:
        with quiet_sqlglot():                 # 方言不支持的写法生成时会打警告
            text = node.sql(dialect=DIALECT)
        if len(text) > self.max_chars:
            text = text[: max(self.max_chars - 1, 0)] + ELLIPSIS
        return text

    def _expand(self, scope: Scope, expr: exp.Expression, depth: int) -> exp.Expression:
        """返回 expr 的副本，其中本层的字段都已展开"""
        tree = expr.copy()
        if isinstance(tree, exp.Query):
            return tree                       # 嵌套子查询原样保留
        if isinstance(tree, exp.Column):
            return tree if tree.is_star else self._expand_column(scope, tree, depth)
        targets = [node for node in _local_nodes(tree) if isinstance(node, exp.Column) and not node.is_star]
        for column in targets:
            column.replace(self._expand_column(scope, column, depth))
        return tree

    def _expand_column(self, scope: Scope, column: exp.Column, depth: int) -> exp.Expression:
        """一个字段的展开结果：实体表字段写全名，查询层的输出换成展开后的表达式，到了深度上限原样保留"""
        located = self._locate(scope, column)
        if isinstance(located, exp.Table):
            return column if is_table_function(located) else _entity_column(located, column)
        if isinstance(located, Scope) and depth < self.render_depth:
            expanded = self._expand_output(located, column.name, depth + 1)
            if expanded is not None:
                return _parenthesize(expanded)
        return column

    def _expand_output(self, scope: Scope, name: str, depth: int) -> exp.Expression | None:
        """展开一个查询层的输出字段，不加外层括号；展不开时返回 None"""
        key = (id(scope), name, depth)
        if key not in self._render_memo:
            self._render_memo[key] = self._compute_expand(scope, name, depth)
        cached = self._render_memo[key]
        return None if cached is None else cached.copy()

    def _compute_expand(self, scope: Scope, name: str, depth: int) -> exp.Expression | None:
        """按查询层的类型展开：UNION 写成 UNION(...)；SELECT 展开投影，找不到再经 * 找来源"""
        expression = scope.expression
        if isinstance(expression, exp.SetOperation):
            names = self.output_names(scope)
            if names is None or name not in names:
                return None
            index = names.index(name)
            branches = []
            for leaf in _leaf_scopes(scope):
                projection = _select_at(leaf.expression, index)
                if projection is not None:
                    branches.append(self._expand(leaf, projection.unalias(), depth))
            return exp.Anonymous(this="UNION", expressions=branches) if branches else None
        if not isinstance(expression, exp.Select):
            return None
        projection = self._projection(scope, name)
        if projection is not None:
            return self._expand(scope, projection.unalias(), depth)
        source = self._star_source(scope, name)
        if isinstance(source, exp.Table):
            return _entity_column(source, exp.column(name))
        if isinstance(source, Scope) and depth < self.render_depth:
            return self._expand_output(source, name, depth + 1)
        return None

    # ── 等值关联键 ──

    def joins(self, scope: Scope) -> list[ScopeJoin]:
        """一个查询层里的关联：各个 JOIN 的 ON、USING，以及 WHERE 里两侧别名不同的等值

        ON 只按 AND 拆子句；剥壳后两侧都是字段的等值，两侧都追溯成直取时取来源的笛卡尔积
        （最多 4 组），两侧是同一个实体字段的丢掉。被关联的别名放在键对右侧。
        WHERE 里的等值按别名对归组，记为 IMPLICIT。没有条件的关联（逗号、CROSS JOIN）不单独列出。
        """
        select = scope.expression
        if not isinstance(select, exp.Select):
            return []
        result: list[ScopeJoin] = []
        for join in select.args.get("joins") or []:
            alias = join.this.alias_or_name
            condition = join.args.get("on")
            if condition is None and join.args.get("using"):
                condition = self._using_condition(scope, join, alias)
            if condition is None:
                continue
            result.append(ScopeJoin(join_type=_join_type(join), alias=alias, condition=condition,
                                    keys=self._keys(scope, conjuncts(condition), alias)))
        result.extend(self._implicit_joins(scope, select))
        return result

    def _using_condition(self, scope: Scope, join: exp.Join, alias: str) -> exp.Expression | None:
        """把 USING 写成等值条件：左侧取前面来源里有这个字段的那个，不知道时取紧挨着的前一个"""
        order = _from_order(scope)
        earlier = order[: order.index(alias)] if alias in order else order[:-1]
        own = self._own(scope)
        conditions = []
        for identifier in join.args["using"]:
            name = identifier.name
            left = next((a for a in earlier if a in own and name in (self._columns_of(own[a]) or ())),
                        earlier[-1] if earlier else None)
            if left is not None:
                conditions.append(exp.column(name, table=left).eq(exp.column(name, table=alias)))
        return exp.and_(*conditions) if conditions else None

    def _implicit_joins(self, scope: Scope, select: exp.Select) -> list[ScopeJoin]:
        """WHERE 里两侧别名不同的等值，按别名对归组"""
        where = select.args.get("where")
        if where is None:
            return []
        own = self._own(scope)
        order = _from_order(scope)
        groups: dict[tuple[str, str], list[exp.Expression]] = {}
        for condition in conjuncts(where):
            pair = _equality_columns(condition)
            if pair is None:
                continue
            tables = (pair[0].table, pair[1].table)
            if not all(tables) or tables[0] == tables[1]:
                continue
            mine = [t for t in tables if t in own]
            if not mine:
                continue
            # 被关联的一侧：两侧都是本层来源时取 FROM 里靠后的，否则取本层的那个
            alias = max(mine, key=lambda t: order.index(t) if t in order else -1)
            other = tables[1] if alias == tables[0] else tables[0]
            if self.source_of(scope, other) is None:
                continue
            groups.setdefault((other, alias), []).append(condition)
        return [
            ScopeJoin(join_type="IMPLICIT", alias=alias,
                      condition=conditions[0] if len(conditions) == 1 else exp.and_(*conditions),
                      keys=self._keys(scope, conditions, alias))
            for (_, alias), conditions in groups.items()
        ]

    def _keys(self, scope: Scope, conditions: list[exp.Expression], alias: str) -> list[JoinKey]:
        """从等值子句里取出两侧都能追溯到实体表字段的键对"""
        keys: list[JoinKey] = []
        for condition in conditions:
            pair = _equality_columns(condition)
            if pair is None:
                continue
            left, right = pair
            if left.table and left.table == right.table:
                continue                      # 同一个别名的两个字段，是过滤不是关联
            if left.table == alias and right.table != alias:
                left, right = right, left
            left_result = self.resolve_column(scope, left)
            right_result = self.resolve_column(scope, right)
            if left_result.kind != "passthrough" or right_result.kind != "passthrough":
                continue
            pairs = [(a, b) for a in sorted(left_result.sources, key=_ref_key)
                     for b in sorted(right_result.sources, key=_ref_key) if a != b]
            for a, b in pairs[:MAX_KEY_PAIRS]:
                key = JoinKey(left=a, right=b)
                if key not in keys:
                    keys.append(key)
        return keys
