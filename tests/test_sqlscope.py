"""SQL 作用域解析的测试：别名指向、按查询层补表结构、字段追溯、表达式展开、等值关联键"""
import logging
from collections import Counter
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, build_scope

from metadata.importer import load_mart
from metadata.models import FieldRef, JoinKey
from metadata.sqlscope import (
    DIALECT,
    PSEUDO_COLUMNS,
    Resolution,
    ScopeResolver,
    SqlScopeError,
    conjuncts,
    is_table_function,
    parse_statements,
    physical_tables,
    qualify_with_catalog,
    quiet_sqlglot,
    rename_outputs,
    table_id,
    unwrap,
)
from metadata.store import MetadataStore, write_store

MOCK_MART = Path(__file__).parent.parent / "data" / "mock_mart"
SCRIPTS = MOCK_MART / "scripts"
DO_BLOCK = "DO $guard$ BEGIN PERFORM 1; END $guard$;"

DEP = "dwd.corp_dep_acct_d"
LOAN = "dwd.corp_loan_duebill_d"
CUST = "dim.corp_cust"


@pytest.fixture(scope="module")
def catalog(tmp_path_factory) -> MetadataStore:
    """模拟专题库的元数据库，用 MetadataStore 充当 Catalog"""
    mart, issues = load_mart(MOCK_MART)
    assert mart is not None, issues
    db_path = tmp_path_factory.mktemp("sqlscope") / "metadata.db"
    write_store(db_path, mart)
    return MetadataStore(db_path)


def ref(table: str, column: str) -> FieldRef:
    """字段引用的简写"""
    return FieldRef(table=table, column=column)


def passthrough(*refs: FieldRef) -> Resolution:
    """直取的追溯结果"""
    return Resolution("passthrough", frozenset(refs))


def analyze(sql: str, catalog, **options) -> tuple[ScopeResolver, Scope]:
    """解析一条查询并按元数据限定，返回解析器和根作用域"""
    qualified, _ = qualify_with_catalog(parse_statements(sql)[0], catalog)
    root = build_scope(qualified)
    return ScopeResolver(root, **options), root


def mock_insert(script_name: str, catalog, **options) -> tuple[ScopeResolver, Scope, list[str]]:
    """照解析器的做法处理一个模拟脚本

    取出 INSERT 的查询部分，按位置把投影改名成目标字段；同一文件里的临时表注入成 CTE。
    """
    statements = parse_statements((SCRIPTS / script_name).read_text(encoding="utf-8"))
    temporary = {table_id(s.this): s.expression for s in statements
                 if isinstance(s, exp.Create) and isinstance(s.expression, exp.Query)}
    insert = next(s for s in statements if isinstance(s, exp.Insert))
    targets = [column.name for column in insert.this.expressions]
    query = rename_outputs(insert.expression, targets)
    referenced = {table_id(t) for t in physical_tables(query)}
    for name, definition in temporary.items():
        if name in referenced:
            query = query.with_(name, as_=definition.copy())
    qualified, _ = qualify_with_catalog(query, catalog)
    root = build_scope(qualified)
    return ScopeResolver(root, **options), root, targets


def cte_chain(length: int) -> str:
    """一串首尾相接的 CTE：c1 取实体表，之后每一层只取上一层的同名字段"""
    ctes = ["c1 AS (SELECT a.cust_no AS k FROM dwd.corp_dep_acct_d a)"]
    ctes += [f"c{i} AS (SELECT c{i - 1}.k AS k FROM c{i - 1})" for i in range(2, length + 1)]
    return f"WITH {', '.join(ctes)} SELECT c{length}.k AS k FROM c{length}"


# ── 解析与小工具 ──────────────────────────────────────────

def test_parse_statements_ignores_trailing_semicolon_and_comments():
    assert len(parse_statements("SELECT 1;\n-- 末尾的说明\n")) == 1
    assert len(parse_statements("SELECT 1; /* 块注释 */")) == 1
    assert [type(s) for s in parse_statements("SELECT 1;; SELECT 2;")] == [exp.Select, exp.Select]


def test_parse_statements_keeps_script_order():
    text = (SCRIPTS / "11_dwd_corp_dep_acct_d.sql").read_text(encoding="utf-8")

    assert [type(s) for s in parse_statements(text)] == [exp.Delete, exp.Insert]


def test_parse_statements_raises_parse_error():
    with pytest.raises(ParseError):
        parse_statements("SELECT FROM WHERE (")


def test_quiet_sqlglot_silences_warnings_and_restores_level(caplog):
    caplog.set_level(logging.WARNING, logger="sqlglot")

    with quiet_sqlglot():
        sqlglot.parse(DO_BLOCK, read=DIALECT)
    assert not caplog.records
    assert logging.getLogger("sqlglot").level == logging.WARNING     # 出了上下文恢复原级别

    sqlglot.parse(DO_BLOCK, read=DIALECT)
    assert any("unsupported syntax" in record.getMessage() for record in caplog.records)


def test_parse_statements_is_quiet(caplog):
    caplog.set_level(logging.WARNING, logger="sqlglot")

    statements = parse_statements(DO_BLOCK)

    assert [type(s) for s in statements] == [exp.Command]
    assert not caplog.records


def test_table_id_with_and_without_schema():
    tree = parse_statements("SELECT 1 FROM DWD.Corp_Dep_Acct_D a JOIN tmp_x t ON TRUE")[0]

    assert sorted(table_id(t) for t in tree.find_all(exp.Table)) == ["dwd.corp_dep_acct_d", "tmp_x"]


