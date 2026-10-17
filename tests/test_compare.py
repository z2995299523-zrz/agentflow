"""结果集比对规则的测试"""
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

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


# ── 大数 ──────────────────────────────────────────────────

def test_numbers_beyond_the_default_decimal_precision_do_not_raise():
    # 默认精度 28 位，取整到两位小数时绝对值 ≥ 1e26 的数以前会抛 InvalidOperation
    for value in (10 ** 26, 10 ** 30, -(10 ** 30), 1e26, 1e40, Decimal("1E+30"), Decimal("9" * 40 + ".995")):
        assert isinstance(normalize_cell(value), Decimal), value
        assert compare_results([(value,)], [(value,)])


def test_big_numbers_are_still_rounded_and_told_apart():
    assert normalize_cell(Decimal("123456789012345678901234567890.125")) == Decimal(
        "123456789012345678901234567890.13")
    assert compare_results([(Decimal("123456789012345678901234567890.004"),)],
                           [(123456789012345678901234567890,)])
    assert not compare_results([(10 ** 30,)], [(10 ** 30 + 1,)])
    assert not compare_results([(1e40,)], [(1e41,)])


def test_absurdly_large_numbers_are_compared_as_they_are():
    huge = Decimal("1E+5000")

    assert compare_results([(huge,)], [(Decimal("1E+5000"),)])
    assert not compare_results([(huge,)], [(Decimal("2E+5000"),)])


# ── 日期时间 ──────────────────────────────────────────────

def test_midnight_datetime_equals_the_date_of_that_day():
    assert normalize_cell(datetime(2026, 9, 30)) == "2026-09-30"
    assert compare_results([(date(2026, 9, 30),)], [(datetime(2026, 9, 30, 0, 0, 0),)])
    assert compare_results([(datetime(2026, 9, 30),)], [("2026-09-30",)])


def test_datetime_with_a_time_is_written_with_a_space_and_optional_fraction():
    assert normalize_cell(datetime(2026, 9, 30, 13, 45, 10)) == "2026-09-30 13:45:10"
    assert normalize_cell(datetime(2026, 9, 30, 13, 45, 10, 250000)) == "2026-09-30 13:45:10.250000"
    assert normalize_cell(datetime(2026, 9, 30, 0, 0, 0, 1)) == "2026-09-30 00:00:00.000001"
    assert compare_results([(datetime(2026, 9, 30, 13, 45),)], [("2026-09-30 13:45:00",)])


def test_datetime_with_a_time_is_not_the_date_of_that_day():
    assert not compare_results([(date(2026, 9, 30),)], [(datetime(2026, 9, 30, 8, 0),)])
    assert not compare_results([(datetime(2026, 9, 30, 8, 0),)], [(datetime(2026, 9, 30, 8, 1),)])


def test_timezone_aware_datetimes_compare_by_the_instant():
    east8 = timezone(timedelta(hours=8))

    assert compare_results([(datetime(2026, 9, 30, 8, 0, tzinfo=east8),)],
                           [(datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc),)])
    assert normalize_cell(datetime(2026, 9, 30, 9, 30, tzinfo=east8)) == "2026-09-30 01:30:00"


# ── 特殊数值 ──────────────────────────────────────────────

def test_nan_and_infinity_compare_as_themselves():
    assert normalize_cell(float("nan")) == "NaN"
    assert compare_results([(float("nan"),)], [(Decimal("NaN"),)])
    assert compare_results([(float("inf"),)], [(float("inf"),)])
    assert not compare_results([(float("inf"),)], [(float("-inf"),)])
    assert not compare_results([(float("nan"),)], [(0,)])
    assert not compare_results([(float("inf"),)], [(10 ** 30,)])


def test_negative_zero_in_every_spelling_equals_zero():
    for value in (-0.0, Decimal("-0.00"), -0.004, Decimal("-0.001")):
        assert normalize_cell(value) == Decimal("0.00"), value
        assert str(normalize_cell(value)) == "0.00", value
        assert compare_results([(value,)], [(0,)])


