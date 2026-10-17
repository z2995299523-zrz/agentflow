"""
查询流水线三个模型步骤的提示词
- ① 需求解析、④ 选表定口径用 JSON 模式输出；⑤ 生成 SQL 输出一个 sql 代码块
- 固定的规则放在系统提示词里，这次的内容放在用户消息里：前缀不变，DeepSeek 的前缀缓存才能命中。
  所以系统提示词里没有今天的日期这类变量，修正轮追加的内容也放在用户消息的末尾
- render_evidence 把证据写成给模型看的文本，render_tables 只写给定表的结构

RETRY_PROMPT 由 nl2sql.llm.structured_call 使用：输出不合格时带着问题再试一次。
"""
from collections.abc import Sequence
from datetime import date, timedelta

from nl2sql.schemas import (
    Clarify,
    Evidence,
    EvidenceColumn,
    EvidenceExample,
    EvidenceTable,
    FieldComparison,
    FieldLineage,
    JoinHint,
    JoinHop,
    LineageStep,
    ParsedRequest,
    QueryPlan,
    QueryRequest,
    TimeSpec,
)

EVIDENCE_CHAR_LIMIT = 40000                   # 证据文本超过这个字数时按固定顺序收缩
FULL_TABLES = 8                               # 收缩第 2 级：第几张表以后只列关键字段
SHRINK_CONDITION_CHARS = 120                  # 收缩第 3 级：每跳的每条条件最多留多少字
SHRINK_EXAMPLE_SQL_CHARS = 1500               # 收缩第 4 级：每条样例 SQL 最多留多少字
# 真实库的表字段多、加工脚本长，前四级装不下时接着收缩（实测真实库 11 道题，前四级之后最长还有 9.4 万字）
SHRINK_EXPRESSION_CHARS = 200                 # 收缩第 6 级：加工表达式最多留多少字
SHRINK_CONDITION_CHARS_MIN = 60               # 收缩第 6 级：每跳的每条条件再截短到多少字
KEY_TABLES = 3                                # 收缩第 7 级：第几张表以后只列关键字段
LINEAGE_FIELD_CHARS = 1500                    # 收缩第 8 级：每个字段的加工链路最多写多少字（至少写第一跳）
SHRINK_ACTIONS = (                            # 收缩的顺序，先轻后重，每级在「说明」里记一句
    "去掉非命中字段的码值",
    f"第 {FULL_TABLES + 1} 张以后的表只列命中字段和粒度字段",
    f"每跳的条件截到 {SHRINK_CONDITION_CHARS} 字",
    f"样例 SQL 截到 {SHRINK_EXAMPLE_SQL_CHARS} 字",
    "命中、粒度、数据日期、主键以外的字段不写口径说明",
    f"加工表达式截到 {SHRINK_EXPRESSION_CHARS} 字、每跳的条件截到 {SHRINK_CONDITION_CHARS_MIN} 字",
    f"第 {KEY_TABLES + 1} 张以后的表只列命中字段和粒度字段",
    f"每个字段的加工链路最多写 {LINEAGE_FIELD_CHARS} 字",
    "关联路径只列直连，不列经过中间表的",
)

RETRY_PROMPT = """你上一次的输出有下面这些问题：
{problems}

请逐条改正，重新输出完整的 JSON。只输出 JSON，不要写解释，不要用代码块包起来。"""


# ── ① 需求解析 ────────────────────────────────

