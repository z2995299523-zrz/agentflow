AgentFlow — 偿还技术债: RAG 评估基线 + 集成测试

## 任务1: RAG 评估基线 (eval/)

创建 3 个中文测试文档 + 10 个评估问题 + 评估脚本。

### 1.1 创建 data/test_docs/ 目录，放 3 个测试文档

**data/test_docs/公司考勤制度.txt:**
```
## 考勤制度

### 年假政策
员工入职满1年享有5天年假，满3年享有10天年假，满5年享有15天年假。
年假需提前3个工作日申请，经部门主管审批。未使用的年假可累积至下一年度，最多累积20天。

### 病假政策
员工因病需休假，应提供医院开具的病假证明。病假3天以内由部门主管审批，3天以上由HR审批。
病假期间薪资按基本工资的80%发放。

### 加班政策
加班需提前申请并经主管批准。工作日加班按1.5倍薪资计算，周末加班按2倍计算，法定节假日按3倍计算。
每月加班时长不得超过36小时。
```

**data/test_docs/产品手册.txt:**
```
## 智能办公系统 V3.0 产品手册

### 系统概述
智能办公系统V3.0是一款面向中小企业的综合办公平台，包含考勤管理、项目管理、文档协作三大模块。
系统采用微服务架构，前端使用React，后端使用Java Spring Boot，数据库使用PostgreSQL 14。

### 技术规格
- 支持并发用户数：500人
- 响应时间：页面加载<2秒，API响应<500ms
- 数据存储：PostgreSQL 14 + Redis 7 缓存
- 部署方式：Docker + Kubernetes，支持私有云和公有云部署

### 权限管理
系统支持三级权限：管理员（全部权限）、部门主管（本部门数据）、普通员工（个人数据）。
权限采用RBAC（基于角色的访问控制）模型。

### 数据安全
所有数据传输使用HTTPS加密，敏感数据（密码、身份证号）使用AES-256加密存储。
系统每24小时自动备份数据库，备份保留30天。
```

**data/test_docs/数据库配置指南.txt:**
```
## AgentFlow 数据库配置指南

### 支持的数据库
AgentFlow 支持以下数据库后端：
- SQLite：开发环境默认，零配置，数据存储在 data/sample.db
- PostgreSQL：生产环境推荐，需配置 PG_HOST、PG_PORT、PG_USER、PG_PASSWORD、PG_DATABASE
- MySQL 8.0+：实验性支持

### 配置方式
1. 复制 .env.example 为 .env
2. 设置 DB_TYPE=sqlite 或 DB_TYPE=postgresql
3. 如使用 PostgreSQL，配置对应的 PG_* 环境变量
4. 重启服务生效

### 连接池配置
- 最大连接数：默认20，可通过 DB_MAX_CONNECTIONS 调整
- 连接超时：默认30秒
- 空闲超时：默认600秒

### 安全建议
- 生产环境不要使用 SQLite
- 数据库密码使用环境变量，不要硬编码在代码中
- 定期备份数据库文件
```

### 1.2 创建 eval/rag_test_questions.json — 10 题 + 标准答案

