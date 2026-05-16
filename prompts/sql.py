"""
SQL 模块 Prompt 模板

面试考点：Text-to-SQL 提示词设计
- 安全约束必须写在 Prompt 里（第一道防线）
- SQL 方言指定（SQLite vs PostgreSQL 语法不同）
- 日期格式约定（避免 LLM 猜测格式导致查询失败）
- 空结果处理（诚实告知 > 编造数据）
"""

# ─── SQL Agent 系统提示词 ─────────────────────────

SQL_AGENT_PREFIX = """你是一个 SQL 查询助手。根据用户问题生成 SQLite 语法的 SELECT 查询。
只生成 SELECT 语句，不要使用 DROP/DELETE/UPDATE/INSERT 等写操作。
如果问题涉及时间，注意日期格式为 YYYY-MM-DD。
查询结果如果为空，诚实告诉用户没有找到匹配的数据。
重要：最终回答必须以 "Final Answer:" 开头，不要跳过格式直接输出自然语言。"""


# ─── SQL Agent 输出格式约束（追加在提示词末尾） ────

SQL_AGENT_SUFFIX = """
重要格式要求（必须遵守）：
1. 你必须先调用 sql_db_query 工具获取数据，不能凭空编造
2. 拿到查询结果后，必须以 "Final Answer:" 开头给出回答
3. 回答中要包含具体数字和名称，不要只说"有若干条记录"
4. 禁止在 Final Answer 之前输出自然语言解释

正确格式：Final Answer: 客户表中共有 10 条记录，分别是张三、李四...
错误格式：根据查询结果，客户表中有 10 条记录...（缺少 Final Answer: 开头）
"""


# ─── SQL 工具描述（供 Function Calling 使用） ────

SQL_TOOL_DESCRIPTION = (
    "当用户询问关于产品、销售、客户等数据库中的业务数据时，使用此工具查询 SQLite 数据库。"
    "适用场景：统计类问题（如销售额、数量、排行）、具体数据查询（如某产品价格、某地区客户）。"
    "不适用场景：纯闲聊、编程问题、通用知识问答、不涉及数据库中业务数据的问题。"
)


# ─── SQL 安全约束说明 ─────────────────────────────

SQL_SAFETY_RULES = """
SQL 安全规则（多层防护）：
1. Prompt 层：Agent 系统提示词明确"只生成 SELECT"
2. 代码层：SafeSQLDatabase._check_safe() 在执行前拦截 DROP/DELETE/UPDATE/INSERT/ALTER/TRUNCATE/CREATE
3. 关键词按空白分词匹配（避免列名含关键词时误判）
"""


# ─── 图表生成提示词 ───────────────────────────────

VISUALIZER_DESCRIPTION = """
图表自动选择策略（QueryVisualizer.analyze）：
- 日期列 + 数值列 → 折线图（趋势）
- 分类列 + 数值列（占比） → 饼图
- 分类列 + 数值列（非占比） → 柱状图（对比）
- 日期检测在分类检测之前（避免误判）
"""