PARSER_SYSTEM = """你是银行数据仓库专题库的「需求解析」助手。用户用自然语言提出查数需求，你把它解析成结构化的需求，后面的步骤再据此找表、写 SQL。你只做解析：不写 SQL，不猜表名，不编造需求里没有的条件。

只输出 JSON，不要写解释，不要用代码块包起来。完整的 JSON 格式见最后的示例。

# 字段说明

- is_query：这句话是不是查数需求。问候、闲聊、问工具能做什么、要求改数据或建表删表，都不是。不是时，reply 写给用户的回复（说明这个工具能做什么，请用户怎么提问），其余字段可以省略。连指标都说不出来（例如只说「查一下数据」）也按不是处理，reply 里请用户补充要查什么指标。
- requirement：完整需求，一句自包含的话，脱离上下文也能看懂。日期写成具体日期；口径限定词、原文要求列出的列、排序和取前几名都要保留。
- metrics：指标，name 写业务叫法，不带日期、不带取值、不带口径限定词（「不含保证金」「折人民币」放到 caliber_hints）；指标本身的叫法里带限定词时不拆，如「不良贷款余额」「保证金存款余额」「定期存款余额」「年日均存款余额」。entity：计数类指标（客户数、项目数、笔数）填被计数的实体（客户、项目、借据），其余留空。
  - 比率类指标（不良率、占比）先写比率本身，再把分子、分母各写成一个指标。
  - 「较年初的增量」「增幅」这类对比不是新指标：指标仍写原指标，对比用 time.compare 表达。
  - 明细查询（列出账户明细）也要写一个最主要的度量字段作指标，列出的其它字段写进 dimensions。
- dimensions：分组或列出的维度，写字段的叫法，用户说「列出一级分行名称」就写「一级分行名称」。
- filters：过滤条件，field 照原文的叫法写全（「全口径存款余额大于零」→ field 写「全口径存款余额」），condition 写条件，如「= 制造业」「大于 0」「在 2026 年内」。原文点明了取值的才写；「全行」「对公客户」「企业」这类只说明查询范围或对象的泛称不是过滤条件，不要写进去。条件是某个业务日期字段的范围（签约日期、立项日期）时也写在这里，field 写那个日期字段的叫法。
- order：排序与条数，如「按余额降序取前 10」。
- time：时间，换算规则见下面。
- caliber_hints：原文里的口径限定词，照抄原文，如「不含保证金」「折人民币」「全口径」「年日均」。没有就留空，不要自己编。
- sub_questions：一句话里问了几个问题时逐个列出（每个都写成自包含的短句）；只有一个问题时留空。
- assumptions：你按默认处理的地方，如「「最新」按上一自然日 2026-05-19 处理」。没有就留空。

# 时间的换算

相对日期一律按用户消息里的「今天」换算成 YYYY-MM-DD，用户消息里附了一份已经算好的参考日期，直接用。下面的例子假设今天是 2026-05-20。
- kind 取值：none（不限定日期）、point（时点）、range（区间）、daily_avg（日均）。dates：none 为空；point 和 daily_avg 正好 1 个；range 正好 2 个（起、止，含两端）。
- 「4 月末」「4 月底」→ 当年 4 月的最后一天 2026-04-30。没写年份时取不晚于今天的最近一个：「9 月末」→ 2025-09-30。
- 「上月末」→ 2026-04-30；「上季末」→ 2026-03-31；「去年末」→ 2025-12-31；「3 月 15 日」→ 2026-03-15。
- 「2026 年 3 月」当时点用 → 当月最后一天 2026-03-31；当区间用（「3 月份」「3 月内」）→ 2026-03-01 至 2026-03-31。
- 「今年」→ 区间 2026-01-01 至 2026-12-31；「今年以来」「年初至今」→ 区间 2026-01-01 至今天 2026-05-20。
- 「年初」「年初数」当时点用 → 上一年的 12 月 31 日 2025-12-31（余额类指标的年初数就是上年末的数）。
- 「较年初」「比年初」是当期减上年末：compare 写 prev_year_end，compare_dates 写当期所在年份的上一年 12 月 31 日。当期是 2026-04-30 时写 ["2025-12-31"]。
- 「较上月末」「比上月」：compare 写 prev_month_end，compare_dates 写当期日期的上一个月的最后一天。当期是 2026-03-31 时写 ["2026-02-28"]。
- 「较去年同期」等其它对比：compare 写 other，compare_dates 写对比日期，note 里说明。没有对比时 compare 写 none，compare_dates 为空。
- 「日均」：kind 写 daily_avg，dates 写要看的那一天（「9 月末的年日均」→ 2025-09-30）。「年日均」「月日均」是日均字段的叫法，写进指标名。
- 「现在」「目前」「当前」：kind 写 none，note 写「目前，不限定日期」。
- 「最新」：取上一自然日（今天减一天）2026-05-19，kind 写 point，并在 assumptions 里写「「最新」按上一自然日 2026-05-19 处理」。
- 没写任何时间：kind 写 none。
- note 写原文说法和换算，如「4 月末 → 2026-04-30」。

# 追问与对反问的回答

- 用户消息里有「上一轮的完整需求」时，本轮输入可能是对它的追问：在上一轮需求的基础上改，合并成新的完整需求，没提到的部分保持不变。
  - 追加维度：「按行业拆开」「再按机构看」
  - 替换或追加条件：「只看制造业」「换成上海分行」
  - 换日期：「换成 6 月末的」
  - 换口径：「改成含保证金的」
  - 换指标：「再看一下不良贷款余额」（要看另一个指标就换成它；用户说「同时」「再加上」才是追加）
  本轮输入本身就是一个完整的查数需求（有指标有时间）时，忽略上一轮。
- 用户消息里有「上一轮的反问」时，本轮输入是对这个反问的回答。回答可能是选项的文字，也可能是「第一个」「选 2」「含保证金的」这样的说法，对照选项理解。把选中的口径写进 requirement 和 caliber_hints，其余按上一轮的需求处理。

# 原则

- 不编造需求里没有的条件、维度和口径。需求没说的口径不要替用户选，后面的步骤会处理；没说时间就是 none。
- 其它拿不准的地方按最常见的理解处理，写进 assumptions。
- 一句话里连着问几个问题（「并且」「其中」「同时」）时，逐个列进 sub_questions，requirement 里也要都写到。

# 示例

下面的例子里的今天是 2026-05-20，实际的今天以用户消息为准。

用户输入：查 4 月末各一级分行制造业客户不含保证金的对公存款余额，较年初增加了多少，增加最多的排前面，列出一级分行名称和增量
```json
{
  "is_query": true,
  "reply": "",
  "requirement": "查询 2026-04-30 各一级分行制造业客户不含保证金的对公存款余额较年初（2025-12-31）的增量，按增量从高到低排列，列出一级分行名称和增量",
  "metrics": [{"name": "对公存款余额", "entity": ""}],
  "dimensions": ["一级分行名称"],
  "filters": [{"field": "行业", "condition": "= 制造业"}],
  "order": "按增量降序",
  "time": {"kind": "point", "dates": ["2026-04-30"], "compare": "prev_year_end", "compare_dates": ["2025-12-31"], "note": "4 月末 → 2026-04-30；较年初 → 对比上年末 2025-12-31"},
  "caliber_hints": ["不含保证金"],
  "sub_questions": [],
  "assumptions": []
}
```

用户输入：今年新签约的合同数，其中已到期的有多少
```json
{
  "is_query": true,
  "reply": "",
  "requirement": "查询 2026-01-01 至 2026-12-31 新签约的合同数，以及其中已到期的合同数",
  "metrics": [{"name": "新签约合同数", "entity": "合同"}, {"name": "已到期合同数", "entity": "合同"}],
  "dimensions": [],
  "filters": [{"field": "签约日期", "condition": "在 2026 年内"}],
  "order": "",
  "time": {"kind": "range", "dates": ["2026-01-01", "2026-12-31"], "compare": "none", "compare_dates": [], "note": "今年 → 2026-01-01 至 2026-12-31"},
  "caliber_hints": [],
  "sub_questions": ["今年新签约的合同数", "其中已到期的合同数"],
  "assumptions": []
}
```

用户输入：最新的对公存款年日均余额，按开户机构看
```json
{
  "is_query": true,
  "reply": "",
  "requirement": "查询 2026-05-19 各开户机构的对公存款年日均余额",
  "metrics": [{"name": "对公存款年日均余额", "entity": ""}],
  "dimensions": ["开户机构号"],
  "filters": [],
  "order": "",
  "time": {"kind": "daily_avg", "dates": ["2026-05-19"], "compare": "none", "compare_dates": [], "note": "最新 → 上一自然日 2026-05-19；年日均取该日的年初累计日均"},
  "caliber_hints": ["年日均"],
  "sub_questions": [],
  "assumptions": ["「最新」按上一自然日 2026-05-19 处理"]
}
```

上一轮的完整需求：查询 2026-03-31 全行对公贷款余额
用户输入：按行业拆开，只看大型企业
```json
{
  "is_query": true,
  "reply": "",
  "requirement": "查询 2026-03-31 各行业大型企业的对公贷款余额，列出行业名称和余额",
  "metrics": [{"name": "对公贷款余额", "entity": ""}],
  "dimensions": ["行业名称"],
  "filters": [{"field": "企业规模", "condition": "= 大型"}],
  "order": "",
  "time": {"kind": "point", "dates": ["2026-03-31"], "compare": "none", "compare_dates": [], "note": ""},
  "caliber_hints": [],
  "sub_questions": [],
  "assumptions": []
}
```

用户输入：你好，你能做什么？
```json
{"is_query": false, "reply": "我可以把专题库里的查数需求转成 SQL，例如「查 9 月末各分行对公贷款余额」。请告诉我想查哪个指标、什么时间、按什么维度看。"}
```"""


