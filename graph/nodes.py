"""
工作流节点 — RAG 知识问答节点 + SQL 数据查询节点
每个节点读 state["question"]，执行业务逻辑，返回部分状态
"""
from graph.state import AgentState


def rag_node(state: AgentState) -> dict:
    """RAG 知识问答节点

    从向量库检索相关文档，调用 LLM 生成回答。
    向量库空时返回友好提示，不崩溃。

    Args:
        state: 当前工作流状态

    Returns:
        {"rag_result": str, "rag_sources": list}
    """
    question = state.get("question", "")
    if not question.strip():
        return {"rag_result": "问题为空，无法查询。", "rag_sources": []}

    try:
        from rag.chain import ask
        from rag.vectorstore import load_vectorstore

        vectorstore = load_vectorstore()
        # 检查向量库是否为空（用 get() 而非 _collection 私有属性）
        collection_data = vectorstore.get(limit=1)
        if not collection_data or len(collection_data.get("ids", [])) == 0:
            return {
                "rag_result": "知识库为空，请先上传文档后再提问。",
                "rag_sources": [],
            }

        result = ask(question, vectorstore=vectorstore)
        return {
            "rag_result": result.get("answer", ""),
            "rag_sources": result.get("sources", []),
        }
    except Exception as e:
        return {
            "rag_result": f"RAG 查询失败：{e}",
            "rag_sources": [],
        }


# SQL Agent 单例缓存
_sql_agent = None


def sql_node(state: AgentState) -> dict:
    """SQL 数据查询节点

    将自然语言问题转为 SQL 查询，执行并返回结果。
    DB 不可用时友好提示，不崩溃。

    Args:
        state: 当前工作流状态

    Returns:
        {"sql_result": str, "sql_query": str}
    """
    global _sql_agent

    question = state.get("question", "")
    if not question.strip():
        return {"sql_result": "问题为空，无法查询。", "sql_query": ""}

    try:
        from sql.agent import SQLQueryAgent

        if _sql_agent is None:
            _sql_agent = SQLQueryAgent()

        result = _sql_agent.query(question)

        if result.get("success"):
            return {
                "sql_result": result.get("answer", ""),
                "sql_query": result.get("sql", ""),
            }
        else:
            return {
                "sql_result": f"SQL 查询失败：{result.get('error', '未知错误')}",
                "sql_query": result.get("sql", ""),
            }
    except FileNotFoundError:
        return {
            "sql_result": "数据库文件不存在，请检查 data/sample.db 是否就位。",
            "sql_query": "",
        }
    except Exception as e:
        return {
            "sql_result": f"SQL 查询失败：{e}",
            "sql_query": "",
        }
