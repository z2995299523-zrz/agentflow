# Pydantic 数据校验模型详解

> 2026-05-16 | tech | 对话讲解

---

## 一、一句话

**给数据"上锁"——不符合规则的数据根本进不来。**

---

## 二、ETL 类比（6年数据开发经验直接对应）

你以前做 DataStage/Kettle，源表数据进来第一步：

```
源表字段：cust_id VARCHAR(20), open_date DATE, balance DECIMAL(18,2)
          ↓
ETL 校验：cust_id 不能为空、open_date 必须是合法日期、balance 不能是负数
          ↓
不合格 → 错误表；合格 → ODS 层
```

**Pydantic 做一模一样的事，只是在 API 入口做。**

---

## 三、语法对照表

| Pydantic 语法 | 含义 | 等价 SQL |
|--------------|------|---------|
| `question: str` | 字符串类型 | `VARCHAR` |
| `Field(...)` | 必填 | `NOT NULL` |
| `min_length=1` | 最少 1 字符 | `CHECK(LEN(x) >= 1)` |
| `max_length=500` | 最多 500 字符 | `CHECK(LEN(x) <= 500)` |
| `ge=0` | 大于等于 0 | `CHECK(x >= 0)` |
| `description="..."` | 字段说明 | `COMMENT ON COLUMN` |
| `Optional[str]` | 可为空 | 不加 `NOT NULL` |

---

## 四、具体例子

```python
class QueryRequest(BaseModel):
    question: str = Field(..., description="自然语言问题", min_length=1)
```

如果有人发 `{"question": ""}`：
→ Pydantic 直接拦下，返回 HTTP 422
→ **根本不进你的业务逻辑**

---

## 五、为什么不用普通 dict

```python
# ❌ 以前的做法
def query_sql(data: dict):
    question = data.get("question")   # 可能是 None
    if not question:                  # 手动校验
        return {"error": "不能为空"}
    if len(question) > 500:           # 手动校验
        return {"error": "太长"}
    # 然后才干活...

# ✅ 用 Pydantic
def query_sql(request: QueryRequest):
    # request.question 一定是非空字符串，已校验
    # 直接干活，一行校验代码都不用写
```

**Pydantic 把校验逻辑从业务代码里抽走了。** 就像 ETL 工具把数据质量检查从存储过程里抽走一样。

---

## 六、双向校验

```python
@app.post("/sql/query", response_model=SQLQueryResponse)
async def query_sql(request: QueryRequest):
    #                   ↑ 入口校验           ↑ 出口校验
```

| 方向 | 校验什么 | 防止什么 |
|------|---------|---------|
| `QueryRequest`（入口） | 用户传的参数合不合法 | 空字符串、缺字段、类型错误 |
| `SQLQueryResponse`（出口） | 返回数据格式对不对 | 字段名写错、类型不匹配、漏字段 |

后端代码不小心写了 `sql` 而模型里叫 `sql_statement` → FastAPI **启动时报错**，不用等调用才发现。

---

## 七、面试话术

> "Pydantic 本质是数据校验层。我在 API 入口用 QueryRequest 做参数校验——空字符串、缺字段在 Pydantic 层就拦截了，不污染业务逻辑。出口用 Response 模型保证返回格式一致性。这和我以前做 ETL 时源表字段校验是一个道理——脏数据在管道入口就处理掉，不进下游。"

---

## 相关笔记

- [[2026-05-16_tech_fastapi详解]]
- [[2026-05-16_tech_swagger详解]]
