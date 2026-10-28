"""
代码字段与名称字段
代码字段存的是代码（行业代码 C、机构号 1101），拿中文名称去比查不出数据：按名称过滤要用对应的名称字段，
或者按码值说明写代码。③ 证据组装给代码字段标上名称字段或码值表，⑥ 静态校验的 R9 用同一套配对判断
「代码字段和中文名称比较」。

名称字段只在同一张表里找：
- 中文名「××代码」「××编码」「××编号」「××号」「××ID」配「××名称」
- 英文名「××_cd」「××_code」「××_no」「××_id」配「××_nm」「××_name」
中文名配上的优先；同一种写法有几个候选时取表里靠前的。

码值表：只有一个粒度字段、并且这个粒度字段在本表就配得上名称字段的维表，例如行业代码表
（industry_cd → industry_nm）、客户维表（cust_no → cust_nm）。别的表里同名的代码字段，名称可以到码值表里取。
"""
import re
from collections.abc import Callable, Iterable, Sequence

from metadata.models import LAYERS, ColumnMeta, TableMeta

CN_CODE_SUFFIXES: tuple[str, ...] = ("代码", "编码", "编号", "号", "ID")
CN_NAME_SUFFIX = "名称"
EN_CODE_SUFFIXES: tuple[str, ...] = ("_cd", "_code", "_no", "_id")
EN_NAME_SUFFIXES: tuple[str, ...] = ("_nm", "_name")
DIM_TABLE_TYPE = "维表"

_ENUM_SEGMENT = re.compile(r"[;；,，、\n]+")
_ENUM_PAIR = re.compile(r"\s*([^=:：]+?)\s*[=:：]\s*(.+?)\s*")
_CHINESE_NAME = re.compile(r"[一-鿿]+")


def _stem(text: str, suffixes: Sequence[str]) -> str:
    """去掉第一个匹配上的后缀；都不匹配、或者去掉后什么都不剩时返回空串"""
    for suffix in suffixes:
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[:-len(suffix)]
    return ""


def is_code_column(column: ColumnMeta) -> bool:
    """中文名或英文名带代码类的后缀（代码、编码、编号、号、ID；_cd、_code、_no、_id）

    Args:
        column: 字段

    Returns:
        是不是代码字段
    """
    return bool(_stem(column.column_cn.strip(), CN_CODE_SUFFIXES)
                or _stem(column.column_name.lower(), EN_CODE_SUFFIXES))


def name_column(column: ColumnMeta, columns: Sequence[ColumnMeta]) -> str:
    """代码字段在同一张表里对应的名称字段

    Args:
        column: 代码字段
        columns: 这张表的全部字段，按表内顺序

    Returns:
        名称字段的英文名；不是代码字段、或者表里没有对应的名称字段时为空串
    """
    others = [item for item in columns if item.column_name.lower() != column.column_name.lower()]
    cn_stem = _stem(column.column_cn.strip(), CN_CODE_SUFFIXES)
    if cn_stem:
        wanted = cn_stem + CN_NAME_SUFFIX
        for item in others:
            if item.column_cn.strip() == wanted:
                return item.column_name
    en_stem = _stem(column.column_name.lower(), EN_CODE_SUFFIXES)
    if en_stem:
        wanted_names = {en_stem + suffix for suffix in EN_NAME_SUFFIXES}
        for item in others:
            if item.column_name.lower() in wanted_names:
                return item.column_name
    return ""


def code_tables(tables: Iterable[TableMeta], columns_of: Callable[[str], Sequence[ColumnMeta]]
                ) -> dict[str, list[tuple[str, str]]]:
    """码值表索引：只有一个粒度字段、粒度字段在本表配得上名称字段的维表

    Args:
        tables: 参与查找的表，按这个先后登记
        columns_of: 表标识 → 这张表的字段

    Returns:
        粒度字段名（小写）→ [(码值表, 名称字段)]，层级高的在前（专题层、标准层、贴源层），同层按传入的先后
    """
    index: dict[str, list[tuple[str, str]]] = {}
    rank = {layer: position for position, layer in enumerate(LAYERS)}
    for table in sorted(tables, key=lambda item: -rank.get(item.layer, -1)):
        if table.table_type != DIM_TABLE_TYPE or len(table.grain_columns) != 1:
            continue
        columns = columns_of(table.table_id)
        grain = table.grain_columns[0].lower()
        key = next((column for column in columns if column.column_name.lower() == grain), None)
        if key is None:
            continue
        name = name_column(key, columns)
        if name:
            index.setdefault(grain, []).append((table.table_id, name))
    return index


def parse_enum(text: str) -> dict[str, str]:
    """码值说明里「代码=名称」形式的部分：1=大型;2=中型 → {"1": "大型", "2": "中型"}

    段与段用分号、逗号、顿号或换行隔开，代码与名称用等号或冒号隔开；认不出的段跳过。

    Args:
        text: 码值说明

    Returns:
        代码 → 名称，按出现的先后
    """
    pairs: dict[str, str] = {}
    for segment in _ENUM_SEGMENT.split(text or ""):
        matched = _ENUM_PAIR.fullmatch(segment)
        if matched:
            pairs.setdefault(matched.group(1), matched.group(2))
    return pairs


def enum_segments(text: str) -> set[str]:
    """码值说明按分号、逗号、顿号、换行切开后的各段（去掉首尾空白）。只列取值、不写代码的码值说明，各段就是取值

    Args:
        text: 码值说明

    Returns:
        各段的文字
    """
    return {segment.strip() for segment in _ENUM_SEGMENT.split(text or "") if segment.strip()}


def is_chinese_name(text: str) -> bool:
    """是不是纯中文的名称（制造业、上海分行）。带数字、字母、标点的不算：合同编号这类代码里也可能有汉字

    Args:
        text: 字面量的值

    Returns:
        去掉首尾空白后全是汉字时为真
    """
    return bool(_CHINESE_NAME.fullmatch(text.strip()))
