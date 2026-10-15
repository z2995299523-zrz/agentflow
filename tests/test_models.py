"""数据模型的测试：新增字段的默认值和字段引用的可哈希性"""
import pytest
from pydantic import ValidationError

from metadata.models import (
    CARDINALITIES,
    DERIVED_CARDINALITIES,
    FieldRef,
    LineageEdge,
    Mart,
    ScriptColumn,
    ScriptStatement,
)


def test_field_ref_is_hashable_and_frozen():
    a = FieldRef(table="ods.acct", column="cust_no")

    # 相同内容的引用相等，可以去重、当字典的键
    assert {a, FieldRef(table="ods.acct", column="cust_no")} == {a}
    with pytest.raises(ValidationError):
        a.column = "acct_no"


def test_lineage_edge_defaults_to_list_origin_and_not_passthrough():
    edge = LineageEdge(target_table="ads.t", target_column="c", source_table="ods.s", source_column="c")

    assert edge.origin == "list"
    assert edge.is_passthrough is False


def test_derived_cardinalities_extend_manual_ones():
    # 手工关联仍只允许三种，推导结果另有两种
    assert CARDINALITIES == ("N:1", "1:1", "1:N")
    assert DERIVED_CARDINALITIES[:3] == CARDINALITIES
    assert set(DERIVED_CARDINALITIES[3:]) == {"N:N", "未知"}


def test_script_models_default_to_empty_lists():
    statement = ScriptStatement(script_name="a.sql", stmt_index=1, target_table="ads.t",
                                sql_text="INSERT ...", parsed=False)
    column = ScriptColumn(script_name="a.sql", stmt_index=1, target_table="ads.t",
                          target_column="c", expression="NULL", kind="null")

    assert statement.filters == [] and statement.joins == [] and statement.select_sql == ""
    assert column.sources == []
    assert Mart().scripts == [] and Mart().script_columns == [] and Mart().examples == []
