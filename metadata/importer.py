"""
元数据导入
读取资料 → 校验 → 补全（解析加工脚本、合并血缘、推导关联、收样例）→ 算向量 → 写入元数据库 → 输出导入报告
用法: python -m metadata.importer [资料目录] [--no-index | --reindex] [--no-promote] [--force] [--replay]
      不带资料目录时使用 .env 里的 MART_DIR
      向量按文本复用上一次导入的，只算新出现的文本；全部能复用时不加载向量模型
      --reindex 不复用，全部重算：换了向量模型，或怀疑向量有问题时用
      --no-index 不算向量：不加载向量模型、导入更快，召回只能走关键字
      命令行导入保存版本，没有需确认项才默认切换；有需确认项用 promote --accept 确认后切换
      --no-promote 只生成候选，--force 强制生成新版本，--replay 强制召回重放（可能加载 BGE）
"""
import sys
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from metadata.checks import ERROR, WARNING, Issue, ReportSection, check_mart, split_list, table_id_of
from metadata.examples import EXAMPLES_DIR_NAME, load_examples
from metadata.index import BgeEmbedder, Embedder, build_vectors, default_embedder
from metadata.loader import RawMart, load_raw
from metadata.models import ColumnMeta, LineageEdge, Mart, Relation, TableMeta
from metadata.relations import derive_relations
from metadata.script_parser import SCRIPTS_DIR_NAME, merge_lineage, parse_scripts
from metadata.store import MetadataStore, VectorBundle, VectorSet, read_vectors, write_store

BUILD_DIR_NAME = "build"
DB_FILE_NAME = "metadata.db"
REPORT_FILE_NAME = "import_report.md"
TRUE_TEXTS = ("是", "y", "yes", "1", "true")
NO_INDEX_FLAG = "--no-index"
REINDEX_FLAG = "--reindex"
NO_PROMOTE_FLAG = "--no-promote"
FORCE_FLAG = "--force"
REPLAY_FLAG = "--replay"
USAGE = ("用法: python -m metadata.importer [资料目录] [--no-index | --reindex] [--no-promote] [--force] [--replay]\n"
         "  不带资料目录时使用 .env 里的 MART_DIR；向量按文本复用上一次导入的，只算新出现的文本\n"
         "  --reindex 不复用，全部重算；--no-index 不算向量，导入更快，召回只能走关键字\n"
         "  --no-promote 只生成候选版本；--force 资料和代码没变也生成新版本\n"
         "  --replay 强制召回重放（可能加载 BGE），可与 --no-index 一起用；有需确认项用 promote --accept 切换")
MODEL_HINT = ("有 {count} 条文本要算向量。第一次加载向量模型可能较慢，机械盘上冷启动要几分钟；"
              "不需要向量时加 --no-index 跳过")


def _cell(text: str) -> str:
    """Markdown 表格单元格里的竖线和换行会把表格撑乱，转成安全的写法"""
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


