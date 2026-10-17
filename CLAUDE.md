# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> 最后更新: 2026-06-03 | 由 /init 生成

## NL2SQL 改造（进行中）

项目正在改造成「基于专题库模型结构与血缘的自然语言转 SQL」工具，在 `feat/nl2sql` 分支上进行。
旧版本保留在标签 `v1-agentflow`。改造完成之前，本文件后面对旧模块的说明仍然有效，旧模块不要改动。

- 设计文档：`notes/2026-10-13_design_nl2sql专题库改造.md`
- 已完成（第一份计划）：元数据层 `metadata/`、真实资料转换器、模拟专题库 `data/mock_mart/`、方言转换与执行层 `nl2sql/`、评估题库 `eval/nl2sql_questions.yaml`
- 已完成（第二份计划）：SQL 作用域解析 `metadata/sqlscope.py`、加工脚本解析与补血缘 `metadata/script_parser.py`、关联推导 `metadata/relations.py`、血缘图 `lineage/graph.py`、向量索引 `metadata/index.py`（向量存在元数据库里）、样例库 `metadata/examples.py` 与 `data/mock_mart/examples/`、静态校验 `nl2sql/validator.py`、候选召回 `nl2sql/retriever.py`
- 真实专题库：资料、转换器、转换结果都在 `data/real_mart/`，这个目录不入库。真实的表名、模式名、路径不得写进任何会提交的文件
- 待做（第三份计划）：三个模型步骤与证据组装、LangGraph 编排、评估、新界面与接口、下线旧模块

```bash
python -m pytest tests -q                 # NL2SQL 单元测试，不调模型、不加载 BGE（conftest 设了 METADATA_EMBEDDER=hash），821 项
python -m metadata.importer               # 导入 MART_DIR 指向的资料：校验、解析脚本、补血缘、推导关联、收样例、算向量，生成元数据库和导入报告
python -m metadata.importer 目录 --no-index   # 不算向量，几秒导完；第一次加载 BGE 在机械盘上冷启动可能要几分钟
python -m metadata.template 模板.xlsx      # 生成空白的元数据 Excel 模板（目标已存在时不覆盖，要覆盖加 --force）
python -m data.mock_mart.build_mock       # 重新生成模拟专题库（库文件在固态盘上约 15 秒，在机械盘上约 1.5 分钟）
python data/real_mart/convert.py          # 把真实资料转成标准格式（需要本机的 data/real_mart/）
python -m metadata.importer data/real_mart   # 导入真实专题库的元数据
```

## Quick Commands

```bash
# 安装依赖
pip install -r requirements.txt
# 国内加速:
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 运行全部测试
python run_tests.py

# 快速模式（跳过 LLM 调用，仅单元测试）
python run_tests.py --quick

# 运行单个测试套件
python test_sql.py          # SQL 模块 (unittest, 18项)
python test_rag.py          # RAG 模块 (unittest, 5项)
python test_sql_tools.py    # Function Calling (unittest, 9项)
python -m pytest test_graph.py -v --tb=short   # LangGraph (pytest, 17项)

# 运行单个 pytest 用例
python -m pytest test_graph.py::test_supervisor_routing -v

# 质量评估
python run_eval.py

# 本地启动（Windows）
start.bat
# 本地启动（Linux/Mac）
bash start.sh

# 单独启动服务
uvicorn main:app --host 0.0.0.0 --port 8000     # FastAPI → http://localhost:8000/docs
streamlit run app.py --server.port 8501           # WebUI → http://localhost:8501

# Docker
docker-compose up -d
docker-compose down
```

## Environment Setup

- 配置从 **两个 `.env` 文件** 加载：项目 `.env`（优先）+ `D:/hermes-agent-home/.env`（fallback）
- 复制 `.env.example` → `.env`，填入 `DEEPSEEK_API_KEY`
- `HF_ENDPOINT=https://hf-mirror.com` 在 `config.py` 顶部设置（必须在其他 import 之前，否则 BGE 模型下载失败）
- `run_tests.py` 和 `run_eval.py` 各自硬编码读取 `D:/hermes-agent-home/.env` — 如果该路径不存在，测试会因缺 Key 失败

## Architecture

### LangGraph Workflow (核心路由逻辑)

```
START → supervisor → rag | sql | fallback
         rag → [mixed且SQL未跑 → sql] | [其他 → finalize]
         sql → [mixed且RAG未跑 → rag] | [其他 → finalize]
         finalize → END
         fallback → END
```

- **supervisor**: LLM 零样本意图分类 → `rag` / `sql` / `fallback`
- **mixed 双路**: 当意图为 `mixed` 时，RAG 和 SQL 节点会相互路由，确保两者都执行后再 finalize
- **防死循环**: `route_after_rag`/`route_after_sql` 检查对方结果是否已存在，避免无限循环
- **MemorySaver**: 通过 `thread_id` 隔离多轮对话，`make_config(thread_id)` 创建会话，`clear_memory(thread_id)` 清除

### State 定义 (`graph/state.py`)

`AgentState` 是一个 `TypedDict`，字段在各节点间流转：
- `messages` — 用 `add_messages` reducer 自动追加（不覆盖）
- `question` / `intent` / `rag_result` / `sql_result` / `final_answer` — 普通字段，节点返回部分字典即可合并

### 模块依赖关系

