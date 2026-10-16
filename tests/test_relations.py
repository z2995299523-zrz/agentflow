"""关联关系推导的测试：对应关系判定、键域、四种来源的优先级与定向去重、各项上限、报告节，
以及跨模块验收（推导出的关联让 35 道标准 SQL 通过 R6）"""
import random
from collections import Counter
from pathlib import Path

import pytest

from eval.questions import load_questions
from metadata import relations as relations_module
from metadata.importer import load_mart
from metadata.models import (
    ColumnMeta,
    FieldRef,
    JoinKey,
    LineageEdge,
    Mart,
    Relation,
    ScriptColumn,
    ScriptJoin,
    ScriptStatement,
    TableMeta,
)
from metadata.relations import (
    GENERIC_KEY_NAMES,
    MAX_DOMAIN_SIZE,
    MAX_GRAIN_FANIN,
    MAX_MAPPINGS_PER_PAIR,
    MAX_PROPAGATION_PER_JOIN,
    SECTION_RELATIONS,
    cardinality,
    derive_relations,
    key_domains,
    relation_rank,
)
from metadata.script_parser import SCRIPTS_DIR_NAME, merge_lineage, parse_scripts
from nl2sql.validator import SqlValidator

MOCK_MART = Path(__file__).resolve().parents[1] / "data" / "mock_mart"

DEP_D = "dwd.corp_dep_acct_d"
LOAN_D = "dwd.corp_loan_duebill_d"
CUST = "dim.corp_cust"
ORG = "dim.org"
ORG_INFO = "ods.org_info"
RATE = "ods.exch_rate"
ADS_DEP = "ads.corp_cust_dep_bal_d"
ADS_LOAN = "ads.corp_cust_loan_bal_d"
ADS_ORG = "ads.org_corp_dep_bal_d"

SCHEMA_LAYER = {"ods": "贴源层", "dwd": "整合层", "dim": "整合层", "ads": "专题层"}
LAYER_NUMBER = {"ods": 0, "dwd": 1, "dim": 1, "ads": 2}


# ── 构造资料的小工具 ──────────────────────────────────────

def ref(text: str) -> FieldRef:
    """「ods.src.k」写成字段引用"""
    table, column = text.rsplit(".", 1)
    return FieldRef(table=table, column=column)


def make_mart(tables: list[tuple], taken: list[tuple[str, str]] = (),
              joins: list[tuple[str, list[tuple[str, str]]]] = (), relations: list[Relation] = (),
              script_columns: list[ScriptColumn] = (), extra_edges: list[LineageEdge] = ()) -> Mart:
    """手写的小资料

    Args:
        tables: (表标识, 粒度字段, 字段列表[, 层级])；层级不写时按模式名取
        taken: (目标「表.字段」, 源「表.字段」)，一条直取血缘
        joins: (脚本名, [(左「表.字段」, 右「表.字段」), …])，每项是该脚本里的一处关联，括号里的多个键对是复合键
        relations: 手工关联
        script_columns: 脚本逐字段表达式
        extra_edges: 另外的血缘边
    """
    metas, columns = [], []
    for table_id, grain, names, *layer in tables:
        schema, name = table_id.split(".")
        metas.append(TableMeta(table_id=table_id, layer=layer[0] if layer else SCHEMA_LAYER[schema],
                               schema_name=schema, table_name=name, table_cn=name, table_type="全量表",
                               grain_columns=list(grain)))
        columns += [ColumnMeta(table_id=table_id, column_name=column, ordinal=i, column_cn=column)
                    for i, column in enumerate(names, 1)]
    lineage = [LineageEdge(target_table=ref(target).table, target_column=ref(target).column,
                           source_table=ref(source).table, source_column=ref(source).column,
                           is_passthrough=True, origin="script", script_name="x.sql")
               for target, source in taken]
    statements = [
        ScriptStatement(
            script_name=name, stmt_index=1, target_table="", sql_text="", parsed=True,
            joins=[ScriptJoin(scope="main", join_type="JOIN", table=ref(pairs[0][1]).table,
                              condition=" AND ".join(f"{left} = {right}" for left, right in pairs),
                              keys=[JoinKey(left=ref(left), right=ref(right)) for left, right in pairs])])
        for name, pairs in joins]
    return Mart(tables=metas, columns=columns, lineage=lineage + list(extra_edges), relations=list(relations),
                scripts=statements, script_columns=list(script_columns))


def manual(left: str, left_columns: list[str], right: str, right_columns: list[str], card: str,
           note: str = "") -> Relation:
    """一条手工填写的关联"""
    return Relation(left_table=left, left_columns=left_columns, right_table=right, right_columns=right_columns,
                    cardinality=card, origin="manual", note=note)


def find(relations: list[Relation], left: str, right: str, origin: str | None = None) -> list[Relation]:
    """按左右表（以及来源）找关联"""
    return [r for r in relations if r.left_table == left and r.right_table == right
            and (origin is None or r.origin == origin)]


def one(relations: list[Relation], left: str, right: str, origin: str | None = None) -> Relation:
    """左右表确定时恰好有一条"""
    found = find(relations, left, right, origin)
    assert len(found) == 1, found
    return found[0]


def between(relations: list[Relation], a: str, b: str) -> list[Relation]:
    """两张表之间的全部关联，不分方向，保持输出顺序"""
    return [r for r in relations if {r.left_table, r.right_table} == {a, b}]


def origins(relations: list[Relation]) -> Counter:
    """各来源的条数"""
    return Counter(r.origin for r in relations)


def report_text(section) -> str:
    """报告节合成一段文字，便于查找"""
    return "\n".join(section.lines)


# ── 模拟库 ────────────────────────────────────────────────