def test_physical_tables_skip_cte_references():
    sql = """
        WITH org AS (SELECT o.org_no FROM ods.org_info o),
             cur AS (SELECT a.org_no FROM dwd.corp_dep_acct_d a
                     WHERE a.cust_no IN (SELECT c.cust_no FROM dim.corp_cust c))
        SELECT cur.org_no
        FROM cur
        JOIN org ON cur.org_no = org.org_no
        JOIN dim.org d ON d.org_no = org.org_no
    """
    tables = physical_tables(parse_statements(sql)[0])

    # 不带模式的 org、cur 是 CTE；带模式的 dim.org 即使与 CTE 同名也是实体表；子查询里的表也算
    assert sorted(table_id(t) for t in tables) == [
        "dim.corp_cust", "dim.org", "dwd.corp_dep_acct_d", "ods.org_info"]


def test_physical_tables_skip_table_functions():
    sql = "SELECT g.n FROM generate_series(1, 3) AS g(n) JOIN dim.org d ON TRUE"

    assert [table_id(t) for t in physical_tables(parse_statements(sql)[0])] == ["dim.org"]


def test_conjuncts_split_and_only():
    where = parse_statements(
        "SELECT 1 FROM t WHERE a = 1 AND (b = 2 AND c = 3) AND (d = 4 OR e = 5) "
        "AND NOT (f = 6 AND g = 7) AND CASE WHEN h = 8 AND i = 9 THEN 1 END = 1")[0].args["where"]
    expected = ["a = 1", "b = 2", "c = 3", "(d = 4 OR e = 5)", "NOT (f = 6 AND g = 7)",
                "CASE WHEN h = 8 AND i = 9 THEN 1 END = 1"]

    assert [part.sql() for part in conjuncts(where.this)] == expected
    assert [part.sql() for part in conjuncts(where)] == expected      # 也接受 WHERE 节点本身
    assert conjuncts(None) == []


def test_unwrap_strips_alias_paren_non_temporal_cast_and_trim():
    projections = parse_statements(
        "SELECT (CAST(TRIM(a.x) AS VARCHAR(20))) AS y, a.x::int, CAST(a.d AS DATE), "
        "to_char(a.d, 'yyyy'), COALESCE(a.x, '') FROM t a")[0].selects

    assert unwrap(projections[0]).sql() == "a.x"
    assert unwrap(projections[1]).sql() == "a.x"
    assert isinstance(unwrap(projections[2]), exp.Cast)        # 转成日期不剥
    assert unwrap(projections[3]) is projections[3]
    assert unwrap(projections[4]) is projections[4]


def test_rename_outputs_by_position():
    query = parse_statements(
        "SELECT a.x, a.y AS z FROM t a UNION ALL (SELECT b.p, b.q FROM s b)")[0]
    before = query.sql()

    renamed = rename_outputs(query, ["c1", "c2"])

    assert renamed.sql() == (
        "SELECT a.x AS c1, a.y AS c2 FROM t AS a UNION ALL (SELECT b.p AS c1, b.q AS c2 FROM s AS b)")
    assert query.sql() == before                               # 原树不变
    with pytest.raises(SqlScopeError, match="2"):
        rename_outputs(query, ["c1"])                          # 列数对不上
    with pytest.raises(SqlScopeError):
        rename_outputs(parse_statements("SELECT * FROM t")[0], ["c1"])


# ── 按元数据限定 ──────────────────────────────────────────

def test_qualify_qualifies_every_column(catalog):
    text = (SCRIPTS / "21_ads_corp_cust_dep_bal_d.sql").read_text(encoding="utf-8")
    insert = next(s for s in parse_statements(text) if isinstance(s, exp.Insert))

    qualified, gaps = qualify_with_catalog(insert.expression, catalog)

    assert gaps == set()
    assert all(column.table for column in qualified.find_all(exp.Column))


def test_qualify_patches_schema_and_returns_gaps(catalog):
    sql = ("SELECT a.cust_no, a.no_such_col, c.cust_nm FROM dwd.corp_dep_acct_d a "
           "JOIN dim.corp_cust c ON a.cust_no = c.cust_no")

    qualified, gaps = qualify_with_catalog(parse_statements(sql)[0], catalog)

    assert gaps == {ref(DEP, "no_such_col")}
    assert "a.no_such_col AS no_such_col" in qualified.sql(DIALECT)


def test_qualify_finds_gap_referenced_from_correlated_subquery(catalog):
    sql = ("SELECT a.cust_no FROM dwd.corp_dep_acct_d a WHERE EXISTS "
           "(SELECT 1 FROM dim.corp_cust c WHERE c.cust_no = a.cust_no AND a.ghost = '1')")

    _, gaps = qualify_with_catalog(parse_statements(sql)[0], catalog)

    assert gaps == {ref(DEP, "ghost")}


def test_qualify_unknown_cte_column_raises(catalog):
    sql = "WITH c AS (SELECT a.cust_no FROM dwd.corp_dep_acct_d a) SELECT c.no_such FROM c"

    with pytest.raises(SqlScopeError, match="no_such"):
        qualify_with_catalog(parse_statements(sql)[0], catalog)


def test_qualify_retries_without_schema(catalog):
    """USING 的字段两张表都没有：带结构限定会报错，去掉结构重试能成功"""
    sql = "SELECT a.cust_no FROM dwd.corp_dep_acct_d a JOIN dim.corp_cust c USING (ghost_key)"

    qualified, gaps = qualify_with_catalog(parse_statements(sql)[0], catalog)

    assert "a.ghost_key = c.ghost_key" in qualified.sql(DIALECT)
    assert gaps == set()


