"""提示词：系统提示词的固定性与示例、三组消息的内容、证据文本的分节与收缩"""
import json
import re
from calendar import monthrange
from datetime import date, timedelta

import pytest
import sqlglot

import prompts.nl2sql as prompts
from nl2sql.parser import check_parsed
from nl2sql.planner import check_plan
from nl2sql.schemas import (
    Clarify,
    ClarifyOption,
    Evidence,
    EvidenceColumn,
    EvidenceTable,
    FieldComparison,
    FieldLineage,
    FilterItem,
    JoinHint,
    JoinHop,
    LineageStep,
    Metric,
    ParsedRequest,
    QueryPlan,
    QueryRequest,
    TimeSpec,
)
from prompts.nl2sql import (
    DIALECT_RULES,
    GENERATOR_SYSTEM,
    PARSER_SYSTEM,
    PLANNER_SYSTEM,
    RETRY_PROMPT,
    generator_messages,
    parser_messages,
    planner_messages,
    render_evidence,
    render_tables,
)
from tests.step_fixtures import (
    CUST_BAL,
    DIM_ORG,
    DWD_ACCT,
    EXAMPLE_ID,
    EXAMPLE_SQL,
    ORG_BAL,
    evidence_table,
    make_evidence,
    make_parsed,
    make_plan,
)

TODAY = date(2026, 10, 13)
LINEAGE_SECTIONS = ["## 指标字段的加工链路", "## 同源字段对比", "## 表之间的上下游", "## 还可以上溯到的表"]
ALL_GROUP_SECTIONS = ["## 候选表", "## 关联路径", "## 说明"]
EXAMPLE_SECTION = "## 相似样例"


@pytest.fixture(scope="module")
def evidence(mock_store):
    return make_evidence(mock_store)


def json_blocks(text: str) -> list[str]:
    """系统提示词里 ```json 代码块的内容"""
    return re.findall(r"```json\n(.*?)```", text, re.S)


def sql_blocks(text: str) -> list[str]:
    """文本里 ```sql 代码块的内容"""
    return re.findall(r"```sql\n(.*?)```", text, re.S)


def table_block(text: str, table_id: str) -> str:
    """证据文本里一张表的那一段，从「### 表标识 」到下一个标题"""
    start = text.index(f"### {table_id} ")
    ends = [e for e in (text.find("\n### ", start + 1), text.find("\n## ", start + 1)) if e != -1]
    return text[start:min(ends)] if ends else text[start:]


def section(text: str, header: str) -> str:
    """证据文本里一节的内容，从标题到下一个二级标题"""
    start = text.index(header)
    end = text.find("\n## ", start + 1)
    return text[start:end] if end != -1 else text[start:]


def evidence_for(plan: QueryPlan) -> Evidence:
    """按方案里写到的表和字段造一份证据，用来核对提示词里的示例方案自洽"""
    names: dict[str, set[str]] = {}
    for item in plan.tables:
        names.setdefault(item.table, set())
    for item in plan.fields:
        names.setdefault(item.table, set()).add(item.column)
    for join in plan.joins:
        names.setdefault(join.left_table, set()).update(join.left_columns)
        names.setdefault(join.right_table, set()).update(join.right_columns)
    tables = [EvidenceTable(table_id=table, table_cn=table, layer="专题层", table_type="快照表", reason="recall",
                            columns=[EvidenceColumn(name=name, cn=name) for name in sorted(columns)])
              for table, columns in names.items()]
    return Evidence(use_lineage=True, use_examples=False, tables=tables)


# ── 系统提示词是固定的，带 JSON 格式示例 ────────

@pytest.mark.parametrize("system", [PARSER_SYSTEM, PLANNER_SYSTEM])
def test_json_steps_ask_for_json_only_and_show_the_format(system):
    assert "只输出 JSON" in system and "json" in system.lower()
    assert json_blocks(system)                                  # DeepSeek 的 JSON 模式要求提示词里有格式示例


def test_parser_examples_are_valid_parsed_requests():
    parsed = [ParsedRequest.model_validate(json.loads(block)) for block in json_blocks(PARSER_SYSTEM)]

    assert len(parsed) >= 3
    assert all(check_parsed(item) == [] for item in parsed)
    assert any(not item.is_query for item in parsed)                              # 不是查数需求
    assert any(item.time.compare == "prev_year_end" for item in parsed)           # 较年初
    assert any(metric.entity for item in parsed for metric in item.metrics)       # 计数类带被计数的实体
    assert any(len(item.sub_questions) >= 2 for item in parsed)                   # 一句多问
    assert any(item.clarify is not None for item in parsed)                       # 缺对象时 ① 反问
    assert any(item.assumptions and item.clarify is None and "第一个选项" in item.assumptions[0] for item in parsed)


def pending_clarify_examples() -> list[ParsedRequest]:
    """① 的示例里上一轮停在反问上的那几个（示例前面写了「上一轮的反问」），按出现的顺序"""
    examples = []
    for match in re.finditer(r"((?:(?!```).)*?)```json\n(.*?)```", PARSER_SYSTEM, re.S):
        if "上一轮的反问：" in match.group(1):
            examples.append(ParsedRequest.model_validate(json.loads(match.group(2))))
    return examples


def test_the_parser_examples_show_both_an_answer_and_a_new_question_after_a_clarify():
    """上一轮停在反问上：一个示例是回答（不能再问，按第一个选项），一个是没理会反问的新问题（照常反问）"""
    answer, new_question = pending_clarify_examples()

    assert all(check_parsed(item, pending_clarify=True) == [] for item in (answer, new_question))
    assert answer.answers_clarify and answer.clarify is None and "第一个选项" in answer.assumptions[0]
    assert not new_question.answers_clarify and new_question.clarify is not None
    assert "没有回答上一轮的反问" in new_question.assumptions[0]
    assert "保证金" not in new_question.requirement                     # 上一轮的需求和反问都不带过来


def test_the_parser_example_lists_objects_as_a_query_even_after_an_unrelated_round():
    """只列某类对象的信息也是查数需求：指标写对象的叫法、不带被计数的实体；和上一轮接不上时按新需求解析"""
    before, _, after = PARSER_SYSTEM.partition("用户输入：查询客户经理的信息")
    parsed = ParsedRequest.model_validate(json.loads(json_blocks(after)[0]))

    assert before.rstrip().splitlines()[-1].startswith("上一轮的完整需求：")
    assert parsed.is_query and parsed.clarify is None and parsed.dimensions == []
    assert [(metric.name, metric.entity) for metric in parsed.metrics] == [("客户经理", "")]     # 列明细，不是计数
    assert "合同" not in parsed.requirement                                                 # 上一轮的合同数不带过来


def test_planner_examples_are_valid_plans_covering_the_four_decisions():
    plans = [QueryPlan.model_validate(json.loads(block)) for block in json_blocks(PLANNER_SYSTEM)]

    assert {plan.decision for plan in plans} == {"ok", "need_clarify", "need_upstream", "cannot_answer"}
    for plan in plans:
        assert check_plan(plan, evidence_for(plan), allow_clarify=True, allow_upstream=True) == [], plan.decision


def test_dialect_rules_are_the_eleven_numbered_rules():
    assert re.findall(r"^(\d+)\. ", DIALECT_RULES, re.M) == [str(number) for number in range(1, 12)]
    for keyword in ("PostgreSQL", "GaussDB", "DATE 'YYYY-MM-DD'", "CURRENT_DATE", "SELECT *", "CASE WHEN", "NULLIF",
                    "ORDER BY", "LIMIT", "GROUPING SETS", "COUNT(DISTINCT"):
        assert keyword in DIALECT_RULES, keyword


