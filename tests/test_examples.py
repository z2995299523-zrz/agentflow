"""样例库的测试：切分与编号、拒收只影响单条、模拟样例通过校验且能执行、不与题库撞车"""
import re
from pathlib import Path

import pytest

from eval.questions import load_questions
from metadata.checks import ERROR, WARNING, ReportSection
from metadata.examples import EXAMPLES_DIR_NAME, SECTION_EXAMPLES, load_examples, parse_example_text
from nl2sql.executor import DuckDBExecutor
from nl2sql.validator import SqlValidator, ValidationIssue, ValidationReport

MOCK_MART = Path(__file__).parent.parent / "data" / "mock_mart"
MOCK_EXAMPLES_DIR = MOCK_MART / EXAMPLES_DIR_NAME
QUESTIONS = load_questions()
SQL_QUESTIONS = [question for question in QUESTIONS if question.expect == "sql"]
MAX_TEXT_SIMILARITY = 0.5       # 样例需求与题目的二元组 Jaccard 必须小于它


def read_mock_examples() -> list:
    """不经过校验，把模拟样例目录里的全部样例切出来"""
    raws = []
    for path in sorted(MOCK_EXAMPLES_DIR.glob("*.sql")):
        found, issues = parse_example_text(path.read_text(encoding="utf-8"), path.name)
        assert not issues, issues
        raws += found
    return raws


MOCK_RAWS = read_mock_examples()


def write_files(directory: Path, files: dict[str, str | bytes]) -> Path:
    """把几个样例文件写进目录，返回目录"""
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (directory / name).write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return directory


class FakeValidator:
    """假的校验函数：SQL 里有 BAD 报一条错误，有 WARN 只报警告，有 CRASH 直接抛异常；记下被调用时的 SQL"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, sql: str) -> ValidationReport:
        self.calls.append(sql)
        if "CRASH" in sql:
            raise RuntimeError("校验函数坏了")
        issues = []
        if "BAD" in sql:
            issues.append(ValidationIssue("R3", ERROR, "表 ads.nope 不存在于元数据"))
        if "WARN" in sql:
            issues.append(ValidationIssue("R6", WARNING, "关联条件不在已知的关联关系里"))
        return ValidationReport(issues=issues, tables=["ads.b", "ads.a"], columns=["ads.a.x"])


# ── 切分 ──────────────────────────────────────────────────

SAMPLE_FILE = """\
-- 示例文件的说明，不是 SQL
-- 需求: 查甲
-- 口径: 折人民币
SELECT 1;

