"""
导入校验
对读到的原始资料做数据质量检核。错误会中止导入，警告只写进导入报告

规则编号：
    M01 清单或必填列缺失，或表清单为空  M02 必填单元格为空
    M03 表重复                    M04 同一张表内字段重复
    M05 层级、表类型或标准表取值非法  M06 表在字段清单里没有字段
    M07 粒度字段或数据日期字段不在字段清单里
    M08 快照表没有数据日期字段    M09 血缘的目标不在字段清单里
    M10 血缘的源字段不在字段清单里
    M11 关联关系引用了不存在的表或字段
    M12 关联关系的对应关系非法，或左右字段个数不一致
    M13 字段所属的表不在表清单里
    M14 表名没有带模式名（给出正确写法；这类写法不会被当成外部源放过）
    W01 血缘的源表不在表清单里（外部源）
    W02 血缘存在环                W03 标准层、汇总层或专题层的表没有血缘
    W04 表没有填粒度字段（按层级汇总）
    W05 表里有字段没有填数据类型
    W06 加工脚本里的语句无法结构化解析（只保存原文），或者 INSERT 的列数对不上
    W07 加工脚本的目标表不在表清单里
    W08 样例被拒收（没有通过静态校验，或缺需求、缺 SQL），或样例文件本身有问题（编码无法识别、第一条需求之前有 SQL）

W06 到 W08 由脚本解析和样例解析在导入的补全阶段报出，规则写在各自的模块里。
"""
from collections import Counter
from dataclasses import dataclass

import networkx as nx

from metadata.loader import RawMart, RawRow
from metadata.models import CARDINALITIES, LAYERS, STANDARD_MARKS, TABLE_TYPES, normalize_layer
from metadata.template import COLUMN_SHEET, LINEAGE_SHEET, RELATION_SHEET, SHEETS, TABLE_SHEET

ERROR = "错误"
WARNING = "警告"
SNAPSHOT_TYPE = "快照表"
DOWNSTREAM_LAYERS = ("标准层", "汇总层", "专题层")   # 这几层的表应当有血缘
SUMMARY_EXAMPLES = 3                       # 汇总类警告里举几个例子


@dataclass(frozen=True)
class Issue:
    """一条校验问题"""

    level: str             # 错误 / 警告
    rule: str              # 规则编号
    sheet: str             # 清单名
    row_no: int | None     # 行号，不针对具体行时为 None
    message: str


@dataclass(frozen=True)
class ReportSection:
    """导入报告里的一节统计，由各个补全步骤给出，渲染在问题清单之前"""

    title: str                  # 小节标题
    lines: tuple[str, ...]      # 小节内容，每个元素是一行 Markdown；为空时不渲染这一节


def split_list(text: str) -> list[str]:
    """把逗号分隔的文本拆成列表，兼容中文逗号"""
    return [part.strip() for part in text.replace("，", ",").split(",") if part.strip()]


def table_id_of(row: RawRow) -> str:
    """由表清单的一行得到表的唯一标识"""
    return f"{row.values['schema_name']}.{row.values['table_name']}"


def match_bare_name(name: str, tables: dict[str, RawRow]) -> list[str]:
    """表名没带模式名时，返回表清单里英文名相同的表，可能不止一张；带了模式名则返回空列表"""
    if "." in name:
        return []
    return [table_id for table_id in tables if table_id.split(".", 1)[1] == name]


class _BareNames:
    """收集没带模式名的表名，同一个表名在同一张清单里只报一次"""

    def __init__(self, sheet: str) -> None:
        self.sheet = sheet
        self.first_row: dict[str, int] = {}
        self.counts: Counter[str] = Counter()
        self.matches: dict[str, list[str]] = {}

    def add(self, name: str, matches: list[str], row_no: int) -> None:
        """登记一次出现"""
        self.first_row.setdefault(name, row_no)
        self.counts[name] += 1
        self.matches[name] = matches

    def ambiguous_tables(self) -> set[str]:
        """没带模式名的写法同时对上了多张表时，涉及到的那些表"""
        return {table_id for matches in self.matches.values() if len(matches) > 1
                for table_id in matches}

    def report(self, issues: list[Issue]) -> None:
        """把收集到的问题写进问题清单，行号取首次出现的那一行"""
        for name, matches in self.matches.items():
            if len(matches) == 1:
                advice = f"应写成 {matches[0]}"
            else:
                advice = f"而且在多个模式下都有这张表（{'、'.join(matches)}），请写带模式名的全名"
            issues.append(Issue(
                ERROR, "M14", self.sheet, self.first_row[name],
                f"表名 {name} 没有带模式名，{advice}（涉及 {self.counts[name]} 行）"))


