"""① 需求解析：核对规则（每条有正例和反例）、检索词转换、带重试的解析"""
from datetime import date

import pytest

from nl2sql.llm import ScriptedLLM, StepError
from nl2sql.parser import check_parsed, parse_request, to_recall_query
from nl2sql.retriever import RecallQuery
from nl2sql.schemas import FilterItem, Metric, ParsedRequest, QueryRequest, TimeSpec
from tests.step_fixtures import make_parsed

TODAY = date(2026, 10, 13)
REQUEST = QueryRequest(question="查 2026 年 9 月末各一级分行不含保证金的对公存款余额")


# ── check_parsed：是查数需求时 ────────────────

def test_a_complete_query_passes():
    assert check_parsed(make_parsed()) == []


def test_a_query_needs_a_requirement():
    problems = check_parsed(make_parsed(requirement="  "))

    assert len(problems) == 1 and "requirement" in problems[0]


@pytest.mark.parametrize("metrics", [[], [Metric(name="  ")]])
def test_a_query_needs_at_least_one_named_metric(metrics):
    problems = check_parsed(make_parsed(metrics=metrics))

    assert len(problems) == 1
    assert "metrics" in problems[0] and "is_query" in problems[0]       # 告诉模型：说不出指标就不是查数需求


def test_problems_add_up():
    problems = check_parsed(make_parsed(requirement="", metrics=[], time=TimeSpec(kind="point")))

    assert len(problems) == 3


@pytest.mark.parametrize("bad", ["2026-9-30", "2026/09/30", "20260930", "2026-02-30", "9月30日", ""])
def test_dates_must_be_valid_iso_dates(bad):
    problems = check_parsed(make_parsed(time=TimeSpec(kind="point", dates=[bad])))

    assert len(problems) == 1
    assert "time.dates[0]" in problems[0] and "YYYY-MM-DD" in problems[0] and bad in problems[0]


def test_compare_dates_must_be_valid_iso_dates():
    time = TimeSpec(kind="point", dates=["2026-09-30"], compare="prev_year_end", compare_dates=["2025-12-32"])

    problems = check_parsed(make_parsed(time=time))

    assert len(problems) == 1 and "time.compare_dates[0]" in problems[0] and "2025-12-32" in problems[0]


@pytest.mark.parametrize("time", [
    TimeSpec(kind="none"),
    TimeSpec(kind="point", dates=["2026-09-30"]),
    TimeSpec(kind="daily_avg", dates=["2026-09-30"]),
    TimeSpec(kind="range", dates=["2026-01-01", "2026-09-30"]),
    TimeSpec(kind="range", dates=["2026-09-30", "2026-09-30"]),          # 起止同一天也行
    TimeSpec(kind="point", dates=["2024-02-29"]),                         # 闰日
])
def test_the_right_number_of_dates_passes(time):
    assert check_parsed(make_parsed(time=time)) == []


@pytest.mark.parametrize("kind, dates, expected", [
    ("point", [], "1 个"),
    ("point", ["2026-09-30", "2026-08-31"], "1 个"),
    ("daily_avg", [], "1 个"),
    ("daily_avg", ["2026-01-01", "2026-09-30"], "1 个"),
    ("range", ["2026-01-01"], "2 个"),
    ("range", ["2026-01-01", "2026-06-30", "2026-12-31"], "2 个"),
    ("none", ["2026-09-30"], "为空"),
])
def test_the_wrong_number_of_dates_is_reported_with_the_fix(kind, dates, expected):
    problems = check_parsed(make_parsed(time=TimeSpec(kind=kind, dates=dates)))

    assert len(problems) == 1
    assert "time.dates" in problems[0] and kind in problems[0] and expected in problems[0]


def test_a_range_must_not_start_after_it_ends():
    problems = check_parsed(make_parsed(time=TimeSpec(kind="range", dates=["2026-09-30", "2026-01-01"])))

    assert len(problems) == 1 and "起" in problems[0] and "止" in problems[0] and "2026-09-30" in problems[0]


@pytest.mark.parametrize("compare", ["prev_year_end", "prev_month_end", "other"])
def test_a_comparison_needs_compare_dates(compare):
    problems = check_parsed(make_parsed(time=TimeSpec(kind="point", dates=["2026-09-30"], compare=compare)))

    assert len(problems) == 1 and "compare_dates" in problems[0] and compare in problems[0]


@pytest.mark.parametrize("compare, compare_dates", [
    ("prev_year_end", ["2025-12-31"]),
    ("prev_month_end", ["2026-08-31"]),
    ("other", ["2025-09-30"]),
    ("none", []),
])
def test_a_comparison_with_compare_dates_passes(compare, compare_dates):
    time = TimeSpec(kind="point", dates=["2026-09-30"], compare=compare, compare_dates=compare_dates)

    assert check_parsed(make_parsed(time=time)) == []


# ── check_parsed：不是查数需求时 ──────────────

def test_a_non_query_needs_a_reply():
    problems = check_parsed(ParsedRequest(is_query=False))

    assert len(problems) == 1 and "reply" in problems[0]


def test_a_non_query_with_a_reply_passes_without_any_other_field():
    assert check_parsed(ParsedRequest(is_query=False, reply="我可以把查数需求转成 SQL，请告诉我想查哪个指标")) == []


def test_a_non_query_skips_the_query_checks():
    parsed = ParsedRequest(is_query=False, reply="你好", time=TimeSpec(kind="point", dates=["乱写的"]))

    assert check_parsed(parsed) == []


# ── to_recall_query ───────────────────────────