def test_qualify_uses_catalog_for_unqualified_columns(catalog):
    sql = ("SELECT cust_nm, bal_rmb FROM dwd.corp_dep_acct_d a "
           "JOIN dim.corp_cust c ON a.cust_no = c.cust_no")

    qualified, _ = qualify_with_catalog(parse_statements(sql)[0], catalog)

    assert [s.sql(DIALECT) for s in qualified.selects] == ["c.cust_nm AS cust_nm", "a.bal_rmb AS bal_rmb"]


def test_qualify_leaves_external_tables_alone(catalog):
    sql = "SELECT u.k, u.v FROM ext.raw_src u JOIN dim.org o ON u.k = o.org_no"

    qualified, gaps = qualify_with_catalog(parse_statements(sql)[0], catalog)

    assert gaps == set()
    assert qualified.sql(DIALECT).startswith("SELECT u.k AS k, u.v AS v FROM ext.raw_src AS u")


def test_qualify_does_not_modify_input(catalog):
    tree = parse_statements("SELECT A.CUST_NO FROM DWD.CORP_DEP_ACCT_D A")[0]
    before = tree.sql()

    qualified, _ = qualify_with_catalog(tree, catalog)

    assert tree.sql() == before
    assert qualified.sql(DIALECT) == "SELECT a.cust_no AS cust_no FROM dwd.corp_dep_acct_d AS a"


# ── 别名与输出列 ──────────────────────────────────────────

def test_aliases_point_to_tables_and_scopes(catalog):
    resolver, root, _ = mock_insert("21_ads_corp_cust_dep_bal_d.sql", catalog)

    cur = resolver.source_of(root, "cur")
    inner = resolver.source_of(cur, "a")

    assert isinstance(cur, Scope) and cur.is_cte
    assert isinstance(inner, exp.Table) and table_id(inner) == DEP
    assert resolver.source_of(root, "a") is None            # CTE 内部的别名在外层不可见
    assert resolver.source_of(root, "no_such") is None
    assert list(resolver.sources(root)) == ["cur", "ytd", "mtd"]


def test_cte_name_not_in_from_is_not_a_source(catalog):
    sql = "WITH c AS (SELECT a.cust_no FROM dwd.corp_dep_acct_d a) SELECT d.org_no FROM dim.org d"
    resolver, root = analyze(sql, catalog)

    assert resolver.source_of(root, "c") is None
    assert list(resolver.sources(root)) == ["d"]


def test_correlated_subquery_alias_found_in_outer_scope(catalog):
    sql = ("SELECT a.cust_no AS cust_no FROM dwd.corp_dep_acct_d a WHERE EXISTS "
           "(SELECT 1 FROM dim.corp_cust c WHERE c.cust_no = a.cust_no)")
    resolver, root = analyze(sql, catalog)
    sub = root.subquery_scopes[0]
    outer_column = next(c for c in sub.expression.find_all(exp.Column) if c.table == "a")

    assert table_id(resolver.source_of(sub, "a")) == DEP
    assert resolver.resolve_column(sub, outer_column) == passthrough(ref(DEP, "cust_no"))
    assert resolver.source_of(root, "c") is None             # 外层看不到子查询里的别名


def test_cte_and_derived_table_cannot_see_outer_aliases(catalog):
    """CTE 和普通派生表不是相关子查询，看不到主查询或同层兄弟的别名"""
    sql = """
        WITH c AS (SELECT o.org_no AS k FROM ods.org_info o)
        SELECT a.cust_no
        FROM dwd.corp_dep_acct_d a
        JOIN c ON c.k = a.org_no
        JOIN (SELECT d.cust_no FROM dim.corp_cust d) x ON x.cust_no = a.cust_no
    """
    resolver, root = analyze(sql, catalog)
    cte, derived = resolver.source_of(root, "c"), resolver.source_of(root, "x")

    assert table_id(resolver.source_of(cte, "o")) == "ods.org_info"
    assert resolver.source_of(cte, "a") is None
    assert resolver.source_of(derived, "a") is None


def test_output_names(catalog):
    plain, plain_root = analyze("SELECT a.cust_no, a.bal_rmb AS bal FROM dwd.corp_dep_acct_d a", catalog)
    union, union_root = analyze(
        "SELECT a.cust_no AS k, a.org_no AS o FROM dwd.corp_dep_acct_d a "
        "UNION ALL SELECT l.cust_no, l.org_no FROM dwd.corp_loan_duebill_d l", catalog)
    star, star_root = analyze("SELECT * FROM ext.raw_src", catalog)
    known_star, known_root = analyze("SELECT * FROM dim.org", catalog)
    listed, listed_root = analyze(
        "WITH c(x, y) AS (SELECT a.cust_no, a.org_no FROM dwd.corp_dep_acct_d a) SELECT c.x FROM c", catalog)

    assert plain.output_names(plain_root) == ["cust_no", "bal"]
    assert union.output_names(union_root) == ["k", "o"]             # UNION 取最左分支
    assert star.output_names(star_root) is None                     # 外部表的 * 展不开
    assert known_star.output_names(known_root) == [
        "org_no", "org_nm", "org_lvl", "lvl1_org_no", "lvl1_org_nm", "lvl2_org_no", "lvl2_org_nm"]
    assert listed.output_names(listed.source_of(listed_root, "c")) == ["x", "y"]


def test_unqualified_column_found_by_searching_sources(catalog):
    """没带别名的列按来源搜索：恰好一个来源有这个字段就算它的"""
    sql = ("SELECT cust_nm AS nm, cust_no AS k FROM dwd.corp_dep_acct_d a "
           "JOIN dim.corp_cust c ON a.cust_no = c.cust_no")
    root = build_scope(parse_statements(sql)[0])          # 不限定，保留没带别名的列
    resolver = ScopeResolver(root, catalog=catalog)

    assert resolver.owners(root, "cust_nm") == ["c"]
    assert resolver.owners(root, "cust_no") == ["a", "c"]
    assert resolver.owners(root, "ghost") == []
    assert resolver.resolve_output(root, "nm") == passthrough(ref(CUST, "cust_nm"))
    assert resolver.resolve_output(root, "k").kind == "unresolved"     # 两个来源都有：有歧义
    assert ScopeResolver(root).owners(root, "cust_nm") == []           # 没有目录时不知道实体表的字段


