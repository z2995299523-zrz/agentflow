"""⑤ 生成 SQL：从回复里取出 SQL、带修正信息的生成"""
import pytest

from nl2sql.generator import extract_sql, generate_sql
from nl2sql.llm import ScriptedLLM, StepError
from tests.step_fixtures import DIM_ORG, ORG_BAL, make_evidence, make_parsed, make_plan

GOOD_SQL = """SELECT o.lvl1_org_nm AS org_name,
       SUM(b.dep_bal_rmb) AS dep_bal
FROM ads.org_corp_dep_bal_d b
JOIN dim.org o ON b.org_no = o.org_no
WHERE b.data_dt = DATE '2026-09-30'
GROUP BY o.lvl1_org_nm"""


# ── extract_sql ───────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("```sql\nSELECT 1\n```", "SELECT 1"),                                                  # 标准写法
    ("```SQL\nSELECT 1;\n```", "SELECT 1"),                                                 # 大小写不限，去末尾分号
    ("好的，SQL 如下：\n```\nSELECT 1\n```\n以上。", "SELECT 1"),                              # 没有语言标记
    ("```text\n不是 SQL\n```\n```sql\nSELECT 2\n```\n```sql\nSELECT 3\n```", "SELECT 2"),     # 第一个 sql 块
    ("```\nSELECT 1\n```\n```sql\nSELECT 2\n```", "SELECT 2"),                               # sql 块优先于无标记块
    ("```\nSELECT 1\n```\n```\nSELECT 2\n```", "SELECT 1"),                                   # 多个无标记块取第一个
    ("SELECT 1;", "SELECT 1"),                                                              # 没有代码块取全文
    ("  \n SELECT a\nFROM t ;; \n", "SELECT a\nFROM t"),                                    # 去首尾空白和多个分号
    ("```sql\nSELECT 1", "SELECT 1"),                                                       # 输出被截断，没有结束围栏
    ("```postgresql\nSELECT 1\n```", "SELECT 1"),                                           # 写成 postgresql 的 sql 块
    ("```sql\nSELECT a\n  FROM t\n WHERE x = 1;\n```", "SELECT a\n  FROM t\n WHERE x = 1"),   # 保留内部的换行和缩进
])
def test_extract_sql(text, expected):
    assert extract_sql(text) == expected


def test_extract_sql_of_an_empty_reply_is_empty():
    assert extract_sql("   ") == "" and extract_sql("```sql\n```") == ""


def test_extract_sql_keeps_semicolons_inside_the_statement():
    assert extract_sql("```sql\nSELECT ';' AS sep;\n```") == "SELECT ';' AS sep"


# ── generate_sql ──────────────────────────────

@pytest.fixture(scope="module")
def evidence(mock_store):
    return make_evidence(mock_store)


def test_generate_sql_extracts_the_sql_and_records_one_call(evidence):
    llm = ScriptedLLM([f"```sql\n{GOOD_SQL};\n```"])

    sql, record = generate_sql(llm, make_parsed(), make_plan(), evidence)

    assert sql == GOOD_SQL
    assert (record.step, record.attempt, record.ok) == ("generate", 1, True)
    assert record.reply == f"```sql\n{GOOD_SQL};\n```"
    assert llm.calls[0]["json_mode"] is False
    system, user = llm.calls[0]["messages"]
    assert system["role"] == "system" and user["role"] == "user"
    assert make_parsed().requirement in user["content"]


def test_generate_sql_lists_only_the_tables_of_the_plan(evidence):
    llm = ScriptedLLM(["```sql\nSELECT 1\n```"])

    generate_sql(llm, make_parsed(), make_plan(), evidence)

    user = llm.calls[0]["messages"][1]["content"]
    assert ORG_BAL in user and DIM_ORG in user
    assert "dep_cust_cnt" in user and "lvl2_org_nm" in user         # 方案里的两张表，字段列全（包括方案没用到的）
    assert "mtd_avg_bal_rmb" not in user and "cur_dep_bal_rmb" not in user      # 客户汇总表不在方案里，它独有的字段不出现


def test_generate_sql_sends_the_previous_sql_and_the_errors_when_repairing(evidence):
    llm = ScriptedLLM(["```sql\nSELECT 2\n```"])

    sql, record = generate_sql(llm, make_parsed(), make_plan(), evidence, previous_sql="SELECT 1 FROM x",
                               errors=["R5：快照表 ads.org_corp_dep_bal_d 没有数据日期条件"])

    assert sql == "SELECT 2" and record.attempt == 1
    user = llm.calls[0]["messages"][1]["content"]
    assert "SELECT 1 FROM x" in user and "R5：快照表 ads.org_corp_dep_bal_d 没有数据日期条件" in user


def test_generate_sql_wraps_model_errors_in_step_errors(evidence):
    llm = ScriptedLLM([TimeoutError("连接超时")])

    with pytest.raises(StepError) as caught:
        generate_sql(llm, make_parsed(), make_plan(), evidence)

    assert caught.value.step == "generate" and caught.value.message.startswith("模型调用失败：")
    assert [r.ok for r in caught.value.calls] == [False]


def test_generate_sql_does_not_fail_on_a_reply_without_sql(evidence):
    # 回复里没有 SQL 不是调用失败：交给静态校验去报，进入修正回路
    sql, record = generate_sql(ScriptedLLM(["```sql\n```"]), make_parsed(), make_plan(), evidence)

    assert sql == "" and record.ok


def test_a_generated_sql_goes_through_the_validator(evidence, mock_validator):
    llm = ScriptedLLM([f"```sql\n{GOOD_SQL};\n```"])

    sql, _ = generate_sql(llm, make_parsed(), make_plan(), evidence)
    report = mock_validator.validate(sql, expected_tables=[ORG_BAL, DIM_ORG],
                                     expected_columns=[f"{ORG_BAL}.dep_bal_rmb", f"{DIM_ORG}.lvl1_org_nm"])

    assert report.ok and not report.issues
