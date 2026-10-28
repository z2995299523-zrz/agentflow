"""加工脚本解析与血缘合并的测试：语句切分与降级、目标字段按位置对应、临时表并入、过滤与关联、
血缘回填、脚本补边、直取判定、报告节"""
import logging
import re
from collections import Counter
from pathlib import Path

import pytest

from metadata.checks import WARNING
from metadata.importer import load_mart
from metadata.models import (
    ColumnMeta,
    FieldRef,
    JoinKey,
    LineageEdge,
    Mart,
    ScriptColumn,
    ScriptFilter,
    ScriptStatement,
    TableMeta,
)
from metadata.script_parser import (
    SCRIPTS_DIR_NAME,
    SECTION_LINEAGE,
    SECTION_SCRIPTS,
    ScriptParseResult,
    is_bare_reference,
    merge_lineage,
    parse_script_text,
    parse_scripts,
)
from metadata.store import MetadataStore

MOCK_MART = Path(__file__).resolve().parents[1] / "data" / "mock_mart"
SCRIPTS = MOCK_MART / SCRIPTS_DIR_NAME

DEP = "dwd.corp_dep_acct_d"
LOAN = "dwd.corp_loan_duebill_d"
CUST = "dim.corp_cust"
ORG_INFO = "ods.org_info"
DATE_PARAM = "CAST('${data_dt}' AS DATE)"          # 脚本里的 DATE '${data_dt}' 展开后的写法

# 合并血缘用的小资料：贴源层 ods.src 加工出专题层 ads.tgt；专题层 ads.bare、标准层 dwd.mid 没有加工脚本
SRC, TGT, BARE, MID = "ods.src", "ads.tgt", "ads.bare", "dwd.mid"
SMALL_FIELDS = {SRC: ["k", "v", "w"], TGT: ["k", "v", "n", "c", "t", "m", "u", "x"], BARE: ["k"], MID: ["k"]}
SMALL_LAYERS = {SRC: "贴源层", TGT: "专题层", BARE: "专题层", MID: "标准层"}


@pytest.fixture(scope="module")
def mock_mart() -> Mart:
    """模拟专题库的四张清单"""
    mart, issues = load_mart(MOCK_MART)
    assert mart is not None, issues
    return mart


@pytest.fixture(scope="module")
def catalog(mock_mart) -> MetadataStore:
    """模拟专题库充当元数据目录，手写的小脚本都引用模拟库的表"""
    return MetadataStore.from_mart(mock_mart)


@pytest.fixture(scope="module")
def parsed(mock_mart) -> ScriptParseResult:
    """模拟专题库七个加工脚本的解析结果"""
    return parse_scripts(SCRIPTS, mock_mart)


def ref(table: str, column: str) -> FieldRef:
    """字段引用的简写"""
    return FieldRef(table=table, column=column)


def parse(sql: str, catalog, script_name: str = "t.sql"):
    """解析一段手写的脚本"""
    return parse_script_text(sql, script_name, catalog)


def statement_of(statements: list[ScriptStatement], script_name: str) -> ScriptStatement:
    """取某个脚本唯一的一条语句"""
    found = [s for s in statements if s.script_name == script_name]
    assert len(found) == 1, found
    return found[0]


def columns_of(columns: list[ScriptColumn], table: str) -> dict[str, ScriptColumn]:
    """一张目标表的字段表达式：字段名 → 表达式，按脚本里的顺序"""
    return {c.target_column: c for c in columns if c.target_table == table}


def joins_summary(statement: ScriptStatement) -> list[tuple]:
    """关联的要点：(查询层, 关联类型, 被关联的表, 键对)"""
    return [(j.scope, j.join_type, j.table, j.keys) for j in statement.joins]


def small_mart(lineage: list[LineageEdge] | None = None) -> Mart:
    """合并血缘用的小资料，血缘清单由测试给出"""
    return Mart(
        tables=[TableMeta(table_id=t, layer=SMALL_LAYERS[t], schema_name=t.split(".")[0],
                          table_name=t.split(".")[1], table_cn=t, table_type="全量表", grain_columns=[])
                for t in SMALL_FIELDS],
        columns=[ColumnMeta(table_id=t, column_name=c, ordinal=i, column_cn=c)
                 for t, names in SMALL_FIELDS.items() for i, c in enumerate(names, 1)],
        lineage=lineage or [],
    )


def edge(target: str, source: str, transform: str = "") -> LineageEdge:
    """一条清单血缘，表和字段写成「模式.表.字段」"""
    target_table, target_column = target.rsplit(".", 1)
    source_table, source_column = source.rsplit(".", 1)
    return LineageEdge(target_table=target_table, target_column=target_column, source_table=source_table,
                       source_column=source_column, transform=transform, script_name="清单里的脚本名.sql")


def script_column(column: str, kind: str, sources: list[str], expression: str,
                  script_name: str = "load_tgt.sql") -> ScriptColumn:
    """ads.tgt 的一个字段在脚本里的表达式"""
    return ScriptColumn(script_name=script_name, stmt_index=1, target_table=TGT, target_column=column,
                        expression=expression, sources=[ref(*s.rsplit(".", 1)) for s in sources], kind=kind)


def parse_result(*columns: ScriptColumn) -> ScriptParseResult:
    """手工构造的脚本解析结果：每个脚本一条写 ads.tgt 的语句"""
    statements = {c.script_name: ScriptStatement(script_name=c.script_name, stmt_index=1, target_table=TGT,
                                                 sql_text="", parsed=True)
                  for c in columns}
    return ScriptParseResult(statements=list(statements.values()), columns=list(columns))


# ── 模拟库的七个脚本 ──────────────────────────────────────

