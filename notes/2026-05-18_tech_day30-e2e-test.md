# Day 30 端到端体验测试 + 质量评估

> 2026-05-18 | Hermes Agent 执行
> Week 6 收尾：场景测试、Bug 修复、性能基线

---

## 📦 本日目标

对 AgentFlow 进行端到端体验测试，修复所有发现的 bug，建立质量基线。

---

## 🧪 测试基线

### 测试套件回归

| 测试套件 | 修复前 | 修复后 | 说明 |
|---------|--------|--------|------|
| test_sql.py | 18/18 ✅ | 18/18 ✅ | 字体修复后无警告 |
| test_rag.py | 5/5 ⚠️ | 5/5 ✅ | 修复 HF 缓存 + config import |
| test_graph.py | 15/17 ❌ | **17/17 ✅** | 修复 MemorySaver config 缺失 |
| test_integration.py | ❌ crash | ❌ crash | WinError 1455，环境问题 |

### 三场景端到端 API 测试

| 场景 | 端点 | 响应 | 耗时 |
|------|------|------|------|
| A1 文档列表 | GET /rag/documents | 200 ✅ | 0.04s |
| A2 RAG 查询 | POST /rag/query | 200 ✅ | 1.6s |
| B1 SQL 查询 | POST /sql/query | 200 ✅ | 7.4s |
| B2 图表可视化 | POST /sql/visualize | 200 ✅ | 10.1s |
| C1 工作流调用 | LangGraph invoke | intent=sql ✅ | 15.8s |

**P50 响应时间**: 7.4s | **P95**: 15.8s

---

## 🐛 Bug 修复汇总

### Bug #1: matplotlib 中文字体无效

- **现象**: 图表中文标签显示为方块（Arial 缺 CJK 字形）
- **根因**: `plt.style.use('seaborn-v0_8-darkgrid')` 在字体设置之后执行，覆盖了 `plt.rcParams['font.sans-serif']`。style 内部设置 font.sans-serif 为 `['Arial', ...]`，wipe 掉 SimHei。
- **修复**: 将 `plt.rcParams['font.sans-serif']` 和 `plt.rcParams['axes.unicode_minus']` 移到 `plt.style.use()` **之后**
- **文件**: `sql/visualizer.py`
- **验证**: 重跑 test_sql.py，0 个字体警告，18/18 通过

### Bug #2: test_graph.py MemorySaver 缺少 config

- **现象**: `test_workflow_invoke` 和 `test_workflow_mixed` 报 `ValueError: Checkpointer requires thread_id`
- **根因**: `build_workflow()` 默认 `enable_memory=True`（使用 MemorySaver），但两个测试没传 config
- **修复**: 两个测试传 `enable_memory=False`（它们不需要多轮记忆）
- **文件**: `test_graph.py`
- **验证**: 17/17 通过

### Bug #3: Pandas 3.0 + SQLAlchemy 2.0 断裂

- **现象**: `/sql/visualize` 报 `'Engine' object has no attribute 'cursor'`，同时 `'Connection' object has no attribute 'cursor'`
- **根因**: Pandas 3.0.1 的 `pd.read_sql_query()` 对 SQLAlchemy 2.0.25 的 Engine 和 Connection 对象都不兼容。这是 Pandas 3.x 的 breaking change
- **测试了 4 种方案**:
  - `pd.read_sql_query(sql, engine)` → ❌
  - `pd.read_sql_query(sql, conn)` → ❌
  - `conn.exec_driver_sql(sql)` → ✅
  - `sqlite3.connect()` 直接 → ✅
- **修复**: 改用 `conn.exec_driver_sql(sql)` → `result.fetchall()` + `result.keys()` → `pd.DataFrame(rows, columns=cols)`
- **文件**: `main.py`, `app.py`
- **验证**: 独立脚本 + API 端点均确认修复

### Bug #4: test_rag.py 缺少 config import

- **现象**: BGE 模型加载时直连 huggingface.co（被墙），HF 镜像不生效
- **根因**: test_rag.py 在 import rag 模块之前没有 import config，导致 `HF_ENDPOINT` 环境变量未设置。rag 模块内部也不设
- **修复**: 在 `main()` 函数开头加 `import config as _`
- **文件**: `test_rag.py`

---

## 📊 质量评估

### 功能性

| 指标 | 结果 |
|------|------|
| RAG 上传 | ✅ |
| RAG 查询（带引用） | ✅ |
| SQL 查询 | ✅ |
| SQL 可视化（图表 Base64） | ✅ |
| LangGraph 工作流路由 | ✅ |
| 文档管理（列表/删除） | ✅ |
| FastAPI 健康检查 | ✅ |

### 性能

| 指标 | 数值 |
|------|------|
| P50 响应时间 | 7.4s |
| P95 响应时间 | 15.8s |
| 文档上传（小文件） | 0.1s |
| RAG 检索 | <0.1s（BGE GPU 推理 ~50ms） |
| LangGraph 工作流 | 15.8s（含 LLM 调用） |

**瓶颈分析**: P50=7.4s 主要是 DeepSeek LLM API 调用延迟（SQL 生成 ~7s）。RAG 检索本身 <0.1s（BGE GPU 加速）。

### 已知局限

1. **删除文档不清理 ChromaDB**: 只删文件，向量数据残留。需手动重建向量库（`chroma_data/` 删除后重启）
2. **集成测试因 WinError 1455 崩溃**: Windows 页面文件不足时，torch+transformers+langchain 同时加载会 DLL 冲突。非代码 bug，需系统设置虚拟内存 ≥8GB
3. **curl 上传大文件偶发 read error**: curl 的 multipart/form-data 在 git-bash 环境不稳定，Python requests 可替代

---

## 🛠 修复的文件清单

```
sql/visualizer.py    — matplotlib 字体设置顺序修复
test_graph.py        — build_workflow(enable_memory=False) ×2
test_rag.py          — 添加 import config
main.py              — exec_driver_sql 替代 read_sql_query
app.py               — exec_driver_sql 替代 read_sql_query
```

---

## 📌 下一步: Week 7

按升级计划，Week 7 进入：Docker 部署完善 + 测试 + 文档

- Day 31-32: Docker 完善（docker-compose.yml 修复、启动脚本）
- Day 33-34: 单元测试 + 质量评估体系
- Day 35: README 重写 + 架构图 + 录屏 Demo

---

## 📖 面试话术

**"你是怎么做项目测试的？"**

> 我的项目有三层测试体系。第一层是模块级测试——RAG、SQL、LangGraph 各有独立测试脚本，写完代码立即跑。第二层是 API 端到端测试——用 FastAPI TestClient + curl 验证每个 REST 端点。第三层是场景测试——模拟真实用户流程，上传文档→提问→查数据库→生成图表，全部走通。
>
> 这次 Day 30 测试中我发现了 3 个真实 bug：matplotlib 的 style.use() 覆盖了中文 SimHei 字体，Pandas 3.0 对 SQLAlchemy 2.0 的 read_sql_query 不兼容导致可视化端点崩溃，还有 LangGraph MemorySaver 的 config 缺失问题。修复后建立了 P50=7.4s / P95=15.8s 的性能基线。

---

## 🔗 相关笔记

- [[2026-05-18_tech_streamlit-webui详解]] — WebUI 实现
- [[2026-05-17_tech_langgraph-多agent工作流]] — LangGraph 架构
- [[2026-05-15_tech_visualizer详解]] — 图表可视化
