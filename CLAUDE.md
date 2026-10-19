# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> 最后更新: 2026-10-19 | 界面从 Streamlit 换成 React + Ant Design（前后端分开），新增 SSE 流式查数和会话历史

## 项目概述

AgentFlow 现在是「基于专题库模型结构与血缘的自然语言转 SQL」工具（查数副驾驶）：用自然语言描述查数需求，结合专题库的数据字典、字段血缘、加工脚本和历史样例，生成可审核的 GaussDB / PostgreSQL 查询 SQL。改造在 `feat/nl2sql` 分支上完成；旧版本（文档问答、旧 SQL Agent、意图路由）保留在标签 `v1-agentflow`，代码已经删除。

- 设计文档：`notes/2026-10-13_design_nl2sql专题库改造.md`
- 真实专题库：资料、转换器、转换结果、评估文件和报告都在 `data/real_mart/`，这个目录不入库。真实的表名、模式名、路径不得写进任何会提交的文件

## Quick Commands

```bash
# 安装依赖（国内加速加 -i https://pypi.tuna.tsinghua.edu.cn/simple）
pip install -r requirements.txt

python run_tests.py                       # 单元测试，1552 项，不调模型、不加载 BGE（--quick 效果相同）
python -m pytest tests/test_workflow.py -q  # 单个测试文件

python -m metadata.importer               # 导入 MART_DIR 指向的资料：校验、解析脚本、补血缘、推导关联、收样例、算向量
python -m metadata.importer 目录 --no-index   # 不算向量，几秒导完；第一次加载 BGE 在机械盘上冷启动可能要几分钟
python -m metadata.template 模板.xlsx      # 生成空白的元数据 Excel 模板（目标已存在时不覆盖，要覆盖加 --force）
python -m data.mock_mart.build_mock       # 重新生成模拟专题库（库文件在固态盘上约 15 秒，在机械盘上约 1.5 分钟）
python data/real_mart/convert.py          # 把真实资料转成标准格式（需要本机的 data/real_mart/）

python run_eval.py                        # 模拟库三组对照评估（调用 DeepSeek，40 题，约 3 分钟），报告写到 eval/reports/
python run_eval.py --configs C --ids B01,K02   # 只跑部分配置和题目
python run_eval.py --static data/real_mart/eval_questions.yaml --mart data/real_mart --out data/real_mart/eval   # 真实库静态评估

uvicorn main:app --port 8000              # 接口 → http://localhost:8000/docs（启动后在后台预热流水线）
cd web && npm install && npm run dev      # 界面开发 → http://localhost:5173，/api 转给 8000（EXECUTOR=duckdb 时展示模拟库的执行结果）
cd web && npm run build                   # 前端类型检查加构建，产物在 web/dist；只做类型检查用 npm run typecheck
start.bat                                 # Windows 同时启动接口和界面（http://localhost:8080）；Linux/Mac 用 bash start.sh；Docker 用 docker-compose up -d
```

## Environment Setup

- 配置从 **两个 `.env` 文件** 加载：项目 `.env`（优先）+ `D:/hermes-agent-home/.env`（fallback），见 `config.py`
- 复制 `.env.example` → `.env`，填入 `DEEPSEEK_API_KEY`；`MART_DIR`、`SQL_DIALECT`、`EXECUTOR`、`LLM_TIMEOUT` 的说明在模板里
- `HF_ENDPOINT=https://hf-mirror.com` 在 `config.py` 顶部设置（必须在其他 import 之前，否则 BGE 模型下载失败）
- 单元测试的 `tests/conftest.py` 设了 `METADATA_EMBEDDER=hash`，不加载 BGE
- 依赖版本：fastapi ≥ 0.124 加 starlette ≥ 0.49.1；langgraph 限定 1.x；sqlglot 限定 25.x
- 前端：Node ≥ 20.19；React 19、antd 6、React Flow 12、Vite 8、TypeScript 5.9，版本写死在 `web/package.json`（选的都是发布满两周的版本），国内装依赖用 `--registry=https://registry.npmmirror.com`

## Architecture

### 现有服务器部署

接手服务器发布时先读 `deploy/CLAUDE_CODE_SERVER_DEPLOYMENT.md`。当前线上使用 Nginx + systemd + 独立 Python 环境，HTTPS 8447；真实专题库资料在 Git 外通过校验快照部署，入口有访问密码，`EXECUTOR=none`，只生成并校验 SQL。代码更新、真实资料更新、会话备份和回退的具体命令都在该文件中。Docker Compose 保留为另一种启动方式。

