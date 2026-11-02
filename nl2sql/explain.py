"""
① 判断用户在问上一轮的结果时走这里，全部由代码按记录和元数据写出，不调模型。
只读上一轮的依据（RoundBasis，里面没有查询结果）和元数据：对不上为 unknown，重名为 ambiguous；
没有可比较方案为 no_previous；SQL 或方案里为 used，精简记录里为 pruned，候选里为 not_chosen；
其余按第 0 层重放区分 not_recalled（池子里没入选）与 outside（不在池子里）。
"""
import difflib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from metadata.models import LAYERS, TableMeta
from metadata.store import MetadataStore
from nl2sql.parser import to_recall_query
from nl2sql.retriever import Retriever
from nl2sql.schemas import Explanation, FieldMatch, RoundBasis, TableDiagnosis
from prompts.nl2sql import FULL_FIELD_TABLES

MAX_SIMILAR = 5
DIM_TABLE_TYPE = "维表"
_QUOTES = "'\"`「」『』"
_ENGLISH = re.compile(r"[a-z0-9_.]+")
_HAN = re.compile(r"[\u4e00-\u9fff]")


@dataclass(frozen=True)
class TableMatch:
    """名字对上的表；重名时分得出先后就是最优先的一张，similar 是其余同名的表；
    分不出先后时 ambiguous 为真，没对上时 similar 是相近的表"""

    table_id: str = ""
    similar: tuple[str, ...] = ()
    ambiguous: bool = False


def _normalize(name: str) -> str:
    """归一大小写、空白和首尾引号，英文名末尾的「表」字去掉"""
    text = "".join(unicodedata.normalize("NFKC", name).lower().split()).strip(_QUOTES)
    if text.endswith("表") and _ENGLISH.fullmatch(text[:-1].strip(_QUOTES)):
        text = text[:-1].strip(_QUOTES)
    return text


def is_table_name(name: str) -> bool:
    """是不是英文表名：归一并去掉引号后只有英文、数字、下划线和点"""
    return bool(_ENGLISH.fullmatch(_normalize(name)))


def _priority(table: TableMeta) -> tuple[bool, int, str]:
    """同名的表的先后：标准表在前，再按层级从上往下，和选表的规则一样"""
    return (not table.standard, -LAYERS.index(table.layer) if table.layer in LAYERS else 1, table.table_id)


def resolve_table(store: MetadataStore, name: str, *, loose: bool = False) -> TableMatch:
    """依次按全名、无模式的英文名、中文名解析，找不到时给出相近的表

    Args:
        store: 元数据
        name: 用户写的名字
        loose: 用户明确在说这张表（问为什么用它、要求改用它）时为真：中文叫法只被一张表的中文名包含，就算对上
    """
    normalized = _normalize(name)
    if not normalized:
        return TableMatch()
    tables = store.tables()
    for attribute in ("table_id", "table_name", "table_cn"):
        found = sorted((table for table in tables if _normalize(getattr(table, attribute)) == normalized),
                       key=_priority)
        if found:
            others = tuple(table.table_id for table in found[1:])
            if len(found) > 1 and _priority(found[0])[:2] == _priority(found[1])[:2]:
                return TableMatch(similar=tuple(sorted(table.table_id for table in found)), ambiguous=True)
            return TableMatch(found[0].table_id, others)
    chinese = bool(_HAN.search(normalized))
    if loose and chinese:
        partial = [table.table_id for table in tables if normalized in _normalize(table.table_cn)]
        if len(partial) == 1:
            return TableMatch(partial[0])

    def order(table: TableMeta) -> tuple[bool, float, str]:
        """中文包含优先，再按相似度和表标识排序"""
        other = _normalize(table.table_cn if chinese else table.table_name)
        contains = bool(normalized and other and (normalized in other or other in normalized))
        return (not contains if chinese else False, -difflib.SequenceMatcher(None, normalized, other).ratio(), table.table_id)

    return TableMatch(similar=tuple(table.table_id for table in sorted(tables, key=order)[:MAX_SIMILAR]))


def diagnose_unresolved(name: str, match: TableMatch) -> TableDiagnosis:
    """对不上的名字写成诊断；相近表只供界面选择，原名字不能直接改用"""
    if match.ambiguous:
        return TableDiagnosis(name=name, stage="ambiguous", similar=list(match.similar),
                              conclusion=f"「{name}」对上了 {len(match.similar)} 张表，要写带模式名的全名。")
    return TableDiagnosis(name=name, stage="unknown", similar=list(match.similar),
                          conclusion=f"元数据里没有「{name}」这张表。")


