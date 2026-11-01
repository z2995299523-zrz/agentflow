"""
SQL 方言转换
生成和校验用的是目标库方言（GaussDB/PostgreSQL），模拟库是 DuckDB，测试数据是 SQLite，执行前需要转换

SQLite 没有 DATE 类型，CAST('2026-09-30' AS DATE) 得到的是数字 2026，只靠 sqlglot 转出来能跑但结果是错的，
所以 to_sqlite 先改写语法树：日期转换换成 date()、datetime()，取年月、日期运算换成 strftime()、date() 的写法，
小数转换换成 REAL（否则 CAST(x AS NUMERIC) 还是整数，除法会丢小数）。
date_trunc、regexp、regexp_replace 由执行层注册成函数。
"""
import re
from collections.abc import Callable

import sqlglot
from sqlglot import exp
from sqlglot.dialects.sqlite import SQLite

DEFAULT_DIALECT = "postgres"
MOCK_DIALECT = "duckdb"
SQLITE_DIALECT = "sqlite"
# 终端着色用的 ANSI 转义序列（CSI）。sqlglot 的解析错误用它给出错位置加下划线
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DATE_TYPES = (exp.DataType.Type.DATE,)
_TIME_TYPES = (exp.DataType.Type.TIMESTAMP, exp.DataType.Type.TIMESTAMPTZ, exp.DataType.Type.TIMESTAMPNTZ,
               exp.DataType.Type.TIMESTAMPLTZ, exp.DataType.Type.DATETIME)
_REAL_TYPES = (exp.DataType.Type.DECIMAL, exp.DataType.Type.DOUBLE, exp.DataType.Type.FLOAT)
_EXTRACT_FORMATS = {"YEAR": "%Y", "MONTH": "%m", "DAY": "%d", "HOUR": "%H", "MINUTE": "%M", "SECOND": "%S",
                    "DOW": "%w", "DOY": "%j", "EPOCH": "%s"}
_TIME_UNITS = ("HOUR", "MINUTE", "SECOND")


class _SQLiteWithAliasColumns(SQLite):
    """SQLite 的 CTE 能写字段名列表 WITH t(a, b) AS ...，sqlglot 默认把它丢掉，后面用到 a、b 的地方就报错"""

    class Generator(SQLite.Generator):
        SUPPORTS_TABLE_ALIAS_COLUMNS = True


def transpile_statements(sql: str, source: str = DEFAULT_DIALECT,
                         target: str = MOCK_DIALECT) -> list[str]:
    """把一段 SQL 从源方言转成目标方言

    注释一律丢掉，不带进输出：sqlglot 输出注释时不转义正文，行注释里写 */ 和分号，
    就能让「按源方言只有一条语句」的 SQL 在目标库里变成好几条；注释本身也不属于语句，
    只有注释的片段（例如分号后面的一句说明）不算一条语句。

    Args:
        sql: 一条或多条用分号隔开的 SQL
        source: 源方言
        target: 目标方言

    Returns:
        逐条语句的列表，不含空语句，也不含只有注释的语句

    Raises:
        sqlglot.errors.ParseError: SQL 无法按源方言解析
    """
    return [statement for statement in sqlglot.transpile(sql, read=source, write=target, comments=False)
            if statement.strip()]


def _call(name: str, *arguments: exp.Expression) -> exp.Anonymous:
    return exp.Anonymous(this=name, expressions=list(arguments))


def _is_datetime(node: exp.Expression) -> bool:
    """这个表达式在 SQLite 里是一个日期文本：日期转换或当前日期"""
    return isinstance(node, (exp.CurrentDate, exp.CurrentTimestamp)) or (
        isinstance(node, exp.Cast) and node.to.this in _DATE_TYPES + _TIME_TYPES)


def _shift(base: exp.Expression, interval: exp.Interval, sign: str) -> exp.Expression:
    """日期加减 INTERVAL 换成 date(x, '+1 month')，时分秒用 datetime"""
    amount, unit = interval.this.name, str(interval.unit or "DAY").upper()
    matched = re.fullmatch(r"\s*(-?\d+)\s*([a-zA-Z]*)\s*", amount)
    if matched and matched.group(2):
        amount, unit = matched.group(1), matched.group(2).upper()
    unit = unit.rstrip("S")
    modifier = exp.Literal.string(f"{sign}{amount} {unit.lower()}")
    return _call("datetime" if unit in _TIME_UNITS else "date", base.copy(), modifier)


def _rewrite_arithmetic(node: exp.Expression) -> exp.Expression:
    """日期运算：加减 INTERVAL，两个日期相减得天数"""
    if isinstance(node, (exp.Add, exp.Sub)) and isinstance(node.expression, exp.Interval):
        return _shift(node.this, node.expression, "+" if isinstance(node, exp.Add) else "-")
    if isinstance(node, exp.Sub) and _is_datetime(node.this) and _is_datetime(node.expression):
        days = exp.Sub(this=_call("julianday", node.this.copy()), expression=_call("julianday", node.expression.copy()))
        return exp.Cast(this=days, to=exp.DataType.build("INTEGER"))
    return node