@dataclass
class ImportReport:
    """一次导入的结果"""

    mart_dir: Path
    db_path: Path
    succeeded: bool
    issues: list[Issue] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    sections: list[ReportSection] = field(default_factory=list)   # 各补全步骤的统计
    version_id: str | None = None

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
        if self.version_id is not None:
            lines.insert(3, f"- 版本：{self.version_id}")
        if self.counts:
            lines += ["", "## 入库数量", "", "| 类别 | 数量 |", "|------|------|"]
            lines += [f"| {name} | {count} |" for name, count in self.counts.items()]
        for section in self.sections:
            if section.lines:
                lines += ["", f"## {section.title}", "", *section.lines]
        if self.issues:
            lines += ["", "## 问题清单", "", "| 级别 | 规则 | 清单 | 行号 | 说明 |",
                      "|------|------|------|------|------|"]
            lines += [
                f"| {issue.level} | {issue.rule} | {_cell(issue.sheet)} | "
                f"{issue.row_no if issue.row_no is not None else ''} | {_cell(issue.message)} |"
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
    """读取并校验一套资料的四张清单，不解析加工脚本、不推导关联

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


def enrich_mart(mart: Mart, mart_dir: Path) -> tuple[Mart, list[Issue], list[ReportSection]]:
    """补全一套已经校验通过的资料：解析加工脚本 → 合并血缘 → 推导关联 → 收样例

    这一步只产生警告，不会让导入失败。

    Args:
        mart: load_mart 得到的资料
        mart_dir: 资料目录，加工脚本和样例在它的子目录里

    Returns:
        (补全后的资料, 问题清单, 报告节)
    """
    mart_dir = Path(mart_dir)
    parsed = parse_scripts(mart_dir / SCRIPTS_DIR_NAME, mart)
    issues = list(parsed.issues)
    sections = [parsed.section]

    lineage, section = merge_lineage(mart, parsed)
    sections.append(section)
    mart = mart.model_copy(update={"scripts": parsed.statements, "script_columns": parsed.columns,
                                   "lineage": lineage})

    relations, section = derive_relations(mart)
    sections.append(section)
    mart = mart.model_copy(update={"relations": relations})

    # 唯一一处由底层 metadata 向上依赖 nl2sql：样例要用静态校验器把关，只在导入时用到，所以延迟导入
    from nl2sql.validator import SqlValidator

    validate = SqlValidator(MetadataStore.from_mart(mart)).validate
    examples, example_issues, section = load_examples(mart_dir / EXAMPLES_DIR_NAME, validate)
    issues += example_issues
    sections.append(section)
    mart = mart.model_copy(update={"examples": examples})
    return mart, issues, sections


def _previous_vectors(db_path: Path, reindex: bool) -> tuple[VectorBundle | None, str]:
    """上一次导入写下的向量，这次按文本复用

    Returns:
        (向量, 用不了的原因)；用不了时向量为 None，原因写进导入报告
    """
    if reindex:
        return None, f"加了 {REINDEX_FLAG}"
    try:
        previous = read_vectors(db_path)
    except FileNotFoundError:
        return None, "元数据库还不存在"
    except RuntimeError as error:
        return None, str(error)
    if previous is None or not any(vector_set.ref_ids for vector_set in previous.sets.values()):
        return None, "上一次导入没有算向量"
    return previous, ""


def _merged_vectors(paths: Sequence[Path], reindex: bool) -> tuple[VectorBundle | None, str]:
    """按来源顺序拼接兼容的向量，保留第一份来源用不了的原因。"""
    if reindex:
        return None, f"加了 {REINDEX_FLAG}"
    merged = None
    first_note = "元数据库还不存在"
    for index, path in enumerate(paths):
        bundle, note = _previous_vectors(path, False)
        if index == 0:
            first_note = note
        if bundle is None:
            continue
        if merged is None:
            merged = VectorBundle(bundle.embedder, bundle.dim, dict(bundle.sets))
        elif (bundle.embedder, bundle.dim) == (merged.embedder, merged.dim):
            for kind, incoming in bundle.sets.items():
                if kind not in merged.sets:
                    merged.sets[kind] = incoming
                else:
                    previous = merged.sets[kind]
                    merged.sets[kind] = VectorSet(
                        kind, previous.ref_ids + incoming.ref_ids, previous.texts + incoming.texts,
                        np.concatenate((previous.matrix, incoming.matrix), axis=0))
    return merged, "" if merged is not None else first_note


def run_import(mart_dir: Path, db_path: Path | None = None, *, build_index: bool = True,
               embedder: Embedder | None = None, reindex: bool = False,
               before_embed: Callable[[int], None] | None = None,
               report_path: Path | None = None, reuse_from: Sequence[Path] | None = None,
               version_id: str | None = None) -> ImportReport:
    """导入一套资料到元数据库，并把导入报告写到资料目录的 build 子目录

    有错误时不写元数据库，原有的元数据库保持不变。向量按文本复用原有元数据库里的，只算新出现的文本。

    Args:
        mart_dir: 资料目录
        db_path: 元数据库路径，默认是 资料目录/build/metadata.db
        build_index: 是否为字段和样例算向量
        embedder: 向量器，为 None 时按环境变量取默认的
        reindex: 不复用原有的向量，全部重算
        before_embed: 要调用向量器之前调它一次，参数是要算的文本条数；全部复用时不调
        report_path: 报告路径，默认是资料目录/build/import_report.md
        reuse_from: 按顺序读取的向量来源，默认只读 db_path
        version_id: 可选的版本号，同时写进元数据库和报告

    Returns:
        导入报告
    """
    mart_dir = Path(mart_dir)
    build_dir = mart_dir / BUILD_DIR_NAME
    db_path = Path(db_path) if db_path is not None else default_db_path(mart_dir)
    report_path = Path(report_path) if report_path is not None else build_dir / REPORT_FILE_NAME

    mart, issues = load_mart(mart_dir)
    if mart is None:
        report = ImportReport(mart_dir=mart_dir, db_path=db_path, succeeded=False, issues=issues)
    else:
        mart, enrich_issues, sections = enrich_mart(mart, mart_dir)
        issues = issues + enrich_issues
        vectors = None
        if build_index:
            previous, note = _merged_vectors([db_path] if reuse_from is None else reuse_from, reindex)
            vectors, section = build_vectors(mart, embedder if embedder is not None else default_embedder(),
                                             previous=previous, previous_note=note, before_embed=before_embed)
            sections.append(section)
        write_store(db_path, mart, vectors, info={"version_id": version_id} if version_id is not None else None)
        report = ImportReport(
            mart_dir=mart_dir, db_path=db_path, succeeded=True, issues=issues, sections=sections,
            counts=MetadataStore.from_mart(mart, vectors).counts(),
        )

    report.version_id = version_id
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report.to_markdown(), encoding="utf-8")
    return report


def _hint_before_loading(count: int) -> None:
    """要调 BGE 算向量之前提示一声：加载模型期间没有任何输出，免得以为卡住了"""
    print(MODEL_HINT.format(count=count), flush=True)


def main(argv: list[str] | None = None) -> int:
    """命令行入口

    Returns:
        0 表示导入成功；1 表示失败；2 表示参数不认识（或者 --no-index 和 --reindex 一起用），打印了用法
    """
    args = sys.argv[1:] if argv is None else argv
    build_index = NO_INDEX_FLAG not in args
    reindex = REINDEX_FLAG in args
    names = [arg for arg in args if arg not in (NO_INDEX_FLAG, REINDEX_FLAG, NO_PROMOTE_FLAG, FORCE_FLAG, REPLAY_FLAG)]
    if len(names) > 1 or any(name.startswith("-") for name in names) or (reindex and not build_index):
        print(USAGE)
        return 2
    if names:
        mart_dir = Path(names[0])
    else:
        import config  # 延迟导入：只有不带参数运行时才需要读取 .env
        mart_dir = Path(config.MART_DIR)

    try:
        from metadata.versions import import_version  # 延迟导入，版本层依赖本模块

        # 只在真要调 BGE 时提示；向量全部能复用时不加载模型，也就不提示
        embedder = default_embedder() if build_index else None
        hint = _hint_before_loading if isinstance(embedder, BgeEmbedder) else None
        result = import_version(mart_dir, build_index=build_index, embedder=embedder, reindex=reindex,
                                force=FORCE_FLAG in args, promote=NO_PROMOTE_FLAG not in args, before_embed=hint,
                                replay=REPLAY_FLAG in args)
    except (FileNotFoundError, ValueError, RuntimeError) as error:
        print(f"导入失败: {error}")
        return 1
    if result.skipped:
        print(f"资料和代码都没变，与活动版本 {result.active} 相同，没有生成新版本；要重新导入加 --force")
        return 0
    report = result.report
    if report is not None:
        print(report.to_markdown())
    if result.diff_report is not None:
        if result.confirm_items:
            print("差异需确认项：")
            for number, item in enumerate(result.confirm_items, 1):
                print(f"{number}. {item}")
            print(f"差异报告：{result.diff_report}")
        else:
            print(f"差异：没有需确认项（报告：{result.diff_report}）")
    if report is None or not report.succeeded:
        print(f"版本 {result.version_id} 导入失败，活动版本没变；报告在 {result.version_dir / REPORT_FILE_NAME}")
        return 1
    if result.promoted:
        previous = f"（上一个：{result.previous}）" if result.previous else "（这是第一个版本）"
        print(f"版本 {result.version_id} 已切换为活动版本{previous}")
    elif result.confirm_items:
        print(f"版本 {result.version_id} 有 {len(result.confirm_items)} 项需要确认，没有切换；"
              f"看过差异报告后运行 python -m metadata.versions promote {result.version_id} --accept")
    else:
        print(f"版本 {result.version_id} 是候选版本，没有切换；"
              f"要切换运行 python -m metadata.versions promote {result.version_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
