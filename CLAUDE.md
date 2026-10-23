# CLAUDE.md

AgentFlow 是「基于专题库模型结构与血缘的自然语言转 SQL」工具（查数副驾驶）：用自然语言描述查数需求，结合专题库的数据字典、字段血缘、加工脚本和历史样例，生成可审核的 GaussDB / PostgreSQL 查询 SQL。设计文档 `notes/2026-10-13_design_nl2sql专题库改造.md`。旧版本（文档问答、旧 SQL Agent、意图路由）在标签 `v1-agentflow`，代码已删。

## 常用命令

```bash
pip install -r requirements.txt           # 国内加速加 -i https://pypi.tuna.tsinghua.edu.cn/simple
python run_tests.py                       # 全部单元测试，不调模型、不加载 BGE
python -m pytest tests/test_workflow.py -q  # 单个测试文件

python -m metadata.importer               # 导入 MART_DIR 指向的资料：校验、解析脚本、补血缘、推导关联、收样例、算向量
python -m metadata.importer 目录 --no-index   # 不算向量，几秒导完；第一次加载 BGE 在机械盘上要几分钟
python -m metadata.template 模板.xlsx      # 生成空白的元数据 Excel 模板（已存在时不覆盖，要覆盖加 --force）
python -m data.mock_mart.build_mock       # 重新生成模拟专题库（固态盘约 15 秒，机械盘约 1.5 分钟）
python data/real_mart/convert.py          # 把真实资料转成标准格式（需要本机的 data/real_mart/）

python run_eval.py                        # 模拟库三组对照评估（调 DeepSeek，43 题约 3 分钟），流程见 /run-eval
uvicorn main:app --port 8000              # 接口 → http://localhost:8000/docs，启动后在后台预热流水线
cd web && npm install && npm run dev      # 界面开发 → http://localhost:5173，/api 转给 8000
cd web && npm run build                   # 前端类型检查加构建；只查类型用 npm run typecheck
start.bat                                 # Windows 同时起接口和界面（http://localhost:8080）；Linux/Mac 用 bash start.sh；Docker 用 docker-compose up -d
```

## 环境

- 配置从两个 `.env` 加载：项目 `.env` 优先，`D:/hermes-agent-home/.env` 兜底（见 `config.py`）。`DEEPSEEK_API_KEY`、`MART_DIR`、`SQL_DIALECT`、`EXECUTOR`、`LLM_TIMEOUT` 的说明在 `.env.example`
- `HF_ENDPOINT=https://hf-mirror.com` 在 `config.py` 顶部设置，要在其他 import 之前，否则 BGE 模型下载失败
- 依赖版本：fastapi ≥ 0.124 加 starlette ≥ 0.49.1；langgraph 限定 1.x；sqlglot 限定 25.x
- 技术栈：DeepSeek Flash（产品里调用的模型 `deepseek-flash`，思考模式在 `config.get_llm_kwargs` 里关掉）、LangGraph（不带检查点）、sqlglot（方言转换、作用域解析、静态校验）、BGE `bge-small-zh-v1.5`（向量存进元数据库，numpy 全量检索）、DuckDB（模拟库执行）、FastAPI、React + Ant Design。元数据库和会话历史都是 SQLite，放在 `data/<资料>/build/`，不入库。Docker Compose 两个容器：api（构建时导入模拟库元数据）、web（多阶段构建，nginx 托管）

## 架构

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
  ① 缺对象或指标时反问，直接去 ⑦；④ need_upstream：上溯一层回到 ②（最多 2 层），② 还拿 ④ 说缺的东西逐条单独补召回第 0 层的表；need_clarify、cannot_answer 直接去 ⑦
  ④ ok：只说「日均」没说统计区间时 clarify_daily_average 改成反问；prune_plan 去掉用不到的表（孤立表、只用关联键关联专题层的维表，记进 dropped_tables，⑤ 再用就是 R8 错误）再去 ⑤
  ⑥ 有错误：带着错误回到 ⑤（最多修正 2 次）