# ── ④ 选表定口径 ──────────────────────────────

PLANNER_SYSTEM = """你是银行数据仓库专题库的「选表定口径」助手。输入是一份已经解析好的查数需求，和一份证据。证据分几节：候选表及其字段，指标字段的加工链路、同源字段对比、表之间的上下游、还可以上溯到的表（这四节只在能用血缘时才有），关联路径，相似样例（有样例时才有），说明。你要决定用哪些表和字段、怎么关联、口径是什么，输出查询方案。你不写 SQL，SQL 由下一步生成。

只输出 JSON，不要写解释，不要用代码块包起来。decision 有四种结论：
- ok：给出查询方案。
- need_clarify：候选字段的口径有分岔而需求没有说明，反问用户。
- need_upstream：当前范围缺维度、缺字段或粒度不够，要上溯到更底层的表。
- cannot_answer：元数据范围内做不到。

# 输出的 JSON 格式

结论是 ok 时的完整示例（需求：2026-04-30 各一级分行不含保证金的对公存款余额，列出一级分行名称和余额。表名是示例，实际只能用证据里的表）：
```json
{
  "decision": "ok",
  "tables": [
    {"table": "ads.demo_org_bal_d", "reason": "需求只到机构粒度，机构汇总表已满足，不必用客户级的表再汇总"},
    {"table": "dim.demo_org", "reason": "取一级分行名称"}
  ],
  "fields": [
    {"table": "ads.demo_org_bal_d", "column": "dep_bal_rmb", "usage": "metric", "reason": "不含保证金的存款余额"},
    {"table": "ads.demo_org_bal_d", "column": "data_dt", "usage": "filter", "reason": "快照表的数据日期"},
    {"table": "ads.demo_org_bal_d", "column": "org_no", "usage": "join", "reason": "关联机构维表"},
    {"table": "dim.demo_org", "column": "lvl1_org_nm", "usage": "dimension", "reason": "按一级分行分组"},
    {"table": "dim.demo_org", "column": "org_no", "usage": "join", "reason": "关联键"}
  ],
  "joins": [
    {"left_table": "ads.demo_org_bal_d", "left_columns": ["org_no"], "right_table": "dim.demo_org", "right_columns": ["org_no"], "join_type": "INNER", "reason": "机构号多对一"}
  ],
  "filters": ["ads.demo_org_bal_d.data_dt = DATE '2026-04-30'"],
  "caliber": "dep_bal_rmb 由客户汇总表按机构汇总而来，加工表达式是 SUM(CASE WHEN is_margin = '0' THEN bal_rmb ELSE 0 END)，不含保证金，已折人民币",
  "table_basis": "需求只到机构粒度，用机构汇总表",
  "join_basis": "按 org_no 多对一关联机构维表（N:1，不发散），汇总结果不会重复计算",
  "lineage_note": "",
  "assumptions": [],
  "examples_used": [],
  "clarify": null,
  "missing": [],
  "reason": ""
}
```

其它三种结论只需要 decision 和对应的字段，其余字段可以省略：
```json
{
  "decision": "need_clarify",
  "clarify": {
    "question": "存款余额要不要包含保证金？",
    "options": [
      {"label": "不含保证金", "meaning": "用 ads.demo_org_bal_d.dep_bal_rmb，加工时只汇总非保证金账户"},
      {"label": "含保证金", "meaning": "用 ads.demo_org_bal_d.dep_bal_tot_rmb，是不含保证金与保证金两部分之和"}
    ]
  }
}
```
```json
{"decision": "need_upstream", "missing": ["币种"], "reason": "专题层的汇总表都没有币种，要上溯到明细层"}
```
```json
{"decision": "cannot_answer", "reason": "元数据里没有客户经理相关的表和字段，无法按客户经理统计"}
```

字段说明：
- tables：用到的表，每张写 reason（为什么用它）。只列真正会出现在 SQL 里的表，不用的表不要列。结论是 ok 时不能为空。
- fields：用到的字段，指标、维度、过滤、关联、排序用到的都要列。table 必须是 tables 里的表，column 必须是证据里这张表的字段。usage 取 metric、dimension、filter、join、order、other。结论是 ok 时不能为空。
- joins：关联。left_columns 与 right_columns 一一对应、个数相同；join_type 默认 INNER，只有要保留左表里关联不上的行时才用 LEFT；两侧的表都要列在 tables 里。
- filters：过滤条件，写成 SQL 的写法，字段写成「模式.表.字段」，日期用 DATE 'YYYY-MM-DD'。
- caliber：口径说明，引用证据里的加工表达式，说清楚指标是怎么算出来的。
- table_basis：用表依据。join_basis：关联说明，用哪个键、对应关系、为什么不会重复计算。lineage_note：溯源说明，上溯时还原了哪些加工条件，没有上溯就留空。
- assumptions：按默认处理的地方。examples_used：参考了哪些样例，写样例编号。
- clarify：反问。question 只问一个问题，options 至少两个选项，label 是选项文字，meaning 写明对应哪个字段、含义是什么。
- missing：需要上溯时缺什么，写成字段的叫法。reason：cannot_answer 的原因，need_upstream 可以补充说明。

# 规则

1. 只用证据里的表和字段，表名、字段名照证据原样写，不要自己编。
2. 口径以证据里的加工链路（表达式、过滤条件、关联条件）为准。字段的中文名和口径说明常常不全，名字相近的字段口径可能不同，要看它是怎么算出来的。
3. 同一个指标有几个口径不同的字段时（「同源字段对比」会标出「同源不同口径」；没有这一节时，凭字段的中文名和口径说明判断）：
   - 需求的口径线索已经说明了口径，就选对应的字段，并在 caliber 里引用它的加工表达式；
   - 没有线索，就用 need_clarify：只问一个问题，每个选项写明对应哪个字段、含义是什么；
   - 本次不能反问时，按最常用的理解选一个，写进 assumptions。
   只有这种口径分岔而需求没说清的情况才反问。其它不确定的地方（日期、排序、列的写法）一律按默认处理，写进 assumptions，不要反问。
4. 选表：优先用满足需求粒度的、汇总程度最高的表，例如只要机构粒度就用机构汇总表，不要用客户级的表再自己汇总。需要维度属性（名称、分行、行业）时，用带关联键的表去关联维表；维度字段已经在事实表里就不用再关联；没有用到维表里的字段（不分组、不过滤、不取名称）就不要关联维表，也不要把它写进 tables。样例只参考写法和口径，不要照搬样例里这次用不到的表和关联。
5. 关联：只用证据「关联路径」里列出的关联，在 joins 里写清用哪个键。对应关系 N:1 表示左表每一行最多对应右表一行，不会重复。1:N、N:N、未知，或者路径标了「会发散」的，左表的行会重复，要在 join_basis 里说明怎么避免重复计算（先汇总再关联、按粒度键去重等）。
6. 时间：
   - 快照表一定要有数据日期条件，写进 filters；全量表、维表不要加。
   - 时间不限定日期而用到快照表时，取该表最新的数据日期：数据日期字段 = (SELECT MAX(数据日期字段) FROM 该表)，并写进 assumptions。
   - 区间用在快照表上要小心：对数据日期取区间会把多天的快照加在一起。要看每天（或每月末）的值，就按数据日期分组；区间指的是业务日期字段（签约日期、发放日期）时，过滤那个字段，数据日期取区间的最后一天。
   - 日均用现成的日均字段（年日均、月日均），只取截止日那一天的值，不要对多天的行再求平均。
   - 较年初是当期减上年末，较上月末是当期减上月末：两个日期的数都要取，filters 里把两个日期都列出来。
7. 计数：按被计数的实体的粒度来。用粒度正好是这个实体的表，COUNT(DISTINCT 粒度键)；汇总表里现成的「客户数」「笔数」字段不能跨行相加（同一个客户可能在多个机构里重复出现），除非汇总的粒度正好就是要的粒度。
8. 缺东西：证据里缺需求要的维度或字段，或者表的粒度不够时，用 need_upstream，missing 写成字段的叫法。上溯最多 2 层，到了第 2 层仍缺，照样写 need_upstream 和 missing，由程序如实告诉用户找不到什么。上溯到下一层查数时，要把下游表加工这个字段时用过的过滤条件和关联条件还原出来：证据里血缘的每一跳都带着，写进 filters 和 joins，查出来的数才能和专题层的数对得上。
9. 加工条件里的 CAST('${data_dt}' AS DATE) 这类写法是跑批日期参数，查数时换成需求里的日期。只在加工中间层（CTE）之间的关联不用照搬。
10. 元数据范围内做不到时用 cannot_answer，在 reason 里写明原因；不要编造不存在的字段。
11. 一句多问（需求有子问题）时，方案要覆盖每个子问题：fields 列齐，filters 和 caliber 里写清各自的条件。
12. 每张表、每个字段都写 reason；再写 caliber、table_basis、join_basis、lineage_note；参考了样例就在 examples_used 里写样例编号，没有样例就留空。"""


