"""
④ 选表定口径
模型按证据给出四种结论之一：ok（查询方案）、need_clarify（反问）、need_upstream（缺什么，要上溯）、
cannot_answer（元数据里做不到）。代码核对方案里的表、字段、关联都在证据里之后才往下走。
结论是 ok 的方案再由 prune_plan 去掉用不到的表（孤立表、只用来关联而去掉不改变结果的维表）；
能反问时，需求只说「日均」而方案挑了一种统计区间，由 clarify_daily_average 改成反问。
"""
import difflib
import re
from collections.abc import Iterable, Sequence

from nl2sql.llm import LLMClient, structured_call
from nl2sql.schemas import CallRecord, Clarify, ClarifyOption, Evidence, ParsedRequest, PlanJoin, QueryPlan
from prompts.nl2sql import planner_messages

MAX_SUGGESTIONS = 5               # 报错时附上最接近的几个字段名
TOP_LAYER = "专题层"              # 专题层的表与维表参照完整（ETL 保证）：只用关联键关联维表不改变结果
DIM_TABLE_TYPE = "维表"


def plan_query(llm: LLMClient, parsed: ParsedRequest, evidence: Evidence, *, allow_clarify: bool,
               allow_upstream: bool, base_sql: str = "") -> tuple[QueryPlan, list[CallRecord]]:
    """调模型定查询方案，输出经 check_plan 核对，不合格带着问题重试一次

    Args:
        llm: 模型
        parsed: ① 的结果
        evidence: ③ 的结果
        allow_clarify: 本轮能不能反问（本轮已经是对反问的回答时不能）
        allow_upstream: 能不能要求上溯（A 组不能）
        base_sql: 追问时上一轮的 SQL，方案以它为底增量修改；不是追问时为空

    Returns:
        (查询方案, 调用记录)

    Raises:
        StepError: 模型调用失败，或者两次输出都不合格
    """
    messages = planner_messages(parsed, evidence, allow_clarify=allow_clarify, allow_upstream=allow_upstream,
                                base_sql=base_sql)
    return structured_call(llm, "plan", messages, QueryPlan,
                           lambda plan: check_plan(plan, evidence, allow_clarify=allow_clarify,
                                                   allow_upstream=allow_upstream))


def _join(names: Iterable[str]) -> str:
    """列表写成「a、b、c」"""
    return "、".join(names)


def _unique(items: Iterable[str]) -> list[str]:
    """保序去重"""
    return list(dict.fromkeys(items))


def _closest(name: str, candidates: Iterable[str]) -> list[str]:
    """最接近的几个名字，按相似度从高到低，相似度相同时按名字排"""
    scored = sorted(((-difflib.SequenceMatcher(None, name.lower(), candidate.lower()).ratio(), candidate)
                     for candidate in set(candidates)))
    return [candidate for _, candidate in scored[:MAX_SUGGESTIONS]]


def _unknown_table(table: str, available: Sequence[str]) -> str:
    """表不在证据里的问题：列出可用的表；大小写不对、没带模式名时点出正确的写法"""
    lowered = [name.lower() for name in available]
    hint = ""
    if table.lower() in lowered:
        hint = f"（表名要和证据里完全一致，应写成 {available[lowered.index(table.lower())]}）"
    else:
        matches = [name for name in available if name.split(".", 1)[-1].lower() == table.lower()]
        if len(matches) == 1:
            hint = f"（要带模式名，应写成 {matches[0]}）"
    return f"表 {table} 不在候选里{hint}，可用的表：{_join(available) or '（没有）'}"


def _unknown_column(table: str, column: str, names: set[str], where: str) -> str:
    """字段不在表里的问题：大小写不对时点出正确的写法，否则列出最接近的字段"""
    exact = [name for name in names if name.lower() == column.lower()]
    if exact:
        return f"表 {table} 没有字段 {column}（{where}）；字段名要和证据里完全一致，应写成 {exact[0]}"
    return f"表 {table} 没有字段 {column}（{where}）；最接近的字段：{_join(_closest(column, names)) or '（没有）'}"


