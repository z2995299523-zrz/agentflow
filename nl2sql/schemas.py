"""
查询流水线各步骤的输入输出结构
流水线：① 需求解析 → ② 候选召回 → ③ 证据组装 → ④ 选表定口径 → ⑤ 生成 SQL → ⑥ 静态校验 → ⑦ 输出。
这里的结构是各模块之间的契约，写定后冻结：要改先改契约，再改实现。

流水线本身无状态：追问、反问的上下文由调用方放进 QueryRequest 传进来，
上一轮结果里的 requirement、clarify 就是下一轮的 prev_requirement、prev_clarify；
上一轮校验通过时，它的 sql 和 validation.tables 就是下一轮的 prev_sql、prev_tables（见 QueryResult.next_context）。
"""
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

Status = Literal["ok", "need_clarify", "unvalidated", "failed"]
Decision = Literal["ok", "need_clarify", "need_upstream", "cannot_answer"]
STEPS: tuple[str, ...] = ("parse", "recall", "evidence", "plan", "generate", "validate", "output")
STEP_LABELS: dict[str, str] = {
    "parse": "① 需求解析", "recall": "② 候选召回", "evidence": "③ 血缘展开", "plan": "④ 选表定口径",
    "generate": "⑤ 生成 SQL", "validate": "⑥ 静态校验", "output": "⑦ 输出",
}
INTERNAL_STEP = "internal"                    # 节点里没接住的程序错误


# ── 请求 ──────────────────────────────────────

class ClarifyOption(BaseModel):
    """反问的一个选项"""

    label: str                                # 选项，例如「不含保证金」
    meaning: str = ""                         # 含义，例如「用存款余额字段，加工时剔除了保证金账户」


class Clarify(BaseModel):
    """一次反问"""

    question: str
    options: list[ClarifyOption] = Field(default_factory=list)


class QueryRequest(BaseModel):
    """一轮查询请求"""

    question: str                             # 本轮输入
    prev_requirement: str = ""                # 上一轮的完整需求
    prev_clarify: Clarify | None = None       # 上一轮提出、还没回答的反问；本轮是在回答它还是问了新问题，由 ① 判断
    prev_sql: str = ""                        # 上一轮校验通过的 SQL；① 判断本轮是在上一轮上追问时，④ ⑤ 以它为底增量修改
    prev_tables: list[str] = Field(default_factory=list)      # 上一轮 SQL 用到的表；追问时直接放进候选
    today: date | None = None                 # None 时用系统日期
    use_lineage: bool = True                  # 对照开关：A 组 False/False，B 组 True/False，C 组 True/True
    use_examples: bool = True


# ── ① 需求解析 ────────────────────────────────

class Metric(BaseModel):
    """一个指标"""

    name: str                                 # 业务叫法，不带日期和取值：「对公贷款余额」「立项项目数」
    entity: str = ""                          # 计数类指标被计数的实体：「项目」；不是计数类时为空


class FilterItem(BaseModel):
    """一个过滤要素"""

    field: str                                # 字段的叫法：「行业」「项目开始日期」
    condition: str = ""                       # 条件：「= 制造业」「在 2026 年内」「大于 0」


class TimeSpec(BaseModel):
    """时间要素，相对日期已按「今天」换算成具体日期"""

    kind: Literal["none", "point", "range", "daily_avg"] = "none"   # 不限（当前状态）、时点、区间、日均
    dates: list[str] = Field(default_factory=list)   # YYYY-MM-DD；point 和 daily_avg 一个，range 两个（起、止，含两端），none 没有
    compare: Literal["none", "prev_year_end", "prev_month_end", "other"] = "none"   # 较年初（对比上年末）、较上月末
    compare_dates: list[str] = Field(default_factory=list)
    note: str = ""                            # 原文说法与换算：「9 月末 → 2026-09-30」


