"""
样例解析
examples/ 目录下的 .sql 文件，每条以「-- 需求:」开头，「-- 口径:」可选，之后是 SQL，
到下一个「-- 需求:」或文件末尾结束。每条样例都要通过静态校验才收下，不通过的只拒收这一条。

本模块在底层的 metadata 包里，不直接依赖 nl2sql 的校验器，由调用方传入校验函数。

骨架：接口已定，实现由第二份计划的 E 任务补上
"""
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from metadata.checks import Issue, ReportSection
from metadata.models import Example

if TYPE_CHECKING:
    from nl2sql.validator import ValidationReport

EXAMPLES_DIR_NAME = "examples"
SECTION_EXAMPLES = "样例"


@dataclass(frozen=True)
class RawExample:
    """从样例文件里切出来、尚未校验的一条样例"""

    requirement: str
    caliber_note: str
    sql_text: str
    source_file: str
    line_no: int                 # 「-- 需求:」所在的行号


def parse_example_text(text: str, source_file: str) -> tuple[list[RawExample], list[Issue]]:
    """把一个样例文件切成多条样例

    Args:
        text: 文件全文
        source_file: 文件名

    Returns:
        (样例, 问题清单)
    """
    raise NotImplementedError


def load_examples(examples_dir: Path,
                  validate: Callable[[str], "ValidationReport"]) -> tuple[list[Example], list[Issue], ReportSection]:
    """读取并校验全部样例

    Args:
        examples_dir: 样例目录，不存在时返回空结果
        validate: 静态校验函数，输入 SQL，返回校验报告

    Returns:
        (收下的样例, 问题清单, 报告节)
    """
    return [], [], ReportSection(SECTION_EXAMPLES, ())
