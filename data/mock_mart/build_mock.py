"""
模拟专题库造数
用法: python -m data.mock_mart.build_mock
生成 build/mock.duckdb，步骤：按字段清单建表 → 生成贴源层数据 → 逐日执行加工脚本 → 对平检核

只有贴源层是随机生成的，标准层和专题层由 scripts/ 里的加工脚本真实跑出来，
所以数据字典、血缘、脚本、数据四者一致。
"""
import csv
import hashlib
import math
import random
import sys
import tempfile
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import duckdb

from metadata.importer import load_mart
from metadata.models import Mart
from nl2sql.dialect import transpile_statements

MART_DIR = Path(__file__).parent
SCRIPTS_DIR_NAME = "scripts"
DEFAULT_DB_PATH = MART_DIR / "build" / "mock.duckdb"
DEFAULT_START = date(2025, 12, 31)      # 上年末，较年初增量的基期
DEFAULT_END = date(2026, 9, 30)
DEFAULT_SEED = 20260930
DATE_PARAM = "${data_dt}"               # 加工脚本里的批量日期参数
FINGERPRINT_SUFFIX = ".fingerprint"     # 记录造数输入指纹的文件后缀

# ── 参考数据 ──────────────────────────────────────────────
HEAD_OFFICE_NO = "0000"
BRANCHES = [    # (一级分行机构号, 名称, [(二级分行机构号, 名称)])
    ("1000", "北京分行", [("1100", "北京朝阳分行"), ("1200", "北京海淀分行")]),
    ("2000", "上海分行", [("2100", "上海浦东分行"), ("2200", "上海徐汇分行")]),
    ("3000", "广东分行", [("3100", "广州天河分行"), ("3200", "深圳南山分行")]),
]
SUB_BRANCH_SUFFIXES = ["第一支行", "第二支行"]

INDUSTRIES = [
    ("C", "制造业"),
    ("D", "电力、热力、燃气及水生产和供应业"),
    ("E", "建筑业"),
    ("F", "批发和零售业"),
    ("G", "交通运输、仓储和邮政业"),
    ("I", "信息传输、软件和信息技术服务业"),
    ("K", "房地产业"),
    ("L", "租赁和商务服务业"),
]
INDUSTRY_WEIGHTS = [30, 5, 12, 20, 8, 10, 8, 7]
INDUSTRY_WORDS = {"C": "机械制造", "D": "能源", "E": "建设工程", "F": "商贸",
                  "G": "物流", "I": "信息技术", "K": "置业", "L": "商务服务"}
SCALES = ["1", "2", "3", "4"]           # 大型、中型、小型、微型
SCALE_WEIGHTS = [10, 25, 40, 25]
SCALE_DEPOSIT_BASE = {"1": 5e7, "2": 8e6, "3": 1.5e6, "4": 3e5}
SCALE_LOAN_BASE = {"1": 8e7, "2": 1.5e7, "3": 3e6, "4": 5e5}

SUBJECTS = [    # (科目号, 科目名称, 科目类别)
    ("200101", "单位活期存款", "01"),
    ("200201", "单位定期存款", "02"),
    ("200202", "单位通知存款", "02"),
    ("201101", "银行承兑汇票保证金", "03"),
    ("201102", "信用证保证金", "03"),
    ("211101", "个人活期存款", "11"),
    ("211201", "个人定期存款", "12"),
]
CURRENT_SUBJECT = "200101"
FIXED_SUBJECTS = ["200201", "200202"]
MARGIN_SUBJECTS = ["201101", "201102"]
PERSONAL_SUBJECTS = ["211101", "211201"]

BASE_RATES = {"CNY": 1.0, "USD": 7.10, "EUR": 7.75, "HKD": 0.91}
LOAN_PRODUCTS = ["1001", "1002", "1003", "1004"]

NAME_REGIONS = ["北京", "上海", "广州", "深圳", "杭州", "南京"]
NAME_FIRST = ["华", "盛", "鑫", "泰", "恒", "瑞", "嘉", "宏", "裕", "安", "德", "永"]
NAME_SECOND = ["达", "通", "源", "丰", "隆", "信", "诚", "兴", "茂", "和"]

