"""
召回重放命令：改了召回或元数据之后，不调大模型，看标准用表在候选里的名次变了多少
用法: python -m eval.recall_replay 题库 [--mart 资料目录] [--parse-missing] [--model 模型] [--today 日期] [--baseline 结果文件]

按题号从逐题记录里取 ① 的结果，直接跑第 0 层召回，打印标准用表进前 4（证据全列字段的那几张）、前 8、候选的题数，
和上一次（或 --baseline 指定的那次）比变好、变差了几题。结果存到 资料目录/build/replay/，下一次就和它比。
  ① 的记录：模拟库在 eval/reports/runs/<时间>/C.jsonl，其它资料在 资料目录/eval/*.jsonl，新的优先；题目文字改过的记录不用
  --parse-missing：没有记录的题只调一次 ① 补上，存到 资料目录/build/parsed_records.jsonl，以后接着重放
只测得了第 ② 步：对的表找到没有。SQL 对不对要跑完整评估；改了 ① 的提示词，记录就和线上不一样了，也要重新跑评估。
"""
try:
    import pyarrow  # noqa: F401  先于 torch 导入：Windows 上先 torch 后 pyarrow，加载 BGE 时进程直接崩溃
except ImportError:
    pass

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import config
from eval.nl2sql_eval import MOCK_MART_DIR, REPORTS_DIR
from eval.questions import EVAL_TODAY
from eval.regression import Question, gold_groups, last_parsed, read_questions, replay_parsed
from metadata.importer import BUILD_DIR_NAME, default_db_path
from metadata.store import MetadataStore
from nl2sql.llm import ChatModelClient, LLMClient, StepError
from nl2sql.parser import parse_request
from nl2sql.schemas import ParsedRequest, QueryRequest

PARSED_FILE_NAME = "parsed_records.jsonl"
REPLAY_DIR_NAME = "replay"
MOCK_RECORDS = "C.jsonl"            # 模拟库每次评估里 C 组的逐题记录
TOP_RANKS = (4, 8)                  # 证据里前 4 张候选全列字段；前 8 张是第 0 层标准表的名额
PARSE_WORKERS = 4                   # 和评估的默认线程数一样
PARSE_TOKENS = 5200                 # ① 每次调用大约的 token 数，真实库 200 题的记录算出来的
MAX_LISTED = 30                     # 题号最多列几道


@dataclass
class ReplayRun:
    """一次重放的结果"""

    questions: str                       # 题库文件名
    metadata_version: str | None
    ranks: dict[str, int | None]         # 题号 → 最好一组标准用表的名次，不在候选里是 None


def _order(rank: int | None) -> float:
    return float("inf") if rank is None else rank


def _rank_text(rank: int | None) -> str:
    return "不在候选" if rank is None else str(rank)


def parsed_records_path(mart_dir: Path) -> Path:
    """--parse-missing 写的记录：资料目录/build/parsed_records.jsonl"""
    return Path(mart_dir) / BUILD_DIR_NAME / PARSED_FILE_NAME


def record_files(mart_dir: Path) -> list[Path]:
    """一套资料的逐题记录，新的在前"""
    mart_dir = Path(mart_dir).resolve()
    if mart_dir == MOCK_MART_DIR.resolve():
        paths = list((REPORTS_DIR / "runs").glob(f"*/{MOCK_RECORDS}"))
    else:
        paths = list((mart_dir / "eval").glob("*.jsonl"))
    cache = parsed_records_path(mart_dir)
    return sorted([*paths, cache] if cache.is_file() else paths, key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)


def _same_question(record: dict, question: Question) -> bool:
    """记录里第一轮的文字和题库一样；记录没存文字时不核对"""
    results = record.get("results") or [{}]
    return results[0].get("question", question.turns[0]) == question.turns[0]


def collect_parsed(files: Sequence[Path], questions: Sequence[Question]) -> dict[str, ParsedRequest]:
    """按题号取 ① 的结果：files 里靠前的文件优先，同一文件里靠后的记录优先；题都找齐了就不再读剩下的文件"""
    wanted = {question.id: question for question in questions if gold_groups(question)}
    found: dict[str, ParsedRequest] = {}
    for path in files:
        if wanted.keys() <= found.keys():
            break
        in_file: dict[str, ParsedRequest] = {}
        with path.open(encoding="utf-8-sig") as file:
            for line in file:
                if not line.strip():
                    continue
                record = json.loads(line)
                question = wanted.get(record.get("question_id"))
                parsed = last_parsed(record) if question is not None and _same_question(record, question) else None
                if parsed is not None:
                    in_file[question.id] = parsed
        found = {**in_file, **found}
    return found