# ── 字段追溯 ──────────────────────────────────────────────

def test_direct_column_is_passthrough(catalog):
    resolver, root = analyze("SELECT a.acct_no AS acct_no FROM ods.core_dep_acct a", catalog)

    assert resolver.resolve_output(root, "acct_no") == passthrough(ref("ods.core_dep_acct", "acct_no"))


def test_cast_to_text_counts_as_passthrough_but_cast_to_date_does_not(catalog):
    sql = """
        SELECT CAST(a.acct_no AS VARCHAR(40)) AS v1, a.cust_no::text AS v2,
               CAST(a.acct_bal AS NUMERIC(20, 2)) AS v3, TRIM(a.org_no) AS v4,
               CAST(a.open_dt AS DATE) AS d1, to_char(a.open_dt, 'yyyymmdd') AS d2,
               COALESCE(a.cust_no, '') AS d3, a.acct_no || a.cust_no AS d4,
               SUBSTR(a.org_no, 1, 4) AS d5, a.acct_bal * 1 AS d6
        FROM ods.core_dep_acct a
    """
    resolver, root = analyze(sql, catalog)
    kinds = {name: resolver.resolve_output(root, name).kind for name in resolver.output_names(root)}

    assert kinds == {"v1": "passthrough", "v2": "passthrough", "v3": "passthrough", "v4": "passthrough",
                     "d1": "derived", "d2": "derived", "d3": "derived", "d4": "derived",
                     "d5": "derived", "d6": "derived"}
    assert resolver.resolve_output(root, "d1").sources == {ref("ods.core_dep_acct", "open_dt")}
    assert resolver.resolve_output(root, "d4").sources == {
        ref("ods.core_dep_acct", "acct_no"), ref("ods.core_dep_acct", "cust_no")}


def test_cte_chain_traced_to_entity_fields(catalog):
    resolver, root, _ = mock_insert("21_ads_corp_cust_dep_bal_d.sql", catalog)
    margin_and_balance = frozenset({ref(DEP, "bal_rmb"), ref(DEP, "is_margin")})

    assert resolver.resolve_output(root, "cust_no") == passthrough(ref(DEP, "cust_no"))
    assert resolver.resolve_output(root, "dep_bal_rmb") == Resolution("derived", margin_and_balance)
    assert resolver.resolve_output(root, "dep_bal_tot_rmb") == Resolution("derived", frozenset({ref(DEP, "bal_rmb")}))
    assert resolver.resolve_output(root, "ytd_avg_bal_rmb") == Resolution("derived", margin_and_balance)


def test_derived_table_is_traced(catalog):
    sql = """
        SELECT x.cust_no AS cust_no, x.tot AS tot
        FROM (SELECT a.cust_no, SUM(a.bal_rmb) AS tot FROM dwd.corp_dep_acct_d a GROUP BY a.cust_no) x
    """
    resolver, root = analyze(sql, catalog)

    assert resolver.resolve_output(root, "cust_no") == passthrough(ref(DEP, "cust_no"))
    assert resolver.resolve_output(root, "tot") == Resolution("derived", frozenset({ref(DEP, "bal_rmb")}))


def test_union_of_passthrough_branches_keeps_every_source(catalog):
    sql = """
        SELECT a.cust_no AS cust_no, a.bal_rmb AS bal FROM dwd.corp_dep_acct_d a
        UNION ALL
        SELECT l.cust_no, l.bal_rmb * 1 FROM dwd.corp_loan_duebill_d l
    """
    resolver, root = analyze(sql, catalog)

    assert resolver.resolve_output(root, "cust_no") == passthrough(ref(DEP, "cust_no"), ref(LOAN, "cust_no"))
    assert resolver.resolve_output(root, "bal") == Resolution(
        "derived", frozenset({ref(DEP, "bal_rmb"), ref(LOAN, "bal_rmb")}))


def test_union_merges_branch_kinds(catalog):
    sql = """
        SELECT NULL AS n1, NULL AS n2, '1' AS c1, now() AS t1, a.cust_no AS p1 FROM dwd.corp_dep_acct_d a
        UNION ALL
        SELECT NULL, '0', NULL, NULL, NULL FROM dwd.corp_loan_duebill_d l
    """
    resolver, root = analyze(sql, catalog)
    kinds = {name: resolver.resolve_output(root, name).kind for name in resolver.output_names(root)}

    assert kinds == {"n1": "null", "n2": "constant", "c1": "constant", "t1": "etl_time", "p1": "derived"}


def test_union_inside_cte_is_traced(catalog):
    sql = """
        WITH u AS (SELECT a.cust_no AS k FROM dwd.corp_dep_acct_d a
                   UNION ALL SELECT l.cust_no FROM dwd.corp_loan_duebill_d l)
        SELECT u.k AS k FROM u
    """
    resolver, root = analyze(sql, catalog)

    assert resolver.resolve_output(root, "k") == passthrough(ref(DEP, "cust_no"), ref(LOAN, "cust_no"))


