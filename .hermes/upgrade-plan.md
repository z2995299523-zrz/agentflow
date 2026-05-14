# AgentFlow v1.0 → v2.0 升级计划

> 基于 10 个 Boss直聘 Agent 开发工程师岗位分析制定
> 目标：让 AgentFlow 覆盖 90% 岗位核心技术栈，成为面试中可展示的完整作品

---

## 一、目标定义

### 升级后 AgentFlow 应具备的能力（对标岗位需求）

| 能力 | 岗位覆盖率 | 当前状态 | 目标 |
|------|-----------|---------|------|
| RAG 知识库问答 | 80% | 骨架（空文件） | 完整实现 |
| Text-to-SQL 数据分析 | 差异化优势 | 骨架（空文件） | 完整实现 |
| LangGraph 多Agent工作流 | 80% | 无 | 新增 |
| Function Calling 工具调用 | 60% | 无 | 新增 |
| 结构化 Prompt Engineering | 60% | 无 | 新增 |
| 向量数据库（ChromaDB） | 60% | 只有配置 | 完整集成 |
| FastAPI 统一接口 | 40% | 只有配置 | 完整实现 |
| Docker 一键部署 | 40% | 有 Dockerfile | 完善 |
| Streamlit WebUI | 面试展示 | 有配置 | 完整实现 |
| MCP 协议支持 | 20% | 无 | 加分项 |

### 核心原则（防过度设计）

- **一个能用的 RAG + 一个能用的 Text-to-SQL + LangGraph 串联 = 90分项目**
- 不求多，求完整。每个模块的边界清晰，代码有测试，README 有截图
- 重点不是"做出来"，而是"讲得清楚"（面试中能解释架构决策）

---

## 二、时间线总览（8周）

```
Week 1-2: RAG 模块实现
Week 3-4: Text-to-SQL + Function Calling
Week 5:   LangGraph 多Agent 工作流串联
Week 6:   FastAPI + Streamlit WebUI
Week 7:   Docker 部署 + 测试 + 文档
Week 8:   面试准备（README截图、Demo录制、简历更新）
```

---

## 三、Week 1-2：RAG 知识库问答（最核心，80%岗位要求）

### Week 1：文档加载 + 向量存储

**Day 1-2：loader.py — 文档加载器**
- 支持 PDF（pypdf）、Word（python-docx）、TXT、Markdown
- 实现文档分块策略（chunk_size=1000, chunk_overlap=200）
- 重点：处理中文文档的编码和分段问题

**Day 3-4：vectorstore.py — 向量化存储**
- ChromaDB 集成：文档 embedding → 存储
- 实现相似度检索（top_k=5）
- 支持持久化存储（重启不丢失）
- 注意：embedding 模型用国内可用的（如 text-embedding-v3 或 通义千问 embedding）

**Day 5：chain.py — 问答链**
- LangChain RetrievalQA 链
- 带来源引用的回答（让用户知道答案来自哪个文档的哪一页）
- 处理"我不知道"的情况（找不到相关内容时诚实回答）

### Week 2：RAG 优化 + API 接口

**Day 6-7：RAG 质量优化**
- 多轮对话记忆（ConversationBufferMemory）
- 检索后重排序（rerank）
- 混合检索（关键词 + 语义）

**Day 8-9：FastAPI 接口**
- POST /rag/upload — 上传文档
- POST /rag/query — 提问
- GET /rag/documents — 列出已上传文档列表
- DELETE /rag/documents/{id} — 删除文档

**Day 10：测试 + README 截图**
- 准备 3-5 个中文文档做测试
- 截图保存到 docs/screenshots/

**里程碑：** 上传一个 PDF，用自然语言提问，得到带引用的回答。可 Demo。

---

## 四、Week 3-4：Text-to-SQL + Function Calling

### Week 3：SQL Agent 实现

**Day 11-12：agent.py — Text-to-SQL Agent**
- LangChain SQLDatabaseChain / SQL Agent
- 自然语言 → SQL 查询 → 执行 → 结果解释
- 支持 SQLite（开发用）和 PostgreSQL（展示企业级能力）
- 安全：只允许 SELECT，禁止 DROP/DELETE/UPDATE

**Day 13-14：visualizer.py — 结果可视化**
- pandas + matplotlib 自动图表生成
- 根据查询结果类型自动选择图表（柱状图/折线图/饼图）
- 图表以 Base64 嵌入 API 响应，前端直接展示