# ── 行数与列对应 ──────────────────────────────────────────

class Unprintable:
    """转成文本时抛错的对象：单元格一旦被规范化就会暴露出来"""

    def __str__(self) -> str:
        raise RuntimeError("单元格被规范化了")


def test_row_count_is_checked_before_any_cell_is_normalized():
    assert not compare_results([(1,)], [(Unprintable(),), (Unprintable(),)])
    assert not compare_results([(Unprintable(),), (Unprintable(),)], [(1,)])


def test_twelve_columns_with_the_same_values_are_matched_quickly():
    amounts = [10, 20, 30, 40, 50, 60]
    gold = [tuple([amount] * 12) for amount in amounts]
    pred = [tuple([amount] * 12) + (amount, "x") for amount in amounts]

    started = time.perf_counter()
    assert compare_results(gold, pred)
    assert compare_results(gold, pred[::-1])
    assert compare_results(gold, pred, ordered=True)
    assert time.perf_counter() - started < 1


def test_twelve_same_value_columns_with_misaligned_rows_fail_quickly():
    # 标准结果：12 个相同的列加一列金额。生成结果的金额列取值相同、与前 12 列的行对应不上。
    # 逐个排列去试要 12! 种，合并成组再对应就是一步的事
    keys = [1, 2, 3, 4, 5, 6]
    gold = [tuple([key] * 12) + (key * 10,) for key in keys]
    swapped = [20, 10, 30, 40, 50, 60]
    pred = [tuple([key] * 12) + (amount,) for key, amount in zip(keys, swapped)]

    started = time.perf_counter()
    assert not compare_results(gold, pred)
    assert not compare_results(gold, pred, ordered=True)
    assert time.perf_counter() - started < 1


def test_identical_prediction_columns_are_interchangeable():
    gold = [("a", "a", 1), ("b", "b", 2)]
    pred = [(1, "a", "a"), (2, "b", "b")]

    assert compare_results(gold, pred)
    assert compare_results(gold, pred, ordered=True)


def test_each_gold_column_needs_its_own_prediction_column_even_if_values_repeat():
    gold = [("a", "a"), ("b", "b")]

    assert not compare_results(gold, [("a",), ("b",)])                       # 列数不够
    assert not compare_results(gold, [("a", "x"), ("b", "y")])               # 第二个 a 列没有对应
    assert not compare_results(gold, [("a", "x"), ("b", "y")], ordered=True)
    assert compare_results(gold, [("a", "a"), ("b", "b")])


def test_columns_with_the_same_multiset_are_told_apart_by_row_alignment():
    # 两列单独看取值一样（都是 1、2、3），但行与行的搭配不同；其中一种对应方式才能对上整行
    gold = [(1, 3), (2, 1), (3, 2)]
    aligned = [(3, 1), (1, 2), (2, 3)]            # 第 2 列对应标准结果的第 1 列
    misaligned = [(1, 2), (2, 1), (3, 3)]

    assert compare_results(gold, aligned)
    assert compare_results(gold, aligned, ordered=True)       # 要求行序，但列序不要求：换列后行序一致
    assert not compare_results(gold, misaligned)
    assert not compare_results(gold, misaligned, ordered=True)


def test_ragged_rows_are_not_equal_and_do_not_raise():
    assert not compare_results([(1, 2), (3,)], [(1, 2), (3, 4)])
    assert not compare_results([(1, 2), (3, 4)], [(1, 2), (3,)])


def test_search_limit_raises_instead_of_hanging(monkeypatch):
    from eval import compare

    monkeypatch.setattr(compare, "MAX_SEARCH_WORK", 5)
    gold = [(1, 3), (2, 1), (3, 2)]
    aligned = [(3, 1), (1, 2), (2, 3)]

    with pytest.raises(ValueError, match="搜索量"):
        compare.compare_results(gold, aligned)
