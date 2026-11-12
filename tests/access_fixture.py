"""两类业务域的合成数据和离线模型，用于权限验收。"""
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path

from lineage.graph import LineageGraph
from metadata.models import ColumnMeta, Example, LineageEdge, Mart, TableMeta
from metadata.store import MetadataStore, write_store
from nl2sql.executor import SQLiteExecutor
from nl2sql.llm import LLMReply
from nl2sql.retriever import Retriever
from nl2sql.schemas import Metric, ParsedRequest, PlanField, PlanTable, QueryPlan
from nl2sql.validator import SqlValidator
from nl2sql.workflow import Pipeline, PipelineDeps
from prompts.nl2sql import GENERATOR_SYSTEM, PARSER_SYSTEM, PLANNER_SYSTEM

PROJECT = "pilot_project.case_activity"
FUND = "pilot_fund.disbursement"
UNCLASSIFIED = "pilot_archive.unclassified_case"
LABELS = {PROJECT: "项目立项统计", FUND: "经费支出统计", UNCLASSIFIED: "待归类记录"}


def build_access_mart(path: Path) -> Path:
    """生成两个已登记域、一个未归类表和跨域血缘的本地演示库。"""
    build = path / "build"
    build.mkdir(parents=True, exist_ok=True)
    tables = []
    columns = []
    for ident, domain in [(PROJECT, "research"), (FUND, "finance"), (UNCLASSIFIED, "")]:
        schema, name = ident.split(".")
        tables.append(TableMeta(table_id=ident, schema_name=schema, table_name=name, table_cn=LABELS[ident],
                                layer="专题层", table_type="全量表", grain_columns=["item_id"], domain_code=domain))
        columns.extend([
            ColumnMeta(table_id=ident, column_name="item_id", column_cn="记录编号", data_type="INTEGER", ordinal=1, is_pk=True),
            ColumnMeta(table_id=ident, column_name="quantity", column_cn="统计值", data_type="INTEGER", ordinal=2),
        ])
    examples = [
        Example(example_id=f"pilot-{i}", requirement=f"统计{LABELS[ident]}", tables=[ident],
                sql_text=f"SELECT SUM(quantity) FROM {ident}", source_file="pilot.sql", line_no=i)
        for i, ident in enumerate((PROJECT, FUND), 1)
    ]
    examples.append(Example(example_id="pilot-mixed", requirement="跨域样例",
                            tables=[PROJECT, FUND], sql_text=f"SELECT * FROM {PROJECT} JOIN {FUND} USING (item_id)",
                            source_file="pilot.sql", line_no=3))
    write_store(build / "metadata.db", Mart(
        tables=tables, columns=columns, examples=examples,
        lineage=[LineageEdge(target_table=PROJECT, target_column="quantity", source_table=FUND, source_column="quantity")],
    ), info={"version_id": "local-access-pilot"})
    db = build / "test_data.db"
    with closing(sqlite3.connect(db)) as conn, conn:
        for ident, values in [(PROJECT, (3, 5)), (FUND, (11, 19)), (UNCLASSIFIED, (90, 99))]:
            conn.execute(f'CREATE TABLE "{ident}" (item_id INTEGER PRIMARY KEY, quantity INTEGER)')
            conn.executemany(f'INSERT INTO "{ident}" VALUES(?,?)', enumerate(values, 1))
    return db


class PilotLLM:
    """按两个固定示例返回离线回复，实际查询仍经过 AgentFlow 流水线。"""

    name = "deepseek-flash"

    def __init__(self) -> None:
        self.generated_sql: str | None = None
        self.evidence_prompts: list[str] = []

    def complete(self, messages: Sequence[Mapping[str, str]], *, json_mode: bool = False) -> LLMReply:
        """模拟解析、选表和 SQL 生成三个阶段。"""
        system = messages[0]["content"]
        user = messages[-1]["content"]
        prefix = "本轮输入：" if system == PARSER_SYSTEM else "完整需求："
        request = next((line.removeprefix(prefix) for line in user.splitlines() if line.startswith(prefix)), "")
        ident = next((ident for ident, label in LABELS.items() if label[:4] in request or ident in request), None)
        if system == PARSER_SYSTEM:
            parsed = ParsedRequest(is_query=ident is not None, requirement=f"统计{LABELS[ident]}" if ident else "",
                                   metrics=[Metric(name="统计值")] if ident else [],
                                   named_tables=[ident] if ident else [],
                                   reply="本地验收可试：统计项目立项数量，或统计经费支出合计。")
            return LLMReply(parsed.model_dump_json())
        self.evidence_prompts.append(user)
        if not ident:
            raise ValueError("离线模型没有匹配的测试问题")
        if system == PLANNER_SYSTEM:
            plan = QueryPlan(decision="ok", tables=[PlanTable(table=ident, reason="已授权汇总表")],
                             fields=[PlanField(table=ident, column="quantity", usage="metric", reason="汇总统计值")])
            return LLMReply(plan.model_dump_json())
        if system == GENERATOR_SYSTEM:
            sql = self.generated_sql or f"SELECT SUM(quantity) AS total FROM {ident}"
            return LLMReply(f"```sql\n{sql}\n```")
        raise ValueError("未知测试模型阶段")


def pilot_pipeline(store: MetadataStore, graph: LineageGraph, db: Path, model: PilotLLM) -> Pipeline:
    """装配真实检索、SQL 校验与 SQLite 执行，仅替换外部模型。"""
    return Pipeline(PipelineDeps(store, graph, Retriever(store, graph, None), SqlValidator(store), model,
                                 executor=SQLiteExecutor(db), llm_for=lambda _: model))
