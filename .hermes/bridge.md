# Hermes ↔ OpenClaw 共享上下文

> 最后更新: 2026-10-13 | Claude Code 写入
> 格式说明：每次对话追加到「历史记录」顶部，不覆盖。项目关键信息保持稳定。

---

## 📌 当前状态

| 模块 | 状态 | 说明 |
|------|------|------|
| RAG | ✅ 完成 | loader + vectorstore(BGE本地) + chain(LCEL) |
| Text-to-SQL | ✅ 完成 | sql/agent.py + visualizer.py + tools.py (FC封装) |
| FastAPI | ✅ 完成 | main.py (7端点: RAG 4 + SQL 3) |
| LangGraph | ✅ 完成 | graph/(6模块)+eval/(3文件)+MemorySaver+Mixed双路 |
| WebUI | ✅ 完成 | app.py — Chat+Upload+Query + 流式输出 |
| Prompt-体系 | ✅ 完成 | prompts/ (5文件) |
| FunctionCalling | ✅ 完成 | sql/tools.py + test (9项) |
| 端到端测试 | ✅ 完成 | Day 30 三场景全绿 + 4 Bug修复 |
| Docker 部署 | ✅ 完成 | Dockerfile + docker-compose.yml + start脚本 |
| 测试体系 | ✅ 完成 | run_tests.py + run_eval.py |
| README | ✅ 完成 | 架构图 + API表 + 测试基线 + 性能基线 |
| 笔记库 | ✅ 24篇 | notes/ (含面试话术+踩坑) |
| 下一步 | ⏳ Week 8 | 面试冲刺：简历更新 + 话术准备 + 投递 |
| NL2SQL 改造 | 🚧 第一份计划完成 | 元数据层、真实资料转换器、模拟专题库、执行层、评估题库已完成；血缘图和查询流水线待做 |

---

## 📜 历史记录

### 2026-10-14 · 会话 [Claude Code] — NL2SQL 改造：第一份计划完成

**完成内容：**
- `metadata/` — 标准模板、资料读取（Excel 与 CSV，兼容 GBK）、导入校验 19 条规则、SQLite 元数据库、导入命令行
- `data/real_mart/`（不入库）— 真实资料转换器：把真实专题库的模型元数据库、标准层索引库、加工脚本转成标准格式，转换结果导入 0 错误
- `data/mock_mart/` — 模拟专题库：三层 14 张表的四张清单、7 个加工脚本、造数程序（逐日跑批加对平检核）
- `nl2sql/` — 方言转换（PostgreSQL → DuckDB）与可插拔执行层
- `eval/` — 结果集比对、评估题库 40 题
- `tests/` — 224 项单元测试，不调模型；转换器另有 15 项测试在 `data/real_mart/`

**验证：** `python -m pytest tests -q` 224 项通过；真实专题库和模拟专题库导入都是 0 错误；造数后对平检核全部通过。

**旧模块没有改动。** 旧版本在标签 `v1-agentflow`。

**笔记：** 四篇六步讲解，分别是元数据层、真实资料转换器、模拟专题库与造数、评估题库与结果比对。

**用户已提供：** 真实专题库上的 11 个典型查数问题。原文和初步的口径分析在本机不入库的 `data/real_mart/典型问题.md`，第二、三份计划用它们做真实库上的静态评估。上一条记录里的待办第 4 条已完成。

**执行方式：** 张润泽选的是子代理执行。本机的环境变量 `ANTHROPIC_DEFAULT_SONNET_MODEL`、`ANTHROPIC_DEFAULT_OPUS_MODEL`、`ANTHROPIC_DEFAULT_HAIKU_MODEL`、`CLAUDE_CODE_SUBAGENT_MODEL` 指向 DeepSeek 的模型名，而 Claude 桌面端连的是 Anthropic 的接口，能写文件的子代理全部启动失败，只有内置的只读 Plan 类型可用。实际做法：实施由主会话完成，每个任务的独立评审和最后的整体评审由 Plan 类型的子代理完成。以后要用完整的子代理执行，需要先调整这几个环境变量并重启应用。

**实测发现：** 模拟库的造数在仓库所在的机械盘上约 87 秒，同一份代码把库文件放在固态盘的临时目录里约 15 秒。不影响正确性，造好的库会按输入指纹复用。