def test_generator_system_embeds_the_rules_and_its_sql_examples_parse():
    assert DIALECT_RULES in GENERATOR_SYSTEM
    examples = sql_blocks(GENERATOR_SYSTEM)

    assert examples
    for sql in examples:
        statements = [item for item in sqlglot.parse(sql, read="postgres") if item is not None]
        assert len(statements) == 1


def test_retry_prompt_is_the_one_written_in_w0():
    assert RETRY_PROMPT == ("你上一次的输出有下面这些问题：\n{problems}\n\n"
                            "请逐条改正，重新输出完整的 JSON。只输出 JSON，不要写解释，不要用代码块包起来。")


def test_system_messages_never_change_between_calls(evidence):
    other = QueryRequest(question="换成含保证金的", prev_requirement="查询 2026-09-30 各开户机构的对公存款余额",
                         prev_clarify=Clarify(question="要不要含保证金？", options=[ClarifyOption(label="含")]))
    assert parser_messages(QueryRequest(question="查贷款"), TODAY)[0] == \
        parser_messages(other, date(2030, 2, 3))[0] == {"role": "system", "content": PARSER_SYSTEM}
    assert planner_messages(make_parsed(), evidence, allow_clarify=True, allow_upstream=True)[0] == \
        planner_messages(make_parsed(sub_questions=["a", "b"]), evidence.model_copy(update={"scope_level": 2}),
                         allow_clarify=False, allow_upstream=False)[0] == {"role": "system", "content": PLANNER_SYSTEM}
    assert generator_messages(make_parsed(), make_plan(), evidence)[0] == \
        generator_messages(make_parsed(), make_plan(), evidence, previous_sql="SELECT 1", errors=["R1：错"])[0] == \
        {"role": "system", "content": GENERATOR_SYSTEM}


# ── ① 需求解析的消息 ──────────────────────────

def test_parser_messages_are_a_system_and_a_user_message():
    messages = parser_messages(QueryRequest(question="查 2026 年 9 月末对公贷款余额"), TODAY)

    assert [m["role"] for m in messages] == ["system", "user"]
    assert "本轮输入：查 2026 年 9 月末对公贷款余额" in messages[1]["content"]


def test_parser_user_message_has_today_and_the_reference_dates():
    user = parser_messages(QueryRequest(question="查上月末对公贷款余额"), TODAY)[1]["content"]

    assert "今天：2026-10-13" in user
    for expected in ("上一自然日 2026-10-12", "上月末 2026-09-30", "上季末 2026-09-30", "去年末 2025-12-31",
                     "本月初 2026-10-01", "本年初 2026-01-01"):
        assert expected in user, expected


@pytest.mark.parametrize("today, expected", [
    (date(2026, 3, 31), ["上一自然日 2026-03-30", "上月末 2026-02-28", "上季末 2025-12-31"]),
    (date(2028, 3, 1), ["上一自然日 2028-02-29", "上月末 2028-02-29"]),                         # 闰年
    (date(2026, 1, 1), ["上一自然日 2025-12-31", "上月末 2025-12-31", "去年末 2025-12-31", "本月初 2026-01-01"]),
    (date(2026, 5, 20), ["上月末 2026-04-30", "上季末 2026-03-31", "去年末 2025-12-31"]),
])
def test_reference_dates_follow_the_calendar(today, expected):
    user = parser_messages(QueryRequest(question="查余额"), today)[1]["content"]

    for fragment in expected:
        assert fragment in user, fragment


def month_end_line(today: date) -> str:
    """用户消息里「没写年份的月末」那一行"""
    user = parser_messages(QueryRequest(question="查 9 月末对公贷款余额"), today)[1]["content"]
    return next(item for item in user.splitlines() if item.startswith("没写年份的月末"))


def month_ends_in(today: date) -> dict[int, date]:
    """「没写年份的月末」那一行，{月份: 日期}"""
    pairs = re.findall(r"(\d+) 月末 (\d{4}-\d{2}-\d{2})", month_end_line(today))
    return {int(month): date.fromisoformat(day) for month, day in pairs}


@pytest.mark.parametrize("today, expected", [
    (date(2026, 10, 20), {9: "2026-09-30", 1: "2026-01-31", 11: "2025-11-30", 12: "2025-12-31"}),  # 线上报的：9 月末是今年的
    (date(2026, 10, 20), {10: "2025-10-31"}),                                   # 本月的月末还没到，取去年的
    (date(2026, 10, 31), {10: "2026-10-31", 11: "2025-11-30"}),                 # 今天就是月末，不晚于今天，取今年的
    (date(2027, 1, 15), {1: "2026-01-31", 9: "2026-09-30", 12: "2026-12-31"}),  # 跨年：1 月中旬，全是去年的
    (date(2027, 2, 1), {1: "2027-01-31", 2: "2026-02-28"}),                     # 跨年后第一个月末刚过
    (date(2026, 12, 31), {1: "2026-01-31", 12: "2026-12-31"}),                  # 一年的最后一天，全是今年的
    (date(2028, 3, 1), {2: "2028-02-29", 3: "2027-03-31"}),                     # 闰年
])
def test_month_ends_without_a_year_take_the_latest_one_not_after_today(today, expected):
    ends = month_ends_in(today)

    for month, day in expected.items():
        assert ends[month].isoformat() == day, month


def test_month_ends_without_a_year_hold_for_every_day_of_two_years():
    today = date(2027, 1, 1)
    while today.year < 2029:                                                    # 2028 是闰年
        ends = month_ends_in(today)
        assert sorted(ends) == list(range(1, 13)), today
        for month, day in ends.items():
            assert day.month == month and (day + timedelta(days=1)).day == 1, today   # 是那个月的最后一天
            assert day <= today, today                                          # 不晚于今天
            assert date(day.year + 1, month, monthrange(day.year + 1, month)[1]) > today, today   # 是最近的一个
        today += timedelta(days=1)


@pytest.mark.parametrize("today, months_from_last_year", [
    (date(2026, 10, 20), [10, 11, 12]),
    (date(2027, 1, 15), list(range(1, 13))),          # 1 月末还没到：全是去年的
    (date(2027, 1, 31), list(range(2, 13))),
    (date(2026, 12, 31), []),                         # 全是今年的，不写「取去年的」那一组
])
def test_month_ends_from_last_year_are_listed_as_their_own_group(today, months_from_last_year):
    head, label, tail = month_end_line(today).partition("今年的还没到、取去年的：")

    assert bool(label) == bool(months_from_last_year)
    assert [int(month) for month in re.findall(r"(\d+) 月末", tail)] == months_from_last_year
    assert all(day.startswith(f"{today.year}-") for day in re.findall(r"\d{4}-\d{2}-\d{2}", head))
    assert all(day.startswith(f"{today.year - 1}-") for day in re.findall(r"\d{4}-\d{2}-\d{2}", tail))


def test_parser_time_rules_send_month_ends_to_the_reference_line_instead_of_fixed_dates():
    rules = PARSER_SYSTEM.split("# 时间的换算", 1)[1].split("# 追问", 1)[0]

    # 按另一个今天写的「「9 月末」→ 2025-09-30」会被模型照抄：今天是 2026-10-20 时也换算成去年
    assert re.search(r"「\d+ ?月[末底][^」]*」[^。；\n]*?→ ?\d{4}-", rules) is None
    assert "「没写年份的月末」" in rules


def test_parser_user_message_without_context_mentions_no_previous_round():
    user = parser_messages(QueryRequest(question="查 2026 年 9 月末对公贷款余额"), TODAY)[1]["content"]

    assert "上一轮" not in user


def test_parser_user_message_carries_the_previous_requirement():
    request = QueryRequest(question="按一级分行拆开", prev_requirement="查询 2026-09-30 全行不含保证金的对公存款余额")

    user = parser_messages(request, TODAY)[1]["content"]

    assert "上一轮的完整需求：查询 2026-09-30 全行不含保证金的对公存款余额" in user
    assert "本轮输入：按一级分行拆开" in user and "追问" in user
    assert "上一轮的反问" not in user