def same_name_note(name: str, match: TableMatch) -> str:
    """重名时取了最优先的一张，说一句；没有重名时为空串"""
    if not match.table_id or not match.similar:
        return ""
    return f"「{name}」有同名的表 {'、'.join(match.similar)}，这里按标准表优先、层级从上往下取了 {match.table_id}。"


def _gaps(store: MetadataStore, table: TableMeta) -> list[str]:
    """按固定顺序列出这张表缺少的元数据"""
    table_id = table.table_id
    checks = [("表说明", bool(table.description.strip())),
              ("血缘", any(table_id in (edge.target_table, edge.source_table) for edge in store.lineage_edges())),
              ("关联关系", any(table_id in (relation.left_table, relation.right_table) for relation in store.relations())),
              ("加工脚本", bool(store.scripts(table_id))),
              ("样例", any(table_id in example.tables for example in store.examples()))]
    return [label for label, present in checks if not present]


def _mentions(text: str, table_name: str) -> bool:
    """文字里有没有写到这张表：按整词比，表名是别的表名的一部分时不算"""
    return re.search(rf"(?<!\w){re.escape(table_name)}(?!\w)", text, re.IGNORECASE) is not None


def _missing_fields(diagnosis: TableDiagnosis) -> None:
    """没有名字相近的字段时追加提示，只描述名称比较的结果"""
    terms = dict.fromkeys(match.term for match in diagnosis.matches if not match.column)       # 同一说法可能既是指标又是维度
    missing = "".join(f"「{term}」" for term in terms)
    if missing:
        diagnosis.reasons.append(f"需求要的{missing}在这张表里没有名字相近的字段。")


def _diagnose(store: MetadataStore, retriever: Retriever, previous: RoundBasis | None,
              name: str) -> TableDiagnosis:
    """按记录里的阶段和当前元数据诊断唯一对上的表"""
    match = resolve_table(store, name, loose=True)
    requirement = previous.requirement if previous else ""
    if not match.table_id:
        return diagnose_unresolved(name, match)
    table = store.table(match.table_id)
    same_name = same_name_note(name, match)
    diagnosis = TableDiagnosis(name=name, table_id=table.table_id, table_cn=table.table_cn,
                              layer=table.layer, table_type=table.table_type, standard=table.standard,
                              grain_columns=list(table.grain_columns), gaps=_gaps(store, table), stage="no_previous",
                              conclusion=f"{table.table_id} 在元数据里，但还没有可以比较的选表记录。",
                              switchable=bool(requirement), reasons=[same_name] if same_name else [])
    plan = previous.plan if previous else None
    if not requirement or plan is None or plan.decision != "ok" or previous.parsed is None:
        if diagnosis.gaps:
            diagnosis.reasons.append(f"它没有{'、'.join(diagnosis.gaps)}，④ 只能凭表名和字段名判断。")
        return diagnosis
    table_id = table.table_id
    if table_id in previous.sql_tables or table_id in [item.table for item in plan.tables]:
        diagnosis.stage, diagnosis.conclusion, diagnosis.switchable = "used", "上一轮用了这张表。", False
        diagnosis.reasons += [item.reason for item in plan.tables if item.table == table_id and item.reason]
        return diagnosis
    query = to_recall_query(previous.parsed)
    diagnosis.matches = [FieldMatch(term=term, role=role, column=column, column_cn=cn, weak=weak)
                         for term, role, column, cn, weak in retriever.match_fields(query, table_id)]
    if table_id in plan.dropped_tables:
        diagnosis.stage = "pruned"
        diagnosis.conclusion = "④ 选了这张表，但它只用来关联、去掉不影响结果，精简方案时去掉了。"
        return diagnosis
    if table_id in previous.candidate_tables:
        diagnosis.stage = "not_chosen"
        diagnosis.conclusion = "这张表进了候选，④ 看过它，没有选。"
        if not table.standard:
            diagnosis.reasons.append("它是非标准表（设计文档没收录、口径没确认），规则是其它候选表能满足需求时不用它。")
        layers = [meta.layer for item in plan.tables if (meta := store.table(item.table)) and meta.layer in LAYERS]
        highest = max(layers, key=LAYERS.index) if layers else ""
        lower = highest and table.layer in LAYERS and LAYERS.index(table.layer) < LAYERS.index(highest)
        if lower and table.table_type != DIM_TABLE_TYPE:      # 维表取名称、部门不受层级限制
            diagnosis.reasons.append(f"④ 按层级从上往下选，这一轮用了{highest}的表，这张表在{table.layer}。")
        position = previous.candidate_tables.index(table_id) + 1
        if position > FULL_FIELD_TABLES:
            diagnosis.reasons.append(f"它排在候选第 {position} 张，第 {FULL_FIELD_TABLES + 1} 张起一般只列关键字段，④ 可能没看到它的全部字段。")
        if _mentions(" ".join([plan.table_basis, *plan.assumptions]), table.table_name):
            diagnosis.reasons.append("④ 的用表依据里提到了它，见上面的用表依据。")
        _missing_fields(diagnosis)
        if diagnosis.reasons == ([same_name] if same_name else []):
            have = "大体都有" if any(match.weak for match in diagnosis.matches) else "都有"
            diagnosis.reasons.append(f"字段名上看，需求要的它{have}；④ 没有写明不选它的理由，可以改用它重新生成比一比。")
    else:
        location = retriever.locate(query, table_id)
        if location.pool == "outside":
            diagnosis.stage = "outside"
            diagnosis.conclusion = "这张表不在第 0 层的召回范围里，④ 没看到它。"
            if previous.scope_level == 0:
                diagnosis.reasons.append(f"它在{table.layer}。第 0 层只查专题层、汇总层的表和标准层的维表，{table.layer}的表只有 ④ 说缺东西、上溯时才带进来，上一轮没有上溯。")
            else:
                diagnosis.reasons.append(f"它在{table.layer}。上一轮上溯了 {previous.scope_level} 层，这张表没有留下。")
        else:
            diagnosis.stage = "not_recalled"
            diagnosis.conclusion = "这张表没有进候选，④ 没看到它。"
            if location.rank is None:
                diagnosis.reasons.append("按上一轮的需求重放召回，它没有命中：表名、字段名、说明都和需求对不上。")
            elif location.pool == "standard":
                diagnosis.reasons.append(f"按上一轮的需求重放召回，它在有命中的 {location.ranked} 张标准表里排第 {location.rank}，候选只取前 {location.slots} 张。")
            else:
                diagnosis.reasons.append(f"它是非标准表，不和标准表一起排名；按上一轮的需求重放召回，它在有命中的 {location.ranked} 张非标准表里排第 {location.rank}，只补前 {location.slots} 张。")
            if location.rank is not None and location.rank <= location.slots:
                diagnosis.reasons.append("这次重放它排在名额内，和上一轮的记录不一致，可能是元数据换过版本。")
        _missing_fields(diagnosis)
    if diagnosis.gaps:
        diagnosis.reasons.append(f"它没有{'、'.join(diagnosis.gaps)}，④ 只能凭表名和字段名判断。")
    return diagnosis