def _check_ok(plan: QueryPlan, evidence: Evidence) -> list[str]:
    """结论是 ok 时：表、字段、关联都要在证据里，样例编号要真实存在"""
    problems = []
    available = evidence.table_ids()
    plan_tables = [item.table for item in plan.tables]
    if not plan.tables:
        problems.append("decision 是 ok 时 tables 不能为空，请列出要用的表（只能用证据里的表）")
    if not plan.fields:
        problems.append("decision 是 ok 时 fields 不能为空，请列出用到的字段（指标、维度、过滤、关联、排序用到的都要列）")
    for table in _unique(plan_tables):
        if table not in available:
            problems.append(_unknown_table(table, available))
    for item in plan.fields:
        ref = f"{item.table}.{item.column}"
        if item.table not in plan_tables:
            if item.table in available:
                problems.append(f"字段 {ref} 所在的表没有列在 tables 里，请把 {item.table} 加进 tables，或者去掉这个字段")
            else:
                problems.append(_unknown_table(item.table, available) + f"（fields 里的 {ref}）")
        elif item.table in available and item.column not in evidence.column_names(item.table):
            problems.append(_unknown_column(item.table, item.column, evidence.column_names(item.table),
                                            f"fields 里的 {ref}"))
    for join in plan.joins:
        title = f"{join.left_table} ↔ {join.right_table}"
        for side, table, columns in (("左", join.left_table, join.left_columns), ("右", join.right_table, join.right_columns)):
            if table not in plan_tables:
                if table in available:
                    problems.append(f"关联（{title}）的{side}表 {table} 没有列在 tables 里，请把它加进 tables，或者去掉这个关联")
                else:
                    problems.append(_unknown_table(table, available) + f"（关联的{side}表）")
                continue
            names = evidence.column_names(table)
            for column in columns:
                if table in available and column not in names:
                    problems.append(_unknown_column(table, column, names, f"关联（{title}）的{side}表"))
        if len(join.left_columns) != len(join.right_columns) or not join.left_columns:
            problems.append(f"关联（{title}）两侧的字段个数必须相同且至少一个，现在左 {len(join.left_columns)} 个、"
                            f"右 {len(join.right_columns)} 个，请成对写出关联键")
    known = [example.example_id for example in evidence.examples]
    for example_id in plan.examples_used:
        if example_id not in known:
            if known:
                problems.append(f"examples_used 里的 {example_id} 不在证据的样例里；证据里的样例编号：{_join(known)}")
            else:
                problems.append(f"examples_used 里的 {example_id} 不在证据的样例里，证据里没有样例，examples_used 请留空")
    return _unique(problems)


def check_plan(plan: QueryPlan, evidence: Evidence, *, allow_clarify: bool, allow_upstream: bool) -> list[str]:
    """核对查询方案，返回能直接告诉模型怎么改的问题；合格时返回空列表

    - ok：至少一张表、一个字段；表都在证据里；字段所属的表在方案的表里、字段在这张表里；
      关联两侧的表在方案的表里、字段存在、两侧个数相同且至少一个；examples_used 都在证据的样例里
    - need_clarify：allow_clarify 为真；clarify 有问题；至少两个带 label 的选项
    - need_upstream：allow_upstream 为真；missing 非空
    - cannot_answer：reason 非空
    """
    if plan.decision == "ok":
        return _check_ok(plan, evidence)
    if plan.decision == "need_clarify":
        if not allow_clarify:
            return ["本轮是对反问的回答，不能再反问，请按最可能的理解给出方案（decision 写 ok），并把这个选择写进 assumptions"]
        problems = []
        clarify = plan.clarify
        if clarify is None or not clarify.question.strip():
            problems.append("decision 是 need_clarify 时必须在 clarify.question 里写一个问题")
        labelled = [option for option in (clarify.options if clarify else []) if option.label.strip()]
        if len(labelled) < 2:
            problems.append(f"clarify.options 至少要有 2 个带 label 的选项（每个选项写明对应哪个字段、含义是什么），"
                            f"现在只有 {len(labelled)} 个")
        return problems
    if plan.decision == "need_upstream":
        if not allow_upstream:
            return ["本次不能上溯（没有可用的上游表），请改用 ok：在候选的表里按最可能的理解给出方案并写进 assumptions；"
                    "确实做不到就用 cannot_answer 并在 reason 里写明原因"]
        if not any(item.strip() for item in plan.missing):
            return ["decision 是 need_upstream 时必须在 missing 里写缺什么，写成字段的叫法，例如 [\"币种\"]"]
        return []
    if not plan.reason.strip():
        return ["decision 是 cannot_answer 时必须在 reason 里写明元数据里做不到的原因"]
    return []


