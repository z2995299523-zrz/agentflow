"""
元数据库
把一套资料落成 SQLite 关系表，可以直接用 SQL 查看。其它模块通过 MetadataStore 只读访问
"""
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from metadata.models import (
    ColumnMeta,
    Example,
    FieldRef,
    LineageEdge,
    Mart,
    Relation,
    ScriptColumn,
    ScriptFilter,
    ScriptJoin,
    ScriptStatement,
    TableMeta,
)

# 页大小必须在建第一张表之前设定。一行 512 维向量约 2 KB，默认的 4 KB 页一页只放得下一行，
# 浪费近一半空间；16 KB 页能把真实库带向量时的增量从约 71 MB 降到约 41 MB，读取速度不变
SCHEMA_SQL = """
PRAGMA page_size = 16384;
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
    source_external INTEGER NOT NULL DEFAULT 0,
    is_passthrough  INTEGER NOT NULL DEFAULT 0,
    origin          TEXT NOT NULL DEFAULT 'list'
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
CREATE TABLE meta_script (
    script_name  TEXT NOT NULL,
    stmt_index   INTEGER NOT NULL,
    target_table TEXT NOT NULL,
    sql_text     TEXT NOT NULL,
    select_sql   TEXT NOT NULL DEFAULT '',
    parsed       INTEGER NOT NULL,
    filters      TEXT NOT NULL DEFAULT '[]',
    joins        TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (script_name, stmt_index)
);
CREATE TABLE meta_script_column (
    script_name   TEXT NOT NULL,
    stmt_index    INTEGER NOT NULL,
    target_table  TEXT NOT NULL,
    target_column TEXT NOT NULL,
    expression    TEXT NOT NULL,
    sources       TEXT NOT NULL DEFAULT '[]',
    kind          TEXT NOT NULL,
    scopes        TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (script_name, stmt_index, target_column)
);
CREATE TABLE meta_example (
    example_id   TEXT PRIMARY KEY,
    requirement  TEXT NOT NULL,
    caliber_note TEXT NOT NULL DEFAULT '',
    sql_text     TEXT NOT NULL,
    tables       TEXT NOT NULL DEFAULT '[]',
    source_file  TEXT NOT NULL,
    line_no      INTEGER NOT NULL
);
CREATE TABLE meta_vector (
    kind   TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    text   TEXT NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY (kind, ref_id)
);
CREATE TABLE meta_info (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

VECTOR_DTYPE = np.float32


@dataclass
class VectorSet:
    """一类文档的向量：字段或样例"""

    kind: str                  # field 或 example
    ref_ids: list[str]         # 文档编号：字段是「模式.表.字段」，样例是样例编号
    texts: list[str]           # 向量化时用的文本
    matrix: np.ndarray         # float32 矩阵，每行一条，已 L2 归一化


@dataclass
class VectorBundle:
    """一次导入算出的全部向量"""

    embedder: str              # 向量器名称。查询时必须用同一个向量器，否则相似度没有意义
    dim: int                   # 向量维度
    sets: dict[str, VectorSet]


def _json(value: object) -> str:
    """列表类字段按 JSON 文本入库，中文不转义，便于直接用 SQL 查看"""
    return json.dumps(value, ensure_ascii=False)


def _vector_rows(vectors: VectorBundle) -> list[tuple[str, str, str, bytes]]:
    """把向量摊成 meta_vector 的行

    Raises:
        ValueError: 编号、文本、矩阵的条数或维度对不上
    """
    rows: list[tuple[str, str, str, bytes]] = []
    for vector_set in vectors.sets.values():
        matrix = np.asarray(vector_set.matrix, dtype=VECTOR_DTYPE)
        count = len(vector_set.ref_ids)
        if len(vector_set.texts) != count or matrix.shape != (count, vectors.dim):
            raise ValueError(
                f"向量「{vector_set.kind}」的编号 {count} 条、文本 {len(vector_set.texts)} 条、"
                f"矩阵 {matrix.shape}，与维度 {vectors.dim} 对不上")
        rows += [(vector_set.kind, ref_id, text, matrix[i].tobytes())
                 for i, (ref_id, text) in enumerate(zip(vector_set.ref_ids, vector_set.texts))]
    return rows


def write_store(db_path: Path, mart: Mart, vectors: VectorBundle | None = None) -> None:
    """全量重建元数据库

    先写临时文件，全部成功后再替换正式文件。中途失败时正式文件不受影响。
    向量和其余内容写进同一个文件，不会出现元数据换了、向量还是旧的情况。

    Args:
        db_path: 元数据库文件路径
        mart: 校验通过、已经补全脚本和关联的资料
        vectors: 字段和样例的向量；为 None 时不写向量（导入时加了 --no-index）

    Raises:
        RuntimeError: 元数据库文件被其它程序占用，无法替换
        ValueError: 向量的条数或维度对不上
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
                "INSERT INTO meta_lineage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(e.target_table, e.target_column, e.source_table, e.source_column, e.transform,
                  e.script_name, int(e.source_external), int(e.is_passthrough), e.origin)
                 for e in mart.lineage],
            )
            conn.executemany(
                "INSERT INTO meta_relation VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(r.left_table, ",".join(r.left_columns), r.right_table, ",".join(r.right_columns),
                  r.cardinality, r.origin, r.note) for r in mart.relations],
            )
            conn.executemany(
                "INSERT INTO meta_script VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(s.script_name, s.stmt_index, s.target_table, s.sql_text, s.select_sql, int(s.parsed),
                  _json([f.model_dump() for f in s.filters]), _json([j.model_dump() for j in s.joins]))
                 for s in mart.scripts],
            )
            conn.executemany(
                "INSERT INTO meta_script_column VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(c.script_name, c.stmt_index, c.target_table, c.target_column, c.expression,
                  _json([s.model_dump() for s in c.sources]), c.kind, _json(c.scopes))
                 for c in mart.script_columns],
            )
            conn.executemany(
                "INSERT INTO meta_example VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(e.example_id, e.requirement, e.caliber_note, e.sql_text, _json(e.tables),
                  e.source_file, e.line_no) for e in mart.examples],
            )
            if vectors is not None:
                conn.executemany("INSERT INTO meta_info VALUES (?, ?)",
                                 [("embedder", vectors.embedder), ("dim", str(vectors.dim))])
                conn.executemany("INSERT INTO meta_vector VALUES (?, ?, ?, ?)", _vector_rows(vectors))
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


