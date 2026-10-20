---
paths:
  - "nl2sql/**"
  - "prompts/**"
---

# 查询流水线与提示词

- 产品里调用的模型是 DeepSeek Chat（OpenAI 兼容接口，langchain-openai）；① 解析、④ 方案用 JSON 模式，温度 0
- `nl2sql/schemas.py` 是步骤之间的契约：改字段要连同调用方和测试一起改
- 新的提示词写进 `prompts/`，不在代码里拼字符串

## 踩坑

1. `langchain_openai` 的 azure 模块会连带导入 transformers 和 torch，冷启动十几到几十秒：`ChatModelClient` 构造时才导入它，测试里用假模块
2. DeepSeek 的 JSON 模式要求提示词里出现 json 字样和完整的格式示例
3. 固定规则放系统提示词、每次变化的内容放用户消息，DeepSeek 的前缀缓存才能命中
4. sqlglot 美化会把 `DATE '…'` 改写成 `CAST('…' AS DATE)`；`format_sql` 用校验器核对美化没有引入新错误
