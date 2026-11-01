"""
按元数据造测试数据：每张元数据表一张 SQLite 表，用来演示查数和报表。里面全是生成的假数据，不是业务数据
用法: python -m metadata.testdata [资料目录] [--out 文件] [--seed 数字] [--today 日期]

默认写到 资料目录/build/test_data.db。表名写成「模式.表」，执行层把 SQL 里的模式.表改成它（见 nl2sql.dialect.to_sqlite）。

造数只看元数据，同样的元数据和种子造出同样的数据：
- 能关联上：关联关系里互相关联的字段、同名的键字段归成一个键域，共用一批键值。键域的大小取其中主键表的最小行数，
  外键抽到的值在每张主键表里都有；只有一个粒度字段的表，这个字段不重复。键域里有码值说明时用码值，类别字段只留几个值
- 字段按含义取值，先看码值说明，再看类型、中文名和英文名：年度、日期、金额、比例、数量、人名、部门、类别
- 代码字段配了名称字段（见 metadata.codes）时，名称由代码决定，同一行里对得上
- 加工脚本里恒为空的字段留空，非键字段有 3% 是空值，和真实数据一样要处理空值
"""
import argparse
import hashlib
import json
import random
import re
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from metadata.codes import enum_segments, name_column, parse_enum
from metadata.importer import BUILD_DIR_NAME, default_db_path
from metadata.models import ColumnMeta, TableMeta
from metadata.store import MetadataStore

FILE_NAME = "test_data.db"
INFO_TABLE = "_test_data_info"
NOTE = "按元数据生成的模拟数据，不是业务数据"
ROWS_BY_LAYER = {"专题层": 300, "汇总层": 300, "标准层": 100, "贴源层": 30}
DEFAULT_ROWS = 100
KEY_BASE = 100001                       # 键值从这里开始，整数字段存数字，文本字段存同样的数字串，SQLite 里能互相关联
DATE_SPAN_DAYS = 3 * 365                # 日期落在今天往前三年内
SNAPSHOT_MONTHS = 12                    # 快照表的数据日期取最近几个月末
NULL_SHARE = 0.03
SMALL_CATEGORIES = 5                    # 类型、状态这类字段的取值个数
NAMED_VALUES = 40                       # 名称、普通文本字段的取值个数
CODE_ENDING = r"(代码|编码|编号|号|ID|id)$"   # 代码字段中文名的结尾，去掉后是名称字段取值的前缀

KEY_SUFFIXES = ("_id", "_cd", "_code", "_no")
LABEL_WORDS = ("类型", "类别", "状态", "级别", "等级", "来源", "性质", "方式", "阶段", "分类")
CATEGORY_WORDS = LABEL_WORDS + ("部门", "单位", "机构")
DELETE_FLAGS = ("del_flag", "delete_flag", "is_delete", "is_deleted", "isdeleted", "deleted", "dr")
PEOPLE_NAMES = ("_by", "_user", "_person", "_staff", "_manager", "_leader", "creator", "updater", "modifier", "operator")
INTEGER_TYPES = ("int", "serial")
REAL_TYPES = ("numeric", "decimal", "double", "real", "float", "money")
DATE_TYPES = ("date",)
TIMESTAMP_TYPES = ("timestamp",)
FLAG_TYPES = ("bool", "bit")
BLANK_TYPES = ("json", "bytea")
SURNAMES = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨"
GIVEN_NAMES = ("一鸣", "子涵", "思远", "雨桐", "浩然", "晓彤", "嘉怡", "明轩", "若溪", "文博", "佳宁", "志强")
DEPARTMENTS = ("综合管理部", "技术管理部", "财务部", "采购部", "资产部", "人力资源部", "信息中心", "后勤保障部")
UNITS = ("甲测试单位", "乙测试单位", "丙测试单位", "丁测试单位", "戊测试单位", "己测试单位")

Value = str | int | float | None
Maker = Callable[[random.Random, int], Value]


def default_path(mart_dir: Path) -> Path:
    """一套资料的测试数据默认路径：资料目录/build/test_data.db"""
    return Path(mart_dir) / BUILD_DIR_NAME / FILE_NAME


