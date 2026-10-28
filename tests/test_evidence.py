"""③ 证据组装的测试：候选表（顺序、字段、截断、命中词、加工类别）、指标字段的加工链路、同源对比、表的上下游、
关联路径（含补进来的中间表）、上溯选项、样例、对照实验 A 组的结构图，以及结果确定、模拟题的标准用表都进证据

小场景用手写的资料和手写的召回结果，精确控制输入；模拟专题库上的验收锚点用 conftest 里的会话夹具。
全部不调模型，不加载 BGE，也不引用真实专题库。
"""
import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from eval.questions import load_questions
from lineage.graph import LineageGraph
from metadata.models import (
    ColumnMeta,
    Example,
    FieldRef,
    LineageEdge,
    Mart,
    Relation,
    ScriptColumn,
    ScriptFilter,
    ScriptJoin,
    ScriptStatement,
    TableMeta,
)
from metadata.store import MetadataStore
from nl2sql import evidence as evidence_module
from nl2sql.evidence import (
    COND_CHARS,
    ENUM_CHARS,
    EXPR_CHARS,
    FULL_DESC_CHARS,
    LINEAGE_DEPTH,
    MAX_COMPARE_FIELDS,
    MAX_CONDS,
    MAX_JOIN_HINTS,
    MAX_JOIN_TABLES,
    MAX_LINEAGE_FIELDS,
    MAX_LINEAGE_TOTAL,
    MAX_STEPS_PER_FIELD,
    MAX_UPSTREAM_OPTIONS,
    SHORT_DESC_CHARS,
    build_evidence,
    drop_examples,
    structure_store,
)
from nl2sql.retriever import ExampleHit, FieldHit, RecallQuery, RecallResult, TableCandidate
from nl2sql.schemas import Evidence, EvidenceExample, JoinHop, TableLink, UpstreamOption

ROOT = Path(__file__).resolve().parents[1]
MOCK_MART_DIR = ROOT / "data" / "mock_mart"
RECALL_TERMS = Path(__file__).parent / "data" / "recall_terms.yaml"

DEP_D = "dwd.corp_dep_acct_d"
LOAN_D = "dwd.corp_loan_duebill_d"
CUST = "dim.corp_cust"
ORG = "dim.org"
ORG_INFO = "ods.org_info"
ADS_DEP = "ads.corp_cust_dep_bal_d"
ADS_LOAN = "ads.corp_cust_loan_bal_d"
ADS_ORG = "ads.org_corp_dep_bal_d"

SCHEMA_LAYER = {"ods": "贴源层", "dwd": "标准层", "dim": "标准层", "ads": "专题层"}


# ── 构造资料、召回结果的小工具 ──────────────────────────────────────

def make_mart(tables: list[tuple], edges: list[LineageEdge] = (), relations: list[Relation] = (),
              scripts: list[ScriptStatement] = (), script_columns: list[ScriptColumn] = (),
              examples: list[Example] = ()) -> Mart:
    """手写的小资料

    tables 的每一项是 (表标识, 字段列表[, 表的属性])。字段是 (字段名, 中文名[, 属性])，字段属性有
    data_type、is_pk、description、enum_values；表的属性有 cn、type、grain、date、desc。
    模式名为 dim 的默认是维表，其余默认是快照表。
    """
    metas, columns = [], []
    for table_id, fields, *rest in tables:
        options = rest[0] if rest else {}
        schema, name = table_id.split(".")
        metas.append(TableMeta(
            table_id=table_id, layer=SCHEMA_LAYER[schema], schema_name=schema, table_name=name,
            table_cn=options.get("cn", name), table_type=options.get("type", "维表" if schema == "dim" else "快照表"),
            grain_columns=list(options.get("grain", ())), date_column=options.get("date"),
            description=options.get("desc", "")))
        for ordinal, (column, column_cn, *extra) in enumerate(fields, 1):
            columns.append(ColumnMeta(table_id=table_id, column_name=column, ordinal=ordinal, column_cn=column_cn,
                                      **(extra[0] if extra else {})))
    return Mart(tables=metas, columns=columns, lineage=list(edges), relations=list(relations), scripts=list(scripts),
                script_columns=list(script_columns), examples=list(examples))


def make_store(*args, **kwargs) -> tuple[MetadataStore, LineageGraph]:
    """手写的小元数据库和它的血缘图"""
    store = MetadataStore.from_mart(make_mart(*args, **kwargs))
    return store, LineageGraph(store)


def edge(target: str, source: str, transform: str = "", script: str = "", external: bool = False,
         passthrough: bool = False, origin: str = "list") -> LineageEdge:
    """一条血缘边，目标和来源写成「表.字段」"""
    target_table, target_column = target.rsplit(".", 1)
    source_table, source_column = source.rsplit(".", 1)
    return LineageEdge(target_table=target_table, target_column=target_column, source_table=source_table,
                       source_column=source_column, transform=transform, script_name=script, source_external=external,
                       is_passthrough=passthrough, origin=origin)


def statement(script: str, target: str, filters: list[tuple[str, str]] = (), joins: list[ScriptJoin] = ()
              ) -> ScriptStatement:
    """一条写数语句，过滤条件写成 (子句, 条件原文)，都在最外层"""
    return ScriptStatement(script_name=script, stmt_index=1, target_table=target, sql_text="", parsed=True,
                           filters=[ScriptFilter(scope="main", clause=clause, condition=condition)
                                    for clause, condition in filters], joins=list(joins))


def relation(left: str, left_columns: list[str], right: str, right_columns: list[str], card: str,
             origin: str = "manual", note: str = "") -> Relation:
    """一条关联"""
    return Relation(left_table=left, left_columns=left_columns, right_table=right, right_columns=right_columns,
                    cardinality=card, origin=origin, note=note)


def script_column(table: str, column: str, kind: str, expression: str = "x", script: str = "load.sql",
                  index: int = 1) -> ScriptColumn:
    """脚本里一个目标字段的表达式"""
    return ScriptColumn(script_name=script, stmt_index=index, target_table=table, target_column=column,
                        expression=expression, kind=kind)


def hit(table_id: str, column: str, term: str, score: float = 0.02) -> FieldHit:
    """召回命中的一个字段"""
    return FieldHit(table_id=table_id, column_name=column, column_cn="", term=term, score=score, via=("keyword",))


def cand(table_id: str, reason: str = "recall", score: float = 0.03, hits: list[FieldHit] = ()) -> TableCandidate:
    """一张候选表"""
    return TableCandidate(table_id=table_id, reason=reason, score=score, hits=list(hits))


def recall_of(*tables: TableCandidate, examples: list[ExampleHit] = (), notes: list[str] = (), level: int = 0
              ) -> RecallResult:
    """手写的召回结果"""
    return RecallResult(scope_level=level, scope_size=len(tables), tables=list(tables), examples=list(examples),
                        notes=list(notes))


def evidence_of(store: MetadataStore, graph: LineageGraph, recall: RecallResult, metrics: list[str] = (), *,
                dimensions: list[str] = (), lineage: bool = True, examples: bool = False, all_layers: bool = False,
                join_graph: LineageGraph | None = None) -> Evidence:
    """用三个开关组装证据；指标检索词决定展开哪些字段的血缘"""
    query = RecallQuery("x", list(metrics), list(dimensions))
    return build_evidence(store, graph, recall, query, use_lineage=lineage, use_examples=examples,
                          all_layers=all_layers, join_graph=join_graph)


def fields_of(evidence: Evidence) -> list[str]:
    """证据里有加工链路的字段，按证据里的顺序"""
    return [item.field for item in evidence.lineage]


@pytest.fixture(scope="module")
def structure_graph(mock_store) -> LineageGraph:
    """模拟专题库只有表结构的关联图（对照实验 A 组求关联路径用）"""
    return LineageGraph(structure_store(mock_store))


def test_limits_are_the_documented_values():
    assert (LINEAGE_DEPTH, MAX_LINEAGE_FIELDS, MAX_STEPS_PER_FIELD, MAX_COMPARE_FIELDS, MAX_JOIN_HINTS,
            MAX_JOIN_TABLES, MAX_UPSTREAM_OPTIONS) == (3, 12, 12, 6, 20, 3, 10)
    assert (FULL_DESC_CHARS, SHORT_DESC_CHARS, ENUM_CHARS, EXPR_CHARS, COND_CHARS, MAX_CONDS) == (
        200, 60, 200, 800, 300, 6)
    assert MAX_LINEAGE_TOTAL == 20


# ── 候选表 ──────────────────────────────────────

