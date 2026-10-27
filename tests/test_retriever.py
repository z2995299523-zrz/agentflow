"""候选召回的测试：三层范围、上游表和样例带入的表不占名额、二元组关键字匹配、两路 RRF 合并、
通用字段与恒为空字段的排除、向量器不一致时的退化、同分时的先后、结果确定，以及模拟题的召回率

全部用哈希向量器，不加载 BGE，也不引用真实专题库。
"""
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
import yaml

from eval.questions import load_questions
from lineage.graph import LineageGraph
from metadata.importer import enrich_mart, load_mart
from metadata.index import HashEmbedder, build_vectors
from metadata.models import ColumnMeta, Example, LineageEdge, Mart, ScriptColumn, TableMeta
from metadata.store import MetadataStore
from nl2sql import retriever as retriever_module
from nl2sql.retriever import (
    MAX_EXAMPLES,
    MAX_EXTRA_TABLES,
    MAX_HITS_PER_TABLE,
    MAX_TABLES,
    MAX_UPSTREAM_TABLES,
    MISSING_TABLES_PER_TERM,
    RecallQuery,
    Retriever,
)

ROOT = Path(__file__).resolve().parents[1]
MOCK_MART_DIR = ROOT / "data" / "mock_mart"
RECALL_TERMS = Path(__file__).parent / "data" / "recall_terms.yaml"
MIN_RECALLED_QUESTIONS = 34          # 37 道出 SQL 的题里，标准用表全部进了候选的题数不能低于它
SCHEMA_LAYER = {"ads": "专题层", "zt": "专题层", "dwd": "整合层", "dim": "整合层", "ods": "贴源层"}

# 子进程里跑的脚本：在模拟库上召回两次，把结果打印成 JSON。换 PYTHONHASHSEED 重复跑，结果必须逐字相同
CHILD_SCRIPT = """
import json, os, sys
os.environ["METADATA_EMBEDDER"] = "hash"
from dataclasses import asdict
from pathlib import Path
from lineage.graph import LineageGraph
from metadata.importer import enrich_mart, load_mart
from metadata.index import HashEmbedder, build_vectors
from metadata.store import MetadataStore
from nl2sql.retriever import RecallQuery, Retriever

root = Path(sys.argv[1])
mart, _ = load_mart(root)
mart, _, _ = enrich_mart(mart, root)
embedder = HashEmbedder()
store = MetadataStore.from_mart(mart, build_vectors(mart, embedder)[0])
retriever = Retriever(store, LineageGraph(store), embedder)
query = RecallQuery("2026 年 6 月末对公存款账户按币种统计账户数和折人民币余额", ["对公存款余额", "账户数"], ["币种代码"], ["客户号"])
first = retriever.recall(query)
second = retriever.recall(query, 1, [item.table_id for item in first.tables])
print(json.dumps([asdict(first), asdict(second)], ensure_ascii=True))
"""


# ── 构造资料的工具 ──────────────────────────────────────

def make_mart(tables: list[tuple], edges: list[LineageEdge] = (), script_columns: list[ScriptColumn] = (),
              examples: list[Example] = ()) -> Mart:
    """手写的小资料：tables 是 (表标识, 表中文名, [(字段名, 字段中文名)], 表类型, 数据日期字段)，后两项可省"""
    metas, columns = [], []
    for table_id, table_cn, fields, *rest in tables:
        schema, name = table_id.split(".")
        table_type = rest[0] if rest else ("维表" if schema == "dim" else "快照表")
        metas.append(TableMeta(table_id=table_id, layer=SCHEMA_LAYER[schema], schema_name=schema, table_name=name,
                               table_cn=table_cn, table_type=table_type, grain_columns=[],
                               date_column=rest[1] if len(rest) > 1 else None))
        columns += [ColumnMeta(table_id=table_id, column_name=column, ordinal=number, column_cn=column_cn)
                    for number, (column, column_cn) in enumerate(fields, 1)]
    return Mart(tables=metas, columns=columns, lineage=list(edges), script_columns=list(script_columns),
                examples=list(examples))


def make_store(mart: Mart, with_vectors: bool = True) -> tuple[MetadataStore, LineageGraph]:
    """由资料建只读入口和血缘图；向量由哈希向量器算"""
    store = MetadataStore.from_mart(mart, build_vectors(mart, HashEmbedder())[0] if with_vectors else None)
    return store, LineageGraph(store)