class ParsedRequest(BaseModel):
    """① 需求解析的结果"""

    is_query: bool
    reply: str = ""                           # 不是查数需求时给用户的回复，说明工具能做什么
    answers_clarify: bool = False             # 本轮输入是不是在回答上一轮的反问；是回答时 ① ④ 都不能再反问，
                                              # 没理会反问、问了新问题时照常能反问。没有上一轮的反问时一律为 False
    follows_up: bool = False                  # 本轮是不是在上一轮的完整需求上追问（增加、修改）；是的话 ② ④ ⑤
                                              # 带上上一轮的 SQL 和表增量修改。没有上一轮的需求时一律为 False
    requirement: str = ""                     # 完整需求复述，一句自包含的话
    metrics: list[Metric] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[FilterItem] = Field(default_factory=list)
    order: str = ""                           # 排序与条数：「按余额降序取前 10」
    time: TimeSpec = Field(default_factory=TimeSpec)
    caliber_hints: list[str] = Field(default_factory=list)    # 原文里的口径限定词：「不含保证金」「折人民币」
    sub_questions: list[str] = Field(default_factory=list)    # 一句话里有几个问题时逐个列出；只有一个时为空
    assumptions: list[str] = Field(default_factory=list)      # 按默认处理的地方：「「最新」按上一自然日 2026-10-12」
    clarify: Clarify | None = None            # 是查数需求、但缺了没法默认的要素（哪个对象、看哪些指标）时的反问；口径分岔归 ④
                                              # 在回答上一轮的反问（answers_clarify 为真）时不能有


# ── ③ 证据 ────────────────────────────────────

class EvidenceColumn(BaseModel):
    """证据里的一个字段"""

    name: str
    cn: str
    data_type: str = ""
    is_pk: bool = False
    description: str = ""                     # 已按证据组装的规则截断
    enum_values: str = ""
    kind: str = ""                            # 加工脚本里的表达式类别（COLUMN_KINDS）；A 组和没有脚本时为空
    hit_terms: list[str] = Field(default_factory=list)        # 命中这个字段的检索词
    name_column: str = ""                     # 代码字段在同表的名称字段（industry_cd → industry_nm），见 metadata.codes
    code_table: str = ""                      # 同表没有名称字段时，证据里码值表的名称字段：「模式.表.字段」


class EvidenceTable(BaseModel):
    """证据里的一张候选表"""

    table_id: str
    table_cn: str
    layer: str
    table_type: str
    grain_columns: list[str] = Field(default_factory=list)
    date_column: str | None = None
    description: str = ""
    # join：关联路径经过、证据组装时补进来的中间表；previous：追问时上一轮 SQL 用到的表；
    # missing：④ 说缺东西时，拿缺的每一样单独在第 0 层召回到的表
    reason: Literal["recall", "upstream", "example", "join", "previous", "missing"]
    score: float = 0.0
    columns: list[EvidenceColumn] = Field(default_factory=list)


class LineageStep(BaseModel):
    """字段加工链路里的一跳，同一个目标字段的来源归并在一起"""

    depth: int                                # 第几跳，从 1 开始
    target: str                               # 模式.表.字段
    sources: list[str]                        # 模式.表.字段，按血缘清单顺序
    unknown_sources: list[str] = Field(default_factory=list)  # sources 里不在字段清单里的（含外部表的），SQL 里不能引用
    expression: str = ""                      # 脚本表达式，没有时用血缘清单的加工逻辑
    filters: list[str] = Field(default_factory=list)          # 「WHERE 条件」「HAVING 条件」
    joins: list[str] = Field(default_factory=list)            # 「LEFT JOIN 表 ON 条件」
    script_name: str = ""
    origin: str = "list"


class FieldLineage(BaseModel):
    """一个指标命中字段的上游链路"""

    field: str                                # 模式.表.字段
    term: str                                 # 命中它的指标检索词
    steps: list[LineageStep] = Field(default_factory=list)
    truncated: bool = False


