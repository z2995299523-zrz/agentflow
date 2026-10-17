"""
④ 选表定口径
模型按证据给出四种结论之一：ok（查询方案）、need_clarify（反问）、need_upstream（缺什么，要上溯）、
cannot_answer（元数据里做不到）。代码核对方案里的表、字段、关联都在证据里之后才往下走。
"""
import difflib
from collections.abc import Iterable, Sequence

from nl2sql.llm import LLMClient, structured_call
from nl2sql.schemas import CallRecord, Evidence, ParsedRequest, QueryPlan
from prompts.nl2sql import planner_messages

MAX_SUGGESTIONS = 5               # 报错时附上最接近的几个字段名


def plan_query(llm: LLMClient, parsed: ParsedRequest, evidence: Evidence, *, allow_clarify: bool,
               allow_upstream: bool) -> tuple[QueryPlan, list[CallRecord]]:
    """调模型定查询方案，输出经 check_plan 核对，不合格带着问题重试一次

    Args:
        llm: 模型
        parsed: ① 的结果
        evidence: ③ 的结果
        allow_clarify: 本轮能不能反问（本轮已经是对反问的回答时不能）
        allow_upstream: 能不能要求上溯（A 组不能）

    Returns:
        (查询方案, 调用记录)

    Raises:
        StepError: 模型调用失败，或者两次输出都不合格
    """
    messages = planner_messages(parsed, evidence, allow_clarify=allow_clarify, allow_upstream=allow_upstream)
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