def make_retriever(mart: Mart, with_embedder: bool = True) -> Retriever:
    """哈希向量建库、哈希向量器检索；with_embedder 为 False 时不传向量器，只走关键字路"""
    store, graph = make_store(mart)
    return Retriever(store, graph, HashEmbedder() if with_embedder else None)


def hit_names(result) -> set[str]:
    """候选表里所有命中字段的字段名"""
    return {hit.column_name for item in result.tables for hit in item.hits}


@pytest.fixture(scope="module")
def mock_mart() -> Mart:
    """模拟专题库补全之后（41 条关联、12 条样例）的资料"""
    mart, _ = load_mart(MOCK_MART_DIR)
    return enrich_mart(mart, MOCK_MART_DIR)[0]


@pytest.fixture(scope="module")
def mock_parts(mock_mart) -> tuple[MetadataStore, LineageGraph]:
    """模拟专题库带哈希向量的只读入口和血缘图"""
    return make_store(mock_mart)


@pytest.fixture(scope="module")
def mock_retriever(mock_parts) -> Retriever:
    return Retriever(*mock_parts, HashEmbedder())


# ── 范围 ──────────────────────────────────────

def test_scope_grows_with_the_upstream_layers(mock_retriever):
    base = ["ads.corp_cust_loan_bal_d"]
    level0, upstream0 = mock_retriever.scope_tables(0)
    assert level0 == ["ads.corp_cust_dep_bal_d", "ads.corp_cust_loan_bal_d", "ads.org_corp_dep_bal_d",
                      "dim.corp_cust", "dim.org"]                      # 专题层全部表加整合层的维表
    assert upstream0 == []

    level1, upstream1 = mock_retriever.scope_tables(1, base)
    assert upstream1 == ["dim.corp_cust", "dwd.corp_loan_duebill_d"]    # 只有整合层，且只有这张贷款表的上游
    assert level1 == level0 + ["dwd.corp_loan_duebill_d"]              # 维表本来就在第 0 层里，不重复

    level2, upstream2 = mock_retriever.scope_tables(2, base)
    assert upstream2[:2] == upstream1                                      # 近的在前：整合层的两张
    assert set(upstream2[2:]) == {"ods.ecif_corp_cust", "ods.industry_dict", "ods.core_loan_duebill",
                                  "ods.exch_rate"}                         # 再加贴源层
    assert not {"ods.core_dep_acct", "dwd.corp_dep_acct_d"} & set(level2)  # 存款那条线不在这张表的上游里
    assert level2 == level0 + upstream2[1:]


def test_dimension_tables_are_not_climbed_and_bad_arguments_are_rejected(mock_retriever):
    assert mock_retriever.scope_tables(2, ["dim.org"]) == (mock_retriever.scope_tables(0)[0], [])
    for level in (1, 2):
        with pytest.raises(ValueError, match="base_tables"):
            mock_retriever.recall(RecallQuery("x", ["存款余额"]), level)
    with pytest.raises(ValueError):
        mock_retriever.scope_tables(3, ["ads.org_corp_dep_bal_d"])


# ── 名额 ──────────────────────────────────────

def ten_table_mart(carriers: int = 10) -> Mart:
    """10 张专题层表、1 张整合层上游表、1 张贴源层上游表、1 张只有样例用到的贴源层表

    t01 有 7 个字段都对得上「存款金额」，其余每张表 1 个（名字一样长，同分时按字段编号，t01 排在最前）；
    carriers 张表带样板字段「时间-年」。
    """
    tables = []
    for number in range(1, 11):
        fields = [("org_no", "机构号")]
        if number <= carriers:
            fields.append(("time_year", "时间-年"))
        fields += ([(f"amt_{i}", f"存款金额{cn}") for i, cn in enumerate("一二三四五六七", 1)]
                   if number == 1 else [("amt", "存款金额一")])
        tables.append((f"ads.t{number:02d}", "存款汇总", fields))
    tables += [("dwd.src", "存款明细", [("amt", "存款金额")]), ("ods.raw", "存款原始", [("amt", "存款金额")]),
               ("ods.other", "其他", [("amt", "存款金额")])]
    edges = [LineageEdge(target_table="ads.t01", target_column="amt_1", source_table="dwd.src", source_column="amt"),
             LineageEdge(target_table="dwd.src", target_column="amt", source_table="ods.raw", source_column="amt")]
    example = Example(example_id="demo.sql#1", requirement="存款金额汇总统计", sql_text="SELECT 1",
                      tables=["ods.other"], source_file="demo.sql", line_no=1)
    return make_mart(tables, edges, examples=[example])