def test_null_constant_and_etl_time(catalog):
    sql = """
        SELECT NULL AS n1, CAST(NULL AS VARCHAR(10)) AS n2, NULL::numeric AS n3,
               '1' AS c1, 0 AS c2, DATE '${data_dt}' AS c3, '${data_dt}' AS c4, current_user AS c5,
               now() AS t1, CURRENT_TIMESTAMP AS t2, CURRENT_DATE AS t3, sysdate AS t4,
               CAST(sysdate AS DATE) AS t5, to_char(clock_timestamp(), 'yyyymmdd') AS t6,
               localtimestamp AS t7
        FROM ods.core_dep_acct a
    """
    resolver, root = analyze(sql, catalog)
    kinds = {name: resolver.resolve_output(root, name).kind for name in resolver.output_names(root)}

    assert {"sysdate", "localtimestamp"} <= PSEUDO_COLUMNS
    assert kinds == {"n1": "null", "n2": "null", "n3": "null",
                     "c1": "constant", "c2": "constant", "c3": "constant", "c4": "constant", "c5": "constant",
                     "t1": "etl_time", "t2": "etl_time", "t3": "etl_time", "t4": "etl_time",
                     "t5": "etl_time", "t6": "etl_time", "t7": "etl_time"}
    assert all(not resolver.resolve_output(root, name).sources for name in kinds)


def test_expression_over_always_null_column_stays_null(catalog):
    """字段本身恒为空时，只经过 CAST、COALESCE、运算、SUM 之类的表达式仍恒为空；带了常量就是常量"""
    sql = """
        WITH u AS (SELECT a.cust_no AS k, NULL AS r FROM dwd.corp_dep_acct_d a
                   UNION ALL SELECT l.cust_no, NULL FROM dwd.corp_loan_duebill_d l)
        SELECT u.r AS p1,
               COALESCE(CAST(u.r AS VARCHAR(1)), CAST(NULL AS VARCHAR(1))) AS p2,
               SUM(u.r) AS p3,
               COALESCE(NULL, NULL) AS p4,
               COALESCE(u.r, '0') AS c1,
               u.r IS NULL AS c2
        FROM u
    """
    resolver, root = analyze(sql, catalog)
    kinds = {name: resolver.resolve_output(root, name).kind for name in resolver.output_names(root)}

    assert kinds == {"p1": "null", "p2": "null", "p3": "null", "p4": "null",
                     "c1": "constant", "c2": "constant"}


def test_aggregate_or_window_without_fields_is_derived(catalog):
    """COUNT(*) 这类不引用字段的聚合、窗口函数是由行计数得到的度量，算加工、来源为空；纯字面量才是常量"""
    aggregates, aggregate_root = analyze("""
        SELECT COUNT(*) AS d1, COUNT(1) AS d2, SUM(1) AS d3,
               '1' AS k1, CAST('1' AS VARCHAR(1)) AS k2, 0 AS k3
        FROM dwd.corp_dep_acct_d a
    """, catalog)
    windows, window_root = analyze("""
        SELECT a.cust_no AS cust_no, ROW_NUMBER() OVER () AS rn, RANK() OVER (ORDER BY a.bal_rmb) AS rk
        FROM dwd.corp_dep_acct_d a
    """, catalog)
    nested, nested_root = analyze("""
        WITH n AS (SELECT a.org_no AS org_no, COUNT(*) AS cnt FROM dwd.corp_dep_acct_d a GROUP BY a.org_no),
             f AS (SELECT '1' AS flag FROM dwd.corp_loan_duebill_d l)
        SELECT n.cnt AS cnt, COALESCE(n.cnt, 0) AS cnt0,
               (SELECT COUNT(f.flag) FROM f) AS flag_cnt,
               (SELECT COUNT(*) FROM dim.org o) AS org_cnt
        FROM n
    """, catalog)
    union, union_root = analyze(
        "SELECT COUNT(*) AS cnt FROM dwd.corp_dep_acct_d a UNION ALL SELECT NULL FROM dwd.corp_loan_duebill_d l",
        catalog)
    measure = Resolution("derived", frozenset())

    assert {name: aggregates.resolve_output(aggregate_root, name)
            for name in aggregates.output_names(aggregate_root)} == {
        "d1": measure, "d2": measure, "d3": measure,
        "k1": Resolution("constant"), "k2": Resolution("constant"), "k3": Resolution("constant")}
    assert windows.resolve_output(window_root, "rn") == measure
    assert windows.resolve_output(window_root, "rk") == Resolution("derived", frozenset({ref(DEP, "bal_rmb")}))
    # 经过 CTE、外面再包一层、在标量子查询里，都仍是加工
    assert {name: nested.resolve_output(nested_root, name) for name in nested.output_names(nested_root)} == {
        "cnt": measure, "cnt0": measure, "flag_cnt": measure, "org_cnt": measure}
    assert union.resolve_output(union_root, "cnt") == measure


def test_scalar_subquery_contributes_its_columns(catalog):
    sql = """
        SELECT a.cust_no AS cust_no,
               (SELECT MAX(c.cust_nm) FROM dim.corp_cust c WHERE c.cust_no = a.cust_no) AS cust_nm
        FROM dwd.corp_dep_acct_d a
    """
    resolver, root = analyze(sql, catalog)

    assert resolver.resolve_output(root, "cust_nm") == Resolution(
        "derived", frozenset({ref(CUST, "cust_nm"), ref(CUST, "cust_no"), ref(DEP, "cust_no")}))


def test_unknown_alias_or_output_is_unresolved(catalog):
    resolver, root = analyze("SELECT zz.x AS x, a.cust_no AS cust_no FROM dwd.corp_dep_acct_d a", catalog)

    assert resolver.resolve_output(root, "x").kind == "unresolved"
    assert resolver.resolve_output(root, "no_such_output").kind == "unresolved"


