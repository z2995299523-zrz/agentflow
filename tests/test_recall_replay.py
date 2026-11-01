"""召回重放命令的测试：按题号取 ① 的记录、没有记录的题只调一次 ①、和上一次比。不调模型、不加载 BGE。"""
import json
import os
from datetime import date, datetime
from pathlib import Path

import pytest
import yaml

from eval import recall_replay
from eval.recall_replay import ReplayRun, collect_parsed, main, parse_turns, record_files, render_run, replay
from eval.regression import read_questions
from metadata import versions
from nl2sql.llm import ScriptedLLM
from nl2sql.schemas import ParsedRequest

NOW = datetime(2026, 11, 1, 12, 0, 0)
TODAY = date(2026, 10, 23)


def parsed_reply(requirement: str = "客户余额", **fields) -> str:
    """① 的一条回复"""
    return json.dumps({"is_query": True, "requirement": requirement, "metrics": [{"name": "余额"}], **fields}, ensure_ascii=False)


def write_questions(path: Path, ids: tuple[str, ...] = ("B03",), **fields) -> Path:
    """虚构的静态格式题库，每题问的都是 ads.cust_bal"""
    items = [{"id": key, "turns": [f"{key} 的客户余额"], "today": "2026-10-23", "gold_table_sets": [["ads.cust_bal"]], **fields}
             for key in ids]
    path.write_text(yaml.safe_dump(items, allow_unicode=True), encoding="utf-8")
    return path


