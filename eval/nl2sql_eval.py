"""
查询流水线的评估
- 模拟库三组对照：A 仅模型结构、B 加血缘、C 加血缘和样例，各跑全部题，主指标是执行准确率；
  错题按规则归因到具体步骤，血缘贡献 = B − A，样例贡献 = C − B
- 真实库静态评估：只跑 C 组，看标准用表进没进候选、SQL 用的表对不对、校验过没过，SQL 对不对由人判断

命令行：
    python -m eval.nl2sql_eval [--configs A,B,C] [--ids B01,K02] [--limit N] [--workers 4] [--today 2026-10-13]
                               [--resume 目录] [--out eval/reports]
    python -m eval.nl2sql_eval --static 题目文件 --mart 资料目录 --out 输出目录
真实库的静态评估必须写 --out，不给默认值，免得报告写进入库的目录。

评估自己执行 SQL（标准 SQL 和生成的 SQL 都带行数上限和超时），所以取流水线时不让它执行。
"""
import argparse
import json
import sys
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, fields
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from eval.compare import compare_results
from eval.questions import EVAL_TODAY, EvalQuestion, load_questions
from metadata.store import MetadataStore
from nl2sql.executor import DuckDBExecutor, ExecResult, Executor
from nl2sql.schemas import INTERNAL_STEP, QueryRequest, QueryResult
from nl2sql.validator import SqlValidator

CONFIGS: dict[str, tuple[bool, bool]] = {"A": (False, False), "B": (True, False), "C": (True, True)}  # (use_lineage, use_examples)
CONFIG_NAMES = {"A": "仅模型结构", "B": "加血缘", "C": "加血缘和样例"}
EVAL_MAX_ROWS = 100_000
EVAL_TIMEOUT_S = 30
DEFAULT_WORKERS = 4
REPORTS_DIR = Path(__file__).parent / "reports"
MOCK_MART_DIR = Path(__file__).resolve().parent.parent / "data" / "mock_mart"
FOLLOW_UP_CATEGORY = "追问"                     # 题库里追问题的类别名
SQL_STATUSES = ("ok", "unvalidated")            # 流水线给出了 SQL 的状态


class PipelineLike(Protocol):
    """评估只需要流水线的 run，测试里传假流水线"""

    def run(self, request: QueryRequest) -> QueryResult:
        """跑一轮查询，不抛异常"""
        ...


@dataclass
class QuestionOutcome:
    """一道题在一组配置下的结果"""

    config: str
    question_id: str
    category: str
    expect: str
    results: list[QueryResult]                # 每一轮一个
    exec_ok: bool | None                      # 期望反问的题为 None
    exec_error: str                           # 生成 SQL 转换或执行报错
    recall_ok: bool | None
    table_ok: bool | None
    column_ok: bool | None
    clarify_ok: bool                          # 该问的问了、不该问的没问
    first_pass_ok: bool | None
    final_ok: bool | None
    elapsed_ms: int
    llm_calls: int
    tokens: int
    attribution: str                          # 对的题为空
    # SQL 用的表包含某一组标准用表（多关联一张不影响结果的维表也算）；期望反问的题、旧记录里没有这一项时为 None
    table_contains_ok: bool | None = None


@dataclass
class EvalSummary:
    """一次评估的全部结果"""

    outcomes: list[QuestionOutcome]
    started: str
    finished: str
    model: str
    today: str
    workers: int = DEFAULT_WORKERS


@dataclass(frozen=True)
class AnswerGroup:
    """一组标准答案的用表和关键字段（写成 模式.表.字段）"""

    tables: frozenset[str]
    columns: frozenset[str]


# ═══════════════ 计分 ═══════════════

def answer_groups(question: EvalQuestion, validator: SqlValidator) -> list[AnswerGroup]:
    """一道题的标准答案组：标准答案一组，每条其它正确写法一组

    其它写法的用表取自静态校验器对它的 SQL 的解析；关键字段没写的，沿用标准答案的。
    读不出表的写法不成组：空集合是任何集合的子集，留着会把召回判成全对。
    """
    groups: list[AnswerGroup] = []
    if question.gold_tables:
        groups.append(AnswerGroup(frozenset(question.gold_tables), frozenset(question.gold_columns)))
    for alternative in question.alternatives:
        tables = frozenset(validator.validate(alternative.sql).tables)
        if tables:
            groups.append(AnswerGroup(tables, frozenset(alternative.columns or question.gold_columns)))
    return groups


class _GoldResults:
    """标准 SQL 的执行结果，按题号缓存；三组配置、多个线程共用，每题只执行一次"""

    def __init__(self, executor: Executor) -> None:
        self._executor = executor
        self._results: dict[str, ExecResult] = {}
        self._lock = threading.Lock()

    def get(self, question: EvalQuestion) -> ExecResult:
        """取标准 SQL 的结果，第一次要时执行"""
        with self._lock:
            if question.id not in self._results:
                self._results[question.id] = self._executor.execute(
                    question.gold_sql, max_rows=EVAL_MAX_ROWS, timeout_s=EVAL_TIMEOUT_S)
            return self._results[question.id]


def evaluate_question(pipeline: PipelineLike, question: EvalQuestion, config: str, executor: Executor,
                      today: date, *, store: MetadataStore, groups: list[AnswerGroup] | None = None,
                      gold_cache: _GoldResults | None = None) -> QuestionOutcome:
    """跑一道题并计分，不抛异常

    追问题跑两轮：第二轮带上第一轮的完整需求，第一轮反问了的话再带上反问；以最后一轮的结果计分。

    Args:
        pipeline: 流水线
        question: 题目
        config: A、B、C 之一
        executor: 执行生成的 SQL 和标准 SQL 用的执行层
        today: 固定的「今天」
        store: 元数据库，算标准答案组的用表
        groups: 标准答案组，批量评估时事先算好传进来；None 时现算
        gold_cache: 标准 SQL 的结果缓存，批量评估时传进来；None 时每次现跑

    Returns:
        这道题的结果

    Raises:
        ValueError: 配置不是 A、B、C 之一（这是调用方的错，不是题目的问题）
    """
    if config not in CONFIGS:
        raise ValueError(f"未知的配置 {config!r}，可选 {'、'.join(CONFIGS)}")
    use_lineage, use_examples = CONFIGS[config]
    results: list[QueryResult] = []
    try:
        results = _run_turns(pipeline, question, use_lineage, use_examples, today)
        if groups is None:
            groups = answer_groups(question, SqlValidator(store))
        return _score(question, config, results, groups, executor, gold_cache or _GoldResults(executor))
    except Exception as error:  # noqa: BLE001 评估不能因为一道题崩掉：计分环节出错也记成这道题答错，把原因写进归因
        wants_sql = question.expect == "sql"
        return QuestionOutcome(
            config=config, question_id=question.id, category=question.category, expect=question.expect,
            results=results, exec_ok=False if wants_sql else None, exec_error="", recall_ok=None, table_ok=None,
            column_ok=None, clarify_ok=False, first_pass_ok=None, final_ok=None,
            elapsed_ms=sum(item.elapsed_ms for item in results), llm_calls=sum(item.llm_calls for item in results),
            tokens=_tokens(results), attribution=f"评估程序出错：{type(error).__name__}: {error}")


