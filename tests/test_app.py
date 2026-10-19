"""界面（Streamlit AppTest）：查数、反问点选、追问与新话题、元数据页、取不到服务时的提示

不起浏览器：AppTest 在进程里直接运行 app.py。流水线和元数据通过替换 nl2sql.service 的两个函数换成假的。
"""
from datetime import date
from pathlib import Path

from streamlit.testing.v1 import AppTest

from nl2sql import service
from nl2sql.schemas import (
    Clarify,
    ClarifyOption,
    ExecSummary,
    ParsedRequest,
    QueryPlan,
    QueryResult,
    TraceStep,
    ValidationSummary,
)

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
SQL = "SELECT\n  SUM(loan_bal_rmb) AS loan_bal_rmb\nFROM ads.corp_cust_loan_bal_d\nWHERE\n  data_dt = CAST('2026-09-30' AS DATE)"
NO_METADATA = "元数据库不存在: x。请先运行 python -m metadata.importer x 导入资料"


def steps() -> list[TraceStep]:
    return [TraceStep(step="parse", label="① 需求解析", elapsed_ms=8, summary="完整需求：查询余额"),
            TraceStep(step="output", label="⑦ 输出", elapsed_ms=2, summary="状态 ok；已执行，返回 1 行")]


def ok_result(requirement: str = "查询 2026-09-30 全行对公贷款余额") -> QueryResult:
    """一个完整的成功结果：SQL、各项说明、假设、参考样例、校验警告、执行结果"""
    plan = QueryPlan(decision="ok", caliber="loan_bal_rmb 是全部对公贷款的余额", table_basis="只到全行，用客户汇总表",
                     join_basis="不需要关联", lineage_note="没有上溯", examples_used=["02_贷款余额.sql#2"])
    return QueryResult(
        status="ok", question="查 9 月末的对公贷款余额", requirement=requirement, sql=SQL, plan=plan,
        assumptions=["余额按人民币折算"], validation=ValidationSummary(ok=True, warnings=["R8：查询方案里的字段没有用到：x"]),
        exec_result=ExecSummary(executed=True, columns=["loan_bal_rmb"], rows=[[1388103204.52]], row_count=1),
        trace=steps(), elapsed_ms=1500)


def clarify_result() -> QueryResult:
    """④ 反问：两个选项"""
    clarify = Clarify(question="贷款余额要不要含票据贴现？", options=[
        ClarifyOption(label="含票据贴现", meaning="用全口径余额"), ClarifyOption(label="不含票据贴现", meaning="用贷款余额")])
    return QueryResult(status="need_clarify", question="查对公贷款余额", requirement="查询全行对公贷款余额",
                       plan=QueryPlan(decision="need_clarify", clarify=clarify), clarify=clarify, trace=steps())


class FakePipeline:
    """假流水线：记下每次收到的请求，按顺序给出预先写好的结果"""

    def __init__(self, results) -> None:
        self._results = list(results)
        self.requests = []

    def stream(self, request):
        self.requests.append(request)
        result = self._results.pop(0)
        yield from result.trace
        yield result


def open_app(monkeypatch, pipeline=None, metadata=None) -> AppTest:
    """换掉 service 里取流水线和元数据的函数，打开界面并运行一次"""
    fake = pipeline if pipeline is not None else FakePipeline([])
    monkeypatch.setattr(service, "get_pipeline", lambda: fake)
    if metadata is not None:
        monkeypatch.setattr(service, "get_metadata", lambda: metadata)
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    assert not at.exception
    return at


def ask(at: AppTest, question: str) -> None:
    at.chat_input[0].set_value(question).run()
    assert not at.exception


def clarify_buttons(at: AppTest) -> list[str]:
    return [button.label for button in at.button if str(button.key).startswith("clarify_")]


# ── 查数 ──────────────────────────────────────────

