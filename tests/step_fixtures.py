"""
三个模型步骤（需求解析、选表定口径、生成 SQL）与提示词的测试夹具
用模拟专题库里真实存在的表和字段，手工构造证据、需求解析结果和查询方案：
证据组装由另一个模块实现，这里的测试不依赖它。
"""
from nl2sql.schemas import (
    Evidence,
    EvidenceColumn,
    EvidenceExample,
    EvidenceTable,
    FieldComparison,
    FieldLineage,
    JoinHint,
    JoinHop,
    LineageStep,
    Metric,
    ParsedRequest,
    PlanField,
    PlanJoin,
    PlanTable,
    QueryPlan,
    TableLink,
    TimeSpec,
    UpstreamOption,
)

ORG_BAL = "ads.org_corp_dep_bal_d"          # 机构对公存款余额日汇总
CUST_BAL = "ads.corp_cust_dep_bal_d"        # 对公客户存款余额日汇总
DIM_ORG = "dim.org"                         # 机构维表
DWD_ACCT = "dwd.corp_dep_acct_d"            # 对公存款账户日快照（标准层）
EXAMPLE_ID = "01_存款余额.sql#2"
EXAMPLE_SQL = """SELECT o.lvl2_org_nm,
       SUM(CASE WHEN b.data_dt = DATE '2026-06-30' THEN b.dep_bal_tot_rmb ELSE 0 END)
     - SUM(CASE WHEN b.data_dt = DATE '2025-12-31' THEN b.dep_bal_tot_rmb ELSE 0 END) AS dep_bal_tot_inc
FROM ads.org_corp_dep_bal_d b
JOIN dim.org o ON b.org_no = o.org_no
WHERE b.data_dt IN (DATE '2026-06-30', DATE '2025-12-31')
GROUP BY o.lvl2_org_nm
ORDER BY dep_bal_tot_inc DESC"""


def evidence_table(store, table_id: str, *, reason: str = "recall", score: float = 1.0,
                   hits: dict[str, list[str]] | None = None, kinds: dict[str, str] | None = None) -> EvidenceTable:
    """按元数据里真实的表和字段造一张证据表

    Args:
        store: 元数据（模拟库）
        table_id: 表标识
        reason: 入选原因
        score: 召回得分
        hits: 字段名 → 命中它的检索词
        kinds: 字段名 → 加工脚本里的表达式类别
    """
    meta = store.table(table_id)
    assert meta is not None, table_id
    hits, kinds = hits or {}, kinds or {}
    columns = [
        EvidenceColumn(name=c.column_name, cn=c.column_cn, data_type=c.data_type, is_pk=c.is_pk,
                       description=c.description, enum_values=c.enum_values, kind=kinds.get(c.column_name, ""),
                       hit_terms=list(hits.get(c.column_name, [])))
        for c in store.columns(table_id)
    ]
    return EvidenceTable(table_id=table_id, table_cn=meta.table_cn, layer=meta.layer, table_type=meta.table_type,
                         grain_columns=list(meta.grain_columns), date_column=meta.date_column,
                         description=meta.description, reason=reason, score=score, columns=columns)