def candidates_fixture() -> tuple[MetadataStore, LineageGraph]:
    """专题层一张表（字段的说明有长有短，有码值），标准层一张、贴源层一张"""
    return make_store([
        ("ads.dep", [
            ("data_dt", "数据日期", {"description": "D" * 250}),
            ("cust_no", "客户号", {"is_pk": True, "description": "K" * 250}),
            ("amt", "存款金额", {"data_type": "numeric(18,2)", "description": "A" * 250, "enum_values": "E" * 300}),
            ("flag", "标志", {"description": "F" * 100, "enum_values": "1=是;0=否" + "，" * 250}),
            ("memo", "备注", {"description": "短说明"}),
            ("rowid", "行号", {"is_pk": True, "description": "P" * 250}),
            ("note", "说明", {"description": "N" * 60}),
        ], {"cn": "存款汇总", "grain": ("data_dt", "cust_no"), "date": "data_dt", "desc": "按客户汇总的存款"}),
        ("dwd.src", [("amt", "存款金额"), ("data_dt", "数据日期")], {"cn": "存款明细", "date": "data_dt"}),
        ("ods.raw", [("amt", "原始金额")], {"cn": "原始存款", "type": "流水表"}),
    ])


def test_candidate_tables_keep_the_recall_order_reasons_scores_and_every_column():
    store, graph = candidates_fixture()
    recall = recall_of(
        cand("ads.dep", "recall", 0.0333, [hit("ads.dep", "amt", "存款余额", 0.0166)]),
        cand("dwd.src", "upstream", 0.02),
        cand("ods.raw", "example", 0.0))

    evidence = evidence_of(store, graph, recall, ["存款余额"])

    assert evidence.table_ids() == ["ads.dep", "dwd.src", "ods.raw"]
    assert [(t.reason, t.score) for t in evidence.tables] == [("recall", 0.0333), ("upstream", 0.02), ("example", 0.0)]
    dep = evidence.table("ads.dep")
    assert (dep.table_cn, dep.layer, dep.table_type, dep.grain_columns, dep.date_column, dep.description) == (
        "存款汇总", "专题层", "快照表", ["data_dt", "cust_no"], "data_dt", "按客户汇总的存款")
    assert [column.name for column in dep.columns] == [c.column_name for c in store.columns("ads.dep")]
    assert [column.cn for column in dep.columns][:3] == ["数据日期", "客户号", "存款金额"]
    amt = next(column for column in dep.columns if column.name == "amt")
    assert (amt.data_type, amt.is_pk) == ("numeric(18,2)", False)
    assert [column.name for column in evidence.table("dwd.src").columns] == ["amt", "data_dt"]
    assert evidence.table("ods.raw").table_type == "流水表"
    assert evidence.column_names("dwd.src") == {"amt", "data_dt"} and evidence.column_names("no.such") == set()


def test_hit_terms_come_from_the_hits_of_each_table():
    store, graph = candidates_fixture()
    recall = recall_of(cand("ads.dep", hits=[hit("ads.dep", "amt", "存款余额"), hit("ads.dep", "memo", "客户")]),
                       cand("dwd.src", "upstream", hits=[hit("dwd.src", "amt", "存款余额")]))

    evidence = evidence_of(store, graph, recall, ["存款余额"])

    dep_terms = {column.name: column.hit_terms for column in evidence.table("ads.dep").columns}
    assert {name: terms for name, terms in dep_terms.items() if terms} == {"amt": ["存款余额"], "memo": ["客户"]}
    assert [column.hit_terms for column in evidence.table("dwd.src").columns] == [["存款余额"], []]


def test_descriptions_are_cut_by_the_role_of_the_column_and_enum_values_always():
    store, graph = candidates_fixture()
    recall = recall_of(cand("ads.dep", hits=[hit("ads.dep", "amt", "存款余额")]))

    columns = {c.name: c for c in evidence_of(store, graph, recall, ["存款余额"]).table("ads.dep").columns}

    assert columns["amt"].description == "A" * FULL_DESC_CHARS + "…"           # 命中的字段
    assert columns["data_dt"].description == "D" * FULL_DESC_CHARS + "…"       # 粒度字段，也是数据日期字段
    assert columns["cust_no"].description == "K" * FULL_DESC_CHARS + "…"       # 粒度字段，也是主键
    assert columns["rowid"].description == "P" * FULL_DESC_CHARS + "…"         # 只是主键
    assert columns["flag"].description == "F" * SHORT_DESC_CHARS + "…"         # 其余字段
    assert columns["memo"].description == "短说明"                              # 不长的原样保留
    assert columns["note"].description == "N" * 60                              # 正好等于上限，不加省略号
    assert columns["amt"].enum_values == "E" * ENUM_CHARS + "…"                # 码值一律截到 200，不看字段的角色
    assert columns["flag"].enum_values == ("1=是;0=否" + "，" * 250)[:ENUM_CHARS] + "…"
    assert columns["memo"].enum_values == ""


def test_a_hit_in_another_table_does_not_make_the_column_a_full_description_column():
    store, graph = candidates_fixture()
    recall = recall_of(cand("ads.dep", hits=[]), cand("dwd.src", "upstream", hits=[hit("dwd.src", "amt", "存款余额")]))

    columns = {c.name: c for c in evidence_of(store, graph, recall, ["存款余额"]).table("ads.dep").columns}

    assert columns["amt"].description == "A" * SHORT_DESC_CHARS + "…"


def test_a_long_table_description_is_cut_too():
    store, graph = make_store([("ads.t", [("v", "指标")], {"desc": "表" * 250})])

    evidence = evidence_of(store, graph, recall_of(cand("ads.t")))

    assert evidence.table("ads.t").description == "表" * FULL_DESC_CHARS + "…"


def test_evidence_header_follows_the_switches_and_the_recall():
    store, graph = candidates_fixture()
    recall = recall_of(cand("ads.dep"), level=1, notes=["向量器核对没过，只走了关键字路"])

    evidence = evidence_of(store, graph, recall, lineage=False, examples=True, all_layers=True)

    assert (evidence.use_lineage, evidence.use_examples, evidence.all_layers, evidence.scope_level) == (
        False, True, True, 1)
    assert evidence.notes == ["向量器核对没过，只走了关键字路"]


def test_code_columns_carry_the_name_column_of_their_table_or_a_code_table_in_the_evidence(mock_store, mock_graph):
    recall = recall_of(cand(ADS_DEP), cand(CUST), cand("ods.ecif_corp_cust"), cand("ods.industry_dict"))

    evidence = evidence_of(mock_store, mock_graph, recall, ["存款余额"])

    cust = {column.name: column for column in evidence.table(CUST).columns}
    assert [cust[name].name_column for name in ("industry_cd", "scale_cd", "cust_no", "industry_nm")] == \
        ["industry_nm", "scale_nm", "cust_nm", ""]
    assert cust["industry_cd"].code_table == ""                                  # 同表有名称字段就不再指码值表
    dep = {column.name: column for column in evidence.table(ADS_DEP).columns}
    assert dep["cust_no"].code_table == "dim.corp_cust.cust_nm"                  # 标准层的维表排在贴源层前面
    assert dep["org_no"].code_table == ""                                        # 机构维表不在证据里
    ecif = {column.name: column for column in evidence.table("ods.ecif_corp_cust").columns}
    assert ecif["industry_cd"].code_table == "ods.industry_dict.industry_nm"


def test_a_code_table_outside_the_evidence_is_not_pointed_to(mock_store, mock_graph):
    evidence = evidence_of(mock_store, mock_graph, recall_of(cand("ods.ecif_corp_cust")), ["客户"])

    columns = {column.name: column for column in evidence.table("ods.ecif_corp_cust").columns}
    assert columns["industry_cd"].code_table == "" and columns["industry_cd"].name_column == ""
    assert columns["cust_no"].name_column == "cust_nm"


def test_candidates_unknown_to_the_store_and_repeated_ones_are_left_out():
    store, graph = candidates_fixture()

    evidence = evidence_of(store, graph, recall_of(cand("ads.dep"), cand("ads.gone"), cand("dwd.src", "upstream"),
                                                   cand("ads.dep", "example")))

    assert [(t.table_id, t.reason) for t in evidence.tables] == [("ads.dep", "recall"), ("dwd.src", "upstream")]


# ── 加工类别 ──────────────────────────────────────