def test_parser_user_message_carries_the_previous_clarify_with_its_options():
    clarify = Clarify(question="存款余额要不要包含保证金？", options=[
        ClarifyOption(label="不含保证金", meaning="用存款余额字段，加工时剔除了保证金账户"),
        ClarifyOption(label="含保证金"),
    ])
    request = QueryRequest(question="含保证金的", prev_requirement="查询 2026-09-30 各开户机构的对公存款余额",
                           prev_clarify=clarify)

    user = parser_messages(request, TODAY)[1]["content"]

    assert "上一轮的完整需求：查询 2026-09-30 各开户机构的对公存款余额" in user
    assert "上一轮的反问：存款余额要不要包含保证金？" in user
    assert "选项 1：不含保证金（用存款余额字段，加工时剔除了保证金账户）" in user
    assert "选项 2：含保证金" in user
    assert "对这个反问的回答" in user and "本轮输入：含保证金的" in user
    assert "问了一个新问题" in user and "answers_clarify" in user          # 是不是回答由模型判断


def test_a_previous_clarify_is_shown_even_without_a_previous_requirement():
    clarify = Clarify(question="要不要含保证金？", options=[ClarifyOption(label="含"), ClarifyOption(label="不含")])

    user = parser_messages(QueryRequest(question="不含", prev_clarify=clarify), TODAY)[1]["content"]

    assert "上一轮的反问：要不要含保证金？" in user and "上一轮的完整需求" not in user


def test_parser_messages_are_deterministic():
    request = QueryRequest(question="查余额", prev_requirement="查询 2026-09-30 全行对公存款余额")

    assert parser_messages(request, TODAY) == parser_messages(request, TODAY)


# ── ④ 选表定口径的消息 ────────────────────────

def test_planner_user_message_carries_the_parsed_request(evidence):
    parsed = make_parsed(
        metrics=[Metric(name="有余额客户数", entity="客户")],
        filters=[FilterItem(field="行业", condition="= 制造业")],
        order="按余额降序取前 10",
        time=TimeSpec(kind="point", dates=["2026-09-30"], compare="prev_year_end", compare_dates=["2025-12-31"],
                      note="9 月末 → 2026-09-30"),
        sub_questions=["年末有余额客户数", "年初有余额客户数"],
        assumptions=["「最新」按上一自然日 2026-10-12 处理"],
    )

    user = planner_messages(parsed, evidence, allow_clarify=True, allow_upstream=True)[1]["content"]

    for fragment in (parsed.requirement, "有余额客户数（被计数的实体：客户）", "一级分行名称", "行业 = 制造业",
                     "按余额降序取前 10", "时点 2026-09-30", "较年初", "2025-12-31", "9 月末 → 2026-09-30",
                     "口径线索：不含保证金", "1. 年末有余额客户数", "2. 年初有余额客户数",
                     "「最新」按上一自然日 2026-10-12 处理"):
        assert fragment in user, fragment


def test_planner_user_message_carries_the_rendered_evidence(evidence):
    user = planner_messages(make_parsed(), evidence, allow_clarify=True, allow_upstream=True)[1]["content"]

    assert render_evidence(evidence) in user


def test_planner_user_message_states_the_two_switches(evidence):
    both = planner_messages(make_parsed(), evidence, allow_clarify=True, allow_upstream=True)[1]["content"]
    neither = planner_messages(make_parsed(), evidence, allow_clarify=False, allow_upstream=False)[1]["content"]

    assert "能不能反问：能" in both and "能不能上溯：能" in both
    assert "能不能反问：不能" in neither and "不能再反问" in neither and "assumptions" in neither
    assert "能不能上溯：不能" in neither and "cannot_answer" in neither


@pytest.mark.parametrize("scope_level, all_layers, expected", [
    (0, False, "第 0 层"), (1, False, "第 1 层"), (2, False, "第 2 层"), (0, True, "全部层级"),
])
def test_planner_user_message_names_the_candidate_scope(mock_store, scope_level, all_layers, expected):
    evidence = make_evidence(mock_store, scope_level=scope_level, all_layers=all_layers)

    user = planner_messages(make_parsed(), evidence, allow_clarify=True, allow_upstream=True)[1]["content"]

    assert f"候选范围：{expected}" in user


def test_planner_user_message_says_what_the_evidence_lacks(mock_store):
    group_a = make_evidence(mock_store, use_lineage=False, use_examples=False, all_layers=True)
    group_c = make_evidence(mock_store)

    a = planner_messages(make_parsed(), group_a, allow_clarify=True, allow_upstream=False)[1]["content"]
    c = planner_messages(make_parsed(), group_c, allow_clarify=True, allow_upstream=True)[1]["content"]

    assert "血缘：没有" in a and "样例：没有" in a and "examples_used 留空" in a
    assert "血缘：有" in c and "样例：有 1 条" in c and EXAMPLE_ID in c


# ── ⑤ 生成 SQL 的消息 ─────────────────────────

def test_generator_user_message_carries_requirement_plan_and_table_structures(evidence):
    plan = make_plan(assumptions=["未指定币种，按折人民币汇总"])

    user = generator_messages(make_parsed(), plan, evidence)[1]["content"]

    assert make_parsed().requirement in user
    for fragment in ("## 查询方案", f"{ORG_BAL}.data_dt = DATE '2026-09-30'", plan.caliber, plan.join_basis,
                     "未指定币种，按折人民币汇总", f"{ORG_BAL}.org_no = {DIM_ORG}.org_no（INNER）", "## 表结构"):
        assert fragment in user, fragment
    assert render_tables(evidence, [ORG_BAL, DIM_ORG]) in user


def test_generator_user_message_lists_only_the_plan_tables(evidence):
    user = generator_messages(make_parsed(), make_plan(), evidence)[1]["content"]
    structure = user.split("## 表结构")[1]

    assert f"### {ORG_BAL} " in structure and f"### {DIM_ORG} " in structure
    assert f"### {CUST_BAL} " not in structure and "mtd_avg_bal_rmb" not in user


def test_generator_user_message_carries_example_sql_in_the_example_group_only(mock_store):
    group_c = make_evidence(mock_store)
    group_b = make_evidence(mock_store, use_examples=False)

    c = generator_messages(make_parsed(), make_plan(), group_c)[1]["content"]
    b = generator_messages(make_parsed(), make_plan(), group_b)[1]["content"]

    assert "## 参考样例" in c and EXAMPLE_SQL in c and "含保证金用全口径存款余额" in c
    assert "## 参考样例" not in b and "DATE '2025-12-31'" not in b


def test_generator_user_message_marks_the_examples_the_plan_used(evidence):
    used = generator_messages(make_parsed(), make_plan(examples_used=[EXAMPLE_ID]), evidence)[1]["content"]
    unused = generator_messages(make_parsed(), make_plan(), evidence)[1]["content"]

    assert "方案参考了这条" in used and "方案参考了这条" not in unused


def test_generator_user_message_has_no_repair_section_on_the_first_round(evidence):
    user = generator_messages(make_parsed(), make_plan(), evidence)[1]["content"]

    assert "上一次生成的 SQL" not in user and "校验错误" not in user


def test_generator_repair_message_carries_the_previous_sql_and_the_errors(evidence):
    errors = ["R5：快照表 ads.org_corp_dep_bal_d（别名 b）没有数据日期条件", "R4：表 dim.org 没有字段 lvl1_name"]

    user = generator_messages(make_parsed(), make_plan(), evidence, previous_sql="SELECT 1 FROM x", errors=errors)[1]["content"]

    assert "## 上一次生成的 SQL" in user and "```sql\nSELECT 1 FROM x\n```" in user
    assert "## 校验错误" in user
    for error in errors:
        assert f"- {error}" in user
    assert "其余保持不变" in user


