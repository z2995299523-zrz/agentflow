"""静态校验的测试：R1–R8 每条规则的正例与反例、报告里实际用到的表和字段、标准 SQL 与模拟脚本"""
import difflib
from pathlib import Path

import pytest
from sqlglot import exp

from eval.questions import load_questions
from metadata.checks import ERROR, WARNING
from metadata.importer import load_mart
from metadata.models import Mart, Relation
from metadata.sqlscope import parse_statements, physical_tables, table_id
from metadata.store import MetadataStore
from nl2sql.validator import SqlValidator, ValidationIssue, ValidationReport

MOCK_MART = Path(__file__).resolve().parents[1] / "data" / "mock_mart"
SCRIPTS = MOCK_MART / "scripts"
DAY = "DATE '2026-09-30'"

LOAN = "ads.corp_cust_loan_bal_d"
DEP = "ads.corp_cust_dep_bal_d"
ORG_BAL = "ads.org_corp_dep_bal_d"

# 测试 R6 用的关联：都是手工构造，不依赖关联推导的结果
KNOWN_RELATIONS = [
    Relation(left_table=ORG_BAL, left_columns=["org_no"], right_table="dim.org", right_columns=["org_no"],
             cardinality="N:1", origin="grain_key"),
    Relation(left_table=DEP, left_columns=["cust_no"], right_table="dim.corp_cust", right_columns=["cust_no"],
             cardinality="N:1", origin="lineage"),
    Relation(left_table=DEP, left_columns=["data_dt", "cust_no", "org_no"], right_table=LOAN,
             right_columns=["data_dt", "cust_no", "org_no"], cardinality="1:1", origin="manual"),
    Relation(left_table="ods.org_info", left_columns=["up_org_no"], right_table="ods.org_info",
             right_columns=["org_no"], cardinality="N:1", origin="script"),
    Relation(left_table="dwd.corp_dep_acct_d", left_columns=["ccy_cd", "data_dt"], right_table="ods.exch_rate",
             right_columns=["ccy_cd", "data_dt"], cardinality="N:1", origin="script"),
]


@pytest.fixture(scope="module")
def mock_mart() -> Mart:
    """只读四张清单的模拟专题库资料，测试按需改关联、改粒度"""
    mart, issues = load_mart(MOCK_MART)
    assert mart is not None, issues
    return mart


@pytest.fixture(scope="module")
def validator(mock_store) -> SqlValidator:
    """模拟专题库补全之后的校验器"""
    return SqlValidator(mock_store)


@pytest.fixture(scope="module")
def related(mock_mart) -> SqlValidator:
    """关联关系完全由 KNOWN_RELATIONS 给定的校验器"""
    return SqlValidator(MetadataStore.from_mart(mock_mart.model_copy(update={"relations": KNOWN_RELATIONS})))


@pytest.fixture(scope="module")
def unrelated(mock_mart) -> SqlValidator:
    """没有任何关联关系的校验器"""
    return SqlValidator(MetadataStore.from_mart(mock_mart.model_copy(update={"relations": []})))


def rules(report: ValidationReport) -> list[str]:
    """报告里出现过的规则编号，去重排序"""
    return sorted({issue.rule for issue in report.issues})


def of(report: ValidationReport, rule: str) -> list[str]:
    """某条规则的全部问题文本"""
    return [issue.message for issue in report.issues if issue.rule == rule]


def mock_selects() -> list[tuple[str, str]]:
    """模拟加工脚本里每条 INSERT 的查询部分；同一文件里的临时表注入成 CTE，照脚本解析器的做法"""
    result = []
    for path in sorted(SCRIPTS.glob("*.sql")):
        statements = parse_statements(path.read_text(encoding="utf-8"))
        temporary = {table_id(s.this): s.expression for s in statements
                     if isinstance(s, exp.Create) and isinstance(s.expression, exp.Query)}
        for insert in (s for s in statements if isinstance(s, exp.Insert)):
            query = insert.expression.copy()
            referenced = {table_id(t) for t in physical_tables(query)}
            for name, definition in temporary.items():
                if name in referenced:
                    query = query.with_(name, as_=definition.copy())
            result.append((path.name, query.sql(dialect="postgres")))
    return result


GOLD = [q for q in load_questions() if q.expect == "sql"]


# ── 报告与构造 ────────────────────────────────────────────

def test_report_properties():
    report = ValidationReport(issues=[ValidationIssue("R4", ERROR, "x"), ValidationIssue("R6", WARNING, "y")])

    assert [issue.rule for issue in report.errors] == ["R4"]
    assert [issue.rule for issue in report.warnings] == ["R6"]
    assert not report.ok
    assert ValidationReport(issues=[ValidationIssue("R6", WARNING, "y")]).ok


