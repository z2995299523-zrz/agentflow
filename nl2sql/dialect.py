"""
SQL 方言转换
生成和校验用的是目标库方言（GaussDB/PostgreSQL），模拟库是 DuckDB，执行前需要转换
"""
import re

import sqlglot

DEFAULT_DIALECT = "postgres"
MOCK_DIALECT = "duckdb"
# 终端着色用的 ANSI 转义序列（CSI）。sqlglot 的解析错误用它给出错位置加下划线
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def transpile_statements(sql: str, source: str = DEFAULT_DIALECT,
                         target: str = MOCK_DIALECT) -> list[str]:
    """把一段 SQL 从源方言转成目标方言

    注释一律丢掉，不带进输出：sqlglot 输出注释时不转义正文，行注释里写 */ 和分号，
    就能让「按源方言只有一条语句」的 SQL 在目标库里变成好几条；注释本身也不属于语句，
    只有注释的片段（例如分号后面的一句说明）不算一条语句。

    Args:
        sql: 一条或多条用分号隔开的 SQL
        source: 源方言
        target: 目标方言

    Returns:
        逐条语句的列表，不含空语句，也不含只有注释的语句

    Raises:
        sqlglot.errors.ParseError: SQL 无法按源方言解析
    """
    return [statement for statement in sqlglot.transpile(sql, read=source, write=target, comments=False)
            if statement.strip()]


def strip_ansi(text: str) -> str:
    """去掉文本里的 ANSI 转义序列，错误信息进入结果和界面之前用"""
    return ANSI_ESCAPE.sub("", text)
