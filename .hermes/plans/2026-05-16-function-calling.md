# Function Calling 工具封装 — 实施计划

> **For Hermes:** 将此计划交给 Claude Code 执行，每步验证测试通过后继续。

**目标：** 将 SQLQueryAgent 封装为 LangChain Function Calling 工具，让 LLM 在对话中自主判断何时查询数据库。

**架构：**
```
用户问题 → LLM (带 tool binding) → 判断是否需要查库
    ├── 需要 → 调用 sql_query tool → SQLQueryAgent → 返回结果 → LLM 用自然语言总结
    └── 不需要 → 直接回答
```

**涉及文件：**
- 新建: `C:\Users\DELL\agentflow\sql\tools.py`
- 新建: `C:\Users\DELL\agentflow\test_sql_tools.py`
- 修改: `C:\Users\DELL\agentflow\sql\__init__.py`

**技术要点：**
- LangChain 1.x 用 `@tool` 装饰器 或 `StructuredTool.from_function()`
- 工具需绑定到 LLM: `llm.bind_tools([tool])`
- ToolMessage 格式处理

---

## Task 1: 创建 sql/tools.py — create_sql_tool 工厂函数

**目标：** 创建 LangChain Tool，将 SQLQueryAgent 包装为函数调用工具

**文件：** 新建 `C:\Users\DELL\agentflow\sql\tools.py`

**实现代码：**

```python
"""
SQL 查询工具 — 将 SQLQueryAgent 封装为 LangChain Function Calling 工具
让 LLM 在对话中自主决定何时需要查询数据库。
"""
from typing import Optional
from langchain_core.tools import tool


# 工具描述 — LLM 据此判断何时调用
SQL_TOOL_DESCRIPTION = """查询企业数据库，用自然语言提问即可。

适用场景：
- 数据统计（销售额、订单量、用户数等）
- 排行查询（TOP N、最高/最低）
- 汇总分析（按分类汇总、按时间趋势）
- 条件筛选（满足某条件的记录）

不适用场景：
- 闲聊、问候、通用知识问题
- 不需要查数据库就能回答的问题

输入：自然语言问题（如"上个月销售额最高的5个产品"）
输出：查询结果的自然语言解释"""


def create_sql_tool(db_uri: Optional[str] = None):
    """
    创建 SQL 查询工具（延迟初始化 SQLQueryAgent）
    
    Args:
        db_uri: 数据库连接字符串，默认从 config.get_db_uri() 获取
    
    Returns:
        LangChain Tool 对象，可绑定到 LLM
    """
    # 延迟导入，避免循环依赖
    from sql.agent import SQLQueryAgent
    
    # 初始化 Agent（只做一次）
    agent = SQLQueryAgent(db_uri=db_uri)
    
    @tool(description=SQL_TOOL_DESCRIPTION)
    def sql_query(question: str) -> str:
        """查询数据库。question 必须是完整的自然语言问题。"""
        result = agent.query(question)
        if result["success"]:
            sql = result.get("sql", "")
            answer = result.get("answer", "")
            if sql:
                return f"[SQL: {sql}]\n\n{answer}"
            return answer
        else:
            return f"查询失败：{result.get('error', '未知错误')}"
    
    # 设置工具名称（装饰器自动取函数名，但显式设置更清晰）
    sql_query.name = "sql_query"
    
    return sql_query
```

**验证：** 文件保存后无语法错误

---

## Task 2: 创建测试文件 test_sql_tools.py

**目标：** 写测试验证 Function Calling 工具的基本行为和 LLM 工具绑定

**文件：** 新建 `C:\Users\DELL\agentflow\test_sql_tools.py`

