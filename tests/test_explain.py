"""上一轮选表的代码解释：只读依据、名字解析、阶段和可追溯的诊断"""
from unittest.mock import Mock

import pytest

from lineage.graph import LineageGraph
from metadata.models import ColumnMeta, Example, LineageEdge, Mart, Relation, ScriptStatement, TableMeta
from metadata.store import MetadataStore
from nl2sql.explain import (
    MAX_SIMILAR,
    TableMatch,
    diagnose_unresolved,
    explain_previous,
    is_table_name,
    resolve_table,
    same_name_note,
)
from nl2sql.retriever import Retriever, TableLocation
from nl2sql.schemas import ExecSummary, Metric, ParsedRequest, PlanTable, QueryPlan, QueryResult, RoundBasis, ValidationSummary


@pytest.fixture
def parts() -> tuple[MetadataStore, Retriever]:
    """虚构资料包含重名、非标准表、下层表和字段缺项"""
    rows = [("ads.demo_used", "经费月汇总", "专题层", True),
            ("ads.demo_fund", "经费历史表", "专题层", False),
            ("dws.demo_fund", "经费历史表", "汇总层", True),
            ("ads.demo_other", "其它统计", "专题层", True),
            ("dwd.demo_detail", "经费流水明细", "标准层", True),
            ("ads.demo_same", "同名统计", "专题层", True),
            ("dm.demo_same", "同名统计", "专题层", True),
            ("ads.demo_tiered", "分层统计", "专题层", True),
            ("dws.demo_tiered", "分层统计", "汇总层", True)]
    tables = [TableMeta(table_id=table, table_name=table.split(".")[1], schema_name=table.split(".")[0],
                        table_cn=cn, layer=layer, standard=standard, table_type="流水表", grain_columns=["month"])
              for table, cn, layer, standard in rows]
    columns = [ColumnMeta(table_id=table.table_id, column_name="amount", column_cn="经费支出", ordinal=1)
               for table in tables if table.table_id != "ads.demo_other"]
    store = MetadataStore.from_mart(Mart(tables=tables, columns=columns))
    return store, Retriever(store, LineageGraph(store), None)


def previous(**changes: object) -> RoundBasis:
    """上一轮用汇总表查经费，另有一张候选和一张精简掉的表"""
    basis = RoundBasis(requirement="查询各月经费支出", parsed=ParsedRequest(is_query=True,
                       requirement="查询各月经费支出", metrics=[Metric(name="经费支出")]),
                       plan=QueryPlan(decision="ok", tables=[PlanTable(table="ads.demo_used", reason="已汇总到月")],
                                      table_basis="按月汇总", caliber="支出金额求和", dropped_tables=["dws.demo_fund"]),
                       sql_tables=["ads.demo_used"], candidate_tables=["ads.demo_used", "ads.demo_fund"])
    return basis.model_copy(update=changes)


@pytest.mark.parametrize("name", ['ads.demo_used', 'ADS.DEMO_USED', 'demo_used', '经费月汇总',
                                  ' 「 ADS.DEMO_USED 」 ', '`ads.demo_used`表', 'ads.demo_used表',
                                  'ＡＤＳ．ＤＥＭＯ＿ＵＳＥＤ', "'ads.demo_used'"])
def test_resolve_unique(parts, name: str) -> None:
    """全名、简称、中文名和归一化写法都对上同一张表"""
    assert resolve_table(parts[0], name).table_id == "ads.demo_used"


@pytest.mark.parametrize("name", ["demo_same", "同名统计"])
def test_resolve_ambiguous(parts, name: str) -> None:
    """同层的标准表重名时分不出先后，要求模式名"""
    match = resolve_table(parts[0], name)
    assert match.ambiguous and not match.table_id
    assert match.similar == ("ads.demo_same", "dm.demo_same")