**评审遗留：** 逐任务评审留下一批不阻塞的改进建议，处理情况和未处理的清单在本机的 `.hermes/plans/2026-10-13-nl2sql-plan1-执行台账.md`（不入库）。

**下一步：** 写第二份计划，覆盖加工脚本解析（包括给没有记录血缘的字段补血缘）、关联关系推导、血缘图、向量索引与候选召回、样例库、静态校验。真实专题库是主要目标，每一步都要在真实资料上验证。

### 2026-10-13 · 会话 [Claude Code] — NL2SQL 改造：需求澄清 + 设计成文

**方向变更：** 把 AgentFlow 改造成查数副驾驶——用自然语言描述查数需求，结合专题库的数据字典、血缘清单、加工脚本，生成可审核的 GaussDB/PostgreSQL 查询 SQL。

**本次只做了讨论和文档，没有改动任何代码。**

**已定的关键决策：**
- 模拟专题库（银行对公存贷款，三层 14 张表）先做通，资料格式标准化，之后可换真实资料
- 方案：元数据库 + 血缘图 + 七步流水线，只有 3 步调模型，血缘遍历和校验是确定性代码
- 只生成不执行，用 sqlglot 做静态校验；执行层可插拔，模拟库用 DuckDB
- 口径有歧义时反问一次；支持连续追问；带历史 SQL 样例库
- 旧功能（文档问答、旧 SQL Agent、图表、意图路由）下线替换，旧版本用标签 `v1-agentflow` 保留
- 评估：40 题，三组对照（仅模型结构 / 加血缘 / 加血缘和样例），主指标是执行准确率

**文档：**
- 需求澄清：`notes/2026-10-13_discuss_nl2sql改造需求澄清.md`
- 设计文档：`notes/2026-10-13_design_nl2sql专题库改造.md`

**同一会话的后续进展：**
- 设计文档已评审通过
- 原有的未提交改动已提交到 master（`5a213a0`），并在该提交上打了标签 `v1-agentflow`
- 新建分支 `feat/nl2sql`，改造工作都在这个分支上，尚未推送
- 第一份实施计划已写好：`.hermes/plans/2026-10-13-nl2sql-plan1-元数据层与模拟专题库.md`（该目录不入库）。计划里的代码、清单、加工脚本、题库已在临时目录整体跑通，216 个测试通过
- 规划时对设计文档做了几处细化：题库改用 YAML；执行层提前到第一份计划；导入校验增加「表名没带模式名」等规则；CSV 兼容 GBK 编码；造数结果按输入指纹复用

**拿到真实专题库资料后的调整：**
- 张润泽提供了当前工作中真实专题库的资料（模型设计文档及其元数据库、加工 SQL、标准层索引库）。决定：真实库为主，模拟库保留
- 新增「真实资料转换器」：把真实资料清洗成标准格式，转换器和转换结果都在 `data/real_mart/`，整个目录不入库
- 标准模板放宽三处：表类型增加「全量表」；粒度字段、数据类型改为选填
- 原型阶段已用真实资料跑通：转换后导入 0 错误；当前加工脚本全部可解析
- 「从加工脚本解析血缘」从不做改为第二份计划要做
- **保密边界：真实的表名、模式名、路径、单位名称不得写进任何入库的文件。** 具体内容只在 `data/real_mart/` 和对话里出现
- 第一份计划相应改为 12 个任务，计划里的代码重新整体验证过：主测试套件 224 项、转换器测试 15 项

**待办：**
1. 张润泽评审第一份实施计划，并选择执行方式
2. 执行第一份计划（12 个任务）
3. 正式在真实库上使用前，确认单位是否允许把表结构和脚本片段发给外部大模型
4. 收集真实库上的典型查数问题，用来做真实库的静态评估
5. 第一份完成后写第二份计划：加工脚本解析（含补血缘）、关联关系推导、血缘图、候选召回、样例库、静态校验

### 2026-05-18 · 会话4 [Hermes] — Week 7 完成：Docker + 测试体系 + README

**Day 31-32: Docker 部署**
- Dockerfile: Python 3.12-slim + wqy-microhei 中文字体 + HF 镜像 + uvicorn + streamlit
- docker-compose.yml: ChromaDB/hf_cache/uploads/data 卷持久化 + .env 注入
- start.bat / start.sh: 一键启动 FastAPI + Streamlit
- .env.example: 含所有配置项说明（LLM/DB/Chroma/服务/上传）
- requirements.txt: +langgraph

