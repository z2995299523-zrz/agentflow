"""
AgentFlow 多Agent 工作流 — LangGraph 实现

核心导出：
- AgentState: 共享状态 TypedDict
- build_workflow: 编译工作流图

使用示例：
    from graph import build_workflow
    wf = build_workflow()
    result = wf.invoke({"question": "产品表有多少条记录？"})
    print(result["final_answer"])
"""
from graph.state import AgentState
from graph.workflow import build_workflow

__all__ = ["AgentState", "build_workflow"]
