"""结果集比对规则的测试"""
from datetime import date
from decimal import Decimal

from eval.compare import compare_results, normalize_cell


def test_identical_results_match():
    rows = [("上海分行", Decimal("100.00")), ("北京分行", Decimal("250.50"))]

    assert compare_results(rows, list(rows))


def test_numbers_are_rounded_to_two_places():
    assert compare_results([(Decimal("100.004"),)], [(100.0,)])
    assert compare_results([(1.005,)], [(Decimal("1.01"),)])       # 四舍五入，不是银行家舍入
    assert not compare_results([(1.00,)], [(1.01,)])


def test_int_float_and_decimal_are_interchangeable():
    assert compare_results([(3,)], [(Decimal("3.00"),)])
    assert compare_results([(3.0,)], [(3,)])


def test_negative_zero_equals_zero():
    assert normalize_cell(-0.001) == Decimal("0.00")
    assert compare_results([(-0.001,)], [(0,)])


def test_column_order_does_not_matter():
    gold = [("a", 1), ("b", 2)]
    pred = [(1, "a"), (2, "b")]

    assert compare_results(gold, pred)


def test_extra_columns_in_prediction_are_allowed():
    gold = [("上海分行", 10)]
    pred = [("2000", "上海分行", 10, 99)]

    assert compare_results(gold, pred)


def test_missing_column_in_prediction_fails():
    assert not compare_results([("a", 1)], [("a",)])
    assert not compare_results([("a", 1)], [("a", 2)])


def test_row_count_must_match():
    assert not compare_results([(1,), (2,)], [(1,)])
    assert not compare_results([(1,)], [(1,), (1,)])


def test_rows_must_align_not_just_columns():
    # 每一列单独看取值都相同，但行与行的搭配不同
    gold = [("a", 1), ("b", 2)]
    pred = [("a", 2), ("b", 1)]

    assert not compare_results(gold, pred)


def test_row_order_ignored_unless_ordered():
    gold = [("a", 1), ("b", 2)]
    pred = [("b", 2), ("a", 1)]

    assert compare_results(gold, pred)
    assert not compare_results(gold, pred, ordered=True)
    assert compare_results(gold, list(gold), ordered=True)


def test_ordered_comparison_allows_extra_and_reordered_columns():
    gold = [("a", 2), ("b", 1)]
    pred = [(2, "x", "a"), (1, "y", "b")]

    assert compare_results(gold, pred, ordered=True)


def test_duplicate_rows_are_counted():
    assert not compare_results([(1,), (1,), (2,)], [(1,), (2,), (2,)])
    assert compare_results([(1,), (1,), (2,)], [(2,), (1,), (1,)])


def test_null_is_not_zero():
    assert compare_results([(None,)], [(None,)])
    assert not compare_results([(None,)], [(0,)])


def test_empty_results():
    assert compare_results([], [])
    assert not compare_results([], [(1,)])


def test_dates_compare_by_value():
    assert compare_results([(date(2026, 9, 30),)], [("2026-09-30",)])


def test_each_gold_column_needs_its_own_prediction_column():
    assert not compare_results([(1, 1)], [(1, 2)])
    assert compare_results([(1, 1)], [(1, 1, 5)])


def test_boolean_is_not_a_number():
    assert normalize_cell(True) == "True"
    assert not compare_results([(True,)], [(1,)])