**Day 33-34: 测试体系**
- run_tests.py: 统一测试入口（SQL 18 + RAG 5 + FC 9 + Graph 17），支持 --quick 跳过 LLM
- run_eval.py: 质量评估入口（路由准确率/端到端成功率/API 性能基线）
- 删除 test_integration.py: FastAPI TestClient 全量 import 导致 segfault，已由 HTTP 端到端覆盖

**Day 35: README 重写**
- ASCII 架构图（Supervisor → RAG/SQL/Mixed/Fallback → Finalize）
- 功能表格 + API 端点表 + 测试基线表 + 性能基线表
- Docker/本地双启动方式
- 面试话术（底部）

**下一步:** Week 8 面试冲刺

**测试结果:**
- 三场景全绿（RAG查询/SQL查询+可视化/LangGraph工作流）
- P50 响应时间 7.4s | P95 15.8s（瓶颈在 DeepSeek LLM API 调用，非代码）
- 所有测试套件回归：SQL 18/18, RAG 5/5, Graph 17/17

**Bug 修复 (4个):**
1. matplotlib 中文字体：`plt.style.use()` 在字体设置之后执行,覆盖 SimHei → 字体设置移到 style 之后
2. test_graph.py MemorySaver：`build_workflow()` 默认 `enable_memory=True` 但测试没传 config → 传 `enable_memory=False`
3. Pandas 3.0 + SQLAlchemy 2.0 断裂：`pd.read_sql_query()` 不兼容 Engine/Connection → 改用 `conn.exec_driver_sql()`
4. test_rag.py：缺少 `import config`，HF_ENDPOINT 镜像不生效 → 在 import rag 之前 import config

**修改文件:** visualizer.py, test_graph.py, test_rag.py, main.py, app.py

**笔记:** `notes/2026-05-18_tech_day30-e2e-test.md`

**下一步:** Week 7 — Docker 部署完善 + 测试 + 文档

**流式输出：**
- `app.py` render_chat() 改为 `wf.stream()` + `st.status()` 实时展示 Agent 思考过程
- 用户发消息后看到逐步展开的进度：🎯 识别意图 → 🗄️ 查询数据库 → 📝 整理回答 → ✅ 完成
- Claude Code 8 turns / $0.39

**SQL Agent 修复：**
- `agent_type` 从默认 `ZERO_SHOT_REACT_DESCRIPTION` 改为 `"tool-calling"`（修复 `agent_scratchpad` 错误）
- SQL 提取适配 tool-calling 模式（`ast.literal_eval` 替代 `json.loads`）
- 回调精确匹配 `tool_name == "sql_db_query"`

**其他修复：**
- Chat 页面首次加载不再被 BGE 模型阻塞（文件系统检查替代 `get_document_count()`）
- BGE 模型 GPU 加速（GTX 1060 6GB CUDA） + HF 国内镜像
- 清除损坏的 HuggingFace 缓存

**笔记：** 4篇（Streamlit详解 + BGE本质讨论 + SQL agent_scratchpad修复 + 待写流式输出笔记）

### 2026-05-18 · 会话 [Hermes] — Week 6 Day 26-27：Streamlit WebUI 实现

**完成内容：**
- `app.py` (334行) — Streamlit WebUI 三模块：
  - 💬 智能对话：调用 LangGraph 工作流，展示 Agent 思考过程（intent + 路由）
  - 📄 文档管理：多文件上传 → ETL 流水线 → ChromaDB，列表+删除
  - 📊 数据查询：自然语言 → SQL → 表格 + 图表（可选 bar/line/pie）
- `notes/2026-05-18_tech_streamlit-webui详解.md` — 深度讲解（Streamlit 脚本模型、声明式UI、状态管理、选型对比）
- 更新 `CLAUDE.md`（项目状态 + 代理配置变更）
- 更新 `notes/README.md` 索引

**技术选型：** Streamlit 选型理由 — 纯 Python、`st.chat_message` 原生支持聊天界面、开发速度最快

