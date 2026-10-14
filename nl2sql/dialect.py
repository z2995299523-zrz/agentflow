"""
SQL 方言转换
生成和校验用的是目标库方言（GaussDB/PostgreSQL），模拟库是 DuckDB，执行前需要转换
"""
import sqlglot

DEFAULT_DIALECT = "postgres"
MOCK_DIALECT = "duckdb"


def transpile_statements(sql: str, source: str = DEFAULT_DIALECT,
                         target: str = MOCK_DIALECT) -> list[str]:
    """把一段 SQL 从源方言转成目标方言

    Args:
        sql: 一条或多条用分号隔开的 SQL
        source: 源方言
        target: 目标方言

    Returns:
        逐条语句的列表，不含空语句

    Raises:
        sqlglot.errors.ParseError: SQL 无法按源方言解析
    """
    return [statement for statement in sqlglot.transpile(sql, read=source, write=target)
            if statement.strip()]
