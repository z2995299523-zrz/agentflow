"""
加工脚本解析
把 scripts/ 目录里的加工脚本解析成按目标表归组的脚本片段和逐字段表达式，再与血缘清单合并：
清单里写了的加工逻辑原样保留，空着的用脚本表达式回填；清单里完全没有血缘的字段，用脚本补上并标注来源。

一个脚本文件的解析步骤：
1. 切分：整个文件交给 sqlglot 解析；失败时在注释、字符串、$tag$ 块之外按分号切开，逐条重试。
   单条仍然失败、看起来又是 INSERT 或建表语句的，用正则取目标表，只存原文
2. 只处理 INSERT … SELECT 和 CREATE TABLE … AS SELECT；TRUNCATE、DELETE、DROP、COMMIT、DO 块等不涉及加工逻辑，忽略
3. 临时表（TEMPORARY 的、或者目标表不在表清单里的建表语句）不单独成条：改写成 CTE 注入引用它的语句，
   建表语句的原文拼在那条语句的原文前面
4. 每条语句按查询层限定字段，把投影按位置对应到目标字段，逐个追溯到实体表字段，并记下各查询层的过滤条件和关联，
   以及每个目标字段追溯时经过了哪几个查询层（血缘图据此只给每个字段带它经过的查询层的条件）。
   带引号的大写名字（"NAME"）先转小写，与元数据的名字一致；原文不动

规则：
    W06 语句无法结构化解析（只保存原文），或者投影的列数与目标字段对不上
    W07 语句的目标表不在表清单里
"""
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope

from metadata.checks import WARNING, Issue, ReportSection
from metadata.models import (
    COLUMN_KINDS,
    FieldRef,
    LineageEdge,
    Mart,
    ScriptColumn,
    ScriptFilter,
    ScriptJoin,
    ScriptStatement,
)
from metadata.sqlscope import (
    DIALECT,
    Catalog,
    ScopeResolver,
    SqlScopeError,
    conjuncts,
    parse_statements,
    physical_tables,
    qualify_with_catalog,
    quiet_sqlglot,
    rename_outputs,
    table_id,
)
from metadata.store import MetadataStore

SCRIPTS_DIR_NAME = "scripts"
SECTION_SCRIPTS = "加工脚本"
SECTION_LINEAGE = "血缘合并"
SCRIPT_ENCODINGS = ("utf-8-sig", "gb18030")    # 依次尝试；中文 Windows 上保存的脚本常是 GBK
TOPIC_LAYER = "专题层"
MAIN_SCOPE = "main"                             # 最外层查询的名字；CTE 用自己的名字，其余子查询按书写顺序编号
STATEMENT_JOINER = ";\n\n"                      # 临时表的建表语句拼在目标语句前面时用的分隔
MAX_EXPRESSION_CHARS = 1500                     # 多条语句的表达式合并后的长度上限，与 ScopeResolver 的默认值一致
ELLIPSIS = "…"
EXAMPLE_COUNT = 5                               # 报告里举几个例子
REASON_CHARS = 200                              # 警告里的报错原因最多保留几个字
KIND_NAMES = {"passthrough": "直取", "derived": "加工", "null": "空值", "constant": "常量",
              "etl_time": "加工时间", "unresolved": "追溯不到"}
# 合并血缘时没有上游、不补边的字段按类别计数；加工却没有来源的是 COUNT(*) 这类由行计数得到的度量
NO_UPSTREAM_NAMES = {"null": "空值", "constant": "常量", "etl_time": "加工时间",
                     "derived": "行计数之类的加工", "unresolved": "追溯不到"}

# 注释、字符串、带引号的标识符、$tag$ 块的开头，以及分号。只有前几种之外的分号才是语句分隔
_TOKENS = re.compile(
    r"--[^\n]*"                                 # 行注释
    r"|/\*.*?(?:\*/|\Z)"                        # 块注释
    r"|'(?:[^']|'')*(?:'|\Z)"                   # 字符串，两个单引号表示一个
    r'|"(?:[^"]|"")*(?:"|\Z)'                   # 带引号的标识符
    r"|(?<![\w$])\$(?:[A-Za-z_]\w*)?\$"         # $tag$ 块的开头，结尾另找同一个标记
    r"|;",
    re.S,
)
_LEADING_NOISE = re.compile(r"(?:\s+|--[^\n]*|/\*.*?(?:\*/|\Z))*", re.S)
_FIRST_WORD = re.compile(r"\w+")
_NAME = r'(?:"(?:[^"]|"")*"|[\w$]+)'
_QUALIFIED_NAME = rf"{_NAME}(?:\s*\.\s*{_NAME})*"
_INSERT_INTO = re.compile(r"\binsert\s+into\b", re.I)
_INSERT_TARGET = re.compile(rf"\binsert\s+into\s+({_QUALIFIED_NAME})", re.I)
_CTAS_TARGET = re.compile(
    rf"create\s+(?:(?:global|local|temp|temporary|unlogged)\s+)*table\s+(?:if\s+not\s+exists\s+)?({_QUALIFIED_NAME})",
    re.I)
_CTAS_QUERY = re.compile(r"\bas\s*\(?\s*(?:select|with|values)\b", re.I)
_TOKEN_REPR = re.compile(r"<Token token_type: [^,]*, text: '?(.*?)'?, line: \d+[^>]*>")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_BARE_REFERENCE = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_]*\.){0,2}([A-Za-z_][A-Za-z0-9_]*)")


@dataclass
class ScriptParseResult:
    """一套资料的加工脚本解析结果"""

    statements: list[ScriptStatement] = field(default_factory=list)
    columns: list[ScriptColumn] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)            # W06、W07
    section: ReportSection = field(default_factory=lambda: ReportSection(SECTION_SCRIPTS, ()))
    metadata_gaps: set[FieldRef] = field(default_factory=set)    # 脚本引用了、字段清单里没有的字段