```python
"""
sql/tools.py 功能测试
验证 Function Calling 工具封装、LLM 绑定、工具调用流程
"""
import sys
import os

# 确保项目根目录在 sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage

from config import get_llm_kwargs
from sql.tools import create_sql_tool, SQL_TOOL_DESCRIPTION


# ─── Fixtures ───────────────────────────────────────────

@pytest.fixture
def sql_tool():
    """创建 SQL 查询工具"""
    return create_sql_tool()


@pytest.fixture
def llm():
    """创建 LLM 实例"""
    kwargs = get_llm_kwargs()
    kwargs["temperature"] = 0
    return ChatOpenAI(**kwargs)


# ─── 工具基础验证 ──────────────────────────────────────

class TestToolBasics:
    """工具基础属性验证"""

    def test_tool_name(self, sql_tool):
        """工具名称应为 sql_query"""
        assert sql_tool.name == "sql_query"

    def test_tool_description(self, sql_tool):
        """工具描述不为空"""
        assert len(sql_tool.description) > 50
        assert "查询" in sql_tool.description

    def test_tool_args_schema(self, sql_tool):
        """工具参数 schema 包含 question"""
        schema = sql_tool.args_schema
        assert schema is not None
        # args_schema 在 @tool 装饰下自动从函数签名生成
        fields = schema.model_fields if hasattr(schema, 'model_fields') else {}
        assert "question" in str(schema).lower()


# ─── 工具调用验证 ──────────────────────────────────────

class TestToolInvocation:
    """工具实际调用验证"""

    def test_simple_query(self, sql_tool):
        """简单查询：产品数量"""
        result = sql_tool.invoke({"question": "一共有多少个产品？"})
        assert isinstance(result, str)
        assert len(result) > 0

    def test_count_query(self, sql_tool):
        """计数查询"""
        result = sql_tool.invoke({"question": "客户表有多少条记录？"})
        assert isinstance(result, str)
        assert len(result) > 0

    def test_top_n_query(self, sql_tool):
        """排行查询"""
        result = sql_tool.invoke({"question": "销售额最高的3个产品是哪些？"})
        assert isinstance(result, str)
        assert len(result) > 0


# ─── LLM 工具绑定验证 ──────────────────────────────────

class TestLLMBinding:
    """LLM 工具绑定 + 工具选择流程"""

    def test_bind_tools(self, llm, sql_tool):
        """LLM 能绑定工具"""
        llm_with_tools = llm.bind_tools([sql_tool])
        assert llm_with_tools is not None

    def test_llm_chooses_tool_for_data_question(self, llm, sql_tool):
        """LLM 对数据类问题选择调用工具"""
        llm_with_tools = llm.bind_tools([sql_tool])
        messages = [
            HumanMessage(content="销售额最高的5个产品是哪些？")
        ]
        response = llm_with_tools.invoke(messages)
        # 这个问题的答案一定需要查数据库 → LLM 应该调用工具
        assert len(response.tool_calls) > 0, (
            f"LLM 应该对数据查询问题调用 sql_query 工具，"
            f"但返回了 {len(response.tool_calls)} 个 tool_calls"
        )

    def test_llm_no_tool_for_chitchat(self, llm, sql_tool):
        """LLM 对闲聊问题不调用工具"""
        llm_with_tools = llm.bind_tools([sql_tool])
        messages = [
            HumanMessage(content="你好，今天天气怎么样？")
        ]
        response = llm_with_tools.invoke(messages)
        # 闲聊不需要查数据库
        assert len(response.tool_calls) == 0, (
            f"LLM 不应该对闲聊问题调用工具，"
            f"但返回了 {len(response.tool_calls)} 个 tool_calls"
        )
```

**验证命令：**
```bash
cd C:\Users\DELL\agentflow && python -m pytest test_sql_tools.py -v --tb=short
```

**期望输出：** 8 passed

---

## Task 3: 更新 sql/__init__.py

**目标：** 导出新模块

**文件：** 修改 `C:\Users\DELL\agentflow\sql\__init__.py`

**修改为（追加导入）：**
```python
from sql.agent import SQLQueryAgent
from sql.visualizer import QueryVisualizer
from sql.tools import create_sql_tool, SQL_TOOL_DESCRIPTION

__all__ = ["SQLQueryAgent", "QueryVisualizer", "create_sql_tool", "SQL_TOOL_DESCRIPTION"]
```

**如果只有 agent 和 visualizer 的导入，追加 tools 相关行即可。**

---

## Task 4: 运行所有 SQL 测试（回归验证）

**目标：** 确保新代码不破坏现有测试

**验证命令：**
```bash
cd C:\Users\DELL\agentflow && python -m pytest test_sql.py test_sql_tools.py -v --tb=short
```

**期望输出：** 18 + 8 = 26 passed

---

## 执行顺序

```
Task 1 → Task 3 → Task 2 → Task 4
(写代码)  (导出)   (测试)   (回归)
```