```
app.py (Streamlit WebUI) ──→ graph/ (LangGraph workflow)
main.py (FastAPI)       ──→ rag/ + sql/  (独立调用，不走 graph)
config.py               ←── 所有模块共用（get_llm_kwargs / get_db_uri）
prompts/                ←── rag/ + sql/ + graph/supervisor 引用
```

- `app.py` 用 `wf.stream()` 实现流式输出，通过 `st.status()` 实时展示 supervisor 意图和 agent 执行过程
- `main.py` 的 7 个 REST 端点是独立调用，不经过 LangGraph（Week 5 之前的遗留设计）

### Test Infrastructure

| 套件 | 框架 | 文件 | LLM 依赖 |
|------|------|------|:--:|
| SQL 模块 | `unittest` | `test_sql.py` | 是 |
| RAG 模块 | `unittest` | `test_rag.py` | 是 |
| FC 工具 | `unittest` | `test_sql_tools.py` | 否 |
| LangGraph | `pytest` | `test_graph.py` | 是 |
| 评估 | `pytest` | `eval/langgraph_eval.py` | 是 |

`run_tests.py --quick` 跳过 LangGraph 评估，其他套件即使需要 LLM 也会运行（需 Key）。

## Tech Stack

| 层 | 技术 | 注意 |
|----|------|------|
| LLM | DeepSeek Chat (via OpenAI 兼容 API) | 默认 provider，便宜/国内直连/中文好 |
| AI 框架 | LangChain 1.x + LangGraph 1.x | ⚠️ 禁止用 0.x chains 模块，用 LCEL |
| Embedding | BAAI/bge-small-zh-v1.5 (sentence-transformers) | 本地免费，GPU 加速 |
| 向量库 | ChromaDB | 本地持久化 `./chroma_data/` |
| 后端 | FastAPI | 异步，7 端点 |
| 前端 | Streamlit | app.py，三模块：Chat/Upload/Query |
| 数据库 | SQLite `./data/sample.db` | 3 表：products/customers/sales（72 条） |
| 部署 | Docker Compose | 4 个卷持久化：chroma_data/uploads/data/hf_cache |

## LangChain 1.x 关键约束（踩坑总结）

1. **禁止 `import langchain.chains`** — 用 LCEL 手动组合
2. **`langchain_chroma` 包不存在** — 用 `langchain_community.vectorstores.Chroma`
3. **`@tool` 装饰器语法**: `@tool(description=...)` 不是 `@tool("...")`
4. **`create_sql_agent` 必须**: `handle_parsing_errors=True` + `return_intermediate_steps=True`
5. **`SafeSQLDatabase.run()` 必须透传 `**kwargs`**（否则 `execution_options` 报错）
6. **DeepSeek 输出格式不稳定** — 通过 suffix 强制 `Final Answer:` 格式解决
7. **`matplotlib.use('Agg')` 必须在 `import pyplot` 之前**
8. **Windows 中文字体**: SimHei + `axes.unicode_minus = False`
9. **Pandas 3.0 + SQLAlchemy 2.0**: `read_sql_query` 不兼容，用 `conn.exec_driver_sql()` 替代

完整踩坑记录见 `notes/2026-05-16_pitfall_fastapi-启动踩坑.md`

## ⛔ 硬性规则（不可跳过）

1. **先测后讲**：代码完成后 → 跑测试 → 全部通过 → 六步教学法讲解 → 写入 notes/
2. **讲解六步**: 📦目标 → 📖讲透 → 🔧选型 → 💡可迁移 → 👨‍💻代码 → ⚠️常见坑
3. **评估基线**: 每个模块必须有数字指标（Recall/准确率/成功率），不做玄学优化
4. **讨论即笔记**: 任何技术讨论/问答内容自动写入 notes/

## Coding Standards

- 所有公开函数写中文 docstring + type hints
- 4 空格缩进，UTF-8 编码
- 代码注释用中文
- LLM 调用统一走 `config.get_llm_kwargs()`，不硬编码 Key
- 新增 Prompt 写入 `prompts/` 目录，不从代码里拼字符串

## Current Status (2026-06-03)

| 模块 | 状态 |
|------|:--:|
| RAG + Embedding | ✅ |
| Text-to-SQL + FC | ✅ |
| LangGraph 多Agent | ✅ |
| FastAPI (7 端点) | ✅ |
| Streamlit WebUI (流式输出) | ✅ |
| Docker 部署 | ✅ |
| 测试体系 + 评估 | ✅ |
| README + 文档 | ✅ |
| 面试冲刺 (Week 8) | ⏳ |

## 用户背景（张润泽）
- 6 年银行对公数据开发（ETL/数据库/调度），大专学历，全职转型 AI 应用开发
- 不能读英文技术文档（需翻译辅助）
- 目标城市：长三角（上海/杭州/宁波）或珠三角
- 所有讲解必须用 ETL/数据库概念做类比
- OpenAI Key 有但无法充值 → 日常开发用 DeepSeek

## 协作方式
- Hermes 负责：需求理解、任务分解、写计划、进度跟进、中文沟通、代码讲解
- Claude Code 负责：编码实现、代码审查、运行测试、修复 bug
- 桥梁文件：`.hermes/bridge.md`（每次会话结束后更新）
- 项目知识库：`notes/`（24 篇笔记，面试可直接用）
