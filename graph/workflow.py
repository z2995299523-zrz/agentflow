"""
多Agent 工作流组装 — StateGraph 节点注册 + 条件路由 + 编译
build_workflow() 返回编译后的可执行图
"""
from langgraph.graph import StateGraph, START, END

from graph.state import AgentState
from graph.nodes import rag_node, sql_node
from graph.supervisor import supervisor_node, route_by_intent
from graph.memory import get_memory


def finalize_node(state: AgentState) -> dict:
    """最终合成节点 — 拼接 RAG 和 SQL 结果

    将 rag_result 和 sql_result 合并为 final_answer。
    只有一个有值时直接输出，两者都有时分段展示。

    Args:
        state: 当前工作流状态

    Returns:
        {"final_answer": str}
    """
    parts = []
    rag = state.get("rag_result", "")
    sql = state.get("sql_result", "")

    if rag:
        parts.append(f"📚 知识库回答：\n{rag}")
        sources = state.get("rag_sources", [])
        if sources:
            parts.append(f"\n📖 参考来源：{', '.join(sources)}")

    if sql:
        parts.append(f"📊 数据查询结果：\n{sql}")
        sql_query = state.get("sql_query", "")
        if sql_query:
            parts.append(f"\n🔍 执行 SQL：\n{sql_query}")

    if not parts:
        return {"final_answer": "未能获取到有效回答，请换个方式提问试试。"}

    return {"final_answer": "\n\n".join(parts)}


def fallback_node(state: AgentState) -> dict:
    """兜底节点 — 无法理解问题时返回友好提示

    Args:
        state: 当前工作流状态

    Returns:
        {"final_answer": str}
    """
    question = state.get("question", "")
    return {
        "final_answer": (
            f"抱歉，我不太理解「{question}」这个问题。\n\n"
            "你可以尝试：\n"
            "1. 知识问答：如「AgentFlow 是什么？」「如何配置数据库连接？」\n"
            "2. 数据查询：如「产品表有多少条记录？」「销售额最高的5个产品是哪些？」\n"
            "3. 上传文档后向我提问文档内容"
        ),
    }


def route_after_rag(state: AgentState) -> str:
    """RAG 节点后的路由：mixed 意图继续走 SQL，否则直接汇总

    Args:
        state: 当前工作流状态（需含 intent 字段）

    Returns:
        下一个节点名称
    """
    if state.get("intent") == "mixed":
        return "sql"
    return "finalize"


def build_workflow(enable_memory: bool = True):
    """构建并编译多Agent 工作流

    图结构：
        START → supervisor → [rag | sql | fallback]
        rag → [mixed→sql | 其他→finalize]
        sql → finalize → END
        fallback → END

    Args:
        enable_memory: 是否启用多轮对话记忆（默认 True）

    Returns:
        编译后的 CompiledGraph，可调用 .invoke()
    """
    graph = StateGraph(AgentState)

    # 注册节点
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("rag", rag_node)
    graph.add_node("sql", sql_node)
    graph.add_node("finalize", finalize_node)
    graph.add_node("fallback", fallback_node)

    # 连线
    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges(
        "supervisor",
        route_by_intent,
        {
            "rag": "rag",
            "sql": "sql",
            "fallback": "fallback",
        },
    )
    # RAG 后条件路由：mixed → SQL → finalize；其他 → finalize
    graph.add_conditional_edges("rag", route_after_rag, {
        "sql": "sql",
        "finalize": "finalize",
    })
    graph.add_edge("sql", "finalize")
    graph.add_edge("finalize", END)
    graph.add_edge("fallback", END)

    if enable_memory:
        return graph.compile(checkpointer=get_memory())
    return graph.compile()
