# Swagger 自动文档详解

> 2026-05-16 | tech | 对话讲解

---

## 一、一句话

**API 的"使用说明书"，自动生成，不用手写。**

---

## 二、ETL 类比

你以前给下游系统做接口，要给接口文档：

```
接口名称：客户信息查询
调用方式：HTTP POST
URL：http://xxx/api/customer/query
参数：cust_id VARCHAR(20) 必填
返回：{cust_name, cust_type, open_date, ...}
```

这个文档是**手动写的**。改一行代码忘记更新 → 文档就废了。

**Swagger：你写代码，文档自动生成，而且是活的——可以直接点按钮测试。**

---

## 三、长什么样

浏览器打开 `http://127.0.0.1:8000/docs`：

```
┌─────────────────────────────────────────────┐
│  AgentFlow API  v2.0.0                      │
│  企业级 AI 智能助手                         │
├─────────────────────────────────────────────┤
│  RAG                                        │
│  ▼ POST /rag/upload   上传文档到知识库      │
│  ▼ POST /rag/query    知识库问答            │
│  ▼ GET  /rag/documents 列出已上传文档        │
│                                             │
│  SQL                                        │
│  ▼ POST /sql/query    Text-to-SQL 查询      │
│  ▼ POST /sql/visualize 查询并生成图表        │
│  ▼ GET  /sql/schema   数据库表结构           │
└─────────────────────────────────────────────┘
```

点开任一接口 → 看到参数说明 → 点 [Try it out] → 直接输入测试 → 看到真实返回结果。

---

## 四、为什么自动生成

```python
class SQLQueryResponse(BaseModel):
    question: str
    sql: str
    answer: str
    success: bool
    error: Optional[str] = None

@app.post("/sql/query", response_model=SQLQueryResponse)
async def query_sql(request: QueryRequest):
    ...
```

FastAPI 从 Pydantic 模型自动读取字段名和类型，生成完整的交互式文档。改代码 → 文档自动更新。

---

## 五、面试怎么用

1. `uvicorn main:app --port 8000`
2. 浏览器打开 `http://127.0.0.1:8000/docs`
3. 面试官看到一个**可以直接玩的 API 商店**

比任何 PPT 都有效——面试官亲眼看到"输入自然语言 → 返回 SQL + 结果"的全流程。

---

## 六、面试话术

> "Swagger 在 FastAPI 里是零配置的，我定义了 Pydantic 模型和路由之后，文档自动生成。面试官不需要看代码，打开 /docs 页面就能直接测试每一个接口——你现场问一个问题，系统实时返回 SQL 和图表。"

---

## 相关笔记

- [[2026-05-16_tech_fastapi详解]]
- [[2026-05-16_tech_pydantic详解]]