class FieldComparison(BaseModel):
    """同一个指标命中的两个字段的同源对比"""

    term: str
    a: str
    b: str                                    # derived_from 时 b 由 a 加工而来
    relation: Literal["derived_from", "same_source_diff", "same_source_same"]
    common: list[str] = Field(default_factory=list)
    note: str = ""


class TableLink(BaseModel):
    """两张候选表之间的上下游关系"""

    upstream: str
    downstream: str                           # downstream 由 upstream 加工而来（可能隔了几层）


class JoinHop(BaseModel):
    """关联路径里的一步"""

    left_table: str
    left_columns: list[str]
    right_table: str
    right_columns: list[str]
    cardinality: str                          # 按行进方向：N:1、1:1、1:N、N:N、未知
    origin: str                               # manual、script、lineage、grain_key
    note: str = ""


class JoinHint(BaseModel):
    """两张候选表之间的关联路径"""

    left_table: str
    right_table: str
    hops: list[JoinHop]                       # 一步是直连，两步经过一张中间表
    fan_out: bool


class UpstreamOption(BaseModel):
    """还可以上溯到的一张表"""

    table_id: str
    table_cn: str
    layer: str


class EvidenceExample(BaseModel):
    """一条相似样例"""

    example_id: str
    requirement: str
    caliber_note: str = ""
    sql_text: str
    tables: list[str] = Field(default_factory=list)


class Evidence(BaseModel):
    """③ 证据组装的结果，④ ⑤ 只能用这里面的表和字段"""

    use_lineage: bool
    use_examples: bool
    all_layers: bool = False
    scope_level: int = 0
    tables: list[EvidenceTable] = Field(default_factory=list)
    lineage: list[FieldLineage] = Field(default_factory=list)
    comparisons: list[FieldComparison] = Field(default_factory=list)
    table_links: list[TableLink] = Field(default_factory=list)
    joins: list[JoinHint] = Field(default_factory=list)
    upstream_options: list[UpstreamOption] = Field(default_factory=list)
    examples: list[EvidenceExample] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def table(self, table_id: str) -> EvidenceTable | None:
        """按表标识取证据里的一张表，不在证据里时返回 None"""
        for table in self.tables:
            if table.table_id == table_id:
                return table
        return None

    def table_ids(self) -> list[str]:
        """证据里全部表的标识，按证据里的顺序"""
        return [table.table_id for table in self.tables]

    def column_names(self, table_id: str) -> set[str]:
        """一张表在证据里的字段名；不在证据里的表返回空集合"""
        table = self.table(table_id)
        return {column.name for column in table.columns} if table is not None else set()


# ── ④ 查询方案 ────────────────────────────────

class PlanTable(BaseModel):
    """方案用到的一张表"""

    table: str
    reason: str = ""


class PlanField(BaseModel):
    """方案用到的一个字段"""

    table: str
    column: str
    usage: Literal["metric", "dimension", "filter", "join", "order", "other"] = "other"
    reason: str = ""


class PlanJoin(BaseModel):
    """方案里的一个关联"""

    left_table: str
    left_columns: list[str]
    right_table: str
    right_columns: list[str]
    join_type: Literal["INNER", "LEFT"] = "INNER"
    reason: str = ""


class QueryPlan(BaseModel):
    """④ 选表定口径的结果"""

    decision: Decision
    tables: list[PlanTable] = Field(default_factory=list)
    fields: list[PlanField] = Field(default_factory=list)
    joins: list[PlanJoin] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)          # SQL 写法：「b.data_dt = DATE '2026-09-30'」
    caliber: str = ""                         # 口径说明，引用血缘表达式
    table_basis: str = ""                     # 用表依据
    join_basis: str = ""                      # 关联说明：键、对应关系、为什么不发散
    lineage_note: str = ""                    # 溯源说明：上溯时还原了哪些加工条件
    assumptions: list[str] = Field(default_factory=list)
    examples_used: list[str] = Field(default_factory=list)
    clarify: Clarify | None = None            # need_clarify
    missing: list[str] = Field(default_factory=list)          # need_upstream：缺什么，写成字段的叫法
    reason: str = ""                          # cannot_answer 的原因；need_upstream 的补充说明
    dropped_tables: list[str] = Field(default_factory=list)   # 代码精简时去掉的表（prune_plan），⑤ 不能再用，模型不填