def _run_turns(pipeline: PipelineLike, question: EvalQuestion, use_lineage: bool, use_examples: bool,
               today: date) -> list[QueryResult]:
    """按轮跑流水线：下一轮带上上一轮的完整需求，上一轮反问了的话再带上反问"""
    results: list[QueryResult] = []
    for turn in question.turns:
        previous = results[-1] if results else None
        request = QueryRequest(
            question=turn, today=today, use_lineage=use_lineage, use_examples=use_examples,
            prev_requirement=previous.requirement if previous is not None else "",
            prev_clarify=previous.clarify if previous is not None and previous.status == "need_clarify" else None)
        results.append(_run_request(pipeline, request))
    return results


def _run_request(pipeline: PipelineLike, request: QueryRequest) -> QueryResult:
    """跑一轮；流水线约定不抛异常，真抛了也记成 failed（停在 internal）"""
    started = time.perf_counter()
    try:
        return pipeline.run(request)
    except Exception as error:  # noqa: BLE001 与流水线自己包装内部错误的格式一致
        return QueryResult(status="failed", question=request.question, failed_step=INTERNAL_STEP,
                           message=f"内部错误：{type(error).__name__}: {error}",
                           elapsed_ms=int((time.perf_counter() - started) * 1000))


def _tokens(results: Sequence[QueryResult]) -> int:
    return sum(call.input_tokens + call.output_tokens for result in results for call in result.calls)


def _score(question: EvalQuestion, config: str, results: list[QueryResult], groups: list[AnswerGroup],
           executor: Executor, gold_cache: _GoldResults) -> QuestionOutcome:
    """按最后一轮的结果给一道题计分"""
    final = results[-1]
    wants_sql = question.expect == "sql"
    exec_ok: bool | None = None
    exec_error = ""
    recall_ok = table_ok = column_ok = table_contains_ok = None
    if wants_sql:
        exec_ok, exec_error = _execute_and_compare(question, final, executor, gold_cache)
        validation = final.validation
        candidates = set(final.candidate_tables)
        used = set(validation.tables) if validation is not None else set()
        recall_ok = any(group.tables <= candidates for group in groups)
        table_ok = validation is not None and any(used == group.tables for group in groups)
        table_contains_ok = validation is not None and any(group.tables <= used for group in groups)
        column_ok = validation is not None and any(group.columns <= set(validation.columns) for group in groups)
    clarify_ok = (final.status == "need_clarify") == (question.expect == "clarify")
    correct = exec_ok if wants_sql else clarify_ok
    attribution = "" if correct else _attribute(question.expect, final, recall_ok, table_ok, column_ok, exec_error)
    return QuestionOutcome(
        config=config, question_id=question.id, category=question.category, expect=question.expect, results=results,
        exec_ok=exec_ok, exec_error=exec_error, recall_ok=recall_ok, table_ok=table_ok, column_ok=column_ok,
        clarify_ok=clarify_ok, first_pass_ok=final.first_pass_ok,
        final_ok=None if final.status not in SQL_STATUSES else final.status == "ok",
        elapsed_ms=sum(item.elapsed_ms for item in results), llm_calls=sum(item.llm_calls for item in results),
        tokens=_tokens(results), attribution=attribution, table_contains_ok=table_contains_ok)


def _execute_and_compare(question: EvalQuestion, final: QueryResult, executor: Executor,
                         gold_cache: _GoldResults) -> tuple[bool, str]:
    """执行生成的 SQL 并与标准 SQL 的结果比对，返回（结果是否一致, 执行或比对的报错）

    只有状态是 ok 或 unvalidated 且有 SQL 时才执行。生成的 SQL 与标准 SQL 都带行数上限和超时。
    任一边的结果被行数上限截断时无法完整比对，按不一致处理，但这不是报错。
    """
    if final.status not in SQL_STATUSES or not final.sql.strip():
        return False, ""
    gold = gold_cache.get(question)
    if not gold.executed:
        return False, f"标准 SQL 执行失败：{gold.error or gold.note}"
    pred = executor.execute(final.sql, max_rows=EVAL_MAX_ROWS, timeout_s=EVAL_TIMEOUT_S)
    if not pred.executed:
        return False, pred.error or pred.note or "未执行"
    if gold.truncated or pred.truncated:
        return False, ""
    try:
        return compare_results(gold.rows, pred.rows, question.ordered), ""
    except Exception as error:  # noqa: BLE001 比对出异常算不一致，原因写进 exec_error
        return False, f"比对出错：{type(error).__name__}: {error}"


def _attribute(expect: str, final: QueryResult, recall_ok: bool | None, table_ok: bool | None,
               column_ok: bool | None, exec_error: str) -> str:
    """给一道错题归因，按顺序取第一条命中的

    1. 流水线失败：停在哪一步，加原因前 60 字
    2. 多余的反问，或该反问没有反问
    3. 召回漏表  4. 选表不对  5. 字段或口径不对
    6. 有执行或方言转换的报错（报告里单列）
    7. 修正两次仍没通过校验
    8. 其余：SQL 能跑，结果与标准答案不一致
    """
    if final.status == "failed":
        message = " ".join(final.message.split())[:60]
        return f"停在 {final.failed_step or '未知步骤'}：{message}"
    if expect == "sql" and final.status == "need_clarify":
        return "④ 多余的反问"
    if expect == "clarify" and final.status != "need_clarify":
        return "④ 该反问没有反问"
    if recall_ok is False:
        return "② 召回漏表"
    if table_ok is False:
        return "④ 选表不对"
    if column_ok is False:
        return "④ 字段或口径不对"
    if exec_error:
        return "执行失败（方言转换或 SQL 报错）"
    if final.status == "unvalidated":
        return "⑥ 修正两次仍未通过校验"
    return "⑤ SQL 与标准答案的结果不一致"


