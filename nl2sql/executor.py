"""
执行层
统一接口，可插拔。真实场景默认不执行，只返回 SQL；
模拟专题库上用 DuckDB 只读执行，供演示和评估使用。以后接真实库时再加一个实现即可
"""
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import duckdb

from nl2sql.dialect import DEFAULT_DIALECT, transpile_statements

DEFAULT_MAX_ROWS = 200


@dataclass
class ExecResult:
    """一次执行的结果"""

    executed: bool                                      # 是否真的执行并拿到了结果
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)     # 最多 max_rows 行
    row_count: int = 0                                  # 截断前的总行数
    truncated: bool = False
    elapsed_ms: int = 0
    error: str = ""                                     # 执行失败的原因
    note: str = ""                                      # 没有执行时的说明


class Executor(Protocol):
    """执行层接口"""

    name: str

    def execute(self, sql: str, max_rows: int | None = DEFAULT_MAX_ROWS) -> ExecResult:
        """执行一条查询。max_rows 为 None 时返回全部行"""
        ...


class NullExecutor:
    """不执行：只生成模式"""

    name = "none"

    def execute(self, sql: str, max_rows: int | None = DEFAULT_MAX_ROWS) -> ExecResult:
        """不执行，直接返回说明"""
        return ExecResult(executed=False, note="仅生成模式，未执行")


class DuckDBExecutor:
    """在模拟专题库（DuckDB）上只读执行"""

    name = "duckdb"

    def __init__(self, db_path: Path, source_dialect: str = DEFAULT_DIALECT) -> None:
        """
        Args:
            db_path: 模拟专题库文件
            source_dialect: 传入 SQL 的方言，执行前转成 DuckDB 方言
        """
        self.db_path = Path(db_path)
        self.source_dialect = source_dialect

    def execute(self, sql: str, max_rows: int | None = DEFAULT_MAX_ROWS) -> ExecResult:
        """转换方言后以只读方式执行。任何失败都以结果的形式返回，不抛异常"""
        started = time.perf_counter()

        def elapsed() -> int:
            return int((time.perf_counter() - started) * 1000)

        try:
            if not self.db_path.exists():
                raise FileNotFoundError(
                    f"模拟专题库不存在: {self.db_path}。请先运行 python -m data.mock_mart.build_mock")
            statements = transpile_statements(sql, source=self.source_dialect)
            if len(statements) != 1:
                raise ValueError(f"只能执行单条语句，收到 {len(statements)} 条")
            con = duckdb.connect(str(self.db_path), read_only=True)
            try:
                cursor = con.execute(statements[0])
                columns = [item[0] for item in cursor.description or []]
                rows = cursor.fetchall()
            finally:
                con.close()
        except Exception as error:  # noqa: BLE001 方言转换失败、SQL 报错、库不存在都归为执行失败
            return ExecResult(executed=False, error=f"{type(error).__name__}: {error}",
                              elapsed_ms=elapsed())

        limited = rows if max_rows is None else rows[:max_rows]
        return ExecResult(executed=True, columns=columns, rows=limited, row_count=len(rows),
                          truncated=len(limited) < len(rows), elapsed_ms=elapsed())


def get_executor(kind: str, mock_db_path: Path | None = None,
                 dialect: str = DEFAULT_DIALECT) -> Executor:
    """按配置创建执行层

    Args:
        kind: none 表示不执行，duckdb 表示在模拟专题库上执行
        mock_db_path: 模拟专题库文件，kind 为 duckdb 时必填
        dialect: 传入 SQL 的方言

    Raises:
        ValueError: kind 不支持，或缺少模拟专题库路径
    """
    if kind == "none":
        return NullExecutor()
    if kind == "duckdb":
        if mock_db_path is None:
            raise ValueError("duckdb 执行层需要提供模拟专题库路径")
        return DuckDBExecutor(mock_db_path, dialect)
    raise ValueError(f"不支持的执行层: {kind}（可选 none、duckdb）")