# ── 记录与结果 ────────────────────────────────

class CallRecord(BaseModel):
    """一次模型调用的记录"""

    step: str
    attempt: int                              # 第几次尝试，从 1 开始
    elapsed_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    ok: bool
    error: str = ""                           # 调用失败或输出不合格的原因
    messages: list[dict[str, str]] = Field(default_factory=list)
    reply: str = ""


class TraceStep(BaseModel):
    """流水线走过的一步"""

    step: str
    label: str                                # STEP_LABELS，上溯时加「（上溯第 1 层）」，修正时加「（第 2 次）」
    elapsed_ms: int
    summary: str = ""                         # 只含元数据，不含执行结果里的数据


class ValidationSummary(BaseModel):
    """静态校验结果的摘要"""

    ok: bool
    errors: list[str] = Field(default_factory=list)           # 「R4：…」
    warnings: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)


class ExecSummary(BaseModel):
    """执行结果的摘要"""

    executed: bool
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: int = 0
    error: str = ""
    note: str = ""


class QueryResult(BaseModel):
    """一轮查询的结果"""

    status: Status
    question: str
    metadata_version: str | None = None       # 本轮使用的元数据版本号；旧结果和内存元数据为 None
    requirement: str = ""                     # 调用方追问时把它作为下一轮的 prev_requirement
    parsed: ParsedRequest | None = None
    sql: str = ""                             # 排版后的 SQL；need_clarify、failed 时为空
    plan: QueryPlan | None = None
    clarify: Clarify | None = None
    message: str = ""                         # failed 的原因，或者不是查数需求时的回复
    failed_step: str = ""                     # STEPS 之一或 INTERNAL_STEP
    assumptions: list[str] = Field(default_factory=list)      # ① 与 ④ 合并去重
    validation: ValidationSummary | None = None
    first_pass_ok: bool | None = None         # 第一次生成的 SQL 是否通过校验；没走到校验为 None
    repair_rounds: int = 0
    scope_level: int = 0
    candidate_tables: list[str] = Field(default_factory=list) # 最后一次证据里的全部表（含上溯、样例、关联补进来的）
    recall_notes: list[str] = Field(default_factory=list)
    exec_result: ExecSummary | None = None
    trace: list[TraceStep] = Field(default_factory=list)
    calls: list[CallRecord] = Field(default_factory=list)
    elapsed_ms: int = 0

    @property
    def llm_calls(self) -> int:
        """模型调用次数，每次尝试算一次"""
        return len(self.calls)

    def public(self) -> dict[str, Any]:
        """给界面和接口用的字典：调用记录里去掉提示词和回复，其余原样

        Returns:
            model_dump(mode="json") 的结果，calls 里每条没有 messages 和 reply
        """
        data = self.model_dump(mode="json")
        for call in data["calls"]:
            call.pop("messages", None)
            call.pop("reply", None)
        return data

    def next_context(self) -> dict[str, Any]:
        """下一轮要带给 QueryRequest 的上下文：完整需求；停在反问上时带反问；校验通过时带 SQL 和它用到的表

        只有 status 是 ok 的 SQL 才带：校验没过的 SQL 不能当下一轮的底稿。
        """
        context: dict[str, Any] = {"prev_requirement": self.requirement}
        if self.status == "need_clarify" and self.clarify is not None:
            context["prev_clarify"] = self.clarify
        if self.status == "ok" and self.sql:
            context["prev_sql"] = self.sql
            context["prev_tables"] = list(self.validation.tables) if self.validation else []
        return context