def test_only_postgres_dialect_is_supported(mock_store):
    with pytest.raises(ValueError, match="postgres"):
        SqlValidator(mock_store, dialect="mysql")


def test_indexes_are_built_at_construction(mock_mart):
    """表字段索引和关联索引在构造时建好，validate 不再回头读元数据的关联清单"""
    store = MetadataStore.from_mart(mock_mart.model_copy(update={"relations": KNOWN_RELATIONS}))
    validator = SqlValidator(store)
    store.relations = lambda: pytest.fail("validate 不应再读关联清单")

    report = validator.validate(
        f"SELECT o.lvl1_org_nm, SUM(b.dep_bal_rmb) FROM {ORG_BAL} b JOIN dim.org o ON b.org_no = o.org_no "
        f"WHERE b.data_dt = {DAY} GROUP BY o.lvl1_org_nm")

    assert report.issues == []


def test_odd_inputs_never_raise(validator):
    for sql in ["SELECT", "SELECT 1", "VALUES (1)", "TABLE dim.org", "SELECT * FROM generate_series(1, 3)",
                "SELECT (SELECT 1) AS x", "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT r.n + 1 FROM r) "
                "SELECT r.n FROM r", "SELECT o.org_no FROM dim.org o, LATERAL (SELECT o.org_nm AS nm) t"]:
        report = validator.validate(sql)
        assert not [issue for issue in report.issues if "检查时出错" in issue.message], sql


def test_rule_crash_becomes_a_warning(validator, monkeypatch):
    """规则代码自身出错时记一条该规则的警告，校验照常结束"""
    monkeypatch.setattr(SqlValidator, "_check_fan_out", lambda self, *args: 1 / 0)

    report = validator.validate(f"SELECT SUM(l.loan_bal_rmb) FROM {LOAN} l WHERE l.data_dt = {DAY}")

    assert [(issue.rule, issue.level) for issue in report.issues] == [("R7", WARNING)]
    assert "检查时出错" in report.issues[0].message and "ZeroDivisionError" in report.issues[0].message


# ── R1 解析 ───────────────────────────────────────────────

def test_r1_syntax_error_gives_position_without_ansi(validator):
    report = validator.validate("SELECT o.org_no\nFROM dim.org o\nWHERE o.org_no = = '1'")

    assert rules(report) == ["R1"]
    (message,) = of(report, "R1")
    assert "第 3 行" in message and "列" in message
    assert "\x1b" not in message
    assert report.tables == [] and report.columns == []


def test_r1_unclosed_quote(validator):
    report = validator.validate("SELECT o.org_no FROM dim.org o WHERE o.org_nm = 'abc")

    assert rules(report) == ["R1"]
    assert "第 1 行" in of(report, "R1")[0]


def test_r1_more_than_one_statement(validator):
    report = validator.validate("SELECT o.org_no FROM dim.org o; SELECT p.org_no FROM ods.org_info p")

    assert rules(report) == ["R1"]
    assert "2 条" in of(report, "R1")[0]


@pytest.mark.parametrize("sql", ["", "   ", "-- 只有注释", ";"])
def test_r1_no_statement(validator, sql):
    assert rules(validator.validate(sql)) == ["R1"]


def test_r1_trailing_semicolon_and_comment_still_one_statement(validator):
    report = validator.validate("SELECT o.org_no FROM dim.org o;\n-- 末尾的说明\n")

    assert report.issues == []
    assert report.tables == ["dim.org"]


# ── R2 只能是查询 ─────────────────────────────────────────

WRITES = {
    "insert": f"INSERT INTO {LOAN} SELECT * FROM {LOAN}",
    "update": f"UPDATE {LOAN} SET loan_bal_rmb = 0",
    "delete": f"DELETE FROM {LOAN}",
    "merge": ("MERGE INTO dim.org t USING dim.org s ON t.org_no = s.org_no "
              "WHEN MATCHED THEN UPDATE SET org_nm = s.org_nm"),
    "create_table_as": "CREATE TABLE tmp_org AS SELECT o.org_no FROM dim.org o",
    "drop": "DROP TABLE dim.org",
    "alter": "ALTER TABLE dim.org ADD COLUMN x INT",
    "truncate": "TRUNCATE TABLE dim.org",
    "copy": "COPY dim.org FROM '/tmp/org.csv'",
    "select_into": "SELECT o.org_no INTO tmp_org FROM dim.org o",
    "begin": "BEGIN",
    "commit": "COMMIT",
    "rollback": "ROLLBACK",
    "grant": "GRANT SELECT ON dim.org TO someone",
    "set": "SET search_path = ads",
    "use": "USE ads",
    "for_update": "SELECT o.org_no FROM dim.org o FOR UPDATE",
    "delete_in_cte": "WITH d AS (DELETE FROM dim.org RETURNING org_no) SELECT d.org_no FROM d",
    "do_block": "DO $$ BEGIN PERFORM 1; END $$",
}