def test_column_kind_comes_from_the_script_and_only_with_lineage():
    store, graph = make_store(
        [("ads.t", [("a", "甲"), ("b", "乙"), ("c", "丙"), ("d", "丁"), ("e", "戊"), ("f", "己")])],
        script_columns=[script_column("ads.t", "a", "derived"),
                        script_column("ads.t", "b", "null", script="s1.sql"),
                        script_column("ads.t", "b", "derived", script="s2.sql"),
                        script_column("ads.t", "c", "null"),
                        script_column("ads.t", "d", "etl_time", script="s1.sql"),
                        script_column("ads.t", "d", "null", script="s2.sql"),
                        script_column("ads.t", "e", "derived", script="s1.sql"),
                        script_column("ads.t", "e", "null", script="s2.sql")])
    recall = recall_of(cand("ads.t"))

    with_lineage = evidence_of(store, graph, recall, lineage=True)
    without = evidence_of(store, graph, recall, lineage=False)

    kinds = {column.name: column.kind for column in with_lineage.table("ads.t").columns}
    assert kinds == {"a": "derived", "b": "derived", "c": "null", "d": "etl_time", "e": "derived", "f": ""}
    assert {column.kind for column in without.table("ads.t").columns} == {""}      # A 组没有脚本这一类证据


# ── 加工链路 ──────────────────────────────────────

def test_lineage_merges_the_sources_of_one_target_into_one_step(mock_store, mock_graph):
    recall = recall_of(cand(ADS_ORG, hits=[hit(ADS_ORG, "dep_bal_rmb", "存款余额")]))

    evidence = evidence_of(mock_store, mock_graph, recall, ["存款余额"])

    (lineage,) = evidence.lineage
    assert (lineage.field, lineage.term, lineage.truncated) == (f"{ADS_ORG}.dep_bal_rmb", "存款余额", False)
    assert [(step.depth, step.target, step.sources) for step in lineage.steps] == [
        (1, f"{ADS_ORG}.dep_bal_rmb", [f"{ADS_DEP}.dep_bal_rmb"]),
        (2, f"{ADS_DEP}.dep_bal_rmb", [f"{DEP_D}.bal_rmb", f"{DEP_D}.is_margin"]),              # 两个来源并在一步里
        (3, f"{DEP_D}.bal_rmb", ["ods.core_dep_acct.acct_bal", "ods.exch_rate.mid_rate"]),
        (3, f"{DEP_D}.is_margin", ["ods.gl_subject.subj_type_cd"]),
    ]
    assert all(step.unknown_sources == [] for step in lineage.steps)
    second = lineage.steps[1]
    assert second.expression == ("SUM(CASE WHEN dwd.corp_dep_acct_d.is_margin = '0' "
                                 "THEN dwd.corp_dep_acct_d.bal_rmb ELSE 0 END)")
    assert (second.script_name, second.origin) == ("21_ads_corp_cust_dep_bal_d.sql", "list")


def test_filters_and_joins_of_a_step_are_written_as_conditions(mock_store, mock_graph):
    recall = recall_of(cand(ADS_DEP, hits=[hit(ADS_DEP, "dep_bal_rmb", "存款余额")]))

    (lineage,) = evidence_of(mock_store, mock_graph, recall, ["存款余额"]).lineage

    step = lineage.steps[0]
    assert step.filters == ["WHERE dwd.corp_dep_acct_d.data_dt = CAST('${data_dt}' AS DATE)"]
    assert [text.split(" ON ")[0] for text in step.joins] == ["JOIN ytd", "JOIN mtd", "JOIN dim.corp_cust"]
    assert step.joins[-1] == "JOIN dim.corp_cust ON dwd.corp_dep_acct_d.cust_no = dim.corp_cust.cust_no"


def test_without_lineage_there_is_no_lineage_comparison_link_or_upstream_option(mock_store, mock_graph):
    recall = recall_of(cand(ADS_ORG, hits=[hit(ADS_ORG, "dep_bal_rmb", "存款余额"),
                                           hit(ADS_ORG, "dep_bal_tot_rmb", "存款余额")]),
                       cand(ADS_DEP))

    evidence = evidence_of(mock_store, mock_graph, recall, ["存款余额"], lineage=False)

    assert (evidence.lineage, evidence.comparisons, evidence.table_links, evidence.upstream_options) == ([], [], [], [])
    assert evidence.joins                                                      # 关联路径各组都有


@pytest.mark.parametrize("join_type, prefix", [
    ("LEFT", "LEFT JOIN"), ("INNER", "INNER JOIN"), ("JOIN", "JOIN"), ("IMPLICIT", "WHERE 等值关联"),
    ("RIGHT", "RIGHT JOIN"), ("FULL", "FULL JOIN"), ("CROSS", "CROSS")])
def test_join_conditions_get_a_prefix_that_follows_the_join_type(join_type, prefix):
    join = ScriptJoin(scope="main", join_type=join_type, table="dim.d", condition="ods.s.k = dim.d.k")
    store, graph = make_store([("ads.t", [("v", "指标")]), ("ods.s", [("v", "指标"), ("k", "键")])],
                              edges=[edge("ads.t.v", "ods.s.v", script="load.sql")],
                              scripts=[statement("load.sql", "ads.t", joins=[join])])

    (lineage,) = evidence_of(store, graph, recall_of(cand("ads.t", hits=[hit("ads.t", "v", "指标")])), ["指标"]).lineage

    assert lineage.steps[0].joins == [f"{prefix} dim.d ON ods.s.k = dim.d.k"]       # 不认识的类型照写


def test_conditions_are_deduplicated_cut_to_length_and_limited_in_number():
    long = "ods.s.note = '" + "x" * 400 + "'"
    filters = [("WHERE", "ods.s.flag = 1"), ("HAVING", "SUM(ods.s.v) > 0"), ("WHERE", "ods.s.flag = 1"),
               ("WHERE", long)]
    filters += [("WHERE", f"ods.s.c{number} = {number}") for number in range(6)]
    store, graph = make_store([("ads.t", [("v", "指标")]), ("ods.s", [("v", "指标")])],
                              edges=[edge("ads.t.v", "ods.s.v", script="load.sql")],
                              scripts=[statement("load.sql", "ads.t", filters=filters)])

    evidence = evidence_of(store, graph, recall_of(cand("ads.t", hits=[hit("ads.t", "v", "指标")])), ["指标"])

    step = evidence.lineage[0].steps[0]
    assert len(step.filters) == MAX_CONDS                                       # 去掉一条重复的以后还有 9 条，只留 6 条
    assert step.filters[:2] == ["WHERE ods.s.flag = 1", "HAVING SUM(ods.s.v) > 0"]
    assert step.filters[2] == ("WHERE " + long)[:COND_CHARS] + "…"
    assert any("超过 6 条" in note for note in evidence.notes)


def test_a_target_written_by_two_scripts_gets_the_conditions_of_both_in_one_step():
    store, graph = make_store(
        [("ads.t", [("v", "指标")]), ("ods.a", [("v", "甲")]), ("ods.b", [("v", "乙")])],
        edges=[edge("ads.t.v", "ods.a.v", script="a.sql", origin="script"), edge("ads.t.v", "ods.b.v", script="b.sql")],
        scripts=[statement("a.sql", "ads.t", filters=[("WHERE", "ods.a.flag = 1"), ("WHERE", "shared = 1")]),
                 statement("b.sql", "ads.t", filters=[("WHERE", "ods.b.flag = 2"), ("WHERE", "shared = 1")])])

    (lineage,) = evidence_of(store, graph, recall_of(cand("ads.t", hits=[hit("ads.t", "v", "指标")])), ["指标"]).lineage

    (step,) = lineage.steps
    assert step.sources == ["ods.a.v", "ods.b.v"]
    assert step.filters == ["WHERE ods.a.flag = 1", "WHERE shared = 1", "WHERE ods.b.flag = 2"]      # 两个脚本的合起来，去重
    assert (step.script_name, step.origin) == ("a.sql", "script")                                   # 取第一跳的


def test_a_step_with_few_conditions_adds_no_note(mock_store, mock_graph):
    recall = recall_of(cand(ADS_DEP, hits=[hit(ADS_DEP, "dep_bal_rmb", "存款余额")]))

    assert evidence_of(mock_store, mock_graph, recall, ["存款余额"]).notes == []