def test_asking_shows_progress_then_the_sql_and_the_explanations(monkeypatch):
    result = ok_result()
    pipeline = FakePipeline([result])
    at = open_app(monkeypatch, pipeline)

    ask(at, "查 9 月末的对公贷款余额")

    request = pipeline.requests[0]
    assert (request.question, request.prev_requirement, request.prev_clarify) == ("查 9 月末的对公贷款余额", "", None)
    assert request.today == date.today() and request.use_lineage and request.use_examples
    assert [code.value for code in at.code] == [SQL] and at.code[0].language == "sql"
    assert "① 需求解析：完整需求：查询余额（8 ms）" in [text.value for text in at.text]      # 状态框里逐步写出「标签：摘要」
    markdown = "\n".join(item.value for item in at.markdown)
    for expected in ("完整需求", result.requirement, "口径说明", "全部对公贷款的余额", "用表依据", "关联说明", "溯源说明",
                     "假设", "余额按人民币折算", "参考样例", "02_贷款余额.sql#2"):
        assert expected in markdown
    assert [item.value for item in at.success] == ["已生成 SQL，静态校验通过"]
    assert len(at.warning) == 1 and "静态校验警告" in at.warning[0].value and "R8" in at.warning[0].value
    assert not at.error                                       # 没有错误：错误和警告是分开的
    assert len(at.dataframe) == 1 and at.dataframe[0].value.shape == (1, 1)
    assert at.session_state["prev_requirement"] == result.requirement


def test_clicking_a_clarify_option_answers_it_with_the_previous_turn(monkeypatch):
    first, second = clarify_result(), ok_result("查询 2026-09-30 全行不含票据贴现的对公贷款余额")
    pipeline = FakePipeline([first, second])
    at = open_app(monkeypatch, pipeline)

    ask(at, "查对公贷款余额")

    assert clarify_buttons(at) == ["含票据贴现", "不含票据贴现"] and not at.code     # 问题和选项都在，还没有 SQL
    assert "贷款余额要不要含票据贴现" in "\n".join(item.value for item in at.markdown)
    assert at.session_state["prev_clarify"] == first.clarify
    at.button(key="clarify_0_1").click().run()

    assert not at.exception
    answer = pipeline.requests[1]
    assert answer.question == "不含票据贴现"                    # 选项文字就是本轮的提问
    assert answer.prev_requirement == first.requirement and answer.prev_clarify == first.clarify
    assert [code.value for code in at.code] == [SQL]
    assert clarify_buttons(at) == []                          # 已经回答过的反问不能再点
    assert at.session_state["prev_clarify"] is None and at.session_state["prev_requirement"] == second.requirement


def test_a_clarify_from_the_parser_asks_to_confirm_the_requirement_and_typing_answers_it(monkeypatch):
    clarify = Clarify(question="要看哪个客户的贷款情况？", options=[ClarifyOption(label="列出全部客户"),
                                                              ClarifyOption(label="指定一个客户")])
    first = QueryResult(status="need_clarify", question="看一下某个客户的贷款情况", requirement="查询某个对公客户的贷款情况",
                        clarify=clarify, trace=steps())                     # ① 问的：还没有方案
    pipeline = FakePipeline([first, ok_result()])
    at = open_app(monkeypatch, pipeline)

    ask(at, "看一下某个客户的贷款情况")

    assert [item.value for item in at.info] == ["需要先确认需求"] and clarify_buttons(at) == ["列出全部客户", "指定一个客户"]
    ask(at, "客户 C000015")                                                  # 不点选项，直接输入回答
    assert pipeline.requests[1].prev_clarify == clarify


def test_follow_up_carries_the_previous_requirement_until_a_new_topic(monkeypatch):
    pipeline = FakePipeline([ok_result("需求一"), ok_result("需求二"), ok_result("需求三")])
    at = open_app(monkeypatch, pipeline)

    ask(at, "第一问")
    ask(at, "追问一句")
    assert pipeline.requests[1].prev_requirement == "需求一"            # 追问：带着上一轮的完整需求
    assert len(at.session_state["turns"]) == 2

    at.button(key="new_topic").click().run()

    assert not at.exception
    assert at.session_state["turns"] == [] and at.session_state["prev_requirement"] == ""
    assert at.session_state["prev_clarify"] is None and not at.code
    ask(at, "换个话题")
    assert pipeline.requests[2].prev_requirement == "" and pipeline.requests[2].prev_clarify is None