def _check_structure(raw: RawMart, issues: list[Issue]) -> bool:
    """M01：清单和必填列是否齐全，表清单里是否至少有一张表。不齐全时后续规则无法进行"""
    for name in raw.missing_sheets:
        issues.append(Issue(ERROR, "M01", name, None, f"缺少清单「{name}」"))
    for name, headers in raw.missing_columns.items():
        issues.append(Issue(ERROR, "M01", name, None,
                            f"清单「{name}」缺少必填列：{'、'.join(headers)}"))
    if TABLE_SHEET.name not in raw.missing_sheets and not raw.rows.get("tables"):
        # 只有表头的空清单（例如刚生成的空白模板）照常导入的话，会把原有的元数据库清空
        issues.append(Issue(ERROR, "M01", TABLE_SHEET.name, None, "表清单里没有任何一张表"))
    return not issues


def _check_required_cells(raw: RawMart, issues: list[Issue]) -> None:
    """M02：必填单元格不能为空"""
    for spec in SHEETS:
        for row in raw.rows.get(spec.key, []):
            for column in spec.columns:
                if column.required and not row.values[column.field]:
                    issues.append(Issue(ERROR, "M02", spec.name, row.row_no,
                                        f"必填列「{column.header}」为空"))


def _check_tables(raw: RawMart, issues: list[Issue]) -> dict[str, RawRow]:
    """M03 M05 M08：表清单自身的检查。返回 表标识 → 表清单行"""
    sheet = TABLE_SHEET.name
    tables: dict[str, RawRow] = {}
    for row in raw.rows["tables"]:
        values = row.values
        if not values["schema_name"] or not values["table_name"]:
            continue  # 已由 M02 报告
        table_id = table_id_of(row)
        if table_id in tables:
            issues.append(Issue(ERROR, "M03", sheet, row.row_no,
                                f"表 {table_id} 重复，首次出现在第 {tables[table_id].row_no} 行"))
            continue
        tables[table_id] = row
        if values["layer"] and normalize_layer(values["layer"]) not in LAYERS:
            issues.append(Issue(ERROR, "M05", sheet, row.row_no,
                                f"层级「{values['layer']}」不在允许范围内（{'、'.join(LAYERS)}）"))
        if values["table_type"] and values["table_type"] not in TABLE_TYPES:
            issues.append(Issue(ERROR, "M05", sheet, row.row_no,
                                f"表类型「{values['table_type']}」不在允许范围内（{'、'.join(TABLE_TYPES)}）"))
        if values["standard"] and values["standard"] not in STANDARD_MARKS:
            issues.append(Issue(ERROR, "M05", sheet, row.row_no,
                                f"标准表「{values['standard']}」不在允许范围内（{'、'.join(STANDARD_MARKS)}，或留空）"))
        if values["table_type"] == SNAPSHOT_TYPE and not values["date_column"]:
            issues.append(Issue(ERROR, "M08", sheet, row.row_no,
                                f"快照表 {table_id} 没有填数据日期字段"))
    return tables