```json
[
  {
    "question": "入职满1年的员工有多少天年假？",
    "ground_truth": "5天",
    "expected_sources": ["公司考勤制度"],
    "type": "factual"
  },
  {
    "question": "年假最多可以累积多少天？",
    "ground_truth": "20天",
    "expected_sources": ["公司考勤制度"],
    "type": "factual"
  },
  {
    "question": "智能办公系统V3.0用什么数据库？",
    "ground_truth": "PostgreSQL 14",
    "expected_sources": ["产品手册"],
    "type": "factual"
  },
  {
    "question": " AgentFlow 生产环境推荐用什么数据库？",
    "ground_truth": "PostgreSQL",
    "expected_sources": ["数据库配置指南"],
    "type": "factual"
  },
  {
    "question": "数据库连接池最大连接数默认是多少？",
    "ground_truth": "20",
    "expected_sources": ["数据库配置指南"],
    "type": "factual"
  },
  {
    "question": "病假3天以上由谁审批？",
    "ground_truth": "HR",
    "expected_sources": ["公司考勤制度"],
    "type": "factual"
  },
  {
    "question": "周末加班薪资如何计算？",
    "ground_truth": "按2倍薪资计算",
    "expected_sources": ["公司考勤制度"],
    "type": "factual"
  },
  {
    "question": "智能办公系统支持哪三级权限？",
    "ground_truth": "管理员、部门主管、普通员工",
    "expected_sources": ["产品手册"],
    "type": "factual"
  },
  {
    "question": "系统有多少种加密方式保护数据安全？",
    "ground_truth": "HTTPS传输加密和AES-256存储加密",
    "expected_sources": ["产品手册"],
    "type": "reasoning"
  },
  {
    "question": "今天北京的天气怎么样？",
    "ground_truth": "无相关信息",
    "expected_sources": [],
    "type": "no_answer"
  }
]
```

### 1.3 创建 eval/rag_eval.py — 评估脚本

核心功能：
1. 加载测试文档并写入 ChromaDB
2. 对每个问题执行 rag.ask()
3. 计算 5 项指标：

