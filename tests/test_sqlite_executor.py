"""测试数据的执行层：PostgreSQL 写法转成 SQLite 后结果要对，只读、超时、限行要守住。表名都是虚构的。"""
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from nl2sql.dialect import to_sqlite
from nl2sql.executor import TEST_DATA_NOTE, SQLiteExecutor, get_executor

ROWS = [("2025-12-31", "2025-12-31 18:00:00", 100.5, 3, " a1 ", "甲"), ("2026-01-15", "2026-01-15 09:30:00", 200.0, 4, "b2", "甲"),
        ("2026-02-01", "2026-02-01 12:00:00", 50.25, 5, "a3", "乙"), ("2026-02-20", "2026-02-20 08:00:00", None, 8, "c", "乙")]


@pytest.fixture
def path(tmp_path: Path) -> Path:
    target = tmp_path / "test_data.db"
    with closing(sqlite3.connect(target)) as connection, connection:
        connection.execute('CREATE TABLE "ads.sales" (day TEXT, moment TEXT, amount REAL, qty INTEGER, code TEXT, region TEXT)')
        connection.executemany('INSERT INTO "ads.sales" VALUES (?, ?, ?, ?, ?, ?)', ROWS)
        connection.execute('CREATE TABLE "ods.other" (id INTEGER)')
    return target


@pytest.fixture
def run(path: Path):
    executor = SQLiteExecutor(path)

    def query(sql: str, **options) -> list[tuple]:
        result = executor.execute(sql, **options)
        assert result.executed, result.error
        return result.rows

    return query


def test_a_date_literal_compares_as_a_date_not_as_a_number(run):
    assert run("SELECT COUNT(*) FROM ads.sales WHERE day >= DATE '2026-02-01'") == [(2,)]
    assert run("SELECT COUNT(*) FROM ads.sales WHERE CAST(moment AS DATE) = DATE '2026-01-15'") == [(1,)]
    assert run("SELECT COUNT(*) FROM ads.sales WHERE moment::timestamp >= TIMESTAMP '2026-02-01 00:00:00'") == [(2,)]


def test_year_month_and_quarter_of_a_nested_cast(run):
    rows = run("SELECT EXTRACT(YEAR FROM CAST(day AS DATE)) AS y, EXTRACT(QUARTER FROM CAST(day AS DATE)) AS q, COUNT(*) "
               "FROM ads.sales GROUP BY 1, 2 ORDER BY 1, 2")
    assert rows == [(2025, 4, 1), (2026, 1, 3)]


def test_to_char_and_date_trunc(run):
    assert run("SELECT to_char(CAST(day AS DATE), 'YYYY-MM') AS m, COUNT(*) FROM ads.sales GROUP BY 1 ORDER BY 1") == [
        ("2025-12", 1), ("2026-01", 1), ("2026-02", 2)]
    assert run("SELECT DISTINCT date_trunc('month', CAST(day AS DATE)) FROM ads.sales WHERE day > '2026-02-01'") == [
        ("2026-02-01 00:00:00",)]
    assert run("SELECT date_trunc('quarter', moment) FROM ads.sales WHERE day = '2026-02-20'") == [("2026-01-01 00:00:00",)]


def test_interval_arithmetic_and_date_difference(run):
    assert run("SELECT COUNT(*) FROM ads.sales WHERE day < DATE '2026-01-01' + INTERVAL '1 month'") == [(2,)]
    assert run("SELECT COUNT(*) FROM ads.sales WHERE day >= DATE '2026-03-01' - INTERVAL '1 month'") == [(2,)]
    assert run("SELECT DATE '2026-03-01' - DATE '2026-02-01'") == [(28,)]


def test_a_numeric_cast_keeps_the_decimals(run):
    assert run("SELECT CAST(qty AS NUMERIC) / 4 FROM ads.sales WHERE qty = 3") == [(0.75,)]
    assert run("SELECT ROUND(CAST(amount AS DECIMAL(10, 2)) / 3, 2) FROM ads.sales WHERE qty = 4") == [(66.67,)]


