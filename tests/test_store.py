"""元数据库的测试"""
import sqlite3
from contextlib import closing

import numpy as np
import pytest

from metadata.models import (
    ColumnMeta,
    Example,
    FieldRef,
    JoinKey,
    LineageEdge,
    Mart,
    Relation,
    ScriptColumn,
    ScriptFilter,
    ScriptJoin,
    ScriptStatement,
    TableMeta,
)
from metadata.store import MetadataStore, VectorBundle, VectorSet, write_store


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
                       data_type="varchar(20)", is_pk=True, enum_values="示例码值"),
        ],
        lineage=[
            LineageEdge(target_table="ads.cust_bal", target_column="bal", source_table="ods.acct",
                        source_column="bal", transform="SUM(ods.acct.bal)", script_name="load_cust_bal.sql",
                        origin="script"),
            LineageEdge(target_table="ods.cust", target_column="cust_no", source_table="src.cust_info",
                        source_column="cust_no", source_external=True, is_passthrough=True),
        ],
        relations=[
            Relation(left_table="ads.cust_bal", left_columns=["cust_no"],
                     right_table="ods.cust", right_columns=["cust_id"],
                     cardinality="N:1", origin="script", note="示例"),
        ],
        scripts=[
            ScriptStatement(
                script_name="load_cust_bal.sql", stmt_index=2, target_table="ads.cust_bal",
                sql_text="INSERT INTO ads.cust_bal ...", select_sql="SELECT ...", parsed=True,
                filters=[ScriptFilter(scope="main", clause="WHERE", condition="ods.acct.flag = '是'")],
                joins=[ScriptJoin(scope="main", join_type="LEFT", table="ods.cust",
                                  condition="ods.acct.cust_no = ods.cust.cust_no",
                                  keys=[JoinKey(left=FieldRef(table="ods.acct", column="cust_no"),
                                                right=FieldRef(table="ods.cust", column="cust_no"))])],
            ),
        ],
        script_columns=[
            ScriptColumn(script_name="load_cust_bal.sql", stmt_index=2, target_table="ads.cust_bal",
                         target_column="bal", expression="SUM(ods.acct.bal)",
                         sources=[FieldRef(table="ods.acct", column="bal")], kind="derived",
                         scopes=["main", "agg"]),
        ],
        examples=[
            Example(example_id="ex.sql#1", requirement="查各客户余额", caliber_note="原币",
                    sql_text="SELECT 1", tables=["ads.cust_bal"], source_file="ex.sql", line_no=1),
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
    assert store.scripts() == mart.scripts
    assert store.scripts(target_table="ods.acct") == []
    assert store.script_columns("ads.cust_bal") == mart.script_columns
    assert store.script_column("ads.cust_bal", "bal").sources == [FieldRef(table="ods.acct", column="bal")]
    assert store.script_column("ads.cust_bal", "no_such") is None
    assert store.examples() == mart.examples
    assert store.counts() == {"表": 3, "字段": 4, "血缘": 2, "关联关系": 1, "加工语句": 1, "样例": 1,
                              "字段向量": 0}


def test_from_mart_matches_reading_back(tmp_path):
    mart = make_mart()
    write_store(tmp_path / "m.db", mart)

    from_file = MetadataStore(tmp_path / "m.db")
    in_memory = MetadataStore.from_mart(mart)

    assert in_memory.db_path is None
    assert in_memory.tables() == from_file.tables()
    assert in_memory.columns("ods.acct") == from_file.columns("ods.acct")
    assert in_memory.lineage_edges() == from_file.lineage_edges()
    assert in_memory.relations() == from_file.relations()
    assert in_memory.scripts() == from_file.scripts()
    assert in_memory.counts() == from_file.counts()


def make_vectors() -> VectorBundle:
    """两条字段向量，维度 4"""
    matrix = np.array([[1, 0, 0, 0], [0, 0.6, 0.8, 0]], dtype=np.float32)
    return VectorBundle(embedder="test-4", dim=4, sets={
        "field": VectorSet(kind="field", ref_ids=["ods.acct.bal", "ods.cust.cust_no"],
                           texts=["账户表 余额 原币", "客户表 客户号"], matrix=matrix),
    })


def test_vectors_round_trip(tmp_path):
    write_store(tmp_path / "m.db", make_mart(), make_vectors())
    store = MetadataStore(tmp_path / "m.db")

    field = store.vectors("field")
    assert store.embedder_name == "test-4"
    assert field.ref_ids == ["ods.acct.bal", "ods.cust.cust_no"]
    assert field.texts[1] == "客户表 客户号"
    assert field.matrix.dtype == np.float32 and field.matrix.shape == (2, 4)
    assert np.array_equal(field.matrix, make_vectors().sets["field"].matrix)
    assert store.vectors("example") is None
    assert store.counts()["字段向量"] == 2


def test_store_without_vectors(tmp_path):
    write_store(tmp_path / "m.db", make_mart())
    store = MetadataStore(tmp_path / "m.db")

    assert store.vectors("field") is None
    assert store.embedder_name is None


def test_vectors_with_wrong_shape_are_rejected_and_keep_old_database(tmp_path):
    db_path = tmp_path / "m.db"
    write_store(db_path, make_mart(), make_vectors())
    broken = make_vectors()
    broken.sets["field"].texts.pop()

    with pytest.raises(ValueError, match="对不上"):
        write_store(db_path, make_mart(), broken)

    assert MetadataStore(db_path).counts()["字段向量"] == 2


def test_json_columns_keep_chinese_readable(tmp_path):
    write_store(tmp_path / "m.db", make_mart())

    with closing(sqlite3.connect(tmp_path / "m.db")) as conn:
        filters, = conn.execute("SELECT filters FROM meta_script").fetchone()

    assert "'是'" in filters                     # 中文没有被转成 \u 转义


def test_old_database_without_new_tables_tells_how_to_fix(tmp_path):
    db_path = tmp_path / "old.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE meta_table (table_id TEXT)")

    with pytest.raises(RuntimeError, match="重新运行 python -m metadata.importer"):
        MetadataStore(db_path)