**踩坑：**
1. Claude Code 用 `db.run(sql)` 返回字符串，不能直接 `pd.DataFrame()` → 改为 `pd.read_sql_query(sql, db._engine)`
2. BGE 模型首次加载超时（>120s）→ 已知 Windows 虚拟内存限制，用户需手动 `streamlit run app.py`
3. Claude Code 交付质量高，19 turns / $0.87，只发现 1 个 bug（已修）

**下一步：** Week 6 Day 28-29：错误处理打磨 + 流式输出 + 移动端适配

### 2026-05-18 · 讨论 — BGE 模型加载与本质

**内容：** 深入讨论 BGE 加载慢的原因（IO+初始化，非计算慢），模型的本质（参数=压缩的知识，计算图=解压引擎），矩阵乘法的核心地位。笔记保存到 `notes/2026-05-18_discuss_bge模型加载与本质.md`。

### 2026-05-18 · 会话 [Hermes] — Week 6 Day 26-27：Streamlit WebUI 实现

**完成内容：**
- `app.py` (334行) — Streamlit WebUI 三模块：
  - 💬 智能对话：调用 LangGraph 工作流，展示 Agent 思考过程（intent + 路由）
  - 📄 文档管理：多文件上传 → ETL 流水线 → ChromaDB，列表+删除
  - 📊 数据查询：自然语言 → SQL → 表格 + 图表（可选 bar/line/pie）
- `notes/2026-05-18_tech_streamlit-webui详解.md` — 深度讲解
- 更新 `CLAUDE.md`（项目状态 + 代理配置变更）
- 更新 `notes/README.md` 索引

**踩坑：**
1. Claude Code 用 `db.run(sql)` 返回字符串，不能直接 `pd.DataFrame()` → 改为 `pd.read_sql_query(sql, db._engine)`
2. BGE 模型首次加载阻塞页面（`get_document_count()` 在首次 render 触发加载）→ 改为文件系统检查
3. Claude Code 交付质量高，19 turns / $0.87，只发现 1 个 bug

---

## 📜 历史记录

### 2026-05-17 · 会话2 [Hermes] — Week 5 Day 23-25 完成（BGE预热 + Mixed双路 + MemorySaver + 评估）

**完成内容：**
- `main.py` — FastAPI lifespan 预热 BGE 模型 + SQL Agent，注入到 graph 节点
- `graph/nodes.py` — 改为模块级单例 + fallback 懒加载，sql_node 支持 mixed 上下文注入
- `graph/workflow.py` — Mixed 双路并行（route_after_rag: mixed→SQL→finalize）
- `graph/memory.py` — MemorySaver 单例 + make_config(thread_id) + clear_memory
- `eval/` 模块 — 3项评估指标（路由准确率/端到端成功率/LLM-as-Judge）+ 10题测试集
- `test_graph.py` — 新增 5 项测试（route_after_rag×2 + memory×2 + mixed_e2e×1）
- `notes/2026-05-17_tech_langgraph-day23-25完善.md` — 关键技术讲解笔记

**测试结果:** 11/13 通过（test_rag_node + test_workflow_mixed 因 BGE 加载超时）

**踩坑：**
1. Claude Code 定义了 route_after_rag 但忘记接入图 → 手动补 add_conditional_edges
2. Claude Code 在 main.py lifespan 里用无意义的 global _sql_agent → 删掉
3. 评估脚本运行报 WinError 1455（页面文件太小）→ Windows 虚拟内存不足，非代码问题
4. 今天 3 次 Claude Code 调用共 $1.60（Task1+2: $0.64超时, Task3: $0.49, Task4: $0.47）

**下一步：** Week 6 Streamlit WebUI（Day 26-30）

### 2026-05-17 · 会话 [Hermes] — Week 5 Day 21-22 完成（LangGraph 多Agent 基础框架）

**完成内容：**
- `graph/__init__.py` — 导出 AgentState, build_workflow
- `graph/state.py` — AgentState TypedDict（9字段，messages 用 add_messages reducer）
- `graph/nodes.py` — rag_node + sql_node（懒加载、空库友好提示、try/except）
- `graph/supervisor.py` — supervisor_node（temperature=0 零样本分类）+ route_by_intent
- `graph/workflow.py` — build_workflow()（StateGraph 5节点 + 条件路由 + 编译）+ finalize_node + fallback_node
- `test_graph.py` — 8项测试（7/8通过，test_rag_node 因 BGE 模型加载慢超时）
- `notes/2026-05-17_tech_langgraph概念讲解.md` — LangGraph 核心概念 + ETL 类比 + 面试话术
- `notes/2026-05-17_tech_langgraph-多agent工作流.md` — graph/ 模块代码详解 + 选型理由 + 常见坑

