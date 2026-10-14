"""评估题库的测试：结构合规，标准 SQL 都能在模拟库上查出结果，埋点确实有区分度"""
from collections import Counter
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from eval.compare import compare_results
from eval.questions import CATEGORY_COUNTS, EvalQuestion, load_questions
from metadata.importer import load_mart
from nl2sql.executor import DuckDBExecutor

MOCK_MART = Path(__file__).parent.parent / "data" / "mock_mart"
QUESTIONS = load_questions()
SQL_QUESTIONS = [question for question in QUESTIONS if question.expect == "sql"]
MART, _ = load_mart(MOCK_MART)
KNOWN_COLUMNS = {(column.table_id, column.column_name) for column in MART.columns}


# ── 题库结构 ──────────────────────────────────────────────

def test_forty_questions_in_the_planned_categories():
    assert len(QUESTIONS) == 40
    assert Counter(question.category for question in QUESTIONS) == CATEGORY_COUNTS


def test_turns_and_expectation_follow_the_category():
    for question in QUESTIONS:
        assert len(question.turns) == (2 if question.category == "追问" else 1), question.id
        assert question.expect == ("clarify" if question.category == "反问" else "sql"), question.id


def test_clarify_questions_carry_no_gold_answer():
    for question in QUESTIONS:
        if question.expect == "clarify":
            assert question.gold_tables == [] and question.gold_columns == [], question.id


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


# ── 题目定义的校验 ────────────────────────────────────────

def test_sql_question_requires_gold_sql():
    with pytest.raises(ValueError, match="必须有标准 SQL"):
        EvalQuestion(id="X01", category="口径区分", turns=["查余额"], expect="sql")


def test_clarify_question_must_not_have_gold_sql():
    with pytest.raises(ValueError, match="不应有标准 SQL"):
        EvalQuestion(id="X02", category="反问", turns=["查余额"], expect="clarify",
                     gold_sql="SELECT 1")


def test_duplicate_ids_are_rejected(tmp_path):
    path = tmp_path / "questions.yaml"
    path.write_text(
        '- {id: A01, category: 反问, turns: ["甲"], expect: clarify}\n'
        '- {id: A01, category: 反问, turns: ["乙"], expect: clarify}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="题目编号重复"):
        load_questions(path)


# ── 埋点的区分度：看上去合理的错误写法，结果必须与标准答案不同 ──

PLAUSIBLE_WRONG_ANSWERS = {
    # 错用含保证金的字段
    "K01": "SELECT SUM(dep_bal_tot_rmb) FROM ads.org_corp_dep_bal_d WHERE data_dt = DATE '2026-09-30'",
    # 把各机构的客户数直接相加，跨机构开户的客户被重复计数
    "K06": "SELECT SUM(dep_cust_cnt) FROM ads.org_corp_dep_bal_d WHERE data_dt = DATE '2026-09-30'",
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
}


@pytest.mark.parametrize("key", PLAUSIBLE_WRONG_ANSWERS)
def test_plausible_wrong_answer_is_told_apart(key, mock_db):
    question = next(item for item in QUESTIONS if item.id == key.split("-")[0])
    executor = DuckDBExecutor(mock_db)

    gold = executor.execute(question.gold_sql, max_rows=None)
    wrong = executor.execute(PLAUSIBLE_WRONG_ANSWERS[key], max_rows=None)

    assert gold.executed and wrong.executed, (gold.error, wrong.error)
    assert not compare_results(gold.rows, wrong.rows, ordered=question.ordered)
