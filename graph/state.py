"""
LangGraph 多Agent 共享状态定义
AgentState 贯穿所有节点，用 TypedDict + Annotated 定义
"""
from typing import TypedDict, Annotated, Sequence

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """多Agent 工作流共享状态

    每个字段在各节点间流转，节点返回部分更新即可合并。
    messages 用 add_messages reducer 自动追加（不覆盖）。
    """
    messages: Annotated[Sequence[BaseMessage], add_messages]
    question: str
    intent: str
    rag_result: str
    rag_sources: list
    sql_result: str
    sql_query: str
    sql_chart: str
    final_answer: str
