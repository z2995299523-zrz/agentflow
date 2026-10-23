"""会话历史：建、列、取、改、删，记一轮时定标题和排序，上下文的取法（和原来界面的规则一致），换实例后数据还在"""
import json
import sqlite3
from itertools import count

import pytest

from nl2sql import history as history_module
from nl2sql.history import TITLE_CHARS, ConversationContext, ConversationStore
from nl2sql.schemas import CallRecord, Clarify, ClarifyOption, ParsedRequest, QueryResult, ValidationSummary

CLARIFY = Clarify(question="对公存款余额按哪个口径？", options=[ClarifyOption(label="含保证金"), ClarifyOption(label="不含保证金")])
REQUIREMENT = "查询 2026-09-30 全行对公存款余额"


def ok_result(requirement: str = REQUIREMENT) -> QueryResult:
    """生成了 SQL 的结果，调用记录里带提示词"""
    return QueryResult(status="ok", question="查存款", requirement=requirement, sql="SELECT 1",
                       calls=[CallRecord(step="parse", attempt=1, elapsed_ms=1, ok=True,
                                         messages=[{"role": "user", "content": "这是提示词"}], reply="原始回复")])


def clarify_result() -> QueryResult:
    """反问的结果"""
    return QueryResult(status="need_clarify", question="查存款", requirement=REQUIREMENT, clarify=CLARIFY)


def not_a_query() -> QueryResult:
    """不是查数需求：没有完整需求"""
    return QueryResult(status="failed", question="你好", failed_step="parse", message="我只会查数",
                       parsed=ParsedRequest(is_query=False, reply="我只会查数"))


@pytest.fixture
def store(tmp_path, monkeypatch):
    """临时目录里的历史库；时间换成一个个递增的假时间，排序不靠运气"""
    ticks = count(1)
    monkeypatch.setattr(history_module, "_now", lambda: f"2026-10-19T00:00:{next(ticks):02d}.000+00:00")
    return ConversationStore(tmp_path / "build" / "history.db")


def test_conversations_are_created_listed_renamed_and_deleted(store, tmp_path):
    first = store.create()
    second = store.create(" 九月末存款 ")

    assert store.path.exists() and store.path.parent == tmp_path / "build"       # 目录不存在时自动建
    assert first.title == "" and second.title == "九月末存款" and first.id != second.id
    assert [item.id for item in store.list_recent()] == [second.id, first.id]   # 新的在前
    assert [item.id for item in store.list_recent(limit=1)] == [second.id]
    assert store.rename(first.id, " 存款 ").title == "存款" and store.rename("nope", "x") is None
    assert store.get(first.id).title == "存款" and store.get("nope") is None

    store.add_turn(first.id, "查存款", ok_result())
    assert store.delete(first.id) is True and store.delete(first.id) is False
    assert store.get(first.id) is None and [item.id for item in store.list_recent()] == [second.id]
    assert ConversationStore(store.path).get(second.id).title == "九月末存款"   # 换一个实例，数据还在


def test_a_turn_keeps_the_public_result_names_the_conversation_and_moves_it_to_the_top(store):
    older, newer = store.create(), store.create()
    question = "  查 2026 年 9 月末\n全行对公存款余额，按一级分行汇总，再按余额从高到低排序，只看前十名的分行  "

    turn = store.add_turn(older.id, question, ok_result())

    assert turn.question == question and turn.result == ok_result().public()
    assert "这是提示词" not in json.dumps(turn.result, ensure_ascii=False)
    detail = store.get(older.id)
    assert detail.title == " ".join(question.split())[:TITLE_CHARS] and len(detail.title) == TITLE_CHARS
    assert detail.turn_count == 1 and detail.turns == [turn] and detail.updated_at == turn.created_at
    assert [item.id for item in store.list_recent()] == [older.id, newer.id]   # 刚问过的排到最前

    store.rename(older.id, "存款排名")
    store.add_turn(older.id, "换成 6 月末", ok_result())
    assert store.get(older.id).title == "存款排名"                              # 有标题以后不再改
    with pytest.raises(LookupError, match="会话不存在"):
        store.add_turn("nope", "查存款", ok_result())