def test_failed_turns_say_where_they_stopped_and_only_a_parsed_turn_replaces_the_context(monkeypatch):
    not_a_query = QueryResult(status="failed", question="你好", failed_step="parse", message="我是查数工具，请说要查什么",
                              parsed=ParsedRequest(is_query=False, reply="我是查数工具，请说要查什么"), trace=steps())
    cannot = QueryResult(status="failed", question="查还款计划", requirement="查询还款计划", failed_step="plan",
                         message="元数据里没有还款计划", trace=steps())
    pipeline = FakePipeline([ok_result("需求一"), not_a_query, cannot])
    at = open_app(monkeypatch, pipeline)

    ask(at, "第一问")
    ask(at, "你好")
    assert [item.value for item in at.info] == ["我是查数工具，请说要查什么"] and not at.error     # 不是查数需求：不算错误
    assert at.session_state["prev_requirement"] == "需求一"                                      # 上下文还在
    ask(at, "查还款计划")

    assert "④ 选表定口径" in at.error[0].value and "元数据里没有还款计划" in at.error[0].value
    assert at.session_state["prev_requirement"] == "查询还款计划"                                 # 解析成功的轮次更新上下文


# ── 元数据 ────────────────────────────────────────

def test_metadata_page_shows_the_lineage_text_after_picking_a_field(monkeypatch, mock_store, mock_graph):
    at = open_app(monkeypatch, metadata=(mock_store, mock_graph))
    at.radio(key="page").set_value("元数据").run()

    assert not at.exception and at.selectbox(key="meta_layer").value == "专题层" and len(at.dataframe) == 1
    assert not at.code                                                  # 还没有选字段，没有链路
    at.selectbox(key="meta_table_专题层").select("ads.org_corp_dep_bal_d").run()
    assert len(at.dataframe) == 2 and not at.code                       # 表清单、字段清单
    at.selectbox(key="meta_column_ads.org_corp_dep_bal_d").select("dep_bal_rmb").run()

    assert not at.exception
    upstream, downstream = [item.value for item in at.code]
    lines = upstream.splitlines()
    assert lines[0].startswith("ads.org_corp_dep_bal_d.dep_bal_rmb（")          # 起点带中文名
    assert lines[1].startswith("  ← ads.corp_cust_dep_bal_d.dep_bal_rmb（")      # 第一层缩进一级
    assert lines[2] == "      表达式：SUM(ads.corp_cust_dep_bal_d.dep_bal_rmb)"       # 每一跳下面写加工表达式
    assert lines[3].startswith("      过滤：WHERE ads.corp_cust_dep_bal_d.data_dt")       # 和过滤条件
    assert lines[4].startswith("    ← dwd.corp_dep_acct_d.bal_rmb（")                       # 第二层再缩进一级
    assert lines[5].startswith("        表达式：SUM(CASE WHEN ")
    assert "        关联：JOIN dim.corp_cust ON dwd.corp_dep_acct_d.cust_no = dim.corp_cust.cust_no" in lines
    assert sum(line.strip() == "加工同上一条" for line in lines) >= 1      # 同一个字段的几个来源共用一套加工，只写一遍
    assert "（没有下游）" in downstream
    at.selectbox(key="meta_column_ads.org_corp_dep_bal_d").select("org_no").run()
    assert at.code[0].value.splitlines()[0].startswith("ads.org_corp_dep_bal_d.org_no（")     # 换字段后链路跟着换


# ── 取不到服务时 ──────────────────────────────────

def test_unavailable_services_show_the_reason_and_the_import_command_instead_of_a_traceback(monkeypatch):
    def missing():
        raise FileNotFoundError(NO_METADATA)

    monkeypatch.setattr(service, "get_pipeline", missing)
    monkeypatch.setattr(service, "get_metadata", missing)
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()

    for page in (None, "元数据"):
        if page is not None:
            at.radio(key="page").set_value(page).run()
        assert not at.exception
        assert "元数据库不存在" in at.error[0].value
        assert [code.value for code in at.code] == ["python -m metadata.importer"]
        assert not at.chat_input
