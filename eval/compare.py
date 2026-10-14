"""
结果集比对
评估时用来判断生成的 SQL 和标准 SQL 在模拟库上查出来的结果是否一致

规则：
- 数值四舍五入到 2 位小数后比较
- 不看列名和列顺序，允许生成结果比标准结果多出列
- 标准结果的每一列都要能在生成结果里找到对应的列，并且按行对应
- 要求顺序时逐行按顺序比较，否则不计行顺序
"""
from collections import Counter
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Sequence

TWO_PLACES = Decimal("0.01")


def normalize_cell(value: object) -> object:
    """把一个单元格转成可以直接比较的规范形式

    数值统一成保留 2 位小数的 Decimal，日期统一成文本，其余转成文本，空值保持为 None。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)       # 布尔值不参与数值比较，避免 True 和 1 被当成相等
    if isinstance(value, (int, float, Decimal)):
        number = value if isinstance(value, Decimal) else Decimal(str(value))
        if not number.is_finite():
            return str(number)
        return number.quantize(TWO_PLACES, rounding=ROUND_HALF_UP) + 0     # 加 0 把 -0.00 规范成 0.00
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def compare_results(gold_rows: Sequence[Sequence[object]], pred_rows: Sequence[Sequence[object]],
                    ordered: bool = False) -> bool:
    """判断生成结果是否与标准结果一致

    Args:
        gold_rows: 标准 SQL 的结果行
        pred_rows: 生成 SQL 的结果行
        ordered: 是否要求行顺序一致

    Returns:
        一致返回 True
    """
    gold = [tuple(normalize_cell(cell) for cell in row) for row in gold_rows]
    pred = [tuple(normalize_cell(cell) for cell in row) for row in pred_rows]
    if len(gold) != len(pred):
        return False
    if not gold:
        return True
    gold_width, pred_width = len(gold[0]), len(pred[0])
    if pred_width < gold_width:
        return False

    def column_signature(rows: list[tuple], index: int) -> object:
        values = [row[index] for row in rows]
        return values if ordered else Counter(values)

    # 第一步：为标准结果的每一列找出取值完全相同的候选列
    pred_signatures = [column_signature(pred, index) for index in range(pred_width)]
    candidates: list[list[int]] = []
    for gold_index in range(gold_width):
        signature = column_signature(gold, gold_index)
        matches = [index for index in range(pred_width) if pred_signatures[index] == signature]
        if not matches:
            return False
        candidates.append(matches)

    # 第二步：在候选里找一种不重复的列对应方式，使整行也对得上
    target = gold if ordered else Counter(gold)

    def search(gold_index: int, chosen: list[int]) -> bool:
        if gold_index == gold_width:
            projected = [tuple(row[index] for index in chosen) for row in pred]
            return (projected if ordered else Counter(projected)) == target
        return any(search(gold_index + 1, chosen + [index])
                   for index in candidates[gold_index] if index not in chosen)

    return search(0, [])