@pytest.fixture(scope="module")
def mock_inputs() -> Mart:
    """模拟专题库解析完加工脚本、合并完血缘、还没推导关联的资料"""
    mart, issues = load_mart(MOCK_MART)
    assert mart is not None, issues
    parsed = parse_scripts(MOCK_MART / SCRIPTS_DIR_NAME, mart)
    lineage, _ = merge_lineage(mart, parsed)
    return mart.model_copy(update={"scripts": parsed.statements, "script_columns": parsed.columns,
                                   "lineage": lineage})


@pytest.fixture(scope="module")
def mock_derived(mock_inputs):
    """模拟专题库的推导结果：(关联关系, 报告节)"""
    return derive_relations(mock_inputs)


# ── 对应关系判定 ──────────────────────────────────────────

def meta(grain: list[str], table_id: str = "ods.t") -> TableMeta:
    """只关心粒度的表"""
    schema, name = table_id.split(".")
    return TableMeta(table_id=table_id, layer="贴源层", schema_name=schema, table_name=name, table_cn=name,
                     table_type="全量表", grain_columns=grain)


@pytest.mark.parametrize("left_grain, left_cols, right_grain, right_cols, expected", [
    (["k"], ["k"], ["k"], ["k"], "1:1"),                          # 两侧的键都覆盖各自的粒度
    (["a", "b"], ["x"], ["k"], ["k"], "N:1"),                     # 只有右侧唯一
    (["k"], ["k"], ["a", "b"], ["a"], "1:N"),                     # 只有左侧唯一；右侧复合粒度只用了一半
    (["a", "b"], ["a"], ["c", "d"], ["c"], "N:N"),                # 两侧都有粒度、都没覆盖
    ([], ["k"], ["k"], ["k"], "N:1"),                             # 左侧没有粒度，右侧唯一：右侧唯一就足够
    (["k"], ["k"], [], ["k"], "1:N"),
    ([], ["k"], [], ["k"], "未知"),                               # 两侧都没有粒度
    (["a"], ["x"], [], ["y"], "未知"),                            # 一侧没覆盖、另一侧没有粒度，判断不了
    ([], ["x"], ["a", "b"], ["a"], "未知"),
    (["k"], ["k", "extra"], ["k"], ["k"], "1:1"),                 # 键比粒度多几个字段，仍然唯一
    (["data_dt", "k"], ["k", "data_dt"], ["k"], ["k"], "1:1"),    # 键的写法顺序不影响
    (["data_dt", "k"], ["k"], ["k"], ["k"], "N:1"),               # 复合粒度只用了一半，不唯一
])
def test_cardinality_cases(left_grain, left_cols, right_grain, right_cols, expected):
    assert cardinality(meta(left_grain, "ods.l"), left_cols, meta(right_grain, "ods.r"), right_cols) == expected


# ── 键域 ──────────────────────────────────────────────────

def test_key_domain_connects_fields_taken_directly_and_numbers_them_from_zero():
    mart = make_mart(
        [("ods.s", ["k"], ["k", "v"]), ("dwd.t", ["k"], ["k", "v"]), ("ads.u", ["k"], ["k", "v"])],
        taken=[("dwd.t.k", "ods.s.k"), ("ads.u.k", "dwd.t.k")],
        extra_edges=[LineageEdge(target_table="ads.u", target_column="v", source_table="dwd.t", source_column="v")])

    domains = key_domains(mart)

    assert domains[ref("ods.s.k")] == domains[ref("dwd.t.k")] == domains[ref("ads.u.k")]
    assert ref("ads.u.v") not in domains                  # 非直取的边不连键域，也不建节点
    assert sorted(set(domains.values())) == [0]


def test_key_domain_includes_join_keys_even_without_lineage():
    mart = make_mart([("ods.a", [], ["k"]), ("ods.b", [], ["id"])], joins=[("j.sql", [("ods.a.k", "ods.b.id")])])

    domains = key_domains(mart)

    assert set(domains) == {ref("ods.a.k"), ref("ods.b.id")}
    assert domains[ref("ods.a.k")] != domains[ref("ods.b.id")]


def test_key_domain_uses_passthrough_script_columns_when_the_list_has_no_edge():
    mart = make_mart(
        [("ods.s", [], ["k"]), ("dwd.t", [], ["k"]), ("dwd.u", [], ["k"])],
        script_columns=[
            ScriptColumn(script_name="x.sql", stmt_index=1, target_table="dwd.t", target_column="k",
                         expression="ods.s.k", sources=[ref("ods.s.k")], kind="passthrough"),
            ScriptColumn(script_name="x.sql", stmt_index=2, target_table="dwd.u", target_column="k",
                         expression="f(ods.s.k)", sources=[ref("ods.s.k")], kind="derived")])

    domains = key_domains(mart)

    assert domains[ref("ods.s.k")] == domains[ref("dwd.t.k")]
    assert ref("dwd.u.k") not in domains


def test_key_domain_ids_do_not_depend_on_the_order_of_edges():
    tables = [("ods.s", [], ["k", "m"]), ("dwd.t", [], ["k", "m"]), ("ads.u", [], ["k", "m"])]
    taken = [("dwd.t.k", "ods.s.k"), ("ads.u.k", "dwd.t.k"), ("ads.u.m", "dwd.t.m"), ("dwd.t.m", "ods.s.m")]
    mart = make_mart(tables, taken=taken)
    shuffled = make_mart(tables, taken=list(reversed(taken)))

    assert key_domains(mart) == key_domains(shuffled)
    assert sorted(set(key_domains(mart).values())) == [0, 1]


def test_key_domain_tolerates_fields_missing_from_the_field_list():
    """脚本补出来的血缘边，来源字段可能不在字段清单里：照常入键域，不出错"""
    mart = make_mart([("ods.s", [], ["k"]), ("dwd.t", [], ["k", "x"])], taken=[("dwd.t.x", "ods.s.x")])

    domains = key_domains(mart)

    assert domains[ref("ods.s.x")] == domains[ref("dwd.t.x")]