@pytest.mark.parametrize("sql", WRITES.values(), ids=WRITES.keys())
def test_r2_rejects_anything_but_a_query(validator, sql):
    report = validator.validate(sql)

    assert rules(report) == ["R2"]                  # R2 不通过就不再往下查
    assert len(report.errors) == 1


@pytest.mark.parametrize("sql", [
    "SELECT o.org_no FROM dim.org o UNION SELECT p.org_no FROM ods.org_info p",
    "SELECT o.org_no FROM dim.org o EXCEPT SELECT p.org_no FROM ods.org_info p",
    "(SELECT o.org_no FROM dim.org o)",
    "SELECT o.org_no AS is_deleted, 'DELETE' AS op FROM dim.org o  -- DELETE FROM dim.org",
    'SELECT o.org_no AS "delete", o.org_nm AS drop_flag FROM dim.org o',
    "SELECT o.org_no FROM dim.org o\nWHERE o.org_nm <> 'DROP TABLE dim.org; COMMIT'",
], ids=["union", "except", "parenthesized", "keyword_in_alias_and_comment", "keyword_as_name",
        "keyword_in_string"])
def test_r2_accepts_queries_and_keywords_inside_names(validator, sql):
    assert validator.validate(sql).issues == []


def test_r2_column_named_like_a_keyword(mock_mart):
    """字段名本身带 delete、drop，按语法树判断不会误伤"""
    tables = mock_mart.tables + [mock_mart.tables[-1].model_copy(update={
        "table_id": "ads.flags", "table_name": "flags", "table_type": "维表", "grain_columns": ["org_no"],
        "date_column": None})]
    columns = mock_mart.columns + [
        mock_mart.columns[0].model_copy(update={"table_id": "ads.flags", "column_name": name, "ordinal": i})
        for i, name in enumerate(["org_no", "is_deleted", "drop_dt"], start=1)]
    store = MetadataStore.from_mart(mock_mart.model_copy(update={"tables": tables, "columns": columns}))
    validator = SqlValidator(store)

    report = validator.validate("SELECT f.is_deleted, f.drop_dt FROM ads.flags f WHERE f.is_deleted = '0'")

    assert report.issues == []
    assert report.columns == ["ads.flags.drop_dt", "ads.flags.is_deleted"]


# ── R3 表存在 ─────────────────────────────────────────────

def test_r3_unknown_table_lists_closest_names(validator):
    report = validator.validate(f"SELECT l.cust_no FROM ads.corp_cust_loan_bal l WHERE l.data_dt = {DAY}")

    (message,) = of(report, "R3")
    assert "ads.corp_cust_loan_bal" in message
    assert LOAN in message                               # 最接近的表
    assert message.count("、") <= 4                      # 最多 5 个候选
    assert report.errors[0].level == ERROR


@pytest.mark.parametrize("word", ["ads.corp_cust_loan_bal", "dwd.loan", "x.y", "ods.exch_rate_d"])
def test_r3_suggestions_are_the_five_most_similar(validator, mock_store, word):
    """候选按 difflib 的相似度取前 5，同分按名字排；剪枝之后与逐个计算的结果相同"""
    ids = [t.table_id for t in mock_store.tables()]
    expected = sorted(ids, key=lambda name: (-difflib.SequenceMatcher(None, name, word).ratio(), name))[:5]

    (message,) = of(validator.validate(f"SELECT t.k FROM {word} t"), "R3")

    assert message.endswith("最接近的表：" + "、".join(expected))


def test_r3_bare_table_name_gets_full_name(validator):
    report = validator.validate(f"SELECT l.cust_no FROM corp_cust_loan_bal_d l WHERE l.data_dt = {DAY}")

    (message,) = of(report, "R3")
    assert f"应写成 {LOAN}" in message


def test_r3_bare_name_unknown_everywhere(validator):
    (message,) = of(validator.validate("SELECT x.k FROM no_such_table x"), "R3")

    assert "没有带模式名" in message
    assert "最接近" in message


