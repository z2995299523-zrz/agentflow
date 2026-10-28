"""代码字段与名称字段的测试：名称字段的配对、码值表索引、码值说明的解析、中文名称的判断"""
import pytest

from metadata.codes import code_tables, is_chinese_name, is_code_column, name_column, parse_enum
from metadata.models import ColumnMeta, TableMeta


def col(name: str, cn: str, enum: str = "", table: str = "dim.t") -> ColumnMeta:
    """一个字段"""
    return ColumnMeta(table_id=table, column_name=name, ordinal=1, column_cn=cn, enum_values=enum)


def table(table_id: str, grain: list[str], table_type: str = "维表", layer: str = "标准层") -> TableMeta:
    """一张表"""
    schema, name = table_id.split(".")
    return TableMeta(table_id=table_id, layer=layer, schema_name=schema, table_name=name, table_cn=name,
                     table_type=table_type, grain_columns=grain)


@pytest.mark.parametrize("name, cn, expected", [
    ("industry_cd", "行业代码", True), ("prod_code", "产品", True), ("cust_no", "客户号", True),
    ("project_id", "项目ID", True), ("subj_no", "科目编号", True), ("org_x", "机构编码", True),
    ("industry_nm", "行业名称", False), ("bal_rmb", "余额", False), ("no", "号", False),
])
def test_code_columns_are_told_by_the_suffix_of_either_name(name, cn, expected):
    assert is_code_column(col(name, cn)) is expected


def test_the_name_column_is_found_by_the_chinese_name_first():
    columns = [col("industry_cd", "行业代码"), col("industry_name", "行业简称"), col("ind_nm", "行业名称")]

    assert name_column(columns[0], columns) == "ind_nm"


def test_the_name_column_is_found_by_the_english_name_when_the_chinese_names_do_not_pair():
    columns = [col("lvl1_org_no", "一级分行机构号"), col("lvl1_org_nm", "一级分行名称")]

    assert name_column(columns[0], columns) == "lvl1_org_nm"


def test_no_name_column_when_the_table_has_none_or_the_column_is_not_a_code():
    columns = [col("industry_cd", "行业代码"), col("bal_rmb", "余额"), col("bal_nm", "余额名称")]

    assert name_column(columns[0], columns) == ""
    assert name_column(columns[1], columns) == ""


def test_code_tables_are_dimensions_with_one_grain_column_that_has_a_name_in_the_table():
    tables = [table("ods.industry_dict", ["industry_cd"], layer="贴源层"), table("dim.org", ["org_no"]),
              table("ods.org_info", ["org_no"], layer="贴源层"), table("dim.pair", ["a_cd", "b_cd"]),
              table("ads.bal", ["org_no"], table_type="快照表", layer="专题层"), table("dim.bare", ["bare_cd"])]
    columns = {
        "ods.industry_dict": [col("industry_cd", "行业代码"), col("industry_nm", "行业名称")],
        "dim.org": [col("org_no", "机构号"), col("org_nm", "机构名称")],
        "ods.org_info": [col("org_no", "机构号"), col("org_nm", "机构名称")],
        "dim.pair": [col("a_cd", "甲代码"), col("a_nm", "甲名称"), col("b_cd", "乙代码")],
        "ads.bal": [col("org_no", "机构号"), col("org_nm", "机构名称")],
        "dim.bare": [col("bare_cd", "某代码")],
    }

    index = code_tables(tables, lambda table_id: columns[table_id])

    assert index == {"industry_cd": [("ods.industry_dict", "industry_nm")],
                     "org_no": [("dim.org", "org_nm"), ("ods.org_info", "org_nm")]}     # 标准层的排在贴源层前面


@pytest.mark.parametrize("text, expected", [
    ("1=大型;2=中型;3=小型", {"1": "大型", "2": "中型", "3": "小型"}),
    ("01：正常，02：销户", {"01": "正常", "02": "销户"}),
    ("0=否、1=是\n9: 未知", {"0": "否", "1": "是", "9": "未知"}),
    ("国民经济行业门类", {}),
    ("", {}),
])
def test_enum_values_written_as_code_and_name_are_parsed(text, expected):
    assert parse_enum(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("制造业", True), (" 上海分行 ", True), ("（2026）沪银借字第001号", False), ("C", False), ("01", False),
    ("制造业A", False), ("", False),
])
def test_only_pure_chinese_text_counts_as_a_name(text, expected):
    assert is_chinese_name(text) is expected