# ── 排序键 ────────────────────────────────────────────────

def test_relation_rank_orders_by_origin_then_safety_then_same_name_then_name():
    tables = {t.table_id: t for t in make_mart(
        [("ods.a", [], ["x"]), ("ods.b", [], ["x"]), ("ads.c", [], ["x"])]).tables}

    def rel(origin: str, card: str, left: str, left_cols: list[str], right: str, right_cols: list[str]) -> Relation:
        return Relation(left_table=left, left_columns=left_cols, right_table=right, right_columns=right_cols,
                        cardinality=card, origin=origin)

    in_order = [
        rel("manual", "N:N", "ods.a", ["x"], "ods.b", ["y"]),        # 来源优先级压过一切
        rel("script", "N:1", "ods.a", ["x"], "ods.b", ["x"]),        # 同样安全，同名键在前
        rel("script", "N:1", "ads.c", ["x"], "ods.b", ["y"]),        # 不同名之间，下游层的表在左的在前
        rel("script", "1:1", "ods.a", ["x"], "ods.b", ["y"]),        # 再按表名字典序：ods.a 在 ods.b 之前
        rel("script", "N:1", "ods.b", ["x"], "ods.a", ["y"]),
        rel("script", "未知", "ods.a", ["x"], "ods.b", ["x"]),       # 未知比 N:N 安全
        rel("script", "N:N", "ods.a", ["x"], "ods.b", ["x"]),
        rel("lineage", "N:1", "ods.a", ["x"], "ods.b", ["x"]),
        rel("grain_key", "N:1", "ods.a", ["x"], "ods.b", ["x"]),
    ]
    shuffled = in_order[:]
    random.Random(7).shuffle(shuffled)

    assert sorted(shuffled, key=lambda r: relation_rank(r, tables)) == in_order


def test_relation_rank_treats_one_to_many_as_safe_as_many_to_one():
    tables = {t.table_id: t for t in make_mart([("ods.a", [], ["x"]), ("ods.b", [], ["x"])]).tables}
    safe = Relation(left_table="ods.a", left_columns=["x"], right_table="ods.b", right_columns=["x"],
                    cardinality="1:N", origin="manual")
    unsafe = safe.model_copy(update={"cardinality": "N:N"})

    assert relation_rank(safe, tables) < relation_rank(unsafe, tables)


def test_relation_rank_is_total_and_tolerates_unknown_tables():
    a = Relation(left_table="x.p", left_columns=["a"], right_table="x.q", right_columns=["a"],
                 cardinality="N:1", origin="script", note="甲")
    b = a.model_copy(update={"note": "乙"})

    assert relation_rank(a, {}) != relation_rank(b, {})


# ── 模拟库：四种来源 ──────────────────────────────────────

def test_manual_relation_is_kept_first_and_unchanged(mock_inputs, mock_derived):
    relations, _ = mock_derived
    given = mock_inputs.relations[0]

    assert relations[0] == given
    assert relations[0].origin == "manual" and relations[0].cardinality == "1:1"
    assert [r.origin for r in between(relations, ADS_DEP, ADS_LOAN)] == ["manual"]    # 同名粒度键的同一映射被合并


def test_script_relations(mock_derived):
    relations, _ = mock_derived
    loan_cust = one(relations, LOAN_D, CUST, "script")
    self_join = one(relations, ORG_INFO, ORG_INFO, "script")

    assert (loan_cust.left_columns, loan_cust.right_columns, loan_cust.cardinality) == (["cust_no"], ["cust_no"], "N:1")
    assert "23_ads_corp_cust_loan_bal_d.sql" in loan_cust.note
    assert (self_join.left_columns, self_join.right_columns, self_join.cardinality) == (
        ["up_org_no"], ["org_no"], "N:1")                    # 自关联保留不同字段，多的一侧在左
    assert "01_dim_org.sql" in self_join.note
    assert one(relations, DEP_D, CUST, "script").cardinality == "N:1"


def test_script_join_written_twice_is_one_relation(mock_derived):
    relations, _ = mock_derived

    assert len(find(relations, ORG_INFO, ORG_INFO)) == 1        # 01 号脚本两次自关联用的是同一对字段


def test_lineage_relation_passes_through_direct_takes(mock_derived):
    relations, _ = mock_derived
    rel = one(relations, ADS_DEP, CUST, "lineage")

    assert (rel.left_columns, rel.right_columns, rel.cardinality) == (["cust_no"], ["cust_no"], "N:1")
    assert rel.note.startswith("键域传递：")
    assert "21_ads_corp_cust_dep_bal_d.sql" in rel.note
    assert "dwd.corp_dep_acct_d.cust_no = dim.corp_cust.cust_no" in rel.note
    assert "23_ads_corp_cust_loan_bal_d.sql" in one(relations, ADS_LOAN, CUST, "lineage").note


def test_lineage_relation_with_a_composite_key(mock_derived):
    relations, _ = mock_derived
    rel = one(relations, DEP_D, RATE, "lineage")

    assert (rel.left_columns, rel.right_columns, rel.cardinality) == (
        ["ccy_cd", "data_dt"], ["ccy_cd", "data_dt"], "N:1")
    assert "11_dwd_corp_dep_acct_d.sql" in rel.note
    assert one(relations, "ods.core_dep_acct", RATE, "script").cardinality == "N:1"      # 脚本里直接写过的那条


def test_lineage_relation_reaches_tables_that_only_hold_a_copy_of_the_key(mock_derived):
    """机构信息表的 up_org_no 与机构维表的 org_no：机构维表的 org_no 直取自机构信息表的 org_no"""
    relations, _ = mock_derived
    rel = one(relations, ORG_INFO, ORG, "lineage")

    assert (rel.left_columns, rel.right_columns, rel.cardinality) == (["up_org_no"], ["org_no"], "N:1")


