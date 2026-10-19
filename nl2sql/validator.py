"""
静态校验
不执行 SQL，用 sqlglot 解析后对照元数据检查。错误必须修正，警告展示给人判断：
    R1 按目标方言能解析，且只有一条语句          错误
    R2 只能是查询语句                            错误
    R3 每张表都存在于元数据                      错误
    R4 每个字段都存在于它所属的表                错误
    R5 快照表在所在查询层带具体的数据日期条件    错误（只取最早、最晚、去重日期的除外）
    R6 表之间的等值关联属于已知关联关系          警告
    R7 汇总查询里被关联的表按粒度字段全部约束    警告
    R8 实际使用的表和字段与查询方案一致          警告（用到了方案明确去掉的表是错误）
    R9 代码字段不和中文名称比较                  错误（能确定是代码字段时才报，见 metadata.codes）

做法：R1 解析；R2 看语法树的根节点和全部节点；R3 遍历实体表节点；R4 按元数据限定字段后逐个查询层检查每个字段；
R5–R7、R9 在限定后的作用域树上按查询层检查；R8 对比查询方案。别名指向、字段追溯、关联键都来自共享的 metadata.sqlscope。
报告同时给出 SQL 实际用到的实体表和字段（穿过 CTE、子查询追到实体表），评估时用来算选表和选字段的准确率。
"""
import difflib
import re
from collections.abc import Callable, Collection, Iterable, Iterator
from dataclasses import dataclass, field

from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import Scope, build_scope, walk_in_scope

from metadata.checks import ERROR, SNAPSHOT_TYPE, WARNING
from metadata.codes import code_tables, enum_segments, is_chinese_name, is_code_column, name_column, parse_enum
from metadata.models import ColumnMeta, FieldRef, JoinKey, Relation, TableMeta
from metadata.sqlscope import (
    DIALECT,
    PSEUDO_COLUMNS,
    ScopeResolver,
    conjuncts,
    is_table_function,
    parse_statements,
    physical_tables,
    qualify_with_catalog,
    table_id,
    unwrap,
)
from metadata.store import MetadataStore

MAX_SUGGESTIONS = 5               # 报错时附上最接近的几个表名或字段名
DATE_EXAMPLE = "DATE '…'"         # R5 示例写法里的日期占位

# 查询里不允许出现的节点：类名 → 说明。按名字取类，sqlglot 小版本改名时不至于导入失败
_FORBIDDEN_NAMES = {
    "Insert": "INSERT 插入", "Update": "UPDATE 更新", "Delete": "DELETE 删除", "Merge": "MERGE 合并",
    "Create": "CREATE 建表或建对象", "Drop": "DROP 删除对象", "Alter": "ALTER 修改对象",
    "TruncateTable": "TRUNCATE 清空表", "Copy": "COPY 导入导出", "Into": "SELECT … INTO 建表",
    "Transaction": "事务控制", "Commit": "事务控制（COMMIT）", "Rollback": "事务控制（ROLLBACK）",
    "Grant": "GRANT 授权", "Set": "SET 修改会话参数", "Use": "USE 切换库",
    "Lock": "FOR UPDATE / FOR SHARE 加锁", "Command": "命令",
}
FORBIDDEN_NODES: tuple[tuple[type, str], ...] = tuple(
    (getattr(exp, name), label) for name, label in _FORBIDDEN_NAMES.items() if hasattr(exp, name))
_FORBIDDEN_TYPES = tuple(node_type for node_type, _ in FORBIDDEN_NODES)
# 不能引用 SELECT 输出别名的子句：语法树里的参数名 → 写进问题里的称呼
_ALIAS_FREE_CLAUSES = {"where": "WHERE 子句", "having": "HAVING 子句", "joins": "JOIN 的关联条件"}
# 在没限定的树上还要查的位置：同层的别名在 SELECT 列表、窗口里互相看不到，在 ORDER BY、GROUP BY 里只能单独出现
_ALIAS_SITES = {**_ALIAS_FREE_CLAUSES, "expressions": "SELECT 列表", "windows": "WINDOW 子句",
                "order": "ORDER BY 子句", "group": "GROUP BY 子句"}
# ORDER BY、GROUP BY 里不改变「这个名字单独出现」的外壳节点：排序项、括号、分组集合
_STANDALONE_WRAPPERS = tuple(
    getattr(exp, name) for name in ("Ordered", "Paren", "Tuple", "Rollup", "Cube", "GroupingSets") if hasattr(exp, name))
# 挂在括号上、PostgreSQL 视同写在括号里面那条查询上的子句：语法树里的参数名 → 称呼
_WRAPPER_CLAUSES = {"with": "WITH", "order": "ORDER BY", "limit": "LIMIT", "offset": "OFFSET"}
# 包在聚合函数外面、不改变它是不是窗口函数的节点
_AGG_WRAPPERS = tuple(getattr(exp, name) for name in ("Filter", "IgnoreNulls", "RespectNulls") if hasattr(exp, name))

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# sqlglot 报错里的词法单元写得很长，只留它的原文
_TOKEN = re.compile(r"<Token token_type: <TokenType\.\w+: [^>]*>, text: (.*?), line: \d+, col: \d+, "
                    r"start: \d+, end: \d+, comments: \[[^\]]*\]>")
_TOKEN_POSITION = re.compile(r"(\d+):(\d+)\s*$")


@dataclass(frozen=True)
class ValidationIssue:
    """一条校验问题"""

    rule: str                    # R1 到 R9
    level: str                   # 错误 / 警告，取值同 metadata.checks
    message: str