def test_generator_repair_round_shares_its_prefix_with_the_first_round(evidence):
    first = generator_messages(make_parsed(), make_plan(), evidence)[1]["content"]
    repair = generator_messages(make_parsed(), make_plan(), evidence, previous_sql="SELECT 1", errors=["R1：错"])[1]["content"]

    common = 0
    while common < min(len(first), len(repair)) and first[common] == repair[common]:
        common += 1
    assert common >= len(first.split("## 要求")[0])             # 表结构、样例都在共同的前缀里，前缀缓存才能命中


BASE_SQL = "SELECT SUM(b.dep_bal_rmb) AS dep_bal FROM ads.org_corp_dep_bal_d AS b WHERE b.data_dt = DATE '2026-08-31'"


def test_a_follow_up_hands_the_previous_sql_to_the_planner_and_the_generator(evidence):
    planner = planner_messages(make_parsed(), evidence, allow_clarify=True, allow_upstream=True,
                               base_sql=BASE_SQL)[1]["content"]
    generator = generator_messages(make_parsed(), make_plan(), evidence, base_sql=BASE_SQL)[1]["content"]

    assert BASE_SQL in planner and planner.index("# 上一轮的 SQL") < planner.index("# 证据")
    assert BASE_SQL in generator and generator.index("## 上一轮的 SQL") < generator.index("## 要求")
    plain = planner_messages(make_parsed(), evidence, allow_clarify=True, allow_upstream=True)[1]["content"]
    assert "上一轮的 SQL" not in plain and "上一轮的 SQL" not in generator_messages(make_parsed(), make_plan(), evidence)[1]["content"]


def test_a_follow_up_repair_round_still_shares_its_prefix_with_the_first_round(evidence):
    first = generator_messages(make_parsed(), make_plan(), evidence, base_sql=BASE_SQL)[1]["content"]
    repair = generator_messages(make_parsed(), make_plan(), evidence, base_sql=BASE_SQL, previous_sql="SELECT 1",
                                errors=["R1：错"])[1]["content"]

    assert repair.startswith(first.split("## 要求")[0])                 # 上一轮的 SQL 也在共同的前缀里


def test_the_planner_is_told_to_use_non_standard_tables_only_as_a_last_resort():
    rule = PLANNER_SYSTEM.split("\n4. 选表")[1].split("\n5. ")[0]
    assert "入选原因是「非标准表」的表没有收进设计文档" in rule and "都满足不了时才用" in rule
    assert "缺的东西单独召回、非标准表都一样" in PLANNER_SYSTEM                   # 缺东西时也要先看它们


def test_the_two_model_steps_are_told_how_to_build_on_the_previous_round():
    assert "14. 追问" in PLANNER_SYSTEM and "上一轮限定的范围" in PLANNER_SYSTEM and "缺的东西单独召回" in PLANNER_SYSTEM
    assert "追问时，用户消息里会给出上一轮的 SQL" in GENERATOR_SYSTEM


def test_the_parser_examples_show_both_a_follow_up_and_a_new_question_after_a_previous_round():
    parsed = [ParsedRequest.model_validate(json.loads(block)) for block in json_blocks(PARSER_SYSTEM)]

    assert {item.follows_up for item in parsed if "follows_up" in item.model_fields_set} == {True, False}


# ── render_evidence：分节 ─────────────────────

@pytest.mark.parametrize("use_lineage, use_examples, all_layers", [
    (False, False, True),        # A 组
    (True, False, False),        # B 组
    (True, True, False),         # C 组
])
def test_each_group_gets_exactly_its_sections(mock_store, use_lineage, use_examples, all_layers):
    text = render_evidence(make_evidence(mock_store, use_lineage=use_lineage, use_examples=use_examples,
                                         all_layers=all_layers))

    for header in ALL_GROUP_SECTIONS:
        assert header in text, header
    for header in LINEAGE_SECTIONS:
        assert (header in text) == use_lineage, header
    assert (EXAMPLE_SECTION in text) == use_examples


def test_sections_come_in_a_fixed_order(evidence):
    text = render_evidence(evidence)
    headers = ["## 候选表", *LINEAGE_SECTIONS, "## 关联路径", EXAMPLE_SECTION, "## 说明"]

    positions = [text.index(header) for header in headers]

    assert positions == sorted(positions)


def test_sections_without_content_say_none_instead_of_disappearing(mock_store):
    bare = Evidence(use_lineage=True, use_examples=True, tables=make_evidence(mock_store).tables[:1])

    text = render_evidence(bare)

    for header in [*LINEAGE_SECTIONS, "## 关联路径", EXAMPLE_SECTION, "## 说明"]:
        assert section(text, header).strip().endswith("（无）"), header


def test_group_a_text_has_no_lineage_or_example_content(mock_store):
    text = render_evidence(make_evidence(mock_store, use_lineage=False, use_examples=False, all_layers=True))

    for absent in ("加工表达式", "SUM(ads.corp_cust_dep_bal_d.dep_bal_rmb)", "同源不同口径", EXAMPLE_SQL, "〔恒为空〕"):
        assert absent not in text, absent
    assert "关联路径" in text and "org_no = dim.org.org_no" in text


# ── render_evidence：候选表 ───────────────────

def test_each_candidate_table_gets_a_block_with_its_facts(evidence):
    block = table_block(render_evidence(evidence), ORG_BAL)

    assert block.startswith(f"### {ORG_BAL} 机构对公存款余额日汇总")
    assert "层级：专题层；类型：快照表；粒度：data_dt, org_no；数据日期字段：data_dt；入选原因：召回" in block
    assert "说明：按开户机构汇总的对公存款余额" in block
    assert "字段：" in block


def test_columns_are_listed_one_per_line_with_markers(evidence):
    block = table_block(render_evidence(evidence), ORG_BAL)

    assert "- data_dt 数据日期 date 〔粒度〕〔主键〕〔数据日期〕" in block.splitlines()
    assert "- org_no 开户机构号 varchar(10) 〔粒度〕〔主键〕〔命中：机构〕" in block.splitlines()
    assert any(line.startswith("- dep_bal_rmb 存款余额 numeric(18,2) 〔命中：对公存款余额〕") for line in block.splitlines())
    assert len([line for line in block.splitlines() if line.startswith("- ")]) == len(evidence.tables[0].columns)


def test_column_notes_show_description_and_enum_values(mock_store):
    evidence = make_evidence(mock_store)
    evidence.tables.append(evidence_table(mock_store, DWD_ACCT, reason="upstream"))

    text = render_evidence(evidence)

    assert "口径：折人民币" in table_block(text, ORG_BAL)
    assert "- is_margin 是否保证金 varchar(1) 码值：1=是;0=否" in table_block(text, DWD_ACCT)


def test_code_columns_show_their_name_column_or_code_table(mock_store):
    evidence = make_evidence(mock_store)
    by_name = {column.name: column for column in evidence.tables[0].columns}
    by_name["org_no"].code_table = "dim.org.org_nm"
    by_name["dep_bal_rmb"].name_column = "dep_nm"

    block = table_block(render_evidence(evidence), ORG_BAL)

    assert "- org_no 开户机构号 varchar(10) 〔粒度〕〔主键〕〔命中：机构〕 码值表：dim.org.org_nm" in block.splitlines()
    assert "名称字段：dep_nm" in block