@pytest.mark.parametrize("name, table_id, others", [
    ("demo_fund", "dws.demo_fund", ("ads.demo_fund",)), ("经费历史表", "dws.demo_fund", ("ads.demo_fund",)),
    ("demo_tiered", "ads.demo_tiered", ("dws.demo_tiered",)),
])
def test_resolve_same_name_prefers_standard_then_upper_layer(parts, name: str, table_id: str, others: tuple) -> None:
    """重名时标准表优先，再按层级从上往下，其余同名的表留着说明"""
    match = resolve_table(parts[0], name)
    assert (match.table_id, match.similar, match.ambiguous) == (table_id, others, False)
    assert table_id in same_name_note(name, match) and others[0] in same_name_note(name, match)


def test_same_name_is_stated_in_the_diagnosis(parts) -> None:
    """按重名规则取的表，诊断里写明取了哪张"""
    store, retriever = parts
    diagnosis = explain_previous(store, retriever, previous(), ["demo_tiered"]).named[0]
    assert diagnosis.table_id == "ads.demo_tiered" and "有同名的表 dws.demo_tiered" in diagnosis.reasons[0]


def test_loose_match_needs_exactly_one_chinese_name(parts) -> None:
    """只有用户明确在说这张表时才放宽：中文叫法只被一张表包含才算对上"""
    store, _ = parts
    assert resolve_table(store, "流水明细", loose=True).table_id == "dwd.demo_detail"
    assert not resolve_table(store, "流水明细").table_id
    assert not resolve_table(store, "经费", loose=True).table_id
    assert resolve_table(store, "「」") == TableMatch()


def test_resolve_similar(parts) -> None:
    """相近表数量有上限，包含的中文名优先，同分按标识排"""
    store, _ = parts
    assert resolve_table(store, "demo_use").similar[0] == "ads.demo_used"
    assert resolve_table(store, "经费历史").similar[:2] == ("ads.demo_fund", "dws.demo_fund")
    assert len(resolve_table(store, "不存在").similar) <= MAX_SIMILAR


@pytest.mark.parametrize("name, expected", [("ads.demo_used", True), ('`demo_used`表', True),
                                             ("demo-xxx", False), ("经费看板", False), ("", False)])
def test_english_table_name(name: str, expected: bool) -> None:
    """只有英文、数字、下划线和点组成的写法才按英文表名处理"""
    assert is_table_name(name) is expected


@pytest.mark.parametrize("name, changes, stage", [
    ("demo_absent", {}, "unknown"), ("demo_same", {}, "ambiguous"),
    ("ads.demo_used", {"plan": None}, "no_previous"), ("ads.demo_used", {}, "used"),
    ("dws.demo_fund", {}, "pruned"), ("ads.demo_fund", {}, "not_chosen"),
    ("ads.demo_other", {}, "not_recalled"), ("dwd.demo_detail", {}, "outside"),
])
def test_eight_stages(parts, name: str, changes: dict, stage: str) -> None:
    """八种阶段都有确定的记录或元数据依据"""
    store, retriever = parts
    diagnosis = explain_previous(store, retriever, previous(**changes), [name]).named[0]
    assert diagnosis.stage == stage and diagnosis.conclusion
    assert diagnosis.switchable == (stage not in ("used", "unknown", "ambiguous"))
    if stage in ("pruned", "not_chosen", "not_recalled", "outside"):
        assert diagnosis.matches
    if stage == "used":
        assert diagnosis.reasons == ["已汇总到月"]
    if diagnosis.table_id:
        assert diagnosis.grain_columns == ["month"] and diagnosis.table_cn and diagnosis.table_type


