"""
元数据库
把一套资料落成 SQLite 关系表，可以直接用 SQL 查看。其它模块通过 MetadataStore 只读访问
"""
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from metadata.models import ColumnMeta, LineageEdge, Mart, Relation, TableMeta

SCHEMA_SQL = """
CREATE TABLE meta_table (
    table_id      TEXT PRIMARY KEY,
    layer         TEXT NOT NULL,
    schema_name   TEXT NOT NULL,
    table_name    TEXT NOT NULL,
    table_cn      TEXT NOT NULL,
    table_type    TEXT NOT NULL,
    grain_columns TEXT NOT NULL,
    date_column   TEXT,
    description   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE meta_column (
    table_id    TEXT NOT NULL,
    column_name TEXT NOT NULL,
    ordinal     INTEGER NOT NULL,
    column_cn   TEXT NOT NULL,
    data_type   TEXT NOT NULL,
    is_pk       INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    enum_values TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (table_id, column_name)
);
CREATE TABLE meta_lineage (
    target_table    TEXT NOT NULL,
    target_column   TEXT NOT NULL,
    source_table    TEXT NOT NULL,
    source_column   TEXT NOT NULL,
    transform       TEXT NOT NULL DEFAULT '',
    script_name     TEXT NOT NULL DEFAULT '',
    source_external INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE meta_relation (
    left_table    TEXT NOT NULL,
    left_columns  TEXT NOT NULL,
    right_table   TEXT NOT NULL,
    right_columns TEXT NOT NULL,
    cardinality   TEXT NOT NULL,
    origin        TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT ''
);
"""


def write_store(db_path: Path, mart: Mart) -> None:
    """全量重建元数据库

    先写临时文件，全部成功后再替换正式文件。中途失败时正式文件不受影响。

    Args:
        db_path: 元数据库文件路径
        mart: 校验通过的资料

    Raises:
        RuntimeError: 元数据库文件被其它程序占用，无法替换
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_name(db_path.name + ".tmp")
    tmp_path.unlink(missing_ok=True)

    try:
        with closing(sqlite3.connect(tmp_path)) as conn:
            conn.executescript(SCHEMA_SQL)
            conn.executemany(
                "INSERT INTO meta_table VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(t.table_id, t.layer, t.schema_name, t.table_name, t.table_cn, t.table_type,
                  ",".join(t.grain_columns), t.date_column, t.description) for t in mart.tables],
            )
            conn.executemany(
                "INSERT INTO meta_column VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(c.table_id, c.column_name, c.ordinal, c.column_cn, c.data_type, int(c.is_pk),
                  c.description, c.enum_values) for c in mart.columns],
            )
            conn.executemany(
                "INSERT INTO meta_lineage VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(e.target_table, e.target_column, e.source_table, e.source_column, e.transform,
                  e.script_name, int(e.source_external)) for e in mart.lineage],
            )
            conn.executemany(
                "INSERT INTO meta_relation VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(r.left_table, ",".join(r.left_columns), r.right_table, ",".join(r.right_columns),
                  r.cardinality, r.origin, r.note) for r in mart.relations],
            )
            conn.commit()
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    try:
        os.replace(tmp_path, db_path)
    except PermissionError as error:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"元数据库文件正被其它程序占用，请先关闭占用它的程序再重试: {db_path}") from error


class MetadataStore:
    """元数据库的只读访问入口

    构造时把内容全部读进内存，之后不再占用数据库文件，重新导入不会被它挡住。
    """

    def __init__(self, db_path: Path) -> None:
        """加载元数据库

        Args:
            db_path: 元数据库文件路径

        Raises:
            FileNotFoundError: 元数据库还没有生成
        """
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"元数据库不存在: {self.db_path}。请先运行 python -m metadata.importer 导入资料"
            )
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            self._tables: dict[str, TableMeta] = {
                row["table_id"]: TableMeta(
                    table_id=row["table_id"], layer=row["layer"], schema_name=row["schema_name"],
                    table_name=row["table_name"], table_cn=row["table_cn"],
                    table_type=row["table_type"],
                    grain_columns=[name for name in row["grain_columns"].split(",") if name],
                    date_column=row["date_column"], description=row["description"],
                )
                for row in conn.execute("SELECT * FROM meta_table ORDER BY rowid")
            }
            self._columns: dict[str, list[ColumnMeta]] = {table_id: [] for table_id in self._tables}
            for row in conn.execute("SELECT * FROM meta_column ORDER BY table_id, ordinal"):
                self._columns[row["table_id"]].append(ColumnMeta(
                    table_id=row["table_id"], column_name=row["column_name"], ordinal=row["ordinal"],
                    column_cn=row["column_cn"], data_type=row["data_type"], is_pk=bool(row["is_pk"]),
                    description=row["description"], enum_values=row["enum_values"],
                ))
            self._lineage: list[LineageEdge] = [
                LineageEdge(
                    target_table=row["target_table"], target_column=row["target_column"],
                    source_table=row["source_table"], source_column=row["source_column"],
                    transform=row["transform"], script_name=row["script_name"],
                    source_external=bool(row["source_external"]),
                )
                for row in conn.execute("SELECT * FROM meta_lineage ORDER BY rowid")
            ]
            self._relations: list[Relation] = [
                Relation(
                    left_table=row["left_table"], left_columns=row["left_columns"].split(","),
                    right_table=row["right_table"], right_columns=row["right_columns"].split(","),
                    cardinality=row["cardinality"], origin=row["origin"], note=row["note"],
                )
                for row in conn.execute("SELECT * FROM meta_relation ORDER BY rowid")
            ]

    def tables(self, layer: str | None = None) -> list[TableMeta]:
        """返回表清单，可按层级过滤，顺序与表清单一致"""
        return [t for t in self._tables.values() if layer is None or t.layer == layer]

    def table(self, table_id: str) -> TableMeta | None:
        """按表标识取一张表，不存在时返回 None"""
        return self._tables.get(table_id)

    def columns(self, table_id: str) -> list[ColumnMeta]:
        """返回一张表的字段，按表内顺序。表不存在时返回空列表"""
        return list(self._columns.get(table_id, []))

    def column(self, table_id: str, column_name: str) -> ColumnMeta | None:
        """取一个字段，不存在时返回 None"""
        for column in self._columns.get(table_id, []):
            if column.column_name == column_name:
                return column
        return None

    def lineage_edges(self) -> list[LineageEdge]:
        """返回全部字段级血缘"""
        return list(self._lineage)

    def relations(self) -> list[Relation]:
        """返回全部关联关系"""
        return list(self._relations)

    def counts(self) -> dict[str, int]:
        """返回各类元数据的数量"""
        return {
            "表": len(self._tables),
            "字段": sum(len(columns) for columns in self._columns.values()),
            "血缘": len(self._lineage),
            "关联关系": len(self._relations),
        }
