"""
元数据数据模型
一套专题库资料（表、字段、血缘、关联关系、加工脚本、样例）在内存里的标准表示
"""
from pydantic import BaseModel, ConfigDict, Field

# 允许的取值范围，模板说明和导入校验共用
LAYERS: tuple[str, ...] = ("贴源层", "整合层", "专题层")
# 快照表：每个数据日期一份全量，查询必须带日期条件；全量表：每次清空重装，只有当前状态
TABLE_TYPES: tuple[str, ...] = ("快照表", "流水表", "维表", "全量表")
# 关联关系清单里手工填写时允许的对应关系
CARDINALITIES: tuple[str, ...] = ("N:1", "1:1", "1:N")
# 推导出的关联另有两种：两侧都不唯一，或者缺粒度字段无法判断
DERIVED_CARDINALITIES: tuple[str, ...] = CARDINALITIES + ("N:N", "未知")
# 血缘边的来源：list 血缘清单；script 从加工脚本解析补上
LINEAGE_ORIGINS: tuple[str, ...] = ("list", "script")
# 关联关系的来源，优先级从高到低：手工、脚本里出现过、血缘传递、同名粒度键
RELATION_ORIGINS: tuple[str, ...] = ("manual", "script", "lineage", "grain_key")
# 加工脚本里目标字段的表达式类别：直取、加工、恒为空、常量、加工时间、追溯不到来源
COLUMN_KINDS: tuple[str, ...] = ("passthrough", "derived", "null", "constant", "etl_time", "unresolved")


class FieldRef(BaseModel):
    """一个字段的引用，可以放进集合、当字典的键"""

    model_config = ConfigDict(frozen=True)

    table: str                       # 表标识：模式名.表英文名
    column: str


class TableMeta(BaseModel):
    """一张表的元数据"""

    table_id: str                    # 唯一标识：模式名.表英文名，全小写
    layer: str                       # 层级
    schema_name: str
    table_name: str
    table_cn: str
    table_type: str                  # 表类型
    grain_columns: list[str]         # 粒度字段：能唯一确定一行的字段，不清楚时为空列表
    date_column: str | None = None   # 数据日期字段，快照表必填
    description: str = ""


class ColumnMeta(BaseModel):
    """一个字段的元数据"""

    table_id: str
    column_name: str
    ordinal: int                     # 字段在表内的顺序，从 1 开始
    column_cn: str
    data_type: str = ""              # 数据类型，不清楚时为空
    is_pk: bool = False
    description: str = ""            # 口径说明
    enum_values: str = ""            # 码值说明


class LineageEdge(BaseModel):
    """一条字段级血缘：源字段 → 目标字段"""

    target_table: str
    target_column: str
    source_table: str
    source_column: str
    transform: str = ""              # 加工逻辑
    script_name: str = ""            # 加工脚本文件名
    source_external: bool = False    # 源表不在表清单里（外部源）
    is_passthrough: bool = False     # 直取：加工表达式只是对源字段的直接引用
    origin: str = "list"             # 来源，取值见 LINEAGE_ORIGINS


class Relation(BaseModel):
    """两张表之间的关联关系"""

    left_table: str
    left_columns: list[str]
    right_table: str
    right_columns: list[str]
    cardinality: str                 # 对应关系，取值见 DERIVED_CARDINALITIES
    origin: str = "manual"           # 来源，取值见 RELATION_ORIGINS；manual 表示关联关系清单里手工填写
    note: str = ""                   # 说明；推导出的关联在这里写依据


class ScriptFilter(BaseModel):
    """加工语句里某一查询层的过滤条件"""

    scope: str                       # 所在查询层：main、CTE 名或子查询的编号
    clause: str                      # WHERE 或 HAVING
    condition: str                   # 条件原文，别名已换成实体表全名


class JoinKey(BaseModel):
    """一对等值关联键，两侧都已追溯到实体表的字段"""

    left: FieldRef
    right: FieldRef


class ScriptJoin(BaseModel):
    """加工语句里的一处关联"""

    scope: str                       # 所在查询层：main、CTE 名或子查询的编号
    join_type: str                   # LEFT、INNER、JOIN；写在 WHERE 里的等值记为 IMPLICIT
    table: str                       # 被关联的实体表全名，或 CTE 名
    condition: str                   # 关联条件原文，别名已换成实体表全名
    keys: list[JoinKey] = Field(default_factory=list)   # 能追溯到实体表字段的等值键对


class ScriptStatement(BaseModel):
    """加工脚本里一条往目标表写数的语句，即这张表的脚本片段"""

    script_name: str                 # 脚本文件名
    stmt_index: int                  # 文件内第几条非空语句，从 1 开始
    target_table: str                # 目标表全名；解析失败时用正则取，取不到为空
    sql_text: str                    # 语句原文，前面拼上它引用的临时表的建表语句
    select_sql: str = ""             # 临时表改写成 CTE 之后的自包含查询
    parsed: bool                     # 是否结构化解析成功；失败时只有原文
    filters: list[ScriptFilter] = Field(default_factory=list)
    joins: list[ScriptJoin] = Field(default_factory=list)


class ScriptColumn(BaseModel):
    """加工语句里一个目标字段的表达式"""

    script_name: str
    stmt_index: int
    target_table: str
    target_column: str
    expression: str                  # CTE、子查询、临时表展开后的表达式，实体表写全名，最长 1500 字符
    sources: list[FieldRef] = Field(default_factory=list)   # 追溯到的实体表字段
    kind: str                        # 表达式类别，取值见 COLUMN_KINDS
    # 追溯这个字段时经过的查询层名，写法与 ScriptFilter.scope、ScriptJoin.scope 一致（main、CTE 名、子查询编号）。
    # 空表示不知道（旧数据，或者追溯不到），按这条语句的全部条件处理
    scopes: list[str] = Field(default_factory=list)


class Example(BaseModel):
    """一条历史查数样例"""

    example_id: str                  # 文件名#序号
    requirement: str                 # 需求
    caliber_note: str = ""           # 口径说明
    sql_text: str
    tables: list[str] = Field(default_factory=list)          # 实际用到的表，取自静态校验报告
    source_file: str
    line_no: int                     # 「-- 需求:」所在的行号


class Mart(BaseModel):
    """一套完整的专题库资料"""

    tables: list[TableMeta] = Field(default_factory=list)
    columns: list[ColumnMeta] = Field(default_factory=list)
    lineage: list[LineageEdge] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    scripts: list[ScriptStatement] = Field(default_factory=list)
    script_columns: list[ScriptColumn] = Field(default_factory=list)
    examples: list[Example] = Field(default_factory=list)