**Day 15：Function Calling 工具封装**
- 将 SQL 查询封装为 Function Calling 工具
- LLM 自主决定何时调用 SQL 工具
- JSON Schema 定义工具参数

### Week 4：结构化 Prompt + 集成

**Day 16-17：Prompt Engineering 体系**
- 创建 prompts/ 目录，系统化管理所有提示词
- System Prompt 模板（角色设定、能力边界、输出格式）
- Few-shot 示例（3-5个标准问答对）
- Chain-of-Thought 思维链引导（复杂查询时强制分步推理）

**Day 18-19：FastAPI 接口**
- POST /sql/query — 自然语言查询数据库
- GET /sql/schema — 获取数据库表结构（让用户知道有什么表）
- POST /sql/visualize — 查询并生成图表

**Day 20：集成测试**
- RAG + SQL 两个模块互相独立但通过统一 API 访问
- 端到端测试：上传文档→提问→查数据库→生成图表

**里程碑：** 问"今年销售额最高的10个产品是哪些"，Agent 自动查数据库、生成柱状图。

---

## 五、Week 5：LangGraph 多Agent 工作流（进阶关键，50%岗位要求）

**Day 21-22：LangGraph 入门 + 单 Agent 改造**
- 将 RAG Agent 改造为 LangGraph 节点
- 将 SQL Agent 改造为 LangGraph 节点
- 创建 Supervisor Agent（路由器，判断用户意图）

**Day 23-24：多Agent 协作工作流**
```
用户提问 → Supervisor Agent（意图识别）
         ├── 文档类问题 → RAG Agent → 带引用回答
         ├── 数据类问题 → SQL Agent → 查询+图表
         └── 综合分析 → RAG Agent + SQL Agent 串联
                      → 先从文档找上下文 → 再查数据库验证
```

**Day 25：状态管理 + 记忆**
- LangGraph State 管理多轮对话上下文
- Agent 间信息传递（RAG 查到的信息传递给 SQL Agent 做条件过滤）
- 对话历史持久化

**里程碑：** Agent 能自主判断问题类型，路由到对应的子Agent，多Agent 协作完成复杂任务。

---

## 六、Week 6：WebUI + 完整产品体验

**Day 26-27：Streamlit WebUI**
- 聊天界面（类似 ChatGPT 的对话式交互）
- 文档上传区域（拖拽上传）
- 数据库查询结果展示（表格 + 图表）
- Agent 思考过程可视化（展示 Supervisor → RAG/SQL 的路由过程）

**Day 28-29：产品打磨**
- 错误处理（API 超时、模型返回异常、文件格式不支持）
- Loading 状态（流式输出，打字机效果）
- 移动端适配

**Day 30：体验测试**
- 找 2-3 个真实场景从头到尾走一遍
- 修复所有肉眼可见的 bug
- 截图保存所有功能界面

**里程碑：** 打开浏览器 → 上传文档/连接数据库 → 自然语言对话 → 得到回答/图表。完整的 SaaS 产品体验。

---

## 七、Week 7：部署 + 测试 + 文档

**Day 31-32：Docker 完善**
- 修复 docker-compose.yml（ChromaDB 持久化、环境变量注入）
- 一键启动脚本（start.sh / start.bat）
- .env.example 完整配置说明

**Day 33-34：测试**
- RAG 模块单元测试（文档加载、向量检索、问答链）
- SQL 模块单元测试（SQL 生成、执行、图表）
- LangGraph 工作流集成测试
- API 接口测试（FastAPI TestClient）

**Day 35：文档**
- README.md 重写：架构图、功能截图、快速开始、API 文档链接
- 架构图：用 excalidraw 或 mermaid 画系统架构图
- 录屏 Demo（3-5分钟，展示核心功能）

**里程碑：** `docker-compose up -d` → 浏览器打开 → 一切正常工作。README 有截图有架构图。

---

## 八、Week 8：面试冲刺

**Day 36-37：简历更新**
- 项目经验核心位置放 AgentFlow（不叫"个人项目"，叫"开源项目"）
- 用 STAR 法则描述：Situation（AI Agent 市场需求）→ Task（开发企业级智能助手平台）→ Action（独立设计架构，实现 RAG+Text-to-SQL+LangGraph 多Agent）→ Result（覆盖 XX 功能，GitHub XX stars）
- 技术栈关键词密集但自然：LangChain, LangGraph, RAG, ChromaDB, FastAPI, Docker, Function Calling, Prompt Engineering