-- 需求: 查乙
SELECT 2
FROM t;
-- 需求：查丙
-- 口径：含保证金
SELECT 3
"""


def test_one_file_holds_several_examples():
    raws, issues = parse_example_text(SAMPLE_FILE, "a.sql")

    assert issues == []
    assert [(raw.requirement, raw.caliber_note, raw.sql_text, raw.line_no) for raw in raws] == [
        ("查甲", "折人民币", "SELECT 1;", 2),
        ("查乙", "", "SELECT 2\nFROM t;", 6),
        ("查丙", "含保证金", "SELECT 3", 9),
    ]
    assert {raw.source_file for raw in raws} == {"a.sql"}


def test_caliber_is_optional_and_fullwidth_colon_is_accepted():
    raws, _ = parse_example_text(SAMPLE_FILE, "a.sql")

    assert raws[1].caliber_note == ""              # 没写口径
    assert raws[2].requirement == "查丙"           # 「需求：」全角冒号
    assert raws[2].caliber_note == "含保证金"      # 「口径：」全角冒号


def test_several_caliber_lines_are_joined_but_a_caliber_after_the_sql_is_just_a_comment():
    text = "-- 需求: 甲\n-- 口径: 折人民币\n-- 口径: 不含保证金\nSELECT 1\n-- 口径: 这是 SQL 里的注释\n"

    raws, _ = parse_example_text(text, "a.sql")

    assert raws[0].caliber_note == "折人民币；不含保证金"
    assert raws[0].sql_text == "SELECT 1\n-- 口径: 这是 SQL 里的注释"


def test_windows_line_endings_and_bom_do_not_shift_line_numbers():
    text = "﻿-- 需求: 甲\r\nSELECT 1\r\n\r\n-- 需求: 乙\r\nSELECT 2\r\n"

    raws, issues = parse_example_text(text, "a.sql")

    assert issues == []
    assert [(raw.requirement, raw.sql_text, raw.line_no) for raw in raws] == [("甲", "SELECT 1", 1), ("乙", "SELECT 2", 4)]


def test_sql_before_the_first_requirement_warns_and_is_ignored():
    raws, issues = parse_example_text("-- 说明\n\nSELECT 0;\n-- 需求: 甲\nSELECT 1\n", "a.sql")

    assert [raw.sql_text for raw in raws] == ["SELECT 1"]
    assert [(issue.level, issue.rule, issue.sheet, issue.row_no) for issue in issues] == [
        (WARNING, "W08", SECTION_EXAMPLES, 3)]
    assert issues[0].message.startswith("a.sql 第一条") and "第 3 行" in issues[0].message


def test_file_without_any_requirement_warns_and_yields_nothing():
    raws, issues = parse_example_text("SELECT 1;\n", "a.sql")

    assert raws == []
    assert len(issues) == 1 and issues[0].rule == "W08" and "没有" in issues[0].message


def test_comments_and_blank_lines_before_the_first_requirement_are_fine():
    assert parse_example_text("", "a.sql") == ([], [])
    assert parse_example_text("-- 只有说明\n\n-- 需求: 甲\nSELECT 1\n", "a.sql")[1] == []


# ── 读取与拒收 ────────────────────────────────────────────

def test_a_failed_validation_rejects_only_that_example(tmp_path):
    write_files(tmp_path, {"a.sql": "-- 需求: 甲\nSELECT 1\n\n-- 需求: 乙\nSELECT BAD\n\n-- 需求: 丙\nSELECT 3\n"})
    validator = FakeValidator()

    examples, issues, section = load_examples(tmp_path, validator)

    assert [example.example_id for example in examples] == ["a.sql#1", "a.sql#3"]
    assert len(validator.calls) == 3
    assert len(issues) == 1
    issue = issues[0]
    assert (issue.level, issue.rule, issue.sheet, issue.row_no) == (WARNING, "W08", SECTION_EXAMPLES, 4)
    assert issue.message.startswith("a.sql 第 2 条样例：") and "R3" in issue.message and "ads.nope" in issue.message
    assert section == ReportSection(SECTION_EXAMPLES, ("- 样例文件 1 个，样例 3 条：收下 2 条，拒收 1 条",))


def test_accepted_example_carries_text_position_and_tables_from_the_report(tmp_path):
    write_files(tmp_path, {"a.sql": "\n-- 需求: 甲\n-- 口径: 折人民币\nSELECT 1\n"})

    examples, issues, _ = load_examples(tmp_path, FakeValidator())

    assert issues == []
    example = examples[0]
    assert (example.requirement, example.caliber_note, example.sql_text) == ("甲", "折人民币", "SELECT 1")
    assert (example.source_file, example.line_no) == ("a.sql", 2)
    assert example.tables == ["ads.b", "ads.a"]          # 原样取自校验报告


def test_warnings_alone_do_not_reject_an_example(tmp_path):
    write_files(tmp_path, {"a.sql": "-- 需求: 甲\nSELECT WARN\n"})

    examples, issues, _ = load_examples(tmp_path, FakeValidator())

    assert [example.example_id for example in examples] == ["a.sql#1"]
    assert issues == []


def test_example_ids_are_file_name_and_position_whatever_the_validation_says(tmp_path):
    write_files(tmp_path, {
        "b.sql": "-- 需求: 乙一\nSELECT 1\n-- 需求: 乙二\nSELECT 2\n",
        "a.sql": "-- 需求: 甲一\nSELECT 1\n-- 需求: 甲二\nSELECT BAD\n-- 需求: 甲三\nSELECT 3\n",
        "notes.txt": "-- 需求: 不是 sql 文件\nSELECT 9\n",
    })

    strict, _, _ = load_examples(tmp_path, FakeValidator())
    lenient, _, _ = load_examples(tmp_path, lambda sql: ValidationReport(tables=["ads.a"]))

    assert [example.example_id for example in lenient] == ["a.sql#1", "a.sql#2", "a.sql#3", "b.sql#1", "b.sql#2"]
    assert [example.example_id for example in strict] == ["a.sql#1", "a.sql#3", "b.sql#1", "b.sql#2"]
    assert load_examples(tmp_path, FakeValidator())[0] == strict          # 重复读取结果相同


def test_missing_directory_gives_an_empty_result_without_validating(tmp_path):
    validator = FakeValidator()

    result = load_examples(tmp_path / "no_such_dir", validator)

    assert result == ([], [], ReportSection(SECTION_EXAMPLES, ()))
    assert validator.calls == []


def test_incomplete_examples_and_a_crashing_validator_never_fail_the_import(tmp_path):
    write_files(tmp_path, {
        "a.sql": "-- 需求: 只有需求\n\n-- 需求:\nSELECT 2\n\n-- 需求: 校验会崩\nSELECT CRASH\n\n-- 需求: 正常\nSELECT 4\n"})
    validator = FakeValidator()

    examples, issues, section = load_examples(tmp_path, validator)

    assert [example.example_id for example in examples] == ["a.sql#4"]
    assert validator.calls == ["SELECT CRASH", "SELECT 4"]          # 缺需求、缺 SQL 的不送去校验
    assert [(issue.rule, issue.row_no) for issue in issues] == [("W08", 1), ("W08", 3), ("W08", 6)]
    assert "没有 SQL" in issues[0].message and "没有写需求" in issues[1].message and "校验本身出错" in issues[2].message
    assert "拒收 3 条" in section.lines[0]


def test_gbk_and_bom_files_are_readable_and_unreadable_files_are_skipped(tmp_path):
    write_files(tmp_path, {
        "gbk.sql": "-- 需求: 国标编码\nSELECT 1\n".encode("gb18030"),
        "bom.sql": "﻿-- 需求: 带标记\nSELECT 2\n".encode("utf-8"),
        "broken.sql": b"-- \xff\xff\nSELECT 3\n",
    })

    examples, issues, section = load_examples(tmp_path, FakeValidator())

    assert [(example.example_id, example.requirement) for example in examples] == [
        ("bom.sql#1", "带标记"), ("gbk.sql#1", "国标编码")]
    assert [(issue.rule, issue.row_no) for issue in issues] == [("W08", None)]
    assert "broken.sql" in issues[0].message and "编码" in issues[0].message
    assert "样例文件 3 个" in section.lines[0]


# ── 模拟样例 ──────────────────────────────────────────────

def test_mock_examples_exist_and_are_split_into_a_few_files():
    assert len(MOCK_RAWS) >= 12
    assert 2 <= len({raw.source_file for raw in MOCK_RAWS}) <= 3
    assert all(raw.requirement and raw.sql_text for raw in MOCK_RAWS)


def test_mock_examples_all_pass_the_real_validator(mock_store):
    examples, issues, section = load_examples(MOCK_EXAMPLES_DIR, SqlValidator(mock_store).validate)

    assert issues == []
    assert len(examples) == len(MOCK_RAWS)
    assert "拒收 0 条" in section.lines[0]
    assert all(example.tables for example in examples)
    assert mock_store.examples() == examples                  # 补全阶段收进元数据的就是这些


@pytest.mark.parametrize("raw", MOCK_RAWS, ids=lambda raw: f"{raw.source_file[:2]}-line{raw.line_no}")
def test_mock_example_runs_on_the_mock_database(raw, mock_db):
    result = DuckDBExecutor(mock_db).execute(raw.sql_text, max_rows=None)

    assert result.executed, result.error
    assert result.row_count >= 1


# ── 不与题库撞车 ──────────────────────────────────────────

HAN_RUN = re.compile(r"[一-鿿]+")
DATE_AND_NUMBER_PATTERNS = (
    re.compile(r"\d{4}\s*[-/]\s*\d{1,2}\s*[-/]\s*\d{1,2}"),     # 2026-09-30
    re.compile(r"\d+\s*年"),
    re.compile(r"\d+\s*月\s*[末底初份]?"),
    re.compile(r"\d+\s*[日号]"),
    re.compile(r"\d+(?:\.\d+)?"),                                # 其余数字
)


def han_bigrams(text: str) -> set[str]:
    """去掉日期和数字之后，每段连续汉字里相邻两个字组成的二元组"""
    for pattern in DATE_AND_NUMBER_PATTERNS:
        text = pattern.sub(" ", text)
    return {run[i:i + 2] for run in HAN_RUN.findall(text) for i in range(len(run) - 1)}


def similarity(a: str, b: str) -> float:
    """两段需求文本的二元组 Jaccard 相似度，日期和数字都先去掉"""
    left, right = han_bigrams(a), han_bigrams(b)
    return len(left & right) / len(left | right) if left | right else 0.0


@pytest.fixture(scope="module")
def signature_of(mock_store):
    """SQL → (用表集合, 用到的非日期字段集合)，取自静态校验报告；日期字段指数据类型是 date 的字段"""
    validator = SqlValidator(mock_store)
    date_columns = {f"{table.table_id}.{column.column_name}" for table in mock_store.tables()
                    for column in mock_store.columns(table.table_id) if column.data_type.lower().startswith("date")}

    def signature(sql: str) -> tuple[frozenset[str], frozenset[str]]:
        report = validator.validate(sql)
        assert report.ok, report.errors
        return frozenset(report.tables), frozenset(name for name in report.columns if name not in date_columns)

    return signature


def test_text_helper_drops_dates_and_numbers():
    assert han_bigrams("查 2026 年 9 月末各行 C000015，2026-09-30 前 10 名") == {"各行"}
    assert han_bigrams("6月30日的贷款") == {"的贷", "贷款"}


def test_guards_catch_a_question_that_only_changes_the_date(signature_of):
    question = next(item for item in QUESTIONS if item.id == "D05")
    moved_sql = question.gold_sql.replace("2026-09-30", "2026-06-30")
    moved_text = question.turns[0].replace("9 月末", "6 月末")

    assert signature_of(moved_sql) == signature_of(question.gold_sql)
    assert similarity(moved_text, question.turns[0]) == 1.0


def test_no_example_has_the_table_and_field_signature_of_a_question(signature_of):
    gold = {question.id: signature_of(question.gold_sql) for question in SQL_QUESTIONS}

    for raw in MOCK_RAWS:
        same = [question_id for question_id, signature in gold.items() if signature == signature_of(raw.sql_text)]
        assert not same, f"{raw.source_file} 第 {raw.line_no} 行的样例与题目 {same} 用的表和字段完全相同"


def test_no_example_reads_like_a_question():
    for raw in MOCK_RAWS:
        closest = max((similarity(raw.requirement, question.turns[0]), question.id) for question in QUESTIONS)
        assert closest[0] < MAX_TEXT_SIMILARITY, (
            f"{raw.source_file} 第 {raw.line_no} 行的需求与题目 {closest[1]} 的相似度 {closest[0]:.2f}")