def test_grain_key_relation(mock_derived):
    relations, _ = mock_derived
    rel = one(relations, ADS_ORG, ORG, "grain_key")

    assert (rel.left_columns, rel.right_columns, rel.cardinality) == (["org_no"], ["org_no"], "N:1")
    assert rel.note.startswith("同名粒度键：")
    assert one(relations, ADS_DEP, ORG, "grain_key").cardinality == "N:1"
    assert one(relations, ADS_LOAN, ORG, "grain_key").cardinality == "N:1"


def test_grain_key_one_to_one_puts_the_downstream_table_on_the_left(mock_derived):
    relations, _ = mock_derived
    rel = one(relations, ORG, ORG_INFO, "grain_key")

    assert rel.cardinality == "1:1" and rel.left_columns == ["org_no"]
    assert find(relations, ORG_INFO, ORG, "grain_key") == []
    assert one(relations, DEP_D, "ods.core_dep_acct", "grain_key").cardinality == "1:1"


def test_one_pair_can_keep_different_mappings(mock_derived):
    relations, _ = mock_derived
    pair = between(relations, ORG, ORG_INFO)

    assert [(r.origin, r.left_table, r.left_columns, r.right_columns) for r in pair] == [
        ("lineage", ORG_INFO, ["up_org_no"], ["org_no"]),         # 按上级机构号
        ("grain_key", ORG, ["org_no"], ["org_no"]),               # 按机构号
    ]


def test_same_mapping_from_several_sources_keeps_the_highest_priority(mock_derived):
    relations, _ = mock_derived

    # ecif 客户表 → 行业代码表：脚本里写过，同名粒度键也推得出，留脚本
    assert [r.origin for r in between(relations, "ods.ecif_corp_cust", "ods.industry_dict")] == ["script"]
    # 对公客户维表 → 行业代码表：血缘传递和同名粒度键都推得出，留血缘传递
    assert [r.origin for r in between(relations, CUST, "ods.industry_dict")] == ["lineage"]
    # 存款账户日快照 → 对公客户维表：脚本和同名粒度键都有
    assert [r.origin for r in between(relations, DEP_D, CUST)] == ["script"]


def test_each_mapping_appears_once_and_each_pair_has_at_most_three(mock_derived):
    relations, _ = mock_derived

    def mapping(r: Relation):
        return frozenset(tuple(sorted(((r.left_table, a), (r.right_table, b))))
                         for a, b in zip(r.left_columns, r.right_columns))

    assert len({mapping(r) for r in relations}) == len(relations)
    per_pair = Counter(tuple(sorted((r.left_table, r.right_table))) for r in relations)
    assert max(per_pair.values()) <= MAX_MAPPINGS_PER_PAIR


def test_upstream_table_never_joins_downstream_table_by_same_name_key(mock_derived):
    relations, _ = mock_derived

    grain = [r for r in relations if r.origin == "grain_key"]

    assert grain
    assert all(LAYER_NUMBER[r.left_table.split(".")[0]] >= LAYER_NUMBER[r.right_table.split(".")[0]] for r in grain)
    assert find(relations, "ods.core_dep_acct", DEP_D) == []     # 下游层的表总在左边，上游表不会出现在左侧


def test_mock_counts_by_origin(mock_derived):
    relations, section = mock_derived

    assert origins(relations) == {"manual": 1, "script": 7, "lineage": 15, "grain_key": 18}
    assert "手工 1、脚本 7、血缘传递 15、同名粒度键 18" in report_text(section)
    assert "脚本 7、血缘传递 15、同名粒度键 38" in report_text(section)      # 去重之前生成的候选
    assert "合并掉 20 条" in report_text(section)
    assert "键域 29 个，最大 4 个字段" in report_text(section)
    assert "每对表超过 3 种映射而去掉 0 条" in report_text(section)          # 没有一对表超过上限
    assert "1 种 39 对、2 种 1 对、3 种 0 对" in report_text(section)
    assert "对应关系：N:1 36、1:1 5、1:N 0、N:N 0、未知 0" in report_text(section)


def test_output_is_deterministic_and_idempotent(mock_inputs, mock_derived):
    relations, section = mock_derived

    again, again_section = derive_relations(mock_inputs)
    redone, _ = derive_relations(mock_inputs.model_copy(update={"relations": relations}))

    assert again == relations and again_section == section
    assert redone == relations                                  # 已推导过的资料再推导一遍，结果相同


def test_output_does_not_depend_on_the_order_of_tables_columns_and_lineage(mock_inputs, mock_derived):
    relations, _ = mock_derived
    rng = random.Random(3)
    tables, columns, lineage = list(mock_inputs.tables), list(mock_inputs.columns), list(mock_inputs.lineage)
    for items in (tables, columns, lineage):
        rng.shuffle(items)

    shuffled, _ = derive_relations(mock_inputs.model_copy(update={"tables": tables, "columns": columns,
                                                                  "lineage": lineage}))

    assert shuffled == relations


# ── 小资料：脚本来源 ──────────────────────────────────────

def test_script_join_gets_cardinality_from_grain():
    mart = make_mart([("ods.a", ["a_id"], ["a_id", "fk"]), ("ods.b", ["b_id"], ["b_id"])],
                     joins=[("load.sql", [("ods.a.fk", "ods.b.b_id")])])

    relations, _ = derive_relations(mart)

    rel = one(relations, "ods.a", "ods.b", "script")
    assert (rel.left_columns, rel.right_columns, rel.cardinality) == (["fk"], ["b_id"], "N:1")
    assert "load.sql" in rel.note and "ods.a.fk = ods.b.b_id" in rel.note


