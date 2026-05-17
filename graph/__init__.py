"""
AgentFlow 多Agent 工作流 — LangGraph 实现

核心导出：
- AgentState: 共享状态 TypedDict
- build_workflow: 编译工作流图
- make_config: 创建会话配置（thread_id 隔离）
- clear_memory: 清除会话记忆

使用示例：
    from graph import build_workflow, make_config
    wf = build_workflow()
    config = make_config("user-123")
    result = wf.invoke({"question": "产品表有多少条记录？"}, config)
    print(result["final_answer"])
"""
from graph.state import AgentState
from graph.workflow import build_workflow
from graph.memory import make_config, clear_memory

__all__ = ["AgentState", "build_workflow", "make_config", "clear_memory"]
