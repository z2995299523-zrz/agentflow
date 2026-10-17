"""
执行层
统一接口，可插拔。真实场景默认不执行，只返回 SQL；
模拟专题库上用 DuckDB 只读执行，供演示和评估使用。以后接真实库时再加一个实现即可
"""
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import duckdb

from nl2sql.dialect import DEFAULT_DIALECT, strip_ansi, transpile_statements

DEFAULT_MAX_ROWS = 200
DEFAULT_TIMEOUT_S = 30
INTERRUPT_GRACE_S = 5       # 超时后调用 interrupt()，再最多等这么久让查询退出


@dataclass
class ExecResult:
    """一次执行的结果"""

    executed: bool                                      # 是否真的执行并拿到了结果
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)     # 最多 max_rows 行
    row_count: int = 0                                  # 实际返回的行数（不是查询的总行数）
    truncated: bool = False                             # 后面还有没返回的行
    elapsed_ms: int = 0
    error: str = ""                                     # 执行失败的原因
    note: str = ""                                      # 没有执行时的说明


class Executor(Protocol):
    """执行层接口"""

    name: str

    def execute(self, sql: str, max_rows: int | None = DEFAULT_MAX_ROWS,
                timeout_s: float | None = DEFAULT_TIMEOUT_S) -> ExecResult:
        """执行一条查询

        Args:
            sql: 目标库方言的 SQL
            max_rows: 最多返回几行，None 表示全部
            timeout_s: 超时秒数，None 表示不限时
        """
        ...


class NullExecutor:
    """不执行：只生成模式"""

    name = "none"

    def execute(self, sql: str, max_rows: int | None = DEFAULT_MAX_ROWS,
                timeout_s: float | None = DEFAULT_TIMEOUT_S) -> ExecResult:
        """不执行，直接返回说明"""
        return ExecResult(executed=False, note="仅生成模式，未执行")


class DuckDBExecutor:
    """在模拟专题库（DuckDB）上只读执行

    每次执行各开各的只读连接，多线程并发调用互不影响。
    """

    name = "duckdb"

    def __init__(self, db_path: Path, source_dialect: str = DEFAULT_DIALECT) -> None:
        """
        Args:
            db_path: 模拟专题库文件
            source_dialect: 传入 SQL 的方言，执行前转成 DuckDB 方言
        """
        self.db_path = Path(db_path)
        self.source_dialect = source_dialect

    def execute(self, sql: str, max_rows: int | None = DEFAULT_MAX_ROWS,
                timeout_s: float | None = DEFAULT_TIMEOUT_S) -> ExecResult:
        """转换方言后以只读方式执行。任何失败都以结果的形式返回，不抛异常

        查询和读取结果都在工作线程里做：max_rows 不是 None 时只取 max_rows + 1 行（多的一行用来判断后面
        还有没有行，笛卡尔积之类的大结果不会整个读回内存）；主线程最多等 timeout_s 秒，到点调用
        interrupt() 打断查询，再等最多 INTERRUPT_GRACE_S 秒。

        Args:
            sql: 源方言的 SQL，只能是一条语句
            max_rows: 最多返回几行，None 表示全部
            timeout_s: 超时秒数，None 表示不限时

        Returns:
            执行结果；超时的 error 是「执行超过 N 秒，已中断」
        """
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
            # 只读只保护库文件本身；关掉外部访问，生成的 SQL 才读写不了本机的其它文件
            con = duckdb.connect(str(self.db_path), read_only=True,
                                 config={"enable_external_access": False})
        except Exception as error:  # noqa: BLE001 方言转换失败、库不存在、打不开都归为执行失败
            return ExecResult(executed=False, error=_describe(error), elapsed_ms=elapsed())

        outcome: dict[str, object] = {}

        def work() -> None:
            """工作线程：执行、读结果、关连接都在这里，连接不跨线程使用（打断除外）"""
            try:
                outcome["data"] = _run(con, statements[0], max_rows)
            except Exception as error:  # noqa: BLE001 SQL 报错、被打断都记下来，由主线程处理
                outcome["error"] = error
            finally:
                try:
                    con.close()
                except Exception:  # noqa: BLE001 连接已经被关掉或损坏，没有别的可做
                    pass

        worker = threading.Thread(target=work, name="duckdb-execute", daemon=True)
        worker.start()
        worker.join(timeout_s)
        if worker.is_alive():
            try:
                con.interrupt()
            except Exception:  # noqa: BLE001 查询恰好在这一刻结束、连接已关闭
                pass
            worker.join(INTERRUPT_GRACE_S)
            seconds = f"{timeout_s:g}"
            message = (f"执行超过 {seconds} 秒，已中断" if not worker.is_alive()
                       else f"执行超过 {seconds} 秒，中断未生效")
            return ExecResult(executed=False, error=message, elapsed_ms=elapsed())
        if "error" in outcome:
            return ExecResult(executed=False, error=_describe(outcome["error"]), elapsed_ms=elapsed())
        columns, rows, truncated = outcome["data"]
        return ExecResult(executed=True, columns=columns, rows=rows, row_count=len(rows),
                          truncated=truncated, elapsed_ms=elapsed())


def _run(con: duckdb.DuckDBPyConnection, statement: str, max_rows: int | None
         ) -> tuple[list[str], list[tuple], bool]:
    """执行一条语句并按行数上限读取结果，返回（列名, 行, 后面是否还有行）"""
    cursor = con.execute(statement)
    if cursor.description is None:                      # 没有结果集的语句
        return [], [], False
    columns = [item[0] for item in cursor.description]
    if max_rows is None:
        return columns, cursor.fetchall(), False
    limit = max(max_rows, 0)
    rows = cursor.fetchmany(limit + 1)
    return columns, rows[:limit], len(rows) > limit


def _describe(error: object) -> str:
    """错误写成「类型: 信息」，去掉信息里的 ANSI 转义"""
    return f"{type(error).__name__}: {strip_ansi(str(error))}"


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