# ── 切分语句 ──────────────────────────────────────────────

@dataclass
class _Span:
    """按分号切出来的一段原文"""

    text: str                       # 去掉首尾空白，不含结尾的分号
    offset: int                     # text 在文件里的起点
    line: int                       # 第一行代码（跳过开头的注释）的行号


@dataclass
class _Piece:
    """一条语句：原文、位置和语法树"""

    text: str
    line: int
    tree: exp.Expression | None     # 解析失败时为 None
    error: str = ""                 # 解析失败的原因


def _scan(text: str) -> Iterator[tuple[str, int, int]]:
    """依次给出注释、字面量（字符串、带引号的标识符、$tag$ 块）和分号的位置：(种类, 起点, 终点)"""
    pos = 0
    while True:
        match = _TOKENS.search(text, pos)
        if match is None:
            return
        token, start, end = match.group(0), match.start(), match.end()
        if token == ";":
            kind = "semicolon"
        elif token.startswith(("--", "/*")):
            kind = "comment"
        elif token.startswith('"'):
            kind = "identifier"
        else:
            kind = "string"
            if token.startswith("$"):           # $tag$ 块：找同一个标记结尾，找不到就到文件末尾
                close = text.find(token, end)
                end = len(text) if close < 0 else close + len(token)
        yield kind, start, end
        pos = end


def _code_only(text: str) -> str:
    """去掉注释，字符串和 $tag$ 块换成空字符串，只留下语句本身的写法，用来判断语句种类、取表名"""
    parts: list[str] = []
    last = 0
    for kind, start, end in _scan(text):
        if kind in ("semicolon", "identifier"):
            continue
        parts.append(text[last:start])
        parts.append(" " if kind == "comment" else "''")
        last = end
    parts.append(text[last:])
    return "".join(parts)


def _split_sql(text: str) -> list[_Span]:
    """在注释、字符串、$tag$ 块之外按分号切开，丢掉只有空白和注释的片段"""
    cuts = [start for kind, start, _ in _scan(text) if kind == "semicolon"]
    spans: list[_Span] = []
    for begin, end in zip([0] + [cut + 1 for cut in cuts], cuts + [len(text)]):
        chunk = text[begin:end]
        body = chunk.strip()
        if not _code_only(body).strip():
            continue
        offset = begin + len(chunk) - len(chunk.lstrip())
        code = offset + _LEADING_NOISE.match(body).end()
        spans.append(_Span(text=body, offset=offset, line=text.count("\n", 0, code) + 1))
    return spans


def _short(text: str) -> str:
    """报错原因压成一行，去掉终端颜色码，过长的截断"""
    text = re.sub(r"\s+", " ", _ANSI.sub("", text)).strip()
    return text if len(text) <= REASON_CHARS else text[:REASON_CHARS] + ELLIPSIS


def _parse_error(error: Exception, text: str, span: _Span) -> str:
    """解析失败的原因；sqlglot 给的行号是相对这一段的，换算成在整个文件里的行号"""
    details = getattr(error, "errors", None)
    if details:
        first = details[0]
        description = _TOKEN_REPR.sub(lambda m: f"「{m.group(1)}」", str(first.get("description") or ""))
        line = first.get("line")
        if isinstance(line, int):
            line += text.count("\n", 0, span.offset)
        return _short(f"{description}（第 {line} 行第 {first.get('col')} 列）")
    return _short(str(error) or type(error).__name__)


def _split_statements(text: str) -> list[_Piece]:
    """把脚本切成一条条语句并解析

    先把整个文件交给 sqlglot，成功并且条数与自己切出来的一致时，按顺序配上原文；
    整个文件解析失败（或者条数对不上）时逐段解析，失败的那一段语法树为 None。
    """
    spans = _split_sql(text)
    try:
        trees = parse_statements(text)
    except Exception:                           # ParseError、TokenError，以及 sqlglot 遇到怪写法时的其它异常
        trees = None
    if trees is not None and len(trees) == len(spans):
        return [_Piece(span.text, span.line, tree) for span, tree in zip(spans, trees)]
    pieces: list[_Piece] = []
    for span in spans:
        try:
            trees = parse_statements(span.text)
        except Exception as error:
            pieces.append(_Piece(span.text, span.line, None, _parse_error(error, text, span)))
            continue
        if len(trees) == 1:
            pieces.append(_Piece(span.text, span.line, trees[0]))
        else:                                   # 极少见：sqlglot 在一段里又切出几条，原文改用生成的写法
            pieces.extend(_Piece(tree.sql(dialect=DIALECT), span.line, tree) for tree in trees)
    return pieces


# ── 语句种类与查询部分 ────────────────────────────────────

def _normalize_name(raw: str) -> str:
    """正则取到的表名规范成表标识：去掉引号，一律转小写（与 _fold_quoted 的约定一致）"""
    parts = re.findall(_NAME, raw)
    return ".".join((part[1:-1].replace('""', '"') if part.startswith('"') else part).lower() for part in parts)


def _fold_quoted(tree: exp.Expression) -> None:
    """带引号、含大写字母的标识符转成小写（仍带引号），原地修改

    元数据里的表名、字段名都是小写（真实资料的转换器统一转过），脚本里写 "NAME" 这类带引号的大写名字时，
    按 PostgreSQL 的规则它和 name 是两个名字，追溯出来的来源就和清单对不上。这里按元数据的约定一律转小写。
    """
    for identifier in tree.find_all(exp.Identifier):
        if identifier.quoted and identifier.this != identifier.this.lower():
            identifier.set("this", identifier.this.lower())


