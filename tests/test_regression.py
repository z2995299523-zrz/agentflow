"""只使用入库题库和临时资料验证零成本回归，不读现有运行报告。"""
import json
import os
from pathlib import Path

import pytest
import yaml

from eval import regression
from eval.questions import EvalQuestion
from eval.regression import GateSources, find_sources, read_questions, replay_recall, run_regression, session_impact, static_check
from metadata import versions
from metadata.models import Mart
from metadata.store import MetadataStore
from nl2sql.history import ConversationStore
from nl2sql.schemas import ParsedRequest, QueryResult, ValidationSummary
from tests.helpers import write_mart


def write_questions(path: Path, groups: list[list[str]] | None = None) -> Path:
    """写入虚构的静态格式题库。"""
    path.write_text(yaml.safe_dump([{"id": "B03", "turns": ["客户余额"], "today": "2026-10-23",
                                    "gold_table_sets": groups or [["ads.cust_bal"]]}]), encoding="utf-8")
    return path


def write_record(path: Path) -> Path:
    """写入最小解析结果，后面的空解析不覆盖最后一次成功解析。"""
    parsed = ParsedRequest(is_query=True, requirement="客户余额", metrics=[{"name": "余额"}])
    record = {"question_id": "B03", "results": [{"parsed": parsed.model_dump()}, {"parsed": None}]}
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def test_read_two_formats_and_mock_static_consistency(mock_store, tmp_path):
    mock_path = Path(__file__).resolve().parents[1] / "eval" / "nl2sql_questions.yaml"
    questions = read_questions(mock_path)
    checked = static_check(mock_store, questions)
    assert len(questions) == 43 and len(checked) == 37
    assert all(not issues for issues in checked.values()), checked
    static = read_questions(write_questions(tmp_path / "questions.yaml"))
    assert static[0].gold_table_sets == [["ads.cust_bal"]]


def test_static_deleted_table_and_alternative_group(tiny_store, tmp_path):
    questions = read_questions(write_questions(tmp_path / "questions.yaml"))
    assert static_check(tiny_store, questions) == {"B03": []}
    assert static_check(MetadataStore.from_mart(Mart()), questions) == {"B03": ["R3 表 ads.cust_bal 不存在"]}
    alternatives = read_questions(write_questions(tmp_path / "questions.yaml", [["ads.absent"], ["ods.cust"]]))
    assert static_check(tiny_store, alternatives) == {"B03": []}


def test_mock_format_sql_alternatives_and_missing_key_column(tiny_store):
    question = EvalQuestion(id="Q", category="测试", turns=["客户"], expect="sql", gold_sql="SELECT cust_no FROM ods.cust",
                            gold_tables=["ods.cust"], gold_columns=["ods.cust.cust_no"],
                            alternatives=[{"sql": "SELECT absent FROM ods.cust"}])
    assert any("R4" in e and "absent" in e for e in static_check(tiny_store, [question])["Q"])
    question.alternatives = []
    question.gold_columns = ["ods.cust.gone"]
    assert static_check(tiny_store, [question]) == {"Q": ["R4 字段 ods.cust.gone 不存在"]}
    clarify = EvalQuestion(id="C", category="反问", turns=["查数"], expect="clarify")
    assert static_check(tiny_store, [clarify]) == {}


def test_find_sources_for_mock_without_existing_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(regression, "ROOT", tmp_path)
    questions = tmp_path / "eval" / "nl2sql_questions.yaml"
    questions.parent.mkdir()
    questions.write_text("[]", encoding="utf-8")
    for name in ("20261020", "20261022", "20261023"):
        directory = tmp_path / "eval" / "reports" / "runs" / name
        directory.mkdir(parents=True)
        if name != "20261023":
            (directory / "C.jsonl").write_text("", encoding="utf-8")
    sources = find_sources(tmp_path / "data" / "mock_mart" / ".")
    assert sources.questions == questions and sources.runs.parent.name == "20261022"
    sources.runs.unlink()
    (tmp_path / "eval" / "reports" / "runs" / "20261020" / "C.jsonl").unlink()
    assert find_sources(tmp_path / "data" / "mock_mart").runs is None


def test_find_sources_custom_latest_modification(tmp_path):
    assert find_sources(tmp_path) == GateSources()
    questions = write_questions(tmp_path / "eval_questions.yaml")
    directory = tmp_path / "eval"
    directory.mkdir()
    for name, stamp in (("z_static_eval.jsonl", 100), ("a_static_eval.jsonl", 200), ("ignore.jsonl", 300)):
        path = directory / name
        path.write_text("", encoding="utf-8")
        os.utime(path, (stamp, stamp))
    assert find_sources(tmp_path) == GateSources(questions, directory / "a_static_eval.jsonl")