def test_notes_and_summary(parts) -> None:
    """没有记录、未走到选表和没有选定用表分别说明，概要使用第一条依据"""
    store, retriever = parts
    for basis in (None, RoundBasis()):
        explanation = explain_previous(store, retriever, basis, [])
        assert explanation.summary == explanation.notes[0] == "这个会话还没有查询记录，没有可以解释的选表。"
    explanation = explain_previous(store, retriever, previous(plan=None, message="缺指标"), [])
    assert explanation.summary == "上一轮没有走到选表。缺指标"
    explanation = explain_previous(store, retriever, previous(plan=QueryPlan(decision="cannot_answer", reason="缺粒度")), [])
    assert explanation.summary == "上一轮没有选定用表：缺粒度"
    from nl2sql.schemas import Clarify
    explanation = explain_previous(store, retriever, previous(plan=QueryPlan(decision="need_clarify", clarify=Clarify(question="哪种口径？"))), [])
    assert "哪种口径？" in explanation.notes[0]
    explanation = explain_previous(store, retriever, previous(), [])
    assert explanation.summary == "上一轮用了 1 张表：ads.demo_used。"
    assert explanation.table_basis == "按月汇总" and explanation.caliber == "支出金额求和"
    assert explanation.dropped_tables == ["dws.demo_fund"] and explanation.candidate_tables == previous().candidate_tables
    explanation = explain_previous(store, retriever, previous(), ["ads.demo_used", "ads.demo_fund"])
    assert explanation.summary == "上一轮用了这张表。共问了 2 张表。"


def test_metadata_version_note(parts, monkeypatch) -> None:
    """版本不同才提示按当前版本诊断"""
    store, retriever = parts
    monkeypatch.setattr(type(store), "version_id", property(lambda self: "new"))
    explanation = explain_previous(store, retriever, previous(metadata_version="old"), [])
    assert "old，现在是 new" in explanation.notes[0]
    assert not explain_previous(store, retriever, previous(metadata_version="new"), []).notes
    assert not explain_previous(store, retriever, previous(), []).notes


def test_gaps_and_no_previous_without_parsed(parts) -> None:
    """资料缺项顺序固定，没有解析记录就不做召回重放或字段比较"""
    store, retriever = parts
    spy = Mock(wraps=retriever)
    diagnosis = explain_previous(store, spy, previous(parsed=None), ["ads.demo_fund"]).named[0]
    assert diagnosis.stage == "no_previous" and not diagnosis.matches
    spy.locate.assert_not_called()
    spy.match_fields.assert_not_called()
    assert diagnosis.gaps == ["表说明", "血缘", "关联关系", "加工脚本", "样例"]
    assert "它没有表说明、血缘、关联关系、加工脚本、样例" in diagnosis.reasons[-1]
    assert not explain_previous(store, retriever, None, ["ads.demo_fund"]).named[0].switchable
    unresolved = diagnose_unresolved("demo_absent", resolve_table(store, "demo_absent"))
    assert not unresolved.switchable


def test_gaps_recognize_sources_relations_scripts_and_examples() -> None:
    """血缘源端或目标端、关联任一侧和样例用表都算已有资料"""
    table = TableMeta(table_id="ads.demo_t", schema_name="ads", table_name="demo_t", table_cn="演示表",
                      layer="专题层", table_type="流水表", grain_columns=[], description="已说明")
    for source, target in [("ads.demo_t", "ads.demo_out"), ("ods.demo_in", "ads.demo_t")]:
        mart = Mart(tables=[table], lineage=[LineageEdge(source_table=source, source_column="id", target_table=target, target_column="id")],
                    relations=[Relation(left_table="ads.demo_t", right_table="ads.demo_out", left_columns=["id"], right_columns=["id"], cardinality="N:1")],
                    scripts=[ScriptStatement(script_name="demo.sql", stmt_index=1, target_table="ads.demo_t", sql_text="SELECT 1", parsed=True)],
                    examples=[Example(example_id="demo#1", requirement="演示", sql_text="SELECT 1", tables=["ads.demo_t"], source_file="demo.sql", line_no=1)])
        store = MetadataStore.from_mart(mart)
        diagnosis = explain_previous(store, Mock(), None, ["ads.demo_t"]).named[0]
        assert diagnosis.gaps == []