def _write_target(text: str) -> str | None:
    """看起来是 INSERT 或建表语句时返回目标表（取不到为空串），否则返回 None

    只看语句开头的关键字，DO 块、函数体里写的 INSERT 不算。
    """
    code = _code_only(text).strip()
    first = _FIRST_WORD.match(code)
    keyword = first.group(0).lower() if first else ""
    if keyword in ("insert", "with") and _INSERT_INTO.search(code):
        match = _INSERT_TARGET.search(code)
        return _normalize_name(match.group(1)) if match else ""
    if keyword == "create":
        match = _CTAS_TARGET.match(code)
        if match and _CTAS_QUERY.search(code, match.end()):
            return _normalize_name(match.group(1))
    return None


def _identifier_name(node: exp.Expression) -> str:
    """列清单里一个字段名的规范写法：一律小写，与元数据的约定一致"""
    ident = node.this if isinstance(node, (exp.Column, exp.ColumnDef)) else node
    return (ident.this if isinstance(ident, exp.Identifier) else node.name).lower()


def _listed_columns(node: exp.Expression) -> list[str] | None:
    """INSERT、建表语句写明的列清单，没写时为 None"""
    if isinstance(node, exp.Schema) and node.expressions:
        return [_identifier_name(column) for column in node.expressions]
    return None


def _target_table(node: exp.Expression | None) -> exp.Table | None:
    """INSERT、建表语句写入的表节点；不是普通表时返回 None"""
    table = node.this if isinstance(node, exp.Schema) else node
    if isinstance(table, exp.Table) and isinstance(table.this, exp.Identifier):
        return table
    return None


def _unwrap_query(node: exp.Expression | None) -> exp.Expression | None:
    """去掉查询外面的括号；不是查询时返回 None"""
    while isinstance(node, (exp.Subquery, exp.Paren)) and not node.alias:
        node = node.this
    return node if isinstance(node, exp.Query) else None


def _values_query(values: exp.Values) -> exp.Expression:
    """把 VALUES 改写成 SELECT … UNION ALL SELECT …，各列就能像查询一样追溯（都是常量）"""
    selects = []
    for row in values.expressions:
        items = row.expressions if isinstance(row, exp.Tuple) else [row]
        selects.append(exp.select(*[item.copy() for item in items]))
    query: exp.Expression = selects[0]
    for select in selects[1:]:
        query = exp.Union(this=query, expression=select, distinct=False)
    return query


def _prepend_ctes(query: exp.Expression, ctes: list[exp.CTE], recursive: bool = False) -> None:
    """把 CTE 放到查询自己的 WITH 的最前面，没有 WITH 时新建一个"""
    if not ctes:
        return
    with_ = query.args.get("with")
    if with_ is None:
        query.set("with", exp.With(expressions=ctes, recursive=recursive or None))
        return
    with_.set("expressions", [*ctes, *with_.expressions])
    if recursive:
        with_.set("recursive", True)


def _insert_query(insert: exp.Insert) -> exp.Expression | None:
    """INSERT 的查询部分（副本）：写在 INSERT 前面的 WITH 挪进来，VALUES 改写成 UNION ALL；不是查询时返回 None"""
    source = insert.expression
    if isinstance(source, exp.Values):
        query = _values_query(source)
    else:
        query = _unwrap_query(source.copy() if source is not None else None)
    if query is None:
        return None
    with_ = insert.args.get("with")
    if isinstance(with_, exp.With):              # WITH … INSERT …：CTE 挂在 INSERT 上，查询部分里没有
        _prepend_ctes(query, [cte.copy() for cte in with_.expressions], bool(with_.args.get("recursive")))
    return query


def _is_ctas(tree: exp.Expression) -> bool:
    """是不是 CREATE TABLE … AS SELECT"""
    return (isinstance(tree, exp.Create) and str(tree.args.get("kind") or "").upper() == "TABLE"
            and _target_table(tree.this) is not None and _unwrap_query(tree.expression) is not None)


def _is_temporary(create: exp.Create) -> bool:
    """建表语句是否带 TEMP / TEMPORARY"""
    properties = create.args.get("properties")
    return isinstance(properties, exp.Properties) and any(
        isinstance(item, exp.TemporaryProperty) for item in properties.expressions)


# ── 临时表 ────────────────────────────────────────────────

@dataclass(eq=False)
class _Temp:
    """登记的临时表：同一个文件里先建、供后面的语句引用"""

    name: str                       # 建表语句里写的表标识，可能带模式名
    query: exp.Expression           # 建表用的查询
    columns: list[str] | None       # 建表语句写明的列清单
    text: str                       # 建表语句原文
    stmt_index: int
    line: int
    deps: list["_Temp"]             # 它引用的、更早登记的临时表
    used: bool = False              # 已经并入别的语句，或者已经单独成条
    merged: bool = False            # 已经并入别的语句


def _closure(temps: Iterable[_Temp]) -> list[_Temp]:
    """临时表连同它们依赖的临时表，按登记顺序排列（被依赖的在前）"""
    found: dict[int, _Temp] = {}
    stack = list(temps)
    while stack:
        temp = stack.pop()
        if id(temp) not in found:
            found[id(temp)] = temp
            stack.extend(temp.deps)
    return sorted(found.values(), key=lambda temp: temp.stmt_index)


def _rename_tables(node: exp.Expression, names: dict[str, str]) -> exp.Expression:
    """把对带模式名的临时表的引用改成 CTE 名（别名不变），原地修改并返回 node"""
    for table in physical_tables(node):
        tid = table_id(table)
        if "." in tid and tid in names:
            table.set("catalog", None)
            table.set("db", None)
            table.set("this", exp.to_identifier(names[tid]))
    return node