# ═══════════════ 并发运行与落盘 ═══════════════

def outcome_to_record(outcome: QuestionOutcome) -> dict:
    """一道题的结果转成可以写进 jsonl 的字典：各字段加上每一轮的 QueryResult（带提示词和回复）"""
    record = {item.name: getattr(outcome, item.name) for item in fields(outcome) if item.name != "results"}
    record["results"] = [result.model_dump(mode="json") for result in outcome.results]
    return record


def load_outcomes(run_dir: Path, configs: Sequence[str]) -> dict[tuple[str, str], QuestionOutcome]:
    """读取 run_dir 里已有的逐题记录，键是（配置, 题号）

    读不出来的行（例如进程被打断时写了一半的最后一行）直接跳过，那道题会重跑；同一道题有多行时取最后一行。
    按字节读、按换行符切行、逐行解码：最后一行截在汉字中间时只丢这一行；记录里的 U+2028 这类字符
    json.dumps 不转义，按文本的 splitlines 切会把一行切断。
    """
    found: dict[tuple[str, str], QuestionOutcome] = {}
    for config in configs:
        path = Path(run_dir) / f"{config}.jsonl"
        if not path.exists():
            continue
        for raw in path.read_bytes().split(b"\n"):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw.decode("utf-8"))
                results = [QueryResult.model_validate(item) for item in record.pop("results")]
                outcome = QuestionOutcome(results=results, **record)
            except (ValueError, TypeError, KeyError):       # 解码失败、JSON 不完整、字段缺失或多出来
                continue
            found[(outcome.config, outcome.question_id)] = outcome
    return found


def _repair_tail(path: Path) -> None:
    """上次被打断时最后一行可能没写完、没有换行：补一个换行，免得新记录粘在残缺的行后面"""
    if path.exists() and path.stat().st_size > 0:
        with path.open("rb+") as handle:
            handle.seek(-1, 2)
            if handle.read(1) != b"\n":
                handle.write(b"\n")


def _append_line(path: Path, record: dict, lock: threading.Lock) -> None:
    """往 jsonl 追加一行，追加时加锁"""
    line = json.dumps(record, ensure_ascii=False, default=str)
    with lock:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def run_eval(pipeline: PipelineLike, questions: Sequence[EvalQuestion], configs: Sequence[str], *, store: MetadataStore,
             executor: Executor, today: date, workers: int = DEFAULT_WORKERS, run_dir: Path, resume: bool = False,
             model: str = "", on_outcome: Callable[[QuestionOutcome], None] | None = None) -> EvalSummary:
    """并发跑全部题，每题跑完立刻追加到 run_dir/{config}.jsonl；resume 时跳过已有的（配置, 题号）

    Args:
        pipeline: 流水线
        questions: 题目
        configs: 要跑的配置，A、B、C 的任意组合
        store: 元数据库
        executor: 执行层，执行生成的 SQL 和标准 SQL
        today: 固定的「今天」
        workers: 并发线程数
        run_dir: 逐题记录目录
        resume: 接着上次的记录跑；记录目录里已经有记录而不接着跑时报错，免得新旧混在一起
        model: 模型名，写进报告
        on_outcome: 每做完一道题调用一次（在调用 run_eval 的线程里），用来显示进度

    Returns:
        全部结果：resume 时包含上次做完的，按配置、题库顺序排列

    Raises:
        ValueError: 配置不认识，或目录里已有记录而没有 resume
    """
    unknown = [config for config in configs if config not in CONFIGS]
    if unknown:
        raise ValueError(f"未知的配置 {unknown}，可选 {'、'.join(CONFIGS)}")
    configs = list(dict.fromkeys(configs))
    run_dir = Path(run_dir)
    paths = {config: run_dir / f"{config}.jsonl" for config in configs}
    done: dict[tuple[str, str], QuestionOutcome] = {}
    if resume:
        done = load_outcomes(run_dir, configs)
        for path in paths.values():
            _repair_tail(path)
    else:
        existing = [path.name for path in paths.values() if path.exists() and path.stat().st_size > 0]
        if existing:
            raise ValueError(f"{run_dir} 里已有逐题记录 {existing}：要接着跑请加 resume，要重跑请换一个目录")
    run_dir.mkdir(parents=True, exist_ok=True)

    validator = SqlValidator(store)
    groups = {question.id: answer_groups(question, validator) for question in questions}      # 启动时算一次
    gold_cache = _GoldResults(executor)
    lock = threading.Lock()
    started = _now()

    def work(config: str, question: EvalQuestion) -> QuestionOutcome:
        outcome = evaluate_question(pipeline, question, config, executor, today, store=store,
                                    groups=groups[question.id], gold_cache=gold_cache)
        _append_line(paths[config], outcome_to_record(outcome), lock)
        return outcome

    tasks = [(config, question) for config in configs for question in questions
             if (config, question.id) not in done]
    new: list[QuestionOutcome] = []
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        futures = [pool.submit(work, config, question) for config, question in tasks]
        for future in as_completed(futures):
            outcome = future.result()
            new.append(outcome)
            if on_outcome is not None:
                on_outcome(outcome)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)      # 被打断时没开始的题取消，已经在跑的做完并落盘

    wanted = {question.id for question in questions}
    outcomes = [outcome for key, outcome in done.items() if key[1] in wanted] + new
    config_order = {config: number for number, config in enumerate(configs)}
    question_order = {question.id: number for number, question in enumerate(questions)}
    outcomes.sort(key=lambda item: (config_order[item.config], question_order[item.question_id]))
    return EvalSummary(outcomes=outcomes, started=started, finished=_now(), model=model, today=today.isoformat(),
                       workers=workers)


# ═══════════════ 汇总指标 ═══════════════