def test_level_one_drops_the_name_hints_of_columns_that_were_not_hit(mock_store, monkeypatch):
    evidence = heavy_evidence(mock_store)
    by_name = {column.name: column for column in evidence.tables[0].columns}
    by_name["margin_bal_rmb"].name_column = "margin_nm"                  # 没命中
    by_name["org_no"].code_table = "dim.org.org_nm"                      # 命中
    texts = shrunk_texts(evidence, monkeypatch, levels=1)

    assert "名称字段：margin_nm" in table_block(texts[0], ORG_BAL)
    assert "名称字段：margin_nm" not in table_block(texts[1], ORG_BAL)
    assert "码值表：dim.org.org_nm" in table_block(texts[1], ORG_BAL)


def test_the_generator_is_told_not_to_use_the_tables_the_plan_dropped(evidence):
    plan = make_plan(dropped_tables=["dim.corp_cust"])

    user = generator_messages(make_parsed(), plan, evidence)[1]["content"]

    assert "不要用的表：dim.corp_cust" in user.split("## 表结构")[0]
    assert "不要用的表" not in generator_messages(make_parsed(), make_plan(), evidence)[1]["content"]


def test_clarify_rules_name_the_usual_splits_and_forbid_choosing_a_caliber_by_default():
    assert "日均的统计区间" in PLANNER_SYSTEM and "只说清了其中一个分岔" in PLANNER_SYSTEM
    assert "不同的指标不算分岔" in PLANNER_SYSTEM
    assert "就说明这里该反问" in PLANNER_SYSTEM
    assert "按规则 3 反问" in PLANNER_SYSTEM                                       # 只说「日均」时
    assert "汇总程度高、但没有这个指标字段的表不能拿别的字段来凑" in PLANNER_SYSTEM


def test_the_parser_asks_back_only_for_a_missing_object_or_metric_and_never_while_answering():
    assert "要看的是单个对象" in PARSER_SYSTEM and "口径的分岔" in PARSER_SYSTEM
    assert "回答反问时 clarify 写 null" in PARSER_SYSTEM
    assert "没理会反问，问了一个新问题" in PARSER_SYSTEM and "照常用 clarify 反问" in PARSER_SYSTEM
    assert "恰好带了选项里的说法，也是新问题" in PARSER_SYSTEM                   # 带了选项的词不等于在回答


def test_code_value_rules_tell_both_model_steps_to_filter_names_with_the_name_column():
    assert "「名称字段」「码值表」的是代码字段" in PLANNER_SYSTEM
    assert "不要拿中文名称去比代码字段" in DIALECT_RULES


def test_script_kinds_mark_empty_etl_time_and_constant_columns(mock_store):
    evidence = make_evidence(mock_store)
    by_name = {column.name: column for column in evidence.tables[0].columns}
    by_name["margin_bal_rmb"].kind = "null"
    by_name["ytd_avg_bal_rmb"].kind = "etl_time"
    by_name["dep_cust_cnt"].kind = "constant"
    by_name["dep_bal_tot_rmb"].kind = "passthrough"

    block = table_block(render_evidence(evidence), ORG_BAL)

    assert "- margin_bal_rmb 保证金存款余额 numeric(18,2) 〔恒为空〕" in block
    assert "- ytd_avg_bal_rmb 年日均存款余额 numeric(18,2) 〔加工时间〕" in block
    assert "- dep_cust_cnt 有余额客户数 integer 〔常量〕" in block
    assert "- dep_bal_tot_rmb 全口径存款余额 numeric(18,2) 〔命中：对公存款余额〕" in block       # 直取、加工不加标记


@pytest.mark.parametrize("reason, expected", [
    ("recall", "入选原因：召回"), ("upstream", "入选原因：上溯带入"),
    ("example", "入选原因：样例带入"), ("join", "入选原因：关联路径经过的中间表"),
    ("previous", "入选原因：上一轮用过"), ("missing", "入选原因：缺的东西单独召回"),
    ("extra", "入选原因：非标准表（设计文档没收录，单独排名补进来）"),
])
def test_candidate_reasons_are_spelled_out(mock_store, reason, expected):
    evidence = make_evidence(mock_store)
    evidence.tables[2].reason = reason

    assert expected in table_block(render_evidence(evidence), DIM_ORG)


def test_tables_from_the_fifth_on_list_only_key_columns_and_their_name_columns(mock_store):
    evidence = make_evidence(mock_store)
    evidence.tables.append(evidence_table(mock_store, DWD_ACCT, reason="upstream"))
    fifth = evidence.tables[0].model_copy(deep=True)
    fifth.table_id = "ads.extra_05"
    next(column for column in fifth.columns if column.name == "org_no").name_column = "org_nm"
    fifth.columns.append(EvidenceColumn(name="org_nm", cn="开户机构名称", data_type="varchar(60)"))
    evidence.tables.append(fifth)

    text = render_evidence(evidence)

    assert "另有 " not in table_block(text, DWD_ACCT)                       # 第 4 张照旧全列
    kept = table_block(text, "ads.extra_05")
    for name in ("data_dt", "org_no", "dep_bal_rmb", "dep_bal_tot_rmb", "org_nm"):    # 粒度、数据日期、主键、命中、名称
        assert f"- {name} " in kept, name
    assert "- margin_bal_rmb " not in kept and "- ytd_avg_bal_rmb " not in kept
    assert "- （另有 3 个字段未列出）" in kept
    assert prompts.COMPACT_NOTE in section(text, "## 说明")
    assert prompts.COMPACT_NOTE not in render_evidence(make_evidence(mock_store))          # 三张表时不提


@pytest.mark.parametrize("reason", ["upstream", "example", "previous", "missing"])
def test_tables_brought_in_by_lifting_or_examples_are_listed_in_full_even_after_the_fourth(mock_store, reason):
    """上溯、样例带入的表是模型要从里面找缺的字段的地方：压成只列关键字段，模型看不到就会一直要求上溯"""
    evidence = make_evidence(mock_store)
    evidence.tables.append(evidence_table(mock_store, CUST_BAL, reason="recall"))
    evidence.tables[3].table_id = "ads.extra_04"
    evidence.tables.append(evidence_table(mock_store, DWD_ACCT, reason=reason))

    text = render_evidence(evidence)

    assert "另有 " not in table_block(text, DWD_ACCT)                       # 第 5 张，但照旧全列
    assert prompts.COMPACT_NOTE not in section(text, "## 说明")             # 一张也没压缩，不提


def test_tables_keep_the_order_of_the_evidence(evidence):
    text = render_evidence(evidence)

    assert [text.index(f"### {table.table_id} ") for table in evidence.tables] == sorted(
        text.index(f"### {table.table_id} ") for table in evidence.tables)


# ── render_evidence：B、C 组的四节 ────────────

def test_lineage_section_shows_each_hop_with_expression_filters_joins_and_script(evidence):
    text = section(render_evidence(evidence), "## 指标字段的加工链路")

    assert f"### {ORG_BAL}.dep_bal_rmb（指标「对公存款余额」）" in text
    assert f"第 1 跳：{ORG_BAL}.dep_bal_rmb ← {CUST_BAL}.dep_bal_rmb" in text
    assert f"加工表达式：SUM({CUST_BAL}.dep_bal_rmb)" in text
    assert f"过滤条件：WHERE {CUST_BAL}.data_dt = CAST('${{data_dt}}' AS DATE)" in text
    assert f"第 2 跳：{CUST_BAL}.dep_bal_rmb ← {DWD_ACCT}.bal_rmb、{DWD_ACCT}.is_margin" in text
    assert f"{DWD_ACCT}.is_margin = '0'" in text
    assert f"关联条件：JOIN dim.corp_cust ON {DWD_ACCT}.cust_no = dim.corp_cust.cust_no" in text
    assert "脚本：22_ads_org_corp_dep_bal_d.sql" in text and "脚本：21_ads_corp_cust_dep_bal_d.sql" in text