def make_evidence(store, *, use_lineage: bool = True, use_examples: bool = True, scope_level: int = 0,
                  all_layers: bool = False) -> Evidence:
    """一份像样的证据：存款余额类需求，候选是机构汇总表、客户汇总表、机构维表

    use_lineage 为假时没有血缘、对比、上下游、上溯选项，字段也没有加工类别（A 组）；
    use_examples 为真时带一条样例（C 组）。
    """
    kinds = {"dep_bal_rmb": "derived", "dep_bal_tot_rmb": "derived"} if use_lineage else {}
    tables = [
        evidence_table(store, ORG_BAL, score=0.9, kinds=kinds,
                       hits={"dep_bal_rmb": ["对公存款余额"], "dep_bal_tot_rmb": ["对公存款余额"], "org_no": ["机构"]}),
        evidence_table(store, CUST_BAL, score=0.8, kinds=kinds,
                       hits={"dep_bal_rmb": ["对公存款余额"], "dep_bal_tot_rmb": ["对公存款余额"]}),
        evidence_table(store, DIM_ORG, score=0.5, hits={"lvl1_org_nm": ["一级分行名称"], "org_no": ["机构"]}),
    ]
    evidence = Evidence(
        use_lineage=use_lineage, use_examples=use_examples, all_layers=all_layers, scope_level=scope_level,
        tables=tables,
        joins=[JoinHint(left_table=ORG_BAL, right_table=DIM_ORG, fan_out=False,
                        hops=[JoinHop(left_table=ORG_BAL, left_columns=["org_no"], right_table=DIM_ORG,
                                      right_columns=["org_no"], cardinality="N:1", origin="grain_key")])],
        notes=["关键字路：向量器不一致，只走了关键字路"],
    )
    if use_lineage:
        evidence.lineage = [
            FieldLineage(field=f"{ORG_BAL}.dep_bal_rmb", term="对公存款余额", steps=[
                LineageStep(depth=1, target=f"{ORG_BAL}.dep_bal_rmb", sources=[f"{CUST_BAL}.dep_bal_rmb"],
                            expression=f"SUM({CUST_BAL}.dep_bal_rmb)",
                            filters=[f"WHERE {CUST_BAL}.data_dt = CAST('${{data_dt}}' AS DATE)"],
                            script_name="22_ads_org_corp_dep_bal_d.sql", origin="list"),
                LineageStep(depth=2, target=f"{CUST_BAL}.dep_bal_rmb",
                            sources=[f"{DWD_ACCT}.bal_rmb", f"{DWD_ACCT}.is_margin"],
                            expression=f"SUM(CASE WHEN {DWD_ACCT}.is_margin = '0' THEN {DWD_ACCT}.bal_rmb ELSE 0 END)",
                            filters=[f"WHERE {DWD_ACCT}.data_dt = CAST('${{data_dt}}' AS DATE)"],
                            joins=[f"JOIN dim.corp_cust ON {DWD_ACCT}.cust_no = dim.corp_cust.cust_no"],
                            script_name="21_ads_corp_cust_dep_bal_d.sql", origin="list"),
            ]),
        ]
        evidence.comparisons = [
            FieldComparison(term="对公存款余额", a=f"{ORG_BAL}.dep_bal_rmb", b=f"{ORG_BAL}.dep_bal_tot_rmb",
                            relation="same_source_diff", common=[f"{CUST_BAL}.dep_bal_rmb"],
                            note="dep_bal_rmb 只汇总非保证金账户，dep_bal_tot_rmb 汇总全部账户"),
            FieldComparison(term="对公存款余额", a=f"{CUST_BAL}.dep_bal_rmb", b=f"{ORG_BAL}.dep_bal_rmb",
                            relation="derived_from"),
        ]
        evidence.table_links = [TableLink(upstream=CUST_BAL, downstream=ORG_BAL)]
        evidence.upstream_options = [UpstreamOption(table_id=DWD_ACCT, table_cn="对公存款账户日快照", layer="标准层")]
    if use_examples:
        evidence.examples = [
            EvidenceExample(example_id=EXAMPLE_ID, requirement="2026 年 6 月末各二级分行含保证金存款比年初多了多少",
                            caliber_note="含保证金用全口径存款余额 dep_bal_tot_rmb；年初指上年末（2025-12-31）",
                            sql_text=EXAMPLE_SQL, tables=[ORG_BAL, DIM_ORG]),
        ]
    return evidence


def make_parsed(**changes) -> ParsedRequest:
    """一份合格的需求解析结果：9 月末各一级分行不含保证金的对公存款余额；changes 覆盖其中的字段"""
    parsed = ParsedRequest(
        is_query=True,
        requirement="查询 2026-09-30 各一级分行不含保证金的对公存款余额，列出一级分行名称和余额",
        metrics=[Metric(name="对公存款余额")],
        dimensions=["一级分行名称"],
        time=TimeSpec(kind="point", dates=["2026-09-30"], note="9 月末 → 2026-09-30"),
        caliber_hints=["不含保证金"],
    )
    return parsed.model_copy(update=changes)


def make_plan(**changes) -> QueryPlan:
    """一份合格的查询方案（对应 make_parsed 和 make_evidence）；changes 覆盖其中的字段"""
    plan = QueryPlan(
        decision="ok",
        tables=[PlanTable(table=ORG_BAL, reason="需求只到机构粒度，机构汇总表已满足"),
                PlanTable(table=DIM_ORG, reason="取一级分行名称")],
        fields=[
            PlanField(table=ORG_BAL, column="dep_bal_rmb", usage="metric", reason="不含保证金的存款余额"),
            PlanField(table=ORG_BAL, column="data_dt", usage="filter", reason="快照表的数据日期"),
            PlanField(table=ORG_BAL, column="org_no", usage="join", reason="关联机构维表"),
            PlanField(table=DIM_ORG, column="lvl1_org_nm", usage="dimension", reason="按一级分行分组"),
            PlanField(table=DIM_ORG, column="org_no", usage="join", reason="关联键"),
        ],
        joins=[PlanJoin(left_table=ORG_BAL, left_columns=["org_no"], right_table=DIM_ORG, right_columns=["org_no"],
                        join_type="INNER", reason="机构号多对一，不发散")],
        filters=[f"{ORG_BAL}.data_dt = DATE '2026-09-30'"],
        caliber="dep_bal_rmb 由客户汇总表按机构汇总，加工时只汇总 is_margin = '0' 的账户，不含保证金",
        table_basis="需求只到机构粒度，用机构汇总表",
        join_basis="按 org_no 多对一关联机构维表，不会重复计算",
    )
    return plan.model_copy(update=changes)
