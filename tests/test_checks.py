"""导入校验规则的测试：合法资料不报任何问题，每条规则至少有一个反例"""
import pytest

from metadata.checks import ERROR, WARNING, Issue, check_mart, split_list
from metadata.loader import load_raw
from tests.helpers import write_mart


def run_checks(tmp_path, sheets) -> list[Issue]:
    """把清单写成资料目录并执行校验"""
    return check_mart(load_raw(write_mart(tmp_path / "m", sheets)))


def rules(issues: list[Issue]) -> list[tuple[str, str]]:
    """提取（级别, 规则编号）并去重排序，便于断言"""
    return sorted({(issue.level, issue.rule) for issue in issues})


def test_valid_mart_has_no_issues(tmp_path, tiny_sheets):
    assert run_checks(tmp_path, tiny_sheets) == []


def test_split_list_accepts_chinese_comma_and_spaces():
    assert split_list(" data_dt， acct_no ,") == ["data_dt", "acct_no"]


# ── 每个函数把合法资料改坏一处 ──────────────────────────────

def missing_sheet(s):
    del s["血缘清单"]


def empty_required_cell(s):
    s["表清单"][0][3] = ""


def duplicate_table(s):
    s["表清单"].append(list(s["表清单"][0]))


def duplicate_column(s):
    s["字段清单"].append(list(s["字段清单"][3]))


def bad_layer(s):
    s["表清单"][0][0] = "明细层"


def bad_table_type(s):
    s["表清单"][0][4] = "事实表"


def bad_standard_mark(s):
    s["表清单"][0].append("不是")


def table_without_columns(s):
    s["表清单"].append(["贴源层", "ods", "empty", "空表", "维表", "id", "", ""])


def grain_not_a_column(s):
    s["表清单"][0][5] = "data_dt,no_such"


def date_not_a_column(s):
    s["表清单"][0][6] = "biz_dt"


def snapshot_without_date(s):
    s["表清单"][1][6] = ""


def lineage_target_column_missing(s):
    s["血缘清单"].append(["ads.cust_bal", "no_such", "ods.acct", "bal", "", ""])


def lineage_target_table_missing(s):
    s["血缘清单"].append(["ads.no_such", "bal", "ods.acct", "bal", "", ""])


def lineage_source_column_missing(s):
    s["血缘清单"].append(["ads.cust_bal", "bal", "ods.acct", "no_such", "", ""])


def relation_table_missing(s):
    s["关联关系"][0][2] = "dim.no_such"


def relation_column_missing(s):
    s["关联关系"][0][3] = "no_such"


def relation_bad_cardinality(s):
    s["关联关系"][0][4] = "多对一"


def relation_column_count_mismatch(s):
    s["关联关系"][0][1] = "cust_no,data_dt"


def column_of_unknown_table(s):
    s["字段清单"].append(["ods.no_such", "id", "编号", "integer", "", "", ""])


def external_source(s):
    s["血缘清单"].append(["ods.cust", "cust_nm", "src.cust_info", "cust_nm", "", ""])


def lineage_cycle(s):
    s["血缘清单"].append(["ods.acct", "bal", "ads.cust_bal", "bal", "", ""])


def downstream_table_without_lineage(s):
    s["血缘清单"].clear()


def table_without_grain(s):
    s["表清单"][2][5] = ""


def column_without_type(s):
    s["字段清单"][8][3] = ""


