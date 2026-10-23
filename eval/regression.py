"""
零成本回归（元数据切换前的关卡 G4，不调大模型）
- 题库静态核对：题库的标准 SQL（含其它正确答案）、标准用表、关键字段在新旧两版上各核对一遍，比哪些题变差了
- 召回重放：拿最近一次评估逐题记录里 ① 的输出，在新旧两版上各跑一遍 ②（第 0 层），比标准用表还在不在候选里。
  要用向量器算检索词的向量，BGE 时要加载模型，所以由调用方决定跑不跑
- 会话影响：会话库里有多少个会话的上一轮用到了被删的表，只报数。用 ConversationStore 读，
  打开时会照常执行一遍建表语句（已经存在就不动）、写一下库版本号，不改会话内容
题库有两种格式：模拟库的 eval/nl2sql_questions.yaml，真实库静态评估的题目文件（带 gold_table_sets）
"""
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml

import config
from eval.nl2sql_eval import StaticQuestion, best_group_rank, load_static_questions
from eval.questions import EvalQuestion, load_questions
from lineage.graph import LineageGraph
from metadata.store import MetadataStore
from nl2sql.history import ConversationStore
from nl2sql.parser import to_recall_query
from nl2sql.retriever import Retriever
from nl2sql.schemas import ParsedRequest
from nl2sql.service import embedder_for
from nl2sql.validator import SqlValidator

Question = EvalQuestion | StaticQuestion
ROOT = Path(__file__).resolve().parents[1]
MAX_ITEMS = 30


@dataclass
class GateSources:
    """可选的题库和逐题运行记录，不存在时对应值为 None。"""

    questions: Path | None = None
    runs: Path | None = None


@dataclass
class ReplayOutcome:
    """一道题的候选顺序、标准用表是否齐全、最好一组的名次。"""

    candidates: list[str]
    recall_ok: bool
    best_rank: int | None


@dataclass
class RegressionResult:
    """需确认项、影响面报告正文、各关卡简述。"""

    confirm_items: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    gates: dict[str, str] = field(default_factory=dict)


def find_sources(mart_dir: Path) -> GateSources:
    """按资料目录寻找题库与最近的逐题记录；模拟库使用仓库约定路径。"""
    mart_dir = Path(mart_dir).resolve()
    if mart_dir == (ROOT / "data" / "mock_mart").resolve():
        questions = ROOT / "eval" / "nl2sql_questions.yaml"
        directories = sorted((ROOT / "eval" / "reports" / "runs").glob("*"),
                             key=lambda p: p.name, reverse=True)
        runs = next((p / "C.jsonl" for p in directories if p.is_dir() and (p / "C.jsonl").is_file()), None)
    else:
        questions = mart_dir / "eval_questions.yaml"
        paths = [p for p in (mart_dir / "eval").glob("*.jsonl") if "static_eval" in p.name and p.is_file()]
        runs = max(paths, key=lambda p: (p.stat().st_mtime_ns, p.name)) if paths else None
    return GateSources(questions if questions.is_file() else None, runs)


def read_questions(path: Path) -> list[Question]:
    """按内容识别两种题库格式，使用原有加载器做结构与题号校验。"""
    items = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if isinstance(items, list) and any(isinstance(q, dict) and "gold_table_sets" in q for q in items):
        return load_static_questions(path)
    return load_questions(path)


def _groups(question: Question) -> list[list[str]]:
    if isinstance(question, StaticQuestion):
        return question.gold_table_sets
    return [question.gold_tables] if question.expect != "clarify" and question.gold_tables else []


def static_check(store: MetadataStore, questions: Sequence[Question]) -> dict[str, list[str]]:
    """核对题库里的标准 SQL、用表和关键字段，跳过期望反问及无标准的题。"""
    validator = SqlValidator(store, dialect=config.SQL_DIALECT)
    results = {}
    for question in questions:
        groups = _groups(question)
        if isinstance(question, StaticQuestion):
            if any(all(store.table(t) is not None for t in group) for group in groups):
                results[question.id] = []
            else:
                missing = sorted({t for group in groups for t in group if store.table(t) is None})
                results[question.id] = [f"R3 表 {t} 不存在" for t in missing]
            continue
        sqls = [sql for sql in [question.gold_sql, *(a.sql for a in question.alternatives)] if sql.strip()]
        if question.expect == "clarify" or not (sqls or groups):
            continue
        issues = [f"{e.rule} {e.message}" for sql in sqls for e in validator.validate(sql).errors]
        issues += [f"R3 表 {t} 不存在" for group in groups for t in group if store.table(t) is None]
        for ref in question.gold_columns:
            table, separator, column = ref.rpartition(".")
            if not separator or store.column(table, column) is None:
                issues.append(f"R4 字段 {ref} 不存在")
        results[question.id] = list(dict.fromkeys(issues))
    return results