def test_mock_scripts_yield_seven_inserts_and_fifty_six_columns(parsed):
    assert [(s.script_name, s.stmt_index, s.target_table, s.parsed) for s in parsed.statements] == [
        ("01_dim_org.sql", 2, "dim.org", True),
        ("02_dim_corp_cust.sql", 2, CUST, True),
        ("11_dwd_corp_dep_acct_d.sql", 2, DEP, True),
        ("12_dwd_corp_loan_duebill_d.sql", 2, LOAN, True),
        ("21_ads_corp_cust_dep_bal_d.sql", 2, "ads.corp_cust_dep_bal_d", True),
        ("22_ads_org_corp_dep_bal_d.sql", 2, "ads.org_corp_dep_bal_d", True),
        ("23_ads_corp_cust_loan_bal_d.sql", 4, "ads.corp_cust_loan_bal_d", True),   # 临时表的建表语句并进来，不单独成条
    ]
    assert len(parsed.columns) == 56
    assert Counter(c.kind for c in parsed.columns) == {"passthrough": 32, "derived": 24}
    assert parsed.issues == []
    assert parsed.metadata_gaps == set()


def test_target_columns_follow_the_insert_column_list(parsed):
    org = columns_of(parsed.columns, "dim.org")

    assert list(org) == ["org_no", "org_nm", "org_lvl", "lvl1_org_no", "lvl1_org_nm", "lvl2_org_no", "lvl2_org_nm"]
    assert (org["org_no"].kind, org["org_no"].sources, org["org_no"].expression) == (
        "passthrough", [ref(ORG_INFO, "org_no")], "ods.org_info.org_no")
    assert org["lvl1_org_no"].kind == "derived"
    assert org["lvl1_org_no"].sources == [ref(ORG_INFO, "org_lvl"), ref(ORG_INFO, "org_no")]
    assert {(c.script_name, c.stmt_index) for c in org.values()} == {("01_dim_org.sql", 2)}


def test_columns_map_by_position_not_by_alias(catalog):
    _, columns, issues, _ = parse("""
        INSERT INTO ads.corp_cust_loan_bal_d (cust_no, org_no, loan_bal_rmb)
        SELECT l.org_no AS cust_no, l.cust_no AS whatever, SUM(l.bal_rmb) AS x
        FROM dwd.corp_loan_duebill_d l
        GROUP BY l.org_no, l.cust_no;
    """, catalog)
    loan = columns_of(columns, "ads.corp_cust_loan_bal_d")

    assert issues == []
    assert list(loan) == ["cust_no", "org_no", "loan_bal_rmb"]
    assert loan["cust_no"].sources == [ref(LOAN, "org_no")]          # 第 1 列，不看它的别名
    assert loan["org_no"].sources == [ref(LOAN, "cust_no")]
    assert (loan["loan_bal_rmb"].kind, loan["loan_bal_rmb"].expression) == (
        "derived", "SUM(dwd.corp_loan_duebill_d.bal_rmb)")


def test_cte_chain_traced_to_entity_fields(parsed):
    dep = columns_of(parsed.columns, "ads.corp_cust_dep_bal_d")

    assert (dep["cust_no"].kind, dep["cust_no"].sources, dep["cust_no"].expression) == (
        "passthrough", [ref(DEP, "cust_no")], "dwd.corp_dep_acct_d.cust_no")
    assert dep["dep_bal_rmb"].kind == "derived"
    assert dep["dep_bal_rmb"].sources == [ref(DEP, "bal_rmb"), ref(DEP, "is_margin")]
    assert dep["dep_bal_rmb"].expression == (
        "SUM(CASE WHEN dwd.corp_dep_acct_d.is_margin = '0' THEN dwd.corp_dep_acct_d.bal_rmb ELSE 0 END)")
    assert dep["dep_bal_tot_rmb"].expression == "SUM(dwd.corp_dep_acct_d.bal_rmb)"


def test_temporary_table_merged_into_the_statement_that_uses_it(parsed):
    stmt = statement_of(parsed.statements, "23_ads_corp_cust_loan_bal_d.sql")
    loan = columns_of(parsed.columns, "ads.corp_cust_loan_bal_d")

    assert stmt.sql_text.startswith("CREATE TEMPORARY TABLE tmp_corp_loan AS")
    assert stmt.sql_text.index("CREATE TEMPORARY") < stmt.sql_text.index("INSERT INTO ads.corp_cust_loan_bal_d")
    assert "DROP TABLE" not in stmt.sql_text and "DELETE FROM" not in stmt.sql_text
    assert stmt.select_sql.startswith("WITH tmp_corp_loan AS (")
    assert "INSERT" not in stmt.select_sql
    assert (loan["cust_no"].kind, loan["cust_no"].sources) == ("passthrough", [ref(LOAN, "cust_no")])
    assert loan["npl_bal_rmb"].expression == (
        "SUM(CASE WHEN dwd.corp_loan_duebill_d.is_npl = '1' THEN dwd.corp_loan_duebill_d.bal_rmb ELSE 0 END)")
    assert joins_summary(stmt) == [
        ("tmp_corp_loan", "JOIN", CUST, [JoinKey(left=ref(LOAN, "cust_no"), right=ref(CUST, "cust_no"))])]
    assert stmt.filters == [ScriptFilter(scope="tmp_corp_loan", clause="WHERE",
                                         condition=f"dwd.corp_loan_duebill_d.data_dt = {DATE_PARAM}")]


def test_join_keys_between_ctes_on_the_same_field_are_dropped(parsed):
    stmt = statement_of(parsed.statements, "21_ads_corp_cust_dep_bal_d.sql")

    assert joins_summary(stmt) == [
        ("main", "JOIN", "ytd", []),                 # cur 和 ytd 的客户号、机构号来自同一个实体字段，没有键对
        ("main", "JOIN", "mtd", []),
        ("cur", "JOIN", CUST, [JoinKey(left=ref(DEP, "cust_no"), right=ref(CUST, "cust_no"))]),
    ]
    assert stmt.joins[2].condition == "dwd.corp_dep_acct_d.cust_no = dim.corp_cust.cust_no"


def test_composite_join_key(parsed):
    rate = statement_of(parsed.statements, "11_dwd_corp_dep_acct_d.sql").joins[1]

    assert (rate.scope, rate.join_type, rate.table) == ("main", "JOIN", "ods.exch_rate")
    assert rate.keys == [
        JoinKey(left=ref("ods.core_dep_acct", "ccy_cd"), right=ref("ods.exch_rate", "ccy_cd")),
        JoinKey(left=ref("ods.core_dep_acct", "data_dt"), right=ref("ods.exch_rate", "data_dt")),
    ]
    assert rate.condition == ("ods.core_dep_acct.ccy_cd = ods.exch_rate.ccy_cd "
                              "AND ods.core_dep_acct.data_dt = ods.exch_rate.data_dt")


