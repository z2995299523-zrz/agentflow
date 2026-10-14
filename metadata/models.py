"""
元数据数据模型
一套专题库资料（表、字段、血缘、关联关系）在内存里的标准表示
"""
from pydantic import BaseModel, Field

# 允许的取值范围，模板说明和导入校验共用
LAYERS: tuple[str, ...] = ("贴源层", "整合层", "专题层")
# 快照表：每个数据日期一份全量，查询必须带日期条件；全量表：每次清空重装，只有当前状态
TABLE_TYPES: tuple[str, ...] = ("快照表", "流水表", "维表", "全量表")
CARDINALITIES: tuple[str, ...] = ("N:1", "1:1", "1:N")


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


class Relation(BaseModel):
    """两张表之间的关联关系"""

    left_table: str
    left_columns: list[str]
    right_table: str
    right_columns: list[str]
    cardinality: str                 # 对应关系
    origin: str = "manual"           # 来源，manual 表示关联关系清单里手工填写
    note: str = ""


class Mart(BaseModel):
    """一套完整的专题库资料"""

    tables: list[TableMeta] = Field(default_factory=list)
    columns: list[ColumnMeta] = Field(default_factory=list)
    lineage: list[LineageEdge] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
