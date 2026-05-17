"""多轮对话记忆管理 — MemorySaver + thread_id 隔离"""

from langgraph.checkpoint.memory import MemorySaver

_memory: MemorySaver | None = None


def get_memory() -> MemorySaver:
    """获取全局 MemorySaver 单例"""
    global _memory
    if _memory is None:
        _memory = MemorySaver()
    return _memory


def make_config(thread_id: str = "default") -> dict:
    """创建带 thread_id 的 config（用于区分不同用户/会话）

    Args:
        thread_id: 会话标识，如 "user-123" 或 "session-abc"

    Returns:
        {"configurable": {"thread_id": "..."}}
    """
    return {"configurable": {"thread_id": thread_id}}


def clear_memory(thread_id: str = "default") -> None:
    """清除指定会话的记忆（重置对话）

    注：MemorySaver 不直接支持按 thread_id 清除，
    简单方案：新建 MemorySaver 实例（清除所有会话）。
    """
    global _memory
    _memory = MemorySaver()