def test_at_most_eight_tables_and_upstream_and_example_tables_take_no_slot():
    retriever = make_retriever(ten_table_mart())
    result = retriever.recall(RecallQuery("存款金额汇总统计", ["存款金额"]), 1, ["ads.t01"])
    assert [item.reason for item in result.tables] == ["recall"] * MAX_TABLES + ["upstream", "example"]
    assert result.tables[-2].table_id == "dwd.src" and result.tables[-2].hits     # 上游表在自己的范围里找命中字段
    assert result.tables[-1].table_id == "ods.other" and result.tables[-1].hits == []
    assert result.examples[0].example_id == "demo.sql#1"
    scores = [item.score for item in result.tables[:MAX_TABLES]]
    assert scores == sorted(scores, reverse=True)
    assert max(len(item.hits) for item in result.tables) == MAX_HITS_PER_TABLE    # t01 有 7 个字段命中，只列 5 个
    # 上溯只往后追加：专题层这 8 张表的先后和得分，与不上溯时完全一样
    level0 = retriever.recall(RecallQuery("存款金额汇总统计", ["存款金额"]))
    assert [(item.table_id, item.score) for item in result.tables[:MAX_TABLES]] == [
        (item.table_id, item.score) for item in level0.tables if item.reason == "recall"]
    assert result.scope_size == 11 and result.scope_level == 1


def test_tables_of_the_previous_round_are_carried_in_without_taking_a_slot():
    retriever = make_retriever(ten_table_mart())
    query = RecallQuery("存款金额汇总统计", ["存款金额"])
    recalled = [item.table_id for item in retriever.recall(query).tables if item.reason == "recall"]

    result = retriever.recall(query, carry_tables=["dwd.src", recalled[0], "ads.nope", "dwd.src"])

    reasons = [item.reason for item in result.tables]
    assert [item.table_id for item in result.tables if item.reason == "previous"] == ["dwd.src"]  # 已在候选里的、没有的、重复的跳过
    assert reasons.count("recall") == MAX_TABLES and reasons.index("previous") == MAX_TABLES      # 不占名额，紧跟召回的表
    assert next(item for item in result.tables if item.table_id == "dwd.src").hits == []          # 第 0 层范围外，没有命中


def standard_tables() -> list[tuple]:
    """extra_mart 里的 10 张标准表"""
    return [(f"ads.t{number:02d}", "存款汇总", [(f"amt_t{number}", "存款金额一")]) for number in range(1, 11)]


def extra_mart() -> Mart:
    """10 张标准表各有 1 个字段「存款金额一」；4 张非标准表各有 1 个字段，中文名正好是「存款金额」，
    和标准表一起排名时能挤进前 8；还有 1 张非标准表只有「备注」

    标准表的字段名各不相同：同一层里超过一半的表都有的字段名会被当成样板字段，不参与计分
    """
    tables = standard_tables()
    tables += [(f"ads.x{number}", "存款实库表", [("amt", "存款金额")]) for number in range(1, 5)]
    tables.append(("ads.x9", "其他实库表", [("note", "备注")]))
    mart = make_mart(tables)
    return mart.model_copy(update={"tables": [table.model_copy(update={"standard": table.table_id.startswith("ads.t")})
                                              for table in mart.tables]})


def test_non_standard_tables_rank_on_their_own_and_take_no_slot():
    mart = extra_mart()
    retriever = make_retriever(mart)
    query = RecallQuery("存款金额汇总统计", ["存款金额"])

    result = retriever.recall(query)

    assert retriever.scope_tables(0)[0] == [f"ads.t{number:02d}" for number in range(1, 11)]     # 第 0 层只有标准表
    assert [item.reason for item in result.tables] == ["recall"] * MAX_TABLES + ["extra"] * MAX_EXTRA_TABLES
    extras = result.tables[MAX_TABLES:]
    assert {item.table_id for item in extras} < {"ads.x1", "ads.x2", "ads.x3", "ads.x4"}           # 只要有命中的
    assert all(item.hits for item in extras)
    # 标准表的先后和得分，和元数据里没有非标准表时完全一样
    standard_only = make_retriever(make_mart(standard_tables()))
    assert [(item.table_id, item.score) for item in result.tables[:MAX_TABLES]] == [
        (item.table_id, item.score) for item in standard_only.recall(query).tables]
    # 对照实验 A 组：全部表一起排名，不分是不是标准表，非标准表照样占名额，没有 extra
    group_a = retriever.recall(query, all_layers=True)
    assert any(item.table_id.startswith("ads.x") for item in group_a.tables[:MAX_TABLES])
    assert {item.reason for item in group_a.tables} == {"recall"}


