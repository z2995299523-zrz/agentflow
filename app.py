"""
专题库查数：Streamlit 界面
两页（侧边栏切换）：
- 查数：用自然语言提问，逐步显示七步进度，展示 SQL、口径说明、假设、校验结果、执行结果；
  流水线反问时点一个选项就是回答；追问时自动带上上一轮的完整需求；「新话题」清空会话
- 元数据：按层级浏览表，再看字段，选一个字段看它的上游、下游链路（缩进文本，每一跳带加工表达式）

流水线和元数据一律写成 service.get_pipeline()、service.get_metadata() 的形式去取，测试里替换这两个函数。
取不到（元数据库没导入、没配模型 Key）时页面上写明原因和导入命令，不报堆栈。
"""
from collections import defaultdict
from datetime import date

import pandas as pd
import streamlit as st

import config
from metadata.models import LAYERS
from metadata.store import MetadataStore
from nl2sql import service
from nl2sql.schemas import INTERNAL_STEP, STEP_LABELS, Clarify, QueryRequest, QueryResult, TraceStep
from nl2sql.workflow import Pipeline

PAGES = ("查数", "元数据")
DEFAULT_LAYER = "专题层"
LINEAGE_DEPTH = 3
EXPRESSION_CHARS = 300                 # 链路里每一跳的加工表达式最多显示多少字
EXAMPLE_PLACEHOLDER = "例如：查 2026 年 9 月末各一级分行的对公存款余额"

st.set_page_config(page_title="专题库查数", layout="wide")


# ── 会话 ──────────────────────────────────────────

def init_session() -> None:
    """会话里存三样：已经问过的轮次、上一轮的完整需求、上一轮的反问（追问和回答反问时发给流水线）"""
    if "turns" not in st.session_state:
        st.session_state.turns = []                    # 每轮一个字典：question、result
    if "prev_requirement" not in st.session_state:
        st.session_state.prev_requirement = ""
    if "prev_clarify" not in st.session_state:
        st.session_state.prev_clarify = None


def new_topic() -> None:
    """「新话题」：清空会话，下一句话不再当作追问"""
    st.session_state.turns = []
    st.session_state.prev_requirement = ""
    st.session_state.prev_clarify = None
    st.session_state.pop("pending_question", None)


def answer_clarify(label: str) -> None:
    """点了反问的一个选项：下一次运行时把选项文字当作本轮的提问发出去"""
    st.session_state.pending_question = label


def remember(question: str, result: QueryResult) -> None:
    """记下这一轮。解析成功（结果里有完整需求）才更新上一轮的需求和反问：

    解析失败、不是查数需求的轮次不动它们，用户可以接着上一个话题问，也可以重新点同一个选项。
    """
    st.session_state.turns.append({"question": question, "result": result})
    if result.requirement:
        st.session_state.prev_requirement = result.requirement
        st.session_state.prev_clarify = result.clarify if result.status == "need_clarify" else None


# ── 小工具 ────────────────────────────────────────

def plain(text: str) -> str:
    """文本里的 $ 转义：Streamlit 的 Markdown 会把两个 $ 之间的内容当成公式（加工条件里常有 ${data_dt}）"""
    return text.replace("$", "\\$")


def format_step(step: TraceStep) -> str:
    """一步进度写成「标签：摘要（耗时）」"""
    return f"{step.label}：{step.summary}（{step.elapsed_ms} ms）"


def unique_names(names: list[str]) -> list[str]:
    """重名的列加序号区分：表格组件不接受重复的列名"""
    seen: dict[str, int] = {}
    unique = []
    for name in names:
        seen[name] = seen.get(name, 0) + 1
        unique.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    return unique


def show_unavailable(error: Exception) -> None:
    """取不到流水线或元数据时：写明原因和导入命令，不显示堆栈"""
    st.error(f"暂时用不了：{plain(str(error) or type(error).__name__)}")
    st.markdown("如果还没有导入元数据，先在命令行里运行：")
    st.code(service.IMPORT_COMMAND, language="bash")
    st.caption("模型的 Key 在项目 .env 里配置。")


# ── 查数页：结果 ──────────────────────────────────