# ── 方案精简 ──────────────────────────────────────

def _mentioned(table: str, columns: Iterable[str], filters: Sequence[str]) -> bool:
    """过滤条件里有没有写到这张表：写了「模式.表.」，或者写了 columns 里的字段名（按整词，带别名的也算）"""
    patterns = [rf"(?<![\w.]){re.escape(table)}\."] + [rf"(?<!\w){re.escape(name)}(?!\w)" for name in columns]
    return any(re.search(pattern, text, re.IGNORECASE) for text in filters for pattern in patterns)


def _sides(join: PlanJoin, table: str) -> tuple[list[str], str, list[str]]:
    """关联里这张表一侧的字段、另一侧的表和字段"""
    if join.right_table == table:
        return join.right_columns, join.left_table, join.left_columns
    return join.left_columns, join.right_table, join.right_columns


def _redundant_dimension(plan: QueryPlan, evidence: Evidence, table: str) -> PlanJoin | None:
    """只用来关联、去掉也不改变结果的维表：返回要和它一起去掉的那条关联，不是这种维表时返回 None"""
    meta = evidence.table(table)
    if meta is None or meta.table_type != DIM_TABLE_TYPE or not meta.grain_columns:
        return None
    joins = [join for join in plan.joins if table in (join.left_table, join.right_table)]
    if len(joins) != 1 or joins[0].left_table == joins[0].right_table:
        return None                                                  # 中间表、自关联不动
    join = joins[0]
    keys, other, _ = _sides(join, table)
    other_meta = evidence.table(other)
    if other_meta is None or other not in {item.table for item in plan.tables}:
        return None
    used = [item for item in plan.fields if item.table == table]
    if any(item.usage != "join" or item.column not in keys for item in used):
        return None
    if not set(meta.grain_columns) <= set(keys):                     # 关联键要包含维表的全部粒度字段：N:1，不发散
        return None
    others = [column.name for column in meta.columns if column.name not in keys]
    if _mentioned(table, others, plan.filters):
        return None
    if (join.join_type == "LEFT" and join.right_table == table) or other_meta.layer == TOP_LAYER:
        return join
    return None


def prune_plan(plan: QueryPlan, evidence: Evidence) -> tuple[QueryPlan, list[str]]:
    """去掉方案里用不到的表

    - 孤立表：没有字段、不在任何关联里、过滤条件里也没写到
    - 只用来关联的维表：证据里是维表；方案里只引用了它的关联键，过滤条件里也没写到它；只出现在一条关联里（不是中间表）；
      关联键包含它的全部粒度字段（N:1，不发散）；并且关联另一侧是专题层的表（专题层与维表参照完整），
      或者它在 LEFT JOIN 的右边（左表的行原样保留）。去掉时连同这条关联、另一侧只为这条关联列出的关联键一起去掉。
      整合层、贴源层的表内连接维表不动：那常常是在还原专题层加工时的内连接过滤
    只处理结论是 ok 的方案，返回新方案，不修改传入的对象。去掉的表记进方案的 dropped_tables：
    ⑤ 不能再用它们（样例里常有这些关联，⑤ 容易照抄回来），校验时用到了就报错。

    Args:
        plan: ④ 的方案
        evidence: ③ 的结果

    Returns:
        (精简后的方案, 去掉的表，按去掉的先后)
    """
    if plan.decision != "ok":
        return plan, []
    removed: list[str] = []
    current = plan
    changed = True
    while changed and len(current.tables) > 1:
        changed = False
        for item in current.tables:
            table = item.table
            in_joins = any(table in (join.left_table, join.right_table) for join in current.joins)
            has_fields = any(field.table == table for field in current.fields)
            if not in_joins and not has_fields:
                if _mentioned(table, [], current.filters):          # 孤立表只认「模式.表.」：字段名和别的表重名的太多
                    continue
                current = current.model_copy(update={"tables": [t for t in current.tables if t.table != table]})
            else:
                join = _redundant_dimension(current, evidence, table)
                if join is None:
                    continue
                _, other, other_keys = _sides(join, table)
                joins = [j for j in current.joins if j is not join]
                still_keys = {column for j in joins if other in (j.left_table, j.right_table)
                              for column in _sides(j, other)[0]}
                fields = [f for f in current.fields if f.table != table and not (
                    f.table == other and f.usage == "join" and f.column in other_keys and f.column not in still_keys)]
                current = current.model_copy(update={
                    "tables": [t for t in current.tables if t.table != table], "fields": fields, "joins": joins})
            removed.append(table)
            changed = True
            break
    if removed:
        current = current.model_copy(update={"dropped_tables": [*current.dropped_tables, *removed]})
    return current, removed