def replay_recall(store: MetadataStore, records: Path, questions: Sequence[Question]) -> dict[str, ReplayOutcome]:
    """重放记录里最后一次成功解析，直接调用第零层召回，不调用任何大模型。"""
    known = {q.id: q for q in questions if _groups(q)}
    retriever = Retriever(store, LineageGraph(store), embedder_for(store))
    outcomes = {}
    with Path(records).open(encoding="utf-8-sig") as file:
        for line in file:
            if not line.strip():
                continue
            record = json.loads(line)
            question = known.get(record["question_id"])
            if question is None:
                continue
            parsed = next((r["parsed"] for r in reversed(record["results"]) if r.get("parsed") is not None), None)
            if parsed is None:
                continue
            query = to_recall_query(ParsedRequest.model_validate(parsed))
            candidates = [t.table_id for t in retriever.recall(query, 0).tables]
            rank = best_group_rank(_groups(question), candidates)
            outcomes[question.id] = ReplayOutcome(candidates, rank is not None, rank)
    return outcomes


def session_impact(history_db: Path, removed_tables: set[str]) -> int:
    """统计上一轮用到被删表的会话个数；历史库不存在时不创建文件。"""
    if not Path(history_db).is_file() or not removed_tables:
        return 0
    store = ConversationStore(history_db)
    return sum(bool(removed_tables.intersection(store.context(c.id).prev_tables))
               for c in store.list_recent(limit=100000))


def _one_line(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _failed(result: RegressionResult, gate: str, error: Exception) -> None:
    reason = _one_line(str(error)) or type(error).__name__
    result.gates[gate] = f"没跑完：{reason}"
    result.confirm_items.append(f"零成本回归没跑完：{reason}")


def run_regression(mart_dir: Path, baseline: MetadataStore, candidate: MetadataStore, *,
                   removed_tables: set[str], replay: bool, sources: GateSources | None = None) -> RegressionResult:
    """分别比较静态核对和召回重放的退化；各关卡出错不影响其它关卡继续。"""
    result = RegressionResult()
    questions = None
    try:
        sources = find_sources(mart_dir) if sources is None else sources
        if sources.questions is None:
            result.gates["题库静态核对"] = "没跑：没有题库"
        else:
            questions = read_questions(sources.questions)
            old, new = static_check(baseline, questions), static_check(candidate, questions)
            result.gates["题库静态核对"] = (f"这一版 {sum(not e for e in new.values())}/{len(new)} 通过，"
                                          f"基线 {sum(not e for e in old.values())}/{len(old)}")
            for key, issues in new.items():
                if key in old and not old[key] and issues:
                    result.confirm_items.append(f"题库 {key} 在这一版上核对不过：{_one_line(issues[0])}")
    except Exception as error:  # 单个回归关卡失败，候选版本仍然可供人工审核
        _failed(result, "题库静态核对", error)
    try:
        if sources is None or sources.questions is None:
            result.gates["召回重放"] = "没跑：没有题库"
        elif sources.runs is None:
            result.gates["召回重放"] = "没跑：没有逐题记录"
        elif not replay:
            result.gates["召回重放"] = "没跑：向量模型没加载，要跑加 --replay"
        elif questions is None:
            result.gates["召回重放"] = "没跑：题库读取失败"
        else:
            old_recall = replay_recall(baseline, sources.runs, questions)
            new_recall = replay_recall(candidate, sources.runs, questions)
            result.gates["召回重放"] = (f"这一版 {sum(o.recall_ok for o in new_recall.values())}/{len(new_recall)} 进候选，"
                                      f"基线 {sum(o.recall_ok for o in old_recall.values())}/{len(old_recall)}")
            for key, outcome in new_recall.items():
                if key in old_recall and old_recall[key].recall_ok and not outcome.recall_ok:
                    result.confirm_items.append(f"召回重放：{key} 的标准用表掉出了候选")
    except Exception as error:
        _failed(result, "召回重放", error)
    try:
        count = session_impact(Path(mart_dir) / "build" / "history.db", removed_tables)
        result.gates["会话影响"] = f"{count} 个会话的上一轮用到了被删的表"
    except Exception as error:
        _failed(result, "会话影响", error)
    result.lines = [f"- {gate}：{message}" for gate, message in result.gates.items()]
    if result.confirm_items:
        result.lines += ["", "退化或未完成的检查：", ""]
        result.lines += [f"- {item}" for item in result.confirm_items[:MAX_ITEMS]]
        if len(result.confirm_items) > MAX_ITEMS:
            result.lines.append(f"- 等共 {len(result.confirm_items)} 项")
    return result