def test_a_term_that_only_hits_non_standard_tables_is_not_reported_as_missing():
    retriever = make_retriever(extra_mart(), with_embedder=False)

    result = retriever.recall(RecallQuery("看一下备注", ["备注"]))

    assert [(item.table_id, item.reason) for item in result.tables] == [("ads.x9", "extra")]
    assert not any("没有命中任何字段" in note for note in result.notes)


def bridge_mart() -> Mart:
    """十张客户经理统计表把召回的 8 个名额占满；贷款合同和客户之间的关系表，靠「客户经理数」检索不到"""
    tables = [(f"ads.m{number:02d}", "客户经理统计", [("cnt", "客户经理数")]) for number in range(1, 11)]
    tables += [("ads.rel", "贷款合同客户关系", [("contract_no", "合同号"), ("cust_no", "客户号")]),
               ("ads.ctr", "贷款合同", [("contract_no", "合同号")]),
               ("ads.cst", "客户信息", [("cust_no", "客户号")])]
    return make_mart(tables)


def test_each_missing_item_is_recalled_on_its_own_and_takes_no_slot():
    retriever = make_retriever(bridge_mart())
    query = RecallQuery("各客户的客户经理数", ["客户经理数"])
    assert "ads.rel" not in [item.table_id for item in retriever.recall(query).tables]

    result = retriever.recall(query, missing_terms=["合同与客户的关联关系"])

    missing = [item for item in result.tables if item.reason == "missing"]
    assert missing[0].table_id == "ads.rel" and len(missing) == MISSING_TABLES_PER_TERM
    assert missing[0].hits and missing[0].hits[0].term == "合同与客户的关联关系"          # 标出是哪一样缺的东西召回来的
    assert [item.reason for item in result.tables].count("recall") == MAX_TABLES


def test_missing_items_are_capped_never_repeat_and_are_skipped_for_group_a(monkeypatch):
    monkeypatch.setattr(retriever_module, "MAX_MISSING_TABLES", 2)
    retriever = make_retriever(bridge_mart())
    query = RecallQuery("各客户的客户经理数", ["客户经理数"])
    many = ["合同与客户的关联关系", "贷款合同", "客户信息", "合同号", "客户号"]

    result = retriever.recall(query, missing_terms=many)

    ids = [item.table_id for item in result.tables]
    missing = [item for item in result.tables if item.reason == "missing"]
    assert len(missing) == 2 and len(ids) == len(set(ids))
    assert [item.hits[0].term for item in missing] == many[:2]          # 轮流取：前两条各补一张，不是第一条补两张
    assert not [item for item in retriever.recall(query, all_layers=True, missing_terms=many).tables
                if item.reason == "missing"]


def upstream_mart(strong: int, weak: int, unrelated: int) -> Mart:
    """专题层 ads.t01 直接依赖整合层的 strong + weak + unrelated 张表：
    s 开头的字段名与「存款金额」完全一致，w 开头的只对上「存款」，n 开头的完全对不上"""
    tables = [("ads.t01", "存款汇总", [("amt", "存款金额")])]
    edges = []
    for prefix, count, name in (("s", strong, "存款金额"), ("w", weak, "存款"), ("n", unrelated, "机构编号")):
        for number in range(1, count + 1):
            tables.append((f"dwd.{prefix}{number:02d}", "明细", [("amt", name)]))
            edges.append(LineageEdge(target_table="ads.t01", target_column="amt",
                                     source_table=f"dwd.{prefix}{number:02d}", source_column="amt"))
    return make_mart(tables, edges)


def upstream_tables(result) -> list[str]:
    """召回结果里上溯加入的表"""
    return [item.table_id for item in result.tables if item.reason == "upstream"]