@dataclass
class ValidationReport:
    """一条 SQL 的校验报告"""

    issues: list[ValidationIssue] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)     # 实际用到的实体表
    columns: list[str] = field(default_factory=list)    # 实际用到的字段，写成「模式.表.字段」

    @property
    def errors(self) -> list[ValidationIssue]:
        """错误级问题"""
        return [issue for issue in self.issues if issue.level == ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        """警告级问题"""
        return [issue for issue in self.issues if issue.level == WARNING]

    @property
    def ok(self) -> bool:
        """没有错误即为通过，警告不影响"""
        return not self.errors


# ── 一次校验的中间结果 ────────────────────────────────────

@dataclass
class _Run:
    """一次校验过程中收集的问题、用到的表和字段"""

    issues: list[ValidationIssue] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    used: set[FieldRef] = field(default_factory=set)

    def add(self, rule: str, level: str, message: str) -> None:
        """登记一条问题，同一条规则下文字相同的只留一条"""
        issue = ValidationIssue(rule, level, message)
        if issue not in self.issues:
            self.issues.append(issue)

    def error(self, rule: str, message: str) -> None:
        """登记一条错误"""
        self.add(rule, ERROR, message)

    def warn(self, rule: str, message: str) -> None:
        """登记一条警告"""
        self.add(rule, WARNING, message)

    def report(self) -> ValidationReport:
        """整理成报告：表和字段去重排序，保证结果确定"""
        return ValidationReport(
            issues=list(self.issues),
            tables=sorted(set(self.tables)),
            columns=sorted({f"{ref.table}.{ref.column}" for ref in self.used}),
        )


@dataclass
class _Mappings:
    """一对表之间的已知关联"""

    pairs: set[tuple[tuple[str, str], tuple[str, str]]] = field(default_factory=set)   # 规范化后的字段对
    relations: list[Relation] = field(default_factory=list)


class _Catalog:
    """用构造时建好的索引回答 sqlscope 的元数据查询，validate 时不再访问元数据库"""

    def __init__(self, tables: dict[str, TableMeta], columns: dict[str, list[ColumnMeta]]) -> None:
        """
        Args:
            tables: 表标识 → 表
            columns: 表标识 → 字段，按表内顺序
        """
        self._tables = tables
        self._columns = columns

    def table(self, table_id: str) -> TableMeta | None:
        """按表标识取一张表，不存在时返回 None"""
        return self._tables.get(table_id)

    def columns(self, table_id: str) -> list[ColumnMeta]:
        """返回一张表的字段，表不存在时返回空列表"""
        return self._columns.get(table_id, [])


# ── 小工具 ────────────────────────────────────────────────

def _clean(text: str) -> str:
    """去掉 ANSI 转义符，把 sqlglot 报错里冗长的词法单元换成原文"""
    return _TOKEN.sub(r"\1", _ANSI.sub("", text)).strip()


def _parse_error_message(error: Exception) -> str:
    """把 sqlglot 的解析报错写成带行列号的中文说明"""
    details = [item for item in getattr(error, "errors", None) or [] if isinstance(item, dict)]
    if details:
        first = details[0]
        near = _clean(str(first.get("highlight") or ""))
        position = f"第 {first.get('line')} 行第 {first.get('col')} 列"
        if near:
            position += f"（{near} 附近）"
        return f"SQL 无法按 PostgreSQL 语法解析：{position}：{_clean(str(first.get('description') or error))}"
    text = _clean(str(error))
    matched = _TOKEN_POSITION.search(text)          # 词法错误只在文字末尾写「行:列」
    position = f"第 {matched.group(1)} 行第 {matched.group(2)} 列：" if matched else ""
    return f"SQL 无法按 PostgreSQL 语法解析：{position}{text}"


def _closest(word: str, candidates: Iterable[str], n: int = MAX_SUGGESTIONS) -> list[str]:
    """按 difflib 的相似度（SequenceMatcher.ratio）取最接近的 n 个候选，相似度相同时按名字排

    几百张表逐个算 ratio 很慢。quick_ratio 是 ratio 的上界而且便宜：先按上界从高到低排，
    逐个算准确值，上界已经追不上第 n 名时停下。结果与全部算一遍相同。
    """
    matcher = difflib.SequenceMatcher()
    matcher.set_seq2(word)
    bounds = []
    for candidate in set(candidates):
        matcher.set_seq1(candidate)
        bounds.append((matcher.quick_ratio(), candidate))
    bounds.sort(key=lambda item: (-item[0], item[1]))
    best: list[tuple[float, str]] = []
    for bound, candidate in bounds:
        if len(best) == n and bound < best[-1][0]:
            break
        matcher.set_seq1(candidate)
        best.append((matcher.ratio(), candidate))
        best.sort(key=lambda item: (-item[0], item[1]))
        del best[n:]
    return [candidate for _, candidate in best]


def _join(names: Iterable[str]) -> str:
    """列表写成「a、b、c」，为空时写「（无）」"""
    text = "、".join(names)
    return text or "（无）"


def _pair_key(a: tuple[str, str], b: tuple[str, str]) -> tuple[tuple[str, str], tuple[str, str]]:
    """一对字段的规范写法，与左右顺序无关"""
    return (a, b) if a <= b else (b, a)


def _describe(node: exp.Expression) -> str:
    """语句或节点的中文说明"""
    if isinstance(node, exp.Command):
        return f"{str(node.this).upper()} 命令"
    for node_type, label in FORBIDDEN_NODES:
        if isinstance(node, node_type):
            return label
    return f"{type(node).__name__} 语句"


def _unwrap_query(statement: exp.Expression) -> exp.Expression:
    """剥掉整条查询外面的括号：(SELECT ...) 按里面的 SELECT 分析

    括号上挂的 WITH、ORDER BY 等要先用 _hoist_wrapper_clauses 挪进去，否则剥掉括号就丢了。
    """
    node = statement
    while isinstance(node, exp.Subquery) and isinstance(node.this, exp.Query):
        node = node.this
    return node


def _hoist_wrapper_clauses(tree: exp.Expression) -> list[str]:
    """把挂在括号上的 WITH、ORDER BY、LIMIT、OFFSET 挪进括号里面的查询，直接改 tree

    sqlglot 把「WITH c AS (…) (SELECT … FROM c)」和「(SELECT …) ORDER BY x」解析成括号节点带着这些子句，
    剥括号、建作用域时它们不在查询层里，CTE 体和排序表达式就绕过了检查。PostgreSQL 把它们当作写在
    括号里面那条查询上，所以这里照样处理。括号里面已经有同类子句时 PostgreSQL 报语法错误，这里不动它。

    Args:
        tree: 语法树，整棵树里的括号都处理

    Returns:
        括号里外都有的子句称呼（WITH、ORDER BY、LIMIT、OFFSET），按出现顺序，没有重复时为空列表
    """
    clashes: list[str] = []

    def push_down(wrapper: exp.Subquery) -> None:
        inner = wrapper.this
        if not isinstance(inner, exp.Query):
            return
        for key, label in _WRAPPER_CLAUSES.items():
            clause = wrapper.args.get(key)
            if clause is None:
                continue
            if inner.args.get(key) is not None:
                if label not in clashes:
                    clashes.append(label)
                continue
            wrapper.set(key, None)
            inner.set(key, clause)
        if isinstance(inner, exp.Subquery):         # 多层括号：继续往里挪
            push_down(inner)

    for wrapper in list(tree.find_all(exp.Subquery)):
        push_down(wrapper)
    return clashes


def _scope_label(scope: Scope) -> str:
    """查询层的中文称呼，写进问题里帮助定位"""
    if scope.is_root:
        return "主查询"
    parent = scope.expression.parent
    if scope.is_cte and isinstance(parent, exp.CTE):
        return f"CTE {parent.alias}"
    if scope.is_derived_table and parent is not None and parent.alias:
        return f"派生表 {parent.alias}"
    if scope.is_union:
        return "UNION 的一个分支"
    if scope.is_udtf:
        return "表函数或 VALUES"
    return "子查询"


def _clause_of(node: exp.Expression, top: exp.Expression) -> str:
    """节点在查询层 top 的哪个子句里：expressions（投影）、where、joins、group、having、order 等"""
    while node.parent is not None and node.parent is not top:
        node = node.parent
    return node.arg_key if node.parent is top else ""


def _explicit_aliases(select: exp.Expression) -> set[str]:
    """SELECT 列表里另起的输出别名。「a.x AS x」这种和字段同名的不算"""
    if not isinstance(select, exp.Select):
        return set()
    return {p.alias for p in select.expressions
            if isinstance(p, exp.Alias) and p.alias
            and not (isinstance(p.this, exp.Column) and p.this.name == p.alias)}


def _alias_misuse(clause: str, name: str) -> str:
    """在不该用输出别名的位置引用了输出别名的问题文字"""
    where = _ALIAS_SITES[clause]
    if clause in ("order", "group"):
        return (f"{where}里的输出别名 {name} 只能单独出现，放进表达式 PostgreSQL 就认不出了，"
                "请写出别名对应的原表达式")
    if clause in ("expressions", "windows"):
        return f"{where}里不能引用同一层的输出别名 {name}，同层的别名互相看不到，请写出别名对应的原表达式"
    return (f"{where}里不能引用 SELECT 的输出别名 {name}，PostgreSQL 在这里还看不到 SELECT 列表，"
            "请写出别名对应的原表达式")


def _embedded(column: exp.Column, clause: exp.Expression) -> bool:
    """ORDER BY、GROUP BY 里的字段是不是嵌在表达式里：字段往上到子句之间只隔着排序项、括号、分组集合，就是单独出现"""
    node = column.parent
    while node is not None and node is not clause:
        if not isinstance(node, _STANDALONE_WRAPPERS):
            return True
        node = node.parent
    return False


def _alias_sites(select: exp.Select) -> Iterator[tuple[str, exp.Column]]:
    """本层 SELECT 里 PostgreSQL 看不到同层输出别名的位置上，没带别名的字段

    WHERE、HAVING、SELECT 列表、WINDOW 子句的任何位置都看不到；ORDER BY、GROUP BY 里只有别名单独出现才看得到
    （位置序号不是字段，不在其内）。嵌套子查询里的字段属于它自己那一层。

    Yields:
        (子句的参数名, 字段)
    """
    for clause in ("where", "having", "expressions", "windows", "order", "group"):
        nodes = select.args.get(clause)
        for node in nodes if isinstance(nodes, list) else [nodes]:
            if node is None:
                continue
            for column in node.find_all(exp.Column):
                if (column.table or column.find_ancestor(exp.Select) is not select
                        or (clause in ("order", "group") and not _embedded(column, node))):
                    continue
                yield clause, column


def _row_sensitive(agg: exp.AggFunc) -> bool:
    """聚合结果会不会因为关联发散、行数翻倍而变：MIN、MAX、带 DISTINCT 的不会；窗口函数不算汇总"""
    if isinstance(agg, (exp.Min, exp.Max)) or isinstance(agg.this, exp.Distinct):
        return False
    node: exp.Expression = agg
    while isinstance(node.parent, _AGG_WRAPPERS):
        node = node.parent
    return not (isinstance(node.parent, exp.Window) and node.arg_key == "this")


def _mentions(expr: exp.Expression, alias: str) -> bool:
    """表达式里有没有别名 alias 的字段"""
    return any(column.table == alias for column in expr.find_all(exp.Column))


def _equality_targets(condition: exp.Expression) -> list[exp.Column]:
    """一个 AND 子句约束成单值的字段

    「字段 = 另一侧」：另一侧可以是别的别名的字段、字面量、函数，只要不引用这个字段自己的别名；
    只有一个元素的 IN 也算。字段两侧的类型转换、TRIM、括号先剥掉。
    """
    condition = condition.unnest()
    if isinstance(condition, exp.EQ):
        targets = []
        for side, other in ((condition.this, condition.expression), (condition.expression, condition.this)):
            column = unwrap(side)
            if isinstance(column, exp.Column) and column.table and not _mentions(other, column.table):
                targets.append(column)
        return targets
    if isinstance(condition, exp.In) and not condition.args.get("query") and len(condition.expressions) == 1:
        column = unwrap(condition.this)
        if isinstance(column, exp.Column) and column.table and not _mentions(condition.expressions[0], column.table):
            return [column]
    return []


def _aligned_dates(condition: exp.Expression, tracked: Collection[tuple[str, str]]
                   ) -> tuple[tuple[str, str], tuple[str, str]] | None:
    """「日期字段 = 日期字段」：剥掉括号、别名、TRIM、任何类型转换后，两侧都是被看的（别名, 日期字段）时返回这两个"""
    condition = condition.unnest()
    if not isinstance(condition, exp.EQ):
        return None
    sides = []
    for side in (condition.this, condition.expression):
        while isinstance(side, (exp.Alias, exp.Paren, exp.Trim, exp.Cast)):      # TryCast 是 Cast 的子类
            side = side.this
        sides.append(side)
    left, right = sides
    if isinstance(left, exp.Column) and isinstance(right, exp.Column):
        pair = ((left.table, left.name), (right.table, right.name))
        if pair[0] in tracked and pair[1] in tracked:
            return pair
    return None


def _is_null_test(condition: exp.Expression) -> bool:
    """条件是不是 IS NULL、IS NOT NULL：它们没有限定具体是哪天"""
    node = condition.unnest()
    if isinstance(node, exp.Not):
        node = node.this.unnest()
    return isinstance(node, exp.Is) and isinstance(node.expression, exp.Null)


def _is_date_extreme(column: exp.Column, select: exp.Select) -> bool:
    """日期字段是不是用在 MIN(日期)、MAX(日期)、COUNT(DISTINCT 日期) 里，或者是 SELECT DISTINCT 的一个输出"""
    parent = column.parent
    if isinstance(parent, exp.Distinct):
        parent = parent.parent
        if isinstance(parent, exp.Count):
            return True
    if isinstance(parent, (exp.Min, exp.Max)):
        return True
    distinct = select.args.get("distinct")
    if distinct is None or distinct.args.get("on"):
        return False
    projection = column
    while projection.parent is not None and projection.parent is not select:
        projection = projection.parent
    return projection.parent is select and projection.arg_key == "expressions" and unwrap(projection) is column


def _date_extremes_only(scope: Scope, alias: str, date_column: str) -> bool:
    """这一层里别名 alias 的字段是不是只用来取日期的最早、最晚、去重值

    MIN(日期)、MAX(日期)、DISTINCT 日期这几种写法的结果不会把多天的数据加在一起，不要求日期条件。
    至少要有一处这样的用法；WHERE、关联条件、GROUP BY 里的过滤和分组不产生输出，不碍事；
    ORDER BY 里只允许按日期字段排序；SELECT 列表、HAVING 里取了别的字段就不算。
    """
    select = scope.expression
    found = False
    for column in scope.find_all(exp.Column):
        if column.table != alias:
            continue
        clause = _clause_of(column, select)
        if clause in ("where", "joins", "group"):
            continue
        if column.name == date_column and _is_date_extreme(column, select):
            found = True
        elif not (clause == "order" and column.name == date_column and isinstance(column.parent, exp.Ordered)):
            return False
    return found


# ── 校验器 ────────────────────────────────────────────────

class SqlValidator:
    """对照元数据做静态校验

    构造时建好「表 → 字段」和「无序表对 → 已知关联」两个索引，之后每次 validate 只查索引。
    """

    def __init__(self, store: MetadataStore, dialect: str = "postgres") -> None:
        """
        Args:
            store: 元数据
            dialect: 生成 SQL 使用的方言。作用域解析按 PostgreSQL 实现，目前只支持 postgres（GaussDB 与之兼容）

        Raises:
            ValueError: 方言不是 postgres
        """
        if dialect != DIALECT:
            raise ValueError(f"静态校验目前只支持 {DIALECT} 方言（GaussDB 与之兼容），收到 {dialect}")
        self.store = store
        self.dialect = dialect
        tables = {t.table_id: t for t in store.tables()}
        metas = {tid: store.columns(tid) for tid in tables}
        self._tables: dict[str, TableMeta] = tables
        self._columns: dict[str, tuple[str, ...]] = {
            tid: tuple(column.column_name for column in columns) for tid, columns in metas.items()}
        self._column_sets: dict[str, frozenset[str]] = {
            tid: frozenset(names) for tid, names in self._columns.items()}
        self._catalog = _Catalog(tables, metas)
        self._code_tables = code_tables(tables.values(), lambda tid: metas.get(tid, []))     # R9 用
        self._by_name: dict[str, list[str]] = {}          # 表名（不带模式）→ 全名
        for tid in tables:
            self._by_name.setdefault(tid.split(".", 1)[-1], []).append(tid)
        self._relations: dict[tuple[str, str], _Mappings] = {}
        for relation in store.relations():
            key = tuple(sorted((relation.left_table, relation.right_table)))
            mappings = self._relations.setdefault(key, _Mappings())
            mappings.relations.append(relation)
            for left, right in zip(relation.left_columns, relation.right_columns):
                mappings.pairs.add(_pair_key((relation.left_table, left), (relation.right_table, right)))

    def validate(self, sql: str, expected_tables: Collection[str] | None = None,
                 expected_columns: Collection[str] | None = None,
                 excluded_tables: Collection[str] = ()) -> ValidationReport:
        """校验一条 SQL

        R1、R2 不通过时直接返回；R3 不通过时不报 R4；作用域解析失败时报一条 R4 并跳过 R5–R7、R9。
        任何输入都返回报告，不抛异常。

        Args:
            sql: 待校验的 SQL
            expected_tables: 查询方案里的表，传入时检查 R8
            expected_columns: 查询方案里的字段（模式.表.字段），传入时检查 R8
            excluded_tables: 查询方案明确去掉的表（QueryPlan.dropped_tables），SQL 用到了报 R8 错误

        Returns:
            校验报告
        """
        run = _Run()
        statement = self._check_parse(sql, run)
        if statement is None or not self._check_query_only(statement, run):
            return run.report()
        query = _unwrap_query(statement)
        tables_ok = self._check_tables(query, run)
        analysis = self._check_columns(query, run, report=tables_ok)
        if analysis is not None:
            root, resolver = analysis
            self._guarded("R5", run, lambda: self._check_snapshot_dates(root, resolver, run))
            self._guarded("R6", run, lambda: self._check_relations(root, resolver, run))
            self._guarded("R7", run, lambda: self._check_fan_out(root, resolver, run))
            self._guarded("R9", run, lambda: self._check_code_literals(root, resolver, run))
        self._check_plan(run, expected_tables, expected_columns, excluded_tables)
        return run.report()

    @staticmethod
    def _guarded(rule: str, run: _Run, check: Callable[[], None]) -> None:
        """执行一条规则；规则代码遇到没见过的写法出错时记一条警告，不让校验中断"""
        try:
            check()
        except Exception as error:  # noqa: BLE001 校验器在导入和生成回路里使用，不能因一条怪 SQL 崩掉
            run.warn(rule, f"{rule} 检查时出错，已跳过：{type(error).__name__}: {_clean(str(error))}")

    # ── R1 R2 ──

    @staticmethod
    def _check_parse(sql: str, run: _Run) -> exp.Expression | None:
        """R1：能按 PostgreSQL 解析，且只有一条语句（空节点和分号节点不算）

        括号上挂的 WITH、ORDER BY、LIMIT、OFFSET 顺手挪进括号里面（见 _hoist_wrapper_clauses），
        括号里外各写一个的是 PostgreSQL 的语法错误。
        """
        try:
            statements = parse_statements(sql)
        except Exception as error:  # noqa: BLE001 ParseError、TokenError 以及 sqlglot 内部异常都算解析失败
            run.error("R1", _parse_error_message(error))
            return None
        if not statements:
            run.error("R1", "没有可以校验的 SQL 语句")
            return None
        if len(statements) > 1:
            run.error("R1", f"只能有一条 SQL 语句，收到 {len(statements)} 条")
            return None
        statement = statements[0]
        if clashes := _hoist_wrapper_clauses(statement):
            run.error("R1", f"括号里面和外面各写了一个 {_join(clashes)}，PostgreSQL 不允许同一条查询有两个，请合并成一个")
            return None
        return statement

    @staticmethod
    def _check_query_only(statement: exp.Expression, run: _Run) -> bool:
        """R2：根节点是查询，树里没有任何写操作、建表、加锁、事务控制的节点"""
        if not isinstance(_unwrap_query(statement), (exp.Select, exp.SetOperation)):
            run.error("R2", f"只能是查询语句（SELECT），收到的是 {_describe(statement)}")
            return False
        found = []
        for node in statement.walk():
            if isinstance(node, _FORBIDDEN_TYPES):
                label = _describe(node)
                if label not in found:
                    found.append(label)
        for label in found:
            run.error("R2", f"查询里不能有 {label}")
        return not found

    # ── R3 ──

    def _check_tables(self, query: exp.Expression, run: _Run) -> bool:
        """R3：实体表（不含 CTE 名）都在元数据里；顺带登记用到的表"""
        ids: list[str] = []
        for table in physical_tables(query):
            tid = table_id(table)
            if tid not in ids:
                ids.append(tid)
        run.tables = ids
        ok = True
        for tid in ids:
            if tid in self._tables:
                continue
            ok = False
            if "." in tid:
                run.error("R3", f"表 {tid} 不在元数据里；最接近的表：{_join(self._closest_tables(tid))}")
                continue
            matches = self._by_name.get(tid, [])
            if len(matches) == 1:
                run.error("R3", f"表 {tid} 没有带模式名，应写成 {matches[0]}")
            elif matches:
                run.error("R3", f"表 {tid} 没有带模式名，多个模式下都有这张表（{_join(matches)}），请写带模式名的全名")
            else:
                run.error("R3", f"表 {tid} 没有带模式名，元数据里也没有叫这个名字的表；"
                                f"最接近的表：{_join(self._closest_tables(tid))}")
        return ok

    def _closest_tables(self, tid: str) -> list[str]:
        """最接近的表全名：带模式的按全名比，不带模式的按表名比"""
        if "." in tid:
            return _closest(tid, self._tables)
        names = _closest(tid, self._by_name)
        return [full for name in names for full in self._by_name[name]][:MAX_SUGGESTIONS]

    # ── R4 ──

    def _check_columns(self, query: exp.Expression, run: _Run,
                       report: bool) -> tuple[Scope, ScopeResolver] | None:
        """R4：按元数据限定字段后，逐个查询层检查每个字段；顺带收集用到的字段

        Args:
            query: 查询
            run: 本次校验的中间结果
            report: 是否报 R4（R3 不通过时只收集、不报）

        Returns:
            (根作用域, 解析器)，供 R5–R7 使用；作用域解析失败时为 None
        """
        raw = normalize_identifiers(query.copy(), dialect=DIALECT)       # 没限定的树，标识符已规范
        duplicated = report and self._check_duplicate_aliases(raw, run)
        try:
            qualified, gaps = qualify_with_catalog(query, self._catalog)
            root = build_scope(qualified)
            resolver = ScopeResolver(root, catalog=self._catalog)
        except Exception as error:  # noqa: BLE001 SqlScopeError 以及作用域树建不起来的情况
            self._fallback_scan(raw, error, run, report=report and not duplicated)
            return None
        reported: set[FieldRef] = set()
        self._guarded("R4", run, lambda: reported.update(self._scan(root, resolver, run, report)))
        if report:
            for gap in sorted(gaps - reported, key=lambda ref: (ref.table, ref.column)):
                run.error("R4", f"表 {gap.table} 没有字段 {gap.column}")       # 兜底：缺口一个都不漏
            self._guarded("R4", run, lambda: self._check_alias_references(raw, run))
        return root, resolver

    def _check_duplicate_aliases(self, raw: exp.Expression, run: _Run) -> bool:
        """同一层 FROM、JOIN 里两个来源用了同一个名字。PostgreSQL 会报错，sqlglot 限定时却会悄悄改名"""
        found = False
        for select in raw.find_all(exp.Select):
            items = []
            from_ = select.args.get("from")
            if from_ is not None:
                items.append(from_.this)
            items.extend(join.this for join in select.args.get("joins") or [])
            names = [item.alias_or_name for item in items if item.alias_or_name]
            for name in sorted({name for name in names if names.count(name) > 1}):
                run.error("R4", f"同一层查询里有多个来源都叫 {name}，请给它们起不同的别名")
                found = True
        return found

    def _fallback_scan(self, raw: exp.Expression, error: Exception, run: _Run, report: bool) -> None:
        """限定失败时在没限定的树上扫一遍，尽量指出具体哪个字段不对；指不出来就报 sqlglot 的原因"""
        before = len(run.issues)
        try:
            root = build_scope(raw)
            self._scan(root, ScopeResolver(root, catalog=self._catalog), run, report)
        except Exception:  # noqa: BLE001 没限定的树也建不起作用域时，只能报笼统的原因
            pass
        if report and len(run.issues) == before:
            text = _clean(str(error)).replace("SQL 无法按作用域解析: ", "")
            matched = re.match(r"Unknown column: (\S+)", text)
            if matched:
                text = f"引用了 CTE 或子查询里不存在的字段 {matched.group(1)}"
            run.error("R4", f"SQL 无法按作用域解析：{text}")

    def _scan(self, root: Scope, resolver: ScopeResolver, run: _Run, report: bool) -> set[FieldRef]:
        """逐个查询层检查每个字段，返回报过的「实体表没有这个字段」"""
        reported: set[FieldRef] = set()
        for scope in root.traverse():
            for column in scope.find_all(exp.Column, bfs=False):
                if isinstance(column.this, exp.Star):
                    continue
                if column.table:
                    self._check_qualified(scope, column, resolver, run, report, reported)
                else:
                    self._check_unqualified(scope, column, resolver, run, report)
        return reported

    def _source_columns(self, resolver: ScopeResolver, source: exp.Table | Scope) -> tuple[str, ...] | None:
        """一个来源有哪些字段：实体表查元数据，查询层看输出列；不知道时为 None"""
        if isinstance(source, Scope):
            names = resolver.output_names(source)
            return None if names is None else tuple(names)
        return self._columns.get(table_id(source))

    def _describe_source(self, alias: str, source: exp.Table | Scope) -> str:
        """「别名（实体表全名或查询层）」"""
        return f"{alias}（{table_id(source) if isinstance(source, exp.Table) else _scope_label(source)}）"

    def _visible(self, scope: Scope, resolver: ScopeResolver) -> dict[str, exp.Table | Scope]:
        """本层可以引用的来源：本层的，加上相关子查询能看到的外层的"""
        visible: dict[str, exp.Table | Scope] = {}
        current: Scope | None = scope
        while current is not None:
            for alias, source in resolver.sources(current).items():
                visible.setdefault(alias, source)
            current = current.parent if current.can_be_correlated else None
        return visible

    def _check_qualified(self, scope: Scope, column: exp.Column, resolver: ScopeResolver, run: _Run,
                         report: bool, reported: set[FieldRef]) -> None:
        """带别名的字段：别名要有定义；指向实体表就查字段清单，指向查询层就查它的输出列"""
        alias, name = column.table, column.name
        source = resolver.source_of(scope, alias)
        if source is None:
            if report:
                run.error("R4", f"别名 {alias} 没有定义（写作 {alias}.{name}）；"
                                f"可用的别名：{_join(self._visible(scope, resolver))}（位置：{_scope_label(scope)}）")
            return
        if is_table_function(source):          # 表函数的字段由函数产生：不查元数据，也不算用到的实体字段
            return
        if isinstance(source, exp.Table):
            tid = table_id(source)
            known = self._column_sets.get(tid)
            if known is None or name in known:                 # 不在元数据里的表已由 R3 报过
                run.used.add(FieldRef(table=tid, column=name))
                return
            if report:
                message = (f"表 {tid} 没有字段 {name}（写作 {alias}.{name}）；"
                           f"最接近的字段：{_join(_closest(name, self._columns[tid]))}")
                others = [self._describe_source(other, item) for other, item in resolver.sources(scope).items()
                          if other != alias and name in (self._source_columns(resolver, item) or ())]
                if others:
                    message += f"；{_join(others)}有这个字段"
                run.error("R4", message)
                reported.add(FieldRef(table=tid, column=name))
            return
        outputs = resolver.output_names(source)
        if outputs is not None and name not in outputs:
            if report:
                run.error("R4", f"{_scope_label(source)} 没有输出字段 {name}（写作 {alias}.{name}）；"
                                f"最接近的输出字段：{_join(_closest(name, outputs))}")
            return
        run.used.update(resolver.resolve_column(scope, column).sources)

    def _owners(self, scope: Scope, resolver: ScopeResolver, name: str) -> tuple[Scope | None, list[str], bool]:
        """没带别名的字段在哪些来源里：先找本层，相关子查询再往外层找

        Returns:
            (找到它的查询层, 有这个字段的别名, 途中是否有不知道字段的来源)
        """
        unknown = False
        current: Scope | None = scope
        while current is not None:
            owners = resolver.owners(current, name)
            if owners:
                return current, owners, unknown
            unknown = unknown or any(self._source_columns(resolver, source) is None
                                     for source in resolver.sources(current).values())
            current = current.parent if current.can_be_correlated else None
        return None, [], unknown

    def _check_unqualified(self, scope: Scope, column: exp.Column, resolver: ScopeResolver, run: _Run,
                           report: bool) -> None:
        """不带别名的字段：ORDER BY、GROUP BY 里的输出别名、伪列放过；其余在全部来源里找，恰好一个才通过"""
        name = column.name
        clause = _clause_of(column, scope.expression)
        if clause in ("order", "group") and name in self._output_aliases(scope, resolver):
            return
        owner_scope, owners, unknown = self._owners(scope, resolver, name)
        if len(owners) == 1:
            run.used.update(resolver.resolve_column(scope, column).sources)
            return
        if not report:
            return
        if owners:
            sources = resolver.sources(owner_scope)
            described = [self._describe_source(alias, sources[alias]) for alias in owners]
            run.error("R4", f"字段 {name} 有歧义：{_join(described)}都有这个字段，请写成 别名.{name}"
                            f"（位置：{_scope_label(owner_scope)}）")
            return
        if name.lower() in PSEUDO_COLUMNS or unknown:      # 系统值；或者来源里有不知道字段的表，无法判断
            return
        if clause in _ALIAS_FREE_CLAUSES and name in _explicit_aliases(scope.expression):
            run.error("R4", _alias_misuse(clause, name))
            return
        candidates = [item for source in self._visible(scope, resolver).values()
                      for item in self._source_columns(resolver, source) or ()]
        hint = f"；最接近的字段：{_join(_closest(name, candidates))}" if candidates else ""
        run.error("R4", f"字段 {name} 在可见的来源里都没有{hint}（位置：{_scope_label(scope)}）")

    @staticmethod
    def _output_aliases(scope: Scope, resolver: ScopeResolver) -> set[str]:
        """查询层的输出列名，ORDER BY、GROUP BY 可以直接引用"""
        if isinstance(scope.expression, exp.Select):
            return set(scope.expression.named_selects)
        return set(resolver.output_names(scope) or ())

    def _check_alias_references(self, raw: exp.Expression, run: _Run) -> None:
        """在 PostgreSQL 看不到同层输出别名的位置引用了它：WHERE、HAVING、SELECT 列表、窗口，
        以及 ORDER BY、GROUP BY 里嵌在表达式中的（别名单独出现、位置序号都可以）

        sqlglot 限定字段时会把这种引用悄悄换成别名对应的表达式，DuckDB 也能执行，但 PostgreSQL 会报字段不存在，
        所以要在没限定的树上单独查。先按名字粗筛，有嫌疑时才建作用域确认它不是某个来源的字段。
        """
        suspects: list[tuple[exp.Select, str, exp.Column]] = []
        for select in raw.find_all(exp.Select):
            if aliases := _explicit_aliases(select):
                suspects.extend((select, clause, column) for clause, column in _alias_sites(select)
                                if column.name in aliases)
        if not suspects:
            return
        root = build_scope(raw)
        resolver = ScopeResolver(root, catalog=self._catalog)
        scopes = {id(scope.expression): scope for scope in root.traverse()}
        for select, clause, column in suspects:
            scope = scopes.get(id(select))
            if scope is None:
                continue
            _, owners, unknown = self._owners(scope, resolver, column.name)
            if not owners and not unknown:
                run.error("R4", _alias_misuse(clause, column.name))

    # ── R5 ──

    def _check_snapshot_dates(self, root: Scope, resolver: ScopeResolver, run: _Run) -> None:
        """R5：每个查询层里指向快照表的别名，它的数据日期字段要在本层的 WHERE 或某个 JOIN 的条件里受到具体条件约束

        只拿来取最早、最晚、去重日期的快照表不要求日期条件，见 _date_extremes_only。
        """
        for scope in root.traverse():
            select = scope.expression
            if not isinstance(select, exp.Select):
                continue
            snapshots: dict[str, TableMeta] = {}
            for alias, source in resolver.sources(scope).items():
                meta = self._tables.get(table_id(source)) if isinstance(source, exp.Table) else None
                if meta is not None and meta.table_type == SNAPSHOT_TYPE and meta.date_column:
                    snapshots[alias] = meta
            if not snapshots:
                continue
            dated = self._dated(select, {(alias, meta.date_column) for alias, meta in snapshots.items()})
            for alias, meta in snapshots.items():
                if (alias, meta.date_column) in dated or _date_extremes_only(scope, alias, meta.date_column):
                    continue
                run.error("R5", f"快照表 {meta.table_id}（别名 {alias}）没有数据日期条件，会把多天的数据加在一起；"
                                f"请在 WHERE 或关联条件里加上 {alias}.{meta.date_column} = {DATE_EXAMPLE}"
                                f"（位置：{_scope_label(scope)}）")

    @staticmethod
    def _dated(select: exp.Select, tracked: set[tuple[str, str]]) -> set[tuple[str, str]]:
        """本层 WHERE 和各个 JOIN 条件的 AND 子句里，受到具体日期条件约束的（别名, 日期字段）

        两个快照表的日期字段互相写等值只是互相对齐，并不限定是哪天：把这样对齐的字段用并查集归成块，
        块里至少有一个字段受具体条件约束，整块才算有日期条件。具体条件指等于字面量、函数、子查询的结果
        或者别的字段，IN、BETWEEN、比较运算；IS [NOT] NULL 不算。

        Args:
            select: 查询层
            tracked: 要看的（别名, 日期字段）

        Returns:
            有日期条件的（别名, 日期字段），是 tracked 的子集
        """
        parent = {node: node for node in tracked}

        def find(node: tuple[str, str]) -> tuple[str, str]:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        conditions = conjuncts(select.args.get("where"))
        for join in select.args.get("joins") or []:
            conditions += conjuncts(join.args.get("on"))
        concrete: set[tuple[str, str]] = set()
        for condition in conditions:
            mentioned = {(column.table, column.name) for column in walk_in_scope(condition)
                         if isinstance(column, exp.Column)} & tracked
            if not mentioned:
                continue
            if aligned := _aligned_dates(condition, tracked):
                parent[find(aligned[0])] = find(aligned[1])
            elif not _is_null_test(condition):
                concrete |= mentioned
        constrained = {find(node) for node in concrete}
        return {node for node in tracked if find(node) in constrained}

    # ── R6 ──

    def _check_relations(self, root: Scope, resolver: ScopeResolver, run: _Run) -> None:
        """R6：每个追溯到实体表的等值关联键对，两表之间要有一条关联在同一位置包含这对字段"""
        seen: set[tuple[tuple[str, str], tuple[str, str]]] = set()
        for scope in root.traverse():
            for join in resolver.joins(scope):
                for key in join.keys:
                    left, right = (key.left.table, key.left.column), (key.right.table, key.right.column)
                    pair = _pair_key(left, right)
                    if pair in seen:
                        continue
                    seen.add(pair)
                    mappings = self._relations.get(tuple(sorted((left[0], right[0]))))
                    if mappings is not None and pair in mappings.pairs:
                        continue
                    run.warn("R6", self._relation_message(key, mappings))

    @staticmethod
    def _relation_message(key: JoinKey, mappings: _Mappings | None) -> str:
        """R6 的问题文字：附上这对表已知的关联"""
        condition = f"{key.left.table}.{key.left.column} = {key.right.table}.{key.right.column}"
        if mappings is None:
            return f"关联条件 {condition} 不在已知的关联关系里，这两张表之间没有已知关联，请确认关联字段"
        known = "；".join(
            " AND ".join(f"{r.left_table}.{a} = {r.right_table}.{b}" for a, b in zip(r.left_columns, r.right_columns))
            + f"（{r.cardinality}）" for r in mappings.relations)
        return f"关联条件 {condition} 不在已知的关联关系里；这两张表已知的关联：{known}"

    # ── R7 ──

    def _check_fan_out(self, root: Scope, resolver: ScopeResolver, run: _Run) -> None:
        """R7：含汇总的查询层里，被汇总表以外的实体表，粒度字段要全部被等值条件约束"""
        for scope in root.traverse():
            select = scope.expression
            if not isinstance(select, exp.Select):
                continue
            aggregates = [agg for agg in scope.find_all(exp.AggFunc) if _row_sensitive(agg)]
            if not aggregates:
                continue
            sources = resolver.sources(scope)
            summed = set()
            for agg in aggregates:
                body = agg.parent if isinstance(agg.parent, exp.Filter) else agg
                summed.update(column.table for column in body.find_all(exp.Column) if column.table in sources)
            if not summed and select.args.get("from") is not None:          # 只有 COUNT(*) 这类
                summed.add(select.args["from"].this.alias_or_name)
            constrained = self._constrained(select)
            for alias, source in sources.items():
                if alias in summed or not isinstance(source, exp.Table):
                    continue
                meta = self._tables.get(table_id(source))
                if meta is None or not meta.grain_columns:
                    continue
                missing = [name for name in meta.grain_columns if (alias, name) not in constrained]
                if missing:
                    run.warn("R7", f"汇总查询关联了 {meta.table_id}（别名 {alias}），它的粒度是 "
                                   f"({', '.join(meta.grain_columns)})，其中 {', '.join(missing)} 没有用等值条件约束，"
                                   f"一条记录可能关联出多行，汇总结果会重复计算（位置：{_scope_label(scope)}）")

    @staticmethod
    def _constrained(select: exp.Select) -> set[tuple[str, str]]:
        """本层 WHERE 和各个 JOIN 条件的 AND 子句里，被等值约束成单值的（别名, 字段）"""
        conditions = conjuncts(select.args.get("where"))
        for join in select.args.get("joins") or []:
            conditions += conjuncts(join.args.get("on"))
        return {(column.table, column.name) for condition in conditions for column in _equality_targets(condition)}

    # ── R8 ──

    @staticmethod
    def _check_plan(run: _Run, expected_tables: Collection[str] | None,
                    expected_columns: Collection[str] | None, excluded_tables: Collection[str] = ()) -> None:
        """R8：与查询方案对比。表看两边的差集；字段只看方案里有、SQL 没用到的；用到了方案明确去掉的表是错误"""
        excluded = {name.strip().lower() for name in excluded_tables if name.strip()}
        if back := sorted(excluded & set(run.tables)):
            run.error("R8", f"查询方案已经去掉了 {_join(back)}（只用来关联、没有取字段，去掉不影响结果），"
                            f"SQL 里不要再关联，删掉这个关联和它的关联条件")
        if expected_tables is not None:
            expected = {name.strip().lower() for name in expected_tables if name.strip()}
            used = set(run.tables)
            if missing := sorted(expected - used):
                run.warn("R8", f"查询方案里的表没有用到：{_join(missing)}")
            if extra := sorted(used - expected - excluded):
                run.warn("R8", f"用到了查询方案之外的表：{_join(extra)}")
        if expected_columns is not None:
            expected = {name.strip().lower() for name in expected_columns if name.strip()}
            used_columns = {f"{ref.table}.{ref.column}" for ref in run.used}
            if missing := sorted(expected - used_columns):
                run.warn("R8", f"查询方案里的字段没有用到：{_join(missing)}")

    # ── R9 ──

    def _check_code_literals(self, root: Scope, resolver: ScopeResolver, run: _Run) -> None:
        """R9：代码字段和纯中文的名称比较（=、<>、IN、LIKE），查不出数据

        字段要能直取追溯到一个实体表字段，并且能确定它是代码字段：同表有对应的名称字段、
        码值说明里查得到这个名称、或者有以它为粒度的码值表。带数字、字母的字面量不查。
        """
        for scope in root.traverse():
            for node in walk_in_scope(scope.expression):
                for column, value in _literal_comparisons(node):
                    if not is_chinese_name(value):
                        continue
                    resolution = resolver.resolve_column(scope, column)
                    if resolution.kind != "passthrough" or len(resolution.sources) != 1:
                        continue
                    (ref,) = resolution.sources
                    message = self._code_literal_message(ref, value.strip())
                    if message:
                        run.error("R9", message)

    def _code_literal_message(self, ref: FieldRef, value: str) -> str:
        """R9 的问题文字：改用同表的名称字段、按码值说明写代码、或者关联码值表；不能确定是代码字段时返回空串"""
        columns = self._catalog.columns(ref.table)
        meta = next((column for column in columns if column.column_name == ref.column), None)
        if meta is None or not is_code_column(meta):
            return ""
        enum = parse_enum(meta.enum_values)
        if value in enum or value in enum_segments(meta.enum_values):     # 码值说明里登记的取值就是这个中文
            return ""
        codes = [code for code, name in enum.items() if name == value]
        head = f"{ref.table}.{ref.column}（{meta.column_cn}）是代码字段，拿名称「{value}」去比查不出数据"
        code_hint = f"按码值说明应写成 '{codes[0]}'" if codes else ""
        name = name_column(meta, columns)
        if name:
            named = next((column.column_cn for column in columns if column.column_name == name), "")
            return f"{head}；按名称过滤请改用同表的 {ref.table}.{name}" + (f"（{named}）" if named else "") + \
                (f"，或者{code_hint}" if code_hint else "")
        if code_hint:
            return f"{head}；{code_hint}"
        found = [f"{table}.{column}" for table, column in self._code_tables.get(ref.column.lower(), [])
                 if table != ref.table][:MAX_SUGGESTIONS]
        if found:
            return f"{head}；名称在码值表里（{_join(found)}），请关联码值表、按名称字段过滤"
        return ""


def _literal_comparisons(node: exp.Expression) -> list[tuple[exp.Column, str]]:
    """一个比较节点里「字段与字符串字面量」的组合：=、<>、IN 的列表、LIKE / ILIKE（去掉通配符后的文字）"""
    if isinstance(node, (exp.EQ, exp.NEQ)):
        left, right = unwrap(node.left), unwrap(node.right)
        for column, literal in ((left, right), (right, left)):
            if isinstance(column, exp.Column) and isinstance(literal, exp.Literal) and literal.is_string:
                return [(column, literal.this)]
        return []
    if isinstance(node, exp.In):
        column = unwrap(node.this)
        if not isinstance(column, exp.Column):
            return []
        return [(column, item.this) for item in node.expressions if isinstance(item, exp.Literal) and item.is_string]
    if isinstance(node, (exp.Like, exp.ILike)):
        column, pattern = unwrap(node.this), unwrap(node.expression)
        if isinstance(column, exp.Column) and isinstance(pattern, exp.Literal) and pattern.is_string:
            return [(column, pattern.this.replace("%", "").replace("_", ""))]
    return []
