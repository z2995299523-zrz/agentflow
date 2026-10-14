"""
元数据标准模板
定义四张清单的名称、列、必填项和填写说明，并能生成空白的 Excel 模板
用法: python -m metadata.template [输出路径.xlsx]
"""
import sys
from dataclasses import dataclass
from pathlib import Path

from openpyxl import Workbook

from metadata.models import CARDINALITIES, LAYERS, TABLE_TYPES

WORKBOOK_NAME = "metadata.xlsx"
GUIDE_SHEET_NAME = "填写说明"
FULL_NAME_HELP = "带模式名的全名，例如 ads.corp_cust_dep_bal_d。每一行都要填，不能用合并单元格"


@dataclass(frozen=True)
class ColumnSpec:
    """清单里的一列"""

    header: str                # 清单里的中文列名
    field: str                 # 程序内部使用的字段名
    required: bool = False     # 是否必填
    identifier: bool = False   # 是否标识符（表名、字段名），读取时统一转小写
    help: str = ""             # 填写说明


@dataclass(frozen=True)
class SheetSpec:
    """一张清单"""

    name: str                  # 清单名，同时是 Sheet 名和 CSV 文件名
    key: str                   # 程序内部使用的键
    columns: tuple[ColumnSpec, ...]
    optional: bool = False     # 整张清单是否可以不提供


TABLE_SHEET = SheetSpec(
    name="表清单",
    key="tables",
    columns=(
        ColumnSpec("层级", "layer", required=True, help="只能填：" + "、".join(LAYERS)),
        ColumnSpec("模式名", "schema_name", required=True, identifier=True, help="数据库模式名"),
        ColumnSpec("表英文名", "table_name", required=True, identifier=True, help="不带模式名"),
        ColumnSpec("表中文名", "table_cn", required=True),
        ColumnSpec("表类型", "table_type", required=True, help="只能填：" + "、".join(TABLE_TYPES)),
        ColumnSpec("粒度字段", "grain_columns", identifier=True,
                   help="能唯一确定一行的字段，多个用英文逗号分隔。不清楚可以留空，留空后无法判断关联是否会发散"),
        ColumnSpec("数据日期字段", "date_column", identifier=True, help="快照表必填"),
        ColumnSpec("表说明", "description"),
    ),
)

COLUMN_SHEET = SheetSpec(
    name="字段清单",
    key="columns",
    columns=(
        ColumnSpec("表名", "table_id", required=True, identifier=True, help=FULL_NAME_HELP),
        ColumnSpec("字段英文名", "column_name", required=True, identifier=True),
        ColumnSpec("字段中文名", "column_cn", required=True),
        ColumnSpec("数据类型", "data_type", help="例如 varchar(20)、numeric(18,2)、date。不清楚可以留空"),
        ColumnSpec("是否主键", "is_pk", help="主键填「是」，其余留空"),
        ColumnSpec("口径说明", "description", help="数据字典里对该字段的说明"),
        ColumnSpec("码值说明", "enum_values", help="例如 1=对公;2=个人"),
    ),
)

LINEAGE_SHEET = SheetSpec(
    name="血缘清单",
    key="lineage",
    columns=(
        ColumnSpec("目标表", "target_table", required=True, identifier=True, help=FULL_NAME_HELP),
        ColumnSpec("目标字段", "target_column", required=True, identifier=True),
        ColumnSpec("源表", "source_table", required=True, identifier=True,
                   help=FULL_NAME_HELP + "。不在表清单里的源表按外部源处理"),
        ColumnSpec("源字段", "source_column", required=True, identifier=True),
        ColumnSpec("加工逻辑", "transform", help="可不填，不填时从加工脚本解析"),
        ColumnSpec("加工脚本", "script_name", help="脚本文件名，可不填"),
    ),
)

RELATION_SHEET = SheetSpec(
    name="关联关系",
    key="relations",
    optional=True,
    columns=(
        ColumnSpec("左表", "left_table", required=True, identifier=True, help=FULL_NAME_HELP),
        ColumnSpec("左字段", "left_columns", required=True, identifier=True, help="复合键用英文逗号分隔"),
        ColumnSpec("右表", "right_table", required=True, identifier=True, help=FULL_NAME_HELP),
        ColumnSpec("右字段", "right_columns", required=True, identifier=True, help="个数要和左字段一致"),
        ColumnSpec("对应关系", "cardinality", required=True, help="只能填：" + "、".join(CARDINALITIES)),
        ColumnSpec("说明", "note"),
    ),
)

SHEETS: tuple[SheetSpec, ...] = (TABLE_SHEET, COLUMN_SHEET, LINEAGE_SHEET, RELATION_SHEET)


def write_blank_template(path: Path) -> Path:
    """生成空白的 Excel 模板：一张填写说明加四张只有表头的清单

    Args:
        path: 输出文件路径

    Returns:
        实际写入的文件路径
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    guide = workbook.active
    guide.title = GUIDE_SHEET_NAME
    guide.append(["清单", "列名", "是否必填", "说明"])
    for sheet in SHEETS:
        for column in sheet.columns:
            guide.append([
                sheet.name + ("（可选）" if sheet.optional else ""),
                column.header,
                "是" if column.required else "",
                column.help,
            ])

    for sheet in SHEETS:
        worksheet = workbook.create_sheet(sheet.name)
        worksheet.append([column.header for column in sheet.columns])

    workbook.save(path)
    return path


def main(argv: list[str] | None = None) -> int:
    """命令行入口"""
    args = sys.argv[1:] if argv is None else argv
    target = Path(args[0]) if args else Path("metadata_template.xlsx")
    print(f"空白模板已生成: {write_blank_template(target)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
