"""模拟专题库资料的测试：四张清单能通过导入校验，内容符合设计"""
from collections import Counter
from pathlib import Path

from metadata.importer import load_mart

MOCK_MART = Path(__file__).parent.parent / "data" / "mock_mart"
UTF8_BOM = b"\xef\xbb\xbf"


def test_mock_mart_passes_every_check():
    mart, issues = load_mart(MOCK_MART)

    assert issues == []
    assert mart is not None


def test_mock_mart_size():
    mart, _ = load_mart(MOCK_MART)

    assert len(mart.tables) == 14
    assert len(mart.columns) == 93
    assert len(mart.lineage) == 70
    assert len(mart.relations) == 1


def test_layers_map_to_schemas():
    mart, _ = load_mart(MOCK_MART)

    assert Counter(table.layer for table in mart.tables) == {"贴源层": 7, "整合层": 4, "专题层": 3}
    assert {table.schema_name for table in mart.tables if table.layer == "贴源层"} == {"ods"}
    assert {table.schema_name for table in mart.tables if table.layer == "整合层"} == {"dwd", "dim"}
    assert {table.schema_name for table in mart.tables if table.layer == "专题层"} == {"ads"}


def test_csv_files_have_bom_so_excel_opens_them_correctly():
    files = list(MOCK_MART.glob("*.csv"))

    assert {path.name for path in files} == {"表清单.csv", "字段清单.csv", "血缘清单.csv", "关联关系.csv"}
    for path in files:
        assert path.read_bytes().startswith(UTF8_BOM), path.name


def test_lineage_scripts_all_exist():
    mart, _ = load_mart(MOCK_MART)
    script_files = {path.name for path in (MOCK_MART / "scripts").glob("*.sql")}

    assert len(script_files) == 7
    assert {edge.script_name for edge in mart.lineage} == script_files


def test_lineage_has_no_transform_text_like_the_real_one():
    """真实的血缘清单是字段级、不带加工逻辑的，模拟资料保持一致"""
    mart, _ = load_mart(MOCK_MART)

    assert all(edge.transform == "" for edge in mart.lineage)
    assert all(not edge.source_external for edge in mart.lineage)


def test_caliber_difference_lives_only_in_the_script():
    """口径埋点：两个存款余额字段的口径说明一样，区别只写在加工脚本里"""
    mart, _ = load_mart(MOCK_MART)
    described = {column.column_name: column.description for column in mart.columns
                 if column.table_id == "ads.corp_cust_dep_bal_d"}
    script = (MOCK_MART / "scripts" / "21_ads_corp_cust_dep_bal_d.sql").read_text(encoding="utf-8")

    assert described["dep_bal_rmb"] == described["dep_bal_tot_rmb"] == "折人民币"
    assert "SUM(CASE WHEN a.is_margin = '0' THEN a.bal_rmb ELSE 0 END) AS dep_bal_rmb" in script
    assert "SUM(a.bal_rmb) AS dep_bal_tot_rmb" in script