CASES = [
    (missing_sheet, (ERROR, "M01")),
    (empty_required_cell, (ERROR, "M02")),
    (duplicate_table, (ERROR, "M03")),
    (duplicate_column, (ERROR, "M04")),
    (bad_layer, (ERROR, "M05")),
    (bad_table_type, (ERROR, "M05")),
    (bad_standard_mark, (ERROR, "M05")),
    (table_without_columns, (ERROR, "M06")),
    (grain_not_a_column, (ERROR, "M07")),
    (date_not_a_column, (ERROR, "M07")),
    (snapshot_without_date, (ERROR, "M08")),
    (lineage_target_column_missing, (ERROR, "M09")),
    (lineage_target_table_missing, (ERROR, "M09")),
    (lineage_source_column_missing, (ERROR, "M10")),
    (relation_table_missing, (ERROR, "M11")),
    (relation_column_missing, (ERROR, "M11")),
    (relation_bad_cardinality, (ERROR, "M12")),
    (relation_column_count_mismatch, (ERROR, "M12")),
    (column_of_unknown_table, (ERROR, "M13")),
    (external_source, (WARNING, "W01")),
    (lineage_cycle, (WARNING, "W02")),
    (downstream_table_without_lineage, (WARNING, "W03")),
    (table_without_grain, (WARNING, "W04")),
    (column_without_type, (WARNING, "W05")),
]


@pytest.mark.parametrize("mutate, expected", CASES, ids=[mutate.__name__ for mutate, _ in CASES])
def test_single_violation_reports_exactly_its_rule(tmp_path, tiny_sheets, mutate, expected):
    mutate(tiny_sheets)

    assert rules(run_checks(tmp_path, tiny_sheets)) == [expected]


def test_missing_required_column_is_m01(tmp_path, tiny_sheets):
    mart_dir = write_mart(tmp_path / "m", tiny_sheets)
    (mart_dir / "字段清单.csv").write_text(
        "表名,字段英文名,数据类型\nods.acct,bal,numeric\n", encoding="utf-8")

    issues = check_mart(load_raw(mart_dir))

    assert [(i.level, i.rule, i.sheet) for i in issues] == [(ERROR, "M01", "字段清单")]
    assert "字段中文名" in issues[0].message


def test_structure_errors_stop_further_checks(tmp_path, tiny_sheets):
    del tiny_sheets["血缘清单"]
    bad_layer(tiny_sheets)          # 结构不完整时，这个问题暂不报告

    assert rules(run_checks(tmp_path, tiny_sheets)) == [(ERROR, "M01")]


def test_issue_points_at_sheet_and_row(tmp_path, tiny_sheets):
    bad_layer(tiny_sheets)

    issue = run_checks(tmp_path, tiny_sheets)[0]

    assert (issue.sheet, issue.row_no) == ("表清单", 2)
    assert "明细层" in issue.message and "贴源层、整合层、专题层" in issue.message


def test_external_sources_are_grouped_per_table(tmp_path, tiny_sheets):
    tiny_sheets["血缘清单"] += [
        ["ods.cust", "cust_no", "src.cust_info", "cust_no", "", ""],
        ["ods.cust", "cust_nm", "src.cust_info", "cust_nm", "", ""],
    ]

    issues = run_checks(tmp_path, tiny_sheets)

    assert len(issues) == 1
    assert "src.cust_info" in issues[0].message and "2 条" in issues[0].message


def test_cycle_message_shows_the_path(tmp_path, tiny_sheets):
    lineage_cycle(tiny_sheets)

    message = run_checks(tmp_path, tiny_sheets)[0].message

    assert "ods.acct.bal" in message and "ads.cust_bal.bal" in message and "→" in message


# ── 表名没带模式名：最容易出现的填写错误，必须明确报错并给出正确写法 ──

def bare_table_name_in_columns(s):
    for row in s["字段清单"]:
        if row[0] == "ads.cust_bal":
            row[0] = "cust_bal"


def bare_table_name_in_lineage_source(s):
    for row in s["血缘清单"]:
        row[2] = "acct"


def bare_table_name_in_lineage_target(s):
    for row in s["血缘清单"]:
        row[0] = "cust_bal"


def bare_table_name_in_relations(s):
    s["关联关系"][0][2] = "cust"


BARE_NAME_CASES = [bare_table_name_in_columns, bare_table_name_in_lineage_source,
                   bare_table_name_in_lineage_target, bare_table_name_in_relations]


@pytest.mark.parametrize("mutate", BARE_NAME_CASES, ids=[m.__name__ for m in BARE_NAME_CASES])
def test_bare_table_name_is_one_clear_error_without_follow_on_noise(tmp_path, tiny_sheets, mutate):
    mutate(tiny_sheets)

    assert rules(run_checks(tmp_path, tiny_sheets)) == [(ERROR, "M14")]


