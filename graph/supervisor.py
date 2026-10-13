"""
路由器节点 — LLM 零样本意图分类 + 条件路由
分析用户问题，判断走 RAG / SQL / 兜底
"""
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from config import get_llm_kwargs
from graph.state import AgentState

SUPERVISOR_SYSTEM_PROMPT = (
    "你是一个智能路由器。分析用户问题返回意图标签。\n\n"
    "rag=需要查文档才能回答的问题。包括但不限于：\n"
    "  - 人物/实体查询（如'董事长是谁''法定代表人是谁''公司全称是什么'）\n"
    "  - 概念解释/定义/政策/流程/标准\n"
    "  - 配置说明/使用方法/操作指南\n"
    "  - 任何需要从文档中提取具体信息的问题\n"
    "  ⚠️ 只要能通过查文档回答的问题，就是 rag，不管问题看起来多简单。\n\n"
    "sql=需要查数据库统计/排序/计数/求和/平均的问题（如'营收多少''前5名'），不涉及文档。\n\n"
    "mixed=文档和数据库都要查，且一个结果依赖另一个（如'根据文档标准筛选达标产品'）。\n\n"
    "parallel=文档和数据库都要查，但互相独立（如'查投诉客户和处理流程'）。\n\n"
    "unknown=无法判断/闲聊/问候/纯计算。\n\n"
    "只返回一个英文单词，不要解释。"
)


def _get_supervisor_llm():
    """获取路由器专用 LLM（temperature=0 确保稳定分类）"""
    kwargs = get_llm_kwargs()
    kwargs["temperature"] = 0
    return ChatOpenAI(**kwargs)


def supervisor_node(state: AgentState) -> dict:
    """LLM 零样本意图分类

    读取 state["question"]，用 LLM 判断意图，
    返回 {"intent": "rag"|"sql"|"mixed"|"unknown"}

    Args:
        state: 当前工作流状态

    Returns:
        包含 intent 字段的部分状态更新
    """
    question = state.get("question", "")
    if not question.strip():
        return {"intent": "unknown"}

    llm = _get_supervisor_llm()
    try:
        response = llm.invoke([
            SystemMessage(content=SUPERVISOR_SYSTEM_PROMPT),
            HumanMessage(content=question),
        ])
        raw = response.content.strip().lower()
        # 提取第一个英文单词作为意图标签
        intent = raw.split()[0] if raw else "unknown"
        # 归一化到合法值
        valid_intents = {"rag", "sql", "mixed", "parallel", "unknown"}
        if intent not in valid_intents:
            intent = "unknown"
    except Exception:
        intent = "unknown"

    return {"intent": intent}


def route_by_intent(state: AgentState) -> str | list[str]:
    """根据意图返回下一步节点名

    rag → rag 节点
    sql → sql 节点
    mixed → rag（先查文档，后续走 RAG→SQL 串行路径）
    parallel → [rag, sql]（两个节点并行执行，结果合并到 finalize）
    unknown → fallback 兜底

    Args:
        state: 当前工作流状态（需含 intent 字段）

    Returns:
        下一个节点名称，或节点名称列表（并行）
    """
    intent = state.get("intent", "unknown")
    if intent == "parallel":
        return ["rag", "sql"]  # LangGraph 扇出：两组节点同时执行
    mapping = {
        "rag": "rag",
        "sql": "sql",
        "mixed": "rag",
        "unknown": "fallback",
    }
    return mapping.get(intent, "fallback")