def _check_columns(raw: RawMart, tables: dict[str, RawRow],
                   issues: list[Issue]) -> dict[str, set[str]]:
    """M04 M06 M07 M13 M14 W04 W05：字段清单与表清单的对应。返回 表标识 → 字段名集合"""
    columns: dict[str, set[str]] = {table_id: set() for table_id in tables}
    untyped: Counter[str] = Counter()            # 表标识 → 没有数据类型的字段个数
    bare_names = _BareNames(COLUMN_SHEET.name)
    for row in raw.rows["columns"]:
        table_id, name = row.values["table_id"], row.values["column_name"]
        if not table_id or not name:
            continue
        if table_id not in tables:
            matches = match_bare_name(table_id, tables)
            if not matches:
                issues.append(Issue(ERROR, "M13", COLUMN_SHEET.name, row.row_no,
                                    f"字段 {table_id}.{name} 所属的表不在表清单里"))
                continue
            bare_names.add(table_id, matches, row.row_no)
            if len(matches) > 1:
                continue
            table_id = matches[0]       # 能唯一对上：报一次错，其余校验按对上的表继续
        if name in columns[table_id]:
            issues.append(Issue(ERROR, "M04", COLUMN_SHEET.name, row.row_no,
                                f"字段 {table_id}.{name} 重复"))
            continue
        columns[table_id].add(name)
        if not row.values["data_type"]:
            untyped[table_id] += 1
    bare_names.report(issues)
    undecided = bare_names.ambiguous_tables()   # 分不清字段属于哪张表时，不再追究这些表缺字段
    without_grain: dict[str, list[str]] = {}    # 层级 → 没有粒度字段的表

    for table_id, row in tables.items():
        if not columns[table_id]:
            if table_id not in undecided:
                issues.append(Issue(ERROR, "M06", TABLE_SHEET.name, row.row_no,
                                    f"表 {table_id} 在字段清单里没有任何字段"))
            continue
        grains = split_list(row.values["grain_columns"])
        if not grains:
            without_grain.setdefault(normalize_layer(row.values["layer"]), []).append(table_id)
        if untyped[table_id]:
            issues.append(Issue(WARNING, "W05", COLUMN_SHEET.name, None,
                                f"表 {table_id} 有 {untyped[table_id]} 个字段没有填数据类型"))
        for grain in grains:
            if grain not in columns[table_id]:
                issues.append(Issue(ERROR, "M07", TABLE_SHEET.name, row.row_no,
                                    f"表 {table_id} 的粒度字段 {grain} 不在字段清单里"))
        date_column = row.values["date_column"]
        if date_column and date_column not in columns[table_id]:
            issues.append(Issue(ERROR, "M07", TABLE_SHEET.name, row.row_no,
                                f"表 {table_id} 的数据日期字段 {date_column} 不在字段清单里"))
    for layer, names in without_grain.items():
        examples = "、".join(names[:SUMMARY_EXAMPLES])
        issues.append(Issue(WARNING, "W04", TABLE_SHEET.name, None,
                            f"{layer}有 {len(names)} 张表没有填粒度字段，"
                            f"无法判断关联它们时是否会发散，例如 {examples}"))
    return columns