def _inject_temps(query: exp.Expression, temps: list[_Temp]) -> exp.Expression:
    """把临时表改写成 CTE 放进查询的 WITH 最前面，返回新的查询

    不带模式名的临时表直接用原名；带模式名的（目标表不在表清单里的工作表）去掉模式名，
    与已有的 CTE 重名时加序号，查询里对它的引用一并改名。
    """
    if not temps:
        return query
    query = query.copy()
    with_ = query.args.get("with")
    taken = {cte.alias_or_name.lower() for cte in with_.expressions} if isinstance(with_, exp.With) else set()
    names: dict[str, str] = {}
    for temp in temps:
        base = name = temp.name.rsplit(".", 1)[-1]
        if "." in temp.name:                    # 不带模式名的引用要是撞上同名 CTE，本来就指那个 CTE，不会走到这里
            suffix = 2
            while name in taken or name in names.values():
                name, suffix = f"{base}_{suffix}", suffix + 1
        names[temp.name] = name
    ctes = [
        exp.CTE(this=_rename_tables(temp.query.copy(), names),
                alias=exp.TableAlias(this=exp.to_identifier(names[temp.name]),
                                     columns=[exp.to_identifier(column) for column in temp.columns] if temp.columns else None))
        for temp in temps
    ]
    _rename_tables(query, names)
    _prepend_ctes(query, ctes)
    return query


# ── 查询层、过滤条件、关联 ────────────────────────────────

def _source_order(root: exp.Expression) -> dict[int, int]:
    """语法树节点的书写顺序：按节点类型定义参数的顺序深度优先遍历（WITH 在前，然后是投影、FROM、JOIN、WHERE …）

    sqlglot 自带的 walk 按参数设置的先后遍历，而解析器最后才挂上 WITH，CTE 会排到最后，所以自己遍历。
    """
    order: dict[int, int] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        order[id(node)] = len(order)
        keys = [*type(node).arg_types, *(key for key in node.args if key not in type(node).arg_types)]
        children: list[exp.Expression] = []
        for key in keys:
            value = node.args.get(key)
            children.extend(item for item in (value if isinstance(value, list) else [value])
                            if isinstance(item, exp.Expression))
        stack.extend(reversed(children))
    return order


def _ordered_scopes(root: Scope) -> list[Scope]:
    """全部查询层，最外层在前，其余按在语句里出现的位置"""
    order = _source_order(root.expression)
    return sorted(root.traverse(), key=lambda scope: order.get(id(scope.expression), len(order)))


def _name_branches(scope: Scope, name: str, names: dict[int, str]) -> None:
    """UNION 的分支从左到右记为 name#1、name#2 …，中间的 UNION 节点沿用 name"""
    leaves: list[Scope] = []
    stack = [scope]
    while stack:
        current = stack.pop()
        if current.union_scopes:
            names.setdefault(id(current), name)
            stack.extend(reversed(current.union_scopes))
        else:
            leaves.append(current)
    for number, leaf in enumerate(leaves, start=1):
        names[id(leaf)] = f"{name}#{number}"


def _scope_names(scopes: list[Scope], root: Scope) -> dict[int, str]:
    """给每个查询层起名：最外层 main，CTE 用自己的名字，UNION 的分支在所属查询的名字后加 #序号，
    其余子查询（派生表、WHERE 里的子查询、标量子查询）按书写顺序编号 subquery1、subquery2 …"""
    names: dict[int, str] = {}
    count = 0
    for scope in scopes:
        if id(scope) in names:
            continue
        parent = scope.expression.parent
        if scope is root:
            name = MAIN_SCOPE
        elif scope.is_cte and isinstance(parent, exp.CTE):
            name = parent.alias
        else:
            count += 1
            name = f"subquery{count}"
        names[id(scope)] = name
        if scope.union_scopes:
            _name_branches(scope, name, names)
    return names


def _joined_table(resolver: ScopeResolver, scope: Scope, alias: str, names: dict[int, str]) -> str:
    """被关联的来源：实体表写表标识，CTE、派生表写查询层的名字"""
    source = resolver.source_of(scope, alias)
    if isinstance(source, exp.Table):
        return table_id(source)
    if isinstance(source, Scope):
        return names.get(id(source), alias)
    return alias


def _filters_and_joins(resolver: ScopeResolver, scopes: list[Scope],
                       names: dict[int, str]) -> tuple[list[ScriptFilter], list[ScriptJoin]]:
    """各查询层的过滤条件（WHERE、HAVING 按 AND 拆开）和关联，条件里的字段都展开成实体表写法

    WHERE 里两侧别名不同的等值已经作为 IMPLICIT 关联记下，不再算过滤条件。

    Args:
        resolver: 这条语句的作用域解析器
        scopes: _ordered_scopes 给出的全部查询层
        names: _scope_names 给出的查询层名字
    """
    filters: list[ScriptFilter] = []
    joins: list[ScriptJoin] = []
    for scope in scopes:
        select = scope.expression
        if not isinstance(select, exp.Select):
            continue
        name = names[id(scope)]
        scope_joins = resolver.joins(scope)
        implicit = {part.sql() for join in scope_joins if join.join_type == "IMPLICIT"
                    for part in conjuncts(join.condition)}
        for clause in ("where", "having"):
            for condition in conjuncts(select.args.get(clause)):
                if clause == "where" and condition.sql() in implicit:
                    continue
                filters.append(ScriptFilter(scope=name, clause=clause.upper(),
                                            condition=resolver.render(scope, condition)))
        joins.extend(
            ScriptJoin(scope=name, join_type=join.join_type, table=_joined_table(resolver, scope, join.alias, names),
                       condition=resolver.render(scope, join.condition), keys=join.keys)
            for join in scope_joins)
    return filters, joins


