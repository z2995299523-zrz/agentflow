# AgentFlow — Project Context for Codex

> 最后更新: 2026-10-18 | 详细说明见 `CLAUDE.md`，进度见 `.hermes/bridge.md`

## Project Overview

AgentFlow 是「基于专题库模型结构与血缘的自然语言转 SQL」工具（查数副驾驶，个人作品项目，面试展示用）：用自然语言描述查数需求，结合专题库的数据字典、字段血缘、加工脚本和历史样例，生成可审核的 GaussDB / PostgreSQL 查询 SQL，写清口径、用表依据和关联说明；口径有分岔时反问一次，支持连续追问。

旧版本（文档问答 RAG、旧 SQL Agent、LangGraph 意图路由）保留在标签 `v1-agentflow`，代码已删除。

## Project Structure

```
agentflow/
├── app.py              # Streamlit 界面：查数页、元数据页
├── main.py             # FastAPI 接口：/nl2sql/query、/metadata/tables、/metadata/lineage、/health
├── config.py           # 配置：模型、MART_DIR、SQL_DIALECT、EXECUTOR、LLM_TIMEOUT
├── run_tests.py        # 单元测试入口（1475 项，不调模型）
├── run_eval.py         # 评估入口（模拟库三组对照）
├── metadata/           # 元数据层：模板、读取、校验、脚本解析、关联推导、向量、样例、导入、只读入口
├── lineage/graph.py    # 血缘图与关联图
├── nl2sql/             # 查询流水线：schemas、llm、parser、retriever、evidence、planner、generator、validator、output、workflow、service、executor、dialect
├── prompts/nl2sql.py   # 三个模型步骤的提示词与证据渲染
├── eval/               # 题库、结果比对、评估程序、报告
├── data/mock_mart/     # 模拟专题库（银行对公存贷款，三层 14 张表）
├── data/real_mart/     # 真实专题库资料（不入库，真实名称不得写进任何入库文件）
├── tests/              # 单元测试
└── notes/              # 设计文档与技术笔记
```

## Tech Stack

- Python 3.12
- LLM: DeepSeek Chat（OpenAI 兼容接口，langchain-openai），Key 从项目 `.env` 或 Hermes `.env` 读
- 编排: LangGraph 1.x；SQL 解析与校验: sqlglot 25.x
- 向量模型: BAAI/bge-small-zh-v1.5（本地，sentence-transformers），向量存进 SQLite 元数据库
- 模拟库执行: DuckDB；Web: FastAPI + Streamlit

## Coding Standards
- 所有公开函数写中文 docstring + type hints
- 4 空格缩进，UTF-8 编码
- 代码注释用中文
- LLM 调用统一走 `config.get_llm_kwargs()`，不硬编码 Key
- 新增 Prompt 写入 `prompts/` 目录，不从代码里拼字符串

## ⛔ 硬性规则（不可跳过）

1. **先测后讲**：代码完成后 → 跑测试 → 全部通过 → 六步教学法讲解 → 写入 notes/
2. **讲解六步**: 📦目标 → 📖讲透 → 🔧选型 → 💡可迁移 → 👨‍💻代码 → ⚠️常见坑
3. **评估基线**: 每个模块必须有数字指标（Recall/准确率/成功率），不做玄学优化
4. **讨论即笔记**: 任何技术讨论/问答内容自动写入 notes/

## 用户背景（张润泽）
- 6年银行对公数据开发（ETL/数据库/调度）
- 大专学历，全职转型 AI 应用开发
- 不能读英文技术文档（需翻译辅助）
- 目标城市：长三角（上海/杭州/宁波）或珠三角
- 所有讲解必须用 ETL/数据库概念做类比
- OpenAI Key 有但无法充值 → 日常开发用 DeepSeek

## 协作方式
- Hermes 负责：需求理解、任务分解、写计划、进度跟进、中文沟通、代码讲解
- Codex 负责：编码实现、代码审查、运行测试、修复 bug
- 桥梁文件：`.hermes/bridge.md`（每次会话结束后更新）
- 项目知识库：`notes/`（39 篇笔记，面试可直接用）