def parse_turns(llm: LLMClient, question: Question, today: date) -> list[tuple[str, ParsedRequest]]:
    """只跑 ①：各轮输入依次解析，上一轮的完整需求和反问带进下一轮；最后一轮反问了而且题目给了口径时回答一次

    Returns:
        每一轮的 (输入, 解析结果)

    Raises:
        StepError: 模型调用失败，或者两次输出都不合格
    """
    turns: list[tuple[str, ParsedRequest]] = []

    def ask(text: str) -> None:
        last = turns[-1][1] if turns else None
        context = {"prev_requirement": last.requirement, "prev_clarify": last.clarify} if last is not None else {}
        parsed, _ = parse_request(llm, QueryRequest(question=text, today=today, **context), today)
        turns.append((text, parsed))

    for text in question.turns:
        ask(text)
    answer = getattr(question, "clarify_answer", "")
    if turns[-1][1].clarify is not None and answer:
        ask(answer)
    return turns


def parse_missing(llm: LLMClient, questions: Sequence[Question], path: Path, today: date,
                  workers: int = PARSE_WORKERS) -> dict[str, ParsedRequest]:
    """给没有记录的题调 ①，一题一行追加到 path，中途断了已经解析的不丢；解析失败的题打印原因后跳过

    Args:
        llm: 模型
        questions: 没有记录的题
        path: 记录文件，格式和评估的逐题记录一样
        today: 题目自己没写「今天」时用它
        workers: 线程数

    Returns:
        题号 → 最后一次解析结果
    """
    def run(question: Question) -> tuple[Question, list[tuple[str, ParsedRequest]], str]:
        try:
            return question, parse_turns(llm, question, getattr(question, "today", today)), ""
        except StepError as error:
            return question, [], str(error)

    found: dict[str, ParsedRequest] = {}
    path.parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=workers) as pool, path.open("a", encoding="utf-8") as file:
        for question, turns, problem in pool.map(run, questions):
            if problem:
                print(f"{question.id} 解析失败，这道题不算：{problem}")
                continue
            results = [{"question": text, "parsed": parsed.model_dump(mode="json")} for text, parsed in turns]
            record = {"question_id": question.id, "turns": question.turns, "model": getattr(llm, "name", ""), "results": results}
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
            file.flush()
            found[question.id] = turns[-1][1]
    return found


def save_run(run: ReplayRun, mart_dir: Path, now: datetime) -> Path:
    """结果存成 资料目录/build/replay/<题库>-<时间>.json，返回路径"""
    directory = Path(mart_dir) / BUILD_DIR_NAME / REPLAY_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{Path(run.questions).stem}-{now:%Y%m%d-%H%M%S}.json"
    body = {"questions": run.questions, "metadata_version": run.metadata_version, "created": now.isoformat(), "ranks": run.ranks}
    path.write_text(json.dumps(body, ensure_ascii=False, indent=0), encoding="utf-8")
    return path


def latest_run(mart_dir: Path, questions: str) -> Path | None:
    """这个题库上一次的结果；文件名里带时间，按名字排就是按时间排"""
    return max((Path(mart_dir) / BUILD_DIR_NAME / REPLAY_DIR_NAME).glob(f"{Path(questions).stem}-*.json"), default=None)


def read_run(path: Path) -> ReplayRun:
    """读 save_run 存的结果"""
    body = json.loads(Path(path).read_text(encoding="utf-8"))
    return ReplayRun(body["questions"], body["metadata_version"], body["ranks"])


def _counts(ranks: Mapping[str, int | None]) -> list[tuple[str, int]]:
    """各档的题数：(名称, 题数)"""
    return ([(f"进前 {top}", sum(_order(rank) <= top for rank in ranks.values())) for top in TOP_RANKS]
            + [("进候选", sum(rank is not None for rank in ranks.values()))])


def render_run(run: ReplayRun, baseline: ReplayRun | None) -> list[str]:
    """一次重放的汇报：各档题数；有上一次结果时，比两次都有的题，列出变差的题"""
    lines = [f"{run.questions}：{len(run.ranks)} 题，元数据 {run.metadata_version or '没有版本号'}"]
    if baseline is None:
        return lines + [f"{name}：{count}/{len(run.ranks)}" for name, count in _counts(run.ranks)]
    common = run.ranks.keys() & baseline.ranks.keys()
    old = {key: baseline.ranks[key] for key in common}
    new = {key: run.ranks[key] for key in common}
    if len(common) != len(run.ranks) or len(common) != len(baseline.ranks):
        lines.append(f"两次的题不完全一样，只比共同的 {len(common)} 题")
    lines += [f"{name}：{before} → {after}（共 {len(common)} 题）"
              for (name, before), (_, after) in zip(_counts(old), _counts(new))]
    better = sum(_order(new[key]) < _order(old[key]) for key in common)
    worse = sorted(key for key in common if _order(new[key]) > _order(old[key]))
    lines.append(f"名次变好 {better} 题，变差 {len(worse)} 题")
    if worse:
        listed = "、".join(f"{key} {_rank_text(old[key])}→{_rank_text(new[key])}" for key in worse[:MAX_LISTED])
        lines.append(f"变差的题：{listed}" + (f" 等共 {len(worse)} 题" if len(worse) > MAX_LISTED else ""))
    return lines