CORP_CUSTOMER_COUNT = 120
LOAN_CUSTOMER_COUNT = 75                # 前 75 个客户有贷款
INVALID_CUSTOMERS = ["C999001", "C999002", "C999003"]   # 有账户但客户信息表里不存在
CLOSED_ACCOUNT_COUNT = 15
PERSONAL_ACCOUNT_COUNT = 25
PERSONAL_LOAN_COUNT = 8


def date_range(start: date, end: date) -> list[date]:
    """返回从 start 到 end（含两端）的每一天"""
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


# ── 贴源层数据生成 ────────────────────────────────────────
def gen_orgs() -> list[tuple]:
    """机构信息：1 个总行、3 个一级分行、6 个二级分行、12 个支行"""
    rows: list[tuple] = [(HEAD_OFFICE_NO, "总行", None, "1")]
    for lvl1_no, lvl1_nm, children in BRANCHES:
        rows.append((lvl1_no, lvl1_nm, HEAD_OFFICE_NO, "2"))
        for lvl2_no, lvl2_nm in children:
            rows.append((lvl2_no, lvl2_nm, lvl1_no, "3"))
            for index, suffix in enumerate(SUB_BRANCH_SUFFIXES, start=1):
                rows.append((f"{lvl2_no[:3]}{index}", lvl2_nm.removesuffix("分行") + suffix, lvl2_no, "4"))
    return rows


def pick_org(rng: random.Random, sub_branches: list[str], lvl2_branches: list[str]) -> str:
    """选一个办理业务的机构：九成在支行，一成在二级分行本部"""
    return rng.choice(sub_branches) if rng.random() < 0.9 else rng.choice(lvl2_branches)