def test_replay_two_imported_versions_detects_drop(tiny_mart, tiny_sheets, tmp_path):
    first = versions.import_version(tiny_mart, build_index=False)
    tiny_sheets["表清单"] = [row for row in tiny_sheets["表清单"] if row[1:3] != ["ads", "cust_bal"]]
    tiny_sheets["字段清单"] = [row for row in tiny_sheets["字段清单"] if row[0] != "ads.cust_bal"]
    tiny_sheets["血缘清单"].clear()
    tiny_sheets["关联关系"].clear()
    write_mart(tiny_mart, tiny_sheets)
    second = versions.import_version(tiny_mart, build_index=False)
    sources = GateSources(write_questions(tmp_path / "questions.yaml"), write_record(tmp_path / "records.jsonl"))
    old, new = MetadataStore(first.report.db_path), MetadataStore(second.report.db_path)
    outcomes = replay_recall(old, sources.runs, read_questions(sources.questions))
    assert outcomes["B03"].recall_ok and outcomes["B03"].best_rank is not None
    assert "ads.cust_bal" in outcomes["B03"].candidates
    result = run_regression(tiny_mart, old, new, removed_tables={"ads.cust_bal"}, replay=True, sources=sources)
    assert "召回重放：B03 的标准用表掉出了候选" in result.confirm_items
    assert "题库 B03 在这一版上核对不过：R3 表 ads.cust_bal 不存在" in result.confirm_items
    assert "这一版 0/1 通过，基线 1/1" == result.gates["题库静态核对"]


def test_replay_skips_unknown_and_no_standard_records(tiny_store, tmp_path):
    record = tmp_path / "records.jsonl"
    record.write_text('\n{"question_id":"unknown"}\n{"question_id":"C"}\n'
                      '{"question_id":"B03","results":[{"parsed":null}]}\n', encoding="utf-8")
    questions = read_questions(write_questions(tmp_path / "questions.yaml"))
    questions.append(EvalQuestion(id="C", category="反问", turns=["查数"], expect="clarify"))
    assert replay_recall(tiny_store, record, questions) == {}


def test_replay_disabled_does_not_resolve_embedder(tiny_store, tmp_path, monkeypatch):
    def forbidden(store):
        raise AssertionError("不能加载向量模型")
    monkeypatch.setattr(regression, "embedder_for", forbidden)
    result = run_regression(tmp_path, tiny_store, tiny_store, removed_tables=set(), replay=False,
                            sources=GateSources(write_questions(tmp_path / "questions.yaml"), write_record(tmp_path / "records.jsonl")))
    assert "向量模型没加载，要跑加 --replay" in result.gates["召回重放"]
    assert result.confirm_items == []


@pytest.mark.parametrize("questions,runs,reason", [(None, None, "没有题库"), (True, None, "没有逐题记录")])
def test_missing_sources_are_explicit(tiny_store, tmp_path, questions, runs, reason):
    path = write_questions(tmp_path / "q.yaml") if questions else None
    result = run_regression(tmp_path, tiny_store, tiny_store, removed_tables=set(), replay=True, sources=GateSources(path, runs))
    assert reason in result.gates["召回重放"] and result.confirm_items == []


def test_session_impact_counts_conversations_not_turns(tmp_path):
    path = tmp_path / "build" / "history.db"
    assert session_impact(path, {"ads.cust_bal"}) == 0 and not path.exists()
    history = ConversationStore(path)
    conversation = history.create()
    result = QueryResult(status="ok", question="查余额", requirement="客户余额", sql="SELECT bal FROM ads.cust_bal",
                         validation=ValidationSummary(ok=True, tables=["ads.cust_bal", "ods.cust"]))
    for _ in range(2):
        history.add_turn(conversation.id, "查余额", result)
    history.create()
    assert session_impact(path, {"ads.cust_bal", "ods.cust"}) == 1
    assert session_impact(path, {"ads.other"}) == 0


@pytest.mark.parametrize("broken", ["questions", "runs", "history"])
def test_regression_errors_become_pending_and_other_gates_continue(tiny_store, tmp_path, broken):
    questions, records = write_questions(tmp_path / "q.yaml"), write_record(tmp_path / "r.jsonl")
    if broken == "questions":
        questions.write_text("not a list", encoding="utf-8")
    elif broken == "runs":
        records.write_text("not json", encoding="utf-8")
    else:
        (tmp_path / "build").mkdir()
        (tmp_path / "build" / "history.db").write_bytes(b"broken database")
    result = run_regression(tmp_path, tiny_store, tiny_store, removed_tables={"ads.cust_bal"}, replay=True,
                            sources=GateSources(questions, records))
    assert any(item.startswith("零成本回归没跑完：") for item in result.confirm_items)
    assert set(result.gates) == {"题库静态核对", "召回重放", "会话影响"}
    if broken == "runs":
        assert result.gates["题库静态核对"] == "这一版 1/1 通过，基线 1/1"
    if broken != "history":
        assert result.gates["会话影响"].startswith("0 个会话")