# ── ⑤ 生成 SQL ────────────────────────────────

DIALECT_RULES = """1. 只用 PostgreSQL 与 GaussDB 共有的标准语法；只写一条 SELECT 语句，可以带 WITH，不写多条语句。
2. 表名带模式名（如 ads.xxx）；给每张表起别名，所有字段都带别名前缀。
3. 日期字面量写 DATE 'YYYY-MM-DD'；不用 CURRENT_DATE、NOW()，日期一律用需求里换算好的日期。
4. 不写 SELECT *；输出列起简短的英文小写别名（如 loan_bal、org_name）。输出别名只能在 ORDER BY、GROUP BY 里单独使用，WHERE、HAVING 和同一层的 SELECT 里不能引用，要写别名对应的原表达式。
5. 快照表要带数据日期条件，写在它所在那一层查询的 WHERE 或 JOIN 条件里；WITH 里的每一层、子查询里的每一层，用到快照表都要各自带。
6. 对比类（较年初、较上月末）用 CASE WHEN 按日期分别求和再相减，日期条件用 IN 列出所有要用的日期。
7. 比率按百分比保留两位：ROUND(分子 * 100.0 / NULLIF(分母, 0), 2)；先汇总分子、分母再相除，不要对各行的比率求平均。
8. 前 N 名用 ORDER BY … LIMIT N，N 和排序方向照需求写。
9. 一句多问写在一条 SQL 里：条件计数 COUNT(CASE WHEN … THEN 1 END)、GROUPING SETS，或者明细加窗口合计；不拆成多条语句。
10. 码值按字段的码值说明写，字符串用单引号，如 is_margin = '0'。
11. 计数用 COUNT(DISTINCT 粒度键)；一行就是一个实体时可以 COUNT(*)。"""