def write_records(path: Path, requirements: dict[str, str], stamp: int | None = None) -> Path:
    """题号 → 完整需求的逐题记录，第一轮的文字和 write_questions 写的一样"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"question_id": key, "results": [
        {"question": f"{key} 的客户余额", "parsed": json.loads(parsed_reply(requirement))}]}, ensure_ascii=False)
        for key, requirement in requirements.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if stamp is not None:
        os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def imported(tiny_mart: Path) -> Path:
    """导入过的最小资料，还没有向量，召回只走关键字路"""
    versions.import_version(tiny_mart, build_index=False)
    return tiny_mart


def test_collect_parsed_takes_each_question_from_the_newest_file_that_has_it(tmp_path):
    questions = read_questions(write_questions(tmp_path / "q.yaml", ("B03", "B04")))
    newer = write_records(tmp_path / "newer.jsonl", {"B03": "新的"})
    older = write_records(tmp_path / "older.jsonl", {"B03": "旧的", "B04": "只在旧文件里"})
    found = collect_parsed([newer, older], questions)
    assert {key: parsed.requirement for key, parsed in found.items()} == {"B03": "新的", "B04": "只在旧文件里"}


def test_collect_parsed_skips_records_of_a_changed_question_and_stops_when_complete(tmp_path):
    questions = read_questions(write_questions(tmp_path / "q.yaml"))
    changed = tmp_path / "changed.jsonl"
    changed.write_text(json.dumps({"question_id": "B03", "results": [
        {"question": "题目改过了", "parsed": json.loads(parsed_reply("旧题目"))}]}, ensure_ascii=False) + "\n", encoding="utf-8")
    matching = write_records(tmp_path / "matching.jsonl", {"B03": "对的"})
    never_read = tmp_path / "does_not_exist.jsonl"
    assert collect_parsed([changed, matching, never_read], questions)["B03"].requirement == "对的"


def test_record_files_are_newest_first_and_include_the_parsed_cache(tmp_path):
    write_records(tmp_path / "eval" / "old.jsonl", {}, 100)
    write_records(tmp_path / "eval" / "new.jsonl", {}, 300)
    write_records(tmp_path / "build" / "parsed_records.jsonl", {}, 200)
    assert [path.name for path in record_files(tmp_path)] == ["new.jsonl", "parsed_records.jsonl", "old.jsonl"]


def test_render_compares_the_questions_of_both_runs_and_lists_the_worse_ones():
    baseline = ReplayRun("q.yaml", "v1", {"A": 1, "B": 5, "C": 3, "D": 9, "gone": 1})
    run = ReplayRun("q.yaml", "v2", {"A": 1, "B": 2, "C": None, "D": 10, "new": 1})
    lines = render_run(run, baseline)
    assert "两次的题不完全一样，只比共同的 4 题" in lines
    assert "进前 4：2 → 2（共 4 题）" in lines
    assert "进前 8：3 → 2（共 4 题）" in lines
    assert "进候选：4 → 3（共 4 题）" in lines
    assert "名次变好 1 题，变差 2 题" in lines
    assert "变差的题：C 3→不在候选、D 9→10" in lines


def test_replay_reports_missing_questions_saves_the_run_and_compares_with_it_next_time(imported, tmp_path):
    questions = write_questions(tmp_path / "q.yaml", ("B03", "B04"))
    write_records(imported / "eval" / "run.jsonl", {"B03": "客户余额"})

    lines, saved = replay(questions, imported, now=NOW)
    assert any(line.startswith("1 题没有 ① 的记录，没算进去：B04") for line in lines)
    assert "进前 4：1/1" in lines
    assert any("只走了关键字路" in line for line in lines)
    assert saved.parent == imported / "build" / "replay" and saved.name == "q-20261101-120000.json"

    saved.write_text(json.dumps({**json.loads(saved.read_text(encoding="utf-8")), "ranks": {"B03": 9}}), encoding="utf-8")
    lines, _ = replay(questions, imported, now=NOW.replace(hour=13))
    assert "进前 4：0 → 1（共 1 题）" in lines and "名次变好 1 题，变差 0 题" in lines

    lines, _ = replay(questions, imported, baseline=saved, now=NOW.replace(hour=14))      # 指定比哪一次
    assert "进前 4：0 → 1（共 1 题）" in lines


def test_parse_missing_calls_the_model_once_and_the_next_run_reuses_the_record(imported, tmp_path, monkeypatch):
    questions = write_questions(tmp_path / "q.yaml")
    llm = ScriptedLLM([parsed_reply()], name="scripted")
    lines, _ = replay(questions, imported, llm=llm, now=NOW)
    assert len(llm.calls) == 1 and "进前 4：1/1" in lines
    record = json.loads((imported / "build" / "parsed_records.jsonl").read_text(encoding="utf-8"))
    assert record["question_id"] == "B03" and record["model"] == "scripted"
    assert ParsedRequest.model_validate(record["results"][0]["parsed"]).requirement == "客户余额"

    lines, _ = replay(questions, imported, llm=ScriptedLLM([]), now=NOW.replace(hour=13))     # 回复用完了再调用会报错
    assert "进前 4：1 → 1（共 1 题）" in lines


def test_a_failed_parse_skips_the_question_and_writes_nothing(imported, tmp_path, capsys):
    questions = write_questions(tmp_path / "q.yaml")
    lines, saved = replay(questions, imported, llm=ScriptedLLM([ValueError("余额不足")]), now=NOW)
    assert "B03 解析失败" in capsys.readouterr().out
    assert saved is None and any("没有能重放的题" in line for line in lines)
    assert (imported / "build" / "parsed_records.jsonl").read_text(encoding="utf-8") == ""


def test_parse_turns_carries_the_previous_turn_and_answers_a_clarify(tmp_path):
    question = read_questions(write_questions(tmp_path / "q.yaml", clarify_answer="按最常用的口径"))[0]
    question.turns = ["第一轮", "第二轮"]
    clarify = {"question": "看哪个口径？", "options": [{"label": "最常用"}, {"label": "我来指定"}]}
    llm = ScriptedLLM([parsed_reply("第一轮需求"), parsed_reply("第二轮需求", follows_up=False, clarify=clarify),
                       parsed_reply("第三轮需求", answers_clarify=True)])
    turns = parse_turns(llm, question, TODAY)
    assert [text for text, _ in turns] == ["第一轮", "第二轮", "按最常用的口径"]
    assert turns[-1][1].requirement == "第三轮需求"
    assert "第一轮需求" in llm.calls[1]["messages"][-1]["content"]
    assert "看哪个口径？" in llm.calls[2]["messages"][-1]["content"]


def test_main_finds_the_mart_next_to_the_questions(imported, capsys):
    questions = write_questions(imported / "questions.yaml")
    write_records(imported / "eval" / "run.jsonl", {"B03": "客户余额"})
    assert main([str(questions)]) == 0
    output = capsys.readouterr().out
    assert "进前 4：1/1" in output and "结果：" in output


def test_main_exit_codes(imported, tmp_path, capsys):
    assert main(["--today", "昨天", "x.yaml"]) == 2
    assert main([str(tmp_path / "absent.yaml"), "--mart", str(imported)]) == 1
    questions = write_questions(tmp_path / "q.yaml")
    assert main([str(questions), "--mart", str(imported)]) == 1            # 一道能重放的题也没有
    assert "没有能重放的题" in capsys.readouterr().out
    assert main([str(questions), "--mart", str(tmp_path / "no_mart")]) == 1


def test_parse_missing_in_main_uses_the_chosen_model(imported, tmp_path, monkeypatch):
    built = []

    def client(**kwargs):
        built.append(kwargs["model"])
        return ScriptedLLM([parsed_reply()])

    monkeypatch.setattr(recall_replay, "ChatModelClient", client)
    assert main([str(write_questions(tmp_path / "q.yaml")), "--mart", str(imported), "--parse-missing", "--model", "glm-4.7-flash"]) == 0
    assert built == ["glm-4.7-flash"]
