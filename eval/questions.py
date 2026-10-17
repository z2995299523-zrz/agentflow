"""
评估题库
题目的结构定义与读取。题库文件是 YAML，标准 SQL 用块文本书写，便于阅读和审核
"""
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

QUESTIONS_PATH = Path(__file__).parent / "nl2sql_questions.yaml"
EVAL_TODAY = "2026-10-13"       # 评估时固定的「今天」，保证「上月末」这类相对日期可复现

# 每个类别的题数，题库变动时这里要同步修改
CATEGORY_COUNTS: dict[str, int] = {
    "基础时点余额": 8,
    "口径区分": 6,
    "维度关联": 6,
    "时间口径": 5,
    "向上溯源": 5,
    "反问": 5,
    "追问": 5,
}


class Alternative(BaseModel):
    """一道题的另一种正确写法

    例如机构汇总表能答的题，换成客户汇总表按机构 GROUP BY 也能得到同样的结果。
    结果必须与标准 SQL 一致、能通过静态校验，这两点由测试保证。
    """

    model_config = ConfigDict(extra="forbid")

    sql: str                                                # 另一种正确写法，目标库方言
    columns: list[str] = Field(default_factory=list)        # 这种写法的关键字段，写成 表.字段；空表示同 gold_columns

    @model_validator(mode="after")
    def _check_sql(self) -> "Alternative":
        if not self.sql.strip():
            raise ValueError("另一种写法的 SQL 不能为空")
        return self


class EvalQuestion(BaseModel):
    """一道评估题"""

    model_config = ConfigDict(extra="forbid")               # 字段名写错时报错，不悄悄忽略

    id: str
    category: str
    turns: list[str] = Field(min_length=1, max_length=2)    # 一轮或两轮用户输入
    expect: Literal["sql", "clarify"]                       # 期望得到 SQL，还是期望被反问
    gold_sql: str = ""                                      # 标准 SQL，目标库方言
    gold_tables: list[str] = Field(default_factory=list)    # 标准用表
    gold_columns: list[str] = Field(default_factory=list)   # 关键字段，写成 表.字段
    ordered: bool = False                                   # 结果是否要求行顺序
    alternatives: list[Alternative] = Field(default_factory=list)   # 其它正确写法，选表、字段按任一组算对

    @model_validator(mode="after")
    def _check_consistency(self) -> "EvalQuestion":
        has_sql = bool(self.gold_sql.strip())
        if self.expect == "sql" and not has_sql:
            raise ValueError(f"{self.id}: 期望得到 SQL 的题必须有标准 SQL")
        if self.expect == "clarify" and has_sql:
            raise ValueError(f"{self.id}: 期望被反问的题不应有标准 SQL")
        if self.expect == "clarify" and self.alternatives:
            raise ValueError(f"{self.id}: 期望被反问的题不应有其它写法")
        return self


def load_questions(path: Path = QUESTIONS_PATH) -> list[EvalQuestion]:
    """读取题库

    Args:
        path: 题库文件路径

    Returns:
        题目列表，顺序与文件一致

    Raises:
        ValueError: 文件是空的，顶层不是题目列表，某一条不是键值对，题目编号重复，或题目内容不合规
    """
    items = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if items is None:
        raise ValueError(f"题库文件是空的: {path}")
    if not isinstance(items, list):
        raise ValueError(f"题库文件的顶层必须是题目列表，实际是 {type(items).__name__}: {path}")
    not_questions = [number for number, item in enumerate(items, 1) if not isinstance(item, dict)]
    if not_questions:
        raise ValueError(f"题库文件里第 {not_questions} 条不是一道题（应为键值对）: {path}")
    questions = [EvalQuestion(**item) for item in items]
    ids = [question.id for question in questions]
    duplicated = sorted({item for item in ids if ids.count(item) > 1})
    if duplicated:
        raise ValueError(f"题目编号重复: {duplicated}")
    return questions
