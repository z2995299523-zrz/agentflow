"""
Function Calling 工具封装
将 SQLQueryAgent 封装为 LangChain 1.x Tool，供 LLM 自主判断何时查询数据库。
"""
from langchain_core.tools import tool
from prompts.sql import SQL_TOOL_DESCRIPTION


def create_sql_tool(db_uri: str | None = None):
    """
    创建 SQL 查询 Function Calling 工具

    内部延迟导入 SQLQueryAgent，避免循环依赖和过早初始化。

    Args:
        db_uri: SQLAlchemy 连接字符串，默认从 config.get_db_uri() 获取

    Returns:
        Tool: 绑定了 SQLQueryAgent 实例的 LangChain Tool 对象
    """
    # 延迟导入，避免模块加载时就初始化 LLM 和数据库连接
    from sql.agent import SQLQueryAgent

    agent = SQLQueryAgent(db_uri=db_uri)

    @tool(description=SQL_TOOL_DESCRIPTION)
    def sql_query(question: str) -> str:
        """用自然语言查询 SQLite 数据库中的业务数据"""
        result = agent.query(question)
        if result["success"]:
            sql = result["sql"]
            answer = result["answer"]
            if sql:
                return f"[SQL] {sql}\n[结果] {answer}"
            return f"[结果] {answer}"
        else:
            return f"[错误] 查询失败: {result['error']}"

    sql_query.name = "sql_query"
    return sql_query
