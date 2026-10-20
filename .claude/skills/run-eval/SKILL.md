---
name: run-eval
description: 跑 NL2SQL 评估，并和上一份报告对比。改了提示词、召回、证据、方案、生成、校验这些流水线代码之后，用它拿数字说明效果；用户说「跑评估」「看看准确率」时也用。
argument-hint: "[--configs C] [--ids B01,K02]"
---

# 跑评估

模拟库三组对照评估会调 DeepSeek：40 题，三组全跑约 3 分钟。花的是 DeepSeek 的钱（便宜），不是 Claude 的额度。

1. 先跑 `python run_tests.py`，没全部通过就不评估
2. 在后台跑 `python run_eval.py $ARGUMENTS`。不带参数是三组全跑；只验证几道题用 `--configs C --ids B01,K02`，更省时间；中断了可以用 `--resume 上次的逐题记录目录` 接着跑
3. 新报告写在 `eval/reports/YYYY-MM-DD_nl2sql_eval.md`（同一天第二次带 `_2`），逐题记录在 `eval/reports/runs/`（不入库）。拿新报告和上一份对比这几项：
   - 执行准确率（主指标，A、B、C 三组）
   - 选表准确率，即与标准用表完全一致
   - 反问准确率、追问准确率
   - 变差的题逐题列出来，看报告里的错题归因
4. 用一张短表汇报：指标、上一份、这一份、变化。同一份代码跑两次也会差几个百分点（2026-10-19 两次运行 B 组 100% 和 91.4%），差得比这小，就不要下「变好了」「变差了」的结论；拿不准就再跑一次

## 真实库静态评估

需要本机的 `data/real_mart/`。不执行 SQL，只看给没给出 SQL、用表对不对、校验过没过：

```bash
python run_eval.py --static data/real_mart/eval_questions.yaml --mart data/real_mart --out data/real_mart/eval
```

结果留在 `data/real_mart/eval/`，不入库。汇报可以在对话里直接说，但真实题目、表名不写进任何入库文件。
