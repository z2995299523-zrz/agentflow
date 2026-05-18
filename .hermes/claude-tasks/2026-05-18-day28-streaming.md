# Week 6 Day 28-29: 流式输出 — Claude Code 编码任务

修改 `C:\Users\DELL\agentflow\app.py` 的 `render_chat()` 函数，将 `wf.invoke()` 改为 `wf.stream()` 实现流式进度展示。

## 技术背景

LangGraph 的 `StateGraph.stream(state, config)` 方法返回一个迭代器，每完成一个节点就 yield 一次：
```python
for chunk in wf.stream({"question": prompt}, config):
    # chunk = {"supervisor": {"intent": "sql", ...}}
    # 或 chunk = {"rag": {"rag_result": "...", ...}}
    # 或 chunk = {"sql": {"sql_result": "...", ...}}
    # 或 chunk = {"finalize": {"final_answer": "...", ...}}
```

## 修改范围

只修改 `app.py` 中的 `render_chat()` 函数（L92-169 附近），不改其他函数。

## 实现要求

### 1. 用 st.status() 包裹查询过程

```python
with st.status("🤔 Agent 思考中...", expanded=True) as status:
    # ... streaming logic ...
    status.update(label="✅ 回答完成", state="complete")
```

### 2. 流式处理每个节点

```python
thinking_parts = []
final_state = None

for chunk in wf.stream({"question": prompt}, make_config(thread_id)):
    for node_name, state_update in chunk.items():
        if node_name == "supervisor":
            intent = state_update.get("intent", "unknown")
            st.write(f"🎯 Supervisor 识别意图: **{intent}**")
            thinking_parts.append(f"🎯 意图识别: {intent}")
        
        elif node_name == "rag":
            st.write("📚 RAG Agent 正在检索知识库...")
            thinking_parts.append("📚 路由到 RAG 知识库")
        
        elif node_name == "sql":
            st.write("🗄️ SQL Agent 正在查询数据库...")
            thinking_parts.append("🗄️ 路由到 SQL 数据库")
        
        elif node_name == "finalize":
            st.write("📝 正在整理回答...")
        
        elif node_name == "fallback":
            st.write("⚠️ 无法识别意图，返回通用回答...")
            thinking_parts.append("⚠️ Fallback 兜底")
    
    # 保存最后一个非空 state_update 作为 final_state
    if state_update:
        final_state = state_update
```

### 3. 从 final_state 提取结果

```python
# last_state 是最后一个 node 产出的 state_update（不一定是完整的 state）
# 需要特殊处理：final_answer 在 finalize 节点的 state_update 里
if final_state and final_state.get("final_answer"):
    answer = final_state["final_answer"]
else:
    # fallback：某些情况下 final_answer 可能不在最后一个 chunk 里
    answer = "抱歉，未能获取到有效回答。"

thinking = "\n".join(thinking_parts)
```

### 4. 错误处理

```python
try:
    with st.status("🤔 Agent 思考中...", expanded=True) as status:
        # ... streaming ...
        status.update(label="✅ 回答完成", state="complete")
except Exception as e:
    st.error(f"处理请求时出错：{e}")
    answer = f"❌ 处理请求时出错：{e}"
    thinking = ""
```

### 5. 同时更新 thinking 展示

已有的 expander 展示 thinking 的逻辑保持不变，只是 thinking 内容现在更丰富了（每个节点的执行状态都记录了）。

## 注意事项

1. `wf.stream()` 返回的 chunk 中，node_name 是节点函数名（如 `"rag_node"` 还是 `"rag"`）取决于 StateGraph 中 `add_node("rag", rag_node)` 的第一个参数。项目的 graph/workflow.py 中节点名可能是 `"supervisor"`, `"rag"`, `"sql"`, `"finalize"`, `"fallback"` — 需要检查确认。
2. `state_update` 只包含该节点**新增/修改**的字段，不含完整 state。
3. `final_answer` 只在 `finalize` 节点的输出中。
4. `st.status()` 的 `expanded=True` 让用户看到每个步骤，`state="complete"` 收折。

## 交付标准

- 用户发消息后，不再看到一个 spinner 等 5 秒
- 而是看到逐步展开的进度：🎯 识别意图 → 🗄️ 查询数据库 → 📝 整理回答 → ✅ 完成
- 最终回答和以前一样展示在聊天气泡中
- 思考过程 expander 中包含完整的节点执行记录
