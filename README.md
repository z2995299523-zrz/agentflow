# 数仓查数助手（AgentFlow）

基于数据字典和字段血缘，把业务人员的自然语言需求转成可审核的 GaussDB / PostgreSQL 查询 SQL。界面同时展示统计口径、用表依据、关联条件和校验结果，方便数据开发复核后使用。

在数仓里，「余额」「在研项目数」这样的指标往往有多个口径，差异写在加工脚本的过滤条件里。数仓查数助手把字段定义、上游加工逻辑和历史样例带入生成过程。遇到会影响结果的口径分歧，先给出选项请用户确认。后续可以在同一会话里说「只看制造业」或「换成六月末」，继续调整查询。

## 一次查数怎么进行

下面使用仓库自带的银行对公业务模拟专题库。

1. 用户问「查 2026 年 9 月末全行对公存款余额」。
2. 助手根据字段血缘识别三个同源口径，询问要看「全口径（含保证金）」「不含保证金」还是「仅保证金」。
3. 用户选择「不含保证金」。助手选用机构粒度汇总表，按日期取数，说明余额字段的上游加工只汇总 `is_margin = '0'` 的账户。

```sql
SELECT SUM(b.dep_bal_rmb) AS dep_bal_rmb
FROM ads.org_corp_dep_bal_d AS b
WHERE b.data_dt = DATE '2026-09-30';
```

## 核心能力

| 能力 | 在界面里可以做什么 |
| --- | --- |
| 查数与追问 | 看流式处理进度，确认反问选项，复制 SQL，查看和继续历史会话 |
| 核对指标口径 | 查看选表依据、快照日期、统计粒度和关联条件。专题层缺字段时，可按血缘向上寻找 |
| 浏览元数据 | 按层级浏览表，搜索表或字段，查看字段的上下游血缘及每一跳的加工逻辑 |
| 管理访问范围 | 启用登录后，管理员可分配业务域和配置默认模型。服务端按授权约束可见元数据和 SQL 用表 |
| 查看查询结果 | 启用模拟库的只读执行后，可查看结果表格和图表，或导出 CSV。默认配置仅生成 SQL |

## 实现方式

导入阶段校验表、字段、血缘和关联清单，解析加工 SQL 补充字段血缘，并把表结构、历史样例和可选的 BGE 向量写入 SQLite 元数据库。

```text
需求解析 → 候选召回 → 证据组装 → 选表定口径 → 生成 SQL → 静态校验 → 输出
```

- **三步调用模型**。解析需求、按证据选表和确定口径、生成 SQL。召回、血缘展开、关联推导和 SQL 静态校验由代码完成。
- **按证据处理分支**。候选表缺字段时按血缘上溯，口径有分歧时返回反问，SQL 校验失败时带着错误信息重新生成。
- **前后端分离**。FastAPI 提供查数、元数据和会话接口，通过 SSE 返回进度。React 界面负责交互和血缘展示，会话数据存入 SQLite。

| 模块 | 技术 |
| --- | --- |
| 前端 | React 19、TypeScript、Vite、Ant Design、React Flow |
| 接口与编排 | Python 3.12、FastAPI、LangGraph |
| SQL 解析与校验 | sqlglot |
| 模型和检索 | DeepSeek 或兼容接口、关键词检索、本地 BGE 向量 |
| 存储和执行 | SQLite 元数据与会话存储、DuckDB 或 SQLite 模拟库只读执行 |

## 本地体验

需要 Python 3.12，以及 Node.js 20.19+（20.x）或 22.12+。先安装依赖，再复制 `.env.example` 为 `.env`。使用模板的默认配置时，在 `.env` 中填写 `DEEPSEEK_API_KEY`。

```bash
python -m pip install -r requirements.txt
python -m metadata.importer --no-index
```

`--no-index` 使用关键词召回，便于首次启动。启用向量时可运行 `python -m metadata.importer --reindex`，首次构建向量索引会加载 BGE 模型。

- **Windows** 在项目目录运行 `.\start.bat`。
- **Linux / macOS** 在项目目录运行 `bash start.sh`。
- **Docker** 完成 `.env` 配置后运行 `docker compose up -d`。

启动后访问 [查数界面](http://localhost:8080)。直接启动时可访问 [FastAPI 接口文档](http://localhost:8000/docs)。如果想体验模拟库执行，把 `.env` 的 `EXECUTOR` 设为 `duckdb`。

开发前端时分别运行 `uvicorn main:app --port 8000` 和 `cd web && npm ci && npm run dev`，前端开发服务默认在 `http://localhost:5173`。

### 接入自己的专题库

1. 用 `python -m metadata.template 模板.xlsx` 生成模板，填写表清单、字段清单、血缘清单和关联关系。加工脚本放进资料目录的 `scripts/`，样例放进 `examples/`。
2. 在 `.env` 中让 `MART_DIR` 指向该资料目录，运行 `python -m metadata.importer`，检查导入报告。
3. 更新资料后重新导入。出现删表、删字段、疑似改名或样例失效等需要确认的变更时，审核差异后使用 `python -m metadata.versions promote <版本> --accept` 切换。也可用 `list` 查看版本和 `rollback` 回退。

## 测试与评估

```bash
python run_tests.py
cd web && npm run build
```

单元测试使用模拟数据和替身模型。运行 `python run_eval.py` 可以在模拟库上复跑评估。该命令会调用已配置的模型。

| 模拟库评估 | 执行准确率 | 反问准确率 |
| --- | --- | --- |
| 2026-10-17 · DeepSeek Chat · 仅模型结构 | 29/35（82.9%） | 38/40（95.0%） |
| 2026-10-17 · DeepSeek Chat · 加入血缘 | 28/35（80.0%） | 38/40（95.0%） |
| 2026-10-17 · DeepSeek Chat · 加入血缘和样例 | 34/35（97.1%） | 39/40（97.5%） |
| 2026-11-02 · DeepSeek Flash · 加入血缘和样例 | 36/37（97.3%） | 42/43（97.7%） |

前 3 行是同一批题的对照。11 月 2 日使用另一版题库和模型。报告中保留错误归因和评估范围。[10 月 17 日三组对照](eval/reports/2026-10-17_nl2sql_eval.md) · [11 月 2 日评估报告](eval/reports/2026-11-02_nl2sql_eval.md)

## 仓库入口

| 路径 | 内容 |
| --- | --- |
| `main.py`、`config.py` | FastAPI 接口、模型和运行配置 |
| `web/` | 查数、元数据、模型配置和用户权限界面 |
| `metadata/`、`lineage/` | 资料导入、元数据索引、字段血缘和表关联 |
| `nl2sql/`、`prompts/` | 查数流水线、SQL 校验和模型提示词 |
| `data/mock_mart/`、`eval/` | 银行对公模拟专题库、题库和评估 |
| `tests/`、`notes/`、`deploy/` | 单元测试、技术笔记和发布说明 |

原文档问答版本见 [`v1-agentflow` 标签](../../tree/v1-agentflow)。

**张润泽** · 6 年银行对公数据开发经验，关注数仓元数据、指标口径与 AI 查数。[GitHub 主页](https://github.com/z2995299523-zrz)