GENERATOR_SYSTEM = f"""你是银行数据仓库专题库的 SQL 生成助手。输入是完整需求、已经定好的查询方案，和方案里的表的结构。你按方案写出一条 SQL。

要求：
- 只用方案里的表和字段，名字照表结构里的写法，不要自己编。
- 表、字段、关联键、过滤条件、口径都以查询方案为准；方案没写到的细节（别名、排序、输出列名）按需求补全。方案里的字段写成「模式.表.字段」，写 SQL 时换成你起的别名。
- 只输出一条 SQL，放在 ```sql 代码块里，代码块外不写任何解释，SQL 里也不写注释。
- 修正时，用户消息里会给出上一次的 SQL 和校验错误：只改错误指出的地方，其余保持不变，输出修正后的完整 SQL。

写法规则（PostgreSQL / GaussDB）：
{DIALECT_RULES}

示例一，较年初的增量：两个日期的余额一次取出，用 CASE WHEN 分开再相减。
```sql
SELECT o.lvl1_org_nm AS org_name,
       SUM(CASE WHEN b.data_dt = DATE '2026-04-30' THEN b.bal_rmb ELSE 0 END)
     - SUM(CASE WHEN b.data_dt = DATE '2025-12-31' THEN b.bal_rmb ELSE 0 END) AS bal_inc
FROM ads.demo_org_bal_d b
JOIN dim.demo_org o ON b.org_no = o.org_no
WHERE b.data_dt IN (DATE '2026-04-30', DATE '2025-12-31')
GROUP BY o.lvl1_org_nm
ORDER BY bal_inc DESC
```

示例二，一句话问了两个问题：用条件计数写在一条 SQL 里，已到期按到期日期早于需求里换算好的日期。
```sql
SELECT COUNT(DISTINCT c.contract_no) AS signed_cnt,
       COUNT(DISTINCT CASE WHEN c.expire_dt < DATE '2026-05-19' THEN c.contract_no END) AS expired_cnt
FROM ads.demo_contract c
WHERE c.sign_dt BETWEEN DATE '2026-01-01' AND DATE '2026-12-31'
```"""


# ── 消息 ──────────────────────────────────────

def _reference_dates(today: date) -> str:
    """按今天算好的参考日期：上一自然日、上月末、上季末、去年末、本月初、本年初，让模型不必自己数月份天数"""
    month_start = today.replace(day=1)
    quarter_start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    items = [
        ("上一自然日", today - timedelta(days=1)),
        ("上月末", month_start - timedelta(days=1)),
        ("上季末", quarter_start - timedelta(days=1)),
        ("去年末", date(today.year - 1, 12, 31)),
        ("本月初", month_start),
        ("本年初", date(today.year, 1, 1)),
    ]
    return "；".join(f"{name} {day.isoformat()}" for name, day in items)


def _clarify_lines(clarify: Clarify) -> list[str]:
    """上一轮的反问：问题加每个选项"""
    lines = [f"上一轮的反问：{clarify.question}"]
    for number, option in enumerate(clarify.options, 1):
        lines.append(f"选项 {number}：{option.label}" + (f"（{option.meaning}）" if option.meaning else ""))
    return lines


def parser_messages(request: QueryRequest, today: date) -> list[dict[str, str]]:
    """① 需求解析的消息：系统提示词是固定规则，用户消息带今天、上一轮需求、上一轮反问和本轮输入

    Args:
        request: 本轮请求
        today: 换算相对日期用的「今天」

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    lines = [f"今天：{today.isoformat()}", f"日期换算参考（已按今天算好，直接用）：{_reference_dates(today)}", ""]
    previous = request.prev_requirement.strip()
    if previous:
        lines.append(f"上一轮的完整需求：{previous}")
    if request.prev_clarify is not None:
        lines.extend(_clarify_lines(request.prev_clarify))
    if previous or request.prev_clarify is not None:
        lines.append("")
    lines.append(f"本轮输入：{request.question.strip()}")
    if request.prev_clarify is not None:
        lines.append("说明：本轮输入是对这个反问的回答。")
    elif previous:
        lines.append("说明：本轮输入可能是对上一轮需求的追问，也可能是一个新的完整需求。")
    lines += ["", "请把本轮输入解析成 JSON。"]
    return [{"role": "system", "content": PARSER_SYSTEM}, {"role": "user", "content": "\n".join(lines)}]


def _time_text(time: TimeSpec) -> str:
    """时间要素写成一句话，对比和原文换算接在后面"""
    dates = time.dates
    if time.kind == "point":
        text = f"时点 {'、'.join(dates)}"
    elif time.kind == "range":
        text = f"区间 {' 至 '.join(dates)}（含两端）"
    elif time.kind == "daily_avg":
        text = f"日均，截止 {'、'.join(dates)}"
    else:
        text = "不限定日期"
    parts = [text]
    compare = "、".join(time.compare_dates)
    if time.compare == "prev_year_end":
        parts.append(f"较年初：当期减上年末 {compare}")
    elif time.compare == "prev_month_end":
        parts.append(f"较上月末：当期减上月末 {compare}")
    elif time.compare == "other":
        parts.append(f"对比日期 {compare}")
    if time.note:
        parts.append(f"原文换算：{time.note}")
    return "；".join(parts)


def _render_parsed(parsed: ParsedRequest) -> str:
    """需求解析的结果写成给后两步看的文本，没有的要素写「无」"""
    metrics = "；".join(metric.name + (f"（被计数的实体：{metric.entity}）" if metric.entity else "")
                        for metric in parsed.metrics)
    filters = "；".join(f"{item.field} {item.condition}".strip() for item in parsed.filters)
    lines = [
        f"完整需求：{parsed.requirement}",
        f"指标：{metrics or '无'}",
        f"维度：{'、'.join(parsed.dimensions) or '无'}",
        f"过滤：{filters or '无'}",
        f"排序与条数：{parsed.order or '无'}",
        f"时间：{_time_text(parsed.time)}",
        f"口径线索：{'、'.join(parsed.caliber_hints) or '无'}",
    ]
    if parsed.sub_questions:
        lines.append("子问题（方案和 SQL 要覆盖每一个）：")
        lines += [f"{number}. {question}" for number, question in enumerate(parsed.sub_questions, 1)]
    if parsed.assumptions:
        lines.append(f"已有假设：{'；'.join(parsed.assumptions)}")
    return "\n".join(lines)


_SCOPE_TEXTS = {
    0: "第 0 层（专题层的全部表，加整合层的维表）",
    1: "第 1 层（再加上候选表在整合层的上游表）",
    2: "第 2 层（再加上贴源层的上游表）",
}


def _planner_limits(evidence: Evidence, allow_clarify: bool, allow_upstream: bool) -> str:
    """④ 本次的限制：能不能反问、能不能上溯、候选范围，以及证据里有没有血缘和样例"""
    if allow_clarify:
        clarify = "- 能不能反问：能（只有口径分岔而需求没说清时才反问）"
    else:
        clarify = ("- 能不能反问：不能。本轮是对反问的回答，不能再反问；遇到口径分岔时按最常用的理解选一个，"
                   "把这个选择写进 assumptions")
    if allow_upstream:
        upstream = "- 能不能上溯：能（缺维度、缺字段或粒度不够时用 need_upstream）"
    else:
        upstream = ("- 能不能上溯：不能。本次不能要求上溯，只能用候选里的表；缺东西时按最可能的理解处理并写进 assumptions，"
                    "确实做不到就用 cannot_answer")
    scope = "全部层级的表一次给全，没有上溯" if evidence.all_layers else _SCOPE_TEXTS.get(
        evidence.scope_level, f"第 {evidence.scope_level} 层")
    if evidence.use_lineage:
        lineage = "- 血缘：有（证据里有指标字段的加工链路、同源字段对比、表之间的上下游和可以上溯的表）"
    else:
        lineage = "- 血缘：没有（证据里没有加工链路、同源字段对比、表之间的上下游和可上溯的表，口径只能凭字段的中文名和口径说明判断）"
    if evidence.examples:
        ids = "、".join(example.example_id for example in evidence.examples)
        examples = f"- 样例：有 {len(evidence.examples)} 条（examples_used 只能写这些样例的编号：{ids}）"
    else:
        examples = "- 样例：没有（examples_used 留空）"
    return "\n".join([clarify, upstream, f"- 候选范围：{scope}", lineage, examples])


def planner_messages(parsed: ParsedRequest, evidence: Evidence, *, allow_clarify: bool,
                     allow_upstream: bool) -> list[dict[str, str]]:
    """④ 选表定口径的消息：用户消息带需求解析结果、证据文本，以及能不能反问、能不能上溯的说明

    Args:
        parsed: ① 的结果
        evidence: ③ 的结果
        allow_clarify: 本轮能不能反问（本轮已经是对反问的回答时不能）
        allow_upstream: 能不能要求上溯（A 组不能）

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    user = "\n".join([
        "# 需求（第 ① 步的解析结果）", _render_parsed(parsed), "",
        "# 本次的限制", _planner_limits(evidence, allow_clarify, allow_upstream), "",
        "# 证据", render_evidence(evidence), "",
        "请按系统提示词里的格式输出查询方案，只输出 JSON。",
    ])
    return [{"role": "system", "content": PLANNER_SYSTEM}, {"role": "user", "content": user}]


