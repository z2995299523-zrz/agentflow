"""
样例解析
examples/ 目录下的 .sql 文件，每条以「-- 需求:」开头（全角冒号也认），「-- 口径:」可选，之后是 SQL，
到下一个「-- 需求:」或文件末尾结束。每条样例都要通过静态校验才收下，不通过的只拒收这一条，导入照常继续。

本模块在底层的 metadata 包里，不直接依赖 nl2sql 的校验器，由调用方传入校验函数。

规则编号（样例的问题都只是警告）：
    W08 样例被拒收：没有通过静态校验，或者缺需求、缺 SQL；样例文件本身的问题也记在这一条：
        编码无法识别，或第一条「-- 需求:」之前有 SQL
"""
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from metadata.checks import WARNING, Issue, ReportSection
from metadata.models import Example

if TYPE_CHECKING:
    from nl2sql.validator import ValidationReport

EXAMPLES_DIR_NAME = "examples"
SECTION_EXAMPLES = "样例"
EXAMPLE_ENCODINGS = ("utf-8-sig", "gb18030")    # 依次尝试；中文 Windows 上保存的文件常是 GBK
RULE_EXAMPLE = "W08"
MAX_ERRORS_SHOWN = 3        # 拒收说明里最多写几条校验错误
MAX_MESSAGE_CHARS = 160     # 一条校验错误最多写多少个字
CALIBER_JOINER = "；"        # 一条样例写了多行「-- 口径:」时的拼接符

_REQUIREMENT_LINE = re.compile(r"^\s*--\s*需求\s*[:：]\s*(.*?)\s*$")
_CALIBER_LINE = re.compile(r"^\s*--\s*口径\s*[:：]\s*(.*?)\s*$")


@dataclass(frozen=True)
class RawExample:
    """从样例文件里切出来、尚未校验的一条样例"""

    requirement: str
    caliber_note: str
    sql_text: str
    source_file: str
    line_no: int                 # 「-- 需求:」所在的行号


@dataclass
class _Draft:
    """正在切分的一条样例"""

    requirement: str
    line_no: int
    calibers: list[str] = field(default_factory=list)
    body: list[str] = field(default_factory=list)
    in_sql: bool = False         # 是否已经读到 SQL；在这之前的「-- 口径:」才算口径，之后只是 SQL 里的注释


def _is_sql(line: str) -> bool:
    """一行是不是 SQL：空行和 -- 注释行都不算"""
    text = line.strip()
    return bool(text) and not text.startswith("--")


def _trim_blank_lines(lines: list[str]) -> str:
    """去掉首尾的空行和每行末尾的空白，保留 SQL 自己的缩进"""
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(line.rstrip() for line in lines[start:end])