def test_upstream_tables_are_capped_and_only_those_with_hits_are_kept():
    query = RecallQuery("x", ["存款金额"])
    many = make_retriever(upstream_mart(strong=5, weak=6, unrelated=3), with_embedder=False).recall(query, 1, ["ads.t01"])
    few = make_retriever(upstream_mart(strong=2, weak=1, unrelated=6), with_embedder=False).recall(query, 1, ["ads.t01"])

    # 11 张有命中字段：按得分取前 8，得分相同的字段名一致，按名次先后；对不上的一张不要
    assert upstream_tables(many) == [f"dwd.s0{i}" for i in range(1, 6)] + [f"dwd.w0{i}" for i in range(1, 4)]
    assert len(upstream_tables(many)) == MAX_UPSTREAM_TABLES
    scores = [item.score for item in many.tables if item.reason == "upstream"]
    assert scores == sorted(scores, reverse=True) and all(item.hits for item in many.tables)
    assert [item.table_id for item in many.tables if item.reason == "recall"] == ["ads.t01"]     # 上游表不占专题层的名额
    # 只有 3 张有命中字段：就只留这 3 张，不拿没命中的凑数
    assert upstream_tables(few) == ["dwd.s01", "dwd.s02", "dwd.w01"]
    assert many.scope_size == 1 + 14                      # 范围里仍是整个闭包，名额只管最后留下几张候选


def test_nothing_matching_upstream_keeps_the_nearest_eight_instead_of_none():
    """没有一张上游表命中时无从比较，取闭包里最近的 8 张：z1 是直接上游，a1..a9 隔了一层，字母顺序在 z1 前面"""
    tables = [("ads.t01", "存款汇总", [("amt", "存款金额")]), ("dwd.z1", "明细", [("amt", "机构编号")])]
    edges = [LineageEdge(target_table="ads.t01", target_column="amt", source_table="dwd.z1", source_column="amt")]
    for number in range(1, 10):
        tables.append((f"dwd.a{number}", "明细", [("amt", "机构编号")]))
        edges.append(LineageEdge(target_table="dwd.z1", target_column="amt",
                                 source_table=f"dwd.a{number}", source_column="amt"))
    retriever = make_retriever(make_mart(tables, edges), with_embedder=False)

    result = retriever.recall(RecallQuery("x", ["存款金额"]), 1, ["ads.t01"])
    no_terms = retriever.recall(RecallQuery("x", []), 2, ["ads.t01"])

    expected = ["dwd.z1"] + [f"dwd.a{i}" for i in range(1, 8)]
    assert upstream_tables(result) == upstream_tables(no_terms) == expected
    assert all(item.hits == [] for item in result.tables if item.reason == "upstream")
    assert upstream_tables(retriever.recall(RecallQuery("x", ["存款金额"]), 1, ["ads.t01"])) == expected   # 重复调用结果一样


def test_a_dimension_table_that_misses_the_upstream_cut_goes_back_to_the_ordinary_pool():
    """维表本来就在第 0 层：作为上游表被淘汰以后，仍然像第 0 层那样参与专题层的排名，不会因为上溯反而丢掉"""
    tables = [("ads.t01", "存款汇总", [("amt", "存款金额")]), ("dim.d1", "客户维表", [("nm", "存款金额项目")])]
    edges = [LineageEdge(target_table="ads.t01", target_column="amt", source_table="dim.d1", source_column="nm")]
    for number in range(1, 10):
        tables.append((f"dwd.s0{number}", "明细", [("amt", "存款金额")]))
        edges.append(LineageEdge(target_table="ads.t01", target_column="amt",
                                 source_table=f"dwd.s0{number}", source_column="amt"))
    retriever = make_retriever(make_mart(tables, edges), with_embedder=False)

    result = retriever.recall(RecallQuery("x", ["存款金额"]), 1, ["ads.t01"])
    reasons = {item.table_id: item.reason for item in result.tables}

    assert upstream_tables(result) == [f"dwd.s0{i}" for i in range(1, 9)]       # 维表的字段名更长，排在 9 张之后
    assert "dwd.s09" not in reasons                                              # 非维表被淘汰就不再出现
    assert reasons["dim.d1"] == "recall" and reasons["ads.t01"] == "recall"


# ── 关键字路 ──────────────────────────────────────

def test_keyword_route_ranks_by_bigram_coverage_not_by_substring():
    mart = make_mart([("ads.loan", "贷款汇总", [("a", "贷款余额"), ("b", "贷款利率"), ("c", "存款余额")])])
    result = make_retriever(mart, with_embedder=False).recall(RecallQuery("x", ["贷款余额"]))
    hits = result.tables[0].hits
    assert [hit.column_name for hit in hits] == ["a", "c", "b"]         # 覆盖 3/3 加子串分、2/3、1/3
    assert all(hit.via == ("keyword",) for hit in hits)