def _traced_scope_names(resolver: ScopeResolver, root: Scope, column: str, scopes: list[Scope],
                        names: dict[int, str]) -> list[str]:
    """追溯一个目标字段时经过的查询层的名字，最外层在前，其余按在语句里出现的位置；同名的只留一个"""
    traced = {id(scope) for scope in resolver.trace_scopes(root, column)}
    return list(dict.fromkeys(names[id(scope)] for scope in scopes if id(scope) in traced))


def _ref_key(ref: FieldRef) -> tuple[str, str]:
    """字段引用的排序键"""
    return ref.table, ref.column


def _ref_text(ref: FieldRef) -> str:
    """字段引用写成「模式.表.字段」"""
    return f"{ref.table}.{ref.column}"


# ── 解析一个脚本文件 ──────────────────────────────────────

class _FileParser:
    """解析一个脚本文件；结果按语句顺序排列"""

    def __init__(self, script_name: str, catalog: Catalog) -> None:
        self.script_name = script_name
        self.catalog = catalog
        self.statements: list[ScriptStatement] = []
        self.columns: list[ScriptColumn] = []
        self.issues: list[Issue] = []
        self.gaps: set[FieldRef] = set()
        self.ignored = 0                            # 忽略的语句数（TRUNCATE、DELETE、DO 块等）
        self._temps: dict[str, _Temp] = {}          # 表标识 → 最近一次登记的临时表
        self._registered: list[_Temp] = []

    @property
    def merged_temps(self) -> int:
        """并入了别的语句的临时表个数"""
        return sum(temp.merged for temp in self._registered)

    def parse(self, text: str) -> None:
        """解析全文，结果留在实例的属性里。调用方负责包上 quiet_sqlglot"""
        for index, piece in enumerate(_split_statements(text), start=1):
            self._handle(index, piece)
        for temp in reversed(self._registered):    # 没被引用的临时表单独成条；倒着处理，它依赖的会先被标记
            if not temp.used:
                temp.used = True
                self._emit(temp.stmt_index, temp.line, temp.text, temp.name, temp.query, temp.columns,
                           from_insert=False, needed=_closure(temp.deps))
        self.statements.sort(key=lambda statement: statement.stmt_index)
        self.columns.sort(key=lambda column: column.stmt_index)       # 稳定排序，同一条语句内保持字段顺序

    def _where(self, index: int) -> str:
        return f"{self.script_name} 第 {index} 条语句"

    def _warn(self, rule: str, line: int, message: str) -> None:
        self.issues.append(Issue(WARNING, rule, SECTION_SCRIPTS, line, message))

    def _handle(self, index: int, piece: _Piece) -> None:
        """按语句种类分派：INSERT、建表语句做结构化解析，解析不了的写数语句只存原文，其余忽略"""
        tree = piece.tree
        if tree is None:
            target = _write_target(piece.text)
            if target is None:
                self.ignored += 1
            else:
                self._raw(index, piece.line, piece.text, target, f"无法结构化解析，只保存原文：{piece.error}")
            return
        _fold_quoted(tree)
        if isinstance(tree, exp.Insert):
            self._insert(index, piece, tree)
        elif _is_ctas(tree):
            self._ctas(index, piece, tree)
        elif isinstance(tree, exp.Command) and (target := _write_target(piece.text)) is not None:
            self._raw(index, piece.line, piece.text, target, "含有 sqlglot 不支持的写法，只保存原文")
        else:
            self.ignored += 1

    def _insert(self, index: int, piece: _Piece, insert: exp.Insert) -> None:
        table = _target_table(insert.this)
        query = _insert_query(insert)
        if table is None or query is None:
            target = table_id(table) if table is not None else (_write_target(piece.text) or "")
            self._raw(index, piece.line, piece.text, target, "不是写入普通表的 INSERT … SELECT，只保存原文")
            return
        self._emit(index, piece.line, piece.text, table_id(table), query, _listed_columns(insert.this),
                   from_insert=True, needed=_closure(self._direct_temps(query)))

    def _ctas(self, index: int, piece: _Piece, create: exp.Create) -> None:
        target = table_id(_target_table(create.this))
        query = _unwrap_query(create.expression.copy())
        columns = _listed_columns(create.this)
        if _is_temporary(create) or self.catalog.table(target) is None:
            temp = _Temp(name=target, query=query, columns=columns, text=piece.text, stmt_index=index,
                         line=piece.line, deps=self._direct_temps(query))
            self._temps[target] = temp
            self._registered.append(temp)
            return
        self._emit(index, piece.line, piece.text, target, query, columns,
                   from_insert=False, needed=_closure(self._direct_temps(query)))

    def _direct_temps(self, query: exp.Expression) -> list[_Temp]:
        """查询直接引用的、已经登记的临时表"""
        referenced = dict.fromkeys(table_id(table) for table in physical_tables(query))
        return [self._temps[name] for name in referenced if name in self._temps]

    def _emit(self, index: int, line: int, text: str, target: str, query: exp.Expression,
              listed: list[str] | None, *, from_insert: bool, needed: list[_Temp]) -> None:
        """一条写数语句：并入临时表，结构化解析；解析不了的降级为只存原文"""
        query = _inject_temps(query, needed)
        for temp in needed:
            temp.used = temp.merged = True
        sql_text = STATEMENT_JOINER.join([temp.text for temp in needed] + [text])
        try:
            statement, columns, problems = self._analyze(index, line, target, query, listed, from_insert, sql_text)
        except Exception as error:      # 预期的是 SqlScopeError；其余异常同样降级，不让一条语句拖垮整个导入
            reason = str(error) if isinstance(error, SqlScopeError) else f"{type(error).__name__}: {error}"
            self._raw(index, line, sql_text, target, f"无法按查询层解析，只保存原文：{_short(reason)}")
            return
        self.issues.extend(problems)
        self._add(statement, line)
        self.columns.extend(columns)

    def _analyze(self, index: int, line: int, target: str, query: exp.Expression, listed: list[str] | None,
                 from_insert: bool, sql_text: str) -> tuple[ScriptStatement, list[ScriptColumn], list[Issue]]:
        """限定字段、按位置对应目标字段、逐个追溯，并记下过滤条件和关联

        先限定再改名：限定时 GROUP BY、HAVING 里引用的输出别名会展开，改名之后不会悬空。

        Raises:
            SqlScopeError: 无法按查询层解析
        """
        select_sql = query.sql(dialect=DIALECT, pretty=True, comments=False)
        qualified, gaps = qualify_with_catalog(query, self.catalog)
        self.gaps |= gaps
        problems: list[Issue] = []
        analyzed = qualified
        try:
            names = self._target_names(target, qualified, listed, from_insert)
            if names is not None:
                analyzed = rename_outputs(qualified, names)
        except SqlScopeError as error:
            names = None
            problems.append(Issue(WARNING, "W06", SECTION_SCRIPTS, line,
                                  f"{self._where(index)}写入 {target}：{error}，这条语句的字段表达式跳过"))
        root = build_scope(analyzed)
        resolver = ScopeResolver(root, catalog=self.catalog)
        scopes = _ordered_scopes(root)
        scope_names = _scope_names(scopes, root)
        filters, joins = _filters_and_joins(resolver, scopes, scope_names)
        statement = ScriptStatement(script_name=self.script_name, stmt_index=index, target_table=target,
                                    sql_text=sql_text, select_sql=select_sql, parsed=True,
                                    filters=filters, joins=joins)
        columns = []
        for name in names or []:
            resolution = resolver.resolve_output(root, name)
            # 追溯不到的字段不知道经过了哪些查询层，留空，用到时按这条语句的全部条件处理
            traced = ([] if resolution.kind == "unresolved"
                      else _traced_scope_names(resolver, root, name, scopes, scope_names))
            columns.append(ScriptColumn(
                script_name=self.script_name, stmt_index=index, target_table=target, target_column=name,
                expression=resolver.render_output(root, name),
                sources=sorted(resolution.sources, key=_ref_key), kind=resolution.kind, scopes=traced))
        return statement, columns, problems

    def _target_names(self, target: str, qualified: exp.Expression, listed: list[str] | None,
                      from_insert: bool) -> list[str] | None:
        """目标字段名，按位置对应查询的输出列

        写了列清单就用列清单；INSERT 没写时按字段清单的顺序取前几个（列比表少时其余字段取默认值，
        与 PostgreSQL 一致）；建表语句没写时就是查询的输出名。
        目标表不在字段清单里、INSERT 又没写列清单时无法对应，返回 None，目标表的问题由 W07 说明。

        Raises:
            SqlScopeError: 投影里有展不开的 *，无法确定列数
        """
        if listed is not None:
            return listed
        fields = [column.column_name for column in self.catalog.columns(target)] if from_insert else []
        if from_insert and not fields:
            return None
        projections = qualified.selects
        if any(projection.is_star for projection in projections):
            raise SqlScopeError("投影里有 *，无法按位置对应目标字段")
        if not from_insert:
            return [projection.output_name for projection in projections]
        return fields[: len(projections)] if len(projections) <= len(fields) else fields

    def _raw(self, index: int, line: int, text: str, target: str, reason: str) -> None:
        """只存原文，报 W06"""
        self._warn("W06", line, f"{self._where(index)}（写入 {target or '未知的表'}）{reason}")
        self._add(ScriptStatement(script_name=self.script_name, stmt_index=index, target_table=target,
                                  sql_text=text, parsed=False), line)

    def _add(self, statement: ScriptStatement, line: int) -> None:
        """收下一条语句，目标表不在表清单里时报 W07"""
        self.statements.append(statement)
        if statement.target_table and self.catalog.table(statement.target_table) is None:
            self._warn("W07", line, f"{self._where(statement.stmt_index)}的目标表 {statement.target_table} 不在表清单里")


