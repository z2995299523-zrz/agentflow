"""
Text-to-SQL 自然语言查询代理
将自然语言问题转换为 SQL，执行并返回自然语言解释。
"""
from typing import Any

from langchain_openai import ChatOpenAI
from langchain_community.utilities.sql_database import SQLDatabase
from langchain_community.agent_toolkits.sql.toolkit import SQLDatabaseToolkit
from langchain_community.agent_toolkits.sql.base import create_sql_agent
from langchain_core.callbacks import BaseCallbackHandler

from config import get_llm_kwargs, get_db_uri
from prompts.sql import SQL_AGENT_PREFIX, SQL_AGENT_SUFFIX

# 危险 SQL 关键词，执行前必须拦截
FORBIDDEN_KEYWORDS = {"DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "TRUNCATE", "CREATE"}


class _SQLCaptureCallback(BaseCallbackHandler):
    """回调：捕获 Agent 执行过程中生成的 SQL 语句"""

    def __init__(self) -> None:
        super().__init__()
        self.sql_queries: list[str] = []

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        """工具调用开始时记录 SQL"""
        tool_name = serialized.get("name", "")
        # 只捕获 sql_db_query 工具（不含 checker/schema/list）
        if tool_name == "sql_db_query":
            # tool-calling 模式下 input_str 是 Python repr 如 "{'query': 'SELECT ...'}"
            import ast as _ast
            try:
                parsed = _ast.literal_eval(input_str)
                if isinstance(parsed, dict):
                    sql = parsed.get("query", "") or parsed.get("sql", "") or input_str
                else:
                    sql = input_str
            except (ValueError, SyntaxError):
                sql = input_str
            self.sql_queries.append(sql)


class SafeSQLDatabase(SQLDatabase):
    """安全的 SQL 数据库封装，只允许 SELECT 只读查询"""

    def run(self, command: str, fetch: str = "all", include_columns: bool = False, *,
            parameters: Any = None, **kwargs: Any) -> Any:
        """执行 SQL 前进行安全校验"""
        self._check_safe(command)
        return super().run(command, fetch=fetch, include_columns=include_columns,
                           parameters=parameters, **kwargs)

    def run_no_throw(self, command: str, fetch: str = "all", include_columns: bool = False, *,
                     parameters: Any = None, **kwargs: Any) -> Any:
        """执行 SQL 前进行安全校验（即使 run_no_throw 原版不抛异常，安全校验也必须抛）"""
        self._check_safe(command)
        return super().run_no_throw(command, fetch=fetch, include_columns=include_columns,
                                    parameters=parameters, **kwargs)

    @staticmethod
    def _check_safe(command: str) -> None:
        """检查 SQL 是否包含危险操作关键词"""
        command_upper = command.upper().strip()
        # 按空白分词匹配，避免 column_name 含关键词时误判
        tokens = set(command_upper.split())
        for kw in FORBIDDEN_KEYWORDS:
            if kw in tokens:
                raise ValueError(
                    f"安全限制：检测到危险操作 [{kw}]，只允许 SELECT 查询。"
                )


class SQLQueryAgent:
    """Text-to-SQL 查询代理

    将自然语言问题转换为 SQL 查询，执行后返回自然语言解释。
    底层使用 LangChain SQL Agent，自动获取数据库 Schema 并注入 Prompt。

    安全限制：只允许 SELECT 查询，拒绝 DROP/DELETE/UPDATE/INSERT/ALTER/TRUNCATE/CREATE。
    """

    def __init__(self, db_uri: str | None = None) -> None:
        """初始化 Agent，连接数据库

        Args:
            db_uri: SQLAlchemy 连接字符串，默认从 config.get_db_uri() 获取
        """
        self.db_uri = db_uri or get_db_uri()
        self.llm = ChatOpenAI(**get_llm_kwargs())
        self.db = SafeSQLDatabase.from_uri(self.db_uri)
        toolkit = SQLDatabaseToolkit(db=self.db, llm=self.llm)

        # Agent 系统提示词（从 prompts.sql 导入）
        # ⚠️ 使用 tool-calling 模式而非默认的 zero-shot-react-description
        #    原因：LangChain 1.2.13 的 React 模式要求 prompt 包含 {agent_scratchpad}，
        #    自定义 prefix/suffix 会破坏模板结构。tool-calling 用 MessagesPlaceholder 正确处理。
        self.agent = create_sql_agent(
            llm=self.llm,
            toolkit=toolkit,
            agent_type="tool-calling",
            verbose=False,
            prefix=SQL_AGENT_PREFIX,
            handle_parsing_errors=True,
            return_intermediate_steps=True,
            max_iterations=10,
        )

    def query(self, question: str) -> dict:
        """用自然语言查询数据库

        Args:
            question: 自然语言问题，如 "上个月销售额最高的5个产品是哪些？"

        Returns:
            dict: {
                "question": str,      # 原始问题
                "sql": str,           # 生成的 SQL（空字符串表示未生成）
                "answer": str,        # 自然语言回答
                "success": bool,      # 是否成功
                "error": str|None,    # 错误信息（如有）
            }
        """
        callback = _SQLCaptureCallback()
        try:
            # 直接调用 invoke，SQL 通过回调 + intermediate_steps 双路径捕获
            result = self.agent.invoke(
                {"input": question},
                config={"callbacks": [callback]},
            )
            # 优先从回调取 SQL，fallback 到 intermediate_steps
            sql = callback.sql_queries[-1] if callback.sql_queries else ""
            if not sql:
                # 从 intermediate_steps 中提取 SQL（更可靠）
                for step in result.get("intermediate_steps", []):
                    action = step[0] if step else None
                    if action and hasattr(action, "tool_input"):
                        tool_input = action.tool_input
                        # tool-calling 模式：tool_input 是 dict
                        if isinstance(tool_input, dict):
                            candidate = tool_input.get("query", "") or str(tool_input)
                        else:
                            candidate = str(tool_input)
                        if any(kw in candidate.upper() for kw in ("SELECT", "FROM")):
                            sql = candidate
                            break

            # 清理 SQL 字符串：从 Python repr dict 中提取纯 SQL
            sql = str(sql or "")
            import ast as _ast
            try:
                parsed = _ast.literal_eval(sql)
                if isinstance(parsed, dict):
                    sql = parsed.get("query", "") or parsed.get("sql", "") or sql
            except (ValueError, SyntaxError):
                pass
            # 去掉各种前缀包装
            for wrap in ("SQL: ", "SQLQuery: ", "sql_db_query: "):
                sql = sql.replace(wrap, "")

            return {
                "question": question,
                "sql": sql.replace("SQL: ", "").replace("SQLQuery: ", "").strip(),
                "answer": result.get("output", ""),
                "success": True,
                "error": None,
            }
        except Exception as e:
            return {
                "question": question,
                "sql": "",
                "answer": "",
                "success": False,
                "error": str(e),
            }

    def get_schema(self) -> str:
        """获取数据库表结构信息"""
        return self.db.get_table_info()