def test_expression_prefers_the_script_then_the_lineage_list_and_is_cut():
    long = "SUM(" + "ods.s.v + " * 150 + "0)"
    columns = [script_column("ads.t", "scripted", "derived", expression="SUM(ods.s.v)"),
               script_column("ads.t", "long", "derived", expression=long)]
    store, graph = make_store(
        [("ads.t", [("scripted", "有脚本"), ("listed", "只有清单"), ("bare", "什么都没写"), ("long", "很长")]),
         ("ods.s", [("v", "指标")])],
        edges=[edge("ads.t.scripted", "ods.s.v", transform="清单里的文字"),
               edge("ads.t.listed", "ods.s.v", transform="MAX(v)"),
               edge("ads.t.bare", "ods.s.v"), edge("ads.t.long", "ods.s.v")],
        script_columns=columns)
    hits = [hit("ads.t", name, "指标") for name in ("scripted", "listed", "bare", "long")]

    evidence = evidence_of(store, graph, recall_of(cand("ads.t", hits=hits)), ["指标"])

    expressions = {item.field.rsplit(".", 1)[1]: item.steps[0].expression for item in evidence.lineage}
    assert expressions == {"scripted": "SUM(ods.s.v)", "listed": "MAX(v)", "bare": "", "long": long[:EXPR_CHARS] + "…"}
    assert len(long) > EXPR_CHARS


def test_lineage_goes_three_steps_deep_at_most():
    names = [f"f{number}" for number in range(1, 6)]
    store, graph = make_store([("ods.t", [(name, name) for name in names])],
                              edges=[edge(f"ods.t.f{number}", f"ods.t.f{number + 1}") for number in range(1, 5)])

    (lineage,) = evidence_of(store, graph, recall_of(cand("ods.t", hits=[hit("ods.t", "f1", "指标")])), ["指标"]).lineage

    assert [(step.depth, step.target) for step in lineage.steps] == [(1, "ods.t.f1"), (2, "ods.t.f2"), (3, "ods.t.f3")]
    assert LINEAGE_DEPTH == 3 and not lineage.truncated


def test_sources_missing_from_the_field_list_are_marked_unknown():
    store, graph = make_store(
        [("ads.t", [("v", "指标")]), ("ods.s", [("k", "键")])],
        edges=[edge("ads.t.v", "ods.s.k"), edge("ads.t.v", "ods.s.gone", origin="script", passthrough=True),
               edge("ads.t.v", "ext.src.v", external=True)])

    (lineage,) = evidence_of(store, graph, recall_of(cand("ads.t", hits=[hit("ads.t", "v", "指标")])), ["指标"]).lineage

    (step,) = lineage.steps
    assert step.sources == ["ods.s.k", "ods.s.gone", "ext.src.v"]
    assert step.unknown_sources == ["ods.s.gone", "ext.src.v"]              # SQL 里不能引用
    assert step.origin == "list"


def fan_out_fixture() -> tuple[MetadataStore, LineageGraph]:
    """t.v 取自 a1..a4，每个 a 取自四个 b，每个 b 取自一个 c：深度 3 以内 1 + 4 + 16 = 21 步"""
    names = ["v"] + [f"a{i}" for i in range(1, 5)]
    names += [f"{kind}{i}{j}" for kind in "bc" for i in range(1, 5) for j in range(1, 5)]
    edges = [edge("ods.t.v", f"ods.t.a{i}") for i in range(1, 5)]
    edges += [edge(f"ods.t.a{i}", f"ods.t.b{i}{j}") for i in range(1, 5) for j in range(1, 5)]
    edges += [edge(f"ods.t.b{i}{j}", f"ods.t.c{i}{j}") for i in range(1, 5) for j in range(1, 5)]
    return make_store([("ods.t", [(name, name) for name in names])], edges=edges)


def test_a_long_chain_is_cut_at_the_step_limit_and_says_so():
    store, graph = fan_out_fixture()
    recall = recall_of(cand("ods.t", hits=[hit("ods.t", "v", "指标"), hit("ods.t", "a1", "指标", 0.01)]))

    evidence = evidence_of(store, graph, recall, ["指标"])

    long_one, short_one = evidence.lineage
    assert (long_one.field, long_one.truncated, len(long_one.steps)) == ("ods.t.v", True, MAX_STEPS_PER_FIELD)
    assert [step.depth for step in long_one.steps] == [1] + [2] * 4 + [3] * 7           # 按广度优先的顺序留前 12 步
    assert long_one.steps[0].sources == ["ods.t.a1", "ods.t.a2", "ods.t.a3", "ods.t.a4"]
    assert (short_one.field, short_one.truncated, len(short_one.steps)) == ("ods.t.a1", False, 5)
    assert any("ods.t.v" in note and "12 步" in note for note in evidence.notes)
    assert not any("ods.t.a1" in note for note in evidence.notes)


def test_the_memo_graph_walks_each_field_once_and_changes_no_answer(mock_graph, monkeypatch):
    a, b = FieldRef(table=ADS_ORG, column="dep_bal_rmb"), FieldRef(table=ADS_ORG, column="dep_bal_tot_rmb")
    expected = (mock_graph.compare(a, b), mock_graph.upstream(ADS_ORG, "dep_bal_rmb"))
    walked = []
    original = LineageGraph.upstream

    def counting(self, table, column, depth=3):
        walked.append((table, column))
        return original(self, table, column, depth)

    monkeypatch.setattr(LineageGraph, "upstream", counting)
    memo = evidence_module._MemoGraph(mock_graph)

    answers = (memo.compare(a, b), memo.upstream(ADS_ORG, "dep_bal_rmb"))
    memo.compare(b, a)
    memo.compare(a, b)

    assert answers == expected
    assert walked == [(ADS_ORG, "dep_bal_rmb"), (ADS_ORG, "dep_bal_tot_rmb")]       # 比较了三次，每个字段只走一遍


# ── 指标检索词与指标命中字段 ──────────────────────────────────────

def test_only_fields_hit_by_a_metric_term_get_lineage_best_score_first_then_table_then_name():
    tables = [("ads.t1", [("a9", "甲"), ("a1", "乙"), ("a2", "丙"), ("dim_col", "维度")]), ("ads.t2", [("b1", "丁")]),
              ("ads.t3", [("c1", "戊")])]
    store, graph = make_store(tables)
    recall = recall_of(
        cand("ads.t1", hits=[hit("ads.t1", "a2", "余额", 0.02), hit("ads.t1", "a9", "余额", 0.03),
                             hit("ads.t1", "a1", "余额", 0.03), hit("ads.t1", "dim_col", "机构", 0.09)]),
        cand("ads.t2", hits=[hit("ads.t2", "b1", "余额", 0.03)]),
        cand("ads.t3", hits=[hit("ads.t3", "c1", "机构", 0.05)]))

    evidence = evidence_of(store, graph, recall, [" 余额 "], dimensions=["机构"])

    assert fields_of(evidence) == ["ads.t1.a1", "ads.t1.a9", "ads.t2.b1", "ads.t1.a2"]   # 同分先比表的先后，再比字段名
    assert [item.term for item in evidence.lineage] == ["余额"] * 4
    assert all(item.steps == [] and not item.truncated for item in evidence.lineage)         # 没有上游的字段也列出，steps 为空


def test_no_metric_term_means_no_lineage_but_the_rest_is_still_there(mock_store, mock_graph):
    recall = recall_of(cand(ADS_ORG, hits=[hit(ADS_ORG, "org_no", "机构号")]), cand(ADS_DEP), cand(ORG))

    evidence = evidence_of(mock_store, mock_graph, recall, [], dimensions=["机构号"])

    assert evidence.lineage == [] and evidence.comparisons == []
    assert evidence.joins and evidence.table_links


def test_lineage_fields_are_limited_and_the_note_says_how_many_were_left_out(monkeypatch):
    names = [f"m{number}" for number in range(5)]
    store, graph = make_store([("ads.t", [(name, name) for name in names])])
    hits = [hit("ads.t", name, "余额", 0.05 - number * 0.001) for number, name in enumerate(names)]
    monkeypatch.setattr(evidence_module, "MAX_LINEAGE_FIELDS", 3)

    evidence = evidence_of(store, graph, recall_of(cand("ads.t", hits=hits)), ["余额"])

    assert fields_of(evidence) == ["ads.t.m0", "ads.t.m1", "ads.t.m2"]
    assert any("共 5 个" in note and "前 3 个" in note for note in evidence.notes)


# ── 同源对比 ──────────────────────────────────────