def _stable_seed(seed: int, *parts: str) -> int:
    """按表名、字段名算种子：换 PYTHONHASHSEED、换表的先后都不影响每张表造出来的数据"""
    digest = hashlib.sha256("\0".join((str(seed), *parts)).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _is_type(column: ColumnMeta, names: Sequence[str]) -> bool:
    return any(name in column.data_type.lower() for name in names)


def _has_text(column: ColumnMeta, *words: str) -> bool:
    return any(word in column.column_cn for word in words)


def row_count(table: TableMeta) -> int:
    """一张表造几行：专题层、汇总层多一些，下面的层少一些，文件不至于太大"""
    return ROWS_BY_LAYER.get(table.layer, DEFAULT_ROWS)


def _sole_key(table: TableMeta, columns: Sequence[ColumnMeta]) -> str:
    """能单独确定一行的字段（小写），没有时为空串：唯一的粒度字段，否则唯一的主键字段"""
    if len(table.grain_columns) == 1:
        return table.grain_columns[0].lower()
    keys = [column.column_name.lower() for column in columns if column.is_pk]
    return keys[0] if len(keys) == 1 else ""


class _Domains:
    """键域：互相关联的字段归成一组，用并查集合并"""

    def __init__(self) -> None:
        self._parent: dict[tuple[str, str], tuple[str, str]] = {}

    def _find(self, item: tuple[str, str]) -> tuple[str, str]:
        self._parent.setdefault(item, item)
        while self._parent[item] != item:
            self._parent[item] = self._parent[self._parent[item]]
            item = self._parent[item]
        return item

    def join(self, left: tuple[str, str], right: tuple[str, str]) -> None:
        self._parent[self._find(left)] = self._find(right)

    def root(self, item: tuple[str, str]) -> tuple[str, str] | None:
        """item 所在的键域，不在任何键域里时为 None"""
        return self._find(item) if item in self._parent else None


def _is_year(column: ColumnMeta) -> bool:
    return _has_text(column, "年度", "年份") or column.column_name.lower().endswith("_year")


def _enum_codes(column: ColumnMeta) -> list[str]:
    """码值说明里的代码：「代码=名称」和只列取值的两种写法都认"""
    return list(parse_enum(column.enum_values)) or sorted(enum_segments(column.enum_values))


@dataclass
class KeyDomains:
    """键域的结果：字段属于哪个键域，每个键域有几个键值、有没有固定的码值"""

    domains: _Domains
    sizes: dict[tuple[str, str], int]
    codes: dict[tuple[str, str], list[str]]


def key_domains(store: MetadataStore) -> KeyDomains:
    """归出键域

    键值个数取键域里主键表的最小行数：外键抽到的值在每张主键表里都有；没有主键表时取最大的行数。
    键域里过半的字段是类别（类型、状态、部门这类）时只留几个键值，按它分组才看得出分布；
    有码值说明时用码值：查询按码值过滤才查得到。
    """
    domains = _Domains()
    for relation in store.relations():
        for left, right in zip(relation.left_columns, relation.right_columns):
            domains.join((relation.left_table, left.lower()), (relation.right_table, right.lower()))
    for table in store.tables():
        for column in store.columns(table.table_id):
            name = column.column_name.lower()
            if name.endswith(KEY_SUFFIXES):
                domains.join((table.table_id, name), ("*", name))
    holders: dict[tuple[str, str], list[int]] = defaultdict(list)
    members: dict[tuple[str, str], int] = defaultdict(int)
    categories: dict[tuple[str, str], int] = defaultdict(int)
    codes: dict[tuple[str, str], list[str]] = {}
    for table in store.tables():
        columns = store.columns(table.table_id)
        for column in columns:
            root = domains.root((table.table_id, column.column_name.lower()))
            if root is not None:
                members[root] += 1
                categories[root] += _has_text(column, *CATEGORY_WORDS)
                if _enum_codes(column):
                    codes.setdefault(root, _enum_codes(column))
        sole = _sole_key(table, columns)
        root = domains.root((table.table_id, sole)) if sole else None
        if root is not None:
            holders[root].append(row_count(table))
    largest = max(ROWS_BY_LAYER.values())
    sizes = {root: min(holders.get(root, [largest])) for root in members}
    for root, count in members.items():
        if categories[root] * 2 > count:
            sizes[root] = min(sizes[root], SMALL_CATEGORIES)
    return KeyDomains(domains, sizes, codes)


def _typed(column: ColumnMeta, value: int) -> Value:
    """数字字段存数字，文本字段存数字串"""
    return value if _is_type(column, INTEGER_TYPES + REAL_TYPES) else str(value)


def _date_text(rng: random.Random, today: date) -> str:
    return (today - timedelta(days=rng.randrange(DATE_SPAN_DAYS))).isoformat()


def _labels(column: ColumnMeta) -> list[str]:
    title = column.column_cn.strip() or column.column_name
    return [f"{title}{chr(ord('A') + index)}" for index in range(SMALL_CATEGORIES)]


def _person(rng: random.Random) -> str:
    return rng.choice(SURNAMES) + rng.choice(GIVEN_NAMES)


def _code_maker(column: ColumnMeta, codes: Sequence[str]) -> Maker:
    """只取这些代码里的值，整数字段存数字"""
    numeric = _is_type(column, INTEGER_TYPES) and all(code.isdigit() for code in codes)
    values: list[Value] = [int(code) for code in codes] if numeric else list(codes)
    return lambda rng, _: rng.choice(values)


def _year_maker(column: ColumnMeta, today: date) -> Maker:
    return lambda rng, _: _typed(column, today.year - rng.randrange(3))


def _month_end_maker(today: date) -> Maker:
    """快照表的数据日期：最近几个月末，「9 月末的余额」这类查询才查得到"""
    ends = [today.replace(day=1)]
    for _ in range(SNAPSHOT_MONTHS - 1):
        ends.append((ends[-1] - timedelta(days=1)).replace(day=1))
    days = [(start - timedelta(days=1)).isoformat() for start in ends]
    return lambda rng, _: rng.choice(days)


def _number_maker(column: ColumnMeta) -> Maker | None:
    """按中文名认金额、比例、数量、序号、年度；不是数字类的含义时返回 None"""
    whole = _is_type(column, INTEGER_TYPES)
    if _has_text(column, "金额", "经费", "预算", "发生额", "余额", "（元）", "(元)", "万元", "资金", "价"):
        def amount(rng: random.Random, _: int) -> Value:
            value = rng.lognormvariate(11.5, 1.2)
            return int(value) if whole else round(value, 2)
        return amount
    if _has_text(column, "比例", "占比", "百分比", "进度", "比率", "完成率", "使用率"):
        return lambda rng, _: rng.randint(0, 100) if whole else round(rng.uniform(0, 100), 2)
    if _has_text(column, "数量", "人数", "次数", "个数", "件数", "台数", "天数", "数目"):
        return lambda rng, _: rng.randint(1, 200)
    if _has_text(column, "序号", "排序", "顺序"):
        return lambda rng, row: row + 1
    if _has_text(column, "月份"):
        return lambda rng, _: _typed(column, rng.randint(1, 12))
    return None


def _flag_maker(column: ColumnMeta) -> Maker | None:
    """删除标记几乎都是未删除；是否、标识类字段和布尔类型一半一半"""
    name = column.column_name.lower()
    if name in DELETE_FLAGS:
        return lambda rng, _: _typed(column, 0)
    if (_is_type(column, FLAG_TYPES) or column.data_type.lower() in ("char(1)", "varchar(1)")
            or column.column_cn.strip().startswith("是否") or _has_text(column, "标识", "标志")):
        return lambda rng, _: _typed(column, rng.randint(0, 1))
    return None


def _time_maker(column: ColumnMeta, today: date) -> Maker | None:
    """日期存 YYYY-MM-DD，时间戳存 YYYY-MM-DD HH:MM:SS；文本字段按含义是日期时也一样"""
    name = column.column_name.lower()
    if _is_type(column, TIMESTAMP_TYPES) or (name.endswith(("_time", "_at")) and not _is_type(column, ("time",))):
        return lambda rng, _: f"{_date_text(rng, today)} {rng.randint(8, 19):02d}:{rng.randrange(60):02d}:{rng.randrange(60):02d}"
    if _is_type(column, ("time",)):
        return lambda rng, _: f"{rng.randint(8, 19):02d}:{rng.randrange(60):02d}:00"
    if _is_type(column, DATE_TYPES) or _has_text(column, "日期", "时间") or name.endswith("_date"):
        return lambda rng, _: _date_text(rng, today)
    return None


def _text_maker(column: ColumnMeta) -> Maker:
    """文本字段：人名、部门、单位、电话、地址、类别，其余是「字段名 + 序号」"""
    name = column.column_name.lower()
    title = column.column_cn.strip() or column.column_name
    if _has_text(column, "姓名", "负责人", "创建人", "更新人", "修改人", "经办人", "申请人", "联系人") or name.endswith(PEOPLE_NAMES):
        return lambda rng, _: _person(rng)
    if _has_text(column, "部门"):
        return lambda rng, _: rng.choice(DEPARTMENTS)
    if _has_text(column, "单位", "机构", "公司"):
        return lambda rng, _: rng.choice(UNITS)
    if _has_text(column, "电话", "手机", "号码"):
        return lambda rng, _: f"1380000{rng.randrange(10000):04d}"
    if _has_text(column, "地址"):
        return lambda rng, _: f"测试路 {rng.randint(1, 200)} 号"
    if _has_text(column, *LABEL_WORDS):
        labels = _labels(column)
        return lambda rng, _: rng.choice(labels)
    return lambda rng, _: f"{title}{rng.randint(1, NAMED_VALUES)}"


def _plain_maker(column: ColumnMeta, today: date) -> Maker:
    """不是键、也不是空字段时的取值：先看码值说明，再看含义，最后按类型"""
    codes = _enum_codes(column)
    if codes:
        return _code_maker(column, codes)
    made = _flag_maker(column) or _time_maker(column, today) or _number_maker(column)
    if made is not None:
        return made
    if _is_type(column, INTEGER_TYPES):
        return lambda rng, _: rng.randint(1, 1000)
    if _is_type(column, REAL_TYPES):
        return lambda rng, _: round(rng.uniform(1, 10000), 2)
    return _text_maker(column)


def _key_maker(column: ColumnMeta, size: int, unique: bool) -> Maker:
    if unique:
        return lambda rng, row: _typed(column, KEY_BASE + row)
    return lambda rng, _: _typed(column, KEY_BASE + rng.randrange(size))


def _named_value(code: Value, labels: dict[str, str], stem: str) -> str:
    """名称字段的取值：由代码决定，同一个代码在哪张表里都得到同一个名称；小键域写成「项目类型A」，图上好认"""
    if str(code) in labels:
        return labels[str(code)]
    number = int(code) - KEY_BASE if str(code).isdigit() else -1
    return f"{stem}{chr(ord('A') + number)}" if 0 <= number < SMALL_CATEGORIES else f"{stem}{code}"


def _empty_columns(store: MetadataStore, table_id: str) -> set[str]:
    """加工脚本里每条语句都恒为空的字段"""
    kinds: dict[str, set[str]] = defaultdict(set)
    for item in store.script_columns(table_id):
        kinds[item.target_column.lower()].add(item.kind)
    return {name for name, values in kinds.items() if values == {"null"}}


def table_rows(store: MetadataStore, table: TableMeta, keys: KeyDomains, seed: int, today: date) -> list[list[Value]]:
    """造一张表的数据，每行的字段顺序和 store.columns 一样"""
    columns = store.columns(table.table_id)
    sole = _sole_key(table, columns)
    empty = _empty_columns(store, table.table_id)
    roots = [keys.domains.root((table.table_id, column.column_name.lower())) for column in columns]
    makers: list[Maker | None] = []
    for column, root in zip(columns, roots):
        name = column.column_name.lower()
        if name in empty or _is_type(column, BLANK_TYPES):
            makers.append(None)
        elif table.date_column and name == table.date_column.lower():
            makers.append(_month_end_maker(today))
        elif _is_year(column):
            makers.append(_year_maker(column, today))
        elif root in keys.codes:
            makers.append(_code_maker(column, keys.codes[root]))
        elif root is not None:
            makers.append(_key_maker(column, keys.sizes[root], name == sole))
        else:
            makers.append(_plain_maker(column, today))
    named: dict[int, tuple[int, dict[str, str], str]] = {}     # 名称字段的序号 → (代码字段的序号, 码值, 名称前缀)
    positions = {column.column_name: index for index, column in enumerate(columns)}
    for index, column in enumerate(columns):
        partner = name_column(column, columns)
        if partner and (roots[index] is not None or column.enum_values.strip()):
            title = column.column_cn.strip()
            named[positions[partner]] = (index, parse_enum(column.enum_values), re.sub(CODE_ENDING, "", title) or title)
            makers[positions[partner]] = None
    rng = random.Random(_stable_seed(seed, table.table_id))
    rows = []
    for row_number in range(row_count(table)):
        row: list[Value] = [None if make is None else make(rng, row_number) for make in makers]
        for index, value in enumerate(row):
            if value is not None and roots[index] is None and rng.random() < NULL_SHARE:
                row[index] = None
        for index, (code_index, labels, stem) in named.items():
            code = row[code_index]
            row[index] = None if code is None else _named_value(code, labels, stem)
        rows.append(row)
    return rows


def _sqlite_type(column: ColumnMeta) -> str:
    if _is_type(column, INTEGER_TYPES):
        return "INTEGER"
    return "REAL" if _is_type(column, REAL_TYPES) else "TEXT"


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def build(store: MetadataStore, path: Path, *, seed: int = 1, today: date | None = None) -> dict[str, int]:
    """造全部表的数据写进 SQLite，已有的文件先删掉再写

    Args:
        store: 元数据
        path: 目标文件
        seed: 随机种子
        today: 日期落在它往前三年内，默认今天

    Returns:
        {"tables": 表数, "rows": 总行数}
    """
    today = today or date.today()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    keys = key_domains(store)
    total = 0
    tables = store.tables()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        for table in tables:
            columns = store.columns(table.table_id)
            if not columns:
                continue
            definition = ", ".join(f"{_quoted(column.column_name)} {_sqlite_type(column)}" for column in columns)
            connection.execute(f"CREATE TABLE {_quoted(table.table_id)} ({definition})")
            rows = table_rows(store, table, keys, seed, today)
            marks = ", ".join("?" * len(columns))
            connection.executemany(f"INSERT INTO {_quoted(table.table_id)} VALUES ({marks})", rows)
            total += len(rows)
        info = {"note": NOTE, "seed": seed, "today": today.isoformat(), "metadata_version": store.version_id,
                "tables": len(tables), "rows": total}
        connection.execute(f"CREATE TABLE {INFO_TABLE} (key TEXT PRIMARY KEY, value TEXT)")
        connection.executemany(f"INSERT INTO {INFO_TABLE} VALUES (?, ?)", [(key, json.dumps(value, ensure_ascii=False))
                                                                           for key, value in info.items()])
    return {"tables": len(tables), "rows": total}


def main(argv: list[str] | None = None) -> int:
    """命令行入口，返回退出码：0 成功，1 运行失败，2 参数不对"""
    parser = argparse.ArgumentParser(description="按元数据造测试数据，写进 SQLite")
    parser.add_argument("mart", nargs="?", default="data/mock_mart", help="资料目录")
    parser.add_argument("--out", default="", help=f"目标文件，默认 资料目录/{BUILD_DIR_NAME}/{FILE_NAME}")
    parser.add_argument("--seed", type=int, default=1, help="随机种子")
    parser.add_argument("--today", default="", help="日期落在它往前三年内，默认今天，YYYY-MM-DD")
    try:
        args = parser.parse_args(argv)
        today = date.fromisoformat(args.today) if args.today else None
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else 0
    except ValueError:
        print(f"错误：--today 不是 YYYY-MM-DD：{args.today}", file=sys.stderr)
        return 2
    mart_dir = Path(args.mart)
    try:
        store = MetadataStore(default_db_path(mart_dir))
    except (FileNotFoundError, RuntimeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    target = Path(args.out) if args.out else default_path(mart_dir)
    summary = build(store, target, seed=args.seed, today=today)
    print(f"{summary['tables']} 张表、{summary['rows']} 行，写到 {target}（{target.stat().st_size / 1024 / 1024:.1f} MB）")
    print(NOTE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