def test_r3_cte_name_is_not_a_table(validator):
    report = validator.validate(
        f"WITH loan AS (SELECT l.cust_no FROM {LOAN} l WHERE l.data_dt = {DAY}) SELECT loan.cust_no FROM loan")

    assert report.issues == []
    assert report.tables == [LOAN]


def test_r3_checks_tables_inside_subqueries_and_reports_each_once(validator):
    report = validator.validate(f"""
        SELECT l.cust_no FROM {LOAN} l
        WHERE l.data_dt = {DAY}
          AND l.cust_no IN (SELECT c.cust_no FROM dim.no_cust c)
          AND l.org_no IN (SELECT c.org_no FROM dim.no_cust c)
    """)

    assert len(of(report, "R3")) == 1
    assert report.tables == ["ads.corp_cust_loan_bal_d", "dim.no_cust"]


def test_r3_failure_skips_r4(validator):
    report = validator.validate(f"""
        SELECT l.ghost, x.anything, no_such_column
        FROM {LOAN} l JOIN ads.no_such x ON l.cust_no = x.cust_no
        WHERE l.data_dt = {DAY}
    """)

    assert "R3" in rules(report)
    assert of(report, "R4") == []


# ── R4 字段存在 ───────────────────────────────────────────

def test_r4_unknown_qualified_column_suggests_closest(validator):
    report = validator.validate(f"SELECT l.loan_bal FROM {LOAN} l WHERE l.data_dt = {DAY}")

    (message,) = of(report, "R4")
    assert LOAN in message and "loan_bal" in message
    assert "loan_bal_rmb" in message                    # 最接近的字段


def test_r4_unknown_column_points_to_the_alias_that_has_it(validator):
    report = validator.validate(f"""
        SELECT l.cust_nm FROM {LOAN} l JOIN dim.corp_cust c ON l.cust_no = c.cust_no WHERE l.data_dt = {DAY}
    """)

    (message,) = of(report, "R4")
    assert "c（dim.corp_cust）" in message


def test_r4_unknown_unqualified_column(validator):
    report = validator.validate(f"SELECT loan_bal FROM {LOAN} WHERE data_dt = {DAY}")

    (message,) = of(report, "R4")
    assert "loan_bal" in message and "loan_bal_rmb" in message


def test_r4_ambiguous_unqualified_column(validator):
    report = validator.validate(f"""
        SELECT cust_no FROM {LOAN} l JOIN dim.corp_cust c ON l.cust_no = c.cust_no WHERE l.data_dt = {DAY}
    """)

    (message,) = of(report, "R4")
    assert "歧义" in message and "l（" in message and "c（" in message


def test_r4_same_unknown_column_reported_once(validator):
    report = validator.validate(f"""
        SELECT l.ghost FROM {LOAN} l WHERE l.data_dt = {DAY} AND l.ghost > 0 ORDER BY l.ghost
    """)

    assert len(of(report, "R4")) == 1


def test_r4_undefined_alias(validator):
    report = validator.validate(f"SELECT zz.cust_no FROM {LOAN} l WHERE l.data_dt = {DAY}")

    (message,) = of(report, "R4")
    assert "别名 zz 没有定义" in message and "可用的别名：l" in message


def test_r4_output_alias_allowed_in_order_by_and_group_by(validator):
    report = validator.validate(f"""
        SELECT l.org_no AS org, SUM(l.loan_bal_rmb) AS bal
        FROM {LOAN} l WHERE l.data_dt = {DAY}
        GROUP BY org ORDER BY bal DESC
    """)

    assert of(report, "R4") == []


@pytest.mark.parametrize("clause, sql", [
    ("WHERE", f"SELECT l.loan_bal_rmb AS bal FROM {LOAN} l WHERE l.data_dt = {DAY} AND bal > 0"),
    ("HAVING", f"SELECT l.org_no, SUM(l.loan_bal_rmb) AS bal FROM {LOAN} l WHERE l.data_dt = {DAY} "
               "GROUP BY l.org_no HAVING bal > 0"),
], ids=["where", "having"])
def test_r4_output_alias_not_allowed_in_where_or_having(validator, clause, sql):
    """PostgreSQL 的 WHERE、HAVING 看不到 SELECT 的输出别名；sqlglot 限定时会悄悄展开，要单独查"""
    (message,) = of(validator.validate(sql), "R4")

    assert f"{clause} 子句里不能引用 SELECT 的输出别名 bal" in message