def parse_script_text(text: str, script_name: str,
                      catalog: Catalog) -> tuple[list[ScriptStatement], list[ScriptColumn], list[Issue], set[FieldRef]]:
    """解析一个脚本文件的全文

    Args:
        text: 脚本全文
        script_name: 脚本文件名，用于标注来源
        catalog: 表和字段的元数据，用来补全查询层的表结构、判断目标表是否在表清单里

    Returns:
        (脚本片段, 逐字段表达式, 问题清单, 字段清单缺口)，片段和表达式按语句顺序排列
    """
    parser = _FileParser(script_name, catalog)
    with quiet_sqlglot():
        parser.parse(text)
    return parser.statements, parser.columns, parser.issues, parser.gaps


def _read_script(path: Path) -> str | None:
    """依次按 utf-8、gb18030 读取脚本，换行统一成 \\n；都读不了时返回 None"""
    data = path.read_bytes()
    for encoding in SCRIPT_ENCODINGS:
        try:
            return data.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError:
            continue
    return None


def _examples(items: list[str]) -> str:
    """报告里举的例子：前几个用顿号连起来，多出来的写「等」"""
    text = "、".join(items[:EXAMPLE_COUNT])
    return text + ("等" if len(items) > EXAMPLE_COUNT else "")


def _scripts_section(mart: Mart, files: int, result: ScriptParseResult, ignored: int, merged: int) -> ReportSection:
    """报告节「加工脚本」：语句与目标字段的统计、字段清单缺口、没有脚本的专题层表"""
    statements = result.statements
    parsed = sum(statement.parsed for statement in statements)
    targets = {statement.target_table for statement in statements if statement.target_table}
    kinds = Counter(column.kind for column in result.columns)
    gaps = [_ref_text(ref) for ref in sorted(result.metadata_gaps, key=_ref_key)]
    listed_fields = {(column.table_id, column.column_name) for column in mart.columns}
    listed_tables = {table.table_id for table in mart.tables}
    unlisted_targets = sorted({f"{column.target_table}.{column.target_column}" for column in result.columns
                               if column.target_table in listed_tables
                               and (column.target_table, column.target_column) not in listed_fields})
    without = [table.table_id for table in mart.tables if table.layer == TOPIC_LAYER and table.table_id not in targets]

    lines = [
        f"- 脚本文件 {files} 个，写数语句 {len(statements)} 条：结构化解析 {parsed} 条，"
        f"只存原文 {len(statements) - parsed} 条；目标表 {len(targets)} 张",
        f"- 临时表 {merged} 个，建表语句已并入引用它的语句；"
        f"TRUNCATE、DELETE、DROP、COMMIT、DO 块等其余语句 {ignored} 条，不涉及加工逻辑，已忽略",
        f"- 目标字段 {len(result.columns)} 个：" + "、".join(f"{KIND_NAMES[kind]} {kinds[kind]}" for kind in COLUMN_KINDS),
        f"- 字段清单缺口 {len(gaps)} 个（脚本引用了、字段清单里没有）" + (f"，例如 {_examples(gaps)}" if gaps else ""),
    ]
    if unlisted_targets:
        lines.append(f"- 写入的目标字段不在字段清单里 {len(unlisted_targets)} 个，例如 {_examples(unlisted_targets)}")
    lines.append(f"- 无加工脚本的专题层表 {len(without)} 张：{'、'.join(without)}" if without
                 else "- 无加工脚本的专题层表：无")
    return ReportSection(SECTION_SCRIPTS, tuple(lines))