def gen_customers(rng: random.Random, sub_branches: list[str],
                  lvl2_branches: list[str]) -> list[dict]:
    """对公客户：客户号、名称、行业、规模、主办机构"""
    customers = []
    for index in range(CORP_CUSTOMER_COUNT):
        industry_cd = rng.choices(INDUSTRIES, weights=INDUSTRY_WEIGHTS)[0][0]
        name = (NAME_REGIONS[index % len(NAME_REGIONS)] + NAME_FIRST[index // 10]
                + NAME_SECOND[index % 10] + INDUSTRY_WORDS[industry_cd] + "有限公司")
        customers.append({
            "cust_no": f"C{index + 1:06d}",
            "cust_nm": name,
            "industry_cd": industry_cd,
            "scale_cd": rng.choices(SCALES, weights=SCALE_WEIGHTS)[0],
            "home_org": pick_org(rng, sub_branches, lvl2_branches),
        })
    return customers


def gen_exch_rates(rng: random.Random, dates: list[date]) -> list[tuple]:
    """汇率：人民币恒为 1，外币在基准价附近小幅波动"""
    rows = []
    rates = dict(BASE_RATES)
    for day in dates:
        for ccy in rates:
            if ccy != "CNY":
                rates[ccy] *= 1 + rng.gauss(0, 0.0015)
            rows.append((day, ccy, round(rates[ccy], 6)))
    return rows


def gen_dep_rows(rng: random.Random, customers: list[dict], sub_branches: list[str],
                 lvl2_branches: list[str], dates: list[date]) -> list[tuple]:
    """存款账户每日快照"""
    accounts: list[dict] = []

    def add(cust_no: str, cust_type: str, org_no: str, subj_no: str, ccy: str,
            balance: float, volatility: float, stat: str = "0") -> None:
        accounts.append({
            "acct_no": f"62{len(accounts) + 1:014d}",
            "cust_no": cust_no, "cust_type": cust_type, "org_no": org_no, "subj_no": subj_no,
            "ccy": ccy, "balance": balance, "volatility": volatility, "stat": stat,
            "open_dt": date(2015, 1, 1) + timedelta(days=rng.randrange(3650)),
        })

    for customer in customers:
        cust_no, home = customer["cust_no"], customer["home_org"]
        base = SCALE_DEPOSIT_BASE[customer["scale_cd"]] * math.exp(rng.gauss(0, 0.6))
        add(cust_no, "1", home, CURRENT_SUBJECT, "CNY", base, 0.03)
        if rng.random() < 0.50:     # 定期存款
            add(cust_no, "1", home, rng.choice(FIXED_SUBJECTS), "CNY", base * rng.uniform(0.5, 2.0), 0.0)
        if rng.random() < 0.30:     # 保证金
            add(cust_no, "1", home, rng.choice(MARGIN_SUBJECTS), "CNY", base * rng.uniform(0.05, 0.3), 0.005)
        if rng.random() < 0.15:     # 在另一个机构还有活期账户
            other = rng.choice([org for org in sub_branches if org != home])
            add(cust_no, "1", other, CURRENT_SUBJECT, "CNY", base * rng.uniform(0.1, 0.5), 0.03)
        if rng.random() < 0.12:     # 外币活期账户
            ccy = rng.choices(["USD", "EUR", "HKD"], weights=[70, 20, 10])[0]
            add(cust_no, "1", home, CURRENT_SUBJECT, ccy,
                base * rng.uniform(0.05, 0.3) / BASE_RATES[ccy], 0.03)

    for index, cust_no in enumerate(INVALID_CUSTOMERS):     # 客户号无效的对公账户
        org = sub_branches[index]
        add(cust_no, "1", org, CURRENT_SUBJECT, "CNY", 2e6 * rng.uniform(0.5, 1.5), 0.03)
        add(cust_no, "1", org, MARGIN_SUBJECTS[0] if index % 2 == 0 else CURRENT_SUBJECT,
            "USD" if index == 1 else "CNY", 3e5 * rng.uniform(0.5, 1.5), 0.01)

    for _ in range(CLOSED_ACCOUNT_COUNT):                   # 已销户的对公账户
        customer = rng.choice(customers)
        add(customer["cust_no"], "1", customer["home_org"], CURRENT_SUBJECT, "CNY", 0.0, 0.0, stat="1")

    for index in range(PERSONAL_ACCOUNT_COUNT):             # 个人账户
        add(f"P{index + 1:06d}", "2", pick_org(rng, sub_branches, lvl2_branches),
            rng.choice(PERSONAL_SUBJECTS), "CNY", 5e4 * math.exp(rng.gauss(0, 0.8)), 0.02)

    rows = []
    for account in accounts:
        balance = account["balance"]
        is_fixed = account["subj_no"] in FIXED_SUBJECTS
        for day in dates:
            if account["volatility"] > 0:
                balance = max(0.0, balance * (1 + rng.gauss(0, account["volatility"])))
            elif is_fixed and rng.random() < 0.01:          # 定期存款偶尔到期续存，金额变化
                balance *= rng.uniform(0.8, 1.25)
            rows.append((day, account["acct_no"], account["cust_no"], account["cust_type"],
                         account["org_no"], account["subj_no"], account["ccy"],
                         round(balance, 2), account["stat"], account["open_dt"]))
    return rows


def gen_loan_rows(rng: random.Random, customers: list[dict], sub_branches: list[str],
                  dates: list[date]) -> list[tuple]:
    """贷款借据每日快照"""
    period_start, period_days = dates[0], len(dates)
    event_from = min(30, period_days - 1)       # 结清、降级最早发生在第几天；区间很短时不能越界
    duebills: list[dict] = []

    def add(cust_no: str, cust_type: str, org_no: str, principal: float) -> None:
        if rng.random() < 0.70:     # 期初之前已发放
            start_dt = period_start - timedelta(days=rng.randrange(30, 900))
        else:                       # 期间内新发放
            start_dt = period_start + timedelta(days=rng.randrange(1, max(2, period_days - 30)))
        ccy = "USD" if rng.random() < 0.08 else "CNY"
        duebills.append({
            "duebill_no": f"DB{len(duebills) + 1:010d}",
            "cust_no": cust_no, "cust_type": cust_type, "org_no": org_no,
            "prod_cd": rng.choice(LOAN_PRODUCTS), "ccy": ccy,
            "balance": principal / BASE_RATES[ccy],
            "five_cls": rng.choices(["1", "2", "3", "4", "5"], weights=[86, 7, 4, 2, 1])[0],
            "start_dt": start_dt,
            "end_dt": start_dt + timedelta(days=rng.choice([365, 730, 1095])),
            "repay_rate": rng.choice([0.0, 0.0, 0.02, 0.05]),      # 每月 20 日的还款比例
            "settle_offset": rng.randrange(event_from, period_days) if rng.random() < 0.08 else None,
            "downgrade_offset": rng.randrange(event_from, period_days) if rng.random() < 0.08 else None,
        })

    for customer in customers[:LOAN_CUSTOMER_COUNT]:
        base = SCALE_LOAN_BASE[customer["scale_cd"]]
        for _ in range(rng.choice([1, 1, 2, 2, 3])):
            add(customer["cust_no"], "1", customer["home_org"], base * math.exp(rng.gauss(0, 0.5)))
    for index, cust_no in enumerate(INVALID_CUSTOMERS[:2]):          # 客户号无效的对公借据
        add(cust_no, "1", sub_branches[index], 2e6)
    for index in range(PERSONAL_LOAN_COUNT):                         # 个人贷款
        add(f"P{index + 1:06d}", "2", rng.choice(sub_branches), 3e5 * math.exp(rng.gauss(0, 0.5)))

    rows = []
    for duebill in duebills:
        balance, five_cls = duebill["balance"], duebill["five_cls"]
        for offset, day in enumerate(dates):
            if day < duebill["start_dt"]:
                continue            # 尚未发放，不出现在快照里
            if day.day == 20:
                balance *= 1 - duebill["repay_rate"]
            if duebill["settle_offset"] is not None and offset >= duebill["settle_offset"]:
                balance = 0.0       # 已结清，借据仍在快照里但余额为零
            if duebill["downgrade_offset"] is not None and offset == duebill["downgrade_offset"]:
                five_cls = str(min(5, int(five_cls) + 1))
            rows.append((day, duebill["duebill_no"], duebill["cust_no"], duebill["cust_type"],
                         duebill["org_no"], duebill["prod_cd"], duebill["ccy"], round(balance, 2),
                         five_cls, duebill["start_dt"], duebill["end_dt"]))
    return rows


def load_rows(con: duckdb.DuckDBPyConnection, table: str, columns: list[str],
              rows: list[tuple], work_dir: Path) -> None:
    """把一批行写成临时 CSV 再批量装入表。None 写成空串，装入时还原为 NULL"""
    csv_path = work_dir / f"{table}.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(columns)
        writer.writerows(rows)
    con.execute(f"COPY {table} ({', '.join(columns)}) FROM '{csv_path.as_posix()}' "
                "(FORMAT CSV, HEADER, NULLSTR '')")
    csv_path.unlink()


def generate_ods(con: duckdb.DuckDBPyConnection, dates: list[date],
                 seed: int = DEFAULT_SEED) -> None:
    """生成贴源层七张表的数据。同一个种子每次生成的数据相同

    Args:
        con: 已建好表的 DuckDB 连接
        dates: 要生成快照的日期
        seed: 随机种子
    """
    rng = random.Random(seed)
    orgs = gen_orgs()
    sub_branches = [row[0] for row in orgs if row[3] == "4"]
    lvl2_branches = [row[0] for row in orgs if row[3] == "3"]
    customers = gen_customers(rng, sub_branches, lvl2_branches)

    with tempfile.TemporaryDirectory() as tmp:
        work_dir = Path(tmp)
        load_rows(con, "ods.org_info", ["org_no", "org_nm", "up_org_no", "org_lvl"], orgs, work_dir)
        load_rows(con, "ods.industry_dict", ["industry_cd", "industry_nm"], INDUSTRIES, work_dir)
        load_rows(con, "ods.gl_subject", ["subj_no", "subj_nm", "subj_type_cd"], SUBJECTS, work_dir)
        load_rows(con, "ods.ecif_corp_cust", ["cust_no", "cust_nm", "industry_cd", "scale_cd"],
                  [(c["cust_no"], c["cust_nm"], c["industry_cd"], c["scale_cd"]) for c in customers],
                  work_dir)
        load_rows(con, "ods.exch_rate", ["data_dt", "ccy_cd", "mid_rate"],
                  gen_exch_rates(rng, dates), work_dir)
        load_rows(con, "ods.core_dep_acct",
                  ["data_dt", "acct_no", "cust_no", "cust_type", "org_no", "subj_no", "ccy_cd",
                   "acct_bal", "acct_stat", "open_dt"],
                  gen_dep_rows(rng, customers, sub_branches, lvl2_branches, dates), work_dir)
        load_rows(con, "ods.core_loan_duebill",
                  ["data_dt", "duebill_no", "cust_no", "cust_type", "org_no", "prod_cd", "ccy_cd",
                   "loan_bal", "five_cls_cd", "start_dt", "end_dt"],
                  gen_loan_rows(rng, customers, sub_branches, dates), work_dir)


# ── 建表、跑脚本、对平 ────────────────────────────────────
def create_tables(con: duckdb.DuckDBPyConnection, mart: Mart) -> None:
    """按表清单和字段清单建模式和表，物理结构直接来自元数据

    Raises:
        ValueError: 有字段没有数据类型
    """
    for schema in sorted({table.schema_name for table in mart.tables}):
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    columns_by_table = defaultdict(list)
    for column in mart.columns:
        columns_by_table[column.table_id].append(column)
    for table in mart.tables:
        untyped = [column.column_name for column in columns_by_table[table.table_id]
                   if not column.data_type]
        if untyped:
            raise ValueError(f"表 {table.table_id} 的字段 {'、'.join(untyped)} 没有数据类型，无法建表")
        definitions = ", ".join(
            f"{column.column_name} {column.data_type}"
            for column in sorted(columns_by_table[table.table_id], key=lambda c: c.ordinal)
        )
        for statement in transpile_statements(f"CREATE TABLE {table.table_id} ({definitions})"):
            con.execute(statement)


def run_scripts(con: duckdb.DuckDBPyConnection, scripts_dir: Path, dates: list[date]) -> None:
    """按文件名顺序执行加工脚本

    不含日期参数的脚本（维表）只执行一次；含 ${data_dt} 的脚本对每个日期各执行一次。
    脚本按目标库方言书写，执行前转成 DuckDB 方言。

    Raises:
        RuntimeError: 某个脚本执行失败，信息里带脚本名和日期
    """
    once: list[tuple[str, list[str]]] = []
    daily: list[tuple[str, list[str]]] = []
    for path in sorted(scripts_dir.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        statements = transpile_statements(text)     # 日期参数在引号里，转换后原样保留
        (daily if DATE_PARAM in text else once).append((path.name, statements))

    def execute(name: str, statements: list[str], day: date | None) -> None:
        for statement in statements:
            sql = statement.replace(DATE_PARAM, day.isoformat()) if day else statement
            try:
                con.execute(sql)
            except duckdb.Error as error:
                when = f"（数据日期 {day}）" if day else ""
                raise RuntimeError(f"加工脚本 {name}{when} 执行失败: {error}") from error

    for name, statements in once:
        execute(name, statements, None)
    for day in dates:
        for name, statements in daily:
            execute(name, statements, day)


# 每条检核是一句返回「不平的行数」的 DuckDB SQL，结果为 0 才算通过
RECONCILE_CHECKS: list[tuple[str, str]] = [
    ("标准层存款快照的行数 = 贴源层对公正常账户的行数", """
        SELECT COUNT(*) FROM (
            SELECT (SELECT COUNT(*) FROM dwd.corp_dep_acct_d) AS actual,
                   (SELECT COUNT(*) FROM ods.core_dep_acct
                    WHERE cust_type = '1' AND acct_stat = '0') AS expected
        ) WHERE actual <> expected"""),
    ("标准层贷款快照的行数 = 贴源层对公且余额大于零的借据行数", """
        SELECT COUNT(*) FROM (
            SELECT (SELECT COUNT(*) FROM dwd.corp_loan_duebill_d) AS actual,
                   (SELECT COUNT(*) FROM ods.core_loan_duebill
                    WHERE cust_type = '1' AND loan_bal > 0) AS expected
        ) WHERE actual <> expected"""),
    ("客户存款汇总的全口径余额 = 标准层存款快照中有效客户的余额合计", """
        SELECT COUNT(*) FROM (
            SELECT data_dt, SUM(dep_bal_tot_rmb) AS total FROM ads.corp_cust_dep_bal_d GROUP BY data_dt
        ) summary FULL JOIN (
            SELECT a.data_dt, SUM(a.bal_rmb) AS total
            FROM dwd.corp_dep_acct_d a JOIN dim.corp_cust c ON a.cust_no = c.cust_no
            GROUP BY a.data_dt
        ) detail ON summary.data_dt = detail.data_dt
        WHERE summary.total IS DISTINCT FROM detail.total"""),
    ("存款余额 + 保证金存款余额 = 全口径存款余额", """
        SELECT COUNT(*) FROM ads.corp_cust_dep_bal_d
        WHERE dep_bal_rmb + margin_bal_rmb <> dep_bal_tot_rmb"""),
    ("活期存款余额 + 定期存款余额 = 存款余额", """
        SELECT COUNT(*) FROM ads.corp_cust_dep_bal_d
        WHERE cur_dep_bal_rmb + fix_dep_bal_rmb <> dep_bal_rmb"""),
    ("机构存款汇总 = 客户存款汇总按机构合计", """
        SELECT COUNT(*) FROM ads.org_corp_dep_bal_d org FULL JOIN (
            SELECT data_dt, org_no, SUM(dep_bal_rmb) AS dep, SUM(margin_bal_rmb) AS margin,
                   SUM(dep_bal_tot_rmb) AS total, SUM(ytd_avg_bal_rmb) AS ytd
            FROM ads.corp_cust_dep_bal_d GROUP BY data_dt, org_no
        ) cust ON org.data_dt = cust.data_dt AND org.org_no = cust.org_no
        WHERE org.dep_bal_rmb IS DISTINCT FROM cust.dep
           OR org.margin_bal_rmb IS DISTINCT FROM cust.margin
           OR org.dep_bal_tot_rmb IS DISTINCT FROM cust.total
           OR org.ytd_avg_bal_rmb IS DISTINCT FROM cust.ytd"""),
    ("客户贷款汇总的余额 = 标准层贷款快照中有效客户的余额合计", """
        SELECT COUNT(*) FROM (
            SELECT data_dt, SUM(loan_bal_rmb) AS total, SUM(npl_bal_rmb) AS npl
            FROM ads.corp_cust_loan_bal_d GROUP BY data_dt
        ) summary FULL JOIN (
            SELECT l.data_dt, SUM(l.bal_rmb) AS total,
                   SUM(CASE WHEN l.is_npl = '1' THEN l.bal_rmb ELSE 0 END) AS npl
            FROM dwd.corp_loan_duebill_d l JOIN dim.corp_cust c ON l.cust_no = c.cust_no
            GROUP BY l.data_dt
        ) detail ON summary.data_dt = detail.data_dt
        WHERE summary.total IS DISTINCT FROM detail.total OR summary.npl IS DISTINCT FROM detail.npl"""),
    ("月末的年日均 = 年初至当日每日存款余额之和 ÷ 天数", """
        SELECT COUNT(*) FROM ads.corp_cust_dep_bal_d summary JOIN (
            SELECT stat.data_dt AS stat_dt, a.cust_no, a.org_no,
                   SUM(CASE WHEN a.is_margin = '0' THEN a.bal_rmb ELSE 0 END) AS acm
            FROM (SELECT DISTINCT data_dt FROM ads.corp_cust_dep_bal_d
                  WHERE data_dt = last_day(data_dt)) stat
            JOIN dwd.corp_dep_acct_d a
              ON a.data_dt BETWEEN date_trunc('year', stat.data_dt) AND stat.data_dt
            GROUP BY stat.data_dt, a.cust_no, a.org_no
        ) detail ON summary.data_dt = detail.stat_dt
                AND summary.cust_no = detail.cust_no AND summary.org_no = detail.org_no
        WHERE summary.ytd_avg_bal_rmb <> ROUND(
            detail.acm / (date_diff('day', date_trunc('year', summary.data_dt), summary.data_dt) + 1), 2)"""),
]


def verify(con: duckdb.DuckDBPyConnection, mart: Mart) -> list[str]:
    """对平检核

    检查两类问题：每张表的粒度字段是否真的唯一；各层之间的金额和行数是否对得上。

    Returns:
        不平的说明。空列表表示全部通过
    """
    problems = []
    for table in mart.tables:
        if table.grain_columns:        # 没有填粒度字段的表无法检核唯一性
            grain = ", ".join(table.grain_columns)
            duplicated = con.execute(
                f"SELECT COUNT(*) FROM (SELECT {grain} FROM {table.table_id} "
                f"GROUP BY {grain} HAVING COUNT(*) > 1)").fetchone()[0]
            if duplicated:
                problems.append(
                    f"表 {table.table_id} 的粒度字段（{grain}）不唯一，有 {duplicated} 组重复")
        if con.execute(f"SELECT COUNT(*) FROM {table.table_id}").fetchone()[0] == 0:
            problems.append(f"表 {table.table_id} 没有数据")
    for description, sql in RECONCILE_CHECKS:
        mismatched = con.execute(sql).fetchone()[0]
        if mismatched:
            problems.append(f"{description}：有 {mismatched} 行不平")
    return problems


def fingerprint(mart_dir: Path, start: date, end: date, seed: int) -> str:
    """计算造数输入的指纹

    清单、加工脚本、造数程序、日期范围、随机种子任何一项变化，指纹都会变化。
    换行符统一后再计算，避免 git 转换换行符导致误判。
    """
    digest = hashlib.sha256(f"{start}|{end}|{seed}".encode("utf-8"))
    files = (sorted(mart_dir.glob("*.csv")) + sorted((mart_dir / SCRIPTS_DIR_NAME).glob("*.sql"))
             + [Path(__file__)])
    for path in files:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def build(db_path: Path = DEFAULT_DB_PATH, start: date = DEFAULT_START, end: date = DEFAULT_END,
          seed: int = DEFAULT_SEED, mart_dir: Path = MART_DIR) -> list[str]:
    """从零生成模拟专题库

    Args:
        db_path: 输出的 DuckDB 文件，已存在时先删除
        start: 起始数据日期
        end: 结束数据日期
        seed: 随机种子
        mart_dir: 模拟专题库的资料目录

    Returns:
        对平检核发现的问题，空列表表示全部通过

    Raises:
        ValueError: 资料目录里的清单没有通过导入校验
        RuntimeError: 加工脚本执行失败，或库文件被其它程序占用
    """
    mart, issues = load_mart(mart_dir)
    if mart is None:
        details = "；".join(issue.message for issue in issues)
        raise ValueError(f"资料没有通过导入校验，无法造数: {details}")

    db_path = Path(db_path)
    marker = db_path.with_name(db_path.name + FINGERPRINT_SUFFIX)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        db_path.unlink(missing_ok=True)      # 先删库：库被占用时报错，指纹还留着，下次不必无谓地重造
        marker.unlink(missing_ok=True)
    except PermissionError as error:
        raise RuntimeError(
            f"模拟专题库文件正被其它程序占用，请先关闭占用它的程序再重试: {db_path}") from error
    dates = date_range(start, end)

    con = duckdb.connect(str(db_path))
    try:
        con.execute("SET threads TO 1")     # 逐日跑批是大量小语句，单线程比多线程快
        create_tables(con, mart)
        generate_ods(con, dates, seed)
        run_scripts(con, mart_dir / SCRIPTS_DIR_NAME, dates)
        problems = verify(con, mart)
    finally:
        con.close()
    if not problems:
        marker.write_text(fingerprint(mart_dir, start, end, seed), encoding="utf-8")
    return problems


def ensure_built(db_path: Path = DEFAULT_DB_PATH, start: date = DEFAULT_START,
                 end: date = DEFAULT_END, seed: int = DEFAULT_SEED,
                 mart_dir: Path = MART_DIR) -> bool:
    """确保模拟专题库存在，并且与当前的清单、脚本、造数程序一致，否则重新生成

    Returns:
        True 表示本次重新生成了，False 表示已有的库可以直接用

    Raises:
        RuntimeError: 重新生成后对平检核未通过
    """
    db_path = Path(db_path)
    marker = db_path.with_name(db_path.name + FINGERPRINT_SUFFIX)
    current = fingerprint(mart_dir, start, end, seed)
    if db_path.exists() and marker.exists() and marker.read_text(encoding="utf-8") == current:
        return False
    problems = build(db_path, start, end, seed, mart_dir)
    if problems:
        raise RuntimeError("模拟专题库对平检核未通过: " + "；".join(problems))
    return True


def main() -> int:
    """命令行入口。对平检核全部通过返回 0，否则返回 1"""
    started = time.time()
    print(f"开始生成模拟专题库: {DEFAULT_DB_PATH}")
    print(f"数据日期 {DEFAULT_START} 至 {DEFAULT_END}，随机种子 {DEFAULT_SEED}")
    try:
        problems = build()
    except (ValueError, RuntimeError) as error:
        print(f"生成失败: {error}")
        return 1
    print(f"生成完成，用时 {time.time() - started:.1f} 秒")
    if problems:
        print("对平检核未通过：")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("对平检核全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
