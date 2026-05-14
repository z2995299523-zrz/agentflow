# Hermes ↔ OpenClaw 共享上下文

> 最后更新: 2026-05-14 19:30 | Hermes Agent 写入
> 格式说明：每次对话追加到「历史记录」顶部，不覆盖。项目关键信息保持稳定。

---

## 📌 当前状态

| 模块 | 状态 | 说明 |
|------|------|------|
| RAG | ✅ 完成 | loader + vectorstore(BGE本地) + chain(LCEL) |
| Text-to-SQL | ⏳ 待开发 | sql/agent.py 为空 |
| FastAPI | ⏳ 待开发 | main.py 未创建 |
| WebUI | ⏳ 待开发 | Streamlit 未创建 |

---

## 📜 历史记录

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