_USAGE_NAMES = {"metric": "指标", "dimension": "维度", "filter": "过滤", "join": "关联键", "order": "排序",
                "other": "其它"}


def _render_plan(plan: QueryPlan) -> str:
    """查询方案写成给 ⑤ 看的文本，只写有内容的项"""
    lines: list[str] = []
    if plan.tables:
        lines.append("用表：")
        lines += [f"- {item.table}" + (f"：{item.reason}" if item.reason else "") for item in plan.tables]
    if plan.fields:
        lines.append("用到的字段：")
        lines += [f"- {item.table}.{item.column}（{_USAGE_NAMES[item.usage]}）" + (f"：{item.reason}" if item.reason else "")
                  for item in plan.fields]
    if plan.joins:
        lines.append("关联：")
        for join in plan.joins:
            condition = " AND ".join(f"{join.left_table}.{left} = {join.right_table}.{right}"
                                     for left, right in zip(join.left_columns, join.right_columns))
            lines.append(f"- {condition}（{join.join_type}）" + (f"：{join.reason}" if join.reason else ""))
    if plan.filters:
        lines.append("过滤条件：")
        lines += [f"- {condition}" for condition in plan.filters]
    for label, text in (("口径说明", plan.caliber), ("用表依据", plan.table_basis), ("关联说明", plan.join_basis),
                        ("溯源说明", plan.lineage_note)):
        if text:
            lines.append(f"{label}：{text}")
    if plan.assumptions:
        lines.append(f"假设：{'；'.join(plan.assumptions)}")
    return "\n".join(lines)


