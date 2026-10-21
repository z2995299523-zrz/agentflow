"""评估题库的测试：结构合规，标准 SQL 都能在模拟库上查出结果，埋点确实有区分度，其它正确写法与标准答案一致"""
import re
from collections import Counter
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from eval.compare import compare_results
from eval.questions import CATEGORY_COUNTS, Alternative, EvalQuestion, load_questions
from metadata.importer import load_mart
from nl2sql.executor import DuckDBExecutor

MOCK_MART = Path(__file__).parent.parent / "data" / "mock_mart"
QUESTIONS = load_questions()
SQL_QUESTIONS = [question for question in QUESTIONS if question.expect == "sql"]
MART, _ = load_mart(MOCK_MART)
KNOWN_COLUMNS = {(column.table_id, column.column_name) for column in MART.columns}
ALTERNATIVES = [(question, index) for question in QUESTIONS for index in range(len(question.alternatives))]
ORG_TABLE = "ads.org_corp_dep_bal_d"
CUST_TABLE = "ads.corp_cust_dep_bal_d"
AFTER_CLARIFY = "反问之后"


# ── 题库结构 ──────────────────────────────────────────────

def test_forty_three_questions_in_the_planned_categories():
    assert len(QUESTIONS) == 43
    assert Counter(question.category for question in QUESTIONS) == CATEGORY_COUNTS


def test_turns_and_expectation_follow_the_category():
    for question in QUESTIONS:
        assert len(question.turns) == (2 if question.category in ("追问", AFTER_CLARIFY) else 1), question.id
        if question.category != AFTER_CLARIFY:
            assert question.expect == ("clarify" if question.category == "反问" else "sql"), question.id
        # 只有「反问之后」的题要求第一轮停在反问上；追问题的第一轮不计分
        assert question.first_expect == ("clarify" if question.category == AFTER_CLARIFY else None), question.id


def test_questions_after_a_clarify_cover_a_new_question_and_real_answers():
    after = {question.id: question.expect for question in QUESTIONS if question.category == AFTER_CLARIFY}

    # 新问题照常反问（N01），真正的回答不再反问、给出 SQL（N02 回答 ④ 的口径反问，N03 回答 ① 的反问）
    assert after == {"N01": "clarify", "N02": "sql", "N03": "sql"}
    first_turns = {question.turns[0] for question in QUESTIONS if question.id in ("N01", "N02")}
    assert first_turns == {next(item for item in QUESTIONS if item.id == "A01").turns[0]}


def test_clarify_questions_carry_no_gold_answer():
    for question in QUESTIONS:
        if question.expect == "clarify":
            assert question.gold_tables == [] and question.gold_columns == [], question.id
            assert question.alternatives == [], question.id


def test_only_top_n_questions_require_row_order():
    ordered = [question for question in QUESTIONS if question.ordered]

    assert [question.id for question in ordered] == ["D03", "F04"]
    assert all("ORDER BY" in question.gold_sql for question in ordered)


# ── 标准 SQL ──────────────────────────────────────────────

@pytest.mark.parametrize("question", SQL_QUESTIONS, ids=lambda question: question.id)
def test_gold_sql_runs_and_returns_data(question, mock_db):
    result = DuckDBExecutor(mock_db).execute(question.gold_sql, max_rows=None)

    assert result.executed, result.error
    assert result.row_count >= 1
    assert all(cell is not None for row in result.rows for cell in row)


@pytest.mark.parametrize("question", SQL_QUESTIONS, ids=lambda question: question.id)
def test_gold_tables_and_columns_agree_with_sql_and_metadata(question):
    tree = sqlglot.parse_one(question.gold_sql, read="postgres")
    tables_in_sql = {f"{table.db}.{table.name}" for table in tree.find_all(exp.Table)}

    assert set(question.gold_tables) == tables_in_sql
    assert question.gold_columns, "每道题至少要有一个关键字段"
    for item in question.gold_columns:
        table_id, _, column = item.rpartition(".")
        assert table_id in question.gold_tables, item
        assert (table_id, column) in KNOWN_COLUMNS, item
        assert column in question.gold_sql, item


