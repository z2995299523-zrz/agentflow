"""血缘图与关联图的测试：字段上下游链路（深度、防环、加工条件）、同源差异、上下游表闭包、
关联路径（直连优先、一张中间表、发散标记）、唯一性判断"""
import pytest

from lineage.graph import Hop, JoinPath, JoinStep, LineageGraph, SourceDiff
from metadata.models import (
    ColumnMeta,
    FieldRef,
    JoinKey,
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

DEP_D = "dwd.corp_dep_acct_d"
CUST = "dim.corp_cust"
ORG = "dim.org"
ORG_INFO = "ods.org_info"
ADS_DEP = "ads.corp_cust_dep_bal_d"
ADS_LOAN = "ads.corp_cust_loan_bal_d"
ADS_ORG = "ads.org_corp_dep_bal_d"

SCHEMA_LAYER = {"ods": "贴源层", "dwd": "整合层", "dim": "整合层", "ads": "专题层"}


def ref(text: str) -> FieldRef:
    """「ods.src.k」写成字段引用"""
    table, column = text.rsplit(".", 1)
    return FieldRef(table=table, column=column)


# ── 构造小资料的工具 ──────────────────────────────────────

def edge(target: str, source: str, transform: str = "", script: str = "", external: bool = False,
         passthrough: bool = False, origin: str = "list") -> LineageEdge:
    """一条血缘边，目标和来源写成「表.字段」"""
    return LineageEdge(target_table=ref(target).table, target_column=ref(target).column,
                       source_table=ref(source).table, source_column=ref(source).column, transform=transform,
                       script_name=script, source_external=external, is_passthrough=passthrough, origin=origin)


def statement(script: str, target: str, filters: list[tuple[str, str]] = (), joins: list[ScriptJoin] = (),
              select_sql: str = "", index: int = 1) -> ScriptStatement:
    """一条写数语句，过滤条件写成 (查询层, 条件原文)"""
    return ScriptStatement(script_name=script, stmt_index=index, target_table=target, sql_text="", parsed=True,
                           select_sql=select_sql, filters=[ScriptFilter(scope=s, clause="WHERE", condition=c)
                                                           for s, c in filters], joins=list(joins))


def relation(left: str, left_columns: list[str], right: str, right_columns: list[str], card: str,
             origin: str = "manual", note: str = "") -> Relation:
    """一条关联"""
    return Relation(left_table=left, left_columns=left_columns, right_table=right, right_columns=right_columns,
                    cardinality=card, origin=origin, note=note)


def make_store(tables: list[tuple], edges: list[LineageEdge] = (), relations: list[Relation] = (),
               scripts: list[ScriptStatement] = (), script_columns: list[ScriptColumn] = ()) -> MetadataStore:
    """手写的小元数据库：tables 是 (表标识, 粒度字段, 字段列表[, 层级])"""
    metas, columns = [], []
    for table_id, grain, names, *layer in tables:
        schema, name = table_id.split(".")
        metas.append(TableMeta(table_id=table_id, layer=layer[0] if layer else SCHEMA_LAYER[schema],
                               schema_name=schema, table_name=name, table_cn=name, table_type="全量表",
                               grain_columns=list(grain)))
        columns += [ColumnMeta(table_id=table_id, column_name=column, ordinal=i, column_cn=column)
                    for i, column in enumerate(names, 1)]
    return MetadataStore.from_mart(Mart(tables=metas, columns=columns, lineage=list(edges), relations=list(relations),
                                        scripts=list(scripts), script_columns=list(script_columns)))


@pytest.fixture(scope="module")
def graph(mock_store) -> LineageGraph:
    """模拟专题库的血缘图"""
    return LineageGraph(mock_store)


# ── 上游链路 ──────────────────────────────────────────────

def test_upstream_hops_carry_the_target_tables_filters_and_joins(graph):
    hops = graph.upstream(ADS_DEP, "dep_bal_rmb", depth=1)

    assert [(h.depth, h.source) for h in hops] == [(1, ref(f"{DEP_D}.bal_rmb")), (1, ref(f"{DEP_D}.is_margin"))]
    first = hops[0]
    assert isinstance(first, Hop)
    assert first.target == ref(f"{ADS_DEP}.dep_bal_rmb")
    assert (first.origin, first.script_name, first.source_external, first.is_passthrough) == (
        "list", "21_ads_corp_cust_dep_bal_d.sql", False, False)
    expression = "SUM(CASE WHEN dwd.corp_dep_acct_d.is_margin = '0' THEN dwd.corp_dep_acct_d.bal_rmb ELSE 0 END)"
    assert first.script_expression == expression
    assert first.transform == expression                      # 清单里没写加工逻辑，导入时用脚本表达式回填了
    assert len(first.filters) == 3                            # cur、ytd、mtd 各一条 WHERE
    assert ScriptFilter(scope="cur", clause="WHERE",
                        condition="dwd.corp_dep_acct_d.data_dt = CAST('${data_dt}' AS DATE)") in first.filters
    assert [j.table for j in first.joins] == ["ytd", "mtd", CUST]
    assert hops[1].filters == first.filters and hops[1].joins == first.joins        # 同一张目标表，条件相同


def test_upstream_hop_for_a_direct_take_is_marked_passthrough(graph):
    (hop,) = graph.upstream(DEP_D, "cust_no", depth=1)

    assert hop.is_passthrough and hop.source == ref("ods.core_dep_acct.cust_no")
    assert hop.script_name == "11_dwd_corp_dep_acct_d.sql"
    assert any(f.condition == "ods.core_dep_acct.cust_type = '1'" for f in hop.filters)
    assert [j.table for j in hop.joins] == ["ods.gl_subject", "ods.exch_rate"]


def test_upstream_is_breadth_first_and_limited_by_depth(graph):
    full = graph.upstream(ADS_ORG, "dep_bal_rmb", depth=3)

    assert [h.depth for h in full] == [1, 2, 2, 3, 3, 3]
    assert [(h.target.table, h.target.column, h.source.table, h.source.column) for h in full] == [
        (ADS_ORG, "dep_bal_rmb", ADS_DEP, "dep_bal_rmb"),
        (ADS_DEP, "dep_bal_rmb", DEP_D, "bal_rmb"),
        (ADS_DEP, "dep_bal_rmb", DEP_D, "is_margin"),
        (DEP_D, "bal_rmb", "ods.core_dep_acct", "acct_bal"),
        (DEP_D, "bal_rmb", "ods.exch_rate", "mid_rate"),
        (DEP_D, "is_margin", "ods.gl_subject", "subj_type_cd"),
    ]
    assert len(graph.upstream(ADS_ORG, "dep_bal_rmb", depth=2)) == 3
    assert len(graph.upstream(ADS_ORG, "dep_bal_rmb", depth=1)) == 1
    assert graph.upstream(ADS_ORG, "dep_bal_rmb", depth=0) == []
    assert len(graph.upstream(ADS_ORG, "dep_bal_rmb", depth=10)) == 6
    assert graph.upstream(ADS_ORG, "dep_bal_rmb") == full                 # 默认深度 3


def test_upstream_of_a_field_without_lineage_is_empty(graph):
    assert graph.upstream(ORG_INFO, "org_no") == []
    assert graph.upstream("no.such_table", "x") == []
    assert graph.upstream(ADS_ORG, "no_such_column") == []


def test_upstream_and_downstream_are_repeatable(graph):
    assert graph.upstream(ADS_ORG, "dep_bal_rmb") == graph.upstream(ADS_ORG, "dep_bal_rmb")
    assert graph.downstream("ods.core_dep_acct", "cust_no") == graph.downstream("ods.core_dep_acct", "cust_no")


def test_two_fields_feeding_each_other_do_not_loop():
    store = make_store([("ods.t", [], ["x", "y"])], edges=[edge("ods.t.x", "ods.t.y"), edge("ods.t.y", "ods.t.x")])
    graph = LineageGraph(store)

    up = graph.upstream("ods.t", "x", depth=50)
    down = graph.downstream("ods.t", "x", depth=50)

    assert [(h.depth, h.target.column, h.source.column) for h in up] == [(1, "x", "y"), (2, "y", "x")]
    assert [(h.depth, h.target.column, h.source.column) for h in down] == [(1, "y", "x"), (2, "x", "y")]


def test_a_longer_cycle_and_a_self_edge_terminate():
    store = make_store([("ods.t", [], ["a", "b", "c"])],
                       edges=[edge("ods.t.a", "ods.t.b"), edge("ods.t.b", "ods.t.c"), edge("ods.t.c", "ods.t.a"),
                              edge("ods.t.a", "ods.t.a")])

    assert len(LineageGraph(store).upstream("ods.t", "a", depth=100)) == 4        # 每条边只出现一次


def test_diamond_reports_each_edge_once_and_expands_each_field_once():
    tables = [("ods.t", [], ["a", "b", "c", "d", "e"])]
    store = make_store(tables, edges=[edge("ods.t.a", "ods.t.b"), edge("ods.t.a", "ods.t.c"),
                                      edge("ods.t.b", "ods.t.d"), edge("ods.t.c", "ods.t.d"),
                                      edge("ods.t.d", "ods.t.e")])

    hops = LineageGraph(store).upstream("ods.t", "a", depth=10)

    assert [(h.depth, h.target.column, h.source.column) for h in hops] == [
        (1, "a", "b"), (1, "a", "c"), (2, "b", "d"), (2, "c", "d"), (3, "d", "e")]      # d 到 e 只走一次


def test_hop_without_scripts_has_empty_conditions_and_keeps_the_external_flag():
    store = make_store([("ads.t", [], ["v"])], edges=[edge("ads.t.v", "ext.src.v", transform="x + 1",
                                                              script="a.sql", external=True)])

    (hop,) = LineageGraph(store).upstream("ads.t", "v")

    assert (hop.source, hop.source_external, hop.transform) == (ref("ext.src.v"), True, "x + 1")
    assert (hop.script_expression, hop.filters, hop.joins) == ("", (), ())


def test_hop_prefers_the_statement_of_the_scripts_named_on_the_edge():
    """一张目标表有两个脚本往里写数：每条边只带它自己脚本的过滤条件；脚本名对不上时带全部语句的"""
    tables = [("ads.t", [], ["v"]), ("ods.a", [], ["v"]), ("ods.b", [], ["v"]), ("ods.c", [], ["v"])]
    scripts = [statement("a.sql", "ads.t", filters=[("main", "ods.a.flag = 1")]),
               statement("b.sql", "ads.t", filters=[("main", "ods.b.flag = 2")])]
    store = make_store(tables, scripts=scripts, edges=[
        edge("ads.t.v", "ods.a.v", script="a.sql"), edge("ads.t.v", "ods.b.v", script="b.sql"),
        edge("ads.t.v", "ods.c.v", script="unknown.sql")])

    first, second, third = LineageGraph(store).upstream("ads.t", "v")

    assert [f.condition for f in first.filters] == ["ods.a.flag = 1"]
    assert [f.condition for f in second.filters] == ["ods.b.flag = 2"]
    assert [f.condition for f in third.filters] == ["ods.a.flag = 1", "ods.b.flag = 2"]


def test_hop_script_expression_comes_from_the_script_column():
    column = ScriptColumn(script_name="a.sql", stmt_index=1, target_table="ads.t", target_column="v",
                          expression="SUM(ods.a.v)", sources=[ref("ods.a.v")], kind="derived")
    store = make_store([("ads.t", [], ["v"]), ("ods.a", [], ["v"])], edges=[edge("ads.t.v", "ods.a.v", "文字说明")],
                       script_columns=[column])

    (hop,) = LineageGraph(store).upstream("ads.t", "v")

    assert (hop.transform, hop.script_expression) == ("文字说明", "SUM(ods.a.v)")


def test_fields_missing_from_the_field_list_do_not_break_the_graph():
    """脚本补出来的血缘边，来源字段可能不在字段清单里"""
    store = make_store([("ods.s", [], ["k"]), ("dwd.t", [], ["k", "x"])],
                       edges=[edge("dwd.t.x", "ods.s.gone", origin="script", passthrough=True)])
    graph = LineageGraph(store)

    (up,) = graph.upstream("dwd.t", "x")
    (down,) = graph.downstream("ods.s", "gone")

    assert up.source == ref("ods.s.gone") and down.target == ref("dwd.t.x")
    assert graph.upstream_tables("dwd.t") == ["ods.s"]
    assert graph.downstream_tables("ods.s") == ["dwd.t"]


# ── 下游链路 ──────────────────────────────────────────────

def test_downstream_follows_the_fields_that_read_from_this_one(graph):
    hops = graph.downstream("ods.core_dep_acct", "cust_no", depth=3)

    assert [(h.depth, h.source.table, h.source.column, h.target.table, h.target.column) for h in hops] == [
        (1, "ods.core_dep_acct", "cust_no", DEP_D, "cust_no"),
        (2, DEP_D, "cust_no", ADS_DEP, "cust_no"),
        (3, ADS_DEP, "cust_no", ADS_ORG, "dep_cust_cnt"),
    ]
    assert hops[2].script_name == "22_ads_org_corp_dep_bal_d.sql"
    assert [f.condition for f in hops[2].filters] == ["ads.corp_cust_dep_bal_d.data_dt = CAST('${data_dt}' AS DATE)"]
    assert len(graph.downstream("ods.core_dep_acct", "cust_no", depth=1)) == 1
    assert graph.downstream("ods.core_dep_acct", "cust_no", depth=0) == []
    assert graph.downstream(ADS_ORG, "dep_bal_rmb") == []


# ── 同源差异 ──────────────────────────────────────────────

def test_compare_finds_shared_upstream_and_flags_different_processing(graph):
    a, b = ref(f"{ADS_ORG}.dep_bal_rmb"), ref(f"{ADS_ORG}.dep_bal_tot_rmb")

    diff = graph.compare(a, b)

    assert isinstance(diff, SourceDiff)
    assert (diff.a, diff.b) == (a, b)
    assert ref(f"{DEP_D}.bal_rmb") in diff.common
    assert ref(f"{DEP_D}.is_margin") not in diff.common            # 全口径存款余额不看是否保证金
    assert diff.differs
    assert diff.path_a == tuple(graph.upstream(ADS_ORG, "dep_bal_rmb"))
    assert diff.path_b == tuple(graph.upstream(ADS_ORG, "dep_bal_tot_rmb"))


def test_compare_of_fields_without_shared_upstream(graph):
    diff = graph.compare(ref(f"{ADS_DEP}.cust_no"), ref(f"{ADS_LOAN}.cust_no"))

    assert diff.common == () and not diff.differs


def test_compare_counts_a_field_that_is_the_upstream_of_the_other(graph):
    a, b = ref(f"{ADS_DEP}.dep_bal_rmb"), ref(f"{ADS_ORG}.dep_bal_rmb")

    diff = graph.compare(a, b)

    assert a in diff.common and ref(f"{DEP_D}.bal_rmb") in diff.common
    assert diff.differs                                       # 机构层多了一道汇总


def test_compare_of_a_field_with_itself_does_not_differ(graph):
    a = ref(f"{ADS_ORG}.dep_bal_rmb")

    diff = graph.compare(a, a)

    assert diff.common and not diff.differs


def test_compare_same_source_same_processing_does_not_differ():
    tables = [("ods.s", [], ["k"]), ("ads.t1", [], ["x"]), ("ads.t2", [], ["x"])]
    store = make_store(tables, edges=[edge("ads.t1.x", "ods.s.k", "ods.s.k"), edge("ads.t2.x", "ods.s.k", "ods.s.k")])

    diff = LineageGraph(store).compare(ref("ads.t1.x"), ref("ads.t2.x"))

    assert diff.common == (ref("ods.s.k"),) and not diff.differs


def test_compare_same_source_different_transform_differs():
    tables = [("ods.s", [], ["k"]), ("ads.t1", [], ["x"]), ("ads.t2", [], ["x"])]
    store = make_store(tables, edges=[edge("ads.t1.x", "ods.s.k", "SUM(ods.s.k)"),
                                      edge("ads.t2.x", "ods.s.k", "MAX(ods.s.k)")])

    assert LineageGraph(store).compare(ref("ads.t1.x"), ref("ads.t2.x")).differs


def test_compare_same_source_different_filters_differs():
    tables = [("ods.s", [], ["k"]), ("ads.t1", [], ["x"]), ("ads.t2", [], ["x"])]
    store = make_store(
        tables, edges=[edge("ads.t1.x", "ods.s.k", "ods.s.k", script="a.sql"),
                       edge("ads.t2.x", "ods.s.k", "ods.s.k", script="b.sql")],
        scripts=[statement("a.sql", "ads.t1", filters=[("main", "ods.s.flag = 1")]),
                 statement("b.sql", "ads.t2", filters=[("main", "ods.s.flag = 2")])])

    assert LineageGraph(store).compare(ref("ads.t1.x"), ref("ads.t2.x")).differs


# ── 上游表、下游表 ────────────────────────────────────────

def test_upstream_tables_include_tables_that_only_appear_in_joins(graph, mock_store):
    tables = graph.upstream_tables(ADS_DEP)

    assert tables == [CUST, DEP_D, "ods.ecif_corp_cust", "ods.industry_dict", "ods.core_dep_acct", "ods.exch_rate",
                      "ods.gl_subject"]                      # 近的在前，同一层按表标识排序
    assert CUST not in {e.source_table for e in mock_store.lineage_edges() if e.target_table == ADS_DEP}


def test_upstream_tables_can_be_filtered_by_layer(graph):
    assert graph.upstream_tables(ADS_DEP, layers=["贴源层"]) == [
        "ods.ecif_corp_cust", "ods.industry_dict", "ods.core_dep_acct", "ods.exch_rate", "ods.gl_subject"]
    assert graph.upstream_tables(ADS_DEP, layers={"整合层"}) == [CUST, DEP_D]
    assert graph.upstream_tables(ADS_DEP, layers=()) == []
    assert graph.upstream_tables("ods.core_dep_acct") == []


def test_upstream_tables_walk_through_layers_that_are_filtered_out(graph):
    """贴源层的表经由整合层才到得了专题层：只要贴源层时，整合层只是路过"""
    assert "ods.core_dep_acct" in graph.upstream_tables(ADS_ORG, layers=["贴源层"])


def test_downstream_tables_include_tables_that_only_join_this_one(graph):
    assert set(graph.downstream_tables("ods.core_dep_acct")) == {DEP_D, ADS_DEP, ADS_ORG}
    assert set(graph.downstream_tables(CUST)) == {ADS_DEP, ADS_LOAN, ADS_ORG}
    assert set(graph.downstream_tables(CUST, layers=["专题层"])) == {ADS_DEP, ADS_LOAN, ADS_ORG}
    assert graph.downstream_tables(CUST, layers=["贴源层"]) == []
    assert graph.downstream_tables(ADS_ORG) == []


def test_upstream_tables_take_tables_from_the_select_even_when_no_join_lists_them():
    tables = [("ads.t", [], ["v"]), ("ods.a", [], ["v"]), ("ods.d", [], ["k"])]
    sql = "SELECT x.v FROM ods.a x WHERE EXISTS (SELECT 1 FROM ods.d y WHERE y.k = x.v)"
    store = make_store(tables, edges=[edge("ads.t.v", "ods.a.v")],
                       scripts=[statement("a.sql", "ads.t", select_sql=sql)])
    graph = LineageGraph(store)

    assert graph.upstream_tables("ads.t") == ["ods.a", "ods.d"]
    assert graph.downstream_tables("ods.d") == ["ads.t"]


def test_unparsable_select_sql_falls_back_to_the_joins():
    tables = [("ads.t", [], ["v"]), ("ods.a", [], ["v"]), ("ods.d", [], ["k"])]
    join = ScriptJoin(scope="main", join_type="JOIN", table="ods.d", condition="", keys=[])
    store = make_store(tables, edges=[edge("ads.t.v", "ods.a.v")],
                       scripts=[statement("a.sql", "ads.t", joins=[join], select_sql="SELECT FROM WHERE ((")])

    assert LineageGraph(store).upstream_tables("ads.t") == ["ods.a", "ods.d"]


def test_upstream_tables_take_tables_from_script_join_keys():
    tables = [("ads.t", [], ["v"]), ("ods.a", [], ["v"]), ("ods.d", [], ["k"])]
    join = ScriptJoin(scope="main", join_type="JOIN", table="cte_name", condition="",
                      keys=[JoinKey(left=ref("ods.a.v"), right=ref("ods.d.k"))])
    store = make_store(tables, edges=[edge("ads.t.v", "ods.a.v")],
                       scripts=[statement("a.sql", "ads.t", joins=[join])])        # 没有 select_sql，只靠 joins

    assert LineageGraph(store).upstream_tables("ads.t") == ["ods.a", "ods.d"]


def test_upstream_tables_leave_out_external_tables_and_the_table_itself():
    tables = [("ods.a", [], ["v"]), ("ods.b", [], ["v"])]
    store = make_store(tables, edges=[edge("ods.a.v", "ods.b.v"), edge("ods.b.v", "ods.a.v"),
                                      edge("ods.a.v", "ext.src.v", external=True)])
    graph = LineageGraph(store)

    assert graph.upstream_tables("ods.a") == ["ods.b"]          # 成环时不含自己，外部表不进结果
    assert graph.downstream_tables("ods.a") == ["ods.b"]
    assert graph.upstream_tables("no.such_table") == []


def test_unparsed_statement_adds_no_script_tables():
    tables = [("ads.t", [], ["v"]), ("ods.a", [], ["v"])]
    broken = ScriptStatement(script_name="a.sql", stmt_index=1, target_table="ads.t", sql_text="INSERT 乱写的",
                             parsed=False)
    store = make_store(tables, edges=[edge("ads.t.v", "ods.a.v")], scripts=[broken])

    assert LineageGraph(store).upstream_tables("ads.t") == ["ods.a"]


# ── 关联与路径 ────────────────────────────────────────────

def test_relations_between_lists_every_mapping_best_first_in_either_direction(graph):
    forward = graph.relations_between(ORG, ORG_INFO)
    backward = graph.relations_between(ORG_INFO, ORG)

    assert [r.origin for r in forward] == ["lineage", "grain_key"]
    assert forward == backward
    assert graph.relations_between(ORG_INFO, ORG_INFO)[0].origin == "script"      # 自关联
    assert graph.relations_between(ORG_INFO, "ods.industry_dict") == []


def test_join_path_prefers_the_direct_relation(graph):
    path = graph.join_path(ADS_ORG, ORG)

    assert isinstance(path, JoinPath) and not path.fan_out
    assert path.steps == (JoinStep(left_table=ADS_ORG, left_columns=("org_no",), right_table=ORG,
                                   right_columns=("org_no",), cardinality="N:1", origin="grain_key",
                                   note=path.steps[0].note),)
    assert path.steps[0].note.startswith("同名粒度键")


def test_join_path_reversed_flips_cardinality_and_marks_fan_out(graph):
    path = graph.join_path(ORG, ADS_ORG)

    (step,) = path.steps
    assert (step.left_table, step.right_table, step.cardinality) == (ORG, ADS_ORG, "1:N")
    assert path.fan_out


def test_join_path_between_a_table_and_itself_is_empty(graph):
    assert graph.join_path(ORG, ORG) == JoinPath(steps=(), fan_out=False)
    assert graph.join_path("no.such_table", "no.such_table") is None
    assert graph.join_path(ORG, "no.such_table") is None


PATH_TABLES = [("ods.a", ["a"], ["a", "x"]), ("ods.b", ["b"], ["b", "x", "y"]), ("ods.c", ["c"], ["c", "y", "z"]),
               ("ods.d", ["d"], ["d", "z"]), ("ods.e", ["e"], ["e", "w"])]


def test_join_path_through_one_intermediate_table():
    store = make_store(PATH_TABLES, relations=[relation("ods.a", ["x"], "ods.b", ["x"], "N:1"),
                                               relation("ods.b", ["y"], "ods.c", ["y"], "N:1")])

    path = LineageGraph(store).join_path("ods.a", "ods.c")

    assert [(s.left_table, s.left_columns, s.right_table, s.right_columns, s.cardinality) for s in path.steps] == [
        ("ods.a", ("x",), "ods.b", ("x",), "N:1"), ("ods.b", ("y",), "ods.c", ("y",), "N:1")]
    assert not path.fan_out


def test_join_path_needing_two_intermediate_tables_is_not_found():
    store = make_store(PATH_TABLES, relations=[relation("ods.a", ["x"], "ods.b", ["x"], "N:1"),
                                               relation("ods.b", ["y"], "ods.c", ["y"], "N:1"),
                                               relation("ods.c", ["z"], "ods.d", ["z"], "N:1")])
    graph = LineageGraph(store)

    assert graph.join_path("ods.a", "ods.d") is None
    assert graph.join_path("ods.a", "ods.e") is None            # 根本不通
    assert len(graph.join_path("ods.a", "ods.c").steps) == 2


def test_join_path_direct_beats_a_safer_detour():
    store = make_store(PATH_TABLES, relations=[relation("ods.a", ["x"], "ods.c", ["z"], "N:N"),
                                               relation("ods.a", ["x"], "ods.b", ["x"], "N:1"),
                                               relation("ods.b", ["y"], "ods.c", ["y"], "N:1")])

    path = LineageGraph(store).join_path("ods.a", "ods.c")

    assert len(path.steps) == 1 and path.steps[0].cardinality == "N:N" and path.fan_out


def test_join_path_picks_the_intermediate_table_that_does_not_fan_out():
    tables = PATH_TABLES + [("ods.f", ["f"], ["f", "x", "y"])]
    store = make_store(tables, relations=[
        relation("ods.a", ["x"], "ods.b", ["x"], "N:N"), relation("ods.b", ["y"], "ods.c", ["y"], "N:1"),
        relation("ods.a", ["x"], "ods.f", ["x"], "N:1"), relation("ods.f", ["y"], "ods.c", ["y"], "N:1")])

    path = LineageGraph(store).join_path("ods.a", "ods.c")

    assert [s.right_table for s in path.steps] == ["ods.f", "ods.c"]
    assert not path.fan_out


def test_join_path_fan_out_by_direction_and_cardinality():
    cases = {"N:1": False, "1:1": False, "1:N": True, "N:N": True, "未知": True}
    for card, expected in cases.items():
        store = make_store(PATH_TABLES, relations=[relation("ods.a", ["x"], "ods.b", ["x"], card)])
        assert LineageGraph(store).join_path("ods.a", "ods.b").fan_out is expected, card
    store = make_store(PATH_TABLES, relations=[relation("ods.a", ["x"], "ods.b", ["x"], "N:1")])
    assert LineageGraph(store).join_path("ods.b", "ods.a").fan_out                    # 反方向走：1 对多


def test_join_path_fan_out_if_any_step_fans_out():
    store = make_store(PATH_TABLES, relations=[relation("ods.a", ["x"], "ods.b", ["x"], "N:1"),
                                               relation("ods.c", ["y"], "ods.b", ["y"], "N:1")])

    path = LineageGraph(store).join_path("ods.a", "ods.c")

    assert [s.cardinality for s in path.steps] == ["N:1", "1:N"] and path.fan_out


def test_join_path_step_keeps_origin_and_note_and_swaps_columns_when_reversed():
    given = relation("ods.a", ["x"], "ods.b", ["x2"], "N:1", origin="script", note="脚本里写过")
    store = make_store(PATH_TABLES, relations=[given])

    (step,) = LineageGraph(store).join_path("ods.b", "ods.a").steps

    assert step == JoinStep(left_table="ods.b", left_columns=("x2",), right_table="ods.a", right_columns=("x",),
                            cardinality="1:N", origin="script", note="脚本里写过")


def test_join_path_uses_the_best_mapping_between_two_tables():
    store = make_store(PATH_TABLES, relations=[relation("ods.a", ["x"], "ods.b", ["y"], "N:N", origin="grain_key"),
                                               relation("ods.a", ["x"], "ods.b", ["x"], "N:1", origin="script")])

    (step,) = LineageGraph(store).join_path("ods.a", "ods.b").steps

    assert (step.origin, step.right_columns) == ("script", ("x",))


# ── 唯一性 ────────────────────────────────────────────────

def test_is_unique_checks_coverage_of_the_grain(graph):
    assert graph.is_unique(ORG, ["org_no"]) is True
    assert graph.is_unique(ORG, ["org_nm"]) is False
    assert graph.is_unique(ORG, ["org_no", "org_nm"]) is True
    assert graph.is_unique(ADS_DEP, ("data_dt", "cust_no", "org_no")) is True
    assert graph.is_unique(ADS_DEP, {"cust_no", "org_no"}) is False              # 少了 data_dt
    assert graph.is_unique(ORG, []) is False


def test_is_unique_is_unknown_without_grain_or_table():
    graph = LineageGraph(make_store([("ods.free", [], ["a", "b"])]))

    assert graph.is_unique("ods.free", ["a"]) is None
    assert graph.is_unique("no.such_table", ["a"]) is None


def test_graph_builds_from_an_empty_store():
    graph = LineageGraph(make_store([]))

    assert graph.upstream("a.b", "c") == [] and graph.downstream_tables("a.b") == []
    assert graph.join_path("a.b", "c.d") is None