def test_keyword_route_matches_english_words_after_nfkc():
    mart = make_mart([("ads.acct", "账户汇总", [("org_no", "机构号"), ("cust_no", "客户号"), ("bal", "余额")])])
    retriever = make_retriever(mart, with_embedder=False)
    for term in ("ORG_NO", "ＯＲＧ＿ＮＯ"):                                # 大写、全角都归一成 org_no
        hits = retriever.recall(RecallQuery("x", [term])).tables[0].hits
        assert hits[0].column_name == "org_no" and hits[0].via == ("keyword",)


# ── 两路合并 ──────────────────────────────────────

def test_two_routes_are_merged_with_rrf_and_role_weights(mock_parts):
    store, graph = mock_parts
    both, keyword_only = Retriever(store, graph, HashEmbedder()), Retriever(store, graph, None)
    for retriever, routes, expected in ((both, ("vector", "keyword"), 2 / 61), (keyword_only, ("keyword",), 1 / 61)):
        for role, weight in (("metrics", 1.0), ("dimensions", 0.7), ("filters", 0.5)):
            query = RecallQuery("x", **{"metrics": [], role: ["不良贷款余额"]})
            top = retriever.recall(query).tables[0].hits[0]
            assert (top.column_name, top.via) == ("npl_bal_rmb", routes)    # 两路都排第一：1/61 + 1/61
            assert top.score == pytest.approx(weight * expected)


# ── 排除 ──────────────────────────────────────

def test_columns_that_are_always_null_or_etl_time_are_not_recalled():
    tables = [("ads.bal", "余额汇总", [("bal_a", "存款余额"), ("bal_b", "存款余额备用"), ("load_tm", "存款加工时间")])]
    kinds = {"bal_a": "null", "bal_b": "derived", "load_tm": "etl_time"}
    script_columns = [ScriptColumn(script_name="s.sql", stmt_index=1, target_table="ads.bal", target_column=column,
                                   expression="x", kind=kind) for column, kind in kinds.items()]
    query = RecallQuery("x", ["存款余额"])
    assert hit_names(make_retriever(make_mart(tables)).recall(query)) == {"bal_a", "bal_b", "load_tm"}
    result = make_retriever(make_mart(tables, script_columns=script_columns)).recall(query)
    assert hit_names(result) == {"bal_b"}


def test_generic_fields_only_count_when_the_term_names_them():
    fields = [("data_dt", "数据日期"), ("etl_dt", "数据日期"), ("id", "主键"), ("dep_amt", "存款金额"), ("note", "备注")]
    retriever = make_retriever(make_mart([("ads.dep", "客户存款汇总", fields, "快照表", "data_dt")]))
    # 检索词恰好是表名：表名给每个字段都加分，但数据日期（登记的、只靠中文名认出的）和通用键名不计分
    assert hit_names(retriever.recall(RecallQuery("x", ["客户存款汇总"]))) == {"dep_amt", "note"}
    assert {"data_dt", "etl_dt"} <= hit_names(retriever.recall(RecallQuery("x", ["数据日期"])))
    assert "id" in hit_names(retriever.recall(RecallQuery("x", ["主键"])))


def test_boilerplate_fields_of_a_layer_are_treated_as_generic():
    query = RecallQuery("存款金额汇总统计", ["存款金额"])
    everywhere = make_retriever(ten_table_mart(carriers=10)).recall(query)    # 10 张专题层表全都有「时间-年」
    assert "time_year" not in hit_names(everywhere)
    few = make_retriever(ten_table_mart(carriers=5)).recall(query)            # 只有一半的表有，达不到样板的标准
    assert "time_year" in hit_names(few)


# ── 向量器 ──────────────────────────────────────

def test_a_different_embedder_with_the_same_dimension_is_never_mixed_in(mock_parts):
    store, graph = mock_parts
    query = RecallQuery("x", ["不良贷款余额"])
    other = HashEmbedder()
    other.name = "other-512"                                               # 同为 512 维，混用不会报错
    result = Retriever(store, graph, other).recall(query)
    assert result.notes and "hash-bigram-512" in result.notes[0] and "other-512" in result.notes[0]
    assert {hit.via for item in result.tables for hit in item.hits} == {("keyword",)}
    assert any("vector" in hit.via for item in Retriever(store, graph, HashEmbedder()).recall(query).tables
               for hit in item.hits)