def parse_scripts(scripts_dir: Path, mart: Mart) -> ScriptParseResult:
    """解析资料目录里的全部加工脚本

    按文件名顺序读取目录下的 .sql 文件，编码先试 utf-8 再试 gb18030。

    Args:
        scripts_dir: 加工脚本目录，不存在时返回空结果
        mart: 已经校验通过的资料

    Returns:
        解析结果，含报告节「加工脚本」
    """
    scripts_dir = Path(scripts_dir)
    if not scripts_dir.is_dir():
        return ScriptParseResult()
    catalog = MetadataStore.from_mart(mart)
    paths = sorted((path for path in scripts_dir.iterdir() if path.is_file() and path.suffix.lower() == ".sql"),
                   key=lambda path: path.name)
    result = ScriptParseResult()
    ignored = merged = 0
    with quiet_sqlglot():
        for path in paths:
            text = _read_script(path)
            if text is None:
                result.issues.append(Issue(WARNING, "W06", SECTION_SCRIPTS, None,
                                           f"{path.name} 的编码无法识别（试过 utf-8、gb18030），整个文件跳过"))
                continue
            parser = _FileParser(path.name, catalog)
            parser.parse(text)
            result.statements += parser.statements
            result.columns += parser.columns
            result.issues += parser.issues
            result.metadata_gaps |= parser.gaps
            ignored += parser.ignored
            merged += parser.merged_temps
    result.section = _scripts_section(mart, len(paths), result, ignored, merged)
    return result


# ── 血缘合并 ──────────────────────────────────────────────

@dataclass
class _ScriptField:
    """一个目标字段在全部脚本里的表达式；多条语句写同一个字段时按 UNION ALL 合并"""

    kind: str
    sources: list[FieldRef]
    expression: str
    script_of: dict[FieldRef, str]              # 来源 → 最先用到它的脚本名


def _union_kind(kinds: list[str]) -> str:
    """合并几条语句里同一个字段的类别，规则同 UNION：都直取才是直取，都为空才是空值，有直取或加工就是加工"""
    found = set(kinds)
    if found == {"passthrough"}:
        return "passthrough"
    if found == {"null"}:
        return "null"
    if found & {"passthrough", "derived"}:
        return "derived"
    if "unresolved" in found:
        return "unresolved"
    return "etl_time" if "etl_time" in found else "constant"


def _script_fields(columns: Iterable[ScriptColumn]) -> dict[tuple[str, str], _ScriptField]:
    """按目标字段归并脚本表达式，顺序与脚本里第一次出现的顺序一致"""
    groups: dict[tuple[str, str], list[ScriptColumn]] = {}
    for column in columns:
        groups.setdefault((column.target_table, column.target_column), []).append(column)
    merged: dict[tuple[str, str], _ScriptField] = {}
    for key, items in groups.items():
        expressions = list(dict.fromkeys(item.expression for item in items if item.expression))
        if len(expressions) > 1:
            expression = f"UNION({', '.join(expressions)})"
            if len(expression) > MAX_EXPRESSION_CHARS:
                expression = expression[: MAX_EXPRESSION_CHARS - 1] + ELLIPSIS
        else:
            expression = expressions[0] if expressions else ""
        script_of: dict[FieldRef, str] = {}
        for item in items:
            for source in item.sources:
                script_of.setdefault(source, item.script_name)
        merged[key] = _ScriptField(kind=_union_kind([item.kind for item in items]),
                                   sources=sorted(script_of, key=_ref_key), expression=expression,
                                   script_of=script_of)
    return merged


