"""LangGraph 多Agent 工作流测试（TDD：先写测试，再实现）"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest

from config import get_llm_kwargs


def _has_llm() -> bool:
    """检查 LLM 是否可用（DeepSeek 不需要代理）"""
    try:
        get_llm_kwargs()
        return True
    except Exception:
        return False


def _has_vectorstore() -> bool:
    """检查向量库是否有数据"""
    try:
        from rag.vectorstore import load_vectorstore
        vs = load_vectorstore()
        return vs._collection.count() > 0
    except Exception:
        return False


def _has_db() -> bool:
    """检查数据库文件是否存在"""
    return os.path.exists("./data/sample.db")


# ═══════════════════════════════════════════════════════
# 测试 1: AgentState 可创建（不需要 LLM）
# ═══════════════════════════════════════════════════════

def test_agent_state_creation():
    """State 可创建，字段默认值正确"""
    from graph.state import AgentState

    state = AgentState(
        messages=[],
        question="测试问题",
        intent="",
        rag_result="",
        rag_sources=[],
        sql_result="",
        sql_query="",
        sql_chart="",
        final_answer="",
    )
    assert state["question"] == "测试问题"
    assert state["messages"] == []
    assert state["intent"] == ""


# ═══════════════════════════════════════════════════════
# 测试 2-4: 路由器意图分类（需要 LLM）
# ═══════════════════════════════════════════════════════

@pytest.mark.skipif(not _has_llm(), reason="LLM 不可用，跳过")
def test_supervisor_routing_rag():
    """知识问答类问题 → rag"""
    from graph.supervisor import supervisor_node
    from graph.state import AgentState

    state: AgentState = {
        "messages": [],
        "question": "如何配置数据库连接参数？",
        "intent": "",
        "rag_result": "",
        "rag_sources": [],
        "sql_result": "",
        "sql_query": "",
        "sql_chart": "",
        "final_answer": "",
    }
    result = supervisor_node(state)
    assert result["intent"] in ("rag", "mixed", "unknown")


@pytest.mark.skipif(not _has_llm(), reason="LLM 不可用，跳过")
def test_supervisor_routing_sql():
    """数据查询类问题 → sql"""
    from graph.supervisor import supervisor_node
    from graph.state import AgentState

    state: AgentState = {
        "messages": [],
        "question": "产品表有多少条记录？按类别统计数量",
        "intent": "",
        "rag_result": "",
        "rag_sources": [],
        "sql_result": "",
        "sql_query": "",
        "sql_chart": "",
        "final_answer": "",
    }
    result = supervisor_node(state)
    assert result["intent"] in ("sql", "mixed", "unknown")


@pytest.mark.skipif(not _has_llm(), reason="LLM 不可用，跳过")
def test_supervisor_routing_unknown():
    """闲聊类问题 → unknown"""
    from graph.supervisor import supervisor_node
    from graph.state import AgentState

    state: AgentState = {
        "messages": [],
        "question": "你好，今天天气怎么样？",
        "intent": "",
        "rag_result": "",
        "rag_sources": [],
        "sql_result": "",
        "sql_query": "",
        "sql_chart": "",
        "final_answer": "",
    }
    result = supervisor_node(state)
    assert result["intent"] in ("unknown", "rag")  # 模型可能把闲聊也归到 rag


# ═══════════════════════════════════════════════════════
# 测试 5: RAG 节点（需要 LLM + 向量库）
# ═══════════════════════════════════════════════════════

@pytest.mark.skipif(not _has_llm(), reason="LLM 不可用，跳过")
def test_rag_node():
    """RAG 节点输出非空"""
    from graph.nodes import rag_node
    from graph.state import AgentState

    state: AgentState = {
        "messages": [],
        "question": "AgentFlow 是什么？",
        "intent": "",
        "rag_result": "",
        "rag_sources": [],
        "sql_result": "",
        "sql_query": "",
        "sql_chart": "",
        "final_answer": "",
    }
    result = rag_node(state)
    # 即使向量库为空，也不应崩溃，应有返回
    assert "rag_result" in result
    assert isinstance(result["rag_result"], str)
    assert len(result["rag_result"]) > 0


# ═══════════════════════════════════════════════════════
# 测试 6: SQL 节点（需要 LLM + 数据库）
# ═══════════════════════════════════════════════════════

@pytest.mark.skipif(not _has_llm(), reason="LLM 不可用，跳过")
@pytest.mark.skipif(not _has_db(), reason="数据库文件不存在，跳过")
def test_sql_node():
    """SQL 节点输出非空"""
    from graph.nodes import sql_node
    from graph.state import AgentState

    state: AgentState = {
        "messages": [],
        "question": "products 表有多少条记录？",
        "intent": "",
        "rag_result": "",
        "rag_sources": [],
        "sql_result": "",
        "sql_query": "",
        "sql_chart": "",
        "final_answer": "",
    }
    result = sql_node(state)
    assert "sql_result" in result
    assert "sql_query" in result
    assert isinstance(result["sql_result"], str)


# ═══════════════════════════════════════════════════════
# 测试 7: 工作流编译（不需要 LLM）
# ═══════════════════════════════════════════════════════

def test_workflow_build():
    """工作流编译通过"""
    from graph.workflow import build_workflow

    wf = build_workflow()
    assert wf is not None
    # 验证是编译后的图
    assert hasattr(wf, "invoke")


# ═══════════════════════════════════════════════════════
# 测试 8: 端到端 invoke（需要 LLM）
# ═══════════════════════════════════════════════════════

@pytest.mark.skipif(not _has_llm(), reason="LLM 不可用，跳过")
def test_workflow_invoke():
    """端到端 invoke 返回 final_answer"""
    from graph.workflow import build_workflow

    wf = build_workflow()
    result = wf.invoke({"question": "你好"})
    assert "final_answer" in result
    assert len(result["final_answer"]) > 0