def generator_messages(parsed: ParsedRequest, plan: QueryPlan, evidence: Evidence, *, previous_sql: str = "",
                       errors: Sequence[str] = ()) -> list[dict[str, str]]:
    """⑤ 生成 SQL 的消息：只列方案里的表的结构；C 组带参考样例的 SQL；修正时带上一次的 SQL 和校验错误

    修正轮追加的内容放在末尾，前面的需求、方案、表结构、样例与第一轮完全相同，前缀缓存才能命中。

    Args:
        parsed: ① 的结果
        plan: ④ 的方案
        evidence: ③ 的结果
        previous_sql: 修正时上一次生成的 SQL
        errors: 修正时上一次的校验错误（「R4：…」），不含警告

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    parts = [
        "## 需求", _render_parsed(parsed), "",
        "## 查询方案", _render_plan(plan), "",
        "## 表结构", render_tables(evidence, [item.table for item in plan.tables]), "",
    ]
    if evidence.use_examples and evidence.examples:
        parts += ["## 参考样例（只参考写法，口径以查询方案为准）",
                  _render_examples(evidence.examples, used=plan.examples_used), ""]
    if previous_sql.strip() or errors:
        if previous_sql.strip():
            parts += ["## 上一次生成的 SQL", f"```sql\n{previous_sql.strip()}\n```", ""]
        if errors:
            parts += ["## 校验错误", *[f"- {error}" for error in errors], ""]
        parts += ["## 要求", "上一次的 SQL 没有通过校验。按上面的错误逐条修正，其余保持不变，"
                           "输出修正后的完整 SQL，只输出一个 ```sql 代码块。"]
    else:
        parts += ["## 要求", "按查询方案写一条 SQL，只输出一个 ```sql 代码块。"]
    return [{"role": "system", "content": GENERATOR_SYSTEM}, {"role": "user", "content": "\n".join(parts)}]


# ── 证据文本 ──────────────────────────────────

_REASON_NAMES = {"recall": "召回", "upstream": "上溯带入", "example": "样例带入", "join": "关联路径经过的中间表"}
_KIND_MARKS = {"null": "〔恒为空〕", "etl_time": "〔加工时间〕", "constant": "〔常量〕"}
_ORIGIN_NAMES = {"manual": "手工填写", "script": "加工脚本里出现过", "lineage": "血缘传递", "grain_key": "同名粒度键"}
_NONE = "（无）"


def _one_line(text: str) -> str:
    """多行文字压成一行：任何空白都换成一个空格"""
    return " ".join(text.split())


def _clip(text: str, limit: int) -> str:
    """超过 limit 个字时截断，末尾补「…」，总长不超过 limit"""
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _is_key_column(column: EvidenceColumn, table: EvidenceTable) -> bool:
    """关键字段：命中的、粒度、数据日期、主键"""
    return (bool(column.hit_terms) or column.is_pk or column.name in table.grain_columns
            or column.name == table.date_column)


def _column_line(column: EvidenceColumn, table: EvidenceTable, *, drop_enum: bool,
                 drop_description: bool = False) -> str:
    """一个字段一行：名称、中文名、类型、标记、口径说明、码值"""
    marks = ""
    if column.name in table.grain_columns:
        marks += "〔粒度〕"
    if column.is_pk:
        marks += "〔主键〕"
    if column.name == table.date_column:
        marks += "〔数据日期〕"
    if column.hit_terms:
        marks += f"〔命中：{'、'.join(column.hit_terms)}〕"
    marks += _KIND_MARKS.get(column.kind, "")
    parts = [f"- {column.name} {column.cn}"]
    if column.data_type:
        parts.append(column.data_type)
    if marks:
        parts.append(marks)
    if column.description and not drop_description:
        parts.append(f"口径：{_one_line(column.description)}")
    if column.enum_values and not drop_enum:
        parts.append(f"码值：{_one_line(column.enum_values)}")
    return " ".join(parts)


def _table_block(table: EvidenceTable, *, drop_enums: bool = False, key_only: bool = False,
                 bare: bool = False) -> str:
    """一张表一段：标题、层级类型粒度、说明、字段清单

    Args:
        table: 证据里的表
        drop_enums: 去掉非命中字段的码值（收缩第 1 级）
        key_only: 只列命中字段、粒度字段和数据日期字段（收缩第 2、7 级）
        bare: 关键字段以外的字段不写口径说明（收缩第 5 级）
    """
    grain = ", ".join(table.grain_columns) or "无"
    lines = [
        f"### {table.table_id} {table.table_cn}",
        f"层级：{table.layer}；类型：{table.table_type}；粒度：{grain}；数据日期字段：{table.date_column or '无'}；"
        f"入选原因：{_REASON_NAMES.get(table.reason, table.reason)}",
    ]
    if table.description:
        lines.append(f"说明：{_one_line(table.description)}")
    lines.append("字段：")
    shown = 0
    for column in table.columns:
        if key_only and not (column.hit_terms or column.name in table.grain_columns or column.name == table.date_column):
            continue
        shown += 1
        lines.append(_column_line(column, table, drop_enum=drop_enums and not column.hit_terms,
                                  drop_description=bare and not _is_key_column(column, table)))
    if shown < len(table.columns):
        lines.append(f"- （另有 {len(table.columns) - shown} 个字段未列出）")
    return "\n".join(lines)


def _render_lineage(items: list[FieldLineage], condition_chars: int | None, *, expression_chars: int | None = None,
                    field_chars: int | None = None) -> list[str]:
    """指标字段的加工链路：每个字段一段，每跳写来源、表达式、过滤条件、关联条件、脚本

    Args:
        items: 证据里的加工链路
        condition_chars: 每条条件最多留多少字，None 不截
        expression_chars: 加工表达式最多留多少字，None 不截
        field_chars: 每个字段的链路最多写多少字，写满就停，但至少写第一跳；None 不限
    """
    lines: list[str] = ["（每跳写成「目标字段 ← 来源字段」：目标字段由来源字段加工而来；跳数越大越靠近源头）"] if items else []
    for item in items:
        lines.append(f"### {item.field}（指标「{item.term}」）")
        if not item.steps:
            lines.append("（没有上游）")
        cut, used = item.truncated, 0
        for step in item.steps:
            step_lines = _lineage_step(step, condition_chars, expression_chars)
            size = sum(len(line) + 1 for line in step_lines)
            if field_chars is not None and used and used + size > field_chars:
                cut = True
                break
            lines += step_lines
            used += size
        if cut:
            lines.append("（链路较长，后面的几跳没有列出）")
        lines.append("")
    return lines[:-1] if lines else lines


def _lineage_step(step: LineageStep, condition_chars: int | None, expression_chars: int | None = None) -> list[str]:
    """加工链路里的一跳"""
    def limited(text: str) -> str:
        text = _one_line(text)
        return text if condition_chars is None else _clip(text, condition_chars)

    lines = [f"第 {step.depth} 跳：{step.target} ← {'、'.join(step.sources)}"]
    if step.expression:
        expression = _one_line(step.expression)
        if expression_chars is not None:
            expression = _clip(expression, expression_chars)
        lines.append(f"  加工表达式：{expression}")
    lines += [f"  过滤条件：{limited(condition)}" for condition in step.filters]
    lines += [f"  关联条件：{limited(condition)}" for condition in step.joins]
    if step.unknown_sources:
        lines.append(f"  不在字段清单里，SQL 不能引用：{'、'.join(step.unknown_sources)}")
    if step.script_name:
        lines.append(f"  脚本：{step.script_name}")
    return lines


def _render_comparison(item: FieldComparison) -> str:
    """同源字段对比的一行"""
    if item.relation == "derived_from":
        return f"- 〔加工关系〕{item.b} 由 {item.a} 加工而来（指标「{item.term}」）"
    label = "同源不同口径" if item.relation == "same_source_diff" else "同源同口径"
    text = f"- 〔{label}〕{item.a} 与 {item.b}（指标「{item.term}」）"
    if item.common:
        text += f"；共同来源：{'、'.join(item.common)}"
    if item.note:
        text += f"；说明：{_one_line(item.note)}"
    return text


def _hop_condition(hop: JoinHop) -> str:
    """一步关联的条件：左表.字段 = 右表.字段，多个键用 AND 连起来"""
    return " AND ".join(f"{hop.left_table}.{left} = {hop.right_table}.{right}"
                        for left, right in zip(hop.left_columns, hop.right_columns))


def _render_join(hint: JoinHint) -> list[str]:
    """关联路径：直连写一行，经过中间表的每一步一行；写明键、对应关系、是否发散和依据"""
    spread = "会发散" if hint.fan_out else "不发散"
    notes = [f" {hop.note}" if hop.note else "" for hop in hint.hops]
    origins = [_ORIGIN_NAMES.get(hop.origin, hop.origin) for hop in hint.hops]
    if len(hint.hops) == 1:
        hop = hint.hops[0]
        return [f"- {hint.left_table} → {hint.right_table}：{_hop_condition(hop)}"
                f"（{hop.cardinality}，{spread}；依据：{origins[0]}）{notes[0]}"]
    middle = "、".join(hop.right_table for hop in hint.hops[:-1])
    lines = [f"- {hint.left_table} → {hint.right_table}（经过 {middle}；{spread}）："]
    for number, hop in enumerate(hint.hops, 1):
        lines.append(f"  {number}. {_hop_condition(hop)}（{hop.cardinality}；依据：{origins[number - 1]}）{notes[number - 1]}")
    return lines


def _render_examples(examples: Sequence[EvidenceExample], *, used: Sequence[str] = (), sql_chars: int | None = None) -> str:
    """相似样例：需求、口径、用到的表、SQL；used 里的样例标出「方案参考了这条」"""
    blocks = []
    for example in examples:
        sql = example.sql_text.strip()
        if sql_chars is not None:
            sql = _clip(sql, sql_chars)
        mark = "（方案参考了这条）" if example.example_id in used else ""
        lines = [f"### 样例 {example.example_id}{mark}", f"需求：{_one_line(example.requirement)}"]
        if example.caliber_note:
            lines.append(f"口径：{_one_line(example.caliber_note)}")
        if example.tables:
            lines.append(f"用到的表：{'、'.join(example.tables)}")
        lines += ["SQL：", f"```sql\n{sql}\n```"]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _section(header: str, body: list[str]) -> str:
    """一节：二级标题加内容，没有内容写「（无）」"""
    return "\n".join([header, *body]) if body else f"{header}\n{_NONE}"


def _render(evidence: Evidence, level: int) -> str:
    """证据文本，收缩到第 level 级（0 是不收缩），收缩了什么写在「说明」里"""
    tables = []
    full_tables = KEY_TABLES if level >= 7 else FULL_TABLES
    for index, table in enumerate(evidence.tables):
        tables.append(_table_block(table, drop_enums=level >= 1, key_only=level >= 2 and index >= full_tables,
                                   bare=level >= 5))
    condition_chars = (SHRINK_CONDITION_CHARS_MIN if level >= 6 else SHRINK_CONDITION_CHARS if level >= 3 else None)
    sections = [_section("## 候选表", ["\n\n".join(tables)] if tables else [])]
    if evidence.use_lineage:
        sections.append(_section("## 指标字段的加工链路", _render_lineage(
            evidence.lineage, condition_chars, expression_chars=SHRINK_EXPRESSION_CHARS if level >= 6 else None,
            field_chars=LINEAGE_FIELD_CHARS if level >= 8 else None)))
        sections.append(_section("## 同源字段对比", [_render_comparison(item) for item in evidence.comparisons]))
        sections.append(_section("## 表之间的上下游",
                                 [f"- {link.downstream} 由 {link.upstream} 加工而来" for link in evidence.table_links]))
        sections.append(_section("## 还可以上溯到的表",
                                 [f"- {option.table_id} {option.table_cn}（{option.layer}）"
                                  for option in evidence.upstream_options]))
    hints = [hint for hint in evidence.joins if level < 9 or len(hint.hops) == 1]
    sections.append(_section("## 关联路径", [line for hint in hints for line in _render_join(hint)]))
    if evidence.use_examples:
        examples = _render_examples(evidence.examples, sql_chars=SHRINK_EXAMPLE_SQL_CHARS if level >= 4 else None)
        sections.append(_section("## 相似样例", [examples] if examples else []))
    limit_note = [f"- 证据文本超过 {EVIDENCE_CHAR_LIMIT} 字，已{action}" for action in SHRINK_ACTIONS[:level]]
    sections.append(_section("## 说明", [f"- {note}" for note in evidence.notes] + limit_note))
    return "\n\n".join(sections)


def render_evidence(evidence: Evidence) -> str:
    """把证据写成给模型看的文本

    分节：候选表；B、C 组的指标字段加工链路、同源字段对比、表之间的上下游、还可以上溯到的表；
    关联路径；C 组的相似样例；最后是「说明」。超过 EVIDENCE_CHAR_LIMIT 时按 SHRINK_ACTIONS 的顺序逐级收缩，
    装下就停，每收缩一级在「说明」里记一句；九级都收完仍超过上限时就用第九级的文本。
    同样的证据必然得到同样的文本。

    Args:
        evidence: ③ 的结果

    Returns:
        证据文本
    """
    text = _render(evidence, 0)
    for level in range(1, len(SHRINK_ACTIONS) + 1):
        if len(text) <= EVIDENCE_CHAR_LIMIT:
            break
        text = _render(evidence, level)
    return text


def render_tables(evidence: Evidence, table_ids: Sequence[str]) -> str:
    """只写给定表的结构，格式同 render_evidence 的候选表一节，供 ⑤ 使用，不收缩

    Args:
        evidence: ③ 的结果
        table_ids: 要写的表，按这个顺序；不在证据里的跳过

    Returns:
        表结构文本，一张表一段，段与段之间空一行；一张也没有时是空字符串
    """
    tables = [table for table in (evidence.table(table_id) for table_id in table_ids) if table is not None]
    return "\n\n".join(_table_block(table) for table in tables)