def explain_previous(store: MetadataStore, retriever: Retriever, previous: RoundBasis | None,
                     names: Sequence[str]) -> Explanation:
    """只按上一轮依据和当前元数据解释选表，按名字逐一诊断，不读查询结果、不调模型"""
    explanation = Explanation()
    if previous is None or not previous.requirement:
        explanation.notes.append("这个会话还没有查询记录，没有可以解释的选表。")
    else:
        explanation.requirement = previous.requirement
        plan = previous.plan
        explanation.candidate_tables = list(previous.candidate_tables)
        if plan is None:
            explanation.notes.append("上一轮没有走到选表。" + previous.message)
        elif plan.decision == "ok":
            explanation.tables = [table.model_copy(deep=True) for table in plan.tables]
            explanation.table_basis = plan.table_basis
            explanation.caliber = plan.caliber
            explanation.dropped_tables = list(plan.dropped_tables)
        else:
            reason = plan.clarify.question if plan.clarify else plan.reason
            explanation.notes.append("上一轮没有选定用表：" + reason)
    if (previous and previous.metadata_version is not None and store.version_id is not None
            and previous.metadata_version != store.version_id):
        explanation.notes.append(f"上一轮用的元数据版本是 {previous.metadata_version}，现在是 {store.version_id}，下面的诊断按现在的元数据算。")
    explanation.named = [_diagnose(store, retriever, previous, name) for name in names]
    if explanation.named:
        explanation.summary = explanation.named[0].conclusion
        if len(explanation.named) > 1:
            explanation.summary += f"共问了 {len(explanation.named)} 张表。"
    elif explanation.tables:
        explanation.summary = f"上一轮用了 {len(explanation.tables)} 张表：{'、'.join(table.table for table in explanation.tables)}。"
    elif explanation.notes:
        explanation.summary = explanation.notes[0]
    return explanation