def render_status(result: QueryResult) -> None:
    """一句话说明这一轮的结论"""
    if result.status == "ok":
        st.success("已生成 SQL，静态校验通过")
    elif result.status == "unvalidated":
        st.warning(f"已生成 SQL，但修正 {result.repair_rounds} 次后静态校验仍有错误，请人工检查后再使用")
    elif result.status == "need_clarify":
        st.info("需要先确认需求" if result.plan is None else "需要先确认口径")      # 没有方案说明是 ① 问的
    elif result.parsed is not None and not result.parsed.is_query:
        st.info(plain(result.message))                 # 不是查数需求：模型的回复
    else:
        where = STEP_LABELS.get(result.failed_step, "程序内部" if result.failed_step == INTERNAL_STEP else "")
        st.error("没有生成 SQL" + (f"，停在「{where}」" if where else "") + f"：{plain(result.message)}")


def render_clarify(index: int, clarify: Clarify, active: bool) -> None:
    """反问：每个选项一个按钮，点一下就是回答。不是最后一轮的反问只列出来，不能再点"""
    st.markdown(f"**{plain(clarify.question)}**")
    for position, option in enumerate(clarify.options):
        if active:
            st.button(option.label, key=f"clarify_{index}_{position}", on_click=answer_clarify, args=(option.label,))
            if option.meaning:
                st.caption(option.meaning)
        else:
            st.markdown(f"- {plain(option.label)}" + (f"：{plain(option.meaning)}" if option.meaning else ""))


def render_explanations(result: QueryResult) -> None:
    """口径说明、用表依据、关联说明、溯源说明、假设、参考样例"""
    plan = result.plan
    if plan is not None:
        for title, text in (("口径说明", plan.caliber), ("用表依据", plan.table_basis),
                            ("关联说明", plan.join_basis), ("溯源说明", plan.lineage_note)):
            if text.strip():
                st.markdown(f"**{title}**：{plain(text)}")
    if result.assumptions:
        st.markdown("**假设**\n\n" + "\n".join(f"- {plain(item)}" for item in result.assumptions))
    if plan is not None and plan.examples_used:
        st.markdown("**参考样例**：" + "、".join(f"`{item}`" for item in plan.examples_used))


def render_validation(result: QueryResult) -> None:
    """静态校验：错误和警告分开显示"""
    validation = result.validation
    if validation is None:
        return
    if validation.errors:
        st.error("静态校验错误\n\n" + "\n".join(f"- {plain(item)}" for item in validation.errors))
    if validation.warnings:
        st.warning("静态校验警告\n\n" + "\n".join(f"- {plain(item)}" for item in validation.warnings))


def render_execution(result: QueryResult) -> None:
    """执行结果：配置了执行层才有；结果被截断时说明只显示了多少行"""
    outcome = result.exec_result
    if outcome is None:
        return
    if outcome.executed:
        st.markdown("**执行结果**")
        st.dataframe(pd.DataFrame(outcome.rows, columns=unique_names(outcome.columns)))
        note = f"返回 {outcome.row_count} 行，用时 {outcome.elapsed_ms} ms"
        if outcome.truncated:
            note += f"；结果太多被截断了，只显示前 {outcome.row_count} 行"
        st.caption(note)
    elif outcome.error:
        st.error(f"执行失败：{plain(outcome.error)}")
    elif outcome.note:
        st.caption(outcome.note)


def render_result(index: int, result: QueryResult, active: bool) -> None:
    """一轮的结果：结论、完整需求、反问或 SQL、各项说明、校验、执行结果、用量

    Args:
        index: 这是第几轮（从 0 数），反问按钮的键里用
        result: 流水线的结果
        active: 是不是最后一轮；只有最后一轮的反问选项可以点
    """
    render_status(result)
    if result.requirement:
        st.markdown(f"**完整需求**：{plain(result.requirement)}")
    if result.status == "need_clarify" and result.clarify is not None:
        render_clarify(index, result.clarify, active)
    if result.sql:
        st.code(result.sql, language="sql")
    render_explanations(result)
    render_validation(result)
    render_execution(result)
    usage = [f"模型调用 {result.llm_calls} 次", f"用时 {result.elapsed_ms / 1000:.1f} 秒"]
    if result.scope_level:
        usage.append(f"上溯 {result.scope_level} 层")
    if result.repair_rounds:
        usage.append(f"修正 {result.repair_rounds} 次")
    st.caption("，".join(usage))


def render_turn(index: int, turn: dict, active: bool) -> None:
    """已经问过的一轮：问题、结果，以及折叠起来的运行过程"""
    result: QueryResult = turn["result"]
    with st.chat_message("user"):
        st.markdown(plain(turn["question"]))
    with st.chat_message("assistant"):
        render_result(index, result, active)
        with st.expander("运行过程"):
            for step in result.trace:
                st.text(format_step(step))