**Day 38-39：面试话术准备**
- 60秒自我介绍（"6年银行数据开发，现在转型AI应用开发"）
- "为什么转型"的标准回答（数据→AI 是自然延续，不是跳跃）
- 项目 Demo 演示流程：先展示效果 → 再讲架构 → 最后说技术细节
- 学历问题的回答（"大专学历，6年实战经验，这个领域重能力"）

**Day 40：投递启动**
- Boss直聘打招呼模板：直接发 GitHub 链接 + 项目截图
- 目标：每天投 5-10 家，长三角/珠三角优先

**里程碑：** 简历更新完成，项目 README 完善，开始投递。

---

## 九、技术风险与应对

| 风险 | 概率 | 应对 |
|------|------|------|
| LangChain/LangGraph 学习曲线陡 | 高 | Week 1-2 先用最简单的 RetrievalQA，不求深度；LangGraph 到 Week 5 再碰 |
| 英文文档阅读障碍 | 高 | 每个新技术先找中文教程；浏览器装沉浸式翻译插件；关键报错我帮你翻译 |
| API 费用超预算 | 中 | 日常开发用 DeepSeek API（便宜）；OpenAI 只用于 Demo 录制 |
| 某个模块卡住超时 | 中 | 每个模块设定硬截止日，做不完就缩小范围，不延期 |
| 前端 Streamlit 能力有限 | 低 | Streamlit 够用，不要折腾 React/Vue。面试中解释"选了最快出效果的方案"即可 |

---

## 十、监督者校验（Supervisor Pass）

> 按照 career-coaching skill 要求，在做完计划后进行自我校验

### 🟢 高置信度
- 核心技术栈（LangChain+RAG+FastAPI）确实是岗位主流需求 — 10个岗位中8个要求
- 全部岗位接受大专学历 — 已验证
- 长三角/珠三角 Agent 岗位真实存在 — 上海、杭州、宁波、珠海均有
- AgentFlow 方向正确 — RAG + Text-to-SQL 恰好对标 80% 岗位

### 🟡 需验证
- 8周时间是否够？计划覆盖内容较多，建议每周结束时评估进度，必要时砍掉 Week 6-7 的非核心内容
- 国内模型（DeepSeek/通义千问）的 embedding 质量是否够用？需 Week 1 第一天就实测
- LangGraph 中文教程资源较少，可能需要我帮你翻译关键文档

### 🔴 风险点
- **异步编程（asyncio）是隐藏前提**：FastAPI 是异步框架，你以前写的 Python 脚本是同步的。Week 1 必须先花半天理解 async/await，否则后面写接口会一直报错
- **8周每天8-10小时学习强度极高**：建议每周日休息半天，否则第3-4周可能 burnout
- **GitHub 账号活跃度**：你的 GitHub 目前 commit 记录少。从现在开始每天 commit，让 8 周后 GitHub 贡献图是绿的 — 这是 HR 和技术面试官都会看的
- **英语文档问题**：LangChain、FastAPI 的核心文档都是英文。如果你完全无法阅读，开发效率会打 5 折。建议装浏览器翻译插件 + 让我帮你翻译关键报错

### 建议调整
1. **Week 6（WebUI）如果时间不够可以简化** — Streamlit 一行代码就能出聊天界面，先保证功能跑通
2. **Week 7（测试+文档）是最容易被跳过的** — 但恰恰是面试中最加分的。至少要保证 README 有 3 张以上功能截图
3. **不要等 8 周后才开始投简历** — Week 4 就可以更新简历初版，Week 6 开始"试探性投递"练手感

---

## 十一、每日节奏建议

| 时段 | 内容 |
|------|------|
| 08:00-09:00 | 看文档/教程（中文），理解今天要用的技术 |
| 09:00-12:00 | 编码实现（核心工作时间） |
| 12:00-13:30 | 午饭+休息 |
| 13:30-17:30 | 编码实现 + 调试 |
| 17:30-18:00 | Git commit + 写当日进展记录到 bridge.md |
| 19:00-21:00 | 复习/补漏/看岗位/可选 |

每周日休息半天。

---

## 十二、与 OpenClaw 的协作

每次对话结束后，我会更新 `.hermes/bridge.md`，写下：
- 当前进展（哪个模块做到哪了）
- 遇到的问题和解决方案
- 下一步计划

OpenClaw 每次对话前读取该文件，可以：
- 接力开发（接着我未完成的部分继续）
- 审查我写的代码
- 提出改进建议

这样我们两个工具之间就不会重复工作或互相矛盾。
