"""模拟专题库造数的测试"""
import shutil
from collections import Counter
from datetime import date
from pathlib import Path

import duckdb
import pytest

from data.mock_mart.build_mock import (MART_DIR, build, date_range, ensure_built, fingerprint,
                                       gen_orgs, run_scripts, verify)
from metadata.importer import load_mart
from tests.helpers import write_mart

# 短区间：跨年并包含 1 月上旬，一两秒就能跑完
SHORT = {"start": date(2025, 12, 31), "end": date(2026, 1, 12)}
SEP_30 = "DATE '2026-09-30'"


def query(db_path: Path, sql: str) -> list[tuple]:
    """以只读方式查询一个 DuckDB 文件"""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


@pytest.fixture(scope="module")
def short_db(tmp_path_factory) -> Path:
    """短区间的模拟库，本文件内共用，测试不得修改它"""
    path = tmp_path_factory.mktemp("mock") / "short.duckdb"
    assert build(path, **SHORT) == []
    return path


# ── 基础部件 ──────────────────────────────────────────────

def test_date_range_includes_both_ends():
    assert date_range(date(2026, 2, 27), date(2026, 3, 1)) == [
        date(2026, 2, 27), date(2026, 2, 28), date(2026, 3, 1)]


def test_org_hierarchy():
    rows = gen_orgs()

    assert Counter(row[3] for row in rows) == {"1": 1, "2": 3, "3": 6, "4": 12}
    assert ("0000", "总行", None, "1") in rows
    assert ("1101", "北京朝阳第一支行", "1100", "4") in rows


# ── 短区间构建 ────────────────────────────────────────────

def test_short_build_fills_every_day(short_db):
    assert query(short_db, "SELECT COUNT(DISTINCT data_dt) FROM ads.org_corp_dep_bal_d") == [(13,)]


def test_dim_org_flattens_the_hierarchy(short_db):
    rows = query(short_db, "SELECT org_no, lvl1_org_nm, lvl2_org_nm FROM dim.org "
                           "WHERE org_no IN ('0000', '2000', '2100', '2101') ORDER BY org_no")

    assert rows == [
        ("0000", None, None),
        ("2000", "上海分行", None),
        ("2100", "上海分行", "上海浦东分行"),
        ("2101", "上海分行", "上海浦东分行"),
    ]


def test_same_seed_gives_same_data_and_other_seed_differs(short_db, tmp_path):
    checksum = "SELECT SUM(dep_bal_tot_rmb), SUM(ytd_avg_bal_rmb) FROM ads.corp_cust_dep_bal_d"

    build(tmp_path / "again.duckdb", **SHORT)
    build(tmp_path / "other.duckdb", seed=1, **SHORT)

    assert query(tmp_path / "again.duckdb", checksum) == query(short_db, checksum)
    assert query(tmp_path / "other.duckdb", checksum) != query(short_db, checksum)


def test_ensure_built_rebuilds_only_when_inputs_change(tmp_path):
    path = tmp_path / "m.duckdb"

    assert ensure_built(path, **SHORT) is True
    assert ensure_built(path, **SHORT) is False             # 输入没变，直接复用
    assert ensure_built(path, seed=1, **SHORT) is True      # 种子变了，重新生成