### 元数据层（导入时一次算好）

```
四张清单（表、字段、血缘、关联关系）+ 加工脚本 + 历史样例
  → metadata/loader 读取 → checks 校验 → script_parser 解析脚本、补血缘 → relations 推导关联
  → examples 收样例（入库前过静态校验）→ index 算字段和样例向量 → store 写进 SQLite 元数据库
```

- `metadata/store.py` 的 `MetadataStore` 是只读入口，构造时全部读进内存
- `metadata/sqlscope.py` 作用域解析：字段穿过 CTE、UNION、子查询、临时表追溯到实体表
- `metadata/codes.py` 代码字段配名称字段（「××代码」配「××名称」、`_cd` 配 `_nm`）和码值表，证据提示和 R9 共用
- `lineage/graph.py` 血缘图与关联图：字段上游链路（每一跳带加工表达式、过滤条件、关联条件）、同源对比、表的上下游、关联路径

### 查询流水线（`nl2sql/`，七步里三步调模型）

```
QueryRequest → ① parse(模型) → ② recall → ③ evidence → ④ plan(模型) → ⑤ generate(模型) → ⑥ validate → ⑦ output → QueryResult
  ① 缺对象或指标时反问，直接去 ⑦；④ need_upstream：上溯一层回到 ②（最多 2 层）；need_clarify、cannot_answer 直接去 ⑦
  ④ ok：只说「日均」没说统计区间时 clarify_daily_average 改成反问；prune_plan 去掉用不到的表（孤立表、只用关联键关联专题层的维表，记进 dropped_tables，⑤ 再用就是 R8 错误）再去 ⑤
  ⑥ 有错误：带着错误回到 ⑤（最多修正 2 次）
```

- `schemas.py`：各步骤的输入输出，是模块之间的契约；`llm.py`：模型调用（`ChatModelClient`、测试用的 `ScriptedLLM`、带重试的 `structured_call`）
- `parser.py`、`planner.py`、`generator.py`：三个模型步骤；`prompts/nl2sql.py`：提示词、方言规则、证据渲染（超过 4 万字按九级收缩）
- `retriever.py` 候选召回（关键字加向量，三层范围）；`evidence.py` 证据组装；`validator.py` 静态校验 R1–R9；`output.py` 第 ⑦ 步的工具
- `workflow.py`：LangGraph 编排，`Pipeline.run` 和 `stream` 不向外抛异常；`service.py`：按 `MART_DIR` 装配，界面、接口、评估共用
- `executor.py`：执行层（none 或 duckdb，只读、超时、限量读取）；`dialect.py`：PostgreSQL → DuckDB
- 流水线无状态：追问、反问的上下文由调用方放进 `QueryRequest`（`prev_requirement`、`prev_clarify`）
- 对照实验开关 `use_lineage`、`use_examples`：A 组 False/False（全部层级召回、只给表结构推得出的关联、不上溯），B 组 True/False，C 组 True/True

### 界面、接口与评估

前后端分开：浏览器只访问界面，`/api` 开头的请求由 Vite 开发服务器（开发、start 脚本）或 nginx（Docker）去掉前缀转给接口。

- `web/`（React + TypeScript + Vite + Ant Design）：查数页（七步进度边跑边亮、反问点选、追问、会话列表、SQL 高亮复制、执行结果分页和导出）、元数据页（按层级浏览、搜索表和字段、字段清单、React Flow 血缘图）
  - `src/state/chat.tsx`：会话状态放在路由外层，切到元数据页再回来，正在跑的一轮还在；`src/api/client.ts` 用 fetch 读 SSE（EventSource 只能 GET）
  - `src/utils/lineageLayout.ts`：血缘图排版，按跳数分列、重心法排序，不依赖画图组件
- `main.py`（FastAPI，11 个接口）：查数 `POST /nl2sql/query`、`/nl2sql/stream`（SSE）；会话 `/conversations`、`/conversations/{id}`、`/conversations/{id}/turns`（SSE）；元数据 `tables`、`columns`、`search`、`lineage`、`examples`；`GET /health`
  - SSE：流水线在后台线程跑，事件循环等队列，15 秒没有新事件发一行注释保活；浏览器断开时后台照样跑完、照样存历史
  - 启动时在后台线程预热（`service.warm_up`：装配流水线、加载 BGE）；装配用单独的锁，期间元数据和会话接口照常响应