def test_anchor_1_two_deposit_balances_with_the_same_source_but_different_processing(mock_store, mock_graph):
    recall = recall_of(cand(ADS_ORG, hits=[hit(ADS_ORG, "dep_bal_rmb", "存款余额", 0.04),
                                           hit(ADS_ORG, "dep_bal_tot_rmb", "存款余额", 0.03)]))

    evidence = evidence_of(mock_store, mock_graph, recall, ["存款余额"])

    (comparison,) = evidence.comparisons
    assert (comparison.term, comparison.a, comparison.b, comparison.relation) == (
        "存款余额", f"{ADS_ORG}.dep_bal_rmb", f"{ADS_ORG}.dep_bal_tot_rmb", "same_source_diff")
    assert comparison.common == [f"{DEP_D}.bal_rmb", "ods.core_dep_acct.acct_bal", "ods.exch_rate.mid_rate"]
    assert f"{DEP_D}.is_margin" not in comparison.common                        # 全口径存款余额不看是否保证金
    assert comparison.note


@pytest.mark.parametrize("scores", [(0.04, 0.03), (0.03, 0.04)], ids=["downstream first", "upstream first"])
def test_anchor_2_org_balance_is_derived_from_the_customer_balance_whatever_the_ranking(mock_store, mock_graph, scores):
    recall = recall_of(cand(ADS_ORG, hits=[hit(ADS_ORG, "dep_bal_rmb", "存款余额", scores[0])]),
                       cand(ADS_DEP, hits=[hit(ADS_DEP, "dep_bal_rmb", "存款余额", scores[1])]))

    evidence = evidence_of(mock_store, mock_graph, recall, ["存款余额"])

    (comparison,) = evidence.comparisons
    assert (comparison.relation, comparison.a, comparison.b) == (
        "derived_from", f"{ADS_DEP}.dep_bal_rmb", f"{ADS_ORG}.dep_bal_rmb")       # b 由 a 加工而来，方向看血缘，不看排名
    assert f"{ADS_DEP}.dep_bal_rmb" in comparison.common


def source_fixture(transforms: dict[str, str], extra_tables: list[tuple] = (), extra_edges: list[LineageEdge] = ()
                   ) -> tuple[MetadataStore, LineageGraph]:
    """每张 ads 表有一个字段 x，加工自 ods.s.k，加工逻辑各不相同"""
    tables = [("ods.s", [("k", "来源")])] + [(table, [("x", "指标")]) for table in transforms] + list(extra_tables)
    edges = [edge(f"{table}.x", "ods.s.k", transform=transform) for table, transform in transforms.items()]
    return make_store(tables, edges=edges + list(extra_edges))


def recall_of_x(*tables: str, term: str = "金额") -> RecallResult:
    """每张表命中 x 字段，得分依次降低"""
    return recall_of(*[cand(table, hits=[hit(table, "x", term, 0.05 - number * 0.01)])
                       for number, table in enumerate(tables)])


def test_same_source_and_same_processing_is_recorded_as_same_source_same():
    store, graph = source_fixture({"ads.t1": "ods.s.k", "ads.t2": "ods.s.k"})

    evidence = evidence_of(store, graph, recall_of_x("ads.t1", "ads.t2"), ["金额"])

    (comparison,) = evidence.comparisons
    assert (comparison.relation, comparison.a, comparison.b, comparison.common) == (
        "same_source_same", "ads.t1.x", "ads.t2.x", ["ods.s.k"])


def test_fields_without_a_common_upstream_are_not_compared():
    store, graph = source_fixture({"ads.t1": "ods.s.k"}, extra_tables=[("ads.t2", [("x", "指标")])])

    evidence = evidence_of(store, graph, recall_of_x("ads.t1", "ads.t2"), ["金额"])

    assert evidence.comparisons == []


def test_every_pair_of_the_top_fields_is_compared_once_in_ranking_order():
    store, graph = source_fixture({"ads.t1": "SUM(k)", "ads.t2": "MAX(k)", "ads.t3": "MIN(k)"})

    evidence = evidence_of(store, graph, recall_of_x("ads.t1", "ads.t2", "ads.t3"), ["金额"])

    assert [(c.a, c.b, c.relation) for c in evidence.comparisons] == [
        ("ads.t1.x", "ads.t2.x", "same_source_diff"), ("ads.t1.x", "ads.t3.x", "same_source_diff"),
        ("ads.t2.x", "ads.t3.x", "same_source_diff")]


def test_a_pair_hit_by_two_terms_is_recorded_once_under_the_first_term():
    store, graph = source_fixture({"ads.t1": "SUM(k)", "ads.t2": "MAX(k)"})
    recall = recall_of(*[cand(table, hits=[hit(table, "x", "金额", 0.05), hit(table, "x", "余额", 0.04)])
                         for table in ("ads.t1", "ads.t2")])

    evidence = evidence_of(store, graph, recall, ["余额", "金额"])             # 检索词的先后：先「余额」

    assert [(c.term, c.a, c.b) for c in evidence.comparisons] == [("余额", "ads.t1.x", "ads.t2.x")]


def test_only_the_top_fields_of_a_term_are_compared_and_the_note_says_so(monkeypatch):
    store, graph = source_fixture({f"ads.t{number}": f"f{number}(k)" for number in range(1, 5)})
    monkeypatch.setattr(evidence_module, "MAX_COMPARE_FIELDS", 3)

    evidence = evidence_of(store, graph, recall_of_x("ads.t1", "ads.t2", "ads.t3", "ads.t4"), ["金额"])

    assert {field for c in evidence.comparisons for field in (c.a, c.b)} == {"ads.t1.x", "ads.t2.x", "ads.t3.x"}
    assert len(evidence.comparisons) == 3
    assert any("「金额」" in note and "前 3 个" in note for note in evidence.notes)


def test_fields_in_a_same_source_diff_pair_get_lineage_even_without_a_slot(monkeypatch):
    store, graph = source_fixture({"ads.t1": "SUM(k)", "ads.t2": "MAX(k)"},
                                  extra_tables=[("ads.t3", [("x", "指标")]), ("ods.q", [("k", "别处")])],
                                  extra_edges=[edge("ads.t3.x", "ods.q.k")])
    monkeypatch.setattr(evidence_module, "MAX_LINEAGE_FIELDS", 1)

    evidence = evidence_of(store, graph, recall_of_x("ads.t1", "ads.t2", "ads.t3"), ["金额"])

    assert fields_of(evidence) == ["ads.t1.x", "ads.t2.x"]                       # t2 没排进名额，因为和 t1 同源不同口径而补上；t3 没有
    assert [item.term for item in evidence.lineage] == ["金额", "金额"]
    assert evidence.lineage[1].steps and evidence.lineage[1].steps[0].expression == "MAX(k)"


def test_derived_from_fields_without_a_slot_do_not_get_lineage(mock_store, mock_graph, monkeypatch):
    recall = recall_of(cand(ADS_ORG, hits=[hit(ADS_ORG, "dep_bal_rmb", "存款余额", 0.04)]),
                       cand(ADS_DEP, hits=[hit(ADS_DEP, "dep_bal_rmb", "存款余额", 0.03)]))
    monkeypatch.setattr(evidence_module, "MAX_LINEAGE_FIELDS", 1)

    evidence = evidence_of(mock_store, mock_graph, recall, ["存款余额"])

    assert [c.relation for c in evidence.comparisons] == ["derived_from"]
    assert fields_of(evidence) == [f"{ADS_ORG}.dep_bal_rmb"]                    # 只有同源不同口径的才补，上下游关系不补


def test_extra_lineage_fields_are_capped_in_total_and_the_note_says_so(monkeypatch):
    store, graph = source_fixture({f"ads.t{number}": f"f{number}(k)" for number in range(1, 5)})
    monkeypatch.setattr(evidence_module, "MAX_LINEAGE_FIELDS", 1)
    monkeypatch.setattr(evidence_module, "MAX_LINEAGE_TOTAL", 3)

    evidence = evidence_of(store, graph, recall_of_x("ads.t1", "ads.t2", "ads.t3", "ads.t4"), ["金额"])

    assert fields_of(evidence) == ["ads.t1.x", "ads.t2.x", "ads.t3.x"]
    assert any("1 个" in note and "上限 3 个" in note for note in evidence.notes)


# ── 表的上下游 ──────────────────────────────────────

def test_table_links_point_from_the_upstream_table_to_the_downstream_one(mock_store, mock_graph):
    recall = recall_of(cand(ADS_ORG), cand(ADS_DEP), cand(ADS_LOAN), cand(CUST))

    evidence = evidence_of(mock_store, mock_graph, recall)

    assert evidence.table_links == [
        TableLink(upstream=ADS_DEP, downstream=ADS_ORG), TableLink(upstream=CUST, downstream=ADS_ORG),
        TableLink(upstream=CUST, downstream=ADS_DEP), TableLink(upstream=CUST, downstream=ADS_LOAN)]
    assert TableLink(upstream=ADS_ORG, downstream=ADS_DEP) not in evidence.table_links         # 机构表在客户表的下游，不是反过来