def _read_store(db_path: Path) -> tuple[Mart, VectorBundle | None]:
    """把元数据库的全部内容读回内存

    Raises:
        RuntimeError: 文件不是元数据库，或者是旧版本导入器生成的
    """
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            mart = Mart(
                tables=[
                    TableMeta(
                        table_id=row["table_id"], layer=row["layer"], schema_name=row["schema_name"],
                        table_name=row["table_name"], table_cn=row["table_cn"],
                        table_type=row["table_type"],
                        grain_columns=[name for name in row["grain_columns"].split(",") if name],
                        date_column=row["date_column"], description=row["description"],
                    )
                    for row in conn.execute("SELECT * FROM meta_table ORDER BY rowid")
                ],
                columns=[
                    ColumnMeta(
                        table_id=row["table_id"], column_name=row["column_name"], ordinal=row["ordinal"],
                        column_cn=row["column_cn"], data_type=row["data_type"], is_pk=bool(row["is_pk"]),
                        description=row["description"], enum_values=row["enum_values"],
                    )
                    for row in conn.execute("SELECT * FROM meta_column ORDER BY table_id, ordinal")
                ],
                lineage=[
                    LineageEdge(
                        target_table=row["target_table"], target_column=row["target_column"],
                        source_table=row["source_table"], source_column=row["source_column"],
                        transform=row["transform"], script_name=row["script_name"],
                        source_external=bool(row["source_external"]),
                        is_passthrough=bool(row["is_passthrough"]), origin=row["origin"],
                    )
                    for row in conn.execute("SELECT * FROM meta_lineage ORDER BY rowid")
                ],
                relations=[
                    Relation(
                        left_table=row["left_table"],
                        left_columns=[name for name in row["left_columns"].split(",") if name],
                        right_table=row["right_table"],
                        right_columns=[name for name in row["right_columns"].split(",") if name],
                        cardinality=row["cardinality"], origin=row["origin"], note=row["note"],
                    )
                    for row in conn.execute("SELECT * FROM meta_relation ORDER BY rowid")
                ],
                scripts=[
                    ScriptStatement(
                        script_name=row["script_name"], stmt_index=row["stmt_index"],
                        target_table=row["target_table"], sql_text=row["sql_text"],
                        select_sql=row["select_sql"], parsed=bool(row["parsed"]),
                        filters=[ScriptFilter(**item) for item in json.loads(row["filters"])],
                        joins=[ScriptJoin(**item) for item in json.loads(row["joins"])],
                    )
                    for row in conn.execute("SELECT * FROM meta_script ORDER BY rowid")
                ],
                script_columns=[
                    ScriptColumn(
                        script_name=row["script_name"], stmt_index=row["stmt_index"],
                        target_table=row["target_table"], target_column=row["target_column"],
                        expression=row["expression"],
                        sources=[FieldRef(**item) for item in json.loads(row["sources"])],
                        kind=row["kind"],
                        # 加这一列之前导出的库没有它：当作不知道经过了哪些查询层
                        scopes=json.loads(row["scopes"]) if "scopes" in row.keys() else [],
                    )
                    for row in conn.execute("SELECT * FROM meta_script_column ORDER BY rowid")
                ],
                examples=[
                    Example(
                        example_id=row["example_id"], requirement=row["requirement"],
                        caliber_note=row["caliber_note"], sql_text=row["sql_text"],
                        tables=json.loads(row["tables"]), source_file=row["source_file"],
                        line_no=row["line_no"],
                    )
                    for row in conn.execute("SELECT * FROM meta_example ORDER BY rowid")
                ],
            )
            info = {row["key"]: row["value"] for row in conn.execute("SELECT * FROM meta_info")}
            vectors = None
            if "embedder" in info:
                dim = int(info["dim"])
                grouped: dict[str, list[sqlite3.Row]] = {}
                for row in conn.execute("SELECT * FROM meta_vector ORDER BY rowid"):
                    grouped.setdefault(row["kind"], []).append(row)
                vectors = VectorBundle(embedder=info["embedder"], dim=dim, sets={
                    kind: VectorSet(
                        kind=kind,
                        ref_ids=[row["ref_id"] for row in rows],
                        texts=[row["text"] for row in rows],
                        matrix=np.frombuffer(b"".join(row["vector"] for row in rows),
                                             dtype=VECTOR_DTYPE).reshape(len(rows), dim).copy(),
                    )
                    for kind, rows in grouped.items()
                })
    except sqlite3.DatabaseError as error:
        raise RuntimeError(
            f"读不了元数据库 {db_path}：{error}。它可能是旧版本导入器生成的，"
            "请重新运行 python -m metadata.importer 导入") from error
    return mart, vectors