@dataclass
class Metrics:
    """一组配置的汇总指标，计数加平均值；比例用对应的 *_rate 属性取，分母为 0 时是 None"""

    questions: int = 0                  # 题数
    sql_questions: int = 0              # 期望得到 SQL 的题数
    exec_ok: int = 0                    # 执行准确的题数（分母 sql_questions）
    recall_ok: int = 0
    table_ok: int = 0
    table_contains_ok: int = 0          # SQL 用的表包含标准用表（放宽的选表口径）
    column_ok: int = 0
    with_sql: int = 0                   # 流水线给出了 SQL 的题数（状态 ok 或 unvalidated）
    first_pass_ok: int = 0              # 其中第一次生成就通过校验的
    passed: int = 0                     # 其中最终状态是 ok 的
    clarify_ok: int = 0                 # 反问判对的题数（分母 questions）
    follow_up: int = 0                  # 追问题数
    follow_up_ok: int = 0               # 其中执行准确的
    avg_seconds: float = 0.0            # 平均每题耗时
    avg_calls: float = 0.0              # 平均每题模型调用次数
    avg_tokens: float = 0.0             # 平均每题 token 数

    @property
    def exec_rate(self) -> float | None:
        return _ratio(self.exec_ok, self.sql_questions)

    @property
    def recall_rate(self) -> float | None:
        return _ratio(self.recall_ok, self.sql_questions)

    @property
    def table_rate(self) -> float | None:
        return _ratio(self.table_ok, self.sql_questions)

    @property
    def table_contains_rate(self) -> float | None:
        return _ratio(self.table_contains_ok, self.sql_questions)

    @property
    def column_rate(self) -> float | None:
        return _ratio(self.column_ok, self.sql_questions)

    @property
    def first_pass_rate(self) -> float | None:
        return _ratio(self.first_pass_ok, self.with_sql)

    @property
    def passed_rate(self) -> float | None:
        return _ratio(self.passed, self.with_sql)

    @property
    def clarify_rate(self) -> float | None:
        return _ratio(self.clarify_ok, self.questions)

    @property
    def follow_up_rate(self) -> float | None:
        return _ratio(self.follow_up_ok, self.follow_up)


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def compute_metrics(outcomes: Sequence[QuestionOutcome]) -> Metrics:
    """一组配置的汇总指标（outcomes 应当是同一个配置的）"""
    metrics = Metrics(questions=len(outcomes))
    for outcome in outcomes:
        if outcome.expect == "sql":
            metrics.sql_questions += 1
            metrics.exec_ok += outcome.exec_ok is True
            metrics.recall_ok += outcome.recall_ok is True
            metrics.table_ok += outcome.table_ok is True
            metrics.table_contains_ok += outcome.table_contains_ok is True
            metrics.column_ok += outcome.column_ok is True
            if outcome.category == FOLLOW_UP_CATEGORY:
                metrics.follow_up += 1
                metrics.follow_up_ok += outcome.exec_ok is True
        if outcome.final_ok is not None:
            metrics.with_sql += 1
            metrics.first_pass_ok += outcome.first_pass_ok is True
            metrics.passed += outcome.final_ok is True
        metrics.clarify_ok += outcome.clarify_ok
    if outcomes:
        metrics.avg_seconds = sum(item.elapsed_ms for item in outcomes) / len(outcomes) / 1000
        metrics.avg_calls = sum(item.llm_calls for item in outcomes) / len(outcomes)
        metrics.avg_tokens = sum(item.tokens for item in outcomes) / len(outcomes)
    return metrics


# ═══════════════ 报告 ═══════════════

def _rate_text(numerator: int, denominator: int) -> str:
    """「3/5（60.0%）」，分母为 0 时是破折号"""
    return f"{numerator}/{denominator}（{numerator / denominator:.1%}）" if denominator else "—"


def _points(left: float | None, right: float | None) -> str:
    """两个比例的差，单位百分点，写成「+33.3」；任一边没有就是破折号"""
    return "—" if left is None or right is None else f"{(left - right) * 100:+.1f}"


