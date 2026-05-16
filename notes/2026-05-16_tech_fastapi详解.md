# FastAPI 异步接口详解

> 2026-05-16 | tech | Hermes Agent 讲解

---

## 一、问题起源：为什么不用 Flask？

Flask 是同步框架，一个请求处理完才能处理下一个：

```
Flask 同步模式：
请求1 进来 → 处理（等数据库、等 LLM API）→ 返回 → 请求2 进来...
              ↑ 这期间整个线程在发呆等结果
```

如果 RAG 查询要 3 秒，3 个用户同时访问就要等 9 秒。

FastAPI 是异步框架：

```
FastAPI 异步模式：
请求1 进来 → 发起 LLM 调用 → 不等结果，切去处理请求2
请求2 进来 → 发起数据库查询 → 不等结果，切去处理请求3
请求1 的 LLM 返回了 → 切回来，组装响应发给用户1
```

---

## 二、async/await 本质（面试高频）

async 不是"多线程"，而是"协作式多任务"——就像一个服务员同时服务 3 桌客人：
1. 给 1 号桌点菜后不等厨房出菜，直接去 2 号桌点菜
2. 1 号桌菜好了厨房喊他，他再端过去

```python
# 同步（以前的代码）
def query_sql(question):
    result = agent.query(question)  # 卡住3秒，CPU啥也不干
    return result

# 异步（FastAPI）
async def query_sql(question):
    result = await agent.query(question)  # await = "这个要等，先处理别人"
    return result
```

**关键点：** `await` 后面的函数必须是异步的。如果 agent.query() 是同步的，await 也没用——还是阻塞。

---

## 三、数据库类比

| 数据库概念 | FastAPI 概念 |
|-----------|-------------|
| 存储过程参数 | Pydantic Model（QueryRequest）|
| 存储过程返回值 | Response Model（SQLQueryResponse）|
| 参数校验 NOT NULL | `Field(..., min_length=1)` |
| 执行计划 | 路由注册 `@app.post("/sql/query")` |
| 报错给调用方 | `raise HTTPException(400, "查询失败")` |

---

## 四、选型对比

| 框架 | 并发模型 | 自动文档 | 类型校验 | 选用 |
|------|---------|---------|---------|------|
| **FastAPI** | 异步 | ✅ Swagger | ✅ Pydantic | ✅ |
| Flask | 同步 | ❌ | ❌ | ❌ |
| Django REST | 同步为主 | ❌ | ❌ | ❌ |

---

## 五、API 端点设计

```
POST   /rag/upload      上传文档
POST   /rag/query       知识库问答
GET    /rag/documents   文档列表
DELETE /rag/documents/  删除文档
POST   /sql/query       Text-to-SQL
POST   /sql/visualize   查询+图表
GET    /sql/schema      数据库表结构
GET    /health          健康检查
```

---

## 六、常见坑

1. **async/await 的传染性** — 一个函数变 async，调它的也得 async。像病毒往上传播
2. **同步库在异步里是毒药** — 同步调用会阻塞整个事件循环
3. **启动事件不要初始化重对象** — 用懒加载（第一次请求才初始化）
4. **文件上传记得 `python-multipart`** — 忘装直接 500

---

## 七、面试话术

### 30秒版
"我用 FastAPI 给项目做了 7 个 REST API 端点，覆盖文档上传、知识库问答、Text-to-SQL 查询和图表生成。FastAPI 的异步模型能处理高并发，Pydantic 做参数校验，/docs 自动生成 Swagger 接口文档——面试官可以直接在浏览器里测试。"

### 3分钟版
"选 FastAPI 有三个原因：第一，异步高性能，不像 Flask 那样同步阻塞；第二，Pydantic 模型自动做参数校验，不合规的数据在入口就拦截，这和 ETL 的源表校验一个道理；第三，/docs 自动生成 Swagger UI，面试时打开浏览器就能演示所有接口。我在设计上做了懒加载——SQL Agent 和 LLM 连接在第一次请求才初始化，避免启动时阻塞。每个端点都定义了独立的 Request/Response 模型，保证输入输出可预测、可测试。"

---

## 相关笔记

- [[2026-05-16_tech_function-calling详解]]
- [[2026-05-16_tech_prompt-engineering详解]]