def test_star_over_external_table_resolves_by_name(catalog):
    resolver, root = analyze("WITH s AS (SELECT * FROM ext.raw_src) SELECT s.k AS k FROM s", catalog)

    assert resolver.output_names(resolver.source_of(root, "s")) is None
    assert resolver.resolve_output(root, "k") == passthrough(ref("ext.raw_src", "k"))
    assert resolver.render_output(root, "k") == "ext.raw_src.k"


GENERATE_DAYS = "generate_series(DATE '2026-09-01', DATE '2026-09-30', INTERVAL '1 day') AS d(dt)"


def test_table_function_columns_are_not_traced_to_an_entity_table(catalog):
    """表函数的字段由函数产生：不是实体表字段，来源追溯不到，展开时原样保留"""
    sql = f"""
        SELECT d.dt AS dt, d.dt + 1 AS next_dt, l.cust_no AS cust_no
        FROM {GENERATE_DAYS} JOIN {LOAN} l ON l.data_dt = d.dt
    """
    resolver, root = analyze(sql, catalog)

    assert is_table_function(resolver.source_of(root, "d")) and not is_table_function(resolver.source_of(root, "l"))
    assert resolver.resolve_output(root, "dt") == Resolution("unresolved")
    assert resolver.resolve_output(root, "next_dt") == Resolution("unresolved")
    assert resolver.resolve_output(root, "cust_no") == passthrough(ref(LOAN, "cust_no"))
    assert resolver.render_output(root, "dt") == "d.dt"


def test_star_over_table_function_is_not_an_entity_table(catalog):
    resolver, root = analyze("SELECT * FROM generate_series(1, 3) AS g(n)", catalog)

    assert resolver.resolve_output(root, "n") == Resolution("unresolved")
    assert resolver.render_output(root, "n") == "g.n"


def test_depth_limit(catalog):
    shallow, shallow_root = analyze(cte_chain(3), catalog, max_depth=3)
    deep, deep_root = analyze(cte_chain(4), catalog, max_depth=3)
    default, default_root = analyze(cte_chain(12), catalog)
    too_deep, too_deep_root = analyze(cte_chain(13), catalog)

    assert shallow.resolve_output(shallow_root, "k") == passthrough(ref(DEP, "cust_no"))
    assert deep.resolve_output(deep_root, "k").kind == "unresolved"
    assert default.resolve_output(default_root, "k") == passthrough(ref(DEP, "cust_no"))   # 默认上限 12 层
    assert too_deep.resolve_output(too_deep_root, "k").kind == "unresolved"


def test_depth_cut_is_not_memoized(catalog):
    """撞到深度上限得到的结果不缓存，从更近的作用域再追溯仍然成功"""
    resolver, root = analyze(cte_chain(4), catalog, max_depth=3)

    assert resolver.resolve_output(root, "k").kind == "unresolved"
    assert resolver.resolve_output(resolver.source_of(root, "c4"), "k") == passthrough(ref(DEP, "cust_no"))


def test_mock_scripts_classify_like_the_plan(catalog):
    """七个模拟脚本：56 个目标字段，32 个直取、24 个加工"""
    counts: Counter = Counter()
    for path in sorted(SCRIPTS.glob("*.sql")):
        resolver, root, targets = mock_insert(path.name, catalog)
        counts.update(resolver.resolve_output(root, name).kind for name in targets)

    assert counts == {"passthrough": 32, "derived": 24}


def test_temporary_table_injected_as_cte_is_traced(catalog):
    resolver, root, _ = mock_insert("23_ads_corp_cust_loan_bal_d.sql", catalog)

    assert resolver.resolve_output(root, "cust_no") == passthrough(ref(LOAN, "cust_no"))
    assert resolver.render_output(root, "npl_bal_rmb") == (
        "SUM(CASE WHEN dwd.corp_loan_duebill_d.is_npl = '1' THEN dwd.corp_loan_duebill_d.bal_rmb ELSE 0 END)")


# ── 展开表达式文本 ────────────────────────────────────────

def test_render_expands_cte_to_entity_tables(catalog):
    resolver, root, _ = mock_insert("21_ads_corp_cust_dep_bal_d.sql", catalog)
    expected = ("SUM(CASE WHEN dwd.corp_dep_acct_d.is_margin = '0' "
                "THEN dwd.corp_dep_acct_d.bal_rmb ELSE 0 END)")
    projection = next(s for s in root.expression.selects if s.alias_or_name == "dep_bal_rmb")

    assert resolver.render(root, projection) == expected
    assert resolver.render_output(root, "dep_bal_rmb") == expected
    assert resolver.render_output(root, "cust_no") == "dwd.corp_dep_acct_d.cust_no"
    assert resolver.render_output(root, "no_such_output") == ""


def test_render_parenthesizes_compound_expansions(catalog):
    sql = """
        WITH c AS (SELECT a.acct_bal * 2 AS x, a.acct_bal AS y, ROUND(a.acct_bal, 2) AS z FROM ods.core_dep_acct a)
        SELECT c.x + 1 AS v, c.y + 1 AS w, c.z + 1 AS u FROM c
    """
    resolver, root = analyze(sql, catalog)

    assert resolver.render_output(root, "v") == "(ods.core_dep_acct.acct_bal * 2) + 1"
    assert resolver.render_output(root, "w") == "ods.core_dep_acct.acct_bal + 1"
    assert resolver.render_output(root, "u") == "ROUND(ods.core_dep_acct.acct_bal, 2) + 1"


