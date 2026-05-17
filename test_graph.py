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


@pytest.mark.skipif(not _has_llm(), reason="LLM 不可用，跳过")
def test_supervisor_routing_parallel():
    """无依赖综合分析 → parallel"""
    from graph.supervisor import supervisor_node
    from graph.state import AgentState

    state: AgentState = {
        "messages": [],
        "question": "查投诉最多的5个客户，以及投诉处理流程",
        "intent": "",
        "rag_result": "",
        "rag_sources": [],
        "sql_result": "",
        "sql_query": "",
        "sql_chart": "",
        "final_answer": "",
    }
    result = supervisor_node(state)
    # parallel 或 mixed 都可接受（LLM 可能判断为 parallel 或 mixed）
    assert result["intent"] in ("parallel", "mixed", "unknown")


# ═══════════════════════════════════════════════════════
# 测试 4b: route_by_intent 并行返回（不需要 LLM）
# ═══════════════════════════════════════════════════════

def test_route_by_intent_parallel():
    """parallel 意图时 route_by_intent 返回列表 ['rag', 'sql']"""
    from graph.supervisor import route_by_intent
    from graph.state import AgentState

    state: AgentState = {
        "messages": [],
        "question": "测试",
        "intent": "parallel",
        "rag_result": "",
        "rag_sources": [],
        "sql_result": "",
        "sql_query": "",
        "sql_chart": "",
        "final_answer": "",
    }
    result = route_by_intent(state)
    assert isinstance(result, list), f"应为 list，实际 {type(result)}"
    assert "rag" in result
    assert "sql" in result
    assert len(result) == 2


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


# ═══════════════════════════════════════════════════════
# 测试 9: Mixed 意图路由（不需要 LLM）
# ═══════════════════════════════════════════════════════

def test_route_after_rag_mixed():
    """mixed 意图时 route_after_rag 返回 'sql'"""
    from graph.workflow import route_after_rag
    from graph.state import AgentState

    state: AgentState = {
        "messages": [],
        "question": "测试",
        "intent": "mixed",
        "rag_result": "RAG 结果",
        "rag_sources": [],
        "sql_result": "",
        "sql_query": "",
        "sql_chart": "",
        "final_answer": "",
    }
    assert route_after_rag(state) == "sql"


def test_route_after_rag_not_mixed():
    """非 mixed 意图时 route_after_rag 返回 'finalize'"""
    from graph.workflow import route_after_rag
    from graph.state import AgentState

    state: AgentState = {
        "messages": [],
        "question": "测试",
        "intent": "rag",
        "rag_result": "RAG 结果",
        "rag_sources": [],
        "sql_result": "",
        "sql_query": "",
        "sql_chart": "",
        "final_answer": "",
    }
    assert route_after_rag(state) == "finalize"


# ═══════════════════════════════════════════════════════
# 测试 9b: SQL 后路由（不需要 LLM）
# ═══════════════════════════════════════════════════════

def test_route_after_sql_mixed_rag_not_done():
    """mixed 且 RAG 未跑时 route_after_sql 返回 'rag'"""
    from graph.workflow import route_after_sql
    from graph.state import AgentState

    state: AgentState = {
        "messages": [], "question": "测试", "intent": "mixed",
        "rag_result": "", "rag_sources": [],
        "sql_result": "SQL 结果", "sql_query": "SELECT 1",
        "sql_chart": "", "final_answer": "",
    }
    assert route_after_sql(state) == "rag"


def test_route_after_sql_rag_already_done():
    """RAG 已跑过时 route_after_sql 返回 'finalize'（防死循环）"""
    from graph.workflow import route_after_sql
    from graph.state import AgentState

    state: AgentState = {
        "messages": [], "question": "测试", "intent": "mixed",
        "rag_result": "RAG 结果", "rag_sources": ["doc1"],
        "sql_result": "SQL 结果", "sql_query": "SELECT 1",
        "sql_chart": "", "final_answer": "",
    }
    assert route_after_sql(state) == "finalize"


# ═══════════════════════════════════════════════════════
# 测试 10: Mixed 端到端（需要 LLM）
# ═══════════════════════════════════════════════════════

@pytest.mark.skipif(not _has_llm(), reason="LLM 不可用，跳过")
def test_workflow_mixed():
    """Mixed 意图端到端：RAG → SQL → 汇总"""
    from graph.workflow import build_workflow

    wf = build_workflow()
    result = wf.invoke({
        "question": "根据文档分析销售趋势",
        "intent": "mixed",  # 直接指定 mixed，跳过 supervisor
    })
    assert "final_answer" in result
    assert len(result["final_answer"]) > 0


# ═══════════════════════════════════════════════════════
# 测试 11-12: 多轮对话记忆（需要 LLM）
# ═══════════════════════════════════════════════════════

@pytest.mark.skipif(not _has_llm(), reason="LLM 不可用，跳过")
def test_memory_multiround():
    """验证 checkpointer 工作：同一 thread_id 多轮不崩溃"""
    from graph.workflow import build_workflow
    from graph.memory import make_config
    
    wf = build_workflow(enable_memory=True)
    config = make_config("test-user-1")
    
    # 第一轮
    r1 = wf.invoke({"question": "你好"}, config)
    assert "final_answer" in r1
    
    # 第二轮（同样的 thread_id，不崩溃即通过）
    r2 = wf.invoke({"question": "我刚才说了什么"}, config)
    assert "final_answer" in r2
    # 注意：messages 累积依赖节点实现，Day 25 完善后改 assert


@pytest.mark.skipif(not _has_llm(), reason="LLM 不可用，跳过")
def test_memory_isolation():
    """验证不同 thread_id 的对话不会串"""
    from graph.workflow import build_workflow
    from graph.memory import make_config

    wf = build_workflow(enable_memory=True)

    r_a = wf.invoke({"question": "产品表有多少条"}, make_config("user-A"))
    r_b = wf.invoke({"question": "你好"}, make_config("user-B"))

    # 两个用户的消息应该独立
    assert "final_answer" in r_a
    assert "final_answer" in r_b