def test_self_join_keeps_keys_on_different_fields(parsed):
    stmt = statement_of(parsed.statements, "01_dim_org.sql")
    up_to_org = JoinKey(left=ref(ORG_INFO, "up_org_no"), right=ref(ORG_INFO, "org_no"))

    assert joins_summary(stmt) == [("main", "LEFT", ORG_INFO, [up_to_org]), ("main", "LEFT", ORG_INFO, [up_to_org])]


def test_filters_recorded_per_scope_with_entity_names(parsed):
    dep_bal = statement_of(parsed.statements, "21_ads_corp_cust_dep_bal_d.sql")
    dep_acct = statement_of(parsed.statements, "11_dwd_corp_dep_acct_d.sql")
    year_start = f"CAST(DATE_TRUNC('YEAR', {DATE_PARAM}) AS DATE)"

    assert [(f.scope, f.clause) for f in dep_bal.filters] == [("cur", "WHERE"), ("ytd", "WHERE"), ("mtd", "WHERE")]
    assert dep_bal.filters[0].condition == f"dwd.corp_dep_acct_d.data_dt = {DATE_PARAM}"
    assert dep_bal.filters[1].condition == f"dwd.corp_dep_acct_d.data_dt BETWEEN {year_start} AND {DATE_PARAM}"
    assert dep_acct.filters == [                    # WHERE 按 AND 拆成三条
        ScriptFilter(scope="main", clause="WHERE", condition=f"ods.core_dep_acct.data_dt = {DATE_PARAM}"),
        ScriptFilter(scope="main", clause="WHERE", condition="ods.core_dep_acct.cust_type = '1'"),
        ScriptFilter(scope="main", clause="WHERE", condition="ods.core_dep_acct.acct_stat = '0'"),
    ]


def test_enrich_mart_stores_scripts_and_backfilled_lineage(mock_store):
    assert mock_store.counts()["加工语句"] == 7
    assert mock_store.script_column("ads.corp_cust_loan_bal_d", "duebill_cnt").expression == (
        "COUNT(dwd.corp_loan_duebill_d.duebill_no)")
    assert len(mock_store.lineage_edges()) == 70
    assert all(e.transform for e in mock_store.lineage_edges())


# ── 手写的小脚本：查询层、过滤、关联 ──────────────────────

def test_having_and_subquery_scopes(catalog):
    statements, _, issues, _ = parse("""
        INSERT INTO ads.org_corp_dep_bal_d (org_no, dep_bal_tot_rmb)
        SELECT x.org_no, SUM(x.bal_rmb)
        FROM (SELECT a.org_no, a.bal_rmb FROM dwd.corp_dep_acct_d a WHERE a.is_margin = '0') x
        WHERE x.org_no IN (SELECT o.org_no FROM dim.org o WHERE o.org_lvl = '2')
        GROUP BY x.org_no
        HAVING SUM(x.bal_rmb) > 0;
    """, catalog)

    assert issues == []
    assert [(f.scope, f.clause, f.condition) for f in statements[0].filters] == [
        ("main", "WHERE",
         "dwd.corp_dep_acct_d.org_no IN (SELECT o.org_no AS org_no FROM dim.org AS o WHERE o.org_lvl = '2')"),
        ("main", "HAVING", "SUM(dwd.corp_dep_acct_d.bal_rmb) > 0"),
        ("subquery1", "WHERE", "dwd.corp_dep_acct_d.is_margin = '0'"),     # 派生表
        ("subquery2", "WHERE", "dim.org.org_lvl = '2'"),                    # IN 子查询
    ]


def test_union_branches_are_named_after_their_query(catalog):
    statements, columns, _, _ = parse("""
        INSERT INTO ads.corp_cust_dep_bal_d (cust_no, dep_bal_tot_rmb)
        SELECT a.cust_no, a.bal_rmb FROM dwd.corp_dep_acct_d a WHERE a.is_margin = '0'
        UNION ALL
        SELECT l.cust_no, NULL FROM dwd.corp_loan_duebill_d l WHERE l.is_npl = '1';
    """, catalog)
    dep = columns_of(columns, "ads.corp_cust_dep_bal_d")

    assert [(f.scope, f.condition) for f in statements[0].filters] == [
        ("main#1", "dwd.corp_dep_acct_d.is_margin = '0'"), ("main#2", "dwd.corp_loan_duebill_d.is_npl = '1'")]
    assert (dep["cust_no"].kind, dep["cust_no"].sources) == ("passthrough", [ref(DEP, "cust_no"), ref(LOAN, "cust_no")])
    assert dep["dep_bal_tot_rmb"].kind == "derived"
    assert dep["cust_no"].expression == "UNION(dwd.corp_dep_acct_d.cust_no, dwd.corp_loan_duebill_d.cust_no)"