def test_without_vectors_or_embedder_only_the_keyword_route_runs_and_says_why(mock_parts, mock_mart):
    store, graph = mock_parts
    query = RecallQuery("x", ["不良贷款余额"])
    no_embedder = Retriever(store, graph, None).recall(query)
    assert "没有传入向量器" in no_embedder.notes[0]
    bare_store, bare_graph = make_store(mock_mart, with_vectors=False)
    no_vectors = Retriever(bare_store, bare_graph, HashEmbedder()).recall(query)
    assert "没有字段向量" in no_vectors.notes[0]
    for result in (no_embedder, no_vectors):
        assert {hit.via for item in result.tables for hit in item.hits} == {("keyword",)}


# ── 样例 ──────────────────────────────────────

def test_similar_examples_bring_their_tables_in(mock_parts):
    store, graph = mock_parts
    example = next(item for item in store.examples() if item.example_id == "03_上溯整合层.sql#1")
    query = RecallQuery(example.requirement, ["折人民币余额"], ["币种代码"])
    for embedder in (HashEmbedder(), None):                                # 没有向量时只用 Jaccard，也能找到
        result = Retriever(store, graph, embedder).recall(query)
        assert result.examples[0].example_id == example.example_id
        assert len(result.examples) <= MAX_EXAMPLES
        brought = next(item for item in result.tables if item.table_id == "dwd.corp_dep_acct_d")
        assert brought.reason == "example"                                 # 第 0 层范围之外，是样例把它带进来的
    assert Retriever(store, graph, None).recall(RecallQuery("zzqq", ["zzqq"])).examples == []


# ── 先后与确定性 ──────────────────────────────────────

def tie_mart(first: str, second: str) -> Mart:
    """两张表对两个检索词的名次正好对调，得分完全相同"""
    return make_mart([(first, "汇总表", [("p", "甲指标"), ("q", "乙指标项")]),
                      (second, "汇总表", [("p", "甲指标项"), ("q", "乙指标")])])


def test_equal_scores_fall_back_to_layer_then_table_id():
    query = RecallQuery("x", ["甲指标", "乙指标"])
    same_layer = make_retriever(tie_mart("ads.n", "ads.m"), with_embedder=False).recall(query)
    assert [item.table_id for item in same_layer.tables] == ["ads.m", "ads.n"]
    assert same_layer.tables[0].score == pytest.approx(same_layer.tables[1].score)
    # 层级高的在前，哪怕表标识排在后面
    mixed = make_retriever(tie_mart("dim.a", "zt.m"), with_embedder=False).recall(query)
    assert [item.table_id for item in mixed.tables] == ["zt.m", "dim.a"]


def test_results_do_not_depend_on_the_hash_seed():
    outputs = []
    for seed in ("1", "2"):
        done = subprocess.run([sys.executable, "-c", CHILD_SCRIPT, str(MOCK_MART_DIR)], cwd=ROOT, encoding="utf-8",
                              errors="replace", capture_output=True, env={**os.environ, "PYTHONHASHSEED": seed},
                              check=False)
        assert done.returncode == 0, done.stderr
        outputs.append(done.stdout)
    assert outputs[0] == outputs[1]
    first, second = json.loads(outputs[0])
    assert first["scope_level"] == 0 and second["scope_level"] == 1 and first["tables"]


def test_repeated_calls_give_the_same_result(mock_retriever):
    query = RecallQuery("2026 年 9 月末各行业不含保证金的对公存款余额", ["不含保证金的存款余额"], ["行业名称"])
    assert asdict(mock_retriever.recall(query)) == asdict(mock_retriever.recall(query))


def test_notes_explain_empty_queries_and_terms_without_hits(mock_parts):
    store, graph = mock_parts
    assert "没有检索词" in " ".join(Retriever(store, graph, HashEmbedder()).recall(RecallQuery("x", [])).notes)
    result = Retriever(store, graph, None).recall(RecallQuery("x", ["zzqq"]))
    assert result.tables == [] and "检索词「zzqq」没有命中任何字段" in result.notes


# ── 模拟题的召回率 ──────────────────────────────────────