def test_one_to_many_is_flipped_to_many_to_one():
    mart = make_mart([("ods.a", ["a_id"], ["a_id", "fk"]), ("ods.b", ["b_id"], ["b_id"])],
                     joins=[("load.sql", [("ods.b.b_id", "ods.a.fk")])])         # 脚本里把唯一的一侧写在左边

    relations, _ = derive_relations(mart)

    assert find(relations, "ods.b", "ods.a") == []
    rel = one(relations, "ods.a", "ods.b", "script")
    assert (rel.left_columns, rel.right_columns, rel.cardinality) == (["fk"], ["b_id"], "N:1")


def test_many_to_many_kept_when_the_script_really_joins_that_way():
    mart = make_mart([("ods.a", ["a_id"], ["a_id", "k"]), ("ods.b", ["b_id"], ["b_id", "k"])],
                     joins=[("load.sql", [("ods.a.k", "ods.b.k")])])

    relations, section = derive_relations(mart)

    assert one(relations, "ods.a", "ods.b", "script").cardinality == "N:N"
    assert "N:N 1" in report_text(section)


def test_unknown_cardinality_when_grain_is_missing():
    mart = make_mart([("ods.a", [], ["k"]), ("ods.b", [], ["k"])], joins=[("load.sql", [("ods.a.k", "ods.b.k")])])

    relations, _ = derive_relations(mart)

    assert [r.cardinality for r in relations] == ["未知"]


def test_symmetric_cardinality_puts_the_downstream_layer_on_the_left():
    mart = make_mart([("ods.a", ["k"], ["k"]), ("dwd.b", ["k"], ["k"])], joins=[("load.sql", [("ods.a.k", "dwd.b.k")])])

    relations, _ = derive_relations(mart)

    assert one(relations, "dwd.b", "ods.a", "script").cardinality == "1:1"
    assert find(relations, "ods.a", "dwd.b") == []


def test_composite_keys_form_one_relation_per_table_pair():
    mart = make_mart(
        [("ods.a", ["a_id"], ["a_id", "x", "y", "z"]), ("ods.b", ["x", "y"], ["x", "y"]), ("ods.c", ["z"], ["z"])],
        joins=[("load.sql", [("ods.a.x", "ods.b.x"), ("ods.a.y", "ods.b.y"), ("ods.a.z", "ods.c.z")])])

    relations, _ = derive_relations(mart)

    two = one(relations, "ods.a", "ods.b", "script")
    assert (two.left_columns, two.right_columns, two.cardinality) == (["x", "y"], ["x", "y"], "N:1")
    assert one(relations, "ods.a", "ods.c", "script").left_columns == ["z"]


def test_same_mapping_in_several_scripts_is_one_relation_naming_them():
    mart = make_mart([("ods.a", [], ["k"]), ("ods.b", ["k"], ["k"])],
                     joins=[("first.sql", [("ods.a.k", "ods.b.k")]), ("second.sql", [("ods.b.k", "ods.a.k")])])

    relations, _ = derive_relations(mart)

    rel = one(relations, "ods.a", "ods.b", "script")
    assert "first.sql" in rel.note and "second.sql" in rel.note


def test_note_names_at_most_three_scripts_then_counts_the_rest():
    mart = make_mart([("ods.a", [], ["k"]), ("ods.b", ["k"], ["k"])],
                     joins=[(f"s{i}.sql", [("ods.a.k", "ods.b.k")]) for i in range(1, 6)])

    relations, _ = derive_relations(mart)

    note = one(relations, "ods.a", "ods.b", "script").note
    assert "s1.sql、s2.sql、s3.sql 等 5 个脚本" in note and "s4.sql" not in note


def test_keys_written_in_opposite_directions_merge_into_one_composite_key():
    mart = make_mart([("ods.a", [], ["x", "y"]), ("ods.b", ["x", "y"], ["x", "y"])],
                     joins=[("load.sql", [("ods.a.x", "ods.b.x"), ("ods.b.y", "ods.a.y")])])

    relations, _ = derive_relations(mart)

    rel = one(relations, "ods.a", "ods.b", "script")
    assert (rel.left_columns, rel.right_columns, rel.cardinality) == (["x", "y"], ["x", "y"], "N:1")


def test_alternatives_from_one_condition_are_separate_mappings_not_a_composite_key():
    """同一个条件追溯出几个来源（UNION），同一张表的几个字段对着同一个字段：几选一，不是复合键"""
    mart = make_mart([("ods.t", [], ["x", "z"]), ("ods.u", ["k"], ["k"])],
                     joins=[("load.sql", [("ods.t.x", "ods.u.k"), ("ods.t.z", "ods.u.k")])])

    relations, _ = derive_relations(mart)

    assert sorted((r.left_columns, r.right_columns) for r in relations if r.origin == "script") == [
        (["x"], ["k"]), (["z"], ["k"])]


def test_script_join_with_an_external_table_or_an_unlisted_field_is_not_a_script_relation():
    mart = make_mart([("ods.a", ["k"], ["k"]), ("ods.b", ["k"], ["k"])],
                     joins=[("ext.sql", [("ods.a.k", "ext.outside.k")]), ("gap.sql", [("ods.a.k", "ods.b.missing")])])

    relations, section = derive_relations(mart)

    assert [r for r in relations if r.origin == "script"] == []
    assert "1 种有一侧不在表清单里" in report_text(section)
    assert "1 种用到字段清单里没有的字段" in report_text(section)


# ── 小资料：血缘传递 ──────────────────────────────────────