def test_lineage_section_flags_unknown_sources_and_truncation(mock_store):
    evidence = make_evidence(mock_store)
    step = evidence.lineage[0].steps[1]
    evidence.lineage[0].steps[1] = step.model_copy(update={"unknown_sources": ["ext.src_sys.bal"]})
    evidence.lineage[0].truncated = True

    text = section(render_evidence(evidence), "## 指标字段的加工链路")

    assert "不在字段清单里，SQL 不能引用：ext.src_sys.bal" in text
    assert "后面的几跳没有列出" in text


def test_lineage_step_without_conditions_shows_only_what_it_has(mock_store):
    evidence = make_evidence(mock_store)
    evidence.lineage[0].steps = [LineageStep(depth=1, target=f"{ORG_BAL}.org_no", sources=[f"{CUST_BAL}.org_no"])]

    text = section(render_evidence(evidence), "## 指标字段的加工链路")

    assert "过滤条件" not in text and "关联条件" not in text and "加工表达式" not in text and "脚本：" not in text


def test_a_hop_reached_from_two_fields_is_written_in_full_only_once(mock_store):
    evidence = make_evidence(mock_store)
    shared_hop = evidence.lineage[0].steps[1]                                   # 客户汇总表的 dep_bal_rmb 那一跳
    evidence.lineage.append(FieldLineage(field=f"{CUST_BAL}.dep_bal_rmb", term="对公存款余额",
                                         steps=[shared_hop.model_copy(update={"depth": 1})]))

    text = section(render_evidence(evidence), "## 指标字段的加工链路")

    assert text.count(f"SUM(CASE WHEN {DWD_ACCT}.is_margin = '0'") == 1
    assert (f"第 1 跳：{CUST_BAL}.dep_bal_rmb ← {DWD_ACCT}.bal_rmb、{DWD_ACCT}.is_margin"
            f"（加工表达式和条件同 {ORG_BAL}.dep_bal_rmb 的第 2 跳）") in text.splitlines()
    assert "同样的一跳只在第一次写全" in text


def test_a_condition_used_in_several_hops_is_written_once_with_a_number(mock_store):
    evidence = make_evidence(mock_store)
    condition = f"WHERE {DWD_ACCT}.data_dt = CAST('${{data_dt}}' AS DATE)"
    first = evidence.lineage[0].steps[0]
    evidence.lineage[0].steps[0] = first.model_copy(update={"filters": [condition]})

    text = section(render_evidence(evidence), "## 指标字段的加工链路")

    assert text.count(condition) == 1
    assert f"〔条件1〕{condition}" in text.splitlines()
    assert text.count("  过滤条件：〔条件1〕") == 2
    assert text.index("几跳共用的条件") < text.index(f"### {ORG_BAL}.dep_bal_rmb")       # 先列共用的，再列各字段


def test_conditions_used_once_or_too_short_stay_in_their_hop(mock_store):
    text = section(render_evidence(make_evidence(mock_store)), "## 指标字段的加工链路")
    assert "几跳共用的条件" not in text and "〔条件" not in text                       # 两跳的条件不一样

    evidence = make_evidence(mock_store)
    evidence.lineage[0].steps = [step.model_copy(update={"filters": ["WHERE a = 1"]}) for step in evidence.lineage[0].steps]
    text = section(render_evidence(evidence), "## 指标字段的加工链路")
    assert "〔条件" not in text and text.count("  过滤条件：WHERE a = 1") == 2         # 太短，编号不省字


def test_the_planner_is_told_how_to_read_the_shortened_lineage():
    assert "同某字段的第 N 跳" in PLANNER_SYSTEM and "几跳共用的条件" in PLANNER_SYSTEM


def test_comparison_section_names_the_three_relations(mock_store):
    evidence = make_evidence(mock_store)
    evidence.comparisons.append(FieldComparison(term="对公存款余额", a=f"{ORG_BAL}.ytd_avg_bal_rmb",
                                                b=f"{CUST_BAL}.ytd_avg_bal_rmb", relation="same_source_same",
                                                common=[f"{DWD_ACCT}.bal_rmb"]))

    text = section(render_evidence(evidence), "## 同源字段对比")

    assert f"〔同源不同口径〕{ORG_BAL}.dep_bal_rmb 与 {ORG_BAL}.dep_bal_tot_rmb" in text
    assert f"共同来源：{CUST_BAL}.dep_bal_rmb" in text and "dep_bal_rmb 只汇总非保证金账户" in text
    assert f"〔加工关系〕{ORG_BAL}.dep_bal_rmb 由 {CUST_BAL}.dep_bal_rmb 加工而来" in text
    assert f"〔同源同口径〕{ORG_BAL}.ytd_avg_bal_rmb 与 {CUST_BAL}.ytd_avg_bal_rmb" in text
    assert "指标「对公存款余额」" in text


def test_table_links_and_upstream_options_are_listed(evidence):
    text = render_evidence(evidence)

    assert f"- {ORG_BAL} 由 {CUST_BAL} 加工而来" in section(text, "## 表之间的上下游")
    assert f"- {DWD_ACCT} 对公存款账户日快照（整合层）" in section(text, "## 还可以上溯到的表")


# ── render_evidence：关联路径、样例、说明 ──────

def test_a_direct_join_path_states_keys_cardinality_fan_out_and_origin(evidence):
    text = section(render_evidence(evidence), "## 关联路径")

    assert f"- {ORG_BAL} → {DIM_ORG}：{ORG_BAL}.org_no = {DIM_ORG}.org_no（N:1，不发散；依据：同名粒度键）" in text


def test_a_two_hop_join_path_lists_each_hop_and_the_middle_table(mock_store):
    evidence = make_evidence(mock_store)
    evidence.joins = [JoinHint(left_table=CUST_BAL, right_table=DIM_ORG, fan_out=True, hops=[
        JoinHop(left_table=CUST_BAL, left_columns=["cust_no"], right_table="dim.corp_cust", right_columns=["cust_no"],
                cardinality="N:1", origin="lineage"),
        JoinHop(left_table="dim.corp_cust", left_columns=["org_no", "cust_no"], right_table=DIM_ORG,
                right_columns=["org_no", "x"], cardinality="1:N", origin="manual", note="手工登记"),
    ])]

    text = section(render_evidence(evidence), "## 关联路径")

    assert f"- {CUST_BAL} → {DIM_ORG}（经过 dim.corp_cust；会发散）：" in text
    assert f"1. {CUST_BAL}.cust_no = dim.corp_cust.cust_no（N:1；依据：血缘传递）" in text
    assert "2. dim.corp_cust.org_no = dim.org.org_no AND dim.corp_cust.cust_no = dim.org.x（1:N；依据：手工填写） 手工登记" in text


def test_examples_section_gives_requirement_caliber_tables_and_sql(evidence):
    text = section(render_evidence(evidence), EXAMPLE_SECTION)

    assert f"### 样例 {EXAMPLE_ID}" in text
    assert "需求：2026 年 6 月末各二级分行含保证金存款比年初多了多少" in text
    assert "口径：含保证金用全口径存款余额 dep_bal_tot_rmb；年初指上年末（2025-12-31）" in text
    assert f"用到的表：{ORG_BAL}、{DIM_ORG}" in text
    assert f"```sql\n{EXAMPLE_SQL}\n```" in text


def test_notes_section_lists_the_evidence_notes(evidence):
    assert "- 关键字路：向量器不一致，只走了关键字路" in section(render_evidence(evidence), "## 说明")


def test_render_evidence_is_deterministic(evidence):
    assert render_evidence(evidence) == render_evidence(evidence.model_copy(deep=True))


def test_an_empty_evidence_still_renders(mock_store):
    text = render_evidence(Evidence(use_lineage=False, use_examples=False))

    assert "## 候选表" in text and "（无）" in text


# ── render_evidence：超过上限时按四级收缩 ──────