def _duration_text(started: str, finished: str) -> str:
    seconds = max(0, int((datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()))
    return f"{seconds // 60} 分 {seconds % 60} 秒" if seconds >= 60 else f"{seconds} 秒"


def _one_line(text: str, limit: int = 300) -> str:
    return " ".join(text.split())[:limit]


def _metric_rows(metrics: Metrics) -> list[tuple[str, str, float | None]]:
    """一组配置的各项比例指标：（指标名, 显示的文字, 比例），比较表和贡献表共用；第一项是主指标"""
    return [
        ("执行准确率", _rate_text(metrics.exec_ok, metrics.sql_questions), metrics.exec_rate),
        ("候选召回率", _rate_text(metrics.recall_ok, metrics.sql_questions), metrics.recall_rate),
        ("选表准确率", _rate_text(metrics.table_ok, metrics.sql_questions), metrics.table_rate),
        ("选表准确率（包含标准用表）", _rate_text(metrics.table_contains_ok, metrics.sql_questions),
         metrics.table_contains_rate),
        ("字段准确率", _rate_text(metrics.column_ok, metrics.sql_questions), metrics.column_rate),
        ("校验一次通过率", _rate_text(metrics.first_pass_ok, metrics.with_sql), metrics.first_pass_rate),
        ("修正后通过率", _rate_text(metrics.passed, metrics.with_sql), metrics.passed_rate),
        ("反问准确率", _rate_text(metrics.clarify_ok, metrics.questions), metrics.clarify_rate),
        ("追问准确率", _rate_text(metrics.follow_up_ok, metrics.follow_up), metrics.follow_up_rate),
    ]


def _limitations(sql_count: int, alternative_count: int) -> list[str]:
    """报告末尾的局限：设计文档 §13.6 的意思，加上题库里没有修的已知问题"""
    per_question = f"，每道约占 {100 / sql_count:.1f} 个百分点" if sql_count else ""
    return [
        "模拟专题库和题库是为这个项目专门构造的，口径信息被刻意放在加工脚本里而不是字段说明里，以模拟真实字典说明不全的情况。"
        "因此血缘贡献（B−A）反映的是这种情况下的增益，不代表真实环境里的绝对水平。",
        "真实专题库没有数据，生成的 SQL 无法执行，只能做静态评估：选表是否正确、关键字段是否用对、静态校验是否通过，再加使用者的人工判断。"
        "模拟库和真实库的业务领域不同，这里的执行准确率说明的是流水线本身的能力，不能直接当成真实库上的准确率。",
        f"题量小：需要 SQL 的题 {sql_count} 道{per_question}；模型的输出也不完全确定。组间只差几道题的，不足以下结论。",
        "题和样例出自同一个人：样例与题目不重叠由签名和文本相似度的测试把关，但自己出题自己考的倾向仍在。",
        "U05（已销户的对公存款账户数）的 `cust_type = '1'` 条件在模拟数据里不起作用：个人账户全部是正常状态，"
        "漏掉这个条件结果照样正确，所以这道题测不出「还原对公条件」，U05 判对不能说明做到了这一点。数据没有改，只在这里说明。",
        "结果按两位小数比对。现算的日均与加工好的日均字段可能差几分钱（逐行四舍五入），"
        "T03、T04 因此只认现成的日均字段。",
        f"选表、字段准确率按「标准答案和其它正确写法」中任一组算对。题库里有 {alternative_count} 道题登记了其它正确写法"
        "（用机构汇总表的题，登记的是客户汇总表的写法）；没有登记的等价写法（例如改用整合层明细再关联客户维表）"
        "仍会被判为选表不对，执行准确率不受影响。",
        "选表准确率要求 SQL 用的表与某一组完全一致，多关联一张不影响结果的维表也算错；"
        "「选表准确率（包含标准用表）」是放宽的口径，SQL 用的表包含某一组就算对，两个数一起看。",
    ]


def render_report(summary: EvalSummary, questions: Sequence[EvalQuestion]) -> str:
    """评估报告（Markdown）：头部信息、三组对比、贡献、分类别、执行失败、错题明细、局限"""
    by_id = {question.id: question for question in questions}
    configs = [config for config in CONFIGS if any(item.config == config for item in summary.outcomes)]
    outcomes = {config: [item for item in summary.outcomes if item.config == config] for config in configs}
    metrics = {config: compute_metrics(outcomes[config]) for config in configs}
    lines: list[str] = []
    lines += _report_head(summary, configs, outcomes)
    lines += _report_comparison(configs, metrics)
    lines += _report_contribution(configs, metrics)
    lines += _report_categories(configs, outcomes, questions)
    lines += _report_exec_failures(summary.outcomes)
    lines += _report_wrong_answers(configs, outcomes, by_id)
    sql_count = sum(1 for question in questions if question.expect == "sql")
    alternative_count = sum(1 for question in questions if question.alternatives)
    lines += ["## 六、局限", "", *[f"- {text}" for text in _limitations(sql_count, alternative_count)], ""]
    return "\n".join(lines)


def _config_label(config: str) -> str:
    return f"{config} {CONFIG_NAMES[config]}"


def _report_head(summary: EvalSummary, configs: list[str], outcomes: dict[str, list[QuestionOutcome]]) -> list[str]:
    first = outcomes[configs[0]] if configs else []
    sql_count = sum(1 for item in first if item.expect == "sql")
    return [
        "# NL2SQL 评估报告（模拟专题库）", "",
        f"- 日期：{summary.started[:10]}",
        f"- 模型：{summary.model or '未记录'}",
        f"- 评估时的「今天」：{summary.today}",
        f"- 题数：{len(first)} 道（需要 SQL 的 {sql_count} 道，需要反问的 {len(first) - sql_count} 道），"
        f"配置：{'、'.join(_config_label(config) for config in configs) or '无'}",
        f"- 并发：{summary.workers} 个线程",
        f"- 用时：{_duration_text(summary.started, summary.finished)}", "",
    ]


def _report_comparison(configs: list[str], metrics: dict[str, Metrics]) -> list[str]:
    lines = ["## 一、三组对比", "",
             "| 指标 | " + " | ".join(_config_label(config) for config in configs) + " |",
             "|---|" + "---|" * len(configs)]
    for position, cells in enumerate(zip(*(_metric_rows(metrics[config]) for config in configs))):
        name = cells[0][0] + ("（主指标）" if position == 0 else "")
        lines.append(f"| {name} | " + " | ".join(cell[1] for cell in cells) + " |")
    for label, getter, unit in (("平均耗时", lambda m: f"{m.avg_seconds:.1f}", "秒/题"),
                                ("平均模型调用", lambda m: f"{m.avg_calls:.1f}", "次/题"),
                                ("平均 token", lambda m: f"{m.avg_tokens:.0f}", "个/题")):
        lines.append(f"| {label}（{unit}） | " + " | ".join(getter(metrics[config]) for config in configs) + " |")
    lines += ["",
              "分母：执行、召回、选表、字段准确率是需要 SQL 的题；校验两项是流水线给出了 SQL 的题；"
              "反问准确率是全部题；追问准确率是追问题的执行准确率。", ""]
    return lines


def _report_contribution(configs: list[str], metrics: dict[str, Metrics]) -> list[str]:
    lines = ["## 二、血缘与样例的贡献", ""]
    pairs = [(label, high, low) for label, high, low in
             (("血缘贡献（B−A）", "B", "A"), ("样例贡献（C−B）", "C", "B")) if high in configs and low in configs]
    if not pairs:
        lines += [f"贡献按组间的差值算，A、B、C 三组都跑过才能给出（这次跑了：{'、'.join(configs) or '无'}）。", ""]
        return lines
    lines += ["血缘贡献 = B − A，样例贡献 = C − B，单位是百分点，正数表示加上之后变好。", "",
              "| 指标 | " + " | ".join(label for label, _, _ in pairs) + " |", "|---|" + "---|" * len(pairs)]
    rows = {config: _metric_rows(metrics[config]) for config in configs}
    for position, (name, _, _) in enumerate(rows[pairs[0][1]]):
        cells = [_points(rows[high][position][2], rows[low][position][2]) for _, high, low in pairs]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    if len(pairs) < 2:
        lines += ["", "另一项需要 A、B、C 三组都跑过才能给出。"]
    lines.append("")
    return lines


def _report_categories(configs: list[str], outcomes: dict[str, list[QuestionOutcome]],
                       questions: Sequence[EvalQuestion]) -> list[str]:
    categories = list(dict.fromkeys([question.category for question in questions] +
                                    [item.category for config in configs for item in outcomes[config]]))
    lines = ["## 三、分类别的执行准确率", "",
             "| 类别 | " + " | ".join(_config_label(config) for config in configs) + " |", "|---|" + "---|" * len(configs)]
    for category in categories:
        members = {config: [item for item in outcomes[config] if item.category == category] for config in configs}
        if not any(members.values()):
            continue
        clarify_only = all(item.expect == "clarify" for config in configs for item in members[config])
        cells = []
        for config in configs:
            items = members[config]
            if clarify_only:
                cells.append(_rate_text(sum(item.clarify_ok for item in items), len(items)))
            else:
                cells.append(_rate_text(sum(item.exec_ok is True for item in items), len(items)))
        label = f"{category}（按反问准确率）" if clarify_only else category
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines.append("")
    return lines


def _report_exec_failures(outcomes: list[QuestionOutcome]) -> list[str]:
    failed = [item for item in outcomes if item.exec_error]
    lines = ["## 四、执行失败（方言转换或 SQL 报错）", "",
             "这类题记为不正确，和「能跑但结果不一致」的逻辑错误分开列出，"
             "方便区分是方言转换、SQL 写法的问题，还是选表、口径的问题。", ""]
    if not failed:
        return lines + ["没有执行失败的题。", ""]
    for item in failed:
        final = item.results[-1] if item.results else None
        lines += [f"- {_config_label(item.config)} {item.question_id}：{_one_line(item.exec_error)}"]
        if final is not None and final.sql:
            lines += ["", "  ```sql", *[f"  {row}" for row in final.sql.splitlines()], "  ```", ""]
    lines.append("")
    return lines


def _report_wrong_answers(configs: list[str], outcomes: dict[str, list[QuestionOutcome]],
                          by_id: dict[str, EvalQuestion]) -> list[str]:
    lines = ["## 五、错题明细", ""]
    for config in configs:
        wrong = [item for item in outcomes[config] if item.attribution]
        lines += [f"### {_config_label(config)}（错 {len(wrong)} 题）", ""]
        if not wrong:
            lines += ["没有错题。", ""]
            continue
        for item in wrong:
            lines += _wrong_answer_block(item, by_id.get(item.question_id))
    return lines


def _wrong_answer_block(item: QuestionOutcome, question: EvalQuestion | None) -> list[str]:
    final = item.results[-1] if item.results else None
    lines = [f"#### {item.question_id} {item.category}", "", f"- 归因：{item.attribution}"]
    if final is not None:
        lines.append(f"- 状态：{final.status}" + (f"（停在 {final.failed_step}）" if final.status == "failed" else ""))
        lines.append(f"- 完整需求：{final.requirement or '（没有）'}")
        if final.status == "failed" and final.message:
            lines.append(f"- 失败原因：{_one_line(final.message)}")
        if final.clarify is not None:
            options = "；".join(option.label for option in final.clarify.options)
            lines.append(f"- 反问：{final.clarify.question}（{options}）")
    if item.expect == "clarify":
        lines.append("- 期望被反问，没有标准 SQL")
    if item.exec_error:
        lines.append(f"- 执行报错：{_one_line(item.exec_error)}")
    lines.append("")
    if final is not None and final.sql:
        lines += ["生成的 SQL：", "", "```sql", final.sql, "```", ""]
    if question is not None and question.gold_sql:
        lines += ["标准 SQL：", "", "```sql", question.gold_sql.strip(), "```", ""]
    return lines


# ═══════════════ 真实库静态评估 ═══════════════

class StaticQuestion(BaseModel):
    """真实库静态评估的一道题"""

    model_config = ConfigDict(extra="forbid")           # 字段名写错时报错，不悄悄忽略

    id: str
    turns: list[str] = Field(min_length=1)
    today: date
    gold_table_sets: list[list[str]] = Field(min_length=1)   # 任一组被 SQL 用到的表包含即算选对
    clarify_answer: str = ""                  # 流水线反问时自动回答一次用的口径
    note: str = ""

    @field_validator("gold_table_sets")
    @classmethod
    def _groups_not_empty(cls, value: list[list[str]]) -> list[list[str]]:
        if any(not group for group in value):
            raise ValueError("每组标准用表至少要有一张表")
        return value


def load_static_questions(path: Path) -> list[StaticQuestion]:
    """读静态评估的题目文件（YAML 列表）

    Raises:
        ValueError: 文件是空的、顶层不是列表、某一条不是键值对、题号重复，或题目内容不合规
    """
    items = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if items is None:
        raise ValueError(f"题目文件是空的: {path}")
    if not isinstance(items, list):
        raise ValueError(f"题目文件的顶层必须是题目列表，实际是 {type(items).__name__}: {path}")
    not_questions = [number for number, item in enumerate(items, 1) if not isinstance(item, dict)]
    if not_questions:
        raise ValueError(f"题目文件里第 {not_questions} 条不是一道题（应为键值对）: {path}")
    questions = [StaticQuestion(**item) for item in items]
    ids = [question.id for question in questions]
    duplicated = sorted({item for item in ids if ids.count(item) > 1})
    if duplicated:
        raise ValueError(f"题目编号重复: {duplicated}")
    return questions


def best_group_rank(groups: Sequence[Sequence[str]], candidates: Sequence[str]) -> int | None:
    """最好的一组标准用表在候选里的名次，越小越好

    一组的名次 = 这组表里排得最靠后的那张在候选里的位置（从 1 开始），
    也就是「取前几张候选才能把这一组凑齐」。组里有一张表不在候选里，这一组没有名次。
    各组的名次取最小值；没有任何一组有名次时是 None。
    """
    position: dict[str, int] = {}
    for number, table in enumerate(candidates, 1):
        position.setdefault(table, number)
    ranks = [max(position[table] for table in group)
             for group in groups if group and all(table in position for table in group)]
    return min(ranks) if ranks else None


@dataclass
class StaticOutcome:
    """静态评估里一道题的结果"""

    question: StaticQuestion
    results: list[QueryResult]
    status: str                       # 最后一轮的状态
    clarify_count: int                # 反问了几次（各轮里状态是 need_clarify 的轮数）
    recall_ok: bool                   # 某一组标准用表全部进了候选
    best_rank: int | None             # 最好的一组在候选里的名次，见 best_group_rank
    tables_ok: bool                   # SQL 用的表包含某一组标准用表
    validation_ok: bool | None        # 校验是否通过；没有 SQL 时为 None


def run_static_eval(pipeline: PipelineLike, questions: Sequence[StaticQuestion], out_dir: Path) -> Path:
    """跑静态评估，报告和逐题记录写到 out_dir，返回报告路径

    只跑 C 组（加血缘和样例）。每道题先跑各轮输入；最后一轮反问了而且题目给了口径时，
    用口径回答一次；再反问就记为「反问两次」，不再往下问。报告和逐题记录同名不同后缀，
    文件名已被占用时加序号。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    outcomes = [_static_outcome(pipeline, question) for question in questions]
    report_path = _unique_path(out_dir, f"{date.today().isoformat()}_static_eval", (".md", ".jsonl"))
    records = [_static_record(outcome) for outcome in outcomes]
    report_path.with_suffix(".jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False, default=str) + "\n" for record in records), encoding="utf-8")
    report_path.write_text(_render_static_report(outcomes), encoding="utf-8")
    return report_path


def _static_request(text: str, previous: QueryResult | None, today: date) -> QueryRequest:
    return QueryRequest(
        question=text, today=today, use_lineage=True, use_examples=True,
        prev_requirement=previous.requirement if previous is not None else "",
        prev_clarify=previous.clarify if previous is not None and previous.status == "need_clarify" else None)


def _static_outcome(pipeline: PipelineLike, question: StaticQuestion) -> StaticOutcome:
    results: list[QueryResult] = []
    for turn in question.turns:
        results.append(_run_request(pipeline, _static_request(turn, results[-1] if results else None, question.today)))
    if results[-1].status == "need_clarify" and question.clarify_answer:
        results.append(_run_request(pipeline, _static_request(question.clarify_answer, results[-1], question.today)))
    final = results[-1]
    validation = final.validation
    candidates = set(final.candidate_tables)
    return StaticOutcome(
        question=question, results=results, status=final.status,
        clarify_count=sum(1 for result in results if result.status == "need_clarify"),
        recall_ok=any(set(group) <= candidates for group in question.gold_table_sets),
        best_rank=best_group_rank(question.gold_table_sets, final.candidate_tables),
        tables_ok=validation is not None and any(set(group) <= set(validation.tables)
                                                 for group in question.gold_table_sets),
        validation_ok=validation.ok if validation is not None else None)


def _static_record(outcome: StaticOutcome) -> dict:
    return {"question_id": outcome.question.id, "turns": outcome.question.turns, "note": outcome.question.note,
            "status": outcome.status, "clarify_count": outcome.clarify_count, "recall_ok": outcome.recall_ok,
            "best_rank": outcome.best_rank, "tables_ok": outcome.tables_ok, "validation_ok": outcome.validation_ok,
            "results": [result.model_dump(mode="json") for result in outcome.results]}


def _yes_no(value: bool | None) -> str:
    return "—" if value is None else "是" if value else "否"


def _render_static_report(outcomes: list[StaticOutcome]) -> str:
    total = len(outcomes)
    with_sql = [item for item in outcomes if item.validation_ok is not None]
    asked_once = sum(1 for item in outcomes if item.clarify_count == 1)
    asked_twice = sum(1 for item in outcomes if item.clarify_count >= 2)
    lines = [
        "# NL2SQL 真实库静态评估", "",
        f"- 日期：{date.today().isoformat()}",
        f"- 题数：{total} 道，只跑 C 组（加血缘和样例）",
        "- 只看静态结果：SQL 对不对、口径对不对由人判断，下面的逐题明细就是给人看的", "",
        "## 一、汇总", "",
        "| 指标 | 结果 |", "|---|---|",
        f"| 标准用表进入候选 | {_rate_text(sum(item.recall_ok for item in outcomes), total)} |",
        f"| SQL 用的表包含标准用表 | {_rate_text(sum(item.tables_ok for item in outcomes), total)} |",
        f"| 有 SQL 的题里校验通过（0 错误） | {_rate_text(sum(item.validation_ok is True for item in with_sql), len(with_sql))} |",
        f"| 给出了 SQL | {_rate_text(len(with_sql), total)} |",
        f"| 反问过 | 一次 {asked_once} 题，两次 {asked_twice} 题（题目给了口径的，第一次反问自动回答） |", "",
        "名次的算法：一组标准用表的名次，是这组表里排得最靠后的那张在候选里的位置（从 1 开始），"
        "也就是取前几张候选才能把这一组凑齐；组里有一张表不在候选里，这一组没有名次；"
        "有几组时取名次最小的一组；没有任何一组有名次时记为「—」。", "",
        "## 二、逐题结果", "",
        "| 题号 | 状态 | 反问 | 标准用表进候选 | 最好一组的名次 | SQL 用表包含标准用表 | 校验 |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in outcomes:
        validation = "—" if item.validation_ok is None else "校验通过" if item.validation_ok else "校验未通过"
        clarify = {0: "没问", 1: "一次"}.get(item.clarify_count, "反问两次")
        lines.append(f"| {item.question.id} | {item.status} | {clarify} | {_yes_no(item.recall_ok)} | "
                     f"{item.best_rank if item.best_rank is not None else '—'} | {_yes_no(item.tables_ok)} | {validation} |")
    lines += ["", "## 三、逐题明细（供人工判断）", ""]
    for item in outcomes:
        lines += _static_detail(item)
    return "\n".join(lines) + "\n"


def _static_detail(outcome: StaticOutcome) -> list[str]:
    question, final = outcome.question, outcome.results[-1]
    lines = [f"### {question.id}", ""]
    lines += [f"- 输入：{' → '.join(question.turns)}"]
    if question.note:
        lines.append(f"- 备注：{question.note}")
    lines.append(f"- 标准用表（任一组）：{'；'.join('、'.join(group) for group in question.gold_table_sets)}")
    validation = "没有走到校验" if outcome.validation_ok is None else "校验通过" if outcome.validation_ok else "校验未通过"
    lines.append(f"- 结果：状态 {final.status}；{validation}；反问 {outcome.clarify_count} 次")
    if final.status == "failed":
        lines.append(f"- 失败原因（停在 {final.failed_step}）：{_one_line(final.message)}")
    lines.append(f"- 完整需求：{final.requirement or '（没有）'}")
    if final.clarify is not None:
        options = "；".join(f"{option.label}（{option.meaning}）" if option.meaning else option.label
                           for option in final.clarify.options)
        lines.append(f"- 最后一次反问：{final.clarify.question}｜{options}")
    if final.assumptions:
        lines.append(f"- 假设：{'；'.join(final.assumptions)}")
    plan = final.plan
    if plan is not None:
        for label, text in (("口径说明", plan.caliber), ("用表依据", plan.table_basis), ("关联说明", plan.join_basis),
                            ("溯源说明", plan.lineage_note)):
            if text:
                lines.append(f"- {label}：{text}")
    if final.validation is not None:
        used = "、".join(final.validation.tables) or "（无）"
        lines.append(f"- SQL 用到的表：{used}")
        for text in final.validation.errors:
            lines.append(f"- 校验错误：{text}")
        for text in final.validation.warnings:
            lines.append(f"- 校验警告：{text}")
    lines.append("")
    if final.sql:
        lines += ["```sql", final.sql, "```", ""]
    return lines


# ═══════════════ 命令行 ═══════════════

def _unique_path(directory: Path, stem: str, suffixes: Sequence[str] = (".md",)) -> Path:
    """directory 下 stem + 第一个后缀的路径；有任一后缀的同名文件已存在时，依次试 stem_2、stem_3…"""
    number = 1
    while True:
        name = stem if number == 1 else f"{stem}_{number}"
        if not any((directory / f"{name}{suffix}").exists() for suffix in suffixes):
            return directory / f"{name}{suffixes[0]}"
        number += 1


def _build_pipeline(mart_dir: Path | None) -> object:
    """按资料目录装配评估用的流水线

    延迟导入 service：只有真的跑评估才加载模型相关的依赖。评估自己执行 SQL，流水线不执行。
    """
    from nl2sql import service

    return service.build_pipeline(mart_dir, executor_kind="none")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m eval.nl2sql_eval", description="NL2SQL 评估：模拟库三组对照，真实库静态评估")
    parser.add_argument("--configs", default="A,B,C", help="要跑的配置，逗号分隔（A 仅模型结构、B 加血缘、C 加血缘和样例）")
    parser.add_argument("--ids", default="", help="只跑这些题号，逗号分隔")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（先按题号选，再取前 N）")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="并发线程数")
    parser.add_argument("--today", default=EVAL_TODAY, help="固定的今天，YYYY-MM-DD")
    parser.add_argument("--resume", default="", help="接着上次跑：上次的逐题记录目录")
    parser.add_argument("--mart", default="", help="资料目录；模拟库评估默认用 data/mock_mart")
    parser.add_argument("--out", default="", help="输出目录；模拟库评估默认 eval/reports，真实库静态评估必须写")
    parser.add_argument("--static", default="", help="真实库静态评估：题目文件（YAML）")
    return parser


def _check_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """检查并解析参数，有问题时调用 parser.error（打印用法和原因，以退出码 2 退出）"""
    if args.static:
        if not args.out:
            parser.error("真实库静态评估必须写 --out（输出目录）：不给默认值，免得报告写进入库的目录")
        return
    if args.mart and not args.out:
        parser.error("给了 --mart 时必须写 --out：资料目录可能是真实库，报告不能写进入库的 eval/reports")
    configs = [item.strip().upper() for item in args.configs.split(",") if item.strip()]
    unknown = [config for config in configs if config not in CONFIGS]
    if not configs or unknown:
        parser.error(f"配置只能是 {'、'.join(CONFIGS)}，收到 {args.configs!r}" + (f"，不认识 {unknown}" if unknown else ""))
    args.config_list = list(dict.fromkeys(configs))
    try:
        args.today_date = date.fromisoformat(args.today)
    except ValueError:
        parser.error(f"--today 要写成 YYYY-MM-DD，收到 {args.today!r}")
    if args.workers < 1:
        parser.error("--workers 至少是 1")
    if args.limit < 0:
        parser.error("--limit 不能是负数")
    if args.resume and not Path(args.resume).is_dir():
        parser.error(f"--resume 的目录不存在: {args.resume}")
    questions = load_questions()
    wanted = [item.strip() for item in args.ids.split(",") if item.strip()]
    missing = [item for item in wanted if item not in {question.id for question in questions}]
    if missing:
        parser.error(f"题库里没有这些题号: {missing}")
    if wanted:
        questions = [question for question in questions if question.id in wanted]
    args.selected = questions[:args.limit] if args.limit else questions


def main(argv: list[str] | None = None) -> int:
    """命令行入口，返回退出码：0 成功，1 运行失败，2 参数不对"""
    # 输出走管道或重定向时 Windows 上是 GBK，打印不了「↔」这类字符；换成问号，不让评估中途崩掉
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        _check_args(parser, args)
    except SystemExit as stop:          # --help 和参数错误：argparse 直接退出进程，这里改成返回退出码
        return stop.code if isinstance(stop.code, int) else 0
    mart_dir = Path(args.mart) if args.mart else None
    try:
        if args.static:
            return _main_static(args, mart_dir)
        return _main_mock(args, mart_dir or MOCK_MART_DIR)
    except FileNotFoundError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1


def _main_static(args: argparse.Namespace, mart_dir: Path | None) -> int:
    questions = load_static_questions(Path(args.static))
    pipeline = _build_pipeline(mart_dir)
    report = run_static_eval(pipeline, questions, Path(args.out))
    print(f"静态评估完成，共 {len(questions)} 题")
    print(f"报告：{report}")
    print(f"逐题记录：{report.with_suffix('.jsonl')}")
    return 0


def _main_mock(args: argparse.Namespace, mart_dir: Path) -> int:
    from data.mock_mart.build_mock import DEFAULT_DB_PATH, ensure_built

    pipeline = _build_pipeline(mart_dir)
    ensure_built()
    executor = DuckDBExecutor(DEFAULT_DB_PATH)
    out_dir = Path(args.out) if args.out else REPORTS_DIR
    run_dir = Path(args.resume) if args.resume else out_dir / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    model = getattr(getattr(pipeline.deps, "llm", None), "name", "")
    print(f"逐题记录目录：{run_dir}")
    finished = [0]

    def show(outcome: QuestionOutcome) -> None:
        finished[0] += 1
        verdict = "对" if not outcome.attribution else f"错：{outcome.attribution}"
        print(f"[{finished[0]}] {outcome.config} {outcome.question_id} {verdict}", flush=True)

    summary = run_eval(pipeline, args.selected, args.config_list, store=pipeline.deps.store, executor=executor,
                       today=args.today_date, workers=args.workers, run_dir=run_dir, resume=bool(args.resume),
                       model=model, on_outcome=show)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = _unique_path(out_dir, f"{summary.started[:10]}_nl2sql_eval")
    report.write_text(render_report(summary, args.selected), encoding="utf-8")
    for config in args.config_list:
        metrics = compute_metrics([item for item in summary.outcomes if item.config == config])
        print(f"{_config_label(config)}：执行准确率 {_rate_text(metrics.exec_ok, metrics.sql_questions)}，"
              f"反问准确率 {_rate_text(metrics.clarify_ok, metrics.questions)}")
    print(f"报告：{report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
