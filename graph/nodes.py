"""
工作流节点 — RAG 知识问答节点 + SQL 数据查询节点
每个节点读 state["question"]，执行业务逻辑，返回部分状态
"""
from graph.state import AgentState

# 全局单例（由 main.py lifespan 注入，fallback 懒加载）
_vectorstore = None
_sql_agent = None


def init_vectorstore(vs):
    """注入预热的 vectorstore（由 main.py lifespan 调用）"""
    global _vectorstore
    _vectorstore = vs


def init_sql_agent(agent):
    """注入预热的 SQL agent（由 main.py lifespan 调用）"""
    global _sql_agent
    _sql_agent = agent


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

        if _vectorstore is None:
            from rag.vectorstore import load_vectorstore
            init_vectorstore(load_vectorstore())

        # 检查向量库是否为空（用 get() 而非 _collection 私有属性）
        collection_data = _vectorstore.get(limit=1)
        if not collection_data or len(collection_data.get("ids", [])) == 0:
            return {
                "rag_result": "知识库为空，请先上传文档后再提问。",
                "rag_sources": [],
            }

        result = ask(question, vectorstore=_vectorstore)
        return {
            "rag_result": result.get("answer", ""),
            "rag_sources": result.get("sources", []),
        }
    except Exception as e:
        return {
            "rag_result": f"RAG 查询失败：{e}",
            "rag_sources": [],
        }


def sql_node(state: AgentState) -> dict:
    """SQL 数据查询节点

    将自然语言问题转为 SQL 查询，执行并返回结果。
    mixed 意图时注入 RAG 上下文辅助 SQL 生成。
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

    # 如果有 RAG 结果且意图为 mixed，注入上下文
    rag_context = state.get("rag_result", "")
    if rag_context and state.get("intent") == "mixed":
        question = f"参考信息：{rag_context[:500]}\n\n问题：{question}"

    try:
        if _sql_agent is None:
            from sql.agent import SQLQueryAgent
            init_sql_agent(SQLQueryAgent())

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