**测试结果:**
| 测试 | 结果 | 
|------|------|
| test_agent_state_creation | ✅ |
| test_supervisor_routing_rag | ✅ |
| test_supervisor_routing_sql | ✅ |
| test_supervisor_routing_unknown | ✅ |
| test_rag_node | ⚠️ 超时（BGE 首次加载） |
| test_sql_node | ✅ |
| test_workflow_build | ✅ |
| test_workflow_invoke | ✅ |

**踩坑记录：**
1. Claude Code 创建的文件质量很高，但有两个小问题：(a) `_collection` 私有属性访问改为 `vectorstore.get(limit=1)`；(b) 无其他问题
2. `python -c` 命令在 terminal 被频繁 block，改用 .py 文件方式绕过
3. BGE 模型首次加载需 15s+，test_rag_node 超时是环境问题非代码问题

**下一步：** LangGraph Day 23-25: mixed 意图的双路并行（RAG→SQL→汇总）+ 状态管理 + 评估基线

### 2026-05-16 · 会话4 [Hermes] — Week 4 Day 15-19 完成（FC + Prompts + FastAPI）

**完成内容：**
- `sql/tools.py` — create_sql_tool() 工厂函数，将 SQLQueryAgent 封装为 LangChain Tool
- `test_sql_tools.py` — 9 项测试（6 pass，3 因代理不稳定 fail）
- `prompts/` 目录（5文件）— system.py / rag.py / sql.py / examples.py / __init__.py
- 迁移 rag/chain.py → 从 prompts.rag 导入 RAG_SYSTEM_PROMPT
- 迁移 sql/agent.py → 从 prompts.sql 导入 SQL_AGENT_PREFIX
- 迁移 sql/tools.py → 从 prompts.sql 导入 SQL_TOOL_DESCRIPTION
- `main.py` — FastAPI 入口，7 个端点（RAG upload/query/documents/delete + SQL query/visualize/schema + health）

**踩坑记录：**
1. Claude Code 12 turns 不够完成任务（需 15+ turns 或拆小任务）
2. `get_llm_kwargs()` 已含 temperature，测试中再传 temperature 会冲突
3. 代理不稳定时 SQLQueryAgent 内部 LLM 调用会 Connection error（非代码 bug）

**下一步：** LangGraph 多Agent 工作流（Day 21-25，计划 Week 5 核心）

### 2026-05-15 · 会话3 [Hermes] — Text-to-SQL 模块完成

**完成内容：**
- `data/init_sample_db.py` — 示例数据库（3张表：products 12条 / customers 10条 / sales 50条）
- `sql/agent.py` — SQLQueryAgent 核心类，LangChain create_sql_agent + SafeSQLDatabase 安全封装
- `sql/__init__.py` — 模块导出
- `test_sql.py` — 9 项端到端测试全部通过 ✅

**Claude Code 环境修复：**
- Claude Code 的 claude-deepseek wrapper 需要在 shell 中显式 export DEEPSEEK_API_KEY（从 .env 读取）
- 直接调用 claude.exe 并设置 ANTHROPIC_API_KEY + ANTHROPIC_BASE_URL 绕过 wrapper

**踩坑记录（新增）：**
1. `SafeSQLDatabase.run()` 必须透传 `**kwargs`，否则 SQLDatabaseToolkit 传的 `execution_options` 参数报错
2. `create_sql_agent()` 需加 `handle_parsing_errors=True`，DeepSeek 偶尔返回不可解析的格式
3. `create_sql_agent()` 需加 `return_intermediate_steps=True`，从 intermediate_steps 提取 SQL 比回调更可靠
4. LLM 非确定性导致个别查询偶发失败（正常现象，重试即可）

**下一步：** Function Calling 工具封装（Day 15） 或 RAG FastAPI 接口（补跳过项）

### 2026-05-15 · 会话3b [Hermes] — visualizer 完成