def lineage_mart() -> Mart:
    """ods.s 的 k 直取到 dwd.t，再直取到 ads.u；脚本里 ods.s.k = ods.d.id。

    三张表都没有粒度，维表的键用通用名 id，这样同名粒度键不会混进来"""
    tables = [("ods.s", [], ["k"]), ("dwd.t", [], ["k"]), ("ads.u", [], ["k"]), ("ods.d", ["id"], ["id"])]
    return make_mart(tables, taken=[("dwd.t.k", "ods.s.k"), ("ads.u.k", "dwd.t.k")],
                     joins=[("load.sql", [("ods.s.k", "ods.d.id")])])


def test_lineage_candidates_cover_every_table_holding_the_same_key():
    relations, _ = derive_relations(lineage_mart())

    assert {(r.left_table, r.right_table, r.origin) for r in relations} == {
        ("ods.s", "ods.d", "script"), ("dwd.t", "ods.d", "lineage"), ("ads.u", "ods.d", "lineage")}
    assert all(r.cardinality == "N:1" for r in relations)
    assert "键域传递：load.sql 中 ods.s.k = ods.d.id" in one(relations, "ads.u", "ods.d").note


def test_table_with_two_fields_in_one_domain_is_not_a_candidate():
    mart = make_mart(
        [("ods.s", [], ["k"]), ("dwd.t", [], ["k1", "k2"]), ("ods.d", ["id"], ["id"])],
        taken=[("dwd.t.k1", "ods.s.k"), ("dwd.t.k2", "ods.s.k")], joins=[("load.sql", [("ods.s.k", "ods.d.id")])])

    relations, _ = derive_relations(mart)

    assert [r.origin for r in relations] == ["script"]


def test_composite_key_needs_every_field_in_the_table():
    """复合键传递：每个键域里都要有字段落在同一张表里"""
    mart = make_mart(
        [("ods.s", [], ["k1", "k2"]), ("dwd.full", [], ["k1", "k2"]), ("dwd.half", [], ["k1"]),
         ("ods.d", ["x", "y"], ["x", "y"])],
        taken=[("dwd.full.k1", "ods.s.k1"), ("dwd.full.k2", "ods.s.k2"), ("dwd.half.k1", "ods.s.k1")],
        joins=[("load.sql", [("ods.s.k1", "ods.d.x"), ("ods.s.k2", "ods.d.y")])])

    relations, _ = derive_relations(mart)

    rel = one(relations, "dwd.full", "ods.d", "lineage")
    assert (rel.left_columns, rel.right_columns) == (["k1", "k2"], ["x", "y"])
    assert not [r for r in relations if "dwd.half" in (r.left_table, r.right_table)]


def test_gap_field_in_a_script_join_still_reaches_tables_holding_a_real_copy():
    """脚本关联用的 ods.s.gap 不在字段清单里，这条脚本关联本身不计；但 dwd.t.k 直取自它，dwd.t 与维表的关联照样推得出"""
    mart = make_mart([("ods.s", [], ["other"]), ("dwd.t", [], ["k"]), ("ods.d", ["id"], ["id"])],
                     taken=[("dwd.t.k", "ods.s.gap")], joins=[("load.sql", [("ods.s.gap", "ods.d.id")])])

    relations, section = derive_relations(mart)

    assert [(r.left_table, r.left_columns, r.right_table, r.origin) for r in relations] == [
        ("dwd.t", ["k"], "ods.d", "lineage")]
    assert "1 种用到字段清单里没有的字段" in report_text(section)


def test_external_table_in_a_script_join_still_reaches_tables_holding_a_copy():
    mart = make_mart([("dwd.t", [], ["k"]), ("ods.d", ["id"], ["id"])],
                     taken=[("dwd.t.k", "ext.src.k")], joins=[("load.sql", [("ext.src.k", "ods.d.id")])])

    relations, section = derive_relations(mart)

    assert [(r.left_table, r.right_table, r.origin) for r in relations] == [("dwd.t", "ods.d", "lineage")]
    assert "1 种有一侧不在表清单里" in report_text(section)


def test_gap_and_external_lineage_edges_do_not_break_the_derivation():
    mart = make_mart([("ods.s", [], ["k"]), ("dwd.t", [], ["k", "x"]), ("ods.d", ["id"], ["id"])],
                     taken=[("dwd.t.k", "ods.s.k"), ("dwd.t.x", "ods.s.gone"), ("dwd.t.x", "ext.src.x")],
                     joins=[("load.sql", [("ods.s.k", "ods.d.id")])])

    relations, _ = derive_relations(mart)

    assert {(r.left_table, r.right_table) for r in relations} == {("ods.s", "ods.d"), ("dwd.t", "ods.d")}
    assert all(r.left_columns == ["k"] for r in relations)           # 只有真实存在的字段出现在关联里


def test_many_to_many_candidates_are_dropped_but_the_script_one_is_kept():
    mart = make_mart(
        [("ods.x", ["a", "b"], ["a", "b", "k"]), ("dwd.x2", ["c", "d"], ["c", "d", "k"]),
         ("ods.y", ["e", "f"], ["e", "f", "k"])],
        taken=[("dwd.x2.k", "ods.x.k")], joins=[("load.sql", [("ods.x.k", "ods.y.k")])])

    relations, section = derive_relations(mart)

    assert one(relations, "ods.x", "ods.y", "script").cardinality == "N:N"
    assert not [r for r in relations if "dwd.x2" in (r.left_table, r.right_table)]
    assert "传递出的 N:N 丢掉 1 条" in report_text(section)


def test_unknown_candidates_are_kept():
    mart = make_mart([("ods.s", [], ["k"]), ("dwd.t", [], ["k"]), ("ods.d", [], ["k"])],
                     taken=[("dwd.t.k", "ods.s.k")], joins=[("load.sql", [("ods.s.k", "ods.d.k")])])

    relations, _ = derive_relations(mart)

    assert one(relations, "dwd.t", "ods.d", "lineage").cardinality == "未知"


