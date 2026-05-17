"""
路由器节点 — LLM 零样本意图分类 + 条件路由
分析用户问题，判断走 RAG / SQL / 兜底
"""
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from config import get_llm_kwargs
from graph.state import AgentState

SUPERVISOR_SYSTEM_PROMPT = (
    "你是一个智能路由器。分析用户问题返回意图标签。"
    "rag=知识问答（查文档/概念解释/配置说明/使用方法）、"
    "sql=数据查询（查数据库/统计/排行/计数/平均值/最大值）、"
    "mixed=综合分析（需要先查文档再查数据库）、"
    "unknown=无法判断/闲聊/问候。"
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
        valid_intents = {"rag", "sql", "mixed", "unknown"}
        if intent not in valid_intents:
            intent = "unknown"
    except Exception:
        intent = "unknown"

    return {"intent": intent}


def route_by_intent(state: AgentState) -> str:
    """根据意图返回下一步节点名

    rag → rag 节点
    sql → sql 节点
    mixed → rag（先查文档，后续可扩展为双路并行）
    unknown → fallback 兜底

    Args:
        state: 当前工作流状态（需含 intent 字段）

    Returns:
        下一个节点名称
    """
    intent = state.get("intent", "unknown")
    mapping = {
        "rag": "rag",
        "sql": "sql",
        "mixed": "rag",
        "unknown": "fallback",
    }
    return mapping.get(intent, "fallback")