def test_gold_tables_of_the_mock_questions_reach_the_candidates(mock_retriever):
    terms = {item["id"]: item for item in yaml.safe_load(RECALL_TERMS.read_text(encoding="utf-8"))}
    questions = [question for question in load_questions() if question.expect == "sql"]
    assert set(terms) == {question.id for question in questions}
    missed = {}
    for question in questions:
        spec = terms[question.id]
        query = RecallQuery(spec.get("requirement", question.turns[0]), spec.get("metrics", []),
                            spec.get("dimensions", []), spec.get("filters", []))
        result = mock_retriever.recall(query)
        if spec.get("level", 0):                                           # 向上溯源的题：以第 0 层的候选为起点上溯
            result = mock_retriever.recall(query, spec["level"], [item.table_id for item in result.tables])
        absent = set(question.gold_tables) - {item.table_id for item in result.tables}
        if absent:
            missed[question.id] = sorted(absent)
    assert len(questions) - len(missed) >= MIN_RECALLED_QUESTIONS, missed


# ── 全部层级一起排名（对照实验 A 组：没有上溯） ──────────────────────────────

def test_all_layers_ranks_every_table_in_one_pool_and_never_climbs():
    query = RecallQuery("存款金额汇总统计", ["存款金额"])
    retriever = make_retriever(ten_table_mart(), with_embedder=False)       # 10 张专题层表、1 张整合层表、2 张贴源层表

    layered = retriever.recall(query)
    flat = retriever.recall(query, all_layers=True)

    assert (layered.scope_level, layered.scope_size) == (0, 10)
    assert (flat.scope_level, flat.scope_size) == (0, 13)                   # 排名池是全部表
    assert {item.table_id: item.reason for item in layered.tables if item.table_id in ("dwd.src", "ods.raw")} == {}
    ranked = {item.table_id: item.reason for item in flat.tables}
    assert ranked["dwd.src"] == ranked["ods.raw"] == "recall"               # 整合层、贴源层的表和专题层的表一起争名次
    assert {item.reason for item in flat.tables} <= {"recall", "example"}   # 没有上溯加入的表
    assert sum(item.reason == "recall" for item in flat.tables) == MAX_TABLES
    scores = [item.score for item in flat.tables if item.reason == "recall"]
    assert scores == sorted(scores, reverse=True)


def test_all_layers_covers_the_whole_mock_mart_and_leaves_the_default_unchanged(mock_retriever, mock_parts):
    store, _ = mock_parts
    query = RecallQuery("折人民币余额", ["折人民币余额"], ["币种代码"])

    flat = mock_retriever.recall(query, all_layers=True)
    default = mock_retriever.recall(query)

    assert flat.scope_size == len(store.tables()) == 14
    assert default.scope_size == 5
    assert any(item.reason == "recall" and item.table_id.startswith(("dwd.", "ods.")) for item in flat.tables)
    assert not any(item.reason == "recall" and item.table_id.startswith(("dwd.", "ods.")) for item in default.tables)
    assert asdict(mock_retriever.recall(query, all_layers=False)) == asdict(default)       # 默认的行为一点没变
    assert asdict(mock_retriever.recall(query, all_layers=True)) == asdict(flat)


def test_all_layers_cannot_be_combined_with_an_upstream_level(mock_retriever):
    query = RecallQuery("x", ["存款余额"])

    for level in (1, 2):
        with pytest.raises(ValueError, match="all_layers"):
            mock_retriever.recall(query, level, ["ads.org_corp_dep_bal_d"], all_layers=True)


# ── 多条语句写同一个字段 ──────────────────────────────────────

def test_a_field_written_by_several_statements_is_excluded_only_when_every_one_is_null_or_etl_time():
    names = ["all_null", "null_then_real", "real_then_null", "null_and_time", "plain", "unscripted"]
    tables = [("ads.bal", "余额汇总", [(name, f"存款余额{number}") for number, name in enumerate(names, 1)])]
    kinds = {"all_null": ["null", "null"], "null_then_real": ["null", "derived"], "real_then_null": ["derived", "null"],
             "null_and_time": ["null", "etl_time"], "plain": ["derived"]}
    script_columns = [ScriptColumn(script_name=f"s{number}.sql", stmt_index=1, target_table="ads.bal",
                                   target_column=column, expression="x", kind=kind)
                      for column, column_kinds in kinds.items() for number, kind in enumerate(column_kinds, 1)]

    result = make_retriever(make_mart(tables, script_columns=script_columns)).recall(RecallQuery("x", ["存款余额"]))

    assert hit_names(result) == {"null_then_real", "real_then_null", "plain", "unscripted"}      # 第一条是空值也不能排除