def heavy_evidence(mock_store) -> Evidence:
    """十三张表、长的码值和条件、长的样例 SQL，每一级收缩都能省下字数

    后十张是机构汇总表的副本，多一个和 org_no 配对的名称字段 org_nm：第 5 张以后的表不收缩时也只列关键字段
    和它们的名称字段，收缩第 2 级再去掉名称字段，这一级才省得下字数。
    """
    evidence = make_evidence(mock_store)
    for number in range(10):
        table = evidence.tables[0].model_copy(deep=True)
        table.table_id = f"ads.extra_{number:02d}"
        next(column for column in table.columns if column.name == "org_no").name_column = "org_nm"
        table.columns.append(EvidenceColumn(name="org_nm", cn="开户机构名称", data_type="varchar(60)"))
        evidence.tables.append(table)
    for table in evidence.tables:
        for column in table.columns:
            if not column.hit_terms and column.name not in table.grain_columns:
                column.enum_values = "1=甲;2=乙;3=丙;" * 12
    step = evidence.lineage[0].steps[0]
    evidence.lineage[0].steps[0] = step.model_copy(update={
        "filters": ["WHERE " + "ads.corp_cust_dep_bal_d.cust_no IS NOT NULL AND " * 8 + "1 = 1"],
        "joins": ["LEFT JOIN dim.corp_cust ON " + "ads.corp_cust_dep_bal_d.cust_no = dim.corp_cust.cust_no AND " * 6 + "1 = 1"],
    })
    evidence.examples[0] = evidence.examples[0].model_copy(update={"sql_text": EXAMPLE_SQL + "\n-- " + "注释" * 900})
    # 收缩的说明里写着上限的数字。各级文本的长度都保持五位数，逐级压低上限时说明才不会因为数字变短而多省一个字
    evidence.notes.append("填充" * 6000)
    return evidence


NOTES = ["去掉非命中字段的码值", "第 9 张以后的表只列命中字段和粒度字段", "每跳的条件截到 120 字", "样例 SQL 截到 1500 字"]


def shrunk_texts(evidence: Evidence, monkeypatch, levels: int = 4) -> list[str]:
    """依次把上限压到刚好装不下上一级的文本，得到不收缩和收缩 1 至 levels 级的文本"""
    monkeypatch.setattr(prompts, "EVIDENCE_CHAR_LIMIT", 10 ** 9)
    texts = [render_evidence(evidence)]
    for _ in range(levels):
        monkeypatch.setattr(prompts, "EVIDENCE_CHAR_LIMIT", len(texts[-1]) - 1)
        texts.append(render_evidence(evidence))
    return texts


def test_a_text_within_the_limit_is_not_shrunk(evidence):
    assert "证据文本超过" not in render_evidence(evidence)


def test_each_shrink_level_makes_the_text_shorter_and_leaves_a_note(mock_store, monkeypatch):
    texts = shrunk_texts(heavy_evidence(mock_store), monkeypatch)

    assert [len(text) for text in texts] == sorted((len(text) for text in texts), reverse=True)
    assert len(set(len(text) for text in texts)) == 5
    for level, text in enumerate(texts):
        for index, note in enumerate(NOTES):
            assert (note in section(text, "## 说明")) == (index < level), (level, note)


def test_level_one_drops_enum_values_of_columns_that_were_not_hit(mock_store, monkeypatch):
    texts = shrunk_texts(heavy_evidence(mock_store), monkeypatch)
    before, after = table_block(texts[0], ORG_BAL), table_block(texts[1], ORG_BAL)

    assert "码值：1=甲;2=乙;3=丙;" in before
    assert "码值：1=甲" not in after
    assert "- dep_bal_rmb 存款余额 numeric(18,2) 〔命中：对公存款余额〕" in after       # 命中字段的信息照旧


def test_level_one_keeps_the_enum_values_of_hit_columns(mock_store, monkeypatch):
    evidence = heavy_evidence(mock_store)
    next(c for c in evidence.tables[0].columns if c.name == "dep_bal_rmb").enum_values = "0=关;1=开"
    texts = shrunk_texts(evidence, monkeypatch)

    assert "码值：0=关;1=开" in table_block(texts[1], ORG_BAL)


def test_level_two_lists_only_hit_and_grain_columns_from_the_ninth_table_on(mock_store, monkeypatch):
    evidence = heavy_evidence(mock_store)
    texts = shrunk_texts(evidence, monkeypatch)
    eighth, ninth = evidence.tables[7].table_id, evidence.tables[8].table_id

    assert "- org_nm " in table_block(texts[2], eighth)                      # 第 8 张照旧：关键字段加名称字段
    kept = table_block(texts[2], ninth)
    assert "- org_nm " not in kept and "- margin_bal_rmb " not in kept
    assert "- dep_bal_rmb " in kept and "- org_no " in kept and "- data_dt " in kept     # 命中、粒度（含数据日期）
    assert "另有 " in kept and " 个字段未列出" in kept
    assert "- org_nm " in table_block(texts[1], ninth)                       # 上一级还列着名称字段


def test_level_three_clips_the_conditions_of_each_hop_to_120_characters(mock_store, monkeypatch):
    texts = shrunk_texts(heavy_evidence(mock_store), monkeypatch)
    lineage_before, lineage_after = section(texts[2], "## 指标字段的加工链路"), section(texts[3], "## 指标字段的加工链路")

    long_lines = [line for line in lineage_before.splitlines() if line.strip().startswith(("过滤条件：", "关联条件："))]
    clipped = [line for line in lineage_after.splitlines() if line.strip().startswith(("过滤条件：", "关联条件："))]
    assert max(len(line) for line in long_lines) > 150
    assert all(len(line.split("：", 1)[1]) <= 120 for line in clipped)
    assert any(line.endswith("…") for line in clipped)


def test_level_four_clips_the_example_sql_to_1500_characters(mock_store, monkeypatch):
    texts = shrunk_texts(heavy_evidence(mock_store), monkeypatch)

    def example_sql(text):
        return re.search(r"```sql\n(.*?)\n```", section(text, EXAMPLE_SECTION), re.S).group(1)

    assert len(example_sql(texts[3])) > 1500
    assert len(example_sql(texts[4])) <= 1500 and example_sql(texts[4]).endswith("…")


def test_shrinking_stops_at_the_first_level_that_fits(mock_store, monkeypatch):
    evidence = heavy_evidence(mock_store)
    texts = shrunk_texts(evidence, monkeypatch)
    monkeypatch.setattr(prompts, "EVIDENCE_CHAR_LIMIT", len(texts[2]))

    notes = section(render_evidence(evidence), "## 说明")

    assert [note in notes for note in NOTES] == [True, True, False, False]       # 收缩两级就装下了，不再往下收


def test_a_really_big_evidence_is_shrunk_under_the_real_limit(mock_store, monkeypatch):
    evidence = heavy_evidence(mock_store)
    for number in range(40):
        table = evidence.tables[0].model_copy(deep=True)
        table.table_id = f"ads.more_{number:02d}"
        for column in table.columns:
            column.description = "说明" * 60
        evidence.tables.append(table)

    monkeypatch.setattr(prompts, "EVIDENCE_CHAR_LIMIT", 10 ** 9)
    unshrunk = render_evidence(evidence)
    monkeypatch.undo()
    text = render_evidence(evidence)

    assert len(unshrunk) > prompts.EVIDENCE_CHAR_LIMIT                     # 不收缩的话超过 4 万字
    assert len(text) < len(unshrunk) and NOTES[0] in section(text, "## 说明")


# ── 第 5 至 9 级：真实库的表字段多、加工脚本长，前四级装不下时接着收缩 ──────────

LONG_FIELD = f"{CUST_BAL}.dep_bal_tot_rmb"