def test_columns_record_the_query_layers_they_pass_through(catalog):
    """scopes 的名字与 ScriptFilter.scope、ScriptJoin.scope 一致；只在 JOIN 里出现、没被追溯到的 CTE 不算"""
    statements, columns, issues, _ = parse("""
        INSERT INTO ads.corp_cust_dep_bal_d (cust_no, dep_bal_rmb, margin_bal_rmb, dep_bal_tot_rmb, org_no)
        WITH u AS (SELECT a.cust_no AS k, a.bal_rmb AS v FROM dwd.corp_dep_acct_d a WHERE a.is_margin = '0'
                   UNION ALL
                   SELECT l.cust_no, l.bal_rmb FROM dwd.corp_loan_duebill_d l WHERE l.is_npl = '1'),
             side AS (SELECT c.cust_no FROM dim.corp_cust c WHERE c.scale_cd = '1')
        SELECT u.k,
               x.tot,
               (SELECT MAX(o.org_lvl) FROM dim.org o WHERE o.org_no = u.k),
               1,
               u.v
        FROM u
        JOIN (SELECT a.cust_no, SUM(a.bal_rmb) AS tot FROM dwd.corp_dep_acct_d a GROUP BY a.cust_no) x
          ON x.cust_no = u.k
        JOIN side ON side.cust_no = u.k;
    """, catalog)
    dep = columns_of(columns, "ads.corp_cust_dep_bal_d")

    assert issues == []
    assert {"u#1", "u#2", "side"} <= {f.scope for f in statements[0].filters}      # 这些名字也是条件里的查询层名
    assert not any("side" in column.scopes for column in dep.values())
    assert dep["cust_no"].scopes == ["main", "u", "u#1", "u#2"]                    # 穿过 UNION 的两个分支，不含 side
    assert dep["org_no"].scopes == ["main", "u", "u#1", "u#2"]
    assert dep["dep_bal_rmb"].scopes == ["main", "subquery2"]                      # 派生表
    assert dep["margin_bal_rmb"].scopes == ["main", "u", "u#1", "u#2", "subquery1"]   # 标量子查询，它引用了 u.k
    assert dep["dep_bal_tot_rmb"].scopes == ["main"]                               # 常量不经过别的查询层


def test_column_that_cannot_be_traced_records_no_scopes(catalog):
    """追溯不到的字段不知道经过了哪些查询层：留空，血缘图据此退回这条语句的全部条件"""
    _, columns, issues, _ = parse("""
        INSERT INTO ads.corp_cust_dep_bal_d (cust_no, org_no)
        WITH c AS (SELECT a.cust_no FROM dwd.corp_dep_acct_d a WHERE a.bal_rmb > 0)
        SELECT c.cust_no, g.n FROM c JOIN generate_series(1, 3) AS g(n) ON TRUE;
    """, catalog)
    dep = columns_of(columns, "ads.corp_cust_dep_bal_d")

    assert issues == []
    assert (dep["cust_no"].kind, dep["cust_no"].scopes) == ("passthrough", ["main", "c"])
    assert (dep["org_no"].kind, dep["org_no"].scopes) == ("unresolved", [])


def test_mock_scripts_record_the_scopes_of_every_column(parsed):
    dep = columns_of(parsed.columns, "ads.corp_cust_dep_bal_d")

    assert dep["dep_bal_rmb"].scopes == ["main", "cur"]
    assert dep["ytd_avg_bal_rmb"].scopes == ["main", "ytd"]
    assert dep["mtd_avg_bal_rmb"].scopes == ["main", "mtd"]
    assert columns_of(parsed.columns, "ads.corp_cust_loan_bal_d")["loan_bal_rmb"].scopes == ["main", "tmp_corp_loan"]
    assert all(c.scopes[0] == "main" for c in parsed.columns)                       # 每个字段至少经过最外层


def test_where_equality_recorded_as_implicit_join_not_as_filter(catalog):
    statements, _, issues, _ = parse("""
        INSERT INTO ads.corp_cust_dep_bal_d (data_dt, cust_no, org_no)
        SELECT a.data_dt, a.cust_no, a.org_no
        FROM dwd.corp_dep_acct_d a, dim.corp_cust c
        WHERE a.cust_no = c.cust_no AND a.bal_rmb > 0;
    """, catalog)
    stmt = statements[0]

    assert issues == []
    assert joins_summary(stmt) == [
        ("main", "IMPLICIT", CUST, [JoinKey(left=ref(DEP, "cust_no"), right=ref(CUST, "cust_no"))])]
    assert stmt.joins[0].condition == "dwd.corp_dep_acct_d.cust_no = dim.corp_cust.cust_no"
    assert [f.condition for f in stmt.filters] == ["dwd.corp_dep_acct_d.bal_rmb > 0"]


def test_with_before_insert_moved_into_the_query(catalog):
    statements, columns, issues, _ = parse("""
        WITH b AS (SELECT a.org_no, SUM(a.bal_rmb) AS s FROM dwd.corp_dep_acct_d a GROUP BY a.org_no)
        INSERT INTO ads.org_corp_dep_bal_d (org_no, dep_bal_tot_rmb)
        SELECT b.org_no, b.s FROM b;
    """, catalog)
    org = columns_of(columns, "ads.org_corp_dep_bal_d")

    assert issues == []
    assert (org["org_no"].kind, org["org_no"].sources) == ("passthrough", [ref(DEP, "org_no")])
    assert org["dep_bal_tot_rmb"].expression == "SUM(dwd.corp_dep_acct_d.bal_rmb)"
    assert statements[0].select_sql.startswith("WITH b AS (")
    assert "INSERT" not in statements[0].select_sql


def test_group_by_output_alias_still_resolves(catalog):
    statements, columns, issues, _ = parse("""
        INSERT INTO ads.org_corp_dep_bal_d (org_no, dep_bal_tot_rmb)
        SELECT SUBSTR(a.org_no, 1, 4) AS br, SUM(a.bal_rmb) AS s
        FROM dwd.corp_dep_acct_d a
        GROUP BY br;
    """, catalog)

    assert issues == []
    assert columns_of(columns, "ads.org_corp_dep_bal_d")["org_no"].sources == [ref(DEP, "org_no")]
    # 自包含查询保留原来的别名，校验器看到的和脚本里写的一样
    assert re.search(r"GROUP BY\s+br", statements[0].select_sql)


def test_insert_values_gives_constants(catalog):
    _, columns, issues, _ = parse(
        "INSERT INTO ods.industry_dict (industry_cd, industry_nm) VALUES ('A', '农业'), ('B', '采矿业');", catalog)
    codes = columns_of(columns, "ods.industry_dict")

    assert issues == []
    assert {name: c.kind for name, c in codes.items()} == {"industry_cd": "constant", "industry_nm": "constant"}
    assert codes["industry_nm"].expression == "UNION('农业', '采矿业')"