def _rewrite_extract(node: exp.Extract) -> exp.Expression:
    field = node.name.upper()
    if field == "QUARTER":
        month = exp.Cast(this=_call("strftime", exp.Literal.string("%m"), node.expression.copy()),
                         to=exp.DataType.build("INTEGER"))
        shifted = exp.Paren(this=exp.Add(this=month, expression=exp.Literal.number(2)))
        return exp.Paren(this=exp.Div(this=shifted, expression=exp.Literal.number(3), typed=True))    # typed：整数相除
    fmt = _EXTRACT_FORMATS.get(field)
    if fmt is None:
        return node
    return exp.Cast(this=_call("strftime", exp.Literal.string(fmt), node.expression.copy()),
                    to=exp.DataType.build("INTEGER"))


def _rewrite_function(node: exp.Expression) -> exp.Expression:
    """SQLite 没有或写法不同的类型转换和函数"""
    if isinstance(node, exp.Cast):
        if node.to.this in _DATE_TYPES:
            return _call("date", node.this.copy())
        if node.to.this in _TIME_TYPES:
            return _call("datetime", node.this.copy())
        if node.to.this in _REAL_TYPES:
            return exp.Cast(this=node.this.copy(), to=exp.DataType.build("REAL", dialect=SQLITE_DIALECT))
    elif isinstance(node, exp.Extract):
        return _rewrite_extract(node)
    elif isinstance(node, exp.TimestampTrunc):
        return _call("date_trunc", exp.Literal.string(str(node.unit or "day").lower()), node.this.copy())
    elif isinstance(node, exp.TimeToStr):
        return _call("strftime", node.args["format"].copy(), node.this.copy())
    elif isinstance(node, exp.StrToDate):
        return _call("date", node.this.copy())
    elif isinstance(node, exp.RegexpLike):
        return _call("regexp", node.expression.copy(), node.this.copy())
    elif isinstance(node, exp.RegexpReplace):
        return _call("regexp_replace", node.this.copy(), node.expression.copy(), node.args["replacement"].copy())
    elif isinstance(node, exp.Collate):
        return node.this.copy()
    elif isinstance(node, exp.Anonymous) and node.name.lower() == "btrim":
        return _call("trim", *[item.copy() for item in node.expressions])
    elif isinstance(node, exp.Distinct) and len(node.expressions) == 1 and isinstance(node.expressions[0], exp.Tuple):
        # COUNT(DISTINCT (a, b))：SQLite 的 DISTINCT 只收一个参数，拼成一个值
        fields = node.expressions[0].expressions
        joined = _call("printf", exp.Literal.string("|".join(["%s"] * len(fields))), *[item.copy() for item in fields])
        return exp.Distinct(expressions=[joined])
    elif isinstance(node, exp.GroupConcat) and isinstance(node.this, exp.Distinct):
        # GROUP_CONCAT(DISTINCT x, ',')：同样只收一个参数，分隔符默认就是逗号
        node.set("separator", None)
    return node


def _flatten_table(node: exp.Expression) -> exp.Expression:
    """模式.表 换成测试库里叫「模式.表」的表；原来没有别名时补上表名，t.col 这样的引用照样能用"""
    if isinstance(node, exp.Table) and node.db:
        node.set("alias", node.args.get("alias") or exp.TableAlias(this=exp.to_identifier(node.name)))
        node.set("this", exp.to_identifier(f"{node.db}.{node.name}", quoted=True))
        node.set("db", None)
        node.set("catalog", None)
    elif isinstance(node, exp.Column) and node.args.get("db"):
        node.set("db", None)
        node.set("catalog", None)
    return node


def _rewrite_from_leaves(tree: exp.Expression, rewrite: Callable[[exp.Expression], exp.Expression]) -> exp.Expression:
    """从叶子往上逐个节点改写。不用 sqlglot 的 transform：它换掉一个节点后不再进它的子节点，
    EXTRACT(YEAR FROM CAST(x AS DATE)) 里面的 CAST 会漏掉，语句照样能跑，结果是错的"""
    for node in reversed(list(tree.walk())):
        replacement = rewrite(node)
        if replacement is not node:
            if node is tree:
                tree = replacement
            else:
                node.replace(replacement)
    return tree


def to_sqlite(sql: str, source: str = DEFAULT_DIALECT) -> list[str]:
    """把一段 SQL 从源方言转成测试库（SQLite）能执行的写法

    注释一律丢掉，理由同 transpile_statements。

    Args:
        sql: 一条或多条用分号隔开的 SQL
        source: 源方言

    Returns:
        逐条语句的列表，不含空语句

    Raises:
        sqlglot.errors.ParseError: SQL 无法按源方言解析
        ValueError: 用了 SQLite 没有的 ROLLUP、CUBE、GROUPING SETS
    """
    statements = []
    for tree in sqlglot.parse(sql, read=source):
        if tree is None:
            continue
        if tree.find(exp.Rollup, exp.Cube, exp.GroupingSets):
            raise ValueError("测试库（SQLite）不支持 ROLLUP、CUBE、GROUPING SETS，换成普通分组再查")
        for rewrite in (_rewrite_arithmetic, _rewrite_function, _flatten_table):
            tree = _rewrite_from_leaves(tree, rewrite)
        statements.append(tree.sql(dialect=_SQLiteWithAliasColumns, comments=False))
    return statements


def strip_ansi(text: str) -> str:
    """去掉文本里的 ANSI 转义序列，错误信息进入结果和界面之前用"""
    return ANSI_ESCAPE.sub("", text)