def test_r4_where_name_that_is_also_an_input_column_is_fine(validator):
    report = validator.validate(
        f"SELECT l.loan_bal_rmb AS org_no FROM {LOAN} l WHERE org_no = '1101' AND l.data_dt = {DAY}")

    assert of(report, "R4") == []


def test_r4_cte_output_columns(validator):
    hit = validator.validate(f"""
        WITH c AS (SELECT l.cust_no, SUM(l.loan_bal_rmb) AS bal FROM {LOAN} l
                   WHERE l.data_dt = {DAY} GROUP BY l.cust_no)
        SELECT c.cust_no, c.bal FROM c ORDER BY c.bal DESC
    """)
    miss = validator.validate(f"""
        WITH c AS (SELECT l.cust_no, SUM(l.loan_bal_rmb) AS bal FROM {LOAN} l
                   WHERE l.data_dt = {DAY} GROUP BY l.cust_no)
        SELECT c.cust_no, c.loan_bal_rmb FROM c JOIN {LOAN} x ON x.cust_no = c.cust_no
    """)

    assert hit.issues == []
    (message,) = of(miss, "R4")
    assert "CTE c 没有输出字段 loan_bal_rmb" in message and "最接近的输出字段：bal、cust_no" in message
    assert rules(miss) == ["R4"]                       # 作用域解析失败时跳过 R5–R7：主查询的 x 没带日期也不报


def test_r4_derived_table_columns(validator):
    star = validator.validate("SELECT x.org_nm FROM (SELECT * FROM dim.org) x")
    outer_star = validator.validate("SELECT x.* FROM (SELECT o.org_no FROM dim.org o) x")
    miss = validator.validate("SELECT x.org_nm FROM (SELECT o.org_no FROM dim.org o) x")

    assert star.issues == [] and outer_star.issues == []
    (message,) = of(miss, "R4")
    assert "派生表 x" in message and "org_nm" in message


def test_r4_correlated_subquery_sees_outer_alias(validator):
    report = validator.validate(f"""
        SELECT l.cust_no FROM {LOAN} l
        WHERE l.data_dt = {DAY}
          AND EXISTS (SELECT 1 FROM dim.corp_cust c WHERE c.cust_no = l.cust_no AND c.scale_nm = loan_bal_rmb)
    """)

    assert of(report, "R4") == []


def test_r4_pseudo_columns(validator):
    report = validator.validate(f"SELECT l.cust_no, sysdate AS etl_dt, current_user AS who FROM {LOAN} l "
                                f"WHERE l.data_dt = {DAY}")

    assert report.issues == []


def test_r4_duplicate_alias(validator):
    report = validator.validate(f"""
        SELECT a.cust_no FROM {LOAN} a JOIN dim.corp_cust a ON a.cust_no = a.cust_no WHERE a.data_dt = {DAY}
    """)

    (message,) = of(report, "R4")
    assert "a" in message and "别名" in message


def test_r4_join_on_must_not_use_output_alias(validator):
    report = validator.validate(f"""
        SELECT l.cust_no AS k FROM {LOAN} l JOIN dim.corp_cust c ON k = c.cust_no WHERE l.data_dt = {DAY}
    """)

    (message,) = of(report, "R4")
    assert "JOIN 的关联条件里不能引用 SELECT 的输出别名 k" in message


# ── R5 快照表带数据日期 ───────────────────────────────────

def test_r5_snapshot_without_date_condition(validator):
    report = validator.validate(f"SELECT SUM(l.loan_bal_rmb) FROM {LOAN} l")

    (message,) = of(report, "R5")
    assert LOAN in message and "主查询" in message
    assert "l.data_dt = DATE '…'" in message


def test_r5_example_uses_table_name_when_no_alias(validator):
    (message,) = of(validator.validate(f"SELECT SUM(loan_bal_rmb) FROM {LOAN}"), "R5")

    assert "corp_cust_loan_bal_d.data_dt = DATE '…'" in message


@pytest.mark.parametrize("condition", [
    f"l.data_dt = {DAY}",
    f"l.data_dt IN ({DAY}, DATE '2026-08-31')",
    f"l.data_dt BETWEEN DATE '2026-09-01' AND {DAY}",
    f"data_dt = {DAY}",
], ids=["equal", "in", "between", "unqualified"])
def test_r5_any_condition_on_the_date_counts(validator, condition):
    report = validator.validate(f"SELECT SUM(l.loan_bal_rmb) FROM {LOAN} l WHERE {condition}")

    assert of(report, "R5") == []