def test_fingerprint_ignores_line_endings_but_not_content(tmp_path):
    mart_dir = tmp_path / "mart"
    shutil.copytree(MART_DIR, mart_dir, ignore=shutil.ignore_patterns("build", "__pycache__"))
    script = mart_dir / "scripts" / "01_dim_org.sql"
    before = fingerprint(mart_dir, seed=1, **SHORT)

    script.write_bytes(script.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert fingerprint(mart_dir, seed=1, **SHORT) == before

    script.write_bytes(script.read_bytes() + b"-- changed\n")
    assert fingerprint(mart_dir, seed=1, **SHORT) != before
    assert fingerprint(mart_dir, seed=2, **SHORT) != before


# ── 对平检核与失败处理 ────────────────────────────────────

def test_verify_reports_amounts_that_do_not_reconcile(short_db, tmp_path):
    tampered = tmp_path / "tampered.duckdb"
    shutil.copy(short_db, tampered)
    mart, _ = load_mart(MART_DIR)
    con = duckdb.connect(str(tampered))
    con.execute("UPDATE ads.org_corp_dep_bal_d SET dep_bal_rmb = dep_bal_rmb + 1 "
                "WHERE org_no = '1101' AND data_dt = DATE '2026-01-05'")

    problems = verify(con, mart)
    con.close()

    assert len(problems) == 1
    assert "机构存款汇总 = 客户存款汇总按机构合计" in problems[0]
    assert "1 行不平" in problems[0]


def test_verify_reports_grain_that_is_not_unique(short_db, tmp_path):
    tampered = tmp_path / "tampered.duckdb"
    shutil.copy(short_db, tampered)
    mart, _ = load_mart(MART_DIR)
    con = duckdb.connect(str(tampered))
    con.execute("INSERT INTO dim.org SELECT * FROM dim.org WHERE org_no = '1101'")

    problems = verify(con, mart)
    con.close()

    assert any("dim.org" in problem and "不唯一" in problem for problem in problems)


def test_failed_script_names_the_script_and_the_date(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "99_bad.sql").write_text(
        "INSERT INTO no_such_table SELECT DATE '${data_dt}';", encoding="utf-8")
    con = duckdb.connect(":memory:")

    with pytest.raises(RuntimeError, match=r"99_bad\.sql.*2026-01-01"):
        run_scripts(con, scripts, [date(2026, 1, 1)])
    con.close()


def test_build_refuses_a_mart_that_fails_import_checks(tmp_path, tiny_sheets):
    tiny_sheets["表清单"][0][0] = "明细层"
    bad_mart = write_mart(tmp_path / "bad", tiny_sheets)

    with pytest.raises(ValueError, match="没有通过导入校验"):
        build(tmp_path / "x.duckdb", mart_dir=bad_mart, **SHORT)
    assert not (tmp_path / "x.duckdb").exists()


# ── 完整模拟库：确认设计里的埋点在数据里真实存在，否则题库测不出差别 ──

def test_full_database_covers_the_evaluation_period(mock_db):
    assert query(mock_db, "SELECT MIN(data_dt), MAX(data_dt), COUNT(DISTINCT data_dt) "
                          "FROM ads.org_corp_dep_bal_d") == [(date(2025, 12, 31), date(2026, 9, 30), 274)]


def test_margin_deposits_make_the_two_calibers_differ(mock_db):
    dep, margin, total = query(
        mock_db, "SELECT SUM(dep_bal_rmb), SUM(margin_bal_rmb), SUM(dep_bal_tot_rmb) "
                 f"FROM ads.org_corp_dep_bal_d WHERE data_dt = {SEP_30}")[0]

    assert margin > 0
    assert dep + margin == total


def test_invalid_customers_stop_at_the_integration_layer(mock_db):
    in_dwd = query(mock_db, "SELECT COUNT(*) FROM dwd.corp_dep_acct_d WHERE cust_no LIKE 'C999%' "
                            f"AND data_dt = {SEP_30}")[0][0]
    in_ads = query(mock_db, "SELECT COUNT(*) FROM ads.corp_cust_dep_bal_d "
                            "WHERE cust_no LIKE 'C999%'")[0][0]

    assert in_dwd > 0 and in_ads == 0


def test_closed_and_personal_accounts_stop_at_the_source_layer(mock_db):
    closed = query(mock_db, "SELECT COUNT(*) FROM ods.core_dep_acct WHERE cust_type = '1' "
                            f"AND acct_stat = '1' AND data_dt = {SEP_30}")[0][0]
    personal_in_dwd = query(mock_db, "SELECT COUNT(*) FROM dwd.corp_dep_acct_d "
                                     "WHERE cust_no LIKE 'P%'")[0][0]

    assert closed == 15
    assert personal_in_dwd == 0


def test_foreign_currency_deposits_exist(mock_db):
    currencies = query(mock_db, "SELECT DISTINCT ccy_cd FROM dwd.corp_dep_acct_d "
                                f"WHERE data_dt = {SEP_30} ORDER BY 1")

    assert ("CNY",) in currencies and ("USD",) in currencies and len(currencies) >= 3


def test_customer_count_is_not_additive_across_orgs(mock_db):
    summed, distinct = query(mock_db, f"""
        SELECT (SELECT SUM(dep_cust_cnt) FROM ads.org_corp_dep_bal_d WHERE data_dt = {SEP_30}),
               (SELECT COUNT(DISTINCT cust_no) FROM ads.corp_cust_dep_bal_d
                WHERE data_dt = {SEP_30} AND dep_bal_tot_rmb > 0)""")[0]

    assert summed > distinct       # 有客户在多个机构开户，按机构相加会重复计数


def test_non_performing_loans_exist(mock_db):
    rows = query(mock_db, "SELECT SUM(npl_bal_rmb), SUM(loan_bal_rmb) FROM ads.corp_cust_loan_bal_d "
                          "WHERE data_dt IN (DATE '2026-06-30', DATE '2026-09-30') GROUP BY data_dt")

    assert len(rows) == 2
    assert all(0 < npl < loan for npl, loan in rows)


def test_settled_loans_stay_in_source_but_not_in_integration_layer(mock_db):
    in_ods = query(mock_db, "SELECT COUNT(*) FROM ods.core_loan_duebill "
                            f"WHERE cust_type = '1' AND loan_bal = 0 AND data_dt = {SEP_30}")[0][0]
    in_dwd = query(mock_db, "SELECT COUNT(*) FROM dwd.corp_loan_duebill_d WHERE loan_bal = 0")[0][0]

    assert in_ods > 0 and in_dwd == 0


def test_daily_average_differs_from_point_in_time_balance(mock_db):
    average, balance = query(mock_db, "SELECT SUM(ytd_avg_bal_rmb), SUM(dep_bal_rmb) "
                                      f"FROM ads.org_corp_dep_bal_d WHERE data_dt = {SEP_30}")[0]

    assert average != balance


# ── 真实使用里容易遇到的情况 ──────────────────────────────

def test_build_into_directory_with_chinese_and_spaces(tmp_path):
    path = tmp_path / "模拟 库" / "m.duckdb"

    assert build(path, **SHORT) == []
    assert query(path, "SELECT COUNT(*) FROM dim.org") == [(22,)]


def test_database_locked_by_another_program_gives_readable_error(tmp_path, monkeypatch):
    target = tmp_path / "locked.duckdb"
    target.write_bytes(b"")
    original_unlink = Path.unlink

    def unlink(self, missing_ok=False):
        if self == target:
            raise PermissionError("文件被占用")
        return original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)

    with pytest.raises(RuntimeError, match="正被其它程序占用"):
        build(target, **SHORT)


def test_build_refuses_columns_without_data_type(tmp_path, tiny_sheets):
    tiny_sheets["字段清单"][3][3] = ""
    untyped_mart = write_mart(tmp_path / "untyped", tiny_sheets)

    with pytest.raises(ValueError, match="ods.acct 的字段 bal 没有数据类型"):
        build(tmp_path / "x.duckdb", mart_dir=untyped_mart, **SHORT)
