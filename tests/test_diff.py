"""元数据内容差异、需确认项阈值和报告的离线测试。"""
from copy import deepcopy

import pytest

from metadata.diff import MetadataDiff, PARSED_COUNT, confirm_items, diff_stores, render_diff
from metadata.examples import RawExample
from metadata.models import ColumnMeta, Example, LineageEdge, Mart, Relation, ScriptStatement, TableMeta
from metadata.store import MetadataStore
from nl2sql.validator import ValidationIssue, ValidationReport


def table(name: str, cn: str = "测试表") -> TableMeta:
    """构造虚构的测试表。"""
    return TableMeta(table_id=name, schema_name="ads", table_name=name.split(".")[-1],
                     table_cn=cn, layer="专题层", table_type="全量表", grain_columns=["id"])


def column(name: str, cn: str = "编号", ordinal: int = 1, tid: str = "ads.a") -> ColumnMeta:
    """构造测试字段。"""
    return ColumnMeta(table_id=tid, column_name=name, column_cn=cn, ordinal=ordinal, data_type="int")


def compare(old: Mart, new: Mart, **kwargs) -> MetadataDiff:
    """通过只读入口比较两个内存资料集。"""
    return diff_stores(MetadataStore.from_mart(old), MetadataStore.from_mart(new), **kwargs)


def example(requirement: str, sql: str, number: int = 1) -> Example:
    """构造带文件内编号的样例。"""
    return Example(example_id=f"a.sql#{number}", requirement=requirement, sql_text=sql,
                   source_file="a.sql", line_no=number)


def test_table_and_column_add_remove_change():
    old = Mart(tables=[table("ads.a"), table("ads.deleted")], columns=[column("id"), column("gone", "旧字段", 2)])
    new = deepcopy(old)
    new.tables = [table("ads.a").model_copy(update={"description": "新说明", "grain_columns": ["key"]}), table("ads.new")]
    new.columns = [column("id").model_copy(update={"is_pk": True, "enum_values": "1=是"}), column("fresh", "新字段", 3)]
    diff = compare(old, new)
    assert set(diff.tables.added) == {"ads.new"} and set(diff.tables.removed) == {"ads.deleted"}
    assert diff.tables.changed["ads.a"]["description"] == ("", "新说明")
    assert diff.tables.changed["ads.a"]["grain_columns"] == (["id"], ["key"])
    assert set(diff.columns.added) == {("ads.a", "fresh")}
    assert set(diff.columns.removed) == {("ads.a", "gone")}
    assert diff.columns.changed[("ads.a", "id")]["is_pk"] == (False, True)


def test_renames_chinese_first_then_type_ordinal_and_one_to_one():
    old = Mart(tables=[table("ads.a")], columns=[column("x", "金额"), column("y", "其他", 2), column("z", "金额", 3)])
    new = Mart(tables=old.tables, columns=[column("n", "金额", 9), column("m", "新中文", 2)])
    diff = compare(old, new)
    assert diff.column_renames == [(("ads.a", "x"), ("ads.a", "n")), (("ads.a", "y"), ("ads.a", "m"))]
    assert len(diff.columns.removed) == 3 and len(diff.columns.added) == 2
    other = Mart(tables=[table("ads.b")], columns=[column("x", tid="ads.b")])
    assert compare(Mart(tables=old.tables, columns=[column("x")]), other).table_renames == [("ads.a", "ads.b")]


def test_table_rename_overlap_threshold():
    old = Mart(tables=[table("ads.a")], columns=[column(str(i)) for i in range(5)])
    new = Mart(tables=[table("ads.b")], columns=[column(str(i), tid="ads.b") for i in range(1, 6)])
    assert compare(old, new).table_renames == [("ads.a", "ads.b")]
    new.columns.pop(0)
    assert compare(old, new).table_renames == []


def test_chinese_rename_match_precedes_another_columns_ordinal_fallback():
    old = Mart(tables=[table("ads.a")], columns=[column("a", "甲", 1), column("b", "乙", 2)])
    new = Mart(tables=old.tables, columns=[column("n", "乙", 1)])
    assert compare(old, new).column_renames == [(("ads.a", "b"), ("ads.a", "n"))]


def test_lineage_relations_and_script_identity():
    edge = LineageEdge(target_table="ads.a", target_column="id", source_table="ods.a", source_column="id")
    rel = Relation(left_table="ads.a", left_columns=["id"], right_table="ads.b", right_columns=["id"], cardinality="1:1")
    stmt = ScriptStatement(script_name="load.sql", target_table="ads.a", stmt_index=3, sql_text="insert into ads.a select 1", parsed=True)
    old = Mart(lineage=[edge, edge.model_copy(update={"source_column": "gone"})], relations=[rel], scripts=[stmt])
    new = Mart(lineage=[edge.model_copy(update={"transform": "id + 1", "origin": "script"}),
                        edge.model_copy(update={"source_column": "new"})],
               relations=[rel.model_copy(update={"cardinality": "N:1", "origin": "script"})],
               scripts=[stmt.model_copy(update={"stmt_index": 9, "sql_text": "bad sql", "parsed": False})])
    diff = compare(old, new)
    assert len(diff.lineage.added) == len(diff.lineage.removed) == len(diff.lineage.changed) == 1
    assert len(diff.relations.changed) == 1
    assert diff.scripts.changed[("load.sql", "ads.a", 1)]["parsed"] == (True, False)
    assert len(diff.scripts.changed[("load.sql", "ads.a", 1)]["sha256"][0]) == 64
    assert diff.baseline_counts[PARSED_COUNT] == 1 and diff.candidate_counts[PARSED_COUNT] == 0
    text = render_diff(diff)
    assert "能解析变成不能解析的语句：1 项" in text and "内容变了的文件：1 项" in text


