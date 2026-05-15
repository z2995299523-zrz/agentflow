"""
sql 模块 — Text-to-SQL 自然语言数据库查询

提供：
- SQLQueryAgent: 核心查询代理
- 安全校验工具函数
"""
from .agent import SQLQueryAgent

__all__ = ["SQLQueryAgent"]