# ── 关联路径 ──────────────────────────────────────

def test_anchor_3_org_summary_joins_the_org_dimension_by_org_no_without_fan_out(mock_store, mock_graph):
    evidence = evidence_of(mock_store, mock_graph, recall_of(cand(ADS_ORG), cand(ORG)))

    (hint,) = evidence.joins
    assert (hint.left_table, hint.right_table, hint.fan_out) == (ADS_ORG, ORG, False)
    assert hint.hops == [JoinHop(
        left_table=ADS_ORG, left_columns=["org_no"], right_table=ORG, right_columns=["org_no"], cardinality="N:1",
        origin="grain_key", note=f"同名粒度键：{ADS_ORG} 含有 {ORG} 的全部粒度字段（org_no）")]


def test_walking_the_join_the_other_way_fans_out(mock_store, mock_graph):
    evidence = evidence_of(mock_store, mock_graph, recall_of(cand(ORG), cand(ADS_ORG)))

    (hint,) = evidence.joins
    assert (hint.left_table, hint.right_table, hint.fan_out) == (ORG, ADS_ORG, True)
    assert (hint.hops[0].left_table, hint.hops[0].cardinality) == (ORG, "1:N")


def test_anchor_5_without_lineage_the_customer_summary_joins_the_customer_dimension_by_a_grain_key(
        mock_store, mock_graph, structure_graph):
    recall = recall_of(cand(ADS_DEP), cand(CUST))

    structural = evidence_of(mock_store, mock_graph, recall, lineage=False, all_layers=True, join_graph=structure_graph)
    full = evidence_of(mock_store, mock_graph, recall)

    (hop,) = structural.joins[0].hops
    assert (hop.left_columns, hop.right_columns, hop.cardinality, hop.origin) == (
        ["cust_no"], ["cust_no"], "N:1", "grain_key")
    assert hop.note.startswith("同名粒度键")
    assert full.joins[0].hops[0].origin == "lineage"                              # 完整推导里这条映射先被血缘传递认领


def test_anchor_6_without_lineage_the_joins_only_the_scripts_know_about_are_missing(
        mock_store, mock_graph, structure_graph):
    recall = recall_of(cand(ORG_INFO), cand(ORG))

    structural = evidence_of(mock_store, mock_graph, recall, lineage=False, all_layers=True, join_graph=structure_graph)
    full = evidence_of(mock_store, mock_graph, recall)

    (hop,) = structural.joins[0].hops
    assert (hop.left_columns, hop.right_columns, hop.origin) == (["org_no"], ["org_no"], "grain_key")     # 没有 up_org_no
    assert structural.joins[0].hops[0].cardinality == "1:1"
    assert (full.joins[0].hops[0].left_columns, full.joins[0].hops[0].origin) == (["up_org_no"], "lineage")
    assert structure_graph.relations_between(ORG_INFO, ORG_INFO) == []            # 机构表按上级机构号的自关联只在脚本里


def test_join_graph_defaults_to_the_graph_of_the_evidence(mock_store, mock_graph):
    recall = recall_of(cand(ADS_DEP), cand(CUST))

    assert (evidence_of(mock_store, mock_graph, recall).joins
            == evidence_of(mock_store, mock_graph, recall, join_graph=mock_graph).joins)


def chain_fixture(extra: list[tuple] = (), extra_relations: list[Relation] = ()) -> tuple[MetadataStore, LineageGraph]:
    """a 与 c 之间没有直连，要经过 b；d 与 a 直连"""
    tables = [(name, [("k", "键"), ("m", "另一个键")]) for name in ("ads.a", "ads.b", "ads.c", "ads.d")] + list(extra)
    relations = [relation("ads.a", ["k"], "ads.b", ["k"], "N:1"), relation("ads.b", ["m"], "ads.c", ["m"], "N:1"),
                 relation("ads.a", ["m"], "ads.d", ["m"], "N:1")] + list(extra_relations)
    return make_store(tables, relations=relations)


def test_a_table_on_the_join_path_is_added_with_reason_join():
    store, graph = chain_fixture()

    evidence = evidence_of(store, graph, recall_of(cand("ads.a", score=0.05), cand("ads.c", score=0.04)))

    assert evidence.table_ids() == ["ads.a", "ads.c", "ads.b"]
    middle = evidence.table("ads.b")
    assert (middle.reason, middle.score, [column.name for column in middle.columns]) == ("join", 0.0, ["k", "m"])
    (hint,) = evidence.joins
    assert [(hop.left_table, hop.right_table) for hop in hint.hops] == [("ads.a", "ads.b"), ("ads.b", "ads.c")]
    assert not hint.fan_out


def test_a_middle_table_that_is_already_a_candidate_is_not_added_again():
    store, graph = chain_fixture()

    evidence = evidence_of(store, graph, recall_of(cand("ads.a"), cand("ads.b", "upstream"), cand("ads.c")))

    assert evidence.table_ids() == ["ads.a", "ads.b", "ads.c"]
    assert [(h.left_table, h.right_table, len(h.hops)) for h in evidence.joins] == [
        ("ads.a", "ads.b", 1), ("ads.b", "ads.c", 1), ("ads.a", "ads.c", 2)]


def test_direct_joins_come_before_joins_through_a_middle_table():
    store, graph = chain_fixture()

    evidence = evidence_of(store, graph, recall_of(cand("ads.a"), cand("ads.c"), cand("ads.d")))

    assert [(h.left_table, h.right_table, len(h.hops)) for h in evidence.joins] == [
        ("ads.a", "ads.d", 1), ("ads.a", "ads.c", 2)]                           # 候选顺序里 a、c 在前，但直连的排前面


def four_pairs_fixture() -> tuple[MetadataStore, LineageGraph]:
    """四对表 xi、yi，各自要经过自己的中间表 mi 才连得上"""
    names = [f"ads.{kind}{number}" for number in range(1, 5) for kind in "xmy"]
    relations = []
    for number in range(1, 5):
        relations += [relation(f"ads.x{number}", ["k"], f"ads.m{number}", ["k"], "N:1"),
                      relation(f"ads.m{number}", ["m"], f"ads.y{number}", ["m"], "N:1")]
    return make_store([(name, [("k", "键"), ("m", "另一个键")]) for name in names], relations=relations)


def test_at_most_three_middle_tables_are_added_and_later_paths_are_left_out():
    store, graph = four_pairs_fixture()
    candidates = [cand(f"ads.{kind}{number}") for number in range(1, 5) for kind in "xy"]

    evidence = evidence_of(store, graph, recall_of(*candidates))

    assert evidence.table_ids()[8:] == ["ads.m1", "ads.m2", "ads.m3"]
    assert {table.reason for table in evidence.tables[8:]} == {"join"}
    assert [(h.left_table, h.right_table) for h in evidence.joins] == [
        ("ads.x1", "ads.y1"), ("ads.x2", "ads.y2"), ("ads.x3", "ads.y3")]        # 第四条路径要第四张中间表，不记
    assert any("超过 3 张" in note and "1 条" in note for note in evidence.notes)


def test_one_middle_table_shared_by_several_paths_costs_one_slot():
    tables = [(name, [("k", "键"), ("m", "另一个键")]) for name in ("ads.a", "ads.b", "ads.c", "ads.d", "ads.e")]
    relations = [relation("ads.a", ["k"], "ads.d", ["k"], "N:1"), relation("ads.b", ["k"], "ads.d", ["k"], "N:1"),
                 relation("ads.c", ["k"], "ads.d", ["k"], "N:1")]
    store, graph = make_store(tables, relations=relations)

    evidence = evidence_of(store, graph, recall_of(cand("ads.a"), cand("ads.b"), cand("ads.c"), cand("ads.e")))

    assert evidence.table_ids() == ["ads.a", "ads.b", "ads.c", "ads.e", "ads.d"]       # d 只补一次，三条路径都在
    assert len(evidence.joins) == 3 and {len(h.hops) for h in evidence.joins} == {2}


def test_join_hints_are_limited_in_number_and_the_note_says_so(monkeypatch):
    names = ["ads.a", "ads.b", "ads.c", "ads.d"]
    store, graph = make_store([(name, [("k", "键")]) for name in names],
                              relations=[relation("ads.a", ["k"], other, ["k"], "N:1") for other in names[1:]])
    monkeypatch.setattr(evidence_module, "MAX_JOIN_HINTS", 2)

    evidence = evidence_of(store, graph, recall_of(*[cand(name) for name in names]))

    assert [(h.left_table, h.right_table) for h in evidence.joins] == [("ads.a", "ads.b"), ("ads.a", "ads.c")]
    assert any("超过 2 条" in note for note in evidence.notes)


