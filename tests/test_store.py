"""元数据库的测试"""
import sqlite3
from contextlib import closing

import pytest

from metadata.models import ColumnMeta, LineageEdge, Mart, Relation, TableMeta
from metadata.store import MetadataStore, write_store


def make_mart() -> Mart:
    """手工构造一套小资料，不依赖导入流程"""
    return Mart(
        tables=[
            TableMeta(table_id="ods.acct", layer="贴源层", schema_name="ods", table_name="acct",
                      table_cn="账户表", table_type="快照表", grain_columns=["data_dt", "acct_no"],
                      date_column="data_dt", description="账户每日快照"),
            TableMeta(table_id="ads.cust_bal", layer="专题层", schema_name="ads", table_name="cust_bal",
                      table_cn="客户余额汇总", table_type="快照表", grain_columns=["data_dt", "cust_no"],
                      date_column="data_dt"),
            TableMeta(table_id="ods.cust", layer="贴源层", schema_name="ods", table_name="cust",
                      table_cn="客户表", table_type="维表", grain_columns=["cust_no"]),
        ],
        columns=[
            ColumnMeta(table_id="ods.acct", column_name="data_dt", ordinal=1, column_cn="数据日期",
                       data_type="date", is_pk=True),
            ColumnMeta(table_id="ods.acct", column_name="bal", ordinal=2, column_cn="余额",
                       data_type="numeric(18,2)", description="原币", enum_values=""),
            ColumnMeta(table_id="ads.cust_bal", column_name="bal", ordinal=1, column_cn="余额",
                       data_type="numeric(18,2)"),
            ColumnMeta(table_id="ods.cust", column_name="cust_no", ordinal=1, column_cn="客户号",
                       data_type="varchar(20)", is_pk=True),
        ],
        lineage=[
            LineageEdge(target_table="ads.cust_bal", target_column="bal", source_table="ods.acct",
                        source_column="bal", script_name="load_cust_bal.sql"),
            LineageEdge(target_table="ods.cust", target_column="cust_no", source_table="src.cust_info",
                        source_column="cust_no", source_external=True),
        ],
        relations=[
            Relation(left_table="ads.cust_bal", left_columns=["data_dt", "cust_no"],
                     right_table="ods.cust", right_columns=["data_dt", "cust_no"],
                     cardinality="N:1", note="示例"),
        ],
    )


def test_round_trip(tmp_path):
    mart = make_mart()
    db_path = tmp_path / "build" / "metadata.db"     # 目录不存在时自动创建

    write_store(db_path, mart)
    store = MetadataStore(db_path)

    assert store.tables() == mart.tables
    assert store.columns("ods.acct") == mart.columns[:2]
    assert store.lineage_edges() == mart.lineage
    assert store.relations() == mart.relations
    assert store.counts() == {"表": 3, "字段": 4, "血缘": 2, "关联关系": 1}


def test_lookups(tmp_path):
    write_store(tmp_path / "m.db", make_mart())
    store = MetadataStore(tmp_path / "m.db")

    assert [t.table_id for t in store.tables(layer="专题层")] == ["ads.cust_bal"]
    assert store.table("ads.cust_bal").grain_columns == ["data_dt", "cust_no"]
    assert store.table("ods.cust").date_column is None
    assert store.table("no.such") is None
    assert store.column("ods.acct", "bal").description == "原币"
    assert store.column("ods.acct", "no_such") is None
    assert store.columns("no.such") == []


def test_readable_with_plain_sql(tmp_path):
    write_store(tmp_path / "m.db", make_mart())

    with closing(sqlite3.connect(tmp_path / "m.db")) as conn:
        rows = conn.execute(
            "SELECT table_id, grain_columns FROM meta_table WHERE layer = '专题层'").fetchall()

    assert rows == [("ads.cust_bal", "data_dt,cust_no")]


def test_missing_database_tells_how_to_create_it(tmp_path):
    with pytest.raises(FileNotFoundError, match="metadata.importer"):
        MetadataStore(tmp_path / "nope.db")


def test_failed_write_keeps_existing_database(tmp_path):
    db_path = tmp_path / "m.db"
    write_store(db_path, make_mart())
    broken = make_mart()
    broken.columns.append(broken.columns[0].model_copy())     # 主键重复，写库会失败

    with pytest.raises(sqlite3.IntegrityError):
        write_store(db_path, broken)

    assert MetadataStore(db_path).counts()["字段"] == 4        # 旧库完好
    assert not (tmp_path / "m.db.tmp").exists()               # 临时文件已清理


def test_rewrite_replaces_previous_content(tmp_path):
    db_path = tmp_path / "m.db"
    write_store(db_path, make_mart())
    smaller = make_mart()
    smaller.relations.clear()

    write_store(db_path, smaller)

    assert MetadataStore(db_path).counts()["关联关系"] == 0


def test_locked_database_gives_readable_error_and_keeps_old_content(tmp_path, monkeypatch):
    db_path = tmp_path / "m.db"
    write_store(db_path, make_mart())

    def refuse(source, target):
        raise PermissionError("文件被占用")

    monkeypatch.setattr("metadata.store.os.replace", refuse)
    smaller = make_mart()
    smaller.relations.clear()

    with pytest.raises(RuntimeError, match="正被其它程序占用"):
        write_store(db_path, smaller)

    assert MetadataStore(db_path).counts()["关联关系"] == 1
    assert not (tmp_path / "m.db.tmp").exists()


def test_empty_grain_reads_back_as_empty_list(tmp_path):
    mart = make_mart()
    mart.tables[2].grain_columns = []
    mart.columns[1].data_type = ""

    write_store(tmp_path / "m.db", mart)
    store = MetadataStore(tmp_path / "m.db")

    assert store.table("ods.cust").grain_columns == []
    assert store.column("ods.acct", "bal").data_type == ""