def merge_lineage(mart: Mart, parsed: ScriptParseResult) -> tuple[list[LineageEdge], ReportSection]:
    """把脚本解析结果并入血缘：清单优先、脚本补缺、标注来源

    - 清单边原样保留；加工逻辑为空的用该目标字段的脚本表达式回填，已有的不覆盖
    - 直取标记：有脚本表达式时，脚本里是直取并且边的来源在脚本的来源里才算；
      没有脚本表达式时，同名且是唯一来源，或者加工逻辑是对源字段的裸引用，才算
    - 清单里一条血缘都没有的字段，按脚本追溯到的每个来源补一条边，origin=script；
      空值、常量、加工时间，以及没有来源的加工（COUNT(*) 之类）不补边，只计数

    Args:
        mart: 已经校验通过的资料
        parsed: 加工脚本解析结果

    Returns:
        (合并后的全部血缘边：清单边在前、按清单顺序，补的边在后, 报告节「血缘合并」)
    """
    tables = {table.table_id: table for table in mart.tables}
    scripts = _script_fields(parsed.columns)
    listed: dict[tuple[str, str], list[FieldRef]] = {}
    for edge in mart.lineage:
        listed.setdefault((edge.target_table, edge.target_column), []).append(
            FieldRef(table=edge.source_table, column=edge.source_column))

    edges: list[LineageEdge] = []
    backfilled = 0
    for edge in mart.lineage:
        key = (edge.target_table, edge.target_column)
        script = scripts.get(key)
        source = FieldRef(table=edge.source_table, column=edge.source_column)
        transform = edge.transform
        if not transform.strip() and script is not None and script.expression:
            transform = script.expression
            backfilled += 1
        if script is not None:
            passthrough = script.kind == "passthrough" and source in script.sources
        else:
            same_name = edge.source_column.lower() == edge.target_column.lower()
            passthrough = ((same_name and len(set(listed[key])) == 1)
                           or is_bare_reference(edge.transform, edge.source_column))
        edges.append(edge.model_copy(update={"transform": transform, "is_passthrough": passthrough}))

    fields = {(column.table_id, column.column_name) for column in mart.columns}
    added: list[LineageEdge] = []
    no_upstream: Counter[str] = Counter()
    for key, script in scripts.items():
        if key not in fields or key in listed:
            continue
        if not script.sources:
            no_upstream[script.kind] += 1
            continue
        added += [
            LineageEdge(target_table=key[0], target_column=key[1], source_table=source.table,
                        source_column=source.column, transform=script.expression,
                        script_name=script.script_of[source], source_external=source.table not in tables,
                        is_passthrough=script.kind == "passthrough", origin="script")
            for source in script.sources
        ]
    edges += added
    return edges, _lineage_section(mart, parsed, scripts, listed, edges[: len(mart.lineage)], added,
                                   backfilled, no_upstream)


def _lineage_section(mart: Mart, parsed: ScriptParseResult, scripts: dict[tuple[str, str], _ScriptField],
                     listed: dict[tuple[str, str], list[FieldRef]], list_edges: list[LineageEdge],
                     added: list[LineageEdge], backfilled: int, no_upstream: Counter) -> ReportSection:
    """报告节「血缘合并」"""
    layers = {table.table_id: table.layer for table in mart.tables}
    added_fields = {(edge.target_table, edge.target_column) for edge in added}
    inconsistent = sorted(
        f"{key[0]}.{key[1]}" for key, sources in listed.items()
        if (script := scripts.get(key)) is not None and script.sources and not set(sources) & set(script.sources))
    topic = [(column.table_id, column.column_name) for column in mart.columns
             if layers.get(column.table_id) == TOPIC_LAYER]
    before = [key for key in topic if key not in listed]
    remaining = [key for key in before if key not in added_fields]
    scripted_tables = {statement.target_table for statement in parsed.statements}
    no_script = [key for key in remaining if key[0] not in scripted_tables]
    silent = [key for key in remaining if key[0] in scripted_tables and key in scripts]
    other = [f"{key[0]}.{key[1]}" for key in remaining if key[0] in scripted_tables and key not in scripts]

    lines = [
        f"- 清单血缘 {len(list_edges)} 条，其中直取 {sum(edge.is_passthrough for edge in list_edges)} 条；"
        f"加工逻辑为空、用脚本表达式回填 {backfilled} 条",
        f"- 脚本补血缘：字段 {len(added_fields)} 个，边 {len(added)} 条"
        f"（直取 {sum(edge.is_passthrough for edge in added)} 条，"
        f"来源是外部表的 {sum(edge.source_external for edge in added)} 条），来源标为 script",
        f"- 清单没有血缘、脚本里也没有上游的字段 {sum(no_upstream.values())} 个："
        + "、".join(f"{name} {no_upstream[kind]}" for kind, name in NO_UPSTREAM_NAMES.items()),
        f"- 清单与脚本不一致 {len(inconsistent)} 个（两边都有来源，但一个都对不上；以清单为准，只计数）"
        + (f"，例如 {_examples(inconsistent)}" if inconsistent else ""),
        f"- 专题层字段 {len(topic)} 个：合并前没有血缘 {len(before)} 个，脚本补上 {len(before) - len(remaining)} 个，"
        f"仍没有 {len(remaining)} 个（表没有脚本 {len(no_script)}、脚本里没有上游 {len(silent)}、其它 {len(other)}"
        + (f"，其它的例子 {_examples(other)}" if other else "") + "）",
    ]
    return ReportSection(SECTION_LINEAGE, tuple(lines))


def is_bare_reference(transform: str, source_column: str) -> bool:
    """判断清单里写的加工逻辑是不是对源字段的裸引用

    只认 x、t.x、s.t.x 三种写法，最后一段等于源字段名；不区分大小写，可以有首尾空白。
    中文说明、函数、运算、带引号的写法一律不算。

    Args:
        transform: 血缘清单里的加工逻辑原文
        source_column: 源字段名

    Returns:
        是裸引用时为 True
    """
    if not source_column.strip():
        return False
    match = _BARE_REFERENCE.fullmatch(transform.strip())
    return match is not None and match.group(1).lower() == source_column.strip().lower()
