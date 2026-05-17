"""
路由器节点 — LLM 零样本意图分类 + 条件路由
分析用户问题，判断走 RAG / SQL / 兜底
"""
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from config import get_llm_kwargs
from graph.state import AgentState

SUPERVISOR_SYSTEM_PROMPT = (
    "你是一个智能路由器。分析用户问题返回意图标签。\n"
    "rag=纯知识问答（查文档/概念解释/配置说明/使用方法，不涉及数据统计）、\n"
    "sql=纯数据查询（查数据库/统计/排行/计数/平均值/最大值，不涉及文档）、\n"
    "mixed=综合分析有依赖（需要用一个查询的结果作为另一个的条件，"
    "如'根据文档里的标准筛选达标产品'——先查文档拿标准，再用标准查数据库）、\n"
    "parallel=综合分析无依赖（文档和数据可同时查询，互不影响，"
    "如'查投诉最多的5个客户以及投诉处理流程'——客户名单和处理流程各自独立）、\n"
    "unknown=无法判断/闲聊/问候。\n"
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