def test_r5_date_in_join_condition_counts(validator):
    report = validator.validate(f"""
        SELECT a.ccy_cd, SUM(a.acct_bal * r.mid_rate)
        FROM dwd.corp_dep_acct_d a
        JOIN ods.exch_rate r ON a.ccy_cd = r.ccy_cd AND a.data_dt = r.data_dt
        WHERE a.data_dt = {DAY}
        GROUP BY a.ccy_cd
    """)

    assert of(report, "R5") == []


def test_r5_date_in_another_scope_does_not_count(validator):
    report = validator.validate(f"""
        WITH c AS (SELECT b.org_no FROM {ORG_BAL} b WHERE b.data_dt = {DAY})
        SELECT SUM(x.dep_bal_rmb) FROM {ORG_BAL} x JOIN c ON x.org_no = c.org_no
    """)

    (message,) = of(report, "R5")
    assert "（别名 x）" in message and "主查询" in message


def test_r5_snapshot_in_subquery_needs_its_own_date(validator):
    report = validator.validate(f"""
        SELECT o.org_nm FROM dim.org o WHERE o.org_no IN (SELECT b.org_no FROM {ORG_BAL} b)
    """)

    (message,) = of(report, "R5")
    assert "子查询" in message and "b.data_dt" in message


def test_r5_not_required_for_other_table_types(validator):
    assert validator.validate("SELECT o.org_nm, c.cust_nm FROM dim.org o, dim.corp_cust c").issues == []


# ── R6 已知关联 ───────────────────────────────────────────

@pytest.mark.parametrize("sql", [
    f"SELECT o.lvl1_org_nm FROM {ORG_BAL} b JOIN dim.org o ON b.org_no = o.org_no WHERE b.data_dt = {DAY}",
    f"SELECT o.lvl1_org_nm FROM {ORG_BAL} b JOIN dim.org o ON o.org_no = b.org_no WHERE b.data_dt = {DAY}",
    f"SELECT o.lvl1_org_nm FROM dim.org o JOIN {ORG_BAL} b ON b.org_no = o.org_no WHERE b.data_dt = {DAY}",
    f"SELECT o.lvl1_org_nm FROM {ORG_BAL} b, dim.org o WHERE b.org_no = o.org_no AND b.data_dt = {DAY}",
    f"SELECT o.lvl1_org_nm FROM {ORG_BAL} b JOIN dim.org o USING (org_no) WHERE b.data_dt = {DAY}",
    "SELECT o.org_nm, p.org_nm FROM ods.org_info o LEFT JOIN ods.org_info p ON p.org_no = o.up_org_no",
], ids=["on", "on_reversed", "from_swapped", "implicit", "using", "self_join"])
def test_r6_known_relation_in_either_direction(related, sql):
    assert of(related.validate(sql), "R6") == []


def test_r6_unknown_mapping_lists_known_ones(related):
    report = related.validate(
        f"SELECT o.org_nm FROM {ORG_BAL} b JOIN dim.org o ON b.org_no = o.lvl1_org_no WHERE b.data_dt = {DAY}")

    (issue,) = [issue for issue in report.issues if issue.rule == "R6"]
    assert issue.level == WARNING
    assert f"{ORG_BAL}.org_no = dim.org.lvl1_org_no" in issue.message
    assert f"{ORG_BAL}.org_no = dim.org.org_no（N:1）" in issue.message


def test_r6_pair_without_any_relation(related):
    (message,) = of(related.validate(
        f"SELECT o.org_nm FROM {LOAN} l JOIN dim.org o ON l.org_no = o.org_no WHERE l.data_dt = {DAY}"), "R6")

    assert "没有已知关联" in message


def test_r6_keys_traced_through_cte(related):
    sql = f"""
        WITH b AS (SELECT x.org_no, SUM(x.dep_bal_rmb) AS bal FROM {ORG_BAL} x
                   WHERE x.data_dt = {DAY} GROUP BY x.org_no)
        SELECT o.lvl1_org_nm, SUM(b.bal) FROM b JOIN dim.org o ON b.org_no = o.org_no GROUP BY o.lvl1_org_nm
    """

    assert of(related.validate(sql), "R6") == []
    assert len(of(related.validate(sql.replace("o.org_no", "o.lvl2_org_no")), "R6")) == 1


def test_r6_part_of_a_composite_relation_passes(related):
    report = related.validate(f"""
        SELECT b.cust_no, l.loan_bal_rmb FROM {DEP} b JOIN {LOAN} l ON b.cust_no = l.cust_no
        WHERE b.data_dt = {DAY} AND l.data_dt = {DAY}
    """)

    assert of(report, "R6") == []