# ── 查数页：提问 ──────────────────────────────────

def progress_label(result: QueryResult) -> tuple[str, str]:
    """状态框收起后的标题和图标：（标题, complete 或 error）。模型对非查数问题的回复不算出错"""
    if result.status == "ok":
        return "已生成 SQL", "complete"
    if result.status == "unvalidated":
        return "已生成 SQL，校验有错误", "complete"
    if result.status == "need_clarify":
        return "需要确认口径", "complete"
    if result.parsed is not None and not result.parsed.is_query:
        return "不是查数需求", "complete"
    return "没有生成 SQL", "error"


def run_with_progress(pipeline: Pipeline, request: QueryRequest) -> QueryResult:
    """跑一轮查询，每走完一步在状态框里写一行；流水线不该抛异常，真抛了也变成一个 failed 的结果"""
    result = None
    with st.status("正在处理……", expanded=True) as status:
        try:
            for item in pipeline.stream(request):
                if isinstance(item, TraceStep):
                    st.text(format_step(item))
                else:
                    result = item
        except Exception as error:  # noqa: BLE001 界面上不显示堆栈
            result = QueryResult(status="failed", question=request.question, failed_step=INTERNAL_STEP,
                                 message=f"内部错误：{type(error).__name__}: {error}")
        if result is None:
            result = QueryResult(status="failed", question=request.question, failed_step=INTERNAL_STEP,
                                 message="内部错误：流水线没有给出结果")
        label, state = progress_label(result)
        status.update(label=label, state=state, expanded=False)
    return result


def ask(pipeline: Pipeline, question: str, today: date) -> None:
    """发出一轮提问：带上会话里的上一轮需求和反问，显示进度和结果，记进会话"""
    index = len(st.session_state.turns)
    request = QueryRequest(question=question, prev_requirement=st.session_state.prev_requirement,
                           prev_clarify=st.session_state.prev_clarify, today=today)
    with st.chat_message("user"):
        st.markdown(plain(question))
    with st.chat_message("assistant"):
        result = run_with_progress(pipeline, request)
        remember(question, result)
        render_result(index, result, active=True)


def render_query_page() -> None:
    """查数页"""
    st.header("查数")
    today = date.today()
    st.caption(f"用自然语言说要查什么。「现在」「今年」「9 月末」这类说法按今天 {today.isoformat()} 换算。")
    try:
        pipeline = service.get_pipeline()
    except Exception as error:  # noqa: BLE001 取不到的原因很多（没导入、没配 Key），都写明原因，不显示堆栈
        show_unavailable(error)
        return
    typed = st.chat_input(EXAMPLE_PLACEHOLDER)
    question = typed or st.session_state.pop("pending_question", None)
    turns = st.session_state.turns
    for index, turn in enumerate(turns):
        render_turn(index, turn, active=index == len(turns) - 1 and not question)
    if question:
        ask(pipeline, question, today)


# ── 元数据页 ──────────────────────────────────────

def field_label(store: MetadataStore, field_id: str) -> str:
    """字段写成「模式.表.字段（中文名）」，元数据里没有它（外部表的字段等）时只写编号"""
    table, column = field_id.rsplit(".", 1)
    meta = store.column(table, column)
    return f"{field_id}（{meta.column_cn}）" if meta is not None and meta.column_cn else field_id


def hop_details(hop: dict) -> list[str]:
    """一跳的加工说明：表达式、过滤条件、关联条件，一条一行"""
    expression = hop["script_expression"] or hop["transform"] or ("直取" if hop["is_passthrough"] else "")
    lines = []
    if expression:
        flat = " ".join(expression.split())
        lines.append(f"表达式：{flat if len(flat) <= EXPRESSION_CHARS else flat[:EXPRESSION_CHARS] + '…'}")
    lines.extend(f"过滤：{item}" for item in hop["filters"])
    lines.extend(f"关联：{item}" for item in hop["joins"])
    return lines