def test_render_union_lists_every_branch(catalog):
    in_cte, cte_root = analyze("""
        WITH u AS (SELECT a.cust_no AS k FROM dwd.corp_dep_acct_d a
                   UNION ALL SELECT l.cust_no FROM dwd.corp_loan_duebill_d l)
        SELECT u.k AS k FROM u
    """, catalog)
    at_root, union_root = analyze(
        "SELECT a.cust_no AS k FROM dwd.corp_dep_acct_d a UNION ALL SELECT TRIM(l.cust_no) FROM dwd.corp_loan_duebill_d l",
        catalog)

    assert in_cte.render_output(cte_root, "k") == (
        "UNION(dwd.corp_dep_acct_d.cust_no, dwd.corp_loan_duebill_d.cust_no)")
    assert at_root.render_output(union_root, "k") == (
        "UNION(dwd.corp_dep_acct_d.cust_no, TRIM(dwd.corp_loan_duebill_d.cust_no))")


def test_render_filter_expands_window_alias(catalog):
    sql = """
        SELECT x.cust_no AS cust_no
        FROM (SELECT a.cust_no, ROW_NUMBER() OVER (PARTITION BY a.cust_no ORDER BY a.bal_rmb DESC) AS rn
              FROM dwd.corp_dep_acct_d a) x
        WHERE x.rn = 1
    """
    resolver, root = analyze(sql, catalog)
    condition = root.expression.args["where"].this

    assert resolver.render(root, condition) == (
        "ROW_NUMBER() OVER (PARTITION BY dwd.corp_dep_acct_d.cust_no "
        "ORDER BY dwd.corp_dep_acct_d.bal_rmb DESC) = 1")


def test_render_truncates_long_text(catalog):
    resolver, root, _ = mock_insert("21_ads_corp_cust_dep_bal_d.sql", catalog, max_chars=40)

    text = resolver.render_output(root, "dep_bal_rmb")

    assert len(text) == 40
    assert text.startswith("SUM(CASE WHEN dwd.corp_dep_acct_d")
    assert text.endswith("…")


def test_render_stops_at_depth_eight(catalog):
    full, full_root = analyze(cte_chain(8), catalog)
    cut, cut_root = analyze(cte_chain(9), catalog)

    assert full.render_output(full_root, "k") == "dwd.corp_dep_acct_d.cust_no"
    assert cut.render_output(cut_root, "k") == "c1.k"               # 第 8 层之后原样保留


def test_render_leaves_nested_select_alone(catalog):
    sql = """
        SELECT a.cust_no AS k,
               (SELECT MAX(c.cust_nm) FROM dim.corp_cust c WHERE c.cust_no = a.cust_no) AS nm
        FROM dwd.corp_dep_acct_d a
    """
    resolver, root = analyze(sql, catalog)

    assert resolver.render_output(root, "nm") == (
        "(SELECT MAX(c.cust_nm) AS _col_0 FROM dim.corp_cust AS c WHERE c.cust_no = a.cust_no)")


# ── 等值关联键 ────────────────────────────────────────────

def test_joins_trace_keys_to_entity_tables(catalog):
    resolver, root, _ = mock_insert("11_dwd_corp_dep_acct_d.sql", catalog)

    joins = resolver.joins(root)

    assert [(j.join_type, j.alias) for j in joins] == [("JOIN", "s"), ("JOIN", "r")]
    assert joins[0].keys == [JoinKey(left=ref("ods.core_dep_acct", "subj_no"), right=ref("ods.gl_subject", "subj_no"))]
    assert joins[1].keys == [
        JoinKey(left=ref("ods.core_dep_acct", "ccy_cd"), right=ref("ods.exch_rate", "ccy_cd")),
        JoinKey(left=ref("ods.core_dep_acct", "data_dt"), right=ref("ods.exch_rate", "data_dt")),
    ]
    assert joins[1].condition.sql(DIALECT) == "a.ccy_cd = r.ccy_cd AND a.data_dt = r.data_dt"
    assert resolver.render(root, joins[1].condition) == (
        "ods.core_dep_acct.ccy_cd = ods.exch_rate.ccy_cd AND ods.core_dep_acct.data_dt = ods.exch_rate.data_dt")


def test_joins_through_cte_drop_pairs_on_the_same_field(catalog):
    resolver, root, _ = mock_insert("21_ads_corp_cust_dep_bal_d.sql", catalog)
    cur = resolver.source_of(root, "cur")

    # cur 和 ytd 的客户号、机构号都来自同一张表的同一个字段，这样的键对丢掉，关联本身保留
    assert [(j.join_type, j.alias, j.keys) for j in resolver.joins(root)] == [
        ("JOIN", "ytd", []), ("JOIN", "mtd", [])]
    assert [(j.join_type, j.alias, j.keys) for j in resolver.joins(cur)] == [
        ("JOIN", "c", [JoinKey(left=ref(DEP, "cust_no"), right=ref(CUST, "cust_no"))])]


def test_joins_split_on_and_but_not_or(catalog):
    sql = """
        SELECT a.cust_no FROM dwd.corp_dep_acct_d a
        LEFT JOIN dim.corp_cust c ON a.cust_no = c.cust_no AND c.scale_cd = '1'
        LEFT JOIN dim.org o ON a.org_no = o.org_no OR a.org_no = o.lvl1_org_no
    """
    resolver, root = analyze(sql, catalog)

    joins = resolver.joins(root)

    assert [(j.join_type, j.alias, j.keys) for j in joins] == [
        ("LEFT", "c", [JoinKey(left=ref(DEP, "cust_no"), right=ref(CUST, "cust_no"))]),
        ("LEFT", "o", []),
    ]