def _check_lineage(raw: RawMart, tables: dict[str, RawRow],
                   columns: dict[str, set[str]], issues: list[Issue]) -> None:
    """M09 M10 M14 W01 W02 W03：血缘清单的引用完整性、环、缺失"""
    sheet = LINEAGE_SHEET.name
    bare_names = _BareNames(sheet)
    graph = nx.DiGraph()
    tables_with_lineage: set[str] = set()
    external_sources: Counter[str] = Counter()

    for row in raw.rows["lineage"]:
        values = row.values
        target_table, target_column = values["target_table"], values["target_column"]
        source_table, source_column = values["source_table"], values["source_column"]
        if not (target_table and target_column and source_table and source_column):
            continue

        resolved: list[str] = []
        undecided = False
        for name in (target_table, source_table):
            matches = [] if name in tables else match_bare_name(name, tables)
            if matches:
                bare_names.add(name, matches, row.row_no)
                if len(matches) == 1:
                    name = matches[0]   # 能唯一对上：报一次错，其余校验按对上的表继续
                else:
                    undecided = True
            resolved.append(name)
        if undecided:
            # 分不清指向哪张表，这条血缘不再往下查，但算作目标表有血缘
            tables_with_lineage.update(match_bare_name(target_table, tables) or [target_table])
            continue
        target_table, source_table = resolved

        if target_table not in tables or target_column not in columns[target_table]:
            issues.append(Issue(ERROR, "M09", sheet, row.row_no,
                                f"血缘的目标 {target_table}.{target_column} 不在字段清单里"))
            continue
        tables_with_lineage.add(target_table)
        if source_table not in tables:
            external_sources[source_table] += 1
        elif source_column not in columns[source_table]:
            issues.append(Issue(ERROR, "M10", sheet, row.row_no,
                                f"血缘的源字段 {source_table}.{source_column} 不在字段清单里"))
            continue
        graph.add_edge(f"{source_table}.{source_column}", f"{target_table}.{target_column}")

    bare_names.report(issues)
    for source_table, count in sorted(external_sources.items()):
        issues.append(Issue(WARNING, "W01", sheet, None,
                            f"源表 {source_table} 不在表清单里，按外部源处理（涉及 {count} 条血缘）"))

    try:
        cycle = nx.find_cycle(graph)
    except nx.NetworkXNoCycle:
        cycle = []
    if cycle:
        path = " → ".join([cycle[0][0]] + [edge[1] for edge in cycle])
        issues.append(Issue(WARNING, "W02", sheet, None, f"血缘存在环：{path}"))

    for table_id, row in tables.items():
        if normalize_layer(row.values["layer"]) in DOWNSTREAM_LAYERS and table_id not in tables_with_lineage:
            issues.append(Issue(WARNING, "W03", TABLE_SHEET.name, row.row_no,
                                f"{row.values['layer']}的表 {table_id} 没有任何血缘记录"))


def _check_relations(raw: RawMart, tables: dict[str, RawRow],
                     columns: dict[str, set[str]], issues: list[Issue]) -> None:
    """M11 M12 M14：手工关联关系的引用完整性和取值"""
    sheet = RELATION_SHEET.name
    bare_names = _BareNames(sheet)
    for row in raw.rows.get("relations", []):
        values = row.values
        if not (values["left_table"] and values["left_columns"]
                and values["right_table"] and values["right_columns"]):
            continue
        left_columns = split_list(values["left_columns"])
        right_columns = split_list(values["right_columns"])
        for side, table_id, names in (("左", values["left_table"], left_columns),
                                      ("右", values["right_table"], right_columns)):
            if table_id not in tables:
                matches = match_bare_name(table_id, tables)
                if not matches:
                    issues.append(Issue(ERROR, "M11", sheet, row.row_no,
                                        f"{side}表 {table_id} 不在表清单里"))
                    continue
                bare_names.add(table_id, matches, row.row_no)
                if len(matches) > 1:
                    continue
                table_id = matches[0]   # 能唯一对上：报一次错，其余校验按对上的表继续
            for name in names:
                if name not in columns[table_id]:
                    issues.append(Issue(ERROR, "M11", sheet, row.row_no,
                                        f"{side}表字段 {table_id}.{name} 不在字段清单里"))
        if len(left_columns) != len(right_columns):
            issues.append(Issue(ERROR, "M12", sheet, row.row_no, "左字段和右字段的个数不一致"))
        if values["cardinality"] and values["cardinality"] not in CARDINALITIES:
            issues.append(Issue(ERROR, "M12", sheet, row.row_no,
                                f"对应关系「{values['cardinality']}」不在允许范围内"
                                f"（{'、'.join(CARDINALITIES)}）"))
    bare_names.report(issues)


def check_mart(raw: RawMart) -> list[Issue]:
    """对一套原始资料执行全部校验规则

    Args:
        raw: load_raw 读到的原始资料

    Returns:
        问题清单。没有错误级问题时资料才可以入库
    """
    issues: list[Issue] = []
    if not _check_structure(raw, issues):
        return issues
    _check_required_cells(raw, issues)
    tables = _check_tables(raw, issues)
    columns = _check_columns(raw, tables, issues)
    _check_lineage(raw, tables, columns, issues)
    _check_relations(raw, tables, columns, issues)
    return issues
