# AgentFlow — Project Context for Claude Code

> 最后更新: 2026-05-16 | 同步自 Hermes Agent

## Project Overview
AgentFlow 是企业级 AI 智能助手平台（个人作品项目，面试展示用）。

核心能力：知识库问答(RAG) + 自然语言数据分析(Text-to-SQL) + 多Agent协作(LangGraph)

## Current Status (2026-05-16)

| 模块 | 状态 | 文件 |
|------|------|------|
| RAG | ✅ | rag/ (loader+vectorstore+chain), test_rag.py |
| Text-to-SQL | ✅ | sql/agent.py + visualizer.py + tools.py |
| Function Calling | ✅ | sql/tools.py + test_sql_tools.py |
| Prompt 体系 | ✅ | prompts/ (system/rag/sql/examples) |
| FastAPI | ✅ | main.py (7 endpoints) |
| 笔记库 | ✅ | notes/ (16篇,含面试话术+踩坑) |
| LangGraph | ✅ | graph/ (5模块) + test_graph.py (7/8通过) |
| WebUI | ⏳ | Streamlit |
| RAG评估基线 | 🔴 待补 | 10题评估集 + RAGAS |

详细进度见 `.hermes/bridge.md`，升级计划见 `.hermes/upgrade-plan.md`

## Tech Stack
- Python 3.12+
- LangChain 1.x（⚠️ 不要用 0.x 的 chains 模块，用 LCEL）
- LangGraph 1.1.3（已安装，Week 5 用）
- 向量模型: BAAI/bge-small-zh-v1.5 (本地免费, sentence-transformers)
- 向量数据库: ChromaDB (本地持久化 ~/agentflow/chroma_data/)
- LLM: DeepSeek v4-pro (via OpenAI 兼容 API, 从 Hermes .env 读 Key)
- Web: FastAPI + Streamlit

## Project Structure
```
C:\Users\DELL\agentflow\
├── main.py              # FastAPI 入口 (RAG 4端点 + SQL 3端点)
├── config.py            # 全局配置 (LLM/DB/代理/上传)
├── rag/                 # RAG 模块 ✅
│   ├── loader.py        #   文档加载 (PDF/Word/TXT/MD)
│   ├── vectorstore.py   #   向量存储 (ChromaDB + BGE)
│   ├── chain.py         #   LCEL 问答链
│   └── __init__.py
├── sql/                 # Text-to-SQL 模块 ✅
│   ├── agent.py         #   SQL Agent (LangChain create_sql_agent)
│   ├── visualizer.py    #   图表生成 (matplotlib → Base64 PNG)
│   ├── tools.py         #   Function Calling 工具封装
│   └── __init__.py
├── prompts/             # Prompt 集中管理 ✅
│   ├── system.py        #   系统级角色设定
│   ├── rag.py           #   RAG 提示词
│   ├── sql.py           #   SQL 提示词 (含 suffix 格式约束)
│   ├── examples.py      #   Few-shot 示例
│   └── __init__.py      #   统一导出
├── graph/               # LangGraph 多Agent 工作流 ✅
│   ├── __init__.py      #   导出 AgentState, build_workflow
│   ├── state.py         #   共享 State 定义 (AgentState TypedDict)
│   ├── nodes.py         #   RAG + SQL 节点函数
│   ├── supervisor.py    #   路由器 (LLM 零样本意图分类)
│   └── workflow.py      #   StateGraph 组装 + 编译
├── notes/               # 技术笔记库 (面试准备) ✅
│   └── README.md        #   18篇笔记索引
├── data/
│   └── sample.db        # SQLite 示例库 (3表72条)
├── chroma_data/         # ChromaDB 持久化 (gitignore)
├── uploads/             # 文档上传目录
├── test_rag.py          # RAG 测试
├── test_sql.py          # SQL 测试 (18项)
├── test_sql_tools.py    # Function Calling 测试 (9项)
├── test_graph.py        # LangGraph 测试 (8项, 7/8通过)
├── .hermes/
│   ├── bridge.md        # Hermes↔ClaudeCode 协作桥梁
│   ├── upgrade-plan.md  # 8周升级计划
│   └── plans/           # 实施计划存档
└── requirements.txt
```

## Key Config (config.py)

```python
# LLM: 默认 deepseek，支持 openai/dashscope 切换
get_llm_kwargs()  # 返回 {model, api_key, temperature, base_url, [http_client]}

# 代理：按 provider 区分
#   deepseek/dashscope → 国内直连（不走代理）
#   openai             → 走 HTTPS_PROXY 环境变量
PROXY_URL = os.getenv("HTTPS_PROXY", os.getenv("HTTP_PROXY", ""))
_NEEDS_PROXY_PROVIDERS = frozenset({"openai"})

# 数据库: SQLite ./data/sample.db (3表: products/customers/sales)
# 向量库: ChromaDB ./chroma_data/
# 上传: ./uploads/ (Max 50MB)
```

## LangChain 1.x 关键约束（踩坑总结）

1. **禁止 import langchain.chains** — 用 LCEL 手动组合
2. **langchain_chroma 包不存在** — 用 `langchain_community.vectorstores.Chroma`
3. **@tool 装饰器语法**: `@tool(description=...)` 不是 `@tool("...")`
4. **create_sql_agent 必须**: `handle_parsing_errors=True` + `return_intermediate_steps=True`
5. **SafeSQLDatabase.run() 必须透传 **kwargs**（否则 execution_options 报错）
6. **DeepSeek 输出格式不稳定** — 通过 suffix 强制 `Final Answer:` 格式解决
7. **matplotlib.use('Agg') 必须在 import pyplot 之前**
8. **Windows 中文字体**: SimHei + `axes.unicode_minus = False`

完整踩坑记录见 `notes/2026-05-16_pitfall_fastapi-启动踩坑.md`

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

六步教学法详细说明见 agentflow-dev skill。

## 用户背景（张润泽）
- 6年银行对公数据开发（ETL/数据库/调度）
- 大专学历，全职转型 AI 应用开发
- 不能读英文技术文档（需翻译辅助）
- 目标城市：长三角（上海/杭州/宁波）或珠三角
- 所有讲解必须用 ETL/数据库概念做类比
- OpenAI Key 有但无法充值 → 日常开发用 DeepSeek

## 协作方式
- Hermes 负责：需求理解、任务分解、写计划、进度跟进、中文沟通、代码讲解
- Claude Code 负责：编码实现、代码审查、运行测试、修复 bug
- 桥梁文件：`.hermes/bridge.md`（每次会话结束后更新）
- 项目知识库：`notes/`（16篇笔记，面试可直接用）