def test_r6_composite_key_pairs_are_checked_one_by_one(related):
    report = related.validate(f"""
        SELECT a.acct_no FROM dwd.corp_dep_acct_d a
        JOIN ods.exch_rate r ON a.ccy_cd = r.ccy_cd AND a.data_dt = r.data_dt AND a.subj_no = r.ccy_cd
        WHERE a.data_dt = {DAY}
    """)

    (message,) = of(report, "R6")
    assert "dwd.corp_dep_acct_d.subj_no = ods.exch_rate.ccy_cd" in message


def test_r6_without_relations_every_join_warns(unrelated):
    report = unrelated.validate(
        f"SELECT o.lvl1_org_nm FROM {ORG_BAL} b JOIN dim.org o ON b.org_no = o.org_no WHERE b.data_dt = {DAY}")

    assert len(of(report, "R6")) == 1
    assert report.ok                                                # 警告不影响通过


# ── R7 汇总时关联不发散 ───────────────────────────────────

RATE_JOIN = f"""
    SELECT r.mid_rate, SUM(a.bal_rmb) AS bal
    FROM dwd.corp_dep_acct_d a JOIN ods.exch_rate r ON a.ccy_cd = r.ccy_cd
    WHERE a.data_dt = {DAY} AND {{rate_date}}
    GROUP BY r.mid_rate
"""


def test_r7_dimension_joined_by_full_grain(validator):
    report = validator.validate(f"""
        SELECT o.lvl1_org_nm, SUM(b.dep_bal_rmb) FROM {ORG_BAL} b JOIN dim.org o ON b.org_no = o.org_no
        WHERE b.data_dt = {DAY} GROUP BY o.lvl1_org_nm
    """)

    assert of(report, "R7") == []


def test_r7_grain_column_not_constrained(validator):
    report = validator.validate(RATE_JOIN.format(rate_date="r.data_dt >= DATE '2026-09-01'"))

    (issue,) = [issue for issue in report.issues if issue.rule == "R7"]
    assert issue.level == WARNING
    assert "ods.exch_rate" in issue.message and "data_dt" in issue.message
    assert "ccy_cd" not in issue.message.split("其中")[1]           # ccy_cd 已经由 ON 约束


@pytest.mark.parametrize("rate_date", [
    f"r.data_dt = {DAY}",
    f"{DAY} = r.data_dt",
    f"r.data_dt = a.data_dt",
    f"r.data_dt IN ({DAY})",
    "r.data_dt = CAST(now() AS DATE)",
], ids=["literal", "literal_on_left", "other_alias", "single_in", "function"])
def test_r7_equality_constraints_count(validator, rate_date):
    assert of(validator.validate(RATE_JOIN.format(rate_date=rate_date)), "R7") == []


def test_r7_multi_value_in_does_not_count(validator):
    report = validator.validate(RATE_JOIN.format(rate_date=f"r.data_dt IN ({DAY}, DATE '2026-08-31')"))

    assert len(of(report, "R7")) == 1


@pytest.mark.parametrize("measure", [
    "COUNT(DISTINCT a.cust_no)", "MIN(a.bal_rmb)", "MAX(a.bal_rmb)",
    "SUM(a.bal_rmb) OVER (PARTITION BY a.cust_no)",
], ids=["count_distinct", "min", "max", "window_sum"])
def test_r7_skips_scopes_without_row_sensitive_aggregates(validator, measure):
    report = validator.validate(f"""
        SELECT a.cust_no, {measure} FROM dwd.corp_dep_acct_d a JOIN ods.exch_rate r ON a.ccy_cd = r.ccy_cd
        WHERE a.data_dt = {DAY} AND r.data_dt >= DATE '2026-09-01'
        GROUP BY a.cust_no, a.bal_rmb
    """)

    assert of(report, "R7") == []


def test_r7_count_star_sums_the_first_table(validator):
    report = validator.validate(f"""
        SELECT COUNT(*) FROM dwd.corp_dep_acct_d a JOIN ods.exch_rate r ON a.ccy_cd = r.ccy_cd
        WHERE a.data_dt = {DAY} AND r.data_dt >= DATE '2026-09-01'
    """)

    (message,) = of(report, "R7")
    assert "ods.exch_rate" in message                               # a 是被汇总的表，只查 r


def test_r7_tables_inside_aggregates_are_the_summed_ones(validator):
    report = validator.validate(f"""
        SELECT SUM(a.acct_bal * r.mid_rate) FROM dwd.corp_dep_acct_d a JOIN ods.exch_rate r ON a.ccy_cd = r.ccy_cd
        WHERE a.data_dt = {DAY} AND r.data_dt >= DATE '2026-09-01'
    """)

    assert of(report, "R7") == []


