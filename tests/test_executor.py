"""方言转换与执行层的测试"""
from decimal import Decimal

import duckdb
import pytest
from sqlglot.errors import ParseError

from nl2sql.dialect import transpile_statements
from nl2sql.executor import DuckDBExecutor, NullExecutor, get_executor


@pytest.fixture
def small_db(tmp_path):
    """只有一张三行小表的 DuckDB 库"""
    path = tmp_path / "small.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA ads")
    con.execute("CREATE TABLE ads.bal (data_dt DATE, org_no VARCHAR, bal DECIMAL(18,2))")
    con.execute("INSERT INTO ads.bal VALUES (DATE '2026-09-30', '1101', 100.00), "
                "(DATE '2026-09-30', '1102', 250.50), (DATE '2025-12-31', '1101', 80.00)")
    con.close()
    return path


# ── 方言转换 ──────────────────────────────────────────────

def test_transpile_splits_statements_and_keeps_quoted_parameter():
    statements = transpile_statements(
        "DELETE FROM t WHERE d = DATE '${data_dt}'; INSERT INTO t SELECT 1;")

    assert len(statements) == 2
    assert "${data_dt}" in statements[0]        # 引号里的日期参数原样保留，执行时再替换


def test_transpile_converts_postgres_functions():
    sql = transpile_statements("SELECT to_char(data_dt, 'YYYYMM') FROM t")[0]

    assert "STRFTIME" in sql.upper()


def test_transpile_rejects_invalid_sql():
    with pytest.raises(ParseError):
        transpile_statements("SELECT FROM WHERE")


# ── 执行层 ────────────────────────────────────────────────

def test_null_executor_does_not_execute():
    result = NullExecutor().execute("SELECT 1")

    assert not result.executed
    assert result.note == "仅生成模式，未执行"
    assert result.error == ""


def test_duckdb_executor_runs_postgres_sql(small_db):
    result = DuckDBExecutor(small_db).execute(
        "SELECT org_no, SUM(bal) AS bal FROM ads.bal "
        "WHERE data_dt = DATE '2026-09-30' GROUP BY org_no ORDER BY org_no")

    assert result.executed and result.error == ""
    assert result.columns == ["org_no", "bal"]
    assert result.rows == [("1101", Decimal("100.00")), ("1102", Decimal("250.50"))]
    assert (result.row_count, result.truncated) == (2, False)


def test_postgres_date_arithmetic_gives_day_count(small_db):
    sql = ("SELECT (DATE '2026-09-30' - CAST(date_trunc('year', DATE '2026-09-30') AS DATE) + 1) "
           "AS days")

    assert DuckDBExecutor(small_db).execute(sql).rows == [(273,)]


def test_max_rows_truncates_but_reports_total(small_db):
    executor = DuckDBExecutor(small_db)

    limited = executor.execute("SELECT * FROM ads.bal", max_rows=2)
    assert (len(limited.rows), limited.row_count, limited.truncated) == (2, 3, True)

    everything = executor.execute("SELECT * FROM ads.bal", max_rows=None)
    assert (len(everything.rows), everything.truncated) == (3, False)


def test_sql_error_is_returned_not_raised(small_db):
    result = DuckDBExecutor(small_db).execute("SELECT no_such FROM ads.bal")

    assert not result.executed
    assert "no_such" in result.error


def test_write_is_refused_and_data_is_untouched(small_db):
    executor = DuckDBExecutor(small_db)

    result = executor.execute("DELETE FROM ads.bal")

    assert not result.executed and result.error
    assert executor.execute("SELECT COUNT(*) FROM ads.bal").rows == [(3,)]


def test_multiple_statements_are_rejected(small_db):
    result = DuckDBExecutor(small_db).execute("SELECT 1; SELECT 2")

    assert not result.executed
    assert "只能执行单条语句" in result.error


def test_unparseable_sql_is_returned_not_raised(small_db):
    result = DuckDBExecutor(small_db).execute("SELECT FROM WHERE")

    assert not result.executed
    assert result.error.startswith("ParseError")


def test_missing_database_tells_how_to_create_it(tmp_path):
    result = DuckDBExecutor(tmp_path / "nope.duckdb").execute("SELECT 1")

    assert not result.executed
    assert "build_mock" in result.error


def test_get_executor(small_db):
    assert get_executor("none").name == "none"
    assert get_executor("duckdb", small_db).name == "duckdb"
    with pytest.raises(ValueError, match="需要提供模拟专题库路径"):
        get_executor("duckdb")
    with pytest.raises(ValueError, match="不支持的执行层"):
        get_executor("oracle")


def test_trailing_semicolon_is_accepted(small_db):
    result = DuckDBExecutor(small_db).execute("SELECT COUNT(*) AS n FROM ads.bal;")

    assert result.executed and result.rows == [(3,)]


def test_trailing_comment_is_not_a_separate_statement(small_db):
    # 模型生成的 SQL 末尾常带一句说明，分号后面的注释不能被当成第二条语句
    assert transpile_statements("SELECT 1; -- 说明") == ["SELECT 1"]
    assert transpile_statements("SELECT 1;\n/* 口径说明 */\n") == ["SELECT 1"]

    result = DuckDBExecutor(small_db).execute("SELECT COUNT(*) AS n FROM ads.bal;\n-- 全部机构")

    assert result.executed, result.error
    assert result.rows == [(3,)]


def test_generated_sql_cannot_read_or_write_local_files(small_db, tmp_path):
    executor = DuckDBExecutor(small_db)
    target = tmp_path / "leak.csv"
    source = tmp_path / "secret.csv"
    source.write_text("a\n1\n", encoding="utf-8")

    written = executor.execute(f"COPY (SELECT 1 AS a) TO '{target.as_posix()}' (FORMAT CSV)")
    read = executor.execute(f"SELECT * FROM read_csv('{source.as_posix()}')")

    assert not written.executed and not target.exists()
    assert not read.executed
    assert executor.execute("SELECT COUNT(*) FROM ads.bal").rows == [(3,)]     # 库本身照常可读
