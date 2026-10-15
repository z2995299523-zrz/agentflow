"""
加工脚本解析
把 scripts/ 目录里的加工脚本解析成按目标表归组的脚本片段和逐字段表达式，再与血缘清单合并：
清单里写了的加工逻辑原样保留，空着的用脚本表达式回填；清单里完全没有血缘的字段，用脚本补上并标注来源。

骨架：接口已定，实现由第二份计划的 A 任务补上
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from metadata.checks import Issue, ReportSection
from metadata.models import FieldRef, LineageEdge, Mart, ScriptColumn, ScriptStatement

if TYPE_CHECKING:
    from metadata.sqlscope import Catalog

SCRIPTS_DIR_NAME = "scripts"
SECTION_SCRIPTS = "加工脚本"
SECTION_LINEAGE = "血缘合并"


@dataclass
class ScriptParseResult:
    """一套资料的加工脚本解析结果"""

    statements: list[ScriptStatement] = field(default_factory=list)
    columns: list[ScriptColumn] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)            # W06、W07
    section: ReportSection = field(default_factory=lambda: ReportSection(SECTION_SCRIPTS, ()))
    metadata_gaps: set[FieldRef] = field(default_factory=set)    # 脚本引用了、字段清单里没有的字段


def parse_script_text(text: str, script_name: str,
                      catalog: "Catalog") -> tuple[list[ScriptStatement], list[ScriptColumn], list[Issue], set[FieldRef]]:
    """解析一个脚本文件的全文

    Args:
        text: 脚本全文
        script_name: 脚本文件名，用于标注来源
        catalog: 表和字段的元数据，用来补全查询层的表结构

    Returns:
        (脚本片段, 逐字段表达式, 问题清单, 字段清单缺口)
    """
    raise NotImplementedError


def parse_scripts(scripts_dir: Path, mart: Mart) -> ScriptParseResult:
    """解析资料目录里的全部加工脚本

    Args:
        scripts_dir: 加工脚本目录，不存在时返回空结果
        mart: 已经校验通过的资料

    Returns:
        解析结果
    """
    return ScriptParseResult()


def merge_lineage(mart: Mart, parsed: ScriptParseResult) -> tuple[list[LineageEdge], ReportSection]:
    """把脚本解析结果并入血缘

    Args:
        mart: 已经校验通过的资料
        parsed: 加工脚本解析结果

    Returns:
        (合并后的全部血缘边, 报告节)
    """
    return list(mart.lineage), ReportSection(SECTION_LINEAGE, ())


def is_bare_reference(transform: str, source_column: str) -> bool:
    """判断清单里写的加工逻辑是不是对源字段的直接引用（例如 a.cust_no 或 cust_no）

    Args:
        transform: 血缘清单里的加工逻辑原文
        source_column: 源字段名

    Returns:
        是直接引用时为 True
    """
    raise NotImplementedError
