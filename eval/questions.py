"""
评估题库
题目的结构定义与读取。题库文件是 YAML，标准 SQL 用块文本书写，便于阅读和审核
"""
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

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


class EvalQuestion(BaseModel):
    """一道评估题"""

    id: str
    category: str
    turns: list[str] = Field(min_length=1, max_length=2)    # 一轮或两轮用户输入
    expect: Literal["sql", "clarify"]                       # 期望得到 SQL，还是期望被反问
    gold_sql: str = ""                                      # 标准 SQL，目标库方言
    gold_tables: list[str] = Field(default_factory=list)    # 标准用表
    gold_columns: list[str] = Field(default_factory=list)   # 关键字段，写成 表.字段
    ordered: bool = False                                   # 结果是否要求行顺序

    @model_validator(mode="after")
    def _check_consistency(self) -> "EvalQuestion":
        has_sql = bool(self.gold_sql.strip())
        if self.expect == "sql" and not has_sql:
            raise ValueError(f"{self.id}: 期望得到 SQL 的题必须有标准 SQL")
        if self.expect == "clarify" and has_sql:
            raise ValueError(f"{self.id}: 期望被反问的题不应有标准 SQL")
        return self


def load_questions(path: Path = QUESTIONS_PATH) -> list[EvalQuestion]:
    """读取题库

    Args:
        path: 题库文件路径

    Returns:
        题目列表，顺序与文件一致

    Raises:
        ValueError: 题目编号重复，或题目内容不合规
    """
    items = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    questions = [EvalQuestion(**item) for item in items]
    ids = [question.id for question in questions]
    duplicated = sorted({item for item in ids if ids.count(item) > 1})
    if duplicated:
        raise ValueError(f"题目编号重复: {duplicated}")
    return questions