def test_to_recall_query_collects_metrics_entities_dimensions_and_filters():
    parsed = make_parsed(
        metrics=[Metric(name="立项项目数", entity="项目"), Metric(name="完成验收项目数", entity="项目")],
        dimensions=["一级分行名称", "行业"],
        filters=[FilterItem(field="项目开始日期", condition="在 2026 年内"), FilterItem(field="行业", condition="= 制造业")],
    )

    query = to_recall_query(parsed)

    assert query == RecallQuery(requirement=parsed.requirement, metrics=["立项项目数", "完成验收项目数", "项目"],
                                dimensions=["一级分行名称", "行业"], filters=["项目开始日期", "行业"])


def test_to_recall_query_appends_the_extra_terms_to_the_metrics():
    parsed = make_parsed(metrics=[Metric(name="对公存款余额")])

    query = to_recall_query(parsed, ["币种", "是否保证金"])

    assert query.metrics == ["对公存款余额", "币种", "是否保证金"]
    assert query.dimensions == ["一级分行名称"] and query.filters == []


def test_to_recall_query_strips_drops_blanks_and_dedupes_in_order():
    parsed = make_parsed(
        metrics=[Metric(name=" 客户数 ", entity="客户"), Metric(name="客户数", entity=" 客户"), Metric(name="", entity="")],
        dimensions=["机构", " 机构", "", "行业"],
        filters=[FilterItem(field="机构号", condition="= 1101"), FilterItem(field=" 机构号 ")],
    )

    query = to_recall_query(parsed, ["客户", "币种", "  ", "币种"])

    assert query.metrics == ["客户数", "客户", "币种"]
    assert query.dimensions == ["机构", "行业"]
    assert query.filters == ["机构号"]


def test_to_recall_query_without_a_count_entity_adds_nothing_extra():
    assert to_recall_query(make_parsed()).metrics == ["对公存款余额"]


# ── parse_request ─────────────────────────────

def test_parse_request_returns_the_parsed_result_with_one_record():
    llm = ScriptedLLM([make_parsed().model_dump_json()])

    parsed, records = parse_request(llm, REQUEST, TODAY)

    assert parsed == make_parsed()
    assert [(r.step, r.attempt, r.ok) for r in records] == [("parse", 1, True)]
    assert llm.calls[0]["json_mode"] is True
    system, user = llm.calls[0]["messages"]
    assert system["role"] == "system" and user["role"] == "user"
    assert "2026-10-13" in user["content"] and REQUEST.question in user["content"]


def test_parse_request_uses_the_given_today_not_the_request_today():
    request = REQUEST.model_copy(update={"today": date(2020, 1, 1)})
    llm = ScriptedLLM([make_parsed().model_dump_json()])

    parse_request(llm, request, TODAY)

    assert "2026-10-13" in llm.calls[0]["messages"][1]["content"]


def test_parse_request_carries_the_previous_round_into_the_messages():
    request = QueryRequest(question="按一级分行拆开", prev_requirement="查询 2026-09-30 全行对公存款余额")
    llm = ScriptedLLM([make_parsed().model_dump_json()])

    parse_request(llm, request, TODAY)

    user = llm.calls[0]["messages"][1]["content"]
    assert "查询 2026-09-30 全行对公存款余额" in user and "按一级分行拆开" in user


def test_parse_request_retries_when_the_check_finds_problems():
    bad = make_parsed(time=TimeSpec(kind="point", dates=[]))
    llm = ScriptedLLM([bad.model_dump_json(), make_parsed().model_dump_json()])

    parsed, records = parse_request(llm, REQUEST, TODAY)

    assert parsed == make_parsed()
    assert [(r.attempt, r.ok) for r in records] == [(1, False), (2, True)]
    retry = llm.calls[1]["messages"]
    assert retry[-2] == {"role": "assistant", "content": bad.model_dump_json()}
    assert "time.dates" in retry[-1]["content"]                 # 带着具体的问题，模型才知道改哪里


def test_parse_request_retries_after_a_broken_reply():
    llm = ScriptedLLM(['{"is_query": tr', make_parsed().model_dump_json()])

    parsed, records = parse_request(llm, REQUEST, TODAY)

    assert parsed.is_query and [r.ok for r in records] == [False, True]
    assert "不是合法的 JSON" in records[0].error


def test_parse_request_retries_after_a_wrong_shape():
    wrong = '{"is_query": true, "requirement": "x", "metrics": ["对公存款余额"]}'        # 指标应该是对象
    llm = ScriptedLLM([wrong, make_parsed().model_dump_json()])

    _, records = parse_request(llm, REQUEST, TODAY)

    assert records[0].error.startswith("JSON 的结构不对：metrics.0")


def test_parse_request_gives_up_after_two_bad_replies():
    llm = ScriptedLLM(['{"is_query": true}', '{"is_query": true}'])

    with pytest.raises(StepError) as caught:
        parse_request(llm, REQUEST, TODAY)

    assert caught.value.step == "parse"
    assert caught.value.message.startswith("输出两次都不合格：")
    assert [(r.attempt, r.ok) for r in caught.value.calls] == [(1, False), (2, False)]


def test_parse_request_turns_model_errors_into_step_errors():
    llm = ScriptedLLM([TimeoutError("连接超时")])

    with pytest.raises(StepError) as caught:
        parse_request(llm, REQUEST, TODAY)

    assert caught.value.step == "parse" and caught.value.message.startswith("模型调用失败：")


def test_parse_request_accepts_a_non_query_answer():
    reply = ParsedRequest(is_query=False, reply="我可以把专题库里的查数需求转成 SQL")
    llm = ScriptedLLM([reply.model_dump_json()])

    parsed, records = parse_request(llm, QueryRequest(question="你好"), TODAY)

    assert not parsed.is_query and parsed.reply == reply.reply and len(records) == 1