def test_r7_table_without_grain_is_skipped(mock_mart):
    tables = [t.model_copy(update={"grain_columns": []}) if t.table_id == "ods.exch_rate" else t
              for t in mock_mart.tables]
    validator = SqlValidator(MetadataStore.from_mart(mock_mart.model_copy(update={"tables": tables})))

    report = validator.validate(RATE_JOIN.format(rate_date="r.data_dt >= DATE '2026-09-01'"))

    assert of(report, "R7") == []


def test_r7_checks_aggregating_cte(validator):
    report = validator.validate(f"""
        WITH s AS (SELECT r.ccy_cd, SUM(a.bal_rmb) AS bal
                   FROM dwd.corp_dep_acct_d a JOIN ods.exch_rate r ON a.ccy_cd = r.ccy_cd
                   WHERE a.data_dt = {DAY} AND r.data_dt > DATE '2026-09-01' GROUP BY r.ccy_cd)
        SELECT s.ccy_cd, s.bal FROM s
    """)

    (message,) = of(report, "R7")
    assert "CTE s" in message


# ── R8 与查询方案一致 ─────────────────────────────────────

D01 = (f"SELECT o.lvl1_org_nm, SUM(b.dep_bal_rmb) AS dep_bal_rmb FROM {ORG_BAL} b "
       f"JOIN dim.org o ON b.org_no = o.org_no WHERE b.data_dt = {DAY} GROUP BY o.lvl1_org_nm")


def test_r8_skipped_without_plan(validator):
    assert of(validator.validate(D01), "R8") == []


def test_r8_plan_matches(validator):
    report = validator.validate(D01, expected_tables=[ORG_BAL, "dim.org"],
                                expected_columns=[f"{ORG_BAL}.dep_bal_rmb", "dim.org.lvl1_org_nm"])

    assert of(report, "R8") == []


def test_r8_reports_both_differences(validator):
    report = validator.validate(D01, expected_tables=[DEP, "dim.org"],
                                expected_columns=[f"{DEP}.dep_bal_rmb", "dim.org.lvl1_org_nm"])

    messages = of(report, "R8")
    assert len(messages) == 3
    assert any("没有用到" in m and DEP in m for m in messages)
    assert any("之外" in m and ORG_BAL in m for m in messages)
    assert any(f"{DEP}.dep_bal_rmb" in m for m in messages)
    assert all(issue.level == WARNING for issue in report.issues if issue.rule == "R8")


# ── 实际用到的表和字段 ────────────────────────────────────

def test_report_traces_columns_through_cte_to_entity_fields(validator):
    report = validator.validate(f"""
        WITH c AS (SELECT b.org_no, SUM(b.dep_bal_rmb) AS bal FROM {ORG_BAL} b
                   WHERE b.data_dt = {DAY} GROUP BY b.org_no)
        SELECT o.lvl1_org_nm, SUM(c.bal) AS bal FROM c JOIN dim.org o ON c.org_no = o.org_no
        GROUP BY o.lvl1_org_nm ORDER BY bal DESC
    """)

    assert report.tables == [ORG_BAL, "dim.org"]
    assert report.columns == [f"{ORG_BAL}.data_dt", f"{ORG_BAL}.dep_bal_rmb", f"{ORG_BAL}.org_no",
                              "dim.org.lvl1_org_nm", "dim.org.org_no"]


def test_report_star_counts_every_column(validator):
    report = validator.validate("SELECT * FROM dim.org")

    assert len(report.columns) == 7
    assert report.columns == sorted(report.columns)


# ── 标准 SQL 与模拟脚本 ───────────────────────────────────

@pytest.mark.parametrize("question", GOLD, ids=[q.id for q in GOLD])
def test_gold_sql_has_no_errors(validator, question):
    report = validator.validate(question.gold_sql, expected_tables=question.gold_tables,
                                expected_columns=question.gold_columns)

    assert report.errors == []
    assert report.tables == sorted(question.gold_tables)
    assert set(question.gold_columns) <= set(report.columns)
    assert of(report, "R8") == []
    assert not [issue for issue in report.issues if "检查时出错" in issue.message]


def test_gold_sql_count():
    assert len(GOLD) == 35


@pytest.mark.parametrize("script, sql", mock_selects(), ids=[name for name, _ in mock_selects()])
def test_mock_script_selects_have_no_errors(validator, script, sql):
    report = validator.validate(sql)

    assert report.errors == []
    assert not [issue for issue in report.issues if "检查时出错" in issue.message]