def test_quoted_identifiers_are_folded_to_lower_case_like_the_metadata(catalog):
    sql = ('INSERT INTO "DIM"."ORG" ("ORG_NO", "ORG_NM") '
           'SELECT o."ORG_NO", r."NAME" FROM ods.org_info o LEFT JOIN ext.region r ON o."ORG_NO" = r."CODE";')

    statements, columns, issues, gaps = parse(sql, catalog)
    org = columns_of(columns, "dim.org")

    # 元数据里的名字都是小写，带引号的大写写法按小写处理，否则 "NAME" 和 name 会被当成两个字段
    assert issues == [] and gaps == set()
    assert statements[0].target_table == "dim.org"
    assert statements[0].sql_text == sql[:-1]                             # 原文不动
    assert org["org_no"].sources == [ref(ORG_INFO, "org_no")]
    assert org["org_nm"].sources == [ref("ext.region", "name")]
    assert statements[0].joins[0].keys == [JoinKey(left=ref(ORG_INFO, "org_no"), right=ref("ext.region", "code"))]


def test_metadata_gaps_are_collected(catalog):
    _, columns, issues, gaps = parse(
        "INSERT INTO dim.org (org_no, org_nm) SELECT o.org_no, o.ghost_nm FROM ods.org_info o;", catalog)

    assert gaps == {ref(ORG_INFO, "ghost_nm")}
    assert issues == []                              # 缺口写进报告节，不逐条报警告
    assert columns_of(columns, "dim.org")["org_nm"].sources == [ref(ORG_INFO, "ghost_nm")]


# ── 临时表 ────────────────────────────────────────────────

def test_temporary_table_chain_and_work_table_with_schema(catalog):
    sql = (
        "DROP TABLE IF EXISTS tmp_a;\n"
        "CREATE TEMP TABLE tmp_a AS SELECT l.cust_no, l.bal_rmb FROM dwd.corp_loan_duebill_d l WHERE l.bal_rmb > 0;\n"
        "CREATE TABLE work.tmp_b AS SELECT a.cust_no, SUM(a.bal_rmb) AS bal FROM tmp_a a GROUP BY a.cust_no;\n"
        "CREATE TEMPORARY TABLE tmp_unused AS SELECT o.org_no FROM ods.org_info o;\n"
        "INSERT INTO ads.corp_cust_loan_bal_d (cust_no, loan_bal_rmb) SELECT b.cust_no, b.bal FROM work.tmp_b b;\n"
    )
    statements, columns, issues, _ = parse(sql, catalog)
    insert = statements[1]
    loan = columns_of(columns, "ads.corp_cust_loan_bal_d")

    # 没被引用的临时表单独成条，目标表不在清单里报 W07；被引用的两张（含带模式名的工作表）都并进 INSERT
    assert [(s.stmt_index, s.target_table) for s in statements] == [(4, "tmp_unused"), (5, "ads.corp_cust_loan_bal_d")]
    assert [(i.rule, i.row_no) for i in issues] == [("W07", 4)]
    parts = insert.sql_text.split(";\n\n")
    assert [part.split(" AS ")[0] for part in parts[:2]] == ["CREATE TEMP TABLE tmp_a", "CREATE TABLE work.tmp_b"]
    assert parts[2].startswith("INSERT INTO ads.corp_cust_loan_bal_d")
    assert insert.select_sql.startswith("WITH tmp_a AS (")
    assert (loan["cust_no"].kind, loan["cust_no"].sources) == ("passthrough", [ref(LOAN, "cust_no")])
    assert loan["loan_bal_rmb"].expression == "SUM(dwd.corp_loan_duebill_d.bal_rmb)"
    assert insert.filters == [ScriptFilter(scope="tmp_a", clause="WHERE", condition="dwd.corp_loan_duebill_d.bal_rmb > 0")]


def test_temporary_table_with_column_list(catalog):
    _, columns, issues, _ = parse(
        "CREATE TEMP TABLE tmp_c (k, n) AS SELECT c.cust_no, c.cust_nm FROM ods.ecif_corp_cust c;\n"
        "INSERT INTO dim.corp_cust (cust_no, cust_nm) SELECT t.k, t.n FROM tmp_c t;\n", catalog)
    cust = columns_of(columns, CUST)

    assert issues == []
    assert cust["cust_no"].sources == [ref("ods.ecif_corp_cust", "cust_no")]   # 建表语句的列清单按位置改名
    assert cust["cust_nm"].expression == "ods.ecif_corp_cust.cust_nm"


def test_ctas_into_listed_table_is_a_statement(catalog):
    statements, columns, issues, _ = parse(
        "CREATE TABLE dim.org AS SELECT o.org_no, o.org_nm AS org_nm, o.up_org_no AS lvl1_org_no FROM ods.org_info o;",
        catalog)

    assert [(s.target_table, s.parsed) for s in statements] == [("dim.org", True)]
    assert issues == []
    assert [c.target_column for c in columns] == ["org_no", "org_nm", "lvl1_org_no"]    # 按查询的输出名


# ── 忽略、降级与警告 ──────────────────────────────────────

def test_truncate_delete_drop_commit_and_do_blocks_ignored_quietly(catalog, caplog):
    caplog.set_level(logging.WARNING, logger="sqlglot")
    sql = """
        -- 机构维表：全量刷新
        BEGIN;
        TRUNCATE TABLE dim.org;
        DELETE FROM dim.org WHERE org_no IS NULL;
        DO $guard$ BEGIN PERFORM 1; RAISE NOTICE 'a;b'; END $guard$;
        DROP TABLE IF EXISTS tmp_x;
        INSERT INTO dim.org (org_no, org_nm) SELECT o.org_no, o.org_nm FROM ods.org_info o;
        ANALYZE dim.org;
        COMMIT;
    """

    statements, columns, issues, gaps = parse(sql, catalog)

    assert [(s.stmt_index, s.target_table) for s in statements] == [(6, "dim.org")]
    assert len(columns) == 2 and issues == [] and gaps == set()
    assert not caplog.records