def test_joins_where_equality_is_implicit(catalog):
    sql = """
        SELECT a.cust_no FROM dwd.corp_dep_acct_d a, dim.corp_cust c, ods.exch_rate r
        WHERE c.cust_no = a.cust_no AND a.bal_rmb > 0
          AND a.ccy_cd = r.ccy_cd AND a.data_dt = r.data_dt AND a.org_no = a.org_no
    """
    resolver, root = analyze(sql, catalog)

    joins = resolver.joins(root)

    assert [(j.join_type, j.alias) for j in joins] == [("IMPLICIT", "c"), ("IMPLICIT", "r")]
    assert joins[0].keys == [JoinKey(left=ref(DEP, "cust_no"), right=ref(CUST, "cust_no"))]
    assert joins[1].keys == [                                       # 两个等值组成复合键
        JoinKey(left=ref(DEP, "ccy_cd"), right=ref("ods.exch_rate", "ccy_cd")),
        JoinKey(left=ref(DEP, "data_dt"), right=ref("ods.exch_rate", "data_dt")),
    ]
    assert joins[1].condition.sql(DIALECT) == "a.ccy_cd = r.ccy_cd AND a.data_dt = r.data_dt"


def test_joins_using(catalog):
    sql = "SELECT a.org_no FROM dwd.corp_dep_acct_d a JOIN dim.org o USING (org_no)"
    qualified, root = analyze(sql, catalog)
    raw_root = build_scope(parse_statements(sql)[0])                 # 不限定时 USING 原样保留
    raw = ScopeResolver(raw_root, catalog=catalog)
    expected = [JoinKey(left=ref(DEP, "org_no"), right=ref("dim.org", "org_no"))]

    assert [(j.alias, j.keys) for j in qualified.joins(root)] == [("o", expected)]
    assert [(j.alias, j.keys) for j in raw.joins(raw_root)] == [("o", expected)]


def test_joins_put_the_joined_alias_on_the_right(catalog):
    resolver, root = analyze(
        "SELECT a.cust_no FROM dwd.corp_dep_acct_d a JOIN dim.corp_cust c ON c.cust_no = a.cust_no", catalog)

    assert resolver.joins(root)[0].keys == [JoinKey(left=ref(DEP, "cust_no"), right=ref(CUST, "cust_no"))]


def test_joins_see_through_parentheses_and_casts(catalog):
    resolver, root = analyze(
        "SELECT a.cust_no FROM dwd.corp_dep_acct_d a "
        "JOIN dim.corp_cust c ON (CAST(c.cust_no AS VARCHAR(20)) = TRIM(a.cust_no))", catalog)

    assert resolver.joins(root)[0].keys == [JoinKey(left=ref(DEP, "cust_no"), right=ref(CUST, "cust_no"))]


def test_self_join_keeps_pairs_on_different_fields(catalog):
    """机构表自关联两次：上级机构号 = 机构号，两侧是同一张表的不同字段，保留"""
    resolver, root, _ = mock_insert("01_dim_org.sql", catalog)
    up_to_org = JoinKey(left=ref("ods.org_info", "up_org_no"), right=ref("ods.org_info", "org_no"))

    assert [(j.join_type, j.alias, j.keys) for j in resolver.joins(root)] == [
        ("LEFT", "p", [up_to_org]), ("LEFT", "g", [up_to_org])]


def test_joins_through_union_take_the_product_capped_at_four(catalog):
    sql = """
        WITH u AS (SELECT a.cust_no AS k FROM dwd.corp_dep_acct_d a
                   UNION ALL SELECT l.cust_no FROM dwd.corp_loan_duebill_d l
                   UNION ALL SELECT o.cust_no FROM ods.core_dep_acct o),
             v AS (SELECT c.cust_no AS k FROM dim.corp_cust c
                   UNION ALL SELECT e.cust_no FROM ods.ecif_corp_cust e)
        SELECT u.k FROM u JOIN v ON u.k = v.k JOIN dim.corp_cust d ON d.cust_no = v.k
    """
    resolver, root = analyze(sql, catalog)

    product, single = resolver.joins(root)

    assert product.keys == [                                         # 3×2=6 组，只留前 4 组
        JoinKey(left=ref(DEP, "cust_no"), right=ref(CUST, "cust_no")),
        JoinKey(left=ref(DEP, "cust_no"), right=ref("ods.ecif_corp_cust", "cust_no")),
        JoinKey(left=ref(LOAN, "cust_no"), right=ref(CUST, "cust_no")),
        JoinKey(left=ref(LOAN, "cust_no"), right=ref("ods.ecif_corp_cust", "cust_no")),
    ]
    assert single.keys == [                                          # 同一个实体字段的那一组被丢掉
        JoinKey(left=ref("ods.ecif_corp_cust", "cust_no"), right=ref(CUST, "cust_no"))]


def test_joins_in_correlated_subquery(catalog):
    sql = ("SELECT a.cust_no FROM dwd.corp_dep_acct_d a WHERE EXISTS "
           "(SELECT 1 FROM dim.corp_cust c WHERE c.cust_no = a.cust_no AND c.scale_cd = '1')")
    resolver, root = analyze(sql, catalog)
    sub = root.subquery_scopes[0]

    assert resolver.joins(root) == []
    assert [(j.join_type, j.alias, j.keys) for j in resolver.joins(sub)] == [
        ("IMPLICIT", "c", [JoinKey(left=ref(DEP, "cust_no"), right=ref(CUST, "cust_no"))])]


def test_joins_ignore_keys_on_table_function_columns(catalog):
    """一侧是表函数产生的值，不是实体表字段，不出键对"""
    resolver, root = analyze(f"SELECT l.cust_no FROM {GENERATE_DAYS} JOIN {LOAN} l ON l.data_dt = d.dt", catalog)

    (join,) = resolver.joins(root)

    assert join.alias == "l" and join.keys == []


def test_resolver_requires_a_scope():
    with pytest.raises(SqlScopeError):
        ScopeResolver(None)
