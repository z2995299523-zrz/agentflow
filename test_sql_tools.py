"""Function Calling 工具封装测试（使用 pytest 风格）"""
import sys
import os

# 确保从项目根目录运行
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

from config import get_llm_kwargs
from sql.tools import create_sql_tool, SQL_TOOL_DESCRIPTION


def _get_llm_kwargs_no_temp():
    """获取 LLM 参数，去掉 temperature 以便测试中自定义"""
    kwargs = get_llm_kwargs()
    kwargs.pop("temperature", None)
    return kwargs


@pytest.fixture(scope="module")
def sql_tool():
    """模块级 fixture：创建一次工具实例，复用"""
    return create_sql_tool()


# ─── 工具基础属性（不需要网络） ──────────────────────────

def test_tool_name(sql_tool):
    """测试1: 工具名称应为 sql_query"""
    assert sql_tool.name == "sql_query"


def test_tool_description_length(sql_tool):
    """测试2: 工具描述长度应大于50字符"""
    assert len(sql_tool.description) > 50


def test_tool_description_mentions_db(sql_tool):
    """测试3: 工具描述包含数据库相关关键词（证明 LLM 能理解用途）"""
    desc = sql_tool.description
    assert "查询" in desc or "数据" in desc


# ─── 工具调用（需要 LLM + 数据库，需代理） ──────────────

@pytest.mark.skipif(
    not os.environ.get("HTTPS_PROXY") and not os.environ.get("HTTP_PROXY"),
    reason="需要网络代理才能调用 LLM"
)
def test_simple_query(sql_tool):
    """测试4: 简单查询能返回结果"""
    result = sql_tool.invoke({"question": "有哪些产品类别？"})
    assert len(result) > 0
    assert "[结果]" in result or "[错误]" not in result


@pytest.mark.skipif(
    not os.environ.get("HTTPS_PROXY") and not os.environ.get("HTTP_PROXY"),
    reason="需要网络代理才能调用 LLM"
)
def test_count_query(sql_tool):
    """测试5: 计数查询"""
    result = sql_tool.invoke({"question": "products 表总共有多少条记录？"})
    assert len(result) > 0


@pytest.mark.skipif(
    not os.environ.get("HTTPS_PROXY") and not os.environ.get("HTTP_PROXY"),
    reason="需要网络代理才能调用 LLM"
)
def test_ranking_query(sql_tool):
    """测试6: 排行查询"""
    result = sql_tool.invoke({"question": "销售额最高的3个产品是哪些？"})
    assert len(result) > 0


# ─── LLM 工具绑定（需要 LLM API + 代理） ────────────────

@pytest.mark.skipif(
    not os.environ.get("HTTPS_PROXY") and not os.environ.get("HTTP_PROXY"),
    reason="需要网络代理才能调用 LLM"
)
def test_llm_bind_tool(sql_tool):
    """测试7: LLM 能绑定工具"""
    kwargs = _get_llm_kwargs_no_temp()
    llm = ChatOpenAI(**kwargs, temperature=0)
    bound_llm = llm.bind_tools([sql_tool])
    assert bound_llm is not None
    assert "tools" in bound_llm.kwargs


@pytest.mark.skipif(
    not os.environ.get("HTTPS_PROXY") and not os.environ.get("HTTP_PROXY"),
    reason="需要网络代理才能调用 LLM"
)
def test_llm_calls_tool_for_data_question(sql_tool):
    """测试8: LLM 对数据问题会调用工具"""
    kwargs = _get_llm_kwargs_no_temp()
    llm = ChatOpenAI(**kwargs, temperature=0)
    bound_llm = llm.bind_tools([sql_tool])
    response = bound_llm.invoke([HumanMessage(content="查询一下产品表有哪些产品类别")])
    assert len(response.tool_calls) > 0


@pytest.mark.skipif(
    not os.environ.get("HTTPS_PROXY") and not os.environ.get("HTTP_PROXY"),
    reason="需要网络代理才能调用 LLM"
)
def test_llm_no_tool_for_chitchat(sql_tool):
    """测试9: LLM 对闲聊不调用工具"""
    kwargs = _get_llm_kwargs_no_temp()
    llm = ChatOpenAI(**kwargs, temperature=0)
    bound_llm = llm.bind_tools([sql_tool])
    response = bound_llm.invoke([HumanMessage(content="你好，今天天气怎么样？")])
    assert len(response.tool_calls) == 0
