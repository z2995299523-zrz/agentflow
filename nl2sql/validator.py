"""
静态校验
不执行 SQL，用 sqlglot 解析后对照元数据检查。错误必须修正，警告展示给人判断：
    R1 按目标方言能解析，且只有一条语句          错误
    R2 只能是查询语句                            错误
    R3 每张表都存在于元数据                      错误
    R4 每个字段都存在于它所属的表                错误
    R5 快照表在所在查询层带数据日期条件          错误
    R6 表之间的等值关联属于已知关联关系          警告
    R7 汇总查询里被关联的表按粒度字段全部约束    警告
    R8 实际使用的表和字段与查询方案一致          警告

骨架：接口已定，实现由第二份计划的 B 任务补上
"""
from collections.abc import Collection
from dataclasses import dataclass, field

from metadata.checks import ERROR, WARNING
from metadata.store import MetadataStore


@dataclass(frozen=True)
class ValidationIssue:
    """一条校验问题"""

    rule: str                    # R1 到 R8
    level: str                   # 错误 / 警告，取值同 metadata.checks
    message: str


@dataclass
class ValidationReport:
    """一条 SQL 的校验报告"""

    issues: list[ValidationIssue] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)     # 实际用到的实体表
    columns: list[str] = field(default_factory=list)    # 实际用到的字段，写成「模式.表.字段」

    @property
    def errors(self) -> list[ValidationIssue]:
        """错误级问题"""
        return [issue for issue in self.issues if issue.level == ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        """警告级问题"""
        return [issue for issue in self.issues if issue.level == WARNING]

    @property
    def ok(self) -> bool:
        """没有错误即为通过，警告不影响"""
        return not self.errors


class SqlValidator:
    """对照元数据做静态校验"""

    def __init__(self, store: MetadataStore, dialect: str = "postgres") -> None:
        """
        Args:
            store: 元数据
            dialect: 生成 SQL 使用的方言
        """
        self.store = store
        self.dialect = dialect

    def validate(self, sql: str, expected_tables: Collection[str] | None = None,
                 expected_columns: Collection[str] | None = None) -> ValidationReport:
        """校验一条 SQL

        Args:
            sql: 待校验的 SQL
            expected_tables: 查询方案里的表，传入时检查 R8
            expected_columns: 查询方案里的字段（模式.表.字段），传入时检查 R8

        Returns:
            校验报告
        """
        raise NotImplementedError