class MetadataStore:
    """元数据库的只读访问入口

    构造时把内容全部读进内存，之后不再占用数据库文件，重新导入不会被它挡住。
    返回的模型对象是内部数据本身，调用方不要原地修改。
    """

    def __init__(self, db_path: Path) -> None:
        """加载元数据库

        Args:
            db_path: 元数据库文件路径

        Raises:
            FileNotFoundError: 元数据库还没有生成
            RuntimeError: 文件不是元数据库，或者是旧版本导入器生成的
        """
        self.db_path: Path | None = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"元数据库不存在: {self.db_path}。请先运行 python -m metadata.importer 导入资料"
            )
        self._load(*_read_store(self.db_path))

    @classmethod
    def from_mart(cls, mart: Mart, vectors: VectorBundle | None = None) -> "MetadataStore":
        """直接由内存里的资料构造，不经过数据库文件

        导入期间校验样例、各模块的单元测试用它，省掉先写库再读回的步骤。

        Args:
            mart: 一套资料
            vectors: 向量，没有时为 None

        Returns:
            与写库再读回等价的只读入口
        """
        store = cls.__new__(cls)
        store.db_path = None
        store._load(mart, vectors)
        return store

    def _load(self, mart: Mart, vectors: VectorBundle | None) -> None:
        """建立按表、按字段查找用的内部索引"""
        self._tables: dict[str, TableMeta] = {t.table_id: t for t in mart.tables}
        self._columns: dict[str, list[ColumnMeta]] = {table_id: [] for table_id in self._tables}
        for column in mart.columns:
            self._columns.setdefault(column.table_id, []).append(column)
        for columns in self._columns.values():
            columns.sort(key=lambda column: column.ordinal)
        self._lineage: list[LineageEdge] = list(mart.lineage)
        self._relations: list[Relation] = list(mart.relations)
        self._scripts: list[ScriptStatement] = list(mart.scripts)
        self._script_columns: dict[str, list[ScriptColumn]] = {}
        for script_column in mart.script_columns:
            self._script_columns.setdefault(script_column.target_table, []).append(script_column)
        self._examples: list[Example] = list(mart.examples)
        self._vectors = vectors

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

    def scripts(self, target_table: str | None = None) -> list[ScriptStatement]:
        """返回脚本片段，可按目标表过滤，顺序与脚本文件和语句顺序一致"""
        return [s for s in self._scripts if target_table is None or s.target_table == target_table]

    def script_columns(self, target_table: str) -> list[ScriptColumn]:
        """返回一张目标表在加工脚本里的逐字段表达式"""
        return list(self._script_columns.get(target_table, []))

    def script_column(self, table_id: str, column_name: str) -> ScriptColumn | None:
        """取一个目标字段在加工脚本里的表达式。有多条语句写同一个字段时取第一条，没有时返回 None"""
        for script_column in self._script_columns.get(table_id, []):
            if script_column.target_column == column_name:
                return script_column
        return None

    def examples(self) -> list[Example]:
        """返回全部样例"""
        return list(self._examples)

    def vectors(self, kind: str) -> VectorSet | None:
        """取一类文档的向量（field 或 example），导入时没有建向量就返回 None"""
        if self._vectors is None:
            return None
        return self._vectors.sets.get(kind)

    @property
    def embedder_name(self) -> str | None:
        """建向量时用的向量器名称，没有建向量时为 None"""
        return self._vectors.embedder if self._vectors is not None else None

    def counts(self) -> dict[str, int]:
        """返回各类元数据的数量"""
        field_vectors = self.vectors("field")
        return {
            "表": len(self._tables),
            "字段": sum(len(columns) for columns in self._columns.values()),
            "血缘": len(self._lineage),
            "关联关系": len(self._relations),
            "加工语句": len(self._scripts),
            "样例": len(self._examples),
            "字段向量": len(field_vectors.ref_ids) if field_vectors is not None else 0,
        }
