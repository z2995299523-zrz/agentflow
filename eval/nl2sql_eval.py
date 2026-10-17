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
"""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from eval.questions import EvalQuestion
from metadata.store import MetadataStore
from nl2sql.executor import Executor
from nl2sql.schemas import QueryRequest, QueryResult

CONFIGS: dict[str, tuple[bool, bool]] = {"A": (False, False), "B": (True, False), "C": (True, True)}  # (use_lineage, use_examples)
CONFIG_NAMES = {"A": "仅模型结构", "B": "加血缘", "C": "加血缘和样例"}
EVAL_MAX_ROWS = 100_000
EVAL_TIMEOUT_S = 30
DEFAULT_WORKERS = 4
REPORTS_DIR = Path(__file__).parent / "reports"


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


@dataclass
class EvalSummary:
    """一次评估的全部结果"""

    outcomes: list[QuestionOutcome]
    started: str
    finished: str
    model: str
    today: str


def evaluate_question(pipeline: PipelineLike, question: EvalQuestion, config: str, executor: Executor,
                      today: date, *, store: MetadataStore) -> QuestionOutcome:
    """跑一道题并计分，不抛异常

    Args:
        pipeline: 流水线
        question: 题目
        config: A、B、C 之一
        executor: 执行生成的 SQL 和标准 SQL 用的执行层
        today: 固定的「今天」
        store: 元数据库，算标准答案组的用表

    Returns:
        这道题的结果
    """
    raise NotImplementedError("由 W1-C 实现")


def run_eval(pipeline: PipelineLike, questions: Sequence[EvalQuestion], configs: Sequence[str], *, store: MetadataStore,
             executor: Executor, today: date, workers: int = DEFAULT_WORKERS, run_dir: Path, resume: bool = False,
             model: str = "") -> EvalSummary:
    """并发跑全部题，每题跑完立刻追加到 run_dir/{config}.jsonl；resume 时跳过已有的（配置, 题号）"""
    raise NotImplementedError("由 W1-C 实现")


def render_report(summary: EvalSummary, questions: Sequence[EvalQuestion]) -> str:
    """评估报告（Markdown）：头部信息、三组对比、贡献、分类别、执行失败、错题明细、局限"""
    raise NotImplementedError("由 W1-C 实现")


class StaticQuestion(BaseModel):
    """真实库静态评估的一道题"""

    id: str
    turns: list[str]
    today: date
    gold_table_sets: list[list[str]]          # 任一组被 SQL 用到的表包含即算选对
    clarify_answer: str = ""                  # 流水线反问时自动回答一次用的口径
    note: str = ""


def load_static_questions(path: Path) -> list[StaticQuestion]:
    """读静态评估的题目文件（YAML 列表）"""
    raise NotImplementedError("由 W1-C 实现")


def run_static_eval(pipeline: PipelineLike, questions: Sequence[StaticQuestion], out_dir: Path) -> Path:
    """跑静态评估，报告和逐题记录写到 out_dir，返回报告路径"""
    raise NotImplementedError("由 W1-C 实现")


def main(argv: list[str] | None = None) -> int:
    """命令行入口，返回退出码"""
    raise NotImplementedError("由 W1-C 实现")


if __name__ == "__main__":
    raise SystemExit(main())