def test_unparsable_insert_kept_as_text_with_w06(catalog):
    sql = (
        "TRUNCATE TABLE dim.org;\n"
        "INSERT INTO dim.org (org_no) SELECT FROM WHERE (;\n"
        "UPDATE dim.org SET org_nm = WHERE;\n"
        "INSERT INTO dim.org (org_no, org_nm) SELECT o.org_no, o.org_nm FROM ods.org_info o;\n"
    )

    statements, columns, issues, _ = parse(sql, catalog, "broken.sql")

    assert [(s.stmt_index, s.target_table, s.parsed) for s in statements] == [(2, "dim.org", False), (4, "dim.org", True)]
    raw = statements[0]
    assert raw.sql_text == "INSERT INTO dim.org (org_no) SELECT FROM WHERE ("
    assert (raw.select_sql, raw.filters, raw.joins) == ("", [], [])
    assert [(i.level, i.rule, i.row_no) for i in issues] == [(WARNING, "W06", 2)]    # 解析不了的 UPDATE 不报
    assert "broken.sql" in issues[0].message and "第 2 条" in issues[0].message
    assert [c.target_column for c in columns] == ["org_no", "org_nm"]


def test_whole_file_failure_retried_statement_by_statement(catalog):
    sql = (
        "DO $body$\n"
        "BEGIN\n"
        "  PERFORM 1;\n"
        "END\n"
        "$body$;\n"
        "INSERT INTO dim.org (org_no, org_nm) SELECT o.org_no, 'a;b' FROM ods.org_info o;\n"
        "/* 注释里的分号; 不切 */\n"
        "INSERT INTO dim.org (org_no) SELECT FROM WHERE (;\n"
        "INSERT INTO dim.corp_cust (cust_no) SELECT c.cust_no FROM ods.ecif_corp_cust c;\n"
    )

    statements, columns, issues, _ = parse(sql, catalog)

    # $body$ 块、字符串、注释里的分号都不切，所以 DO 块和字符串都完好
    assert [(s.stmt_index, s.target_table, s.parsed) for s in statements] == [
        (2, "dim.org", True), (3, "dim.org", False), (4, CUST, True)]
    assert statements[1].sql_text.startswith("/* 注释里的分号; 不切 */")
    assert [(i.rule, i.row_no) for i in issues] == [("W06", 8)]
    assert columns_of(columns, "dim.org")["org_nm"].expression == "'a;b'"


def test_target_table_not_in_table_list_warns_w07(catalog):
    statements, columns, issues, _ = parse(
        "INSERT INTO ads.no_such (k1, k2) SELECT o.org_no, o.org_nm FROM ods.org_info o;", catalog, "x.sql")

    assert [(s.target_table, s.parsed) for s in statements] == [("ads.no_such", True)]
    assert [(i.level, i.rule, i.row_no) for i in issues] == [(WARNING, "W07", 1)]
    assert "x.sql" in issues[0].message and "ads.no_such" in issues[0].message
    assert [(c.target_column, c.kind) for c in columns] == [("k1", "passthrough"), ("k2", "passthrough")]


def test_insert_without_column_list_into_unlisted_table_only_warns_w07(catalog):
    statements, columns, issues, _ = parse("INSERT INTO ads.no_such SELECT * FROM ext.raw_src;", catalog)

    assert [(s.target_table, s.parsed) for s in statements] == [("ads.no_such", True)]
    assert [i.rule for i in issues] == ["W07"]        # 不知道目标表有哪些字段，无法对应，W07 已经说明
    assert columns == []


def test_insert_without_column_list_follows_field_list_order(catalog):
    _, columns, issues, _ = parse("""
        INSERT INTO dim.corp_cust
        SELECT c.cust_no, c.cust_nm, c.industry_cd, i.industry_nm, c.scale_cd, c.scale_cd
        FROM ods.ecif_corp_cust c LEFT JOIN ods.industry_dict i ON c.industry_cd = i.industry_cd;
        INSERT INTO dim.org SELECT o.org_no, o.org_nm FROM ods.org_info o;
    """, catalog)
    cust = columns_of(columns, CUST)

    assert issues == []
    assert list(cust) == ["cust_no", "cust_nm", "industry_cd", "industry_nm", "scale_cd", "scale_nm"]
    assert cust["scale_nm"].sources == [ref("ods.ecif_corp_cust", "scale_cd")]
    assert cust["industry_nm"].sources == [ref("ods.industry_dict", "industry_nm")]
    # 列比表少时按顺序对应前几个字段，其余字段取默认值，与 PostgreSQL 一致
    assert [c.target_column for c in columns if c.target_table == "dim.org"] == ["org_no", "org_nm"]


def test_column_count_mismatch_warns_w06_and_skips_columns(catalog):
    statements, columns, issues, _ = parse("""
        INSERT INTO dim.org (org_no, org_nm, org_lvl) SELECT o.org_no, o.org_nm FROM ods.org_info o WHERE o.org_lvl = '1';
        INSERT INTO dim.org SELECT o.org_no, o.org_nm, o.org_lvl, o.org_no, o.org_nm, o.org_no, o.org_nm, o.up_org_no
        FROM ods.org_info o;
    """, catalog)

    assert [(i.rule, i.row_no) for i in issues] == [("W06", 2), ("W06", 3)]
    assert all("对不上" in i.message for i in issues)
    assert columns == []
    assert [s.parsed for s in statements] == [True, True]          # 语句本身照常解析，过滤和关联还在
    assert statements[0].filters == [ScriptFilter(scope="main", clause="WHERE", condition="ods.org_info.org_lvl = '1'")]


def test_statement_that_cannot_be_scoped_is_kept_as_text(catalog):
    sql = ("WITH c AS (SELECT o.org_no FROM ods.org_info o)\n"
           "INSERT INTO dim.org (org_no) SELECT c.no_such FROM c;")

    statements, columns, issues, _ = parse(sql, catalog)

    assert [(s.target_table, s.parsed, s.sql_text) for s in statements] == [("dim.org", False, sql[:-1])]
    assert [i.rule for i in issues] == ["W06"] and columns == []


# ── 读取目录 ──────────────────────────────────────────────

def test_missing_directory_gives_empty_result(mock_mart, tmp_path):
    result = parse_scripts(tmp_path / "no_such_dir", mock_mart)

    assert (result.statements, result.columns, result.issues, result.metadata_gaps) == ([], [], [], set())
    assert result.section.lines == ()


