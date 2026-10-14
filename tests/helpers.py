"""
测试辅助：一套最小的合法资料，以及把清单写成资料目录的工具
"""
import csv
from pathlib import Path

from metadata.template import SHEETS

# 最小合法资料：一张贴源层账户表加工出一张专题层汇总表，另有一张客户维表
TINY_MART: dict[str, list[list[str]]] = {
    "表清单": [
        ["贴源层", "ods", "acct", "账户表", "快照表", "data_dt,acct_no", "data_dt", "账户每日快照"],
        ["专题层", "ads", "cust_bal", "客户余额汇总", "快照表", "data_dt,cust_no", "data_dt", ""],
        ["贴源层", "ods", "cust", "客户表", "维表", "cust_no", "", ""],
    ],
    "字段清单": [
        ["ods.acct", "data_dt", "数据日期", "date", "是", "", ""],
        ["ods.acct", "acct_no", "账号", "varchar(32)", "是", "", ""],
        ["ods.acct", "cust_no", "客户号", "varchar(20)", "", "", ""],
        ["ods.acct", "bal", "余额", "numeric(18,2)", "", "原币", ""],
        ["ads.cust_bal", "data_dt", "数据日期", "date", "是", "", ""],
        ["ads.cust_bal", "cust_no", "客户号", "varchar(20)", "是", "", ""],
        ["ads.cust_bal", "bal", "余额", "numeric(18,2)", "", "", ""],
        ["ods.cust", "cust_no", "客户号", "varchar(20)", "是", "", ""],
        ["ods.cust", "cust_nm", "客户名称", "varchar(100)", "", "", ""],
    ],
    "血缘清单": [
        ["ads.cust_bal", "data_dt", "ods.acct", "data_dt", "", "load_cust_bal.sql"],
        ["ads.cust_bal", "cust_no", "ods.acct", "cust_no", "", "load_cust_bal.sql"],
        ["ads.cust_bal", "bal", "ods.acct", "bal", "", "load_cust_bal.sql"],
    ],
    "关联关系": [
        ["ads.cust_bal", "cust_no", "ods.cust", "cust_no", "N:1", "客户余额关联客户表"],
    ],
}


def write_mart(mart_dir: Path, sheets: dict[str, list[list[str]]]) -> Path:
    """把清单内容写成带表头的 CSV 文件，返回资料目录

    sheets 里没有的清单不生成文件，用来模拟清单缺失。
    """
    mart_dir.mkdir(parents=True, exist_ok=True)
    headers = {spec.name: [column.header for column in spec.columns] for spec in SHEETS}
    for name, rows in sheets.items():
        with (mart_dir / f"{name}.csv").open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(headers[name])
            writer.writerows(rows)
    return mart_dir