```

- `schemas.py`：各步骤的输入输出，是模块之间的契约；`llm.py`：模型调用（`ChatModelClient`、测试用的 `ScriptedLLM`、带重试的 `structured_call`）
- `parser.py`、`planner.py`、`generator.py`：三个模型步骤；`prompts/nl2sql.py`：提示词、方言规则、证据渲染（加工链路里重复的跳和条件只写一次，第 5 张以后召回的表只列关键字段，上溯、样例带入的表照旧全列；超过 4 万字按九级收缩）
- `retriever.py` 候选召回（关键字加向量，三层范围）；`evidence.py` 证据组装；`validator.py` 静态校验 R1–R9；`output.py` 第 ⑦ 步的工具
- `workflow.py`：LangGraph 编排，`Pipeline.run` 和 `stream` 不向外抛异常；`service.py`：按 `MART_DIR` 装配，界面、接口、评估共用
- `executor.py`：执行层（none 或 duckdb，只读、超时、限量读取）；`dialect.py`：PostgreSQL → DuckDB
- 流水线无状态：追问、反问的上下文由调用方放进 `QueryRequest`（`prev_requirement`、`prev_clarify`，上一轮校验通过时还有 `prev_sql`、`prev_tables`，规则见 `QueryResult.next_context`）。① 判断本轮是在上一轮上追问（`follows_up`）时，② 把上一轮的表放进候选，④ ⑤ 以上一轮的 SQL 为底增量修改，不多调模型
- 对照实验开关 `use_lineage`、`use_examples`：A 组 False/False（全部层级召回、只给表结构推得出的关联、不上溯），B 组 True/False，C 组 True/True

### 接口、界面与评估

前后端分开：浏览器只访问界面，`/api` 开头的请求由 Vite 开发服务器（开发、start 脚本）或 nginx（Docker）去掉前缀转给接口。

- `main.py`（FastAPI）：查数 `POST /nl2sql/query`、`/nl2sql/stream`（SSE）；会话 `/conversations`、`/conversations/{id}`、`/conversations/{id}/turns`（SSE）；元数据 `tables`、`columns`、`search`、`lineage`、`examples`；`GET /health`
  - SSE：流水线在后台线程跑，事件循环等队列，15 秒没有新事件发一行注释保活；浏览器断开时后台照样跑完、照样存历史
  - 启动时在后台线程预热（`service.warm_up`：装配流水线、加载 BGE）
- `nl2sql/history.py`：会话历史（`资料目录/build/history.db`）。追问、反问的上下文由服务端从会话里算：最近一个有完整需求的轮次的需求，以及它还没回答的反问；那一轮校验通过时还有它的 SQL 和用到的表
- `web/`：查数页（七步进度、反问点选、追问、会话列表、SQL 高亮复制、结果分页导出）、元数据页（按层级浏览、搜索、字段清单、React Flow 血缘图）。前端约定在 `.claude/rules/web.md`
- `eval/`：43 题题库（11 题登记了其它正确答案；「反问之后」3 题测上一轮停在反问上的下一轮，第一轮没反问按答错计）、结果比对、三组对照评估与错题归因、真实库静态评估；报告在 `eval/reports/`

## 约定

- 公开函数写中文 docstring 和类型标注；注释用中文；4 空格缩进，UTF-8
- 模型调用统一走 `config.get_llm_kwargs()`，不硬编码 Key；提示词放 `prompts/`
- 测试用 pytest，放 `tests/`：不调模型、不加载 BGE、不引用 `data/real_mart`；公共夹具在 `tests/conftest.py`（`tiny_store`、`mock_store`、`mock_graph`、`mock_retriever`、`mock_validator`、`mock_db`），`conftest.py` 设了 `METADATA_EMBEDDER=hash`
- 改完代码跑 `python run_tests.py`，动了 `web/` 再跑 `npm run build`，都通过再说做完
- 改了提示词或流水线，用 `/run-eval` 跑评估，拿数字（执行准确率、选表准确率）和上一份报告比，不凭感觉调
- 设计文档写到 `notes/YYYY-MM-DD_design_主题.md` 并登记进 `notes/README.md`；实施计划写到 `.hermes/plans/`（不入库）
- 笔记只在用户要求时写，用 `/write-note`
- 推送、开 PR、合并到 master 要等用户开口；`git push` 会弹窗确认
- `AGENTS.md` 是早先给其它工具写的旧文件，不再维护：开发时不读、不改，也不用和本文件同步

## 保密

- `data/real_mart/` 是用户单位的真实专题库资料（原始资料、转换器、转换结果、评估），不入库
- 真实的表名、模式名、路径、单位名称不写进任何入库文件，代码、测试、笔记、提交说明都算。入库内容只讲方法，例子用虚构表名
- `.claude/hooks/check_real_names.py` 在每次 `git commit`、`git push` 前，自动拿 `data/real_mart/` 里的表名、模式名和 `保密词.txt` 比对（git worktree 里没有这个目录，就用主目录的），命中就拦下。全量自查用 `python .claude/hooks/check_real_names.py --all`

## 踩坑

- 机械盘上第一次装配流水线（导入模型库）加加载 BGE 要几分钟：装配用单独的锁，不能占着元数据的锁，否则这几分钟里所有接口都卡住
- Windows 控制台是 GBK，打印 emoji 会报错（`run_tests.py` 已处理）；脚本打印中文前设 `PYTHONIOENCODING=utf-8`
- 只跟 `nl2sql/`、`prompts/` 有关的坑在 `.claude/rules/nl2sql.md`，前端的在 `.claude/rules/web.md`，改到对应目录时自动加载
