AgentFlow — 创建 LangGraph 多Agent 工作流 (5 源文件 + 1 测试)

## 项目上下文
- 工作目录: C:\Users\DELL\agentflow
- Python 3.12+, LangChain 1.x, LangGraph 1.1.3
- LLM: DeepSeek via OpenAI-compatible API
- 现有模块: rag/ (RAG问答), sql/ (Text-to-SQL), prompts/ (提示词), config.py
- 关键: 用 LCEL 不用 langchain.chains, 用 langchain_community.vectorstores.Chroma

## 你的任务
创建 `graph/` 包，含 5 个模块 + 1 个测试文件。按 TDD 流程（先写测试，再实现）。

### 文件 1: graph/__init__.py — 导出 build_workflow, AgentState

### 文件 2: graph/state.py — 共享状态定义
AgentState 用 TypedDict + Annotated:
- messages: Annotated[Sequence[BaseMessage], add_messages] — 对话历史
- question: str — 当前问题
- intent: str — 路由决策 ("rag","sql","mixed","unknown")
- rag_result: str — RAG 回答
- rag_sources: list — 文档来源
- sql_result: str — SQL 查询结果
- sql_query: str — 生成的 SQL
- sql_chart: str — 图表 base64
- final_answer: str — 最终回答

### 文件 3: graph/nodes.py — RAG + SQL 节点
**rag_node(state) -> dict:**
- 读 state["question"]，调 rag.chain.ask()
- 懒加载 vectorstore（函数内 import，不在模块顶层）
- vectorstore 空时返回友好提示，不崩溃
- 返回 {"rag_result": "...", "rag_sources": [...]}

**sql_node(state) -> dict:**
- 读 state["question"]，用 sql.agent.SQLQueryAgent 查询
- 懒加载 agent 单例
- DB 不可用时友好提示
- 返回 {"sql_result": "...", "sql_query": "SELECT ..."}

### 文件 4: graph/supervisor.py — 路由器
**supervisor_node(state) -> dict:**
- LLM 零样本意图分类（temperature=0 确保稳定）
- System prompt: "你是一个智能路由器。分析用户问题返回意图标签。rag=知识问答（查文档）、sql=数据查询（查数据库/统计/排行）、mixed=综合分析（先查文档再查数据库）、unknown=无法判断/闲聊。只返回一个英文单词。"
- 返回 {"intent": "rag"|"sql"|"mixed"|"unknown"}

**route_by_intent(state) -> str:**
- 读 state["intent"]，返回下一步节点名
- "rag" → "rag", "sql" → "sql", "mixed" → "rag", "unknown" → "fallback"

### 文件 5: graph/workflow.py — 图组装
```python
def build_workflow():
    graph = StateGraph(AgentState)
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("rag", rag_node)
    graph.add_node("sql", sql_node)
    graph.add_node("finalize", finalize_node)
    graph.add_node("fallback", fallback_node)
    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges("supervisor", route_by_intent, {
        "rag": "rag", "sql": "sql", "fallback": "fallback",
    })
    graph.add_edge("rag", "finalize")
    graph.add_edge("sql", "finalize")
    graph.add_edge("finalize", END)
    graph.add_edge("fallback", END)
    return graph.compile()
```

**finalize_node**: 拼 rag_result + sql_result → final_answer
**fallback_node**: 返回友好兜底回复

### 文件 6: test_graph.py — 8 项测试
1. test_agent_state_creation — State 可创建
2. test_supervisor_routing_rag — "如何配置数据库连接" → rag
3. test_supervisor_routing_sql — "产品表有多少条记录" → sql
4. test_supervisor_routing_unknown — "你好" → unknown
5. test_rag_node — RAG 节点输出非空
6. test_sql_node — SQL 节点输出非空
7. test_workflow_build — 工作流编译通过
8. test_workflow_invoke — 端到端 invoke 返回 final_answer

## 关键约束
- 相对导入: from graph.state import AgentState
- LLM 统一走 config.get_llm_kwargs()
- 所有函数有中文 docstring + type hints
- 节点内 try/except，失败返回部分状态不崩溃
- 不要在模块顶层编译图（只在 build_workflow() 内编译）
- 重对象（vectorstore, SQL agent）在节点函数内懒加载

## 验证步骤
1. python -c "from graph import build_workflow, AgentState; print('Import OK')"
2. pytest test_graph.py -v --tb=short
3. python -c "from graph.workflow import build_workflow; wf = build_workflow(); r = wf.invoke({'question': '你好'}); print(r.get('final_answer','')[:200])"
