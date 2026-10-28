# AgentFlow — 基于专题库模型结构与血缘的查数副驾驶

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.x-orange.svg)](https://langchain-ai.github.io/langgraph/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

用自然语言描述查数需求，工具结合专题库的数据字典、字段血缘、加工脚本和历史样例，生成可审核的 GaussDB / PostgreSQL 查询 SQL，并写清口径、用表依据和关联说明。口径有分岔时先反问一次，支持连续追问。

> 旧版本（文档问答 + 旧 SQL Agent + 意图路由）保留在标签 `v1-agentflow`。

## Demo 预览

```
用户：查 2026 年 9 月末全行对公存款余额
助手：「对公存款余额」要按哪个口径统计？
      [全口径（含保证金）]  [不含保证金]  [仅保证金]       ← 依据：三个字段同源，但加工条件不同
用户：（点「不含保证金」）
助手：完整需求  查询 2026-09-30 全行不含保证金的对公存款余额
      SQL       SELECT SUM(b.dep_bal_rmb) FROM ads.org_corp_dep_bal_d AS b
                WHERE b.data_dt = DATE '2026-09-30'
      口径说明  dep_bal_rmb 由客户汇总表按机构汇总，客户层只汇总 is_margin = '0' 的账户……
      用表依据  只到全行汇总，用汇总程度最高的机构汇总表
      执行结果  返回 1 行（模拟库）
```

## 🏗 架构

```
                 ┌──── 元数据层（导入时一次算好）─────────────────────────────┐
四张清单 + 加工脚本 │ 校验 → 脚本解析补血缘 → 推导关联 → 收样例 → 字段向量 → 元数据库 │
+ 历史样例  ──────► │                     血缘图（字段级）+ 关联图（表级）           │
                 └────────────────────────────────────────────────────────────┘
查询流水线（LangGraph，七步里只有三步调模型）：
  ① 需求解析(模型) → ② 候选召回 → ③ 证据组装 → ④ 选表定口径(模型) → ⑤ 生成 SQL(模型) → ⑥ 静态校验 → ⑦ 输出
                       ▲                         │  缺字段：上溯一层（最多 2 层）   │ 有错误：带着错误重写（最多 2 次）
                       └─────────────────────────┘                     ⑤ ◄─────┘
                                                 │  口径分岔：反问 → ⑦
```

- **确定性的部分全是代码**：召回（关键字 + BGE 向量）、血缘展开、同源字段对比、关联路径、静态校验（sqlglot），同样的输入得到同样的证据
- **模型只做三件事**：把需求拆成要素、按证据选表定口径、写 SQL；①④ 用 JSON 模式输出，代码核对不过就带着问题重试一次
- **只生成不执行**：真实库默认只给 SQL；模拟库可以接 DuckDB 只读执行，用来演示和评估
- **前后端分开**：界面（React，`web/`）只调接口；每一轮的七步进度用 SSE 推给浏览器，会话历史存在 SQLite，流水线本身保持无状态

```
浏览器 ──/api──► Vite 开发服务器或 nginx ──去掉 /api──► FastAPI ──► 流水线（后台线程）
   ▲                                                    │ 每走完一步：event: step
   └──────────────── SSE（text/event-stream）◄──────────┘ 跑完存进会话历史：event: turn
```

## ✨ 核心功能

| 功能 | 说明 |
|------|------|
| 🔎 口径从血缘里来 | 指标字段沿血缘上溯三跳，带上每一跳的加工表达式、过滤条件、关联条件；同源不同口径的字段自动比对 |
| ❓ 反问一次 | 只有「同源不同口径」而需求没说时才问，每个选项写明对应哪个字段、怎么加工 |
| ⬆️ 向上溯源 | 专题层缺维度或粒度不够时上溯到标准层、贴源层，并还原下游加工时用过的条件 |
| 💬 连续追问 | 「只看制造业」「换成 6 月末」合并成新的完整需求 |
| ✅ 静态校验 | 表、字段存在，快照表带数据日期，关联不发散等九条规则，不过就带着错误重写 |
| 🖥 查数页 | 七步进度边跑边亮（SSE）；SQL 高亮、一键复制；口径说明、用表依据、校验结果分栏看；反问选项点一下就是回答；执行结果分页、导出 CSV |
| 🗂 会话历史 | 每个会话的每一轮存进 SQLite，左侧按今天、昨天、更早分组，可以搜索、改名、删除；追问的上下文由服务端从会话里算 |
| 🕸 元数据页 | 按层级浏览、搜索表和字段；字段血缘画成节点连线图（上游在左、下游在右），点连线看这一跳的加工表达式、过滤和关联条件 |
| 🔌 REST API | 查数（一次返回、流式）、会话、元数据、健康检查共 11 个接口，Swagger 自动文档 |

## 🚀 快速开始

```bash
# 1. 安装后端依赖（国内加 -i https://pypi.tuna.tsinghua.edu.cn/simple）；界面还需要 Node.js 20.19 或 22.12 以上
pip install -r requirements.txt

# 2. 配置：复制模板，填入 DEEPSEEK_API_KEY
cp .env.example .env

# 3. 导入模拟专题库的元数据（第一次会下载并加载 BGE 模型；加 --no-index 跳过向量）
python -m metadata.importer

# 4. 启动：Windows 用 start.bat，Linux/Mac 用 bash start.sh（第一次会装前端依赖、构建界面）
#    界面 http://localhost:8080，接口文档 http://localhost:8000/docs
#    想在界面上看执行结果，在 .env 里设 EXECUTOR=duckdb
```

改前端代码时开两个终端：一个 `uvicorn main:app --port 8000`，一个 `cd web && npm install && npm run dev`（http://localhost:5173，热更新，`/api` 转给 8000）。

Docker：`docker-compose up -d` 起两个容器，接口 `api`（镜像构建时导入模拟库的元数据，不算向量）和界面 `web`（多阶段构建：Node 构建，nginx 托管），界面 http://localhost:8080。

### 换成自己的专题库

1. `python -m metadata.template 模板.xlsx` 生成空白模板，按表清单、字段清单、血缘清单、关联关系填好，加工脚本放进 `scripts/`，历史样例放进 `examples/`
2. `.env` 里把 `MART_DIR` 指向资料目录
3. `python -m metadata.importer`，看导入报告里的错误和警告
4. 资料改了再运行同一条命令：每次导成一个新版本（向量按文本复用，资料没变不出新版本），和上一版比出差异报告。删了表或字段、疑似改名、样例失效、数量骤减这类情况会停下来等你看报告，确认后 `python -m metadata.versions promote <版本> --accept` 再切换；导错了用 `rollback` 切回上一个版本，`list` 看全部版本

## 📡 API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/nl2sql/query` | 查数：`{question, prev_requirement, prev_clarify}`，返回状态、SQL、口径说明、校验结果、逐步轨迹 |
| `POST` | `/nl2sql/stream` | 同上，SSE 流式返回：每走完一步一条 `step`，最后一条 `result` |
| `GET` / `POST` | `/conversations` | 最近的会话 / 新建会话 |
| `GET` / `PATCH` / `DELETE` | `/conversations/{id}` | 会话的全部轮次和上下文 / 改标题 / 删除 |
| `POST` | `/conversations/{id}/turns` | 在会话里提问（SSE）：上一轮的需求和反问由服务端从会话里取，跑完存下这一轮 |
| `GET` | `/metadata/tables` | 表清单，可按层级过滤 |
| `GET` | `/metadata/columns` | 一张表的字段清单，带每个字段的直接来源、直接去向数 |
| `GET` | `/metadata/search` | 按关键字找表和字段，完全一样的排前面 |
| `GET` | `/metadata/lineage` | 一个字段的上游链路和下游字段，带链路上每个字段的中文名和层级 |
| `GET` | `/metadata/examples` | 样例库里的需求，界面上给用户参考怎么问 |
| `GET` | `/health` | 资料目录、各类元数据数量、执行层、模型、方言；没导入元数据时提示导入命令 |
| `GET` | `/docs` | Swagger 自动文档 |

## 🧪 测试与评估

```bash
python run_tests.py      # 单元测试（1552 项，不调模型、不加载 BGE）
python run_eval.py       # 模拟库三组对照评估（调用 DeepSeek，43 题，几分钟）
```

### 评估基线（模拟库，2026-10-17，deepseek-chat）

| 指标 | A 仅模型结构 | B 加血缘 | C 加血缘和样例 |
|------|:--:|:--:|:--:|
| 执行准确率（主指标，35 道 SQL 题） | 82.9% | 80.0% | **97.1%** |
| 候选召回率 | 100% | 100% | 100% |
| 字段准确率 | 82.9% | 91.4% | 94.3% |
| 反问准确率（40 题） | 95.0% | 95.0% | 97.5% |
| 追问准确率（5 题） | 100% | 100% | 100% |
| 平均耗时 / 模型调用 | 6.0 秒 / 3.3 次 | 6.1 秒 / 3.5 次 | 6.3 秒 / 3.4 次 |

完整报告（含每道错题的归因）：[eval/reports/2026-10-17_nl2sql_eval.md](eval/reports/2026-10-17_nl2sql_eval.md)。题库和模拟库是为本项目构造的，局限写在报告末尾。

## 🛠 技术栈

| 层 | 技术 | 选型理由 |
|----|------|---------|
| LLM | DeepSeek Flash（`deepseek-flash`，OpenAI 兼容接口，关掉思考模式） | 便宜、国内直连、中文好，JSON 模式稳定 |
| 编排 | LangGraph 1.x | 三条回路（上溯、修正、反问）用条件边表达，逐步给出进度 |
| SQL 解析与校验 | sqlglot | 方言转换、作用域解析（CTE、子查询、临时表）、静态校验 |
| Embedding | BGE-small-zh-v1.5（本地） | 免费、离线；向量存进元数据库，numpy 全量检索 |
| 元数据库 | SQLite | 单文件，导入一次，查询时全部读进内存 |
| 模拟库执行 | DuckDB | 只读、带超时，演示和评估用 |
| 后端 | FastAPI | 自动文档；SSE 推送每一步的进度 |
| 前端 | React 19 + TypeScript + Vite + Ant Design 6 | 国内企业后台最常见的组合；血缘图用 React Flow，SQL 高亮用 highlight.js |
| 会话历史 | SQLite | 每套资料一个库文件，和元数据库放在一起；存储层只暴露七个方法，以后换库只换一个类 |

## 📁 项目结构

```
agentflow/
├── main.py             # FastAPI 接口（11 个，含 SSE 流式查数和会话）
├── web/                # 前端（React + TypeScript + Vite + Ant Design）：查数页、元数据页；Dockerfile、nginx.conf
├── config.py           # 配置：模型、资料目录、方言、执行层
├── run_tests.py        # 单元测试入口
├── run_eval.py         # 评估入口
├── metadata/           # 元数据层：模板、读取、校验、脚本解析、关联推导、向量、样例、导入
├── lineage/            # 血缘图与关联图
├── nl2sql/             # 查询流水线：七步、LangGraph 编排、装配、执行层、会话历史
├── prompts/            # 三个模型步骤的提示词
├── eval/               # 题库、结果比对、评估程序、报告
├── data/mock_mart/     # 模拟专题库（银行对公存贷款，三层 14 张表）：清单、加工脚本、样例、造数程序
├── tests/              # 单元测试
└── notes/              # 设计文档与技术笔记
```

## 📝 开发进度

- [x] v1（标签 `v1-agentflow`）：文档问答、Text-to-SQL、LangGraph 多 Agent、Streamlit、Docker
- [x] 元数据层、真实资料转换器、模拟专题库、方言转换与执行层、评估题库
- [x] 加工脚本解析与补血缘、关联推导、血缘图、向量召回、样例库、静态校验
- [x] 三个模型步骤、证据组装、LangGraph 编排、三组对照评估、新界面与接口、下线旧模块
- [x] 前端换成 React + Ant Design（替换 Streamlit）：SSE 流式进度、会话历史（SQLite）、血缘连线图、前后端分开部署

## 👤 作者

**张润泽** — 6 年银行对公数据开发经验，全职转型 AI 应用开发。

- GitHub: [@z2995299523-zrz](https://github.com/z2995299523-zrz)
- 邮箱: z2995299523@gmail.com

---

📌 **面试中怎么说**：查数最难的不是写 SQL，是定口径。字段的中文名和说明常常不全，真正的口径藏在加工脚本里。这个工具把专题库的血缘和加工脚本解析成字段级的加工链路，选表定口径时让模型看着链路做决定，口径有分岔就带着选项反问。七步里只有三步调模型，其余是确定性代码。在模拟库上做了三组对照：加上血缘和样例后，执行准确率从 82.9% 到 97.1%。
