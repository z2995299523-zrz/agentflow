"""
元数据导入
读取资料 → 校验 → 写入元数据库 → 输出导入报告
用法: python -m metadata.importer [资料目录]
      不带参数时使用 .env 里的 MART_DIR
"""
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from metadata.checks import ERROR, WARNING, Issue, check_mart, split_list, table_id_of
from metadata.loader import RawMart, load_raw
from metadata.models import ColumnMeta, LineageEdge, Mart, Relation, TableMeta
from metadata.store import write_store

BUILD_DIR_NAME = "build"
DB_FILE_NAME = "metadata.db"
REPORT_FILE_NAME = "import_report.md"
TRUE_TEXTS = ("是", "y", "yes", "1", "true")


@dataclass
class ImportReport:
    """一次导入的结果"""

    mart_dir: Path
    db_path: Path
    succeeded: bool
    issues: list[Issue] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def errors(self) -> list[Issue]:
        """错误级问题"""
        return [issue for issue in self.issues if issue.level == ERROR]

    @property
    def warnings(self) -> list[Issue]:
        """警告级问题"""
        return [issue for issue in self.issues if issue.level == WARNING]

    def to_markdown(self) -> str:
        """生成 Markdown 格式的导入报告"""
        result = "成功" if self.succeeded else "失败，元数据库未更新"
        lines = [
            "# 元数据导入报告",
            "",
            f"- 资料目录：`{self.mart_dir.as_posix()}`",
            f"- 结果：{result}",
            f"- 错误 {len(self.errors)} 条，警告 {len(self.warnings)} 条",
        ]
        if self.counts:
            lines += ["", "## 入库数量", "", "| 类别 | 数量 |", "|------|------|"]
            lines += [f"| {name} | {count} |" for name, count in self.counts.items()]
        if self.issues:
            lines += ["", "## 问题清单", "", "| 级别 | 规则 | 清单 | 行号 | 说明 |",
                      "|------|------|------|------|------|"]
            lines += [
                f"| {issue.level} | {issue.rule} | {issue.sheet} | "
                f"{issue.row_no if issue.row_no is not None else ''} | {issue.message} |"
                for issue in self.issues
            ]
        return "\n".join(lines) + "\n"


def default_db_path(mart_dir: Path) -> Path:
    """一套资料对应的元数据库默认路径：资料目录/build/metadata.db"""
    return Path(mart_dir) / BUILD_DIR_NAME / DB_FILE_NAME


def build_mart(raw: RawMart) -> Mart:
    """把原始资料转换成标准模型

    只应在 check_mart 没有报出错误之后调用。

    Args:
        raw: 校验通过的原始资料

    Returns:
        标准模型
    """
    tables = [
        TableMeta(
            table_id=table_id_of(row),
            layer=row.values["layer"],
            schema_name=row.values["schema_name"],
            table_name=row.values["table_name"],
            table_cn=row.values["table_cn"],
            table_type=row.values["table_type"],
            grain_columns=split_list(row.values["grain_columns"]),
            date_column=row.values["date_column"] or None,
            description=row.values["description"],
        )
        for row in raw.rows["tables"]
    ]
    known_tables = {table.table_id for table in tables}

    ordinals: dict[str, int] = defaultdict(int)
    columns: list[ColumnMeta] = []
    for row in raw.rows["columns"]:
        values = row.values
        ordinals[values["table_id"]] += 1
        columns.append(ColumnMeta(
            table_id=values["table_id"],
            column_name=values["column_name"],
            ordinal=ordinals[values["table_id"]],
            column_cn=values["column_cn"],
            data_type=values["data_type"],
            is_pk=values["is_pk"].lower() in TRUE_TEXTS,
            description=values["description"],
            enum_values=values["enum_values"],
        ))

    lineage = [
        LineageEdge(
            target_table=row.values["target_table"],
            target_column=row.values["target_column"],
            source_table=row.values["source_table"],
            source_column=row.values["source_column"],
            transform=row.values["transform"],
            script_name=row.values["script_name"],
            source_external=row.values["source_table"] not in known_tables,
        )
        for row in raw.rows["lineage"]
    ]

    relations = [
        Relation(
            left_table=row.values["left_table"],
            left_columns=split_list(row.values["left_columns"]),
            right_table=row.values["right_table"],
            right_columns=split_list(row.values["right_columns"]),
            cardinality=row.values["cardinality"],
            origin="manual",
            note=row.values["note"],
        )
        for row in raw.rows.get("relations", [])
    ]
    return Mart(tables=tables, columns=columns, lineage=lineage, relations=relations)


def load_mart(mart_dir: Path) -> tuple[Mart | None, list[Issue]]:
    """读取并校验一套资料

    Args:
        mart_dir: 资料目录

    Returns:
        (标准模型, 问题清单)。存在错误级问题时标准模型为 None
    """
    raw = load_raw(mart_dir)
    issues = check_mart(raw)
    if any(issue.level == ERROR for issue in issues):
        return None, issues
    return build_mart(raw), issues


def run_import(mart_dir: Path, db_path: Path | None = None) -> ImportReport:
    """导入一套资料到元数据库，并把导入报告写到资料目录的 build 子目录

    有错误时不写元数据库，原有的元数据库保持不变。

    Args:
        mart_dir: 资料目录
        db_path: 元数据库路径，默认是 资料目录/build/metadata.db

    Returns:
        导入报告
    """
    mart_dir = Path(mart_dir)
    build_dir = mart_dir / BUILD_DIR_NAME
    db_path = Path(db_path) if db_path is not None else default_db_path(mart_dir)

    mart, issues = load_mart(mart_dir)
    if mart is None:
        report = ImportReport(mart_dir=mart_dir, db_path=db_path, succeeded=False, issues=issues)
    else:
        write_store(db_path, mart)
        report = ImportReport(
            mart_dir=mart_dir, db_path=db_path, succeeded=True, issues=issues,
            counts={"表": len(mart.tables), "字段": len(mart.columns),
                    "血缘": len(mart.lineage), "关联关系": len(mart.relations)},
        )

    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / REPORT_FILE_NAME).write_text(report.to_markdown(), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    """命令行入口。导入成功返回 0，失败返回 1"""
    args = sys.argv[1:] if argv is None else argv
    if args:
        mart_dir = Path(args[0])
    else:
        import config  # 延迟导入：只有不带参数运行时才需要读取 .env
        mart_dir = Path(config.MART_DIR)

    try:
        report = run_import(mart_dir)
    except (FileNotFoundError, ValueError, RuntimeError) as error:
        print(f"导入失败: {error}")
        return 1
    print(report.to_markdown())
    return 0 if report.succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