def test_gbk_script_and_file_name_order(mock_mart, tmp_path):
    (tmp_path / "b_org.sql").write_bytes(
        "-- 机构维表：全量刷新\nINSERT INTO dim.org (org_no) SELECT o.org_no FROM ods.org_info o;\n".encode("gbk"))
    (tmp_path / "a_cust.sql").write_text(
        "INSERT INTO dim.corp_cust (cust_no) SELECT c.cust_no FROM ods.ecif_corp_cust c;\n", encoding="utf-8")
    (tmp_path / "说明.txt").write_text("不是脚本", encoding="utf-8")

    result = parse_scripts(tmp_path, mock_mart)

    assert [s.script_name for s in result.statements] == ["a_cust.sql", "b_org.sql"]
    assert result.statements[1].sql_text.startswith("-- 机构维表：全量刷新")
    assert result.issues == []


def test_unreadable_script_skipped_with_w06(mock_mart, tmp_path):
    (tmp_path / "bad.sql").write_bytes(bytes([0xFF, 0xFE, 0xFF]))
    (tmp_path / "good.sql").write_text(
        "INSERT INTO dim.org (org_no) SELECT o.org_no FROM ods.org_info o;", encoding="utf-8")

    result = parse_scripts(tmp_path, mock_mart)

    assert [s.script_name for s in result.statements] == ["good.sql"]
    assert [(i.rule, i.row_no) for i in result.issues] == [("W06", None)]
    assert "bad.sql" in result.issues[0].message
    assert "脚本文件 2 个" in result.section.lines[0]


def test_scripts_section_summarizes_the_parse(parsed):
    text = "\n".join(parsed.section.lines)

    assert parsed.section.title == SECTION_SCRIPTS
    assert "脚本文件 7 个" in text
    assert "结构化解析 7 条，只存原文 0 条" in text
    assert "目标表 7 张" in text
    assert "临时表 1 个" in text
    assert "目标字段 56 个：直取 32、加工 24、空值 0、常量 0、加工时间 0、追溯不到 0" in text
    assert "字段清单缺口 0 个" in text
    assert "无加工脚本的专题层表：无" in text


def test_scripts_section_lists_gaps_and_topic_tables_without_scripts(tmp_path):
    mart = small_mart()
    (tmp_path / "load_tgt.sql").write_text("INSERT INTO ads.tgt (k, v) SELECT s.k, s.ghost FROM ods.src s;",
                                           encoding="utf-8")

    result = parse_scripts(tmp_path, mart)
    _, lineage_section = merge_lineage(mart, result)
    no_script = [line for line in result.section.lines if "无加工脚本" in line]

    assert result.metadata_gaps == {ref(SRC, "ghost")}
    assert any("字段清单缺口 1 个" in line and "ods.src.ghost" in line for line in result.section.lines)
    assert no_script == ["- 无加工脚本的专题层表 1 张：ads.bare"]          # 标准层的 dwd.mid 不列，只占一行
    assert not any("无加工脚本" in line for line in lineage_section.lines)  # 整份报告里只出现这一次


# ── 血缘合并 ──────────────────────────────────────────────

def test_list_edges_kept_and_empty_transforms_backfilled_on_mock(mock_mart, parsed):
    expressions = {(c.target_table, c.target_column): c.expression for c in parsed.columns}

    edges, section = merge_lineage(mock_mart, parsed)
    text = "\n".join(section.lines)

    assert len(edges) == len(mock_mart.lineage) == 70              # 模拟库的字段都有清单血缘，不补边
    assert [(e.target_table, e.target_column, e.source_table, e.source_column, e.origin) for e in edges] == [
        (e.target_table, e.target_column, e.source_table, e.source_column, "list") for e in mock_mart.lineage]
    assert all(e.transform == expressions[(e.target_table, e.target_column)] for e in edges)
    assert sum(e.is_passthrough for e in edges) == 32
    assert section.title == SECTION_LINEAGE
    assert "清单血缘 70 条，其中直取 32 条；加工逻辑为空、用脚本表达式回填 70 条" in text
    assert "脚本补血缘：字段 0 个，边 0 条" in text
    assert "清单与脚本不一致 0 个" in text


def test_existing_transform_not_overwritten():
    mart = small_mart([edge("ads.tgt.k", "ods.src.k"), edge("ads.tgt.v", "ods.src.v", "取余额，折人民币")])
    parsed = parse_result(
        script_column("k", "passthrough", ["ods.src.k"], "ods.src.k"),
        script_column("v", "derived", ["ods.src.v", "ods.src.w"], "SUM(ods.src.v)"),
    )

    edges, section = merge_lineage(mart, parsed)

    assert [e.transform for e in edges] == ["ods.src.k", "取余额，折人民币"]
    assert [e.script_name for e in edges] == ["清单里的脚本名.sql"] * 2       # 清单边的其余内容原样保留
    assert any("回填 1 条" in line for line in section.lines)


def test_script_edges_only_for_fields_without_list_lineage():
    mart = small_mart([edge("ads.tgt.k", "ods.src.w")])
    parsed = parse_result(
        script_column("k", "passthrough", ["ods.src.k"], "ods.src.k"),            # 清单已有血缘：不补
        script_column("u", "passthrough", ["ext.raw.u"], "ext.raw.u"),            # 清单没有：补，来源是外部表
        script_column("v", "derived", ["ods.src.v", "ods.src.w"], "ods.src.v * ods.src.w"),
    )

    edges, section = merge_lineage(mart, parsed)
    added = [e for e in edges if e.origin == "script"]

    assert [(e.target_column, e.origin) for e in edges if e.target_column == "k"] == [("k", "list")]
    assert [(e.target_column, e.source_table, e.source_column) for e in added] == [
        ("u", "ext.raw", "u"), ("v", "ods.src", "v"), ("v", "ods.src", "w")]
    assert [(e.is_passthrough, e.source_external) for e in added] == [(True, True), (False, False), (False, False)]
    assert [e.transform for e in added] == ["ext.raw.u", "ods.src.v * ods.src.w", "ods.src.v * ods.src.w"]
    assert {e.script_name for e in added} == {"load_tgt.sql"}
    assert any("脚本补血缘：字段 2 个，边 3 条" in line for line in section.lines)