def lineage_text(store: MetadataStore, field_id: str, hops: list[dict], forward: bool) -> str:
    """把一个字段的链路画成缩进文本

    第一行是起点字段；每往外一层缩进一级，一行一个来源（上游，用 ←）或去向（下游，用 →）；
    每一跳下面缩进写加工表达式、过滤条件、关联条件，同一个字段的几个来源共用一套加工，只写一遍，其余写「同上」。
    同一个字段只展开一次。

    Args:
        store: 元数据，查字段的中文名
        field_id: 起点字段（模式.表.字段）
        hops: nl2sql.service.describe_lineage 给出的上游或下游
        forward: True 是下游（谁由它加工而来），False 是上游
    """
    lines = [field_label(store, field_id)]
    if not hops:
        return "\n".join(lines + [f"  （没有{'下游' if forward else '上游'}）"])
    parent_key, child_key, arrow = ("source", "target", "→") if forward else ("target", "source", "←")
    children: dict[str, list[dict]] = defaultdict(list)
    for hop in hops:
        children[hop[parent_key]].append(hop)
    expanded: set[str] = set()

    def walk(node: str, level: int) -> None:
        if node in expanded:
            return
        expanded.add(node)
        previous: list[str] | None = None
        for hop in children.get(node, []):
            child = hop[child_key]
            unknown = "（不在字段清单里）" if not forward and not hop["source_known"] else ""
            lines.append(f"{'  ' * level}{arrow} {field_label(store, child)}{unknown}")
            details = hop_details(hop)
            indent = "  " * level + "    "
            if details and details == previous:
                lines.append(f"{indent}加工同上一条")
            else:
                lines.extend(indent + line for line in details)
            previous = details
            walk(child, level + 1)

    walk(field_id, 1)
    return "\n".join(lines)


def render_metadata_page() -> None:
    """元数据页：层级 → 表 → 字段 → 上游、下游链路"""
    st.header("元数据")
    try:
        store, graph = service.get_metadata()
    except Exception as error:  # noqa: BLE001
        show_unavailable(error)
        return
    st.caption("　".join(f"{name} {count}" for name, count in store.counts().items()))

    layer = st.selectbox("层级", LAYERS, index=LAYERS.index(DEFAULT_LAYER), key="meta_layer")
    tables = store.tables(layer)
    if not tables:
        st.info(f"{layer}里没有表")
        return
    st.dataframe(pd.DataFrame([{
        "表": table.table_id, "中文名": table.table_cn, "类型": table.table_type,
        "粒度": "、".join(table.grain_columns), "数据日期字段": table.date_column or "",
        "字段数": len(store.columns(table.table_id)),
    } for table in tables]), hide_index=True)

    table_id = st.selectbox("表", [table.table_id for table in tables], index=None, placeholder="选一张表，查看它的字段",
                            format_func=lambda item: f"{item}（{store.table(item).table_cn}）", key=f"meta_table_{layer}")
    if table_id is None:
        return
    meta = store.table(table_id)
    if meta.description:
        st.caption(meta.description)
    columns = store.columns(table_id)
    st.dataframe(pd.DataFrame([{
        "字段": column.column_name, "中文名": column.column_cn, "类型": column.data_type,
        "主键": "是" if column.is_pk else "", "说明": column.description, "码值": column.enum_values,
    } for column in columns]), hide_index=True)

    name = st.selectbox("字段", [column.column_name for column in columns], index=None,
                        placeholder="选一个字段，查看它的上游和下游",
                        format_func=lambda item: f"{item}（{store.column(table_id, item).column_cn}）",
                        key=f"meta_column_{table_id}")
    if name is None:
        return
    try:
        view = service.describe_lineage(store, graph, table_id, name, LINEAGE_DEPTH)
    except LookupError as error:                       # 元数据在页面开着的时候被重新导入了
        st.error(str(error))
        return
    st.markdown(f"**上游**：{plain(view['field'])} 由哪些字段加工而来（最多 {LINEAGE_DEPTH} 层）")
    st.code(lineage_text(store, view["field"], view["upstream"], forward=False), language=None)
    st.markdown("**下游**：哪些字段由它加工而来")
    st.code(lineage_text(store, view["field"], view["downstream"], forward=True), language=None)


# ── 页面框架 ──────────────────────────────────────

init_session()
with st.sidebar:
    st.title("专题库查数")
    page = st.radio("页面", PAGES, key="page")
    if page == "查数":
        st.button("新话题", key="new_topic", on_click=new_topic, help="清空对话，下一句话不再当作追问")
    st.divider()
    st.caption(f"资料目录：{config.MART_DIR}")
    st.caption(f"执行层：{config.EXECUTOR}")

if page == "元数据":
    render_metadata_page()
else:
    render_query_page()