def test_a_path_cut_by_the_hint_limit_does_not_add_its_middle_table(monkeypatch):
    store, graph = chain_fixture()
    monkeypatch.setattr(evidence_module, "MAX_JOIN_HINTS", 1)

    evidence = evidence_of(store, graph, recall_of(cand("ads.a"), cand("ads.c"), cand("ads.d")))

    assert [(h.left_table, h.right_table) for h in evidence.joins] == [("ads.a", "ads.d")]
    assert evidence.table_ids() == ["ads.a", "ads.c", "ads.d"]


def test_tables_without_any_path_give_no_join_hint():
    store, graph = make_store([("ads.a", [("k", "键")]), ("ads.b", [("k", "键")])])

    assert evidence_of(store, graph, recall_of(cand("ads.a"), cand("ads.b"))).joins == []


# ── 上溯选项 ──────────────────────────────────────

def test_at_level_0_the_integration_layer_tables_behind_the_candidates_are_offered(mock_store, mock_graph):
    recall = recall_of(cand(ADS_DEP), cand(ADS_ORG), cand(CUST))

    evidence = evidence_of(mock_store, mock_graph, recall)

    assert evidence.upstream_options == [UpstreamOption(table_id=DEP_D, table_cn="对公存款账户日快照", layer="标准层")]
    assert evidence.table_ids() == [ADS_DEP, ADS_ORG, CUST]


def test_at_level_1_the_source_layer_tables_are_offered_without_repeating_those_in_the_evidence(mock_store, mock_graph):
    recall = recall_of(cand(ADS_DEP), cand(DEP_D, "upstream"), cand("ods.exch_rate", "upstream"), level=1)

    evidence = evidence_of(mock_store, mock_graph, recall)

    assert [option.table_id for option in evidence.upstream_options] == [
        "ods.ecif_corp_cust", "ods.industry_dict", "ods.core_dep_acct", "ods.gl_subject"]      # 贴源层的汇率表已经在证据里
    assert {option.layer for option in evidence.upstream_options} == {"贴源层"}


def test_no_upstream_options_from_level_2_without_lineage_in_all_layers_or_for_dimension_tables(mock_store, mock_graph):
    recall = recall_of(cand(ADS_DEP), cand(DEP_D, "upstream"), level=2)
    assert evidence_of(mock_store, mock_graph, recall).upstream_options == []
    assert evidence_of(mock_store, mock_graph, recall_of(cand(ADS_DEP)), lineage=False).upstream_options == []
    assert evidence_of(mock_store, mock_graph, recall_of(cand(ADS_DEP)), all_layers=True).upstream_options == []
    assert evidence_of(mock_store, mock_graph, recall_of(cand(CUST), cand(ORG))).upstream_options == []        # 维表不再往上找
    assert evidence_of(mock_store, mock_graph, recall_of(cand(CUST), level=1)).upstream_options == []


def test_upstream_options_are_limited_and_the_note_says_so(monkeypatch):
    names = [f"dwd.s{number:02d}" for number in range(1, 5)]
    store, graph = make_store([("ads.t", [("v", "指标")])] + [(name, [("v", "指标")]) for name in names],
                              edges=[edge("ads.t.v", f"{name}.v") for name in names])
    monkeypatch.setattr(evidence_module, "MAX_UPSTREAM_OPTIONS", 3)

    evidence = evidence_of(store, graph, recall_of(cand("ads.t")))

    assert [option.table_id for option in evidence.upstream_options] == names[:3]
    assert any("超过 3 张" in note for note in evidence.notes)


# ── 样例 ──────────────────────────────────────

def test_examples_are_looked_up_in_the_store_and_only_given_with_use_examples(mock_store, mock_graph):
    example = mock_store.examples()[0]
    example_hit = ExampleHit(example_id=example.example_id, requirement="召回时的需求", score=0.03, tables=[])
    recall = recall_of(cand(ADS_DEP), examples=[example_hit])

    with_examples = evidence_of(mock_store, mock_graph, recall, examples=True)
    without = evidence_of(mock_store, mock_graph, recall, examples=False)

    assert with_examples.examples == [EvidenceExample(
        example_id=example.example_id, requirement=example.requirement, caliber_note=example.caliber_note,
        sql_text=example.sql_text, tables=list(example.tables))]
    assert without.examples == [] and with_examples.use_examples and not without.use_examples


def test_an_example_unknown_to_the_store_is_skipped_with_a_note(mock_store, mock_graph):
    known = mock_store.examples()[1]
    hits = [ExampleHit(example_id="no.sql#9", requirement="x", score=0.02, tables=[]),
            ExampleHit(example_id=known.example_id, requirement="x", score=0.01, tables=[])]

    evidence = evidence_of(mock_store, mock_graph, recall_of(cand(ADS_DEP), examples=hits), examples=True)

    assert [item.example_id for item in evidence.examples] == [known.example_id]
    assert any("no.sql#9" in note for note in evidence.notes)


def test_use_examples_off_withholds_examples_but_dropping_their_tables_is_for_drop_examples(mock_store, mock_graph):
    example = mock_store.examples()[0]
    recall = recall_of(cand(ADS_DEP), cand(CUST, "example"),
                       examples=[ExampleHit(example_id=example.example_id, requirement="x", score=0.03, tables=[CUST])])

    kept = evidence_of(mock_store, mock_graph, recall, examples=False)
    dropped = evidence_of(mock_store, mock_graph, drop_examples(recall), examples=False)

    assert kept.table_ids() == [ADS_DEP, CUST] and kept.examples == []
    assert dropped.table_ids() == [ADS_DEP] and dropped.examples == []


def test_drop_examples_removes_the_examples_and_the_tables_they_brought_in():
    example_hit = ExampleHit(example_id="a.sql#1", requirement="x", score=0.03, tables=["ods.z"])
    recall = RecallResult(scope_level=1, scope_size=9, notes=["说明"], examples=[example_hit],
                          tables=[cand("ads.a", "recall"), cand("dwd.b", "upstream"), cand("ods.z", "example")])

    dropped = drop_examples(recall)

    assert [(t.table_id, t.reason) for t in dropped.tables] == [("ads.a", "recall"), ("dwd.b", "upstream")]
    assert dropped.examples == []
    assert (dropped.scope_level, dropped.scope_size, dropped.notes) == (1, 9, ["说明"])
    assert len(recall.tables) == 3 and recall.examples == [example_hit]           # 传进来的对象没改
    assert dropped.notes is not recall.notes and dropped.tables[0] is recall.tables[0]


# ── 说明 ──────────────────────────────────────

def test_notes_start_with_the_recall_notes_and_truncation_notes_follow(monkeypatch):
    names = [f"m{number}" for number in range(3)]
    store, graph = make_store([("ads.t", [(name, name) for name in names])])
    hits = [hit("ads.t", name, "余额", 0.05 - number * 0.001) for number, name in enumerate(names)]
    monkeypatch.setattr(evidence_module, "MAX_LINEAGE_FIELDS", 2)
    recall = recall_of(cand("ads.t", hits=hits), notes=["检索词「机构」没有命中任何字段"])

    evidence = evidence_of(store, graph, recall, ["余额"])

    assert evidence.notes[0] == "检索词「机构」没有命中任何字段"
    assert len(evidence.notes) == 2 and "前 2 个" in evidence.notes[1]
    assert recall.notes == ["检索词「机构」没有命中任何字段"]                       # 传进来的说明没被追加


# ── 结构图 ──────────────────────────────────────

def test_structure_store_has_tables_columns_and_structural_relations_only(mock_store):
    structure = structure_store(mock_store)

    assert [t.table_id for t in structure.tables()] == [t.table_id for t in mock_store.tables()]
    assert all(structure.columns(t.table_id) == mock_store.columns(t.table_id) for t in mock_store.tables())
    assert {r.origin for r in structure.relations()} == {"manual", "grain_key"}
    assert structure.lineage_edges() == [] and structure.scripts() == [] and structure.examples() == []
    assert all(structure.script_columns(t.table_id) == [] for t in mock_store.tables())
    assert structure.vectors("field") is None and structure.vectors("example") is None
    assert structure.embedder_name is None
    counts = structure.counts()
    assert (counts["血缘"], counts["加工语句"], counts["样例"], counts["字段向量"]) == (0, 0, 0, 0)
    assert (counts["表"], counts["字段"]) == (mock_store.counts()["表"], mock_store.counts()["字段"])