# ── 日均没说统计区间时反问 ──────────────────────────────

AVG_WORD = "日均"
AVG_PERIODS = {"年": "从当年 1 月 1 日算到截止日", "季": "从当季第一天算到截止日", "月": "从当月 1 日算到截止日",
               "旬": "从当旬第一天算到截止日"}


def _avg_period(cn: str) -> str:
    """字段中文名里日均的统计区间：「年日均存款余额」→「年」；不是日均字段、没写区间时为空串"""
    index = cn.find(AVG_WORD)
    return cn[index - 1] if index > 0 and cn[index - 1] in AVG_PERIODS else ""


def clarify_daily_average(parsed: ParsedRequest, evidence: Evidence, plan: QueryPlan) -> QueryPlan:
    """需求只说「日均」、没说统计区间，方案却挑了其中一种日均字段时，改成反问

    条件：结论是 ok；需求（完整需求、指标、口径线索）里有「日均」，没有「年日均」「季日均」「月日均」「旬日均」；
    方案的指标字段里有日均字段，证据里还有同一个指标、统计区间不同的日均字段（中文名去掉区间那个字后相同，
    如「年日均存款余额」和「月日均存款余额」）。只用中文名判断，三组都一样。
    调用方只在本轮能反问时调用。

    Args:
        parsed: ① 的结果
        evidence: ③ 的结果
        plan: ④ 的方案

    Returns:
        改成的反问；不满足条件时原样返回
    """
    if plan.decision != "ok":
        return plan
    asked = " ".join([parsed.requirement, *(metric.name for metric in parsed.metrics), *parsed.caliber_hints])
    if AVG_WORD not in asked or any(period + AVG_WORD in asked for period in AVG_PERIODS):
        return plan
    names = {(table.table_id, column.name): column.cn for table in evidence.tables for column in table.columns}
    chosen = {names[(item.table, item.column)].replace(_avg_period(names[(item.table, item.column)]) + AVG_WORD,
                                                       AVG_WORD, 1)
              for item in plan.fields
              if item.usage == "metric" and _avg_period(names.get((item.table, item.column), ""))}
    variants: dict[str, list[str]] = {}
    for (table_id, column), cn in names.items():
        period = _avg_period(cn)
        if period and cn.replace(period + AVG_WORD, AVG_WORD, 1) in chosen:
            variants.setdefault(period, []).append(f"{table_id}.{column}（{cn}）")
    if len(variants) < 2:
        return plan
    periods = [period for period in AVG_PERIODS if period in variants]
    options = [ClarifyOption(label=f"{period}{AVG_WORD}",
                             meaning=f"{AVG_PERIODS[period]}；用 {'、'.join(variants[period][:2])}")
               for period in periods]
    question = f"需求里的「{AVG_WORD}」按哪个统计区间算？（{'、'.join(option.label for option in options)}）"
    return QueryPlan(decision="need_clarify", clarify=Clarify(question=question, options=options))
