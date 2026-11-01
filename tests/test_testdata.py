"""按元数据造测试数据：能关联上、守码值、名称和代码对得上、同样的种子造出同样的数据。表名都是虚构的。"""
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest

from metadata import testdata
from metadata.importer import default_db_path, run_import
from metadata.models import ColumnMeta, Mart, Relation, ScriptColumn, TableMeta
from metadata.store import MetadataStore

TODAY = date(2026, 11, 1)


def table(table_id: str, table_cn: str, grain: list[str], layer: str = "专题层") -> TableMeta:
    schema, name = table_id.split(".")
    return TableMeta(table_id=table_id, layer=layer, schema_name=schema, table_name=name, table_cn=table_cn,
                     table_type="全量表", grain_columns=grain)


def columns(table_id: str, *items: tuple) -> list[ColumnMeta]:
    """items 是 (字段名, 中文名, 类型, 码值说明)，后两项可省"""
    return [ColumnMeta(table_id=table_id, column_name=item[0], ordinal=number, column_cn=item[1],
                       data_type=item[2] if len(item) > 2 else "varchar(50)", enum_values=item[3] if len(item) > 3 else "")
            for number, item in enumerate(items, 1)]


@pytest.fixture
def store() -> MetadataStore:
    project = "ads.project"
    kind = "dwd.project_kind"
    return MetadataStore.from_mart(Mart(
        tables=[table(project, "项目表", ["project_id"]), table(kind, "项目类型表", ["kind_code"], "标准层")],
        columns=columns(project, ("project_id", "项目ID"), ("kind_code", "项目类型编码"), ("kind_name", "项目类型名称"),
                        ("status_code", "状态编码", "varchar(10)", "1=进行中;2=已完成"), ("status_name", "状态名称"),
                        ("amount", "项目经费（元）", "numeric(18,2)"), ("start_date", "项目开始日期", "varchar(10)"),
                        ("create_time", "创建时间", "timestamp"), ("budget_year", "预算年度", "integer"),
                        ("is_deleted", "删除标记", "smallint"), ("leader", "项目负责人"), ("reserved", "备用字段"))
        + columns(kind, ("kind_code", "项目类型编码"), ("kind_name", "项目类型名称")),
        relations=[Relation(left_table=project, left_columns=["kind_code"], right_table=kind, right_columns=["kind_code"],
                            cardinality="N:1")],
        script_columns=[ScriptColumn(script_name="s.sql", stmt_index=1, target_table=project, target_column="reserved",
                                     expression="NULL", kind="null")]))


@pytest.fixture
def db(store: MetadataStore, tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "test_data.db"
    summary = testdata.build(store, path, seed=7, today=TODAY)
    assert summary == {"tables": 2, "rows": 400}          # 专题层 300 行、标准层 100 行
    with closing(sqlite3.connect(path)) as connection:
        yield connection


def values(db: sqlite3.Connection, sql: str) -> list:
    return [row[0] for row in db.execute(sql)]


def test_related_columns_share_keys_so_joins_find_rows(db):
    matched = values(db, 'SELECT COUNT(*) FROM "ads.project" p JOIN "dwd.project_kind" k ON p.kind_code = k.kind_code')[0]
    assert matched == 300                                 # 每个外键都在主键表里，主键不重复
    assert values(db, 'SELECT COUNT(DISTINCT project_id) FROM "ads.project"') == [300]


def test_category_keys_keep_few_values_and_names_follow_the_code(db):
    assert values(db, 'SELECT COUNT(DISTINCT kind_code) FROM "ads.project"') == [testdata.SMALL_CATEGORIES]
    pairs = db.execute('SELECT DISTINCT kind_code, kind_name FROM "ads.project"').fetchall()
    assert len(pairs) == testdata.SMALL_CATEGORIES and all(name.startswith("项目类型") for _, name in pairs)


def test_enum_codes_and_their_names_are_used(db):
    assert set(db.execute('SELECT status_code, status_name FROM "ads.project" WHERE status_code IS NOT NULL')) == {
        ("1", "进行中"), ("2", "已完成")}


def test_values_follow_the_meaning_of_the_column(db):
    assert set(values(db, 'SELECT budget_year FROM "ads.project" WHERE budget_year IS NOT NULL')) <= {2024, 2025, 2026}
    assert set(values(db, 'SELECT is_deleted FROM "ads.project" WHERE is_deleted IS NOT NULL')) == {0}
    days = values(db, 'SELECT start_date FROM "ads.project" WHERE start_date IS NOT NULL')
    assert all("2023-11-01" <= day <= "2026-11-01" for day in days)
    assert all(len(text) == 19 for text in values(db, 'SELECT create_time FROM "ads.project" WHERE create_time IS NOT NULL'))
    assert all(isinstance(amount, float) and amount > 0 for amount in values(db, 'SELECT amount FROM "ads.project" WHERE amount IS NOT NULL'))


def test_columns_always_null_in_the_script_stay_empty_and_others_have_some_nulls(db):
    assert values(db, 'SELECT COUNT(reserved) FROM "ads.project"') == [0]
    assert 0 < values(db, 'SELECT COUNT(*) FROM "ads.project" WHERE leader IS NULL')[0] < 60


def test_the_note_says_this_is_not_business_data(db):
    assert testdata.NOTE in values(db, f"SELECT value FROM {testdata.INFO_TABLE} WHERE key = 'note'")[0]


def test_snapshot_dates_are_month_ends(tmp_path):
    snapshot = table("ads.balance", "余额表", ["cust_id"]).model_copy(update={"date_column": "data_dt"})
    store = MetadataStore.from_mart(Mart(tables=[snapshot], columns=columns("ads.balance", ("cust_id", "客户号"), ("data_dt", "数据日期", "date"))))
    testdata.build(store, tmp_path / "t.db", today=TODAY)
    with closing(sqlite3.connect(tmp_path / "t.db")) as connection:
        days = set(values(connection, 'SELECT data_dt FROM "ads.balance" WHERE data_dt IS NOT NULL'))
    assert "2026-10-31" in days and "2026-09-30" in days and all(day >= "2025-11-30" for day in days)


def test_same_seed_gives_same_data_and_another_seed_differs(store, tmp_path):
    def rows(name: str, seed: int) -> list:
        path = tmp_path / name
        testdata.build(store, path, seed=seed, today=TODAY)
        with closing(sqlite3.connect(path)) as connection:
            return connection.execute('SELECT * FROM "ads.project"').fetchall()

    assert rows("a.db", 1) == rows("b.db", 1)
    assert rows("a.db", 1) != rows("c.db", 2)


def test_command_writes_next_to_the_metadata(tiny_mart, capsys):
    run_import(tiny_mart, build_index=False)
    assert testdata.main([str(tiny_mart), "--today", "2026-11-01"]) == 0
    assert testdata.default_path(tiny_mart).is_file() and testdata.NOTE in capsys.readouterr().out
    assert MetadataStore(default_db_path(tiny_mart)).tables()


def test_command_exit_codes(tmp_path, capsys):
    assert testdata.main([str(tmp_path), "--today", "昨天"]) == 2
    assert testdata.main([str(tmp_path / "absent")]) == 1
    assert "错误" in capsys.readouterr().err