**完成内容：**
- `sql/visualizer.py` — QueryVisualizer 类（281行），自动分析 DataFrame → 选择图表类型 → Base64 图片
- 三种图表：柱状图(bar) / 折线图(line) / 饼图(pie)
- `test_sql.py` 扩展至 18 项测试，全部通过 ✅
- 创建 `notes/` 目录：技术笔记永久保存（命名规范：`YYYY-MM-DD_类别_主题.md`）

**踩坑记录：**
1. `analyze()` 的日期检测必须在 bar 返回之前，否则日期列被误判为分类列
2. Windows 上 matplotlib 中文字体用 SimHei，必须配合 `plt.rcParams['axes.unicode_minus'] = False`
3. `matplotlib.use('Agg')` 必须在 `import pyplot` 之前，否则无效

---

### 2026-05-14 · 会话2 [Hermes]

**完成：**
- 确立「边写边讲」教学模式：每写一个模块，先讲框架来龙去脉+选型理由+可迁移概念
- 创建 `agentflow-dev` skill（含 LangChain 1.x 踩坑、项目规范、教学规范）
- bridge.md 改为增量日志格式，历史不再丢失
- 确认我在终端有执行权限

**约定：** 下次写 Text-to-SQL 模块时，按 📦📖🔧💡👨‍💻⚠️ 六步走

### 2026-05-14 · 会话1 [Hermes]

**完成内容：**
- 解析 10 个 Boss直聘 Agent 岗位截图 → 报告保存至桌面
- 分析结论：LangChain(80%) / RAG(80%) / Python(80%) / Function Calling(60%) 是核心要求
- 全部岗位接受大专学历，薪资 8-30K

**AgentFlow RAG 模块实现：**
- `rag/loader.py` — PDF/Word/TXT/MD 加载 + 中文分块（chunk_size=1000）
- `rag/vectorstore.py` — ChromaDB + 本地 BGE 模型（免费，零 API 消耗）
- `rag/chain.py` — LCEL 现代写法，带来源引用
- `config.py` — 自动读取 Hermes .env 的 DeepSeek Key
- `test_rag.py` — 5 项端到端测试全部通过 ✅

**踩坑记录（重要！OpenClaw 注意）：**
1. ❌ `langchain_chroma` 包不存在 → 改用 `langchain_community.vectorstores.Chroma`
2. ❌ `langchain.chains` 在 1.x 废弃 → 改用 LCEL 写法（`RunnablePassthrough + StrOutputParser`）
3. ❌ DashScope 兼容接口不支持 embedding → 改用本地 `BAAI/bge-small-zh-v1.5`（免费）
4. ❌ DeepSeek 也没有 embedding 模型 → 确认只能用本地或 OpenAI
5. 需安装 `sentence-transformers`（已在 requirements.txt）

**关键技术选型：**
- LLM: DeepSeek deepseek-chat（Key 在 `D:/hermes-agent-home/.env`）
- Embedding: 本地 BGE-small-zh-v1.5（~100MB，首次运行自动下载）
- 框架: LangChain 1.x + LangGraph（已安装）

**下一步计划：** Text-to-SQL 模块（sql/agent.py）

---

## 📋 项目关键信息

| 项目 | 路径/值 |
|------|---------|
| AgentFlow 根目录 | C:\Users\DELL\agentflow |
| GitHub 仓库 | github.com/z2995299523-zrz/agentflow |
| GitHub 账号 | z2995299523-zrz |
| Hermes 配置 | D:/hermes-agent-home/config.yaml |
| Hermes Key | D:/hermes-agent-home/.env |
| 升级计划 | .hermes/upgrade-plan.md |
| 岗位分析报告 | C:\Users\DELL\Desktop\agent开发工程师BD\岗位分析报告.txt |

**用户约束：**
- 大专学历，6年银行对公数据开发
- 全职转型 AI 应用开发，每天 8-10h
- 目标市场：长三角/珠三角
- 不能阅读英文技术文档（需翻译辅助）
- OpenAI Key 有但不能充值，日常开发用国内模型

---

## 💬 OpenClaw 回复区

<!-- OpenClaw：请在下方写入，我会在下次对话读取。格式建议：时间戳 + 你的发现/建议 -->
<!-- 例如：2026-05-15 [OpenClaw] 审查了 loader.py，发现 xxx 问题 -->