```python
"""RAG 检索质量评估 — 自实现指标（ETL对账风格）"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def load_questions(path=None):
    """加载评估问题集"""
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "rag_test_questions.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def load_test_docs():
    """加载测试文档到 ChromaDB"""
    from rag.loader import load_single_file, split_documents
    from rag.vectorstore import create_vectorstore
    
    docs_dir = os.path.join(os.path.dirname(__file__), "..", "data", "test_docs")
    all_chunks = []
    for fname in os.listdir(docs_dir):
        if fname.endswith(".txt"):
            fpath = os.path.join(docs_dir, fname)
            docs = load_single_file(fpath)
            chunks = split_documents(docs)
            for c in chunks:
                c.metadata["source"] = fname
            all_chunks.extend(chunks)
    
    vs = create_vectorstore(all_chunks)
    return vs

def recall_at_k(question, ground_truth, vectorstore, k=5):
    """Recall@K: 命中的相关文档 / 总相关文档"""
    retriever = vectorstore.as_retriever(search_kwargs={"k": k})
    docs = retriever.invoke(question)
    # 简化：检查返回文档的source是否在 expected_sources 中
    sources = set(d.metadata.get("source", "") for d in docs)
    expected = set(question.get("expected_sources", []))
    if not expected:
        return 1.0 if not sources else 0.0  # no_answer: 返回空=正确
    hits = len(sources & expected)
    return hits / len(expected) if expected else 0.0

def precision_at_k(question, ground_truth, vectorstore, k=5):
    """Precision@K: 命中的相关文档 / 返回的K个结果"""
    retriever = vectorstore.as_retriever(search_kwargs={"k": k})
    docs = retriever.invoke(question)
    sources = set(d.metadata.get("source", "") for d in docs)
    expected = set(question.get("expected_sources", []))
    if not sources:
        return 1.0 if not expected else 0.0
    hits = len(sources & expected)
    return hits / len(sources) if sources else 0.0

def mrr(question, ground_truth, vectorstore, k=5):
    """MRR: 1/第一个相关结果的排名"""
    retriever = vectorstore.as_retriever(search_kwargs={"k": k})
    docs = retriever.invoke(question)
    expected = set(question.get("expected_sources", []))
    if not expected:
        return 1.0 if not docs else 0.0
    for i, doc in enumerate(docs):
        if doc.metadata.get("source") in expected:
            return 1.0 / (i + 1)
    return 0.0

def answer_relevance_score(answer, ground_truth):
    """简化版：检查答案是否包含标准答案关键词"""
    if not answer or not ground_truth:
        return 0.0
    # 简单包含检查
    gt_lower = ground_truth.lower()
    ans_lower = answer.lower()
    if gt_lower in ans_lower:
        return 1.0
    # 部分匹配：关键词重叠
    gt_words = set(gt_lower.split())
    ans_words = set(ans_lower.split())
    if not gt_words:
        return 0.0
    overlap = len(gt_words & ans_words)
    return overlap / len(gt_words)

def run_evaluation(questions=None, vectorstore=None):
    """运行完整评估，输出报告"""
    if questions is None:
        questions = load_questions()
    if vectorstore is None:
        vectorstore = load_test_docs()
    
    from rag.chain import ask
    
    recall_scores = []
    precision_scores = []
    mrr_scores = []
    relevance_scores = []
    details = []
    
    for q in questions:
        # 检索评估
        rec = recall_at_k(q, q["ground_truth"], vectorstore)
        prec = precision_at_k(q, q["ground_truth"], vectorstore)
        m = mrr(q, q["ground_truth"], vectorstore)
        
        # 生成回答评估
        result = ask(q["question"], vectorstore=vectorstore)
        answer = result.get("answer", "")
        relevance = answer_relevance_score(answer, q["ground_truth"])
        
        recall_scores.append(rec)
        precision_scores.append(prec)
        mrr_scores.append(m)
        relevance_scores.append(relevance)
        
        details.append({
            "question": q["question"],
            "type": q["type"],
            "recall@5": rec,
            "precision@5": prec,
            "mrr": m,
            "answer_relevance": relevance,
            "answer_preview": answer[:100],
        })
    
    return {
        "total_questions": len(questions),
        "avg_recall@5": sum(recall_scores) / len(recall_scores),
        "avg_precision@5": sum(precision_scores) / len(precision_scores),
        "avg_mrr": sum(mrr_scores) / len(mrr_scores),
        "avg_relevance": sum(relevance_scores) / len(relevance_scores),
        "details": details,
    }

if __name__ == "__main__":
    print("=" * 60)
    print("RAG 检索质量评估")
    print("=" * 60)
    
    result = run_evaluation()
    
    print(f"\n📊 基线指标 (共{result['total_questions']}题):")
    print(f"   Recall@5:      {result['avg_recall@5']:.0%}  (目标 ≥80%)")
    print(f"   Precision@5:   {result['avg_precision@5']:.0%}  (目标 ≥60%)")
    print(f"   MRR:           {result['avg_mrr']:.2f}  (目标 ≥0.70)")
    print(f"   Answer Relev:  {result['avg_relevance']:.0%}  (目标 ≥80%)")
    
    print(f"\n📋 逐题详情:")
    for d in result["details"]:
        ok = "✅" if d["recall@5"] >= 0.8 else "⚠️"
        print(f"   {ok} [{d['type']:10s}] R={d['recall@5']:.0%} P={d['precision@5']:.0%} M={d['mrr']:.2f} | {d['question'][:40]}...")
    
    print("\n" + "=" * 60)
```

### 验证
1. python eval/rag_eval.py
2. 输出 4 项指标 + 逐题详情
3. Recall@5 ≥ 80% 为合格

---

## 任务2: 集成测试 (test_integration.py)

创建集成测试，验证 RAG + SQL 通过 FastAPI 端点端到端可用。

```python
"""集成测试：RAG + SQL 端到端"""
import pytest
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_health():
    """健康检查"""
    r = client.get("/health")
    assert r.status_code == 200

def test_rag_query():
    """RAG 查询端点"""
    r = client.post("/rag/query", json={"question": "年假有多少天？"})
    assert r.status_code in (200, 503)  # 200=成功, 503=向量库空

def test_sql_query():
    """SQL 查询端点"""
    r = client.post("/sql/query", json={"question": "产品表有多少条记录？"})
    assert r.status_code == 200
    data = r.json()
    assert "answer" in data

def test_sql_schema():
    """数据库 Schema 端点"""
    r = client.get("/sql/schema")
    assert r.status_code == 200

def test_rag_documents():
    """文档列表端点"""
    r = client.get("/rag/documents")
    assert r.status_code in (200, 503)
```

## 验证
1. python -m pytest test_integration.py -v --tb=short