def test_structure_store_does_not_touch_the_original_store(mock_store):
    before = mock_store.counts()

    structure_store(mock_store)

    assert mock_store.counts() == before and before["血缘"] > 0 and before["样例"] > 0


def test_structure_store_recomputes_what_the_stored_origins_hide(mock_store, mock_graph):
    structure = LineageGraph(structure_store(mock_store))

    assert [r.origin for r in mock_graph.relations_between(ADS_DEP, CUST)] == ["lineage"]
    assert [(r.origin, r.cardinality) for r in structure.relations_between(ADS_DEP, CUST)] == [("grain_key", "N:1")]
    assert structure.join_path(ADS_DEP, CUST).steps[0].origin == "grain_key"


# ── 模拟专题库上的验收 ──────────────────────────────────────

TERMS = {item["id"]: item for item in yaml.safe_load(RECALL_TERMS.read_text(encoding="utf-8"))}
SQL_QUESTIONS = [question for question in load_questions() if question.expect == "sql"]


def question_recall(retriever, question) -> tuple[RecallQuery, RecallResult]:
    """用手写的检索词召回；向上溯源的题先在第 0 层召回，再以第 0 层的候选表为起点上溯"""
    spec = TERMS[question.id]
    query = RecallQuery(spec.get("requirement", question.turns[0]), spec.get("metrics", []),
                        spec.get("dimensions", []), spec.get("filters", []))
    result = retriever.recall(query)
    if spec.get("level", 0):
        result = retriever.recall(query, spec["level"], [item.table_id for item in result.tables])
    return query, result


@pytest.fixture(scope="module")
def mock_recalls(mock_retriever) -> dict[str, tuple[RecallQuery, RecallResult]]:
    """37 道 SQL 题各自的检索词和召回结果"""
    return {question.id: question_recall(mock_retriever, question) for question in SQL_QUESTIONS}


def build_all(mock_store, mock_graph, mock_recalls, **options) -> dict[str, Evidence]:
    return {qid: build_evidence(mock_store, mock_graph, recall, query, **options)
            for qid, (query, recall) in mock_recalls.items()}


def test_gold_tables_of_all_37_questions_reach_the_evidence(mock_store, mock_graph, mock_recalls):
    evidences = build_all(mock_store, mock_graph, mock_recalls, use_lineage=True, use_examples=True)

    assert len(evidences) == 37
    for question in SQL_QUESTIONS:
        ids = set(evidences[question.id].table_ids())
        assert set(question.gold_tables) <= ids, question.id
        assert {item.table_id for item in mock_recalls[question.id][1].tables} <= ids, question.id    # 证据的表包含召回的表


def test_the_evidence_of_37_questions_takes_under_two_seconds(mock_store, mock_graph, mock_recalls):
    timings = []
    for _ in range(2):                                                          # 取较快的一遍，别让别的进程抢 CPU 影响判断
        started = time.perf_counter()
        build_all(mock_store, mock_graph, mock_recalls, use_lineage=True, use_examples=True)
        timings.append(time.perf_counter() - started)

    assert min(timings) < 2.0, timings


def test_every_evidence_of_the_37_questions_is_consistent(mock_store, mock_graph, mock_recalls):
    for qid, evidence in build_all(mock_store, mock_graph, mock_recalls, use_lineage=True, use_examples=True).items():
        ids = evidence.table_ids()
        assert len(ids) == len(set(ids)), qid                                      # 一张表只出现一次
        assert all(table.columns for table in evidence.tables), qid
        assert len(evidence.lineage) <= MAX_LINEAGE_TOTAL, qid
        assert len({item.field for item in evidence.lineage}) == len(evidence.lineage), qid
        for item in evidence.lineage:
            table, column = item.field.rsplit(".", 1)
            assert column in evidence.column_names(table), (qid, item.field)         # 展开血缘的字段都在候选表里
        for hint in evidence.joins:
            assert {hint.left_table, hint.right_table} <= set(ids), qid
            assert all(hop.left_table in ids and hop.right_table in ids for hop in hint.hops), qid
        assert len(evidence.joins) <= MAX_JOIN_HINTS, qid
        assert [t.reason for t in evidence.tables].count("join") <= MAX_JOIN_TABLES, qid
        assert len(evidence.upstream_options) <= MAX_UPSTREAM_OPTIONS, qid


def test_anchor_4_upstream_questions_reach_the_integration_layer_and_carry_the_processing_joins(
        mock_store, mock_graph, mock_recalls):
    evidences = build_all(mock_store, mock_graph, mock_recalls, use_lineage=True, use_examples=True)

    for qid in ("U01", "U04"):
        assert DEP_D in evidences[qid].table_ids(), qid
    for qid in ("U02", "U03"):
        assert LOAN_D in evidences[qid].table_ids(), qid
    u01 = evidences["U01"]
    assert u01.scope_level == 1
    ads_steps = [step for item in u01.lineage if item.field.startswith("ads.") for step in item.steps]
    ads_joins = [text for step in ads_steps for text in step.joins]
    assert "JOIN dim.corp_cust ON dwd.corp_dep_acct_d.cust_no = dim.corp_cust.cust_no" in ads_joins     # 上溯时要还原的加工条件
    ads_filters = [text for step in ads_steps for text in step.filters]
    assert "WHERE dwd.corp_dep_acct_d.data_dt = CAST('${data_dt}' AS DATE)" in ads_filters
    assert [option.table_id for option in evidences["U05"].upstream_options] == []         # 已经是第 2 层


# ── 确定性 ──────────────────────────────────────

def test_the_same_input_gives_the_same_evidence_and_leaves_the_input_alone(mock_store, mock_graph, mock_recalls):
    for qid in ("B01", "D01", "K03", "U01", "F04"):
        query, recall = mock_recalls[qid]
        before = copy.deepcopy((query, recall))
        first = build_evidence(mock_store, mock_graph, recall, query, use_lineage=True, use_examples=True)
        second = build_evidence(mock_store, mock_graph, recall, query, use_lineage=True, use_examples=True)

        assert first.model_dump() == second.model_dump(), qid
        assert (query, recall) == before, qid


# 子进程里跑的脚本：37 道题的证据（A 组和 C 组各一份）打印成 JSON。换 PYTHONHASHSEED 重复跑，结果必须逐字相同
CHILD_SCRIPT = """
import json, os, sys
os.environ["METADATA_EMBEDDER"] = "hash"
from pathlib import Path
import yaml
from eval.questions import load_questions
from lineage.graph import LineageGraph
from metadata.importer import enrich_mart, load_mart
from metadata.store import MetadataStore
from nl2sql.evidence import build_evidence, drop_examples, structure_store
from nl2sql.retriever import RecallQuery, Retriever

root = Path(sys.argv[1])
mart, _ = load_mart(root)
mart, _, _ = enrich_mart(mart, root)
store = MetadataStore.from_mart(mart)
graph = LineageGraph(store)
structure = LineageGraph(structure_store(store))
retriever = Retriever(store, graph, None)
terms = {item["id"]: item for item in yaml.safe_load(Path(sys.argv[2]).read_text(encoding="utf-8"))}
out = []
for question in load_questions():
    if question.expect != "sql":
        continue
    spec = terms[question.id]
    query = RecallQuery(spec.get("requirement", question.turns[0]), spec.get("metrics", []),
                        spec.get("dimensions", []), spec.get("filters", []))
    recall = retriever.recall(query)
    if spec.get("level", 0):
        recall = retriever.recall(query, spec["level"], [item.table_id for item in recall.tables])
    out.append(build_evidence(store, graph, recall, query, use_lineage=True, use_examples=True).model_dump())
    flat = drop_examples(retriever.recall(query, all_layers=True))
    out.append(build_evidence(store, graph, flat, query, use_lineage=False, use_examples=False, all_layers=True,
                              join_graph=structure).model_dump())
print(json.dumps(out, ensure_ascii=True))
"""


def test_evidence_does_not_depend_on_the_hash_seed():
    children = [subprocess.Popen([sys.executable, "-c", CHILD_SCRIPT, str(MOCK_MART_DIR), str(RECALL_TERMS)], cwd=ROOT,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                                 env={**os.environ, "PYTHONHASHSEED": seed}) for seed in ("1", "2")]
    outputs = []
    for child in children:                                                      # 两个子进程同时跑，省一半时间
        stdout, stderr = child.communicate()
        assert child.returncode == 0, stderr
        outputs.append(stdout)
    assert outputs[0] == outputs[1]
    assert len(json.loads(outputs[0])) == 74