@pytest.mark.parametrize("question", SQL_QUESTIONS, ids=lambda question: question.id)
def test_gold_sql_passes_the_validator_without_errors(question, mock_validator):
    report = mock_validator.validate(question.gold_sql)

    assert report.errors == [], [(issue.rule, issue.message) for issue in report.errors]
    assert set(report.tables) == set(question.gold_tables)


# ── 题目定义的校验 ────────────────────────────────────────

def test_sql_question_requires_gold_sql():
    with pytest.raises(ValueError, match="必须有标准 SQL"):
        EvalQuestion(id="X01", category="口径区分", turns=["查余额"], expect="sql")


def test_clarify_question_must_not_have_gold_sql():
    with pytest.raises(ValueError, match="不应有标准 SQL"):
        EvalQuestion(id="X02", category="反问", turns=["查余额"], expect="clarify",
                     gold_sql="SELECT 1")


def test_clarify_question_must_not_have_alternatives():
    with pytest.raises(ValueError, match="不应有其它写法"):
        EvalQuestion(id="X03", category="反问", turns=["查余额"], expect="clarify",
                     alternatives=[Alternative(sql="SELECT 1")])


def test_first_turn_expectation_needs_two_turns():
    with pytest.raises(ValueError, match="只有两轮的题"):
        EvalQuestion(id="X05", category="反问之后", turns=["查存款余额"], expect="clarify", first_expect="clarify")
    question = EvalQuestion(id="X06", category="反问之后", turns=["查存款余额", "含保证金的"], expect="sql",
                            gold_sql="SELECT 1", first_expect="clarify")
    assert question.first_expect == "clarify"
    assert EvalQuestion(id="X07", category="追问", turns=["甲", "乙"], expect="sql", gold_sql="SELECT 1").first_expect is None


def test_alternative_needs_sql_and_columns_default_to_empty():
    with pytest.raises(ValueError, match="不能为空"):
        Alternative(sql="  \n")
    assert Alternative(sql="SELECT 1").columns == []


def test_misspelled_field_is_rejected_instead_of_silently_ignored():
    with pytest.raises(ValueError):
        EvalQuestion(id="X04", category="口径区分", turns=["查余额"], expect="sql", gold_sql="SELECT 1",
                     alternative=[{"sql": "SELECT 2"}])
    with pytest.raises(ValueError):
        Alternative(sql="SELECT 1", column=["a.b.c"])