def test_script_column_scopes_round_trip_and_old_databases_read_as_unknown(tmp_path):
    write_store(tmp_path / "m.db", make_mart())
    assert MetadataStore(tmp_path / "m.db").script_column("ads.cust_bal", "bal").scopes == ["main", "agg"]
    assert ScriptColumn(script_name="a.sql", stmt_index=1, target_table="t", target_column="c", expression="x",
                        kind="derived").scopes == []                       # 老资料没有这个字段，当作不知道

    with closing(sqlite3.connect(tmp_path / "m.db")) as conn:             # 加 scopes 列之前导出的库：没有这一列
        conn.executescript("""
            CREATE TABLE old_script_column AS
                SELECT script_name, stmt_index, target_table, target_column, expression, sources, kind
                FROM meta_script_column;
            DROP TABLE meta_script_column;
            ALTER TABLE old_script_column RENAME TO meta_script_column;
        """)
    (column,) = MetadataStore(tmp_path / "m.db").script_columns("ads.cust_bal")

    assert column.scopes == [] and column.expression == "SUM(ods.acct.bal)"


def test_columns_of_unlisted_table_do_not_break_the_store():
    mart = make_mart()
    mart.columns.append(ColumnMeta(table_id="ods.other", column_name="x", ordinal=1, column_cn="某字段"))

    store = MetadataStore.from_mart(mart)

    assert store.table("ods.other") is None
    assert [c.column_name for c in store.columns("ods.other")] == ["x"]


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


def test_database_uses_large_pages_to_hold_vectors(tmp_path):
    write_store(tmp_path / "m.db", make_mart(), make_vectors())

    with closing(sqlite3.connect(tmp_path / "m.db")) as conn:
        page_size, = conn.execute("PRAGMA page_size").fetchone()

    assert page_size == 16384