@pytest.mark.parametrize("pool, rank, expected", [("standard", 9, "排第 9"), ("extra", 4, "非标准表"),
                                                   ("standard", None, "没有命中"), ("standard", 1, "名额内")])
def test_replay_reasons(parts, pool: str, rank: int | None, expected: str) -> None:
    """重放名次、独立池和记录不一致都有据可查"""
    store, retriever = parts
    spy = Mock(wraps=retriever)
    spy.locate.return_value = TableLocation(pool, rank, 12, 8 if pool == "standard" else 3)
    diagnosis = explain_previous(store, spy, previous(), ["ads.demo_other"]).named[0]
    assert any(expected in reason for reason in diagnosis.reasons)
    assert any("没有名字相近的字段" in reason for reason in diagnosis.reasons)


def test_outside_after_upstream(parts) -> None:
    """上溯过的轮次只说明该表没有留下"""
    diagnosis = explain_previous(*parts, previous(scope_level=2), ["dwd.demo_detail"]).named[0]
    assert "上溯了 2 层" in diagnosis.reasons[0]


def test_not_chosen_reasons(parts) -> None:
    """层级、候选顺序和方案提及都有记录，名称匹配齐全时保留模型未解释的事实"""
    store, retriever = parts
    plan = previous().plan.model_copy(update={"dropped_tables": [], "table_basis": "没有采用 demo_fund"})
    basis = previous(plan=plan, candidate_tables=["demo_a", "demo_b", "demo_c", "demo_d", "dws.demo_fund"])
    diagnosis = explain_previous(store, retriever, basis, ["dws.demo_fund"]).named[0]
    assert any("专题层" in reason and "汇总层" in reason for reason in diagnosis.reasons)
    assert any("第 5 张" in reason for reason in diagnosis.reasons)
    assert any("用表依据里提到了它" in reason for reason in diagnosis.reasons)
    basis = previous(plan=QueryPlan(decision="ok", tables=[PlanTable(table="dws.demo_fund")]), sql_tables=[], candidate_tables=["ads.demo_used"])
    diagnosis = explain_previous(store, retriever, basis, ["ads.demo_used"]).named[0]
    assert diagnosis.reasons[0].startswith("字段名上看，需求要的它都有")


def test_table_name_inside_another_name_is_not_a_mention(parts) -> None:
    """表名是别的表名的一部分时，不算依据里提到了它；维表不说层级"""
    store, retriever = parts
    plan = previous().plan.model_copy(update={"table_basis": "用 ads.demo_used_month 汇总"})
    reasons = explain_previous(store, retriever, previous(plan=plan, candidate_tables=["ads.demo_used_x", "ads.demo_other"]),
                               ["ads.demo_other"]).named[0].reasons
    assert not any("提到了它" in reason for reason in reasons)
    plan = previous().plan.model_copy(update={"table_basis": "不用 ads.demo_other"})
    reasons = explain_previous(store, retriever, previous(plan=plan, candidate_tables=["ads.demo_other"]),
                               ["ads.demo_other"]).named[0].reasons
    assert any("提到了它" in reason for reason in reasons)


def test_basis_contains_no_execution_result() -> None:
    """查询结果只留在 QueryResult，依据结构和序列化都没有执行数据"""
    result = QueryResult(status="ok", question="查演示", requirement="完整需求", parsed=previous().parsed,
                         plan=previous().plan, validation=ValidationSummary(ok=True, tables=["ads.demo_used"]),
                         exec_result=ExecSummary(executed=True, rows=[["私有结果标记"]]), candidate_tables=["ads.demo_used"])
    basis = result.basis()
    assert "exec_result" not in RoundBasis.model_fields
    assert "exec_result" not in basis.model_dump() and "私有结果标记" not in basis.model_dump_json()
    assert basis.sql_tables == ["ads.demo_used"] and basis.requirement == result.requirement
    assert basis.plan == result.plan and basis.parsed == result.parsed
    assert QueryResult(status="failed", question="演示").basis().sql_tables == []