def test_limits_are_the_documented_values():
    assert (MAX_DOMAIN_SIZE, MAX_PROPAGATION_PER_JOIN, MAX_GRAIN_FANIN, MAX_MAPPINGS_PER_PAIR) == (50, 400, 150, 3)
    assert GENERIC_KEY_NAMES == {"id", "uuid", "guid", "row_id", "rowid", "pk_id"}


def test_domain_larger_than_the_limit_does_not_propagate(monkeypatch):
    mart = make_mart(
        [(f"dwd.t{i}", [], ["k"]) for i in range(1, 5)] + [("ods.d", ["id"], ["id"])],
        taken=[(f"dwd.t{i + 1}.k", f"dwd.t{i}.k") for i in range(1, 4)],
        joins=[("load.sql", [("dwd.t1.k", "ods.d.id")])])

    normal, _ = derive_relations(mart)
    monkeypatch.setattr(relations_module, "MAX_DOMAIN_SIZE", 3)
    limited, section = derive_relations(mart)

    assert origins(normal) == {"script": 1, "lineage": 3}
    assert origins(limited) == {"script": 1}                    # 键域有 4 个字段，超过上限 3：只剩脚本里写过的那条
    assert "超过 3 个字段的键域挡掉 1 种脚本映射" in report_text(section)


def test_propagation_larger_than_the_limit_skips_the_whole_script_join(monkeypatch):
    tables = [("ods.s", [], ["k"]), ("dwd.t", [], ["k"]), ("ads.u", [], ["k"]),
              ("ods.d", ["id"], ["id"]), ("dwd.e", ["id"], ["id"])]
    mart = make_mart(tables, taken=[("dwd.t.k", "ods.s.k"), ("ads.u.k", "ods.s.k"), ("dwd.e.id", "ods.d.id")],
                     joins=[("load.sql", [("ods.s.k", "ods.d.id")])])

    normal, _ = derive_relations(mart)
    monkeypatch.setattr(relations_module, "MAX_PROPAGATION_PER_JOIN", 5)       # 3 × 2 = 6 对，超过 5
    limited, section = derive_relations(mart)

    assert origins(normal) == {"script": 1, "lineage": 5}
    assert origins(limited) == {"script": 1}
    assert "超过 5 对而整条跳过 1 种脚本映射" in report_text(section)


# ── 小资料：同名粒度键 ────────────────────────────────────

def test_grain_key_relations_follow_the_cardinality_rule():
    """维表 dim.t 的粒度是 code；三张事实表都有 code 字段"""
    mart = make_mart([("dim.t", ["code"], ["code", "name"]), ("dwd.f1", ["f1_id"], ["f1_id", "code"]),
                      ("dwd.f2", [], ["code"]), ("ads.f3", ["code"], ["code", "amt"])])

    relations, _ = derive_relations(mart)

    assert origins(relations) == {"grain_key": 3}
    assert one(relations, "dwd.f1", "dim.t").cardinality == "N:1"
    assert one(relations, "dwd.f2", "dim.t").cardinality == "N:1"           # 没有粒度的表也能按维表的粒度关联
    assert one(relations, "ads.f3", "dim.t").cardinality == "1:1"           # 它的粒度也是 code，落在同一个键上


def test_grain_key_needs_every_grain_column_by_name():
    mart = make_mart([("dim.t", ["k1", "k2"], ["k1", "k2"]), ("dwd.full", [], ["k1", "k2"]), ("dwd.half", [], ["k1"])])

    relations, _ = derive_relations(mart)

    assert [(r.left_table, r.right_table, r.left_columns) for r in relations] == [("dwd.full", "dim.t", ["k1", "k2"])]


def test_generic_key_names_are_excluded():
    tables = [("dim.t", ["id"], ["id"]), ("dwd.a", [], ["id"]), ("dwd.b", [], ["id"]),
              ("dim.u", ["uuid"], ["uuid"]), ("dwd.c", [], ["uuid"])]

    relations, _ = derive_relations(make_mart(tables))

    assert relations == []


def test_grain_with_one_generic_and_one_real_column_is_still_used():
    mart = make_mart([("dim.t", ["id", "data_dt"], ["id", "data_dt"]), ("dwd.a", [], ["id", "data_dt"]),
                      ("dwd.b", [], ["id"])])

    relations, _ = derive_relations(mart)

    assert [(r.left_table, r.right_table, r.left_columns) for r in relations] == [
        ("dwd.a", "dim.t", ["data_dt", "id"])]


def test_layer_rule_upstream_table_does_not_join_downstream_table():
    """贴源层、整合层的表虽然有 code 字段，但专题层 t 的粒度不能被上游表关联"""
    mart = make_mart([("ads.t", ["code"], ["code"]), ("ods.s", ["s_id"], ["s_id", "code"]),
                      ("dwd.m", ["m_id"], ["m_id", "code"])])

    relations, _ = derive_relations(mart)

    assert relations == []


def test_layer_rule_same_layer_and_downstream_to_upstream_are_allowed():
    mart = make_mart([("dwd.t", ["code"], ["code"]), ("dwd.same", ["s"], ["s", "code"]),
                      ("ads.down", ["d"], ["d", "code"]), ("ods.up", ["u"], ["u", "code"])])

    relations, _ = derive_relations(mart)

    assert {(r.left_table, r.right_table) for r in relations} == {("dwd.same", "dwd.t"), ("ads.down", "dwd.t")}


def test_grain_key_fan_in_limit_skips_the_whole_table(monkeypatch):
    mart = make_mart([("dim.t", ["code"], ["code"]), *[(f"dwd.f{i}", [], ["code"]) for i in range(1, 4)]])

    normal, _ = derive_relations(mart)
    monkeypatch.setattr(relations_module, "MAX_GRAIN_FANIN", 2)
    limited, section = derive_relations(mart)

    assert origins(normal) == {"grain_key": 3}
    assert limited == []
    assert "1 张表被超过 2 张表关联，整张跳过（少生成 3 条）" in report_text(section)