def test_script_occurrence_per_target_and_file_add_delete():
    def stmt(file, target, index, sql):
        return ScriptStatement(script_name=file, target_table=target, stmt_index=index, sql_text=sql, parsed=True)
    old = Mart(scripts=[stmt("same.sql", "ads.a", 1, "a"), stmt("same.sql", "ads.a", 5, "b"), stmt("gone.sql", "ads.x", 1, "x")])
    new = Mart(scripts=[stmt("same.sql", "ads.b", 1, "new"), stmt("same.sql", "ads.a", 2, "a"),
                        stmt("same.sql", "ads.a", 3, "changed"), stmt("new.sql", "ads.x", 1, "x")])
    diff = compare(old, new)
    assert list(diff.scripts.changed) == [("same.sql", "ads.a", 2)]
    text = render_diff(diff)
    assert "新增文件：1 项\n  - new.sql" in text and "删除文件：1 项\n  - gone.sql" in text


def test_examples_content_identity_modification_and_invalid_reasons():
    old = Mart(examples=[example("保留", "select 1"), example("改写", "select 2", 2),
                         example("删除", "select 3", 3), example("失效", "select 4", 4)])
    new = Mart(examples=[example("保留", "select 1", 99), example("改写", "select 22", 7), example("新增", "select 5", 8)])
    raw = RawExample("失效", "", "select 4", "input.sql", 1)
    validate = lambda sql: ValidationReport([ValidationIssue(f"R{i}", "错误", f"错误{i}") for i in range(1, 5)])
    diff = compare(old, new, candidate_examples=[raw], validate=validate)
    assert [e.requirement for e in diff.examples.added.values()] == ["新增"]
    assert [e.requirement for e in diff.examples.removed.values()] == ["删除"]
    assert [(a.sql_text, b.sql_text) for a, b in diff.example_changes] == [("select 2", "select 22")]
    assert diff.invalid_examples[0][1] == ["R1 错误1", "R2 错误2", "R3 错误3"]
    assert "失效：R1 错误1；R2 错误2；R3 错误3" in render_diff(diff)


def test_confirm_all_rules_in_order():
    diff = MetadataDiff({"表": 100, "字段": 100, "血缘": 100, "关联关系": 100, PARSED_COUNT: 100, "字段向量": 100},
                        {"表": 121, "字段": 121, "血缘": 94, "关联关系": 90, PARSED_COUNT: 0, "字段向量": 0})
    diff.tables.removed["old"] = {}
    diff.column_renames.append((("ads.a", "x"), ("ads.a", "y")))
    diff.invalid_examples.append((example("失效", "x"), []))
    assert confirm_items(diff) == ["删了 1 张表、0 个字段", "疑似改名 1 处（表 0、字段 1）", "失效样例 1 条",
                                   "血缘 100 → 94（少 6.0%）", "关联关系 100 → 90（少 10.0%）",
                                   f"{PARSED_COUNT} 100 → 0（少 100.0%）", "表 100 → 121（多 21.0%）",
                                   "字段 100 → 121（多 21.0%）", "字段向量不全：0 / 121（上一版有向量）"]


@pytest.mark.parametrize("kind", ["表", "字段", "血缘", "关联关系", PARSED_COUNT])
def test_strict_drop_threshold(kind):
    assert confirm_items(MetadataDiff({kind: 100}, {kind: 95})) == []
    assert "少 6.0%" in confirm_items(MetadataDiff({kind: 100}, {kind: 94}))[0]


@pytest.mark.parametrize("kind", ["表", "字段"])
def test_strict_growth_and_zero_baseline(kind):
    assert confirm_items(MetadataDiff({kind: 100}, {kind: 120})) == []
    assert "多 21.0%" in confirm_items(MetadataDiff({kind: 100}, {kind: 121}))[0]
    assert confirm_items(MetadataDiff({kind: 0}, {kind: 5})) == []


def test_description_or_small_growth_is_not_pending():
    old = Mart(tables=[table(f"ads.t{i}") for i in range(5)])
    new = deepcopy(old)
    new.tables[0].description = "说明变了"
    assert confirm_items(compare(old, new)) == []
    new.tables.append(table("ads.more"))
    assert confirm_items(compare(old, new)) == []


def test_report_sections_limits_escape_and_extras():
    old = Mart(tables=[table("ads.a")])
    new = deepcopy(old)
    new.tables[0].description = "a|b\n第二行"
    new.tables += [table(f"ads.t{i}") for i in range(35)]
    text = render_diff(compare(old, new), header=["- 版本：test"], extra_sections=[("影响面（零成本回归）", ["- 通过"])])
    titles = ["需确认项", "数量", "表", "字段", "血缘", "关联关系", "加工脚本", "样例", "影响面（零成本回归）"]
    assert text.startswith("# 元数据差异报告\n")
    assert [text.index(f"\n## {t}\n") for t in titles] == sorted(text.index(f"\n## {t}\n") for t in titles)
    assert "等共 35 项" in text and "ads.t9\n" not in text
    assert "description： → a\\|b 第二行" in text and "## 需确认项\n\n没有" in text