def test_context_follows_the_latest_turn_that_has_a_requirement(store):
    conversation = store.create()
    assert store.context(conversation.id) == ConversationContext()

    asked = store.add_turn(conversation.id, "查存款", clarify_result())
    waiting = ConversationContext(prev_requirement=REQUIREMENT, prev_clarify=CLARIFY, clarify_turn_id=asked.id)
    assert store.context(conversation.id) == waiting

    store.add_turn(conversation.id, "你好", not_a_query())                    # 没有完整需求：上下文不动，反问还能回答
    assert store.context(conversation.id) == waiting and store.get(conversation.id).context == waiting

    answered_result = ok_result("查询 2026-09-30 全行不含保证金的对公存款余额")
    answered_result.validation = ValidationSummary(ok=True, tables=["ads.org_corp_dep_bal_d"])
    store.add_turn(conversation.id, "不含保证金", answered_result)
    answered = ConversationContext(prev_requirement="查询 2026-09-30 全行不含保证金的对公存款余额", prev_sql="SELECT 1",
                                   prev_tables=["ads.org_corp_dep_bal_d"])           # 校验通过：带上 SQL 和用到的表
    assert store.context(conversation.id) == answered and store.get(conversation.id).context == answered
    with pytest.raises(LookupError, match="会话不存在"):
        store.context("nope")


def test_a_turn_preserves_its_metadata_version(store: ConversationStore) -> None:
    """结果带的元数据版本写进历史 JSON，重新打开历史库后仍可读出。"""
    conversation = store.create()
    result = ok_result().model_copy(update={"metadata_version": "20261023-history-test"})

    turn = store.add_turn(conversation.id, "查存款", result)
    detail = ConversationStore(store.path).get(conversation.id)

    assert turn.result["metadata_version"] == result.metadata_version
    assert detail.turns[0].result["metadata_version"] == result.metadata_version


def test_old_turn_without_metadata_version_still_loads_and_provides_context(store: ConversationStore) -> None:
    """旧 JSON 缺少版本字段时按 None 解析，追问上下文保留原来的 SQL 和表。"""
    conversation = store.create()
    result = ok_result()
    result.validation = ValidationSummary(ok=True, tables=["ads.org_corp_dep_bal_d"])
    turn = store.add_turn(conversation.id, "查存款", result)
    old_result = result.public()
    old_result.pop("metadata_version")
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE turns SET result = ? WHERE id = ?", (json.dumps(old_result, ensure_ascii=False), turn.id))

    reopened = ConversationStore(store.path)
    detail = reopened.get(conversation.id)

    assert detail.turns[0].result == old_result
    assert QueryResult.model_validate(detail.turns[0].result).metadata_version is None
    expected = ConversationContext(prev_requirement=REQUIREMENT, prev_sql=result.sql,
                                   prev_tables=["ads.org_corp_dep_bal_d"])
    assert detail.context == expected and reopened.context(conversation.id) == expected


def test_next_context_carries_the_sql_and_tables_only_after_a_validated_turn():
    validated = ok_result()
    validated.validation = ValidationSummary(ok=True, tables=["ads.org_corp_dep_bal_d", "dim.org"])

    assert validated.next_context() == {"prev_requirement": REQUIREMENT, "prev_sql": "SELECT 1",
                                        "prev_tables": ["ads.org_corp_dep_bal_d", "dim.org"]}
    assert clarify_result().next_context() == {"prev_requirement": REQUIREMENT, "prev_clarify": CLARIFY}
    assert ok_result().model_copy(update={"status": "unvalidated"}).next_context() == {"prev_requirement": REQUIREMENT}


def test_context_carries_no_sql_from_a_turn_that_did_not_pass_validation(store):
    conversation = store.create()
    unvalidated = ok_result().model_copy(update={"status": "unvalidated"})

    store.add_turn(conversation.id, "查存款", unvalidated)

    context = store.context(conversation.id)
    assert context.prev_requirement == REQUIREMENT and context.prev_sql == "" and context.prev_tables == []