def _mart_of(questions: Path) -> Path:
    """题库放在资料目录里时就是这套资料，否则是模拟库（它的题库在 eval/ 下）"""
    return questions.parent if default_db_path(questions.parent).is_file() else MOCK_MART_DIR


def replay(questions_path: Path, mart_dir: Path, *, llm: LLMClient | None = None, baseline: Path | None = None,
           today: date = date.fromisoformat(EVAL_TODAY), now: datetime | None = None) -> tuple[list[str], Path | None]:
    """重放一个题库

    Args:
        questions_path: 题库文件
        mart_dir: 资料目录
        llm: 给没有记录的题调 ① 用的模型；None 时这些题不算
        baseline: 比较的结果文件；None 时和这个题库上一次的结果比
        today: 题目自己没写「今天」时用它
        now: 这次的时间，写进结果文件名

    Returns:
        (汇报的每一行, 结果文件)。一道能重放的题也没有时不存结果，结果文件是 None
    """
    questions = read_questions(questions_path)
    parsed = collect_parsed(record_files(mart_dir), questions)
    missing = [question for question in questions if gold_groups(question) and question.id not in parsed]
    if missing and llm is not None:
        print(f"{len(missing)} 题没有 ① 的记录，调模型补上，每题约 {PARSE_TOKENS} token")
        parsed.update(parse_missing(llm, missing, parsed_records_path(mart_dir), today))
        missing = [question for question in missing if question.id not in parsed]
    store = MetadataStore(default_db_path(mart_dir))
    outcomes = replay_parsed(store, parsed, questions)
    lines = []
    if missing:
        ids = "、".join(question.id for question in missing[:MAX_LISTED])
        lines.append(f"{len(missing)} 题没有 ① 的记录，没算进去：{ids}" + ("…" if len(missing) > MAX_LISTED else "")
                     + "；加 --parse-missing 调模型补上")
    if not outcomes:
        return lines + [f"{questions_path.name}：没有能重放的题"], None
    run = ReplayRun(questions_path.name, store.version_id, {key: outcome.best_rank for key, outcome in outcomes.items()})
    previous = baseline or latest_run(mart_dir, run.questions)
    lines += render_run(run, read_run(previous) if previous is not None else None)
    if store.embedder_name is None:
        lines.append("元数据库里没有向量，这次只走了关键字路，名次不能和带向量的结果比")
    return lines, save_run(run, mart_dir, now or datetime.now())


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="召回重放：不调大模型，看标准用表在候选里的名次变了多少")
    parser.add_argument("questions", help="题库文件，模拟库的 eval/nl2sql_questions.yaml 或资料目录里的题目文件")
    parser.add_argument("--mart", default="", help="资料目录；不写时题库所在目录有元数据库就用它，否则用模拟库")
    parser.add_argument("--parse-missing", action="store_true", help="没有 ① 记录的题调模型补上，一题一次")
    parser.add_argument("--model", default=None, help="补记录用的模型（config.MODEL_CHOICES 之一），不写用默认模型")
    parser.add_argument("--today", default=EVAL_TODAY, help="题目自己没写今天时用的今天，YYYY-MM-DD")
    parser.add_argument("--baseline", default="", help="和这份结果比；不写时和这个题库上一次的结果比")
    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行入口，返回退出码：0 成功，1 运行失败，2 参数不对"""
    for stream in (sys.stdout, sys.stderr):     # 输出走管道时 Windows 上是 GBK，打印不了「→」，换成问号
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        today = date.fromisoformat(args.today)
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else 0
    except ValueError:
        print(f"错误：--today 不是 YYYY-MM-DD：{args.today}", file=sys.stderr)
        return 2
    questions = Path(args.questions)
    try:
        mart_dir = Path(args.mart) if args.mart else _mart_of(questions)
        llm = ChatModelClient(timeout=config.LLM_TIMEOUT, model=args.model) if args.parse_missing else None
        lines, saved = replay(questions, mart_dir, llm=llm, baseline=Path(args.baseline) if args.baseline else None, today=today)
    except (FileNotFoundError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    if saved is None:
        return 1
    print(f"结果：{saved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