def heaviest_evidence(mock_store) -> Evidence:
    """在 heavy_evidence 上再加：非关键字段的口径说明、长表达式、一个八跳的长链路、一条经过中间表的关联"""
    evidence = heavy_evidence(mock_store)
    for table in evidence.tables:
        for column in table.columns:
            column.description = column.description or "口径说明" * 10
    first = evidence.lineage[0].steps[0]
    evidence.lineage[0].steps[0] = first.model_copy(update={"expression": "COALESCE(" + "x + " * 80 + "0)"})
    evidence.lineage.append(FieldLineage(field=LONG_FIELD, term="对公存款余额", steps=[
        LineageStep(depth=min(number, 3), target=f"{DWD_ACCT}.f{number}", sources=[f"{DWD_ACCT}.g{number}"],
                    expression="CASE WHEN " + "a = 1 AND " * 12 + "1 = 1 THEN 1 END",
                    filters=[f"WHERE {DWD_ACCT}.data_dt = CAST('${{data_dt}}' AS DATE)"], script_name="x.sql")
        for number in range(1, 9)]))
    evidence.joins.append(JoinHint(left_table=CUST_BAL, right_table=DIM_ORG, fan_out=False, hops=[
        JoinHop(left_table=CUST_BAL, left_columns=["org_no"], right_table=ORG_BAL, right_columns=["org_no"],
                cardinality="N:N", origin="grain_key"),
        JoinHop(left_table=ORG_BAL, left_columns=["org_no"], right_table=DIM_ORG, right_columns=["org_no"],
                cardinality="N:1", origin="grain_key")]))
    return evidence


def lineage_of(text: str, field: str) -> str:
    """加工链路一节里一个字段的那一段"""
    lineage = section(text, "## 指标字段的加工链路")
    start = lineage.index(f"### {field}（")
    end = lineage.find("\n### ", start + 1)
    return lineage[start:end] if end != -1 else lineage[start:]


def test_levels_five_to_nine_each_make_the_text_shorter_and_leave_a_note(mock_store, monkeypatch):
    texts = shrunk_texts(heaviest_evidence(mock_store), monkeypatch, levels=len(prompts.SHRINK_ACTIONS))

    assert len(prompts.SHRINK_ACTIONS) == 9
    assert all(len(longer) > len(shorter) for longer, shorter in zip(texts, texts[1:]))
    for level, text in enumerate(texts):
        notes = section(text, "## 说明")
        assert [action in notes for action in prompts.SHRINK_ACTIONS] == [index < level for index in range(9)]


def test_level_five_keeps_descriptions_of_key_columns_only(mock_store, monkeypatch):
    texts = shrunk_texts(heaviest_evidence(mock_store), monkeypatch, levels=5)
    before, after = table_block(texts[4], ORG_BAL), table_block(texts[5], ORG_BAL)

    def line(block, name):
        return next(row for row in block.splitlines() if row.startswith(f"- {name} "))

    assert "口径：" in line(before, "margin_bal_rmb") and "口径：" not in line(after, "margin_bal_rmb")
    assert "口径：" in line(after, "dep_bal_rmb")                     # 命中
    assert "口径：" in line(after, "org_no") and "口径：" in line(after, "data_dt")   # 粒度、数据日期


def test_level_six_clips_expressions_to_200_and_conditions_to_60_characters(mock_store, monkeypatch):
    texts = shrunk_texts(heaviest_evidence(mock_store), monkeypatch, levels=6)
    lineage = section(texts[6], "## 指标字段的加工链路")

    expressions = [row.split("：", 1)[1] for row in lineage.splitlines() if row.strip().startswith("加工表达式：")]
    conditions = [row.split("：", 1)[1] for row in lineage.splitlines()
                  if row.strip().startswith(("过滤条件：", "关联条件："))]
    assert max(len(text) for text in expressions) <= 200 and any(text.endswith("…") for text in expressions)
    assert max(len(text) for text in conditions) <= 60
    assert max(len(row) for row in section(texts[5], "## 指标字段的加工链路").splitlines()
               if row.strip().startswith("加工表达式：")) > 200


def test_level_seven_lists_only_key_columns_from_the_fourth_table_on(mock_store, monkeypatch):
    evidence = heaviest_evidence(mock_store)
    texts = shrunk_texts(evidence, monkeypatch, levels=7)
    third, fourth = evidence.tables[2].table_id, evidence.tables[3].table_id

    assert "另有 " not in table_block(texts[7], third)                  # 前三张照旧全列
    kept = table_block(texts[7], fourth)
    assert "- margin_bal_rmb " not in kept and "- dep_bal_rmb " in kept and "另有 " in kept
    assert "- margin_bal_rmb " in table_block(texts[6], fourth)


def test_level_eight_limits_the_lineage_of_each_field_but_keeps_the_first_hop(mock_store, monkeypatch):
    texts = shrunk_texts(heaviest_evidence(mock_store), monkeypatch, levels=8)
    before, after = lineage_of(texts[7], LONG_FIELD), lineage_of(texts[8], LONG_FIELD)

    assert before.count("跳：") == 8 and "后面的几跳没有列出" not in before
    assert 1 <= after.count("跳：") < 8 and "后面的几跳没有列出" in after
    hops = after.split("\n（链路较长")[0]
    assert len(hops) - len(hops.split("\n", 1)[0]) <= prompts.LINEAGE_FIELD_CHARS + 1


def test_level_eight_still_writes_a_first_hop_longer_than_the_budget(mock_store, monkeypatch):
    evidence = heaviest_evidence(mock_store)
    monkeypatch.setattr(prompts, "LINEAGE_FIELD_CHARS", 10)
    texts = shrunk_texts(evidence, monkeypatch, levels=8)

    assert lineage_of(texts[8], LONG_FIELD).count("跳：") == 1


def test_level_nine_lists_only_direct_joins(mock_store, monkeypatch):
    texts = shrunk_texts(heaviest_evidence(mock_store), monkeypatch, levels=9)

    assert "经过 " in section(texts[8], "## 关联路径")
    joins = section(texts[9], "## 关联路径")
    assert "经过 " not in joins and f"- {ORG_BAL} → {DIM_ORG}：" in joins


# ── render_tables ─────────────────────────────

def test_render_tables_lists_only_the_given_tables_in_the_given_order(evidence):
    text = render_tables(evidence, [DIM_ORG, ORG_BAL])

    assert text.index(f"### {DIM_ORG} ") < text.index(f"### {ORG_BAL} ")
    assert f"### {CUST_BAL} " not in text and not text.startswith("## ")


def test_render_tables_points_only_to_code_tables_among_the_given_tables(evidence):
    copy = evidence.model_copy(deep=True)
    next(column for column in copy.tables[0].columns if column.name == "org_no").code_table = f"{DIM_ORG}.org_nm"

    assert "码值表" not in render_tables(copy, [ORG_BAL])
    assert f"码值表：{DIM_ORG}.org_nm" in render_tables(copy, [ORG_BAL, DIM_ORG])


def test_render_tables_skips_tables_outside_the_evidence(evidence):
    text = render_tables(evidence, ["ads.no_such_table", DIM_ORG])

    assert "no_such_table" not in text and f"### {DIM_ORG} " in text


def test_render_tables_uses_the_same_block_as_the_candidate_section(evidence):
    block = table_block(render_evidence(evidence), ORG_BAL)

    assert render_tables(evidence, [ORG_BAL]).strip() == block.strip()


def test_render_tables_is_never_shrunk(mock_store, monkeypatch):
    evidence = heavy_evidence(mock_store)
    monkeypatch.setattr(prompts, "EVIDENCE_CHAR_LIMIT", 1)

    text = render_tables(evidence, [evidence.tables[9].table_id])

    assert "- margin_bal_rmb " in text and "码值：1=甲" in text


def test_render_tables_of_nothing_is_empty(evidence):
    assert render_tables(evidence, []) == "" and render_tables(evidence, ["x.y"]) == ""
