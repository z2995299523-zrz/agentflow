# AgentFlow 开发笔记

> 张润泽的技术知识库 — 代码讲解、架构决策、面试话术、踩坑记录
> 命名规则：`YYYY-MM-DD_类别_主题.md`
> 类别：tech / discuss / pitfall / design / interview

## 目录

| 日期 | 类别 | 主题 |
|------|------|------|
| 2026-10-20 | tech | [[2026-10-20_tech_nl2sql-列对象信息的明细查询|NL2SQL：「列出某类对象的信息」被判成不是查数需求——① 补上列明细的写法、重试提示不再把需求推成拒绝，新旧对照 0/15 → 15/15]] |
| 2026-10-19 | tech | [[2026-10-19_tech_真实模型服务器部署|真实模型服务器部署：私有快照、访问密码、资料与会话隔离、发布契约和 Claude Code 交接]] |
| 2026-10-19 | tech | [[2026-10-19_tech_AgentFlow服务器部署|AgentFlow 服务器部署：指定 Git 提交、独立版本与数据、CPU BGE、HTTPS、SSE 公网验收、会话备份恢复]] |
| 2026-10-19 | tech | [[2026-10-19_tech_nl2sql-前端改造|NL2SQL 前端改造：React + Ant Design 替换 Streamlit、前后端分开、SSE 推七步进度、会话历史存 SQLite、血缘画成连线图、装配流水线的锁拆开]] |
| 2026-10-19 | tech | [[2026-10-19_tech_nl2sql-生成质量第二轮|NL2SQL 生成质量第二轮：码值配名称字段与 R9、方案去掉只用来关联的维表、反问规则与日均区间、① 也能反问，三轮评估数字对照]] |
| 2026-10-18 | tech | [[2026-10-18_tech_nl2sql-编排界面与评估基线|NL2SQL 编排、界面与评估基线：七步串成一张图、三条回路有上限、两页界面四个接口、三组对照的首轮数字]] |
| 2026-10-17 | tech | [[2026-10-17_tech_nl2sql-评估与执行层加固|NL2SQL 评估与执行层加固：超时中断、同值列合并成组、其它正确答案、八条归因]] |
| 2026-10-17 | tech | [[2026-10-17_tech_nl2sql-三个模型步骤与提示词|NL2SQL 三个模型步骤与提示词：固定规则放前面、模型的输出过三道关、证据超长按价值逐级收缩]] |
| 2026-10-17 | tech | [[2026-10-17_tech_nl2sql-证据组装|NL2SQL 证据组装：把召回的候选表展开成模型能直接读的证据，链路、对比、关联路径全由代码算好]] |
| 2026-10-17 | tech | [[2026-10-17_tech_nl2sql-候选召回|NL2SQL 候选召回：按层上溯定范围、关键字与向量两路、按名次融合、按表归组、上游和样例的表不占名额、通用字段点名才计分]] |
| 2026-10-17 | tech | [[2026-10-17_tech_nl2sql-样例库|NL2SQL 样例库：需求加 SQL 的文本格式、入库前过静态校验、拒收只影响单条、防止与题库撞车]] |
| 2026-10-16 | tech | [[2026-10-16_tech_nl2sql-关联推导与血缘图|NL2SQL 关联推导与血缘图：四种来源与优先级、键域传递、对应关系判定、每对表保留三种映射、确定性血缘查询]] |
| 2026-10-16 | tech | [[2026-10-16_tech_nl2sql-静态校验|NL2SQL 静态校验：R1–R8、语法树取代关键词黑名单、按作用域查字段、快照日期、关联与发散、实际用到的表和字段]] |
| 2026-10-16 | tech | [[2026-10-16_tech_nl2sql-脚本解析与血缘补全|NL2SQL 加工脚本解析与血缘补全：分句与降级、按位置对应目标字段、临时表并入、清单优先脚本补缺、没有上游的字段按原因记账]] |
| 2026-10-15 | tech | [[2026-10-15_tech_nl2sql-向量索引|NL2SQL 向量索引：字段与样例向量化、向量存进元数据库、numpy 全量检索、BGE 延迟加载与哈希向量器]] |
| 2026-10-15 | tech | [[2026-10-15_tech_nl2sql-SQL作用域解析|NL2SQL SQL 作用域解析：别名指向、按查询层补结构与字段清单缺口、字段追溯五类、表达式展开、等值关联键]] |
| 2026-10-14 | tech | [[2026-10-14_tech_nl2sql-评估题库与结果比对|NL2SQL 评估题库：40 题七个类别、执行结果比对规则、埋点区分度测试]] |
| 2026-10-14 | tech | [[2026-10-14_tech_nl2sql-模拟专题库与造数|NL2SQL 模拟专题库：三层 14 张表、加工脚本逐日跑批、对平检核、五类埋点、方言转换与执行层]] |
| 2026-10-14 | tech | [[2026-10-14_tech_nl2sql-真实资料转换器|NL2SQL 真实资料转换器：严格的接口表加宽容的转换器、十一条清洗规则、源表源字段配对、转换报告]] |
| 2026-10-14 | tech | [[2026-10-14_tech_nl2sql-元数据层|NL2SQL 元数据层：标准模板、导入校验 19 条规则、SQLite 元数据库、幂等导入]] |
| 2026-10-13 | design | [[2026-10-13_design_nl2sql专题库改造|NL2SQL 改造设计：元数据标准格式 + 血缘图 + 七步流水线 + 静态校验 + 模拟专题库 + 三组对照评估]] |
| 2026-10-13 | discuss | [[2026-10-13_discuss_nl2sql改造需求澄清|NL2SQL 改造需求澄清：专题库模型结构 + 血缘驱动，6 项结论及设计影响]] |
| 2026-05-18 | tech | [[2026-05-18_tech_day30-e2e-test|Day 30 端到端体验测试：3场景全绿 + 4 Bug修复 + 性能基线]] |
| 2026-05-18 | pitfall | [[2026-05-18_pitfall_sql-agent-agent_scratchpad修复|SQL Agent agent_scratchpad 错误修复：LangChain 1.2.13 React→tool-calling 迁移]] |
| 2026-05-18 | discuss | [[2026-05-18_discuss_bge模型加载与本质|BGE 模型加载慢的原因 + 模型本质：参数/矩阵乘法/计算图]] |
| 2026-05-18 | tech | [[2026-05-18_tech_streamlit-webui详解|Streamlit WebUI 详解：脚本模型、声明式UI、状态管理、三模块实现]] |
| 2026-05-17 | tech | [[2026-05-17_tech_rag评估指标详解|RAG 评估指标详解：Recall/Precision/MRR/AnswerRelevance + RAGAS Faithfulness]] |
| 2026-05-17 | tech | [[2026-05-17_tech_rag评估与集成测试|RAG 评估基线（10题+4指标）+ 集成测试（5端点）]] |
| 2026-05-17 | design | [[2026-05-17_design_mixed双向路由|Mixed 双向路由：RAG↔SQL 对称设计 + Post-condition Guard 防死循环]] |
| 2026-05-17 | tech | [[2026-05-17_tech_langgraph-day23-25完善|LangGraph Day 23-25：BGE预热 + Mixed双路 + MemorySaver + 评估基线]] |
| 2026-05-17 | discuss | [[2026-05-17_discuss_bge冷启动与多agent顺序|BGE 模型冷启动（FastAPI lifespan）+ 多Agent 协作顺序判断（数据依赖）]] |
| 2026-05-17 | tech | [[2026-05-17_tech_langgraph概念讲解|LangGraph 核心概念：StateGraph / Node / Edge / 条件边（ETL DAG 类比）]] |
| 2026-05-17 | tech | [[2026-05-17_tech_langgraph-多agent工作流|LangGraph 多Agent 工作流代码详解（state/nodes/supervisor/workflow）]] |
| 2026-05-16 | tech | [[2026-05-16_tech_langchain详解|LangChain 框架详解：解决什么问题 + 选型理由]] |
| 2026-05-16 | discuss | [[2026-05-16_discuss_agent-in-langchain|Agent 在 LangChain 的位置 + 多Agent 协作]] |
| 2026-05-16 | tech | [[2026-05-16_tech_function-calling详解|Function Calling 工具封装详解]] |
| 2026-05-16 | tech | [[2026-05-16_tech_prompt-engineering详解|Prompt Engineering 分层体系详解]] |
| 2026-05-16 | tech | [[2026-05-16_tech_fastapi详解|FastAPI 异步接口详解]] |
| 2026-05-16 | tech | [[2026-05-16_tech_pydantic详解|Pydantic 数据校验模型详解]] |
| 2026-05-16 | tech | [[2026-05-16_tech_swagger详解|Swagger 自动文档详解]] |
| 2026-05-16 | pitfall | [[2026-05-16_pitfall_fastapi-启动踩坑|FastAPI 启动 + 代理 + DeepSeek 格式 6个踩坑]] |
| 2026-05-16 | discuss | [[2026-05-16_discuss_async-vs-thread|async/await：线程 vs 协程深度讨论]] |
| 2026-05-15 | tech | [[2026-05-15_tech_text-to-sql-agent详解|Text-to-SQL Agent 代码详解]] |
| 2026-05-15 | tech | [[2026-05-15_tech_visualizer详解|QueryVisualizer 图表可视化详解]] |
