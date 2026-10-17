"""
结果集比对
评估时用来判断生成的 SQL 和标准 SQL 在模拟库上查出来的结果是否一致

规则：
- 数值四舍五入到 2 位小数后比较，位数再大也不报错
- 时间是零点的 datetime 当作同一天的日期，其余的 datetime 写成「YYYY-MM-DD HH:MM:SS[.ffffff]」
- 不看列名和列顺序，允许生成结果比标准结果多出列
- 标准结果的每一列都要能在生成结果里找到对应的列，并且按行对应
- 要求顺序时逐行按顺序比较，否则不计行顺序
- 先比行数，行数不同就不再往下看
"""
from collections import Counter
from datetime import date, datetime, time, timezone
from decimal import ROUND_HALF_UP, Context, Decimal
from typing import Sequence

TWO_PLACES = Decimal("0.01")
ROUNDING_CONTEXT = Context(prec=60, rounding=ROUND_HALF_UP)     # 不动全局的 decimal 设置，多线程评估也安全
MAX_EXACT_DIGITS = 400          # 整数部分超过这么多位的数不再取整，原样比较（DOUBLE 最大约 1.8e308）
MAX_SEARCH_WORK = 20_000_000    # 列对应的搜索量上限：每试一种列按行数计，防止极端结果集拖死评估


def normalize_cell(value: object) -> object:
    """把一个单元格转成可以直接比较的规范形式

    数值统一成保留 2 位小数的 Decimal，日期和时间统一成文本，其余转成文本，空值保持为 None。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)       # 布尔值不参与数值比较，避免 True 和 1 被当成相等
    if isinstance(value, (int, float, Decimal)):
        number = value if isinstance(value, Decimal) else Decimal(str(value))
        if not number.is_finite():
            return str(number)
        return _round_to_two_places(number)
    if isinstance(value, datetime):
        if value.tzinfo is not None:                    # 带时区的按同一时刻比较
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        if value.time() == time(0, 0):                  # 零点：与同一天的 DATE 是同一个值
            return value.date().isoformat()
        return value.isoformat(sep=" ")                 # 微秒为 0 时 isoformat 不写小数部分
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _round_to_two_places(number: Decimal) -> Decimal:
    """四舍五入到 2 位小数

    精度按这个数的位数现算：Decimal 默认只有 28 位，位数不够时 quantize 会抛 InvalidOperation，
    绝对值 ≥ 1e26 的数就是这样。位数大到不可能出现在查询结果里时原样返回。
    """
    if number.adjusted() > MAX_EXACT_DIGITS:
        return number
    context = ROUNDING_CONTEXT
    digits = max(number.adjusted(), 0) + 1 + 2          # 整数位加两位小数
    if digits + 2 > context.prec:
        context = Context(prec=digits + 2, rounding=ROUND_HALF_UP)
    return context.plus(number.quantize(TWO_PLACES, context=context))      # plus 把 -0.00 规范成 0.00


def compare_results(gold_rows: Sequence[Sequence[object]], pred_rows: Sequence[Sequence[object]],
                    ordered: bool = False) -> bool:
    """判断生成结果是否与标准结果一致

    生成结果里取值完全相同的列合并成一组，组内的列可以互换，列对应只在不同的组之间做：
    12 个取值相同的列是一步，而不是 12 的阶乘种排列。每选定一列，就用已选的列拼出行，
    核对这些行是不是标准结果对应几列的行（行的多重集，要求顺序时是行的序列），对不上的分支立刻放弃。

    Args:
        gold_rows: 标准 SQL 的结果行
        pred_rows: 生成 SQL 的结果行
        ordered: 是否要求行顺序一致

    Returns:
        一致返回 True

    Raises:
        ValueError: 列对应的搜索量超过 MAX_SEARCH_WORK（只有大量取值相同、行对应又各不相同的列才会出现）
    """
    if len(gold_rows) != len(pred_rows):        # 先比行数：不同就不必规范化单元格
        return False
    gold = [tuple(normalize_cell(cell) for cell in row) for row in gold_rows]
    pred = [tuple(normalize_cell(cell) for cell in row) for row in pred_rows]
    if not gold:
        return True
    gold_width, pred_width = len(gold[0]), len(pred[0])
    if pred_width < gold_width:
        return False
    if any(len(row) != gold_width for row in gold) or any(len(row) != pred_width for row in pred):
        return False                            # 各行宽度不一，不是一个结果集

    def comparable(rows: list[tuple]) -> object:
        return rows if ordered else Counter(rows)

    gold_columns = list(zip(*gold))
    pred_columns = list(zip(*pred))
    groups: dict[tuple, int] = {}               # 取值完全相同的生成列：取值 → 组号
    group_values: list[tuple] = []
    capacity: list[int] = []                    # 每组有几列，一列只能对应标准结果的一列
    for values in pred_columns:
        if values not in groups:
            groups[values] = len(group_values)
            group_values.append(values)
            capacity.append(0)
        capacity[groups[values]] += 1

    # 标准结果的每一列，能对应哪些组：要求顺序时取值必须完全一样，否则取值的多重集一样即可
    if ordered:
        candidates = [[groups[column]] if column in groups else [] for column in gold_columns]
    else:
        by_multiset: dict[frozenset, list[int]] = {}
        for number, values in enumerate(group_values):
            by_multiset.setdefault(frozenset(Counter(values).items()), []).append(number)
        candidates = [by_multiset.get(frozenset(Counter(column).items()), []) for column in gold_columns]
    if any(not options for options in candidates):
        return False

    # 标准结果里取值完全相同的两列互换对应不影响结果，规定后一列选的组号不小于前一列，免去重复的排列
    previous_twin: list[int | None] = []
    last_seen: dict[tuple, int] = {}
    for index, column in enumerate(gold_columns):
        previous_twin.append(last_seen.get(column))
        last_seen[column] = index

    row_count = len(gold)
    targets: dict[int, object] = {}             # 标准结果前 k 列拼出的行，按需算好缓存
    used = [0] * len(group_values)
    chosen: list[int] = []
    work = 0

    def target(prefix: int) -> object:
        if prefix not in targets:
            targets[prefix] = comparable([row[:prefix] for row in gold])
        return targets[prefix]

    def search(depth: int, partial: list[tuple]) -> bool:
        nonlocal work
        if depth == gold_width:
            return True
        twin = previous_twin[depth]
        floor = chosen[twin] if twin is not None else 0
        for group in candidates[depth]:
            if group < floor or used[group] >= capacity[group]:
                continue
            work += row_count
            if work > MAX_SEARCH_WORK:
                raise ValueError(f"列对应的搜索量超过上限 {MAX_SEARCH_WORK}，无法判断两个结果是否一致")
            column = group_values[group]
            extended = [row + (column[number],) for number, row in enumerate(partial)]
            if comparable(extended) != target(depth + 1):
                continue
            used[group] += 1
            chosen.append(group)
            if search(depth + 1, extended):
                return True
            chosen.pop()
            used[group] -= 1
        return False

    return search(0, [()] * row_count)