def test_duplicate_ids_are_rejected(tmp_path):
    path = tmp_path / "questions.yaml"
    path.write_text(
        '- {id: A01, category: 反问, turns: ["甲"], expect: clarify}\n'
        '- {id: A01, category: 反问, turns: ["乙"], expect: clarify}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="题目编号重复"):
        load_questions(path)


@pytest.mark.parametrize("content, message", [
    ("", "空的"),
    ("# 只有注释\n", "空的"),
    ("id: A01\ncategory: 反问\n", "必须是题目列表"),
    ("just text\n", "必须是题目列表"),
    ('- {id: A01, category: 反问, turns: ["甲"], expect: clarify}\n- 反问\n', "第 \\[2\\] 条不是一道题"),
])
def test_unusable_question_file_raises_value_error(tmp_path, content, message):
    path = tmp_path / "questions.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_questions(path)


def test_alternatives_are_read_from_the_file(tmp_path):
    path = tmp_path / "questions.yaml"
    path.write_text(
        "- id: X01\n  category: 口径区分\n  turns: [\"甲\"]\n  expect: sql\n  gold_sql: SELECT 1\n"
        "  alternatives:\n    - {sql: SELECT 2, columns: [a.b.c]}\n    - {sql: SELECT 3}\n", encoding="utf-8")

    alternatives = load_questions(path)[0].alternatives

    assert [item.sql for item in alternatives] == ["SELECT 2", "SELECT 3"]
    assert alternatives[0].columns == ["a.b.c"] and alternatives[1].columns == []


# ── 其它正确写法 ──────────────────────────────────────────

def test_about_ten_questions_have_alternatives_and_all_of_them_are_org_summary_questions():
    with_alternatives = [question for question in QUESTIONS if question.alternatives]

    assert len(with_alternatives) >= 10
    assert all(ORG_TABLE in question.gold_tables for question in with_alternatives)


def test_every_question_on_the_org_summary_table_has_a_customer_summary_alternative(mock_validator):
    # 机构汇总表能答的题，客户汇总表按机构 GROUP BY 也能答：逐题检查，凡是用机构汇总表的都要登记
    for question in SQL_QUESTIONS:
        if ORG_TABLE in question.gold_tables:
            tables = [set(mock_validator.validate(item.sql).tables) for item in question.alternatives]
            assert any(CUST_TABLE in used and ORG_TABLE not in used for used in tables), question.id


@pytest.mark.parametrize("question, index", ALTERNATIVES,
                         ids=[f"{question.id}-{index + 1}" for question, index in ALTERNATIVES])
def test_alternative_runs_and_gets_the_same_result_as_the_gold_sql(question, index, mock_db):
    executor = DuckDBExecutor(mock_db)

    gold = executor.execute(question.gold_sql, max_rows=None)
    other = executor.execute(question.alternatives[index].sql, max_rows=None)

    assert gold.executed and other.executed, (gold.error, other.error)
    assert compare_results(gold.rows, other.rows, ordered=question.ordered)


@pytest.mark.parametrize("question, index", ALTERNATIVES,
                         ids=[f"{question.id}-{index + 1}" for question, index in ALTERNATIVES])
def test_alternative_passes_the_validator_without_errors(question, index, mock_validator):
    report = mock_validator.validate(question.alternatives[index].sql)

    assert report.errors == [], [(issue.rule, issue.message) for issue in report.errors]
    assert report.tables


@pytest.mark.parametrize("question, index", ALTERNATIVES,
                         ids=[f"{question.id}-{index + 1}" for question, index in ALTERNATIVES])
def test_alternative_is_a_different_answer_with_real_key_columns(question, index, mock_validator):
    alternative = question.alternatives[index]
    report = mock_validator.validate(alternative.sql)
    columns = alternative.columns or question.gold_columns

    assert columns
    for item in columns:
        table_id, _, column = item.rpartition(".")
        assert (table_id, column) in KNOWN_COLUMNS, item
        assert item in report.columns, f"{item} 没有出现在这种写法里"
    # 用的表和关键字段都和标准答案一样的，不必登记
    assert (set(report.tables), set(columns)) != (set(question.gold_tables), set(question.gold_columns))


# ── K06：条件在数据里要真的起作用 ─────────────────────────

def test_k06_condition_filters_out_customers(mock_db):
    question = next(item for item in QUESTIONS if item.id == "K06")
    executor = DuckDBExecutor(mock_db)

    gold = executor.execute(question.gold_sql, max_rows=None).rows[0][0]
    customers = executor.execute("SELECT COUNT(*) FROM dim.corp_cust", max_rows=None).rows[0][0]

    assert "100 万" in question.turns[0] and "1000000" in question.gold_sql
    assert 0 < gold < customers                  # 以前标准答案恰好等于客户维表的行数


@pytest.mark.parametrize("sql", [
    # 先去重再数，和 COUNT(DISTINCT) 是同一个口径
    "SELECT COUNT(*) FROM (SELECT DISTINCT cust_no FROM ads.corp_cust_dep_bal_d "
    "WHERE data_dt = DATE '2026-09-30' AND dep_bal_tot_rmb > 1000000) t",
    "SELECT COUNT(*) FROM (SELECT cust_no FROM ads.corp_cust_dep_bal_d "
    "WHERE data_dt = DATE '2026-09-30' AND dep_bal_tot_rmb > 1000000 GROUP BY cust_no) t",
])
def test_k06_equivalent_spellings_are_accepted(sql, mock_db):
    question = next(item for item in QUESTIONS if item.id == "K06")
    executor = DuckDBExecutor(mock_db)

    gold = executor.execute(question.gold_sql, max_rows=None)
    other = executor.execute(sql, max_rows=None)

    assert other.executed, other.error
    assert compare_results(gold.rows, other.rows)


# ── D03、F04：前 N 名的名次不能并列 ───────────────────────

@pytest.mark.parametrize("question", [item for item in QUESTIONS if item.ordered], ids=lambda item: item.id)
def test_top_n_cut_off_has_no_tie(question, mock_db):
    # 第 N 名和第 N+1 名并列时，取哪一个是随机的，要求顺序的比对会在正确答案之间随机判错
    limit = int(re.search(r"LIMIT\s+(\d+)", question.gold_sql).group(1))
    sort_column = re.search(r"ORDER BY\s+(\w+)", question.gold_sql).group(1)
    wider = re.sub(r"LIMIT\s+\d+", f"LIMIT {limit + 1}", question.gold_sql)

    result = DuckDBExecutor(mock_db).execute(wider, max_rows=None)

    assert result.executed, result.error
    values = [row[result.columns.index(sort_column)] for row in result.rows]
    assert len(values) == limit + 1
    assert values[limit - 1] != values[limit], f"第 {limit} 名和第 {limit + 1} 名并列: {values[limit - 1]}"
    assert all(a > b for a, b in zip(values, values[1:])), "前 N+1 名里有并列，行序不确定"


# ── 埋点的区分度：看上去合理的错误写法，结果必须与标准答案不同 ──

PLAUSIBLE_WRONG_ANSWERS = {
    # 错用含保证金的字段
    "K01": "SELECT SUM(dep_bal_tot_rmb) FROM ads.org_corp_dep_bal_d WHERE data_dt = DATE '2026-09-30'",
    # 把各机构的客户数直接相加，跨机构开户的客户被重复计数
    "K06": "SELECT SUM(dep_cust_cnt) FROM ads.org_corp_dep_bal_d WHERE data_dt = DATE '2026-09-30'",
    # 去掉金额条件：以为「大于 100 万」不重要
    "K06-no-amount": "SELECT COUNT(DISTINCT cust_no) FROM ads.corp_cust_dep_bal_d "
                     "WHERE data_dt = DATE '2026-09-30'",
    # 金额条件用了不含保证金的字段
    "K06-non-margin": "SELECT COUNT(DISTINCT cust_no) FROM ads.corp_cust_dep_bal_d "
                      "WHERE data_dt = DATE '2026-09-30' AND dep_bal_rmb > 1000000",
    # 不按客户去重：跨机构开户的客户被数了好几次
    "K06-count-star": "SELECT COUNT(*) FROM ads.corp_cust_dep_bal_d "
                      "WHERE data_dt = DATE '2026-09-30' AND dep_bal_tot_rmb > 1000000",
    # 先按客户汇总各机构的余额再比：题面写明了逐条记录比
    "K06-per-customer-total": "SELECT COUNT(*) FROM (SELECT cust_no FROM ads.corp_cust_dep_bal_d "
                              "WHERE data_dt = DATE '2026-09-30' GROUP BY cust_no "
                              "HAVING SUM(dep_bal_tot_rmb) > 1000000) t",
    # 数客户维表的行数：以前 K06 的标准答案恰好等于它
    "K06-all-customers": "SELECT COUNT(*) FROM dim.corp_cust",
    # 把时点余额当成年日均
    "T03": "SELECT org_no, dep_bal_rmb FROM ads.org_corp_dep_bal_d WHERE data_dt = DATE '2026-09-30'",
    # 上溯到整合层但没有还原「剔除无效客户」这个加工条件
    "U01": "SELECT ccy_cd, SUM(acct_bal) FROM dwd.corp_dep_acct_d "
           "WHERE data_dt = DATE '2026-09-30' AND is_margin = '0' GROUP BY ccy_cd",
    "U02": "SELECT prod_cd, SUM(bal_rmb) FROM dwd.corp_loan_duebill_d "
           "WHERE data_dt = DATE '2026-09-30' GROUP BY prod_cd",
    "U03": "SELECT five_cls_cd, SUM(bal_rmb) FROM dwd.corp_loan_duebill_d "
           "WHERE data_dt = DATE '2026-09-30' GROUP BY five_cls_cd",
    # 上溯时忘了「不含保证金」的条件
    "U01-margin": "SELECT a.ccy_cd, SUM(a.acct_bal) FROM dwd.corp_dep_acct_d a "
                  "JOIN dim.corp_cust c ON a.cust_no = c.cust_no "
                  "WHERE a.data_dt = DATE '2026-09-30' GROUP BY a.ccy_cd",
    # 关联：不用机构维表，改用贴源层机构信息表的上级机构名归并。贴源层的上级机构名只是「上一级」，
    # 开在二级分行本部的机构，上一级是一级分行，不是它自己所属的二级分行
    "D05": "SELECT p.org_nm, SUM(l.loan_bal_rmb) FROM ads.corp_cust_loan_bal_d l "
           "JOIN ods.org_info o ON l.org_no = o.org_no JOIN ods.org_info p ON o.up_org_no = p.org_no "
           "WHERE l.data_dt = DATE '2026-09-30' GROUP BY p.org_nm",
    # 关联：层级字段选错，二级分行写成一级分行
    "D05-level": "SELECT o.lvl1_org_nm, SUM(l.loan_bal_rmb) FROM ads.corp_cust_loan_bal_d l "
                 "JOIN dim.org o ON l.org_no = o.org_no WHERE l.data_dt = DATE '2026-09-30' GROUP BY o.lvl1_org_nm",
    # 较年初：拿 2026-01-01 当年初，应该是上年末 2025-12-31
    "T02": "SELECT SUM(CASE WHEN data_dt = DATE '2026-09-30' THEN dep_bal_rmb ELSE 0 END) "
           "- SUM(CASE WHEN data_dt = DATE '2026-01-01' THEN dep_bal_rmb ELSE 0 END) "
           "FROM ads.org_corp_dep_bal_d WHERE data_dt IN (DATE '2026-09-30', DATE '2026-01-01')",
    # 日均：对 9 月每天的行直接求平均，是各行的平均值，不是每天合计后的日均
    "T04": "SELECT AVG(dep_bal_rmb) FROM ads.corp_cust_dep_bal_d "
           "WHERE data_dt BETWEEN DATE '2026-09-01' AND DATE '2026-09-30'",
    # 反问之后：没按回答的口径，用了反问的第一个选项（不含保证金）
    "N02": "SELECT org_no, dep_bal_rmb FROM ads.org_corp_dep_bal_d WHERE data_dt = DATE '2026-09-30'",
    # 反问之后：没用回答的客户号，算成了全行合计
    "N03": "SELECT SUM(loan_bal_rmb) FROM ads.corp_cust_loan_bal_d WHERE data_dt = DATE '2026-09-30'",
    # 反问之后：按反问的第一个选项列出了全部客户
    "N03-all-customers": "SELECT cust_no, loan_bal_rmb FROM ads.corp_cust_loan_bal_d WHERE data_dt = DATE '2026-09-30'",
}


@pytest.mark.parametrize("key", PLAUSIBLE_WRONG_ANSWERS)
def test_plausible_wrong_answer_is_told_apart(key, mock_db):
    question = next(item for item in QUESTIONS if item.id == key.split("-")[0])
    executor = DuckDBExecutor(mock_db)

    gold = executor.execute(question.gold_sql, max_rows=None)
    wrong = executor.execute(PLAUSIBLE_WRONG_ANSWERS[key], max_rows=None)

    assert gold.executed and wrong.executed, (gold.error, wrong.error)
    assert not compare_results(gold.rows, wrong.rows, ordered=question.ordered)