- `nl2sql/history.py`：会话历史（SQLite，`资料目录/build/history.db`，不入库）。追问、反问的上下文由服务端从会话里算：最近一个有完整需求的轮次的需求，以及它还没回答的反问
- `eval/`：40 题题库（10 题登记了其它正确答案）、结果比对、三组对照评估与错题归因、真实库静态评估。基线（2026-10-17）：执行准确率 A 82.9% / B 80.0% / C 97.1%，报告 `eval/reports/2026-10-17_nl2sql_eval.md`；第二轮改进后（2026-10-19，同一份代码跑两次）：A 85.7%、82.9% / B 100%、91.4% / C 97.1%、97.1%，选表完全一致 C 组 62.9% → 85.7%、80.0%，报告 `eval/reports/2026-10-19_nl2sql_eval.md`、`…_2.md`

### Test Infrastructure

`tests/` 下全部是 pytest，1552 项，不调模型、不加载 BGE、不引用 `data/real_mart`。公共夹具在 `tests/conftest.py`：`tiny_store`、`mock_store`、`mock_graph`、`mock_retriever`、`mock_validator`、`mock_db`。

## Tech Stack

| 层 | 技术 | 注意 |
|----|------|------|
| LLM | DeepSeek Chat（OpenAI 兼容接口，langchain-openai） | ①④ 用 JSON 模式，温度 0 |
| 编排 | LangGraph 1.x | 不带检查点，流水线无状态 |
| SQL | sqlglot 25.x | 方言转换、作用域解析、静态校验 |
| Embedding | BAAI/bge-small-zh-v1.5（sentence-transformers） | 向量存进元数据库，numpy 全量检索 |
| 元数据库 | SQLite（`data/<资料>/build/metadata.db`） | 生成物不入库 |
| 模拟库执行 | DuckDB | 只读、超时、限量读取 |
| 后端 | FastAPI | SSE 流式返回每一步 |
| 前端 | React 19 + TypeScript + Vite 8 + Ant Design 6 | 血缘图 React Flow，SQL 高亮 highlight.js，路由 react-router 7 |
| 会话历史 | SQLite（`data/<资料>/build/history.db`） | 生成物不入库 |
| 部署 | Docker Compose | 两个容器：api（构建时导入模拟库元数据）、web（多阶段构建，nginx 托管） |

## 踩坑与约束

1. `langchain_openai` 的 azure 模块会连带导入 transformers 和 torch，冷启动十几到几十秒。`ChatModelClient` 构造时才导入它，测试里用假模块
2. DeepSeek 的 JSON 模式要求提示词里出现 json 字样和完整的格式示例
3. 固定规则放系统提示词、每次变化的内容放用户消息，DeepSeek 的前缀缓存才能命中
4. sqlglot 美化会把 `DATE '…'` 改写成 `CAST('…' AS DATE)`；`format_sql` 用校验器核对美化没有引入新错误
5. Windows 控制台是 GBK 时打印 emoji 会报错（`run_tests.py` 已处理）；脚本打印中文前设 `PYTHONIOENCODING=utf-8`
6. 机械盘上第一次装配流水线（导入模型库）加加载 BGE 要几分钟：装配不能占着元数据的锁，否则这几分钟里所有接口都卡住；接口启动时在后台预热
7. antd 的 `<App>` 会多包一层 `div.ant-app`，整页 `height: 100%` 要连它一起设，flex 子项还要 `min-height: 0`，否则整页跟着内容滚动
8. SSE 走代理：nginx 要 `proxy_buffering off`、读超时放长；接口回 `X-Accel-Buffering: no`；DuckDB 的 DECIMAL 到前端是字符串，前端按字符串加千分位，不转浮点
9. 内置浏览器的窗口在后台时页面不渲染：React Flow 量不了节点尺寸、不画连线，弹窗关不掉；截图验证要让窗口在前台

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

## Current Status (2026-10-19)

| 模块 | 状态 |
|------|:--:|
| 元数据层、真实资料转换器、模拟专题库 | ✅ |
| 脚本解析与补血缘、关联推导、血缘图、向量召回、样例库、静态校验 | ✅ |
| 三个模型步骤、证据组装、LangGraph 编排 | ✅ |
| 三组对照评估、真实库静态评估 | ✅ |
| 接口（含 SSE 流式查数、会话历史） | ✅ |
| 前端（React + Ant Design，替换 Streamlit） | ✅ |
| 旧模块下线 | ✅ |
| 面试冲刺 | ⏳ |

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
- 项目知识库：`notes/`（40 篇笔记，面试可直接用）