# ── 手工关联、去重与每对上限 ──────────────────────────────

def test_manual_relation_keeps_its_written_direction_and_cardinality():
    given = manual("ods.b", ["b_id"], "ods.a", ["fk"], "1:N", "手工写的")
    mart = make_mart([("ods.a", ["a_id"], ["a_id", "fk"]), ("ods.b", ["b_id"], ["b_id"])], relations=[given])

    relations, _ = derive_relations(mart)

    assert relations == [given]


def test_manual_relation_wins_over_a_derived_one_with_the_same_mapping():
    given = manual("ods.b", ["b_id"], "ods.a", ["fk"], "1:N", "手工写的")
    mart = make_mart([("ods.a", ["a_id"], ["a_id", "fk"]), ("ods.b", ["b_id"], ["b_id"])], relations=[given],
                     joins=[("load.sql", [("ods.a.fk", "ods.b.b_id")])])

    relations, _ = derive_relations(mart)

    assert relations == [given]


def test_derived_relations_are_recomputed_not_trusted_from_the_input():
    """输入里来源不是 manual 的关联是上一次推导的结果，这次重新推导，不原样带进来"""
    stale = Relation(left_table="ods.a", left_columns=["x"], right_table="ods.b", right_columns=["y"],
                     cardinality="N:1", origin="script", note="旧的")
    mart = make_mart([("ods.a", [], ["x"]), ("ods.b", [], ["y"])], relations=[stale])

    relations, _ = derive_relations(mart)

    assert relations == []


def pair_mart() -> Mart:
    """p、q 两张表之间，脚本里写过五种不同的字段映射"""
    return make_mart(
        [("ods.p", ["pk"], ["pk", "a", "b", "c"]), ("ods.q", ["qk"], ["qk", "a", "b", "c"])],
        joins=[("m1.sql", [("ods.p.a", "ods.q.qk")]),              # 右侧唯一，N:1，字段不同名
               ("m2.sql", [("ods.q.a", "ods.p.pk")]),              # 方向反过来，N:1，q 在左
               ("m3.sql", [("ods.p.pk", "ods.q.qk")]),             # 1:1
               ("m4.sql", [("ods.p.b", "ods.q.b")]),               # N:N，同名
               ("m5.sql", [("ods.p.c", "ods.q.c")])])              # N:N，同名


def test_at_most_three_mappings_per_pair_best_first():
    relations, section = derive_relations(pair_mart())

    kept = between(relations, "ods.p", "ods.q")
    assert [(r.left_table, r.left_columns, r.right_columns, r.cardinality) for r in kept] == [
        ("ods.p", ["a"], ["qk"], "N:1"),
        ("ods.p", ["pk"], ["qk"], "1:1"),
        ("ods.q", ["a"], ["pk"], "N:1"),
    ]
    assert len(relations) == 3
    assert "每对表超过 3 种映射而去掉 2 条" in report_text(section)
    assert "1 种 0 对、2 种 0 对、3 种 1 对" in report_text(section)


def test_manual_relations_are_never_cut_by_the_pair_cap():
    columns = ["a", "b", "c", "d"]
    given = [manual("ods.p", [name], "ods.q", [name], "N:N") for name in columns]
    mart = make_mart([("ods.p", [], columns), ("ods.q", [], columns)], relations=given,
                     joins=[("m1.sql", [("ods.p.a", "ods.q.b")])])

    relations, _ = derive_relations(mart)

    assert relations == given                                   # 4 条手工都在，脚本里那条被挤掉


def test_mapping_distribution_counts_pairs():
    mart = pair_mart()
    extra = make_mart([("ods.r", ["rk"], ["rk", "a"])], joins=[("m6.sql", [("ods.r.a", "ods.p.pk")])])
    mart = mart.model_copy(update={"tables": mart.tables + extra.tables, "columns": mart.columns + extra.columns,
                                   "scripts": mart.scripts + extra.scripts})

    _, section = derive_relations(mart)

    assert "涉及 2 对表" in report_text(section)
    assert "1 种 1 对、2 种 0 对、3 种 1 对" in report_text(section)


def test_report_lists_the_most_related_tables_for_manual_review():
    tables = [("dim.t", ["code"], ["code"])] + [(f"dwd.f{i}", [], ["code"]) for i in range(1, 8)]

    _, section = derive_relations(make_mart(tables))

    line = next(line for line in section.lines if line.startswith("- 被关联次数最多的 5 张表"))
    assert line.startswith("- 被关联次数最多的 5 张表（涉及的表数 / 关联条数）：dim.t（7 张 / 7 条）")
    assert line.count("张 /") == 5


def test_empty_mart_gives_an_empty_result_and_a_report():
    relations, section = derive_relations(Mart())

    assert relations == []
    assert section.title == SECTION_RELATIONS
    assert "关联 0 条，涉及 0 对表" in report_text(section)


# ── 跨模块验收：推导出的关联让标准 SQL 通过 R6 ────────────

GOLD = [q for q in load_questions() if q.expect == "sql"]


@pytest.fixture(scope="module")
def validator(mock_store) -> SqlValidator:
    """用模拟专题库补全之后（含推导出的关联）的元数据做校验"""
    return SqlValidator(mock_store)


@pytest.mark.parametrize("question", GOLD, ids=[q.id for q in GOLD])
def test_gold_sql_has_no_errors_and_no_warnings_with_derived_relations(validator, question):
    report = validator.validate(question.gold_sql, expected_tables=question.gold_tables,
                                expected_columns=question.gold_columns)

    assert [(i.rule, i.message) for i in report.issues] == []


def test_gold_sql_count():
    assert len(GOLD) == 35