def test_fields_without_upstream_get_no_edges_but_are_counted():
    parsed = parse_result(
        script_column("n", "null", [], "NULL"),
        script_column("c", "constant", [], "'1'"),
        script_column("t", "etl_time", [], "CURRENT_TIMESTAMP"),
        script_column("m", "derived", [], "COUNT(*)"),
    )

    edges, section = merge_lineage(small_mart(), parsed)

    assert edges == []
    assert any("脚本里也没有上游的字段 4 个：空值 1、常量 1、加工时间 1、行计数之类的加工 1、追溯不到 0" in line
               for line in section.lines)


def test_passthrough_flag_follows_the_script_when_there_is_one():
    mart = small_mart([edge("ads.tgt.k", "ods.src.k"), edge("ads.tgt.v", "ods.src.v"), edge("ads.tgt.u", "ods.src.w")])
    parsed = parse_result(
        script_column("k", "passthrough", ["ods.src.k"], "ods.src.k"),        # 脚本直取，来源对得上
        script_column("v", "derived", ["ods.src.v"], "TRIM(ods.src.v) || ''"), # 同名单来源，但脚本里是加工
        script_column("u", "passthrough", ["ods.src.k"], "ods.src.k"),        # 脚本直取，但取的不是清单的来源
    )

    edges, _ = merge_lineage(mart, parsed)

    assert [e.is_passthrough for e in edges] == [True, False, False]


def test_passthrough_flag_without_script_uses_name_or_bare_reference():
    mart = small_mart([
        edge("ads.tgt.k", "ods.src.k"),                     # 同名、唯一来源
        edge("ads.tgt.v", "ods.src.v"),                     # 同名，但有两个来源
        edge("ads.tgt.v", "ods.src.w"),
        edge("ads.tgt.n", "ods.src.w", " S.W "),            # 不同名，加工逻辑是对源字段的裸引用
        edge("ads.tgt.c", "ods.src.w", "取 w 的值"),         # 中文说明
    ])

    edges, _ = merge_lineage(mart, ScriptParseResult())

    assert [e.is_passthrough for e in edges] == [True, False, False, True, False]
    assert [e.transform for e in edges] == ["", "", "", " S.W ", "取 w 的值"]      # 没有脚本，不回填


def test_section_counts_list_and_script_disagreements():
    mart = small_mart([edge("ads.tgt.k", "ods.src.w"), edge("ads.tgt.v", "ods.src.v"), edge("ads.tgt.v", "ods.src.w")])
    parsed = parse_result(
        script_column("k", "passthrough", ["ods.src.k"], "ods.src.k"),            # 一个都对不上
        script_column("v", "derived", ["ods.src.v"], "SUM(ods.src.v)"),            # 对上了一个，不算
    )

    edges, section = merge_lineage(mart, parsed)

    assert [e.origin for e in edges] == ["list"] * 3                             # 只计数，以清单为准
    assert any("清单与脚本不一致 1 个" in line and "ads.tgt.k" in line for line in section.lines)


def test_topic_layer_fields_still_without_lineage_by_reason():
    mart = small_mart([edge("ads.tgt.k", "ods.src.k")])
    parsed = parse_result(
        script_column("k", "passthrough", ["ods.src.k"], "ods.src.k"),
        script_column("v", "derived", ["ods.src.v"], "SUM(ods.src.v)"),
        script_column("n", "null", [], "NULL"),
        script_column("c", "constant", [], "'1'"),
    )

    _, section = merge_lineage(mart, parsed)

    # ads.tgt 8 个字段加 ads.bare 1 个；合并前只有 k 有血缘；脚本补上 v；
    # n、c 脚本里没有上游，t、m、u、x 脚本没写，ads.bare.k 所在的表没有脚本
    assert any("专题层字段 9 个：合并前没有血缘 8 个，脚本补上 1 个，仍没有 7 个"
               "（表没有脚本 1、脚本里没有上游 2、其它 4" in line for line in section.lines)


def test_statements_writing_the_same_column_are_combined_like_union():
    parsed = parse_result(
        script_column("u", "passthrough", ["ods.src.k"], "ods.src.k", script_name="load_a.sql"),
        script_column("u", "passthrough", ["ods.src.w"], "ods.src.w", script_name="load_b.sql"),
        script_column("v", "passthrough", ["ods.src.v"], "ods.src.v", script_name="load_a.sql"),
        script_column("v", "null", [], "NULL", script_name="load_b.sql"),
    )

    edges, _ = merge_lineage(small_mart(), parsed)

    assert [(e.target_column, e.source_column, e.script_name, e.is_passthrough, e.transform) for e in edges] == [
        ("u", "k", "load_a.sql", True, "UNION(ods.src.k, ods.src.w)"),
        ("u", "w", "load_b.sql", True, "UNION(ods.src.k, ods.src.w)"),
        ("v", "v", "load_a.sql", False, "UNION(ods.src.v, NULL)"),       # 一条直取、一条恒为空，合起来是加工
    ]


@pytest.mark.parametrize(("transform", "source_column", "expected"), [
    ("cust_no", "cust_no", True),
    ("a.cust_no", "cust_no", True),
    ("dwd.corp_dep_acct_d.cust_no", "cust_no", True),
    ("  A.CUST_NO \n", "cust_no", True),
    ("a.cust_no", "org_no", False),
    ("", "cust_no", False),
    ("取客户号", "cust_no", False),
    ("客户号", "客户号", False),
    ("TRIM(a.cust_no)", "cust_no", False),
    ("a.cust_no || 'x'", "cust_no", False),
    ("x.y.z.cust_no", "cust_no", False),
    ("a. cust_no", "cust_no", False),
    ("a.cust_no", "", False),
])
def test_is_bare_reference(transform, source_column, expected):
    assert is_bare_reference(transform, source_column) is expected