def test_bare_table_name_message_gives_the_full_name_once(tmp_path, tiny_sheets):
    bare_table_name_in_columns(tiny_sheets)

    issues = run_checks(tmp_path, tiny_sheets)

    assert len(issues) == 1                                   # 三行同一个表名只报一次
    assert (issues[0].sheet, issues[0].row_no) == ("字段清单", 6)
    assert "应写成 ads.cust_bal" in issues[0].message and "涉及 3 行" in issues[0].message


def test_bare_lineage_source_is_not_mistaken_for_external_source(tmp_path, tiny_sheets):
    bare_table_name_in_lineage_source(tiny_sheets)

    issues = run_checks(tmp_path, tiny_sheets)

    assert [issue.rule for issue in issues] == ["M14"]        # 不是 W01 外部源
    assert "应写成 ods.acct" in issues[0].message


def test_bare_name_matching_several_schemas_lists_the_candidates(tmp_path, tiny_sheets):
    tiny_sheets["表清单"].append(["整合层", "dim", "cust", "客户维表", "维表", "cust_no", "", ""])
    tiny_sheets["字段清单"].append(["dim.cust", "cust_no", "客户号", "varchar(20)", "是", "", ""])
    tiny_sheets["血缘清单"].append(["dim.cust", "cust_no", "cust", "cust_no", "", ""])

    issues = run_checks(tmp_path, tiny_sheets)

    assert [issue.rule for issue in issues] == ["M14"]
    assert "ods.cust、dim.cust" in issues[0].message


def test_references_in_other_case_still_match(tmp_path, tiny_sheets):
    for row in tiny_sheets["血缘清单"]:
        row[0], row[1], row[2], row[3] = row[0].upper(), row[1].upper(), row[2].upper(), row[3].upper()
    tiny_sheets["关联关系"][0][0] = "ADS.Cust_Bal"

    assert run_checks(tmp_path, tiny_sheets) == []


# ── 真实资料里常见的不完整：允许，但要提醒 ──

def test_full_refresh_table_type_is_accepted(tmp_path, tiny_sheets):
    tiny_sheets["表清单"][2][4] = "全量表"

    assert run_checks(tmp_path, tiny_sheets) == []


def test_untyped_columns_are_counted_per_table(tmp_path, tiny_sheets):
    for row in tiny_sheets["字段清单"]:
        if row[0] == "ods.acct":
            row[3] = ""

    issues = run_checks(tmp_path, tiny_sheets)

    assert [issue.rule for issue in issues] == ["W05"]       # 四个字段合并成一条提醒
    assert "ods.acct" in issues[0].message and "4 个字段" in issues[0].message


def test_tables_without_grain_are_summarised_per_layer(tmp_path, tiny_sheets):
    tiny_sheets["表清单"][0][5] = ""        # 贴源层 ods.acct
    tiny_sheets["表清单"][2][5] = ""        # 贴源层 ods.cust
    tiny_sheets["表清单"][1][5] = ""        # 专题层 ads.cust_bal

    issues = run_checks(tmp_path, tiny_sheets)

    assert [issue.rule for issue in issues] == ["W04", "W04"]       # 每个层级一条，而不是每张表一条
    messages = {issue.message.split("有")[0]: issue.message for issue in issues}
    assert "2 张表" in messages["贴源层"] and "ods.acct、ods.cust" in messages["贴源层"]
    assert "1 张表" in messages["专题层"]


def test_empty_table_list_is_m01(tmp_path, tiny_sheets):
    # 只有表头的清单（例如刚生成的空白模板）不能「导入成功」，否则会把原有的元数据库清空
    tiny_sheets["表清单"] = []

    issues = run_checks(tmp_path, tiny_sheets)

    assert [(i.level, i.rule, i.sheet) for i in issues] == [(ERROR, "M01", "表清单")]