def parse_example_text(text: str, source_file: str) -> tuple[list[RawExample], list[Issue]]:
    """把一个样例文件切成多条样例

    需求或 SQL 为空的样例也照样切出来，编号才不会因为某一条写得不完整而错位，是否拒收由 load_examples 决定。

    Args:
        text: 文件全文
        source_file: 文件名

    Returns:
        (样例, 问题清单)；第一条「-- 需求:」之前如果有 SQL，报一条警告并忽略这些 SQL
    """
    lines = text.removeprefix("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    drafts: list[_Draft] = []
    stray_line = 0                                  # 第一条需求之前，第一行 SQL 的行号
    for line_no, line in enumerate(lines, start=1):
        marked = _REQUIREMENT_LINE.match(line)
        if marked:
            drafts.append(_Draft(marked.group(1), line_no))
            continue
        if not drafts:
            if not stray_line and _is_sql(line):
                stray_line = line_no
            continue
        draft = drafts[-1]
        caliber = None if draft.in_sql else _CALIBER_LINE.match(line)
        if caliber:
            draft.calibers.append(caliber.group(1))
            continue
        draft.in_sql = draft.in_sql or _is_sql(line)
        draft.body.append(line)

    issues: list[Issue] = []
    if stray_line:
        reason = "第一条「-- 需求:」之前" if drafts else "里没有「-- 需求:」，其中"
        issues.append(Issue(WARNING, RULE_EXAMPLE, SECTION_EXAMPLES, stray_line,
                            f"{source_file} {reason}有 SQL（从第 {stray_line} 行起），不属于任何样例，已忽略"))
    examples = [
        RawExample(requirement=draft.requirement,
                   caliber_note=CALIBER_JOINER.join(part for part in draft.calibers if part),
                   sql_text=_trim_blank_lines(draft.body), source_file=source_file, line_no=draft.line_no)
        for draft in drafts
    ]
    return examples, issues


def _read_text(path: Path) -> str | None:
    """依次按 utf-8、gb18030 读取，都读不了时返回 None"""
    data = path.read_bytes()
    for encoding in EXAMPLE_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _short(message: str) -> str:
    """压成一行，太长的截断"""
    text = " ".join(message.split())
    return text if len(text) <= MAX_MESSAGE_CHARS else text[:MAX_MESSAGE_CHARS] + "…"


def _error_summary(report: "ValidationReport") -> str:
    """校验错误的摘要：前几条的规则和说明，多出来的只写个数"""
    errors = report.errors
    parts = [f"{issue.rule} {_short(issue.message)}" for issue in errors[:MAX_ERRORS_SHOWN]]
    if len(errors) > MAX_ERRORS_SHOWN:
        parts.append(f"等共 {len(errors)} 处错误")
    return "；".join(parts)


def _review(raw: RawExample, validate: Callable[[str], "ValidationReport"]) -> tuple["ValidationReport | None", str]:
    """校验一条样例

    Returns:
        (校验报告, 拒收原因)；收下时拒收原因为空串；没来得及校验就拒收时报告为 None
    """
    if not raw.requirement:
        return None, "「-- 需求:」后面没有写需求，已拒收"
    if not any(_is_sql(line) for line in raw.sql_text.split("\n")):
        return None, "需求后面没有 SQL，已拒收"
    try:
        report = validate(raw.sql_text)
    except Exception as error:  # noqa: BLE001 校验函数不该抛异常；万一抛了也只拒收这一条，不能让导入失败
        return None, f"静态校验本身出错（{type(error).__name__}: {_short(str(error))}），已拒收"
    if not report.ok:
        return report, f"未通过静态校验，已拒收。{_error_summary(report)}"
    return report, ""


def load_examples(examples_dir: Path,
                  validate: Callable[[str], "ValidationReport"]) -> tuple[list[Example], list[Issue], ReportSection]:
    """读取并校验全部样例

    按文件名顺序读取目录下的 .sql 文件，编码先试 utf-8 再试 gb18030。
    样例编号是「文件名#序号」，序号是它在文件里的位置（从 1 起，被拒收的也占位），
    所以拒收或删除某一条，不会让同一个文件里其余样例的编号变化。
    校验有错误的样例拒收并报 W08，只有警告的照常收下。

    Args:
        examples_dir: 样例目录，不存在时返回空结果
        validate: 静态校验函数，输入 SQL，返回校验报告

    Returns:
        (收下的样例, 问题清单, 报告节)
    """
    examples_dir = Path(examples_dir)
    if not examples_dir.is_dir():
        return [], [], ReportSection(SECTION_EXAMPLES, ())
    paths = sorted((path for path in examples_dir.iterdir() if path.is_file() and path.suffix.lower() == ".sql"),
                   key=lambda path: path.name)

    accepted: list[Example] = []
    issues: list[Issue] = []
    total = 0
    for path in paths:
        text = _read_text(path)
        if text is None:
            issues.append(Issue(WARNING, RULE_EXAMPLE, SECTION_EXAMPLES, None,
                                f"{path.name} 的编码无法识别（试过 utf-8、gb18030），整个文件跳过"))
            continue
        raws, parse_issues = parse_example_text(text, path.name)
        issues += parse_issues
        total += len(raws)
        for index, raw in enumerate(raws, start=1):
            report, reason = _review(raw, validate)
            if reason:
                issues.append(Issue(WARNING, RULE_EXAMPLE, SECTION_EXAMPLES, raw.line_no,
                                    f"{path.name} 第 {index} 条样例：{reason}"))
                continue
            accepted.append(Example(
                example_id=f"{path.name}#{index}", requirement=raw.requirement, caliber_note=raw.caliber_note,
                sql_text=raw.sql_text, tables=list(report.tables), source_file=path.name, line_no=raw.line_no))

    section = ReportSection(SECTION_EXAMPLES, (
        f"- 样例文件 {len(paths)} 个，样例 {total} 条：收下 {len(accepted)} 条，拒收 {total - len(accepted)} 条",
    ))
    return accepted, issues, section
