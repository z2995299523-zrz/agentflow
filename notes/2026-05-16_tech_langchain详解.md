# LangChain 框架详解

> 2026-05-16 | tech | 对话讲解

---

## 一、问题起源：LLM 应用的胶水代码地狱

LangChain 出现之前（2022年），用 LLM 开发应用需要手写大量重复代码：

- 调 OpenAI API → 手写 HTTP 请求
- 文档检索 → 手写 embedding + 余弦相似度
- Prompt 拼接 → 字符串手动拼接
- 多轮对话 → 手写 history 管理 + 截断
- Agent 工具调用 → 手写 dispatch 逻辑

**每个 AI 项目都在重复造这些轮子。**

---

## 二、LangChain 解决了什么

| 轮子 | 每个项目都要写 | LangChain 提供 |
|------|--------------|---------------|
| Prompt 模板 | 字符串拼接 | `ChatPromptTemplate` |
| LLM 调用 | 手调 SDK | `ChatOpenAI` 统一接口 |
| 文档加载 | PDF/DOCX 各写一套 | 200+ 内置 Loader |
| 文本分块 | 自己写切分 | `RecursiveCharacterTextSplitter` |
| 向量存储 | 对接 ChromaDB | 统一 `VectorStore` 接口 |
| 链式调用 | 函数嵌套地狱 | LCEL `|` 管道操作符 |
| Agent/工具 | 手写 dispatch | `@tool` + `create_sql_agent` |

---

## 三、ETL 类比

| ETL 概念 | LangChain 概念 |
|---------|---------------|
| DataStage Job | Chain（处理流程） |
| Stage（Transformer） | Runnable（管道节点） |
| Job Sequence | `a | b | c` LCEL 管道 |
| Parameter Set | Prompt Template `{context}` |
| 源表连接器 | Document Loader |
| 数据清洗规则 | Text Splitter |
| 目标表写入 | VectorStore |

**核心类比：** DataStage 解决"不必每次都手写 SQL 编排 ETL"，LangChain 解决"不必每次都手写 SDK 编排 LLM 应用"。

---

## 四、选型对比

| 框架 | 优点 | 缺点 | 选用 |
|------|------|------|------|
| **LangChain** | 生态最大、岗位要求 80% | 抽象层多、版本变化快 | ✅ |
| LlamaIndex | 检索更强 | 生态小 | ❌ |
| OpenAI SDK | 简单直接 | 无链式调用、Agent | ❌ |
| DSPy | 自动优化 Prompt | 太新 | ❌ |

---

## 五、AgentFlow 中的使用

| 模块 | 用的 LangChain 能力 |
|------|-------------------|
| 模型调用 | `ChatOpenAI` 统一接口（DeepSeek/OpenAI 切换只改配置） |
| RAG 文档加载 | `PyPDFLoader` + `TextLoader` + `Docx2txtLoader` |
| 中文分块 | `RecursiveCharacterTextSplitter`（中文分隔符优先） |
| 向量存储 | `Chroma` VectorStore（本地持久化） |
| 问答链 | LCEL：`retriever | format | prompt | llm | parser` |
| Function Calling | `@tool` 装饰器 + `llm.bind_tools()` |
| Text-to-SQL | `create_sql_agent` + `SQLDatabaseToolkit` |

---

## 六、常见坑

1. **版本分裂** — 0.x → 1.x 大改，网上 90% 教程是 0.x 的
2. **过度封装** — 简单调用被套 5 层抽象。本项目用 LCEL 避免此问题
3. **DeepSeek 兼容性** — Agent 格式偶发不兼容，通过 suffix 修复
4. **不是银弹** — 简单场景直接用 SDK 更快

---

## 七、面试话术

### 30 秒版
"LangChain 是 LLM 应用的事实标准框架。我选它因为统一了模型接口、提供了链式管道模式、80% 的 Agent 岗位都要求它。"

### 3 分钟版
"LangChain 解决的核心问题是 LLM 应用的胶水代码太多。它把常见模式抽象成可组合的组件，就像 DataStage 把 ETL 抽象成 Stage。我项目用了它 7 个核心能力：ChatOpenAI 统一模型接口、Document Loader、Text Splitter、Chroma 向量存储、LCEL 管道、@tool 封装 Function Calling、create_sql_agent 实现 Text-to-SQL。选型上对比了 LlamaIndex 和直接调 SDK——LangChain 的管道模式和我 6 年 ETL 经验完全吻合：数据从源表流经 Stage 到目标表，LLM 应用的 prompt 流经 retriever → formatter → LLM → parser。面试中我能把架构讲清楚，因为底层思维是通的。"

---

## 相关笔记

- [[2026-05-16_tech_function-calling详解]]
- [[2026-05-16_tech_prompt-engineering详解]]
- [[2026-05-16_tech_fastapi详解]]