def test_text_functions_regexes_and_distinct_on_several_columns(run):
    assert run("SELECT btrim(code) FROM ads.sales WHERE qty = 3") == [("a1",)]
    assert run("SELECT COUNT(*) FROM ads.sales WHERE btrim(code) ~ '^a[[:digit:]]+$'") == [(2,)]
    assert run("SELECT COUNT(DISTINCT (region, qty > 3)) FROM ads.sales") == [(3,)]
    assert run("SELECT string_agg(DISTINCT region, ',') FROM ads.sales") == [("甲,乙",)]


def test_table_name_works_as_an_alias_and_cte_names_stay(run):
    assert run("SELECT sales.qty FROM ads.sales WHERE sales.qty = 3") == [(3,)]
    assert run("WITH t AS (SELECT region, SUM(qty) AS total FROM ads.sales GROUP BY region) "
               "SELECT region FROM t ORDER BY total DESC LIMIT 1") == [("乙",)]


def test_cte_column_lists_are_kept(run):
    assert run("WITH t(a, b) AS (SELECT region, qty FROM ads.sales) SELECT SUM(b) FROM t WHERE a = '甲'") == [(7,)]


def test_window_functions_run_unchanged(run):
    assert run("SELECT region, ROW_NUMBER() OVER (PARTITION BY region ORDER BY qty DESC) AS n FROM ads.sales "
               "ORDER BY region, n LIMIT 1") == [("乙", 1)]


def test_rollup_is_refused_with_a_clear_message(path):
    result = SQLiteExecutor(path).execute("SELECT region, SUM(qty) FROM ads.sales GROUP BY ROLLUP (region)")
    assert not result.executed and "ROLLUP" in result.error
    with pytest.raises(ValueError, match="ROLLUP"):
        to_sqlite("SELECT region FROM ads.sales GROUP BY GROUPING SETS ((region), ())")


@pytest.mark.parametrize("sql", ["INSERT INTO ads.sales (qty) VALUES (1)", "DELETE FROM ads.sales", "DROP TABLE ads.sales",
                                 "UPDATE ads.sales SET qty = 0", "ATTACH DATABASE 'other.db' AS other", "PRAGMA writable_schema = 1"])
def test_only_reads_are_allowed(path, sql):
    result = SQLiteExecutor(path).execute(sql)
    assert not result.executed and result.error
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute('SELECT COUNT(*) FROM "ads.sales"').fetchone() == (4,)


def test_row_limit_and_timeout(path):
    executor = SQLiteExecutor(path)
    limited = executor.execute("SELECT qty FROM ads.sales ORDER BY qty", max_rows=2)
    assert limited.row_count == 2 and limited.truncated
    assert not executor.execute("SELECT qty FROM ads.sales", max_rows=4).truncated
    endless = executor.execute("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT COUNT(*) FROM n",
                               timeout_s=0.05)
    assert not endless.executed and endless.error == "执行超过 0.05 秒，已中断"


def test_float_noise_is_rounded_away(run):
    assert run("SELECT 0.1 + 0.2") == [(0.3,)]


def test_results_say_they_are_not_business_data(path):
    assert SQLiteExecutor(path).execute("SELECT 1").note == TEST_DATA_NOTE


def test_failures_come_back_as_results(path, tmp_path):
    assert "测试数据不存在" in SQLiteExecutor(tmp_path / "absent.db").execute("SELECT 1").error
    assert "只能执行单条语句" in SQLiteExecutor(path).execute("SELECT 1; SELECT 2").error
    assert "no such table" in SQLiteExecutor(path).execute("SELECT * FROM ads.absent").error


def test_get_executor_builds_it(path):
    assert get_executor("sqlite", path).name == "sqlite"
    with pytest.raises(ValueError, match="库文件路径"):
        get_executor("sqlite")
