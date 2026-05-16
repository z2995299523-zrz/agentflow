"""
AgentFlow Prompt Engineering 体系

分层结构：
- prompts/system.py     系统级角色设定 + 能力边界 + 输出格式
- prompts/rag.py        RAG 模块提示词（问答、检索增强、分块说明）
- prompts/sql.py        SQL 模块提示词（Agent、工具描述、安全规则）
- prompts/examples.py   Few-shot 示例（RAG/SQL/工具选择）

设计原则：
1. 每个模块的提示词独立可测试
2. 面试中能解释每一层为什么这么设计
3. 修改一个模块的提示词不影响其他模块

使用方式：
    from prompts.system import MAIN_SYSTEM_PROMPT
    from prompts.rag import RAG_SYSTEM_PROMPT
    from prompts.sql import SQL_AGENT_PREFIX, SQL_TOOL_DESCRIPTION
    from prompts.examples import SQL_EXAMPLES
"""

from prompts.system import (
    MAIN_SYSTEM_PROMPT,
    CAPABILITY_STATEMENT,
    OUTPUT_FORMAT_RULES,
)
from prompts.rag import (
    RAG_SYSTEM_PROMPT,
    RAG_FOLLOWUP_PROMPT,
    CHUNKING_STRATEGY_NOTE,
)
from prompts.sql import (
    SQL_AGENT_PREFIX,
    SQL_AGENT_SUFFIX,
    SQL_TOOL_DESCRIPTION,
    SQL_SAFETY_RULES,
    VISUALIZER_DESCRIPTION,
)
from prompts.examples import (
    RAG_EXAMPLES,
    SQL_EXAMPLES,
    TOOL_SELECTION_EXAMPLES,
)

__all__ = [
    # system
    "MAIN_SYSTEM_PROMPT",
    "CAPABILITY_STATEMENT",
    "OUTPUT_FORMAT_RULES",
    # rag
    "RAG_SYSTEM_PROMPT",
    "RAG_FOLLOWUP_PROMPT",
    "CHUNKING_STRATEGY_NOTE",
    # sql
    "SQL_AGENT_PREFIX",
    "SQL_AGENT_SUFFIX",
    "SQL_TOOL_DESCRIPTION",
    "SQL_SAFETY_RULES",
    "VISUALIZER_DESCRIPTION",
    # examples
    "RAG_EXAMPLES",
    "SQL_EXAMPLES",
    "TOOL_SELECTION_EXAMPLES",
]
