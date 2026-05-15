# Text-to-SQL Agent 开发计划

> 日期: 2026-05-15
> 对应升级计划: Week 3 Day 11-12
> 执行者: Claude Code (通过 Hermes 委托)

---

## 任务1: 创建示例数据库

### 文件: `data/init_sample_db.py`

创建一个 SQLite 数据库 `data/sample.db`，包含以下表：

#### 1. 产品表 (products)
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PRIMARY KEY | 产品ID |
| product_name | TEXT | 产品名称 |
| category | TEXT | 类别（电子产品/日用品/食品） |
| unit_price | REAL | 单价 |

#### 2. 销售表 (sales)
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PRIMARY KEY | 销售ID |
| product_id | INTEGER | 产品ID（外键） |
| quantity | INTEGER | 销售数量 |
| sale_date | TEXT | 销售日期 (YYYY-MM-DD) |
| region | TEXT | 地区（华东/华南/华北/华中） |

#### 3. 客户表 (customers)
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PRIMARY KEY | 客户ID |
| customer_name | TEXT | 客户名称 |
| region | TEXT | 地区 |
| level | TEXT | 客户等级（A/B/C） |

插入至少 10 条产品、50 条销售记录、10 条客户记录，覆盖 3-4 个月的数据，让查询有足够的多样性。

---

## 任务2: 实现 sql/agent.py

### 核心类: SQLQueryAgent

```python
class SQLQueryAgent:
    """
    Text-to-SQL 查询代理
    
    将自然语言问题转换为 SQL 查询，执行后返回自然语言解释。
    底层使用 LangChain SQL Agent，自动获取数据库 Schema 并注入 Prompt。
    
    安全限制：只允许 SELECT 查询，拒绝 DROP/DELETE/UPDATE/INSERT/ALTER/TRUNCATE。
    """
    
    def __init__(self, db_uri: str = None):
        """初始化 Agent，连接数据库"""
        
    def query(self, question: str) -> dict:
        """
        用自然语言查询数据库
        
        Args:
            question: 自然语言问题，如 "上个月销售额最高的5个产品是哪些？"
            
        Returns:
            dict: {
                "question": str,      # 原始问题
                "sql": str,           # 生成的 SQL
                "answer": str,        # 自然语言回答
                "success": bool,      # 是否成功
                "error": str|None     # 错误信息（如有）
            }
        """
    
    def get_schema(self) -> str:
        """获取数据库表结构信息"""
```

### 技术约束（重要！）

1. **LangChain 1.x 兼容**：
   - 使用 `langchain_community.utilities.SQLDatabase` 连接数据库
   - 使用 `langchain_community.agent_toolkits.SQLDatabaseToolkit` 和 `create_sql_agent`
   - 或使用 `langgraph.prebuilt.create_react_agent` + SQLDatabaseToolkit（如果 create_sql_agent 在 1.x 中不可用）
   - ⚠️ 不要用 `langchain.chains.SQLDatabaseChain`（已废弃）
   - ⚠️ 不要用 `langchain_experimental`（不稳定）

2. **LLM 配置**：
   - 使用 `from config import get_llm_kwargs`
   - `llm = ChatOpenAI(**get_llm_kwargs())`
   - 默认用 DeepSeek

3. **数据库连接**：
   - 使用 `from config import get_db_uri` 获取连接字符串
   - 默认 SQLite: `sqlite:///./data/sample.db`

4. **安全限制**：
   - 在 execute 前检查 SQL 是否包含危险关键词
   - 危险关键词: DROP, DELETE, UPDATE, INSERT, ALTER, TRUNCATE, CREATE
   - 发现危险操作时抛出异常并返回错误信息

5. **代码规范**：
   - 所有公开方法有中文 docstring
   - 使用 type hints
   - 4 空格缩进
   - UTF-8 编码
   - 关键逻辑加中文注释解释"为什么这么写"

### Agent 配置建议

```python
# 创建 SQL Agent 的方式（按优先级尝试）：
# 方式1: create_sql_agent (langchain_community)
from langchain_community.agent_toolkits import create_sql_agent, SQLDatabaseToolkit
toolkit = SQLDatabaseToolkit(db=db, llm=llm)
agent = create_sql_agent(llm=llm, toolkit=toolkit, verbose=True)

# 方式2: 如果方式1不可用，用 langgraph
from langgraph.prebuilt import create_react_agent
agent = create_react_agent(llm=llm, tools=toolkit.get_tools())
```

### Prompt 前缀建议

在 Agent 的 prefix 中加入中文指令：
- "你是一个 SQL 查询助手。根据用户问题生成 SQLite 语法的 SELECT 查询。"
- "只生成 SELECT 语句，不要使用 DROP/DELETE/UPDATE/INSERT 等写操作。"
- "如果问题涉及时间，注意日期格式为 YYYY-MM-DD。"
- "查询结果如果为空，诚实告诉用户没有找到匹配的数据。"

---

## 任务3: 实现 sql/__init__.py

```python
"""
sql 模块 — Text-to-SQL 自然语言数据库查询

提供：
- SQLQueryAgent: 核心查询代理
- 安全校验工具函数
"""
from .agent import SQLQueryAgent

__all__ = ["SQLQueryAgent"]
```

---

## 任务4: 创建测试脚本 test_sql.py

测试用例：
1. 基本查询："有哪些产品类别？"
2. 聚合查询："每个类别的产品数量是多少？"
3. 排序查询："销售额最高的5个产品是哪些？"
4. 时间查询："2024年1月的销售额是多少？"
5. 多表关联："华东地区的客户买了哪些产品？"
6. 安全检查：尝试执行 DROP TABLE（应被拒绝）

测试格式：每个查询打印问题 → SQL → 回答 → PASS/FAIL

---

## 执行顺序

1. 先创建 `data/init_sample_db.py` → 运行生成数据库
2. 实现 `sql/agent.py`
3. 实现 `sql/__init__.py`
4. 创建 `test_sql.py` → 运行所有测试
5. 如有报错，修复后重新测试
6. 全部通过后汇报结果

---

## 验收标准

- [ ] 示例数据库创建成功（3 张表，足够测试数据）
- [ ] SQLQueryAgent.query() 能正确回答 5 个基础查询
- [ ] 生成的 SQL 语法正确（SQLite 兼容）
- [ ] 危险操作被正确拦截
- [ ] 所有测试通过
- [ ] 代码有完整的中文注释和 docstring
