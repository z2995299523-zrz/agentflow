"""方言转换与执行层的测试"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import duckdb
import pytest
from sqlglot.errors import ParseError

from nl2sql.dialect import strip_ansi, transpile_statements
from nl2sql.executor import DEFAULT_TIMEOUT_S, DuckDBExecutor, NullExecutor, get_executor

# 交叉连接后求和：DuckDB 没法提前算出结果，几百毫秒内跑不完，但 interrupt() 能立刻打断
SLOW_SQL = "SELECT SUM(a.range * b.range) FROM range(100000000) a, range(100000) b"


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


def test_transpile_drops_comments_and_statements_with_nothing_but_comments():
    # 模型生成的 SQL 常带说明，注释不算语句，也不留在转换后的 SQL 里
    assert transpile_statements("SELECT 1; -- 说明") == ["SELECT 1"]
    assert transpile_statements("SELECT 1;\n/* 口径说明 */\n") == ["SELECT 1"]
    assert transpile_statements("/* 开头说明 */ SELECT 1 -- 行尾说明") == ["SELECT 1"]
    assert transpile_statements("-- 只有注释") == []
    assert transpile_statements("   ;  ; ") == []


def test_comment_body_with_block_end_does_not_count_as_a_statement():
    # PostgreSQL 的块注释可以嵌套，正文里带 */ 的注释以前会被正则切坏，剩下半截当成第二条语句
    assert transpile_statements("SELECT 1; /* 外层 /* 内层 */ 外层 */") == ["SELECT 1"]


def test_comment_cannot_smuggle_extra_statements_past_the_single_statement_check():
    # 行注释里写 */ 和分号：PostgreSQL 眼里整段都是注释，只有一条语句。
    # sqlglot 带注释输出时会把它原样塞进块注释，变成 SELECT 1 /* */ ; SELECT 2; SELECT 3 /* */，DuckDB 会执行三条
    sql = "SELECT 1 -- */ ; SELECT 2; SELECT 3 /*"

    assert transpile_statements(sql) == ["SELECT 1"]


def test_strip_ansi_removes_escape_sequences_but_keeps_text():
    assert strip_ansi("SELECT FROM \x1b[4mWHERE\x1b[0m") == "SELECT FROM WHERE"
    assert strip_ansi("\x1b[1;31m错误\x1b[0m：\x1b[Kxx") == "错误：xx"
    assert strip_ansi("没有转义的文本，含 [4m 字样") == "没有转义的文本，含 [4m 字样"
    assert strip_ansi("") == ""


# ── 执行层 ────────────────────────────────────────────────

def test_null_executor_does_not_execute():
    result = NullExecutor().execute("SELECT 1")

    assert not result.executed
    assert result.note == "仅生成模式，未执行"
    assert result.error == ""


def test_null_executor_accepts_the_same_arguments_as_the_real_one():
    result = NullExecutor().execute("SELECT 1", max_rows=10, timeout_s=1.5)

    assert not result.executed and result.error == ""


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


def test_max_rows_truncates_and_row_count_is_the_rows_returned(small_db):
    executor = DuckDBExecutor(small_db)

    limited = executor.execute("SELECT * FROM ads.bal", max_rows=2)
    assert (len(limited.rows), limited.row_count, limited.truncated) == (2, 2, True)

    everything = executor.execute("SELECT * FROM ads.bal", max_rows=None)
    assert (len(everything.rows), everything.row_count, everything.truncated) == (3, 3, False)


def test_truncated_means_there_are_more_rows_behind(small_db):
    executor = DuckDBExecutor(small_db)

    exactly = executor.execute("SELECT * FROM ads.bal", max_rows=3)
    assert (exactly.row_count, exactly.truncated) == (3, False)       # 刚好取完，后面没有了

    roomy = executor.execute("SELECT * FROM ads.bal", max_rows=100)
    assert (roomy.row_count, roomy.truncated) == (3, False)

    none_wanted = executor.execute("SELECT * FROM ads.bal", max_rows=0)
    assert (none_wanted.rows, none_wanted.truncated) == ([], True)


def test_only_the_needed_rows_are_read_from_a_huge_result(small_db):
    # 一万亿行的交叉连接：整个读回来会撑爆内存，只取 max_rows + 1 行就能很快返回
    sql = "SELECT a.range AS a, b.range AS b FROM range(1000000) a, range(1000000) b"
    started = time.perf_counter()

    result = DuckDBExecutor(small_db).execute(sql, max_rows=5)

    assert result.executed, result.error
    assert (result.row_count, result.truncated) == (5, True)
    assert time.perf_counter() - started < 5


def test_empty_result_keeps_the_column_names(small_db):
    result = DuckDBExecutor(small_db).execute("SELECT org_no FROM ads.bal WHERE 1 = 0")

    assert result.executed and result.rows == []
    assert (result.columns, result.row_count, result.truncated) == (["org_no"], 0, False)


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


def test_comment_cannot_make_the_executor_run_more_than_one_statement(small_db):
    # DuckDB 执行多条语句时返回最后一条的结果：以前这里会拿到 [(3,)]
    result = DuckDBExecutor(small_db).execute("SELECT 1 AS n -- */ ; SELECT 2; SELECT 3 /*")

    assert result.executed, result.error
    assert result.rows == [(1,)]


def test_unparseable_sql_is_returned_not_raised(small_db):
    result = DuckDBExecutor(small_db).execute("SELECT FROM WHERE")

    assert not result.executed
    assert result.error.startswith("ParseError")


def test_error_text_has_no_ansi_escape_sequences(small_db):
    # sqlglot 的解析错误会用 \x1b[4m 给出错位置加下划线
    result = DuckDBExecutor(small_db).execute("SELECT FROM WHERE")

    assert "\x1b" not in result.error
    assert "WHERE" in result.error


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

    # 失败的原因要对：是外部访问被配置禁用，而不是路径写错之类的别的毛病
    assert not written.executed and not target.exists()
    assert "disabled by configuration" in written.error
    assert not read.executed
    assert "disabled by configuration" in read.error
    assert executor.execute("SELECT COUNT(*) FROM ads.bal").rows == [(3,)]     # 库本身照常可读


def test_external_access_cannot_be_switched_back_on(small_db, tmp_path):
    executor = DuckDBExecutor(small_db)
    source = tmp_path / "secret.csv"
    source.write_text("a\n1\n", encoding="utf-8")

    switched = executor.execute("SET enable_external_access = true")

    assert not switched.executed and "external access" in switched.error
    assert not executor.execute(f"SELECT * FROM read_csv('{source.as_posix()}')").executed


# ── 超时与并发 ────────────────────────────────────────────

def test_default_timeout_is_thirty_seconds():
    assert DEFAULT_TIMEOUT_S == 30


def test_slow_query_is_interrupted_at_the_timeout(small_db):
    executor = DuckDBExecutor(small_db)
    started = time.perf_counter()

    result = executor.execute(SLOW_SQL, timeout_s=0.5)
    elapsed = time.perf_counter() - started

    assert not result.executed
    assert result.error == "执行超过 0.5 秒，已中断"
    assert 0.4 < elapsed < 3, elapsed                # 到点就中断，不会等查询自己跑完
    assert result.elapsed_ms >= 400
    # 中断之后同一个执行层照常可用，库文件也没有被占着
    assert executor.execute("SELECT COUNT(*) FROM ads.bal").rows == [(3,)]


def test_timeout_message_shows_whole_seconds_without_decimals(small_db):
    result = DuckDBExecutor(small_db).execute(SLOW_SQL, timeout_s=1)

    assert result.error == "执行超过 1 秒，已中断"


def test_timeout_none_waits_for_the_query(small_db):
    result = DuckDBExecutor(small_db).execute("SELECT COUNT(*) FROM range(3000000) a", timeout_s=None)

    assert result.executed and result.rows == [(3000000,)]


def test_fast_query_is_not_affected_by_the_timeout(small_db):
    started = time.perf_counter()

    result = DuckDBExecutor(small_db).execute("SELECT * FROM ads.bal", timeout_s=30)

    assert result.executed and result.row_count == 3
    assert time.perf_counter() - started < 5         # 不会等满 30 秒


def test_concurrent_executions_each_use_their_own_connection(small_db):
    executor = DuckDBExecutor(small_db)
    sql = "SELECT org_no, SUM(bal) AS bal FROM ads.bal GROUP BY org_no ORDER BY org_no"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: executor.execute(sql), range(60)))

    assert all(result.executed and result.row_count == 2 for result in results), \
        [result.error for result in results if not result.executed]
    assert len({tuple(result.rows) for result in results}) == 1


def test_a_timeout_in_one_thread_does_not_disturb_the_others(small_db):
    executor = DuckDBExecutor(small_db)
    slow: list = []
    thread = threading.Thread(target=lambda: slow.append(executor.execute(SLOW_SQL, timeout_s=1)))
    thread.start()

    quick = [executor.execute("SELECT COUNT(*) FROM ads.bal") for _ in range(10)]
    thread.join(10)

    assert all(result.executed and result.rows == [(3,)] for result in quick)
    assert slow and slow[0].error == "执行超过 1 秒，已中断"


# ── 打断不生效、没有结果集：用假连接走到这两条分支 ──────────

def test_interrupt_that_does_not_take_effect_is_reported_as_such(small_db, monkeypatch):
    release = threading.Event()

    class StuckConnection:
        """卡住不退出的连接：interrupt() 没有效果"""

        def execute(self, statement):
            release.wait(10)
            raise RuntimeError("太晚了")

        def interrupt(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr("nl2sql.executor.duckdb.connect", lambda *args, **kwargs: StuckConnection())
    monkeypatch.setattr("nl2sql.executor.INTERRUPT_GRACE_S", 0.2)

    result = DuckDBExecutor(small_db).execute("SELECT 1", timeout_s=0.2)
    release.set()

    assert not result.executed
    assert result.error == "执行超过 0.2 秒，中断未生效"


def test_statement_without_a_result_set_returns_no_rows_and_reads_nothing(small_db, monkeypatch):
    class NoResultConnection:
        description = None

        def execute(self, statement):
            return self

        def fetchmany(self, size):
            raise AssertionError("没有结果集，不该去读")

        fetchall = fetchmany

        def interrupt(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr("nl2sql.executor.duckdb.connect", lambda *args, **kwargs: NoResultConnection())

    result = DuckDBExecutor(small_db).execute("SELECT 1")

    assert result.executed and (result.columns, result.rows, result.row_count) == ([], [], 0)
