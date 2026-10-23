"""
元数据差异
比较两个元数据库（候选版本和基线版本）规范化、补全之后的内容，不比原始文件：表、字段、血缘、关联、加工语句、样例。
找出疑似改名、失效样例（输入里还在、这一版被拒收），按阈值给出切换前要人确认的事项，渲染成差异报告。
本模块只依赖 metadata 包；要静态校验失效样例时，由调用方传入校验函数（同 metadata/examples.py）。
"""
import hashlib
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from metadata.examples import RawExample
from metadata.models import Example
from metadata.store import MetadataStore

DROP_RATIO = 0.05                    # 表、字段、血缘、关联、能解析的语句少了超过这个比例，要人确认
GROWTH_RATIO = 0.20                  # 表、字段多了超过这个比例，要人确认（来源格式变了、转换重复的信号）
PARSED_COUNT = "能结构化解析的语句"
MAX_ITEMS = 30                       # 报告里每个清单最多列几项
SHORT_SHA = 8                        # 报告里摘要只写前几位


class ValidationIssue(Protocol):
    """校验回调返回的错误，只读取规则和说明。"""

    rule: str
    message: str


class ValidationReport(Protocol):
    """校验回调的最小契约，底层不依赖查询流水线。"""

    @property
    def errors(self) -> Sequence[ValidationIssue]:
        """返回错误级问题。"""
        ...


@dataclass
class ObjectDiff:
    """一类对象的增删改；变化按对象键、属性名保存旧值与新值。"""

    added: dict[Any, Any] = field(default_factory=dict)
    removed: dict[Any, Any] = field(default_factory=dict)
    changed: dict[Any, dict[str, tuple[Any, Any]]] = field(default_factory=dict)


@dataclass
class MetadataDiff:
    """两版元数据的完整差异；疑似改名仍然保留为一删一增。"""

    baseline_counts: dict[str, int]
    candidate_counts: dict[str, int]
    tables: ObjectDiff = field(default_factory=ObjectDiff)
    columns: ObjectDiff = field(default_factory=ObjectDiff)
    lineage: ObjectDiff = field(default_factory=ObjectDiff)
    relations: ObjectDiff = field(default_factory=ObjectDiff)
    scripts: ObjectDiff = field(default_factory=ObjectDiff)
    examples: ObjectDiff = field(default_factory=ObjectDiff)
    table_renames: list[tuple[str, str]] = field(default_factory=list)
    column_renames: list[tuple[tuple[str, str], tuple[str, str]]] = field(default_factory=list)
    example_changes: list[tuple[Example, Example]] = field(default_factory=list)
    invalid_examples: list[tuple[Example, list[str]]] = field(default_factory=list)
    script_baseline_files: set[str] = field(default_factory=set)
    script_candidate_files: set[str] = field(default_factory=set)


def _sha(text: str) -> str:
    """文本的 sha256"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _example_key(example: RawExample | Example) -> str:
    """样例按内容认同一条：需求加 SQL 的摘要。编号是文件内位置，中间删一条后面的都会变，不能用"""
    return _sha(example.requirement + "\n" + example.sql_text)


def _compare(old: dict, new: dict, attributes: Sequence[str]) -> ObjectDiff:
    """按键比较两边的对象：只在一边的是新增或删除，两边都有的逐个比 attributes"""
    result = ObjectDiff({key: new[key] for key in sorted(new.keys() - old.keys())},
                        {key: old[key] for key in sorted(old.keys() - new.keys())})
    for key in sorted(old.keys() & new.keys()):
        changes = {name: (old[key][name], new[key][name]) for name in attributes
                   if old[key][name] != new[key][name]}
        if changes:
            result.changed[key] = changes
    return result


def _models(items: Sequence, key: Callable) -> dict:
    """模型对象按 key 摊成 键 → 字典"""
    return {key(item): item.model_dump() for item in items}


def _scripts(store: MetadataStore) -> dict:
    """加工语句按（文件名, 目标表, 同一文件里写这张表的第几条）认同一条，比原文摘要和能否结构化解析"""
    counters: Counter = Counter()
    rows = {}
    for item in sorted(store.scripts(), key=lambda s: (s.script_name, s.stmt_index)):
        pair = (item.script_name, item.target_table)
        counters[pair] += 1
        rows[(*pair, counters[pair])] = {"sha256": _sha(item.sql_text), "parsed": item.parsed}
    return rows


def diff_stores(baseline: MetadataStore, candidate: MetadataStore, *,
                candidate_examples: Sequence[RawExample] = (),
                validate: Callable[[str], ValidationReport] | None = None) -> MetadataDiff:
    """比较两个完成导入的元数据库；输入样例用于区分主动删除和拒收失效。"""
    old_counts, new_counts = baseline.counts(), candidate.counts()
    old_counts[PARSED_COUNT] = sum(s.parsed for s in baseline.scripts())
    new_counts[PARSED_COUNT] = sum(s.parsed for s in candidate.scripts())
    result = MetadataDiff(old_counts, new_counts)
    old_tables = _models(baseline.tables(), lambda t: t.table_id)
    new_tables = _models(candidate.tables(), lambda t: t.table_id)
    old_columns = _models([c for t in baseline.tables() for c in baseline.columns(t.table_id)],
                          lambda c: (c.table_id, c.column_name))
    new_columns = _models([c for t in candidate.tables() for c in candidate.columns(t.table_id)],
                          lambda c: (c.table_id, c.column_name))
    result.tables = _compare(old_tables, new_tables,
                             ("layer", "table_type", "grain_columns", "date_column", "table_cn", "description"))
    result.columns = _compare(old_columns, new_columns,
                              ("column_cn", "data_type", "is_pk", "description", "enum_values"))
    edge_key = lambda e: (e.target_table, e.target_column, e.source_table, e.source_column)
    result.lineage = _compare(_models(baseline.lineage_edges(), edge_key),
                              _models(candidate.lineage_edges(), edge_key), ("transform", "origin"))
    relation_key = lambda r: (r.left_table, tuple(r.left_columns), r.right_table, tuple(r.right_columns))
    result.relations = _compare(_models(baseline.relations(), relation_key),
                                _models(candidate.relations(), relation_key), ("cardinality", "origin"))
    result.scripts = _compare(_scripts(baseline), _scripts(candidate), ("sha256", "parsed"))
    result.script_baseline_files = {s.script_name for s in baseline.scripts()}
    result.script_candidate_files = {s.script_name for s in candidate.scripts()}

    # 确定性的一对一配对，先用中文名，再用类型与序号；不从增删集合里移走。
    used, paired = set(), set()
    added_by_table: dict[str, list[tuple[tuple[str, str], dict[str, Any]]]] = defaultdict(list)
    for key, value in result.columns.added.items():
        added_by_table[key[0]].append((key, value))
    for chinese_first in (True, False):
        for key, old in result.columns.removed.items():
            if key in paired:
                continue
            for new_key, new in added_by_table[key[0]]:
                if new_key in used:
                    continue
                matches = (bool(old["column_cn"]) and old["column_cn"] == new["column_cn"] if chinese_first else
                           (old["data_type"], old["ordinal"]) == (new["data_type"], new["ordinal"]))
                if matches:
                    used.add(new_key)
                    paired.add(key)
                    result.column_renames.append((key, new_key))
                    break
    used_tables = set()
    for key, old in result.tables.removed.items():
        old_names = {c.column_name for c in baseline.columns(key)}
        for new_key, new in result.tables.added.items():
            if new_key in used_tables or old["table_cn"] != new["table_cn"]:
                continue
            new_names = {c.column_name for c in candidate.columns(new_key)}
            if len(old_names & new_names) / max(len(old_names), len(new_names), 1) >= 0.8:
                used_tables.add(new_key)
                result.table_renames.append((key, new_key))
                break

    old_examples = {_example_key(e): e for e in baseline.examples()}
    new_examples = {_example_key(e): e for e in candidate.examples()}
    result.examples.added = {k: new_examples[k] for k in sorted(new_examples.keys() - old_examples.keys())}
    result.examples.removed = {k: old_examples[k] for k in sorted(old_examples.keys() - new_examples.keys())}
    raw_keys = {_example_key(e) for e in candidate_examples}
    for key, example in list(result.examples.removed.items()):
        if key in raw_keys:
            reasons = ([f"{e.rule} {e.message}" for e in validate(example.sql_text).errors[:3]]
                       if validate else [])
            result.invalid_examples.append((example, reasons))
            del result.examples.removed[key]
    for key, old in list(result.examples.removed.items()):
        match = next((k for k, new in result.examples.added.items() if new.requirement == old.requirement), None)
        if match is not None:
            result.example_changes.append((old, result.examples.added.pop(match)))
            del result.examples.removed[key]
    return result


def confirm_items(diff: MetadataDiff) -> list[str]:
    """按固定顺序生成需人工确认的中文说明，阈值均为严格超过。"""
    items = []
    if diff.tables.removed or diff.columns.removed:
        items.append(f"删了 {len(diff.tables.removed)} 张表、{len(diff.columns.removed)} 个字段")
    tables, columns = len(diff.table_renames), len(diff.column_renames)
    if tables or columns:
        items.append(f"疑似改名 {tables + columns} 处（表 {tables}、字段 {columns}）")
    if diff.invalid_examples:
        items.append(f"失效样例 {len(diff.invalid_examples)} 条")
    for category in ("表", "字段", "血缘", "关联关系", PARSED_COUNT):
        old, new = diff.baseline_counts.get(category, 0), diff.candidate_counts.get(category, 0)
        if old and (old - new) / old > DROP_RATIO:
            items.append(f"{category} {old} → {new}（少 {(old - new) / old:.1%}）")
    for category in ("表", "字段"):
        old, new = diff.baseline_counts.get(category, 0), diff.candidate_counts.get(category, 0)
        if old and (new - old) / old > GROWTH_RATIO:
            items.append(f"{category} {old} → {new}（多 {(new - old) / old:.1%}）")
    if (diff.baseline_counts.get("字段向量", 0) and
            diff.candidate_counts.get("字段向量", 0) < diff.candidate_counts.get("字段", 0)):
        items.append(f"字段向量不全：{diff.candidate_counts.get('字段向量', 0)} / "
                     f"{diff.candidate_counts.get('字段', 0)}（上一版有向量）")
    return items


def _cell(value: Any) -> str:
    """竖线和换行会把 Markdown 撑乱，转成安全的写法"""
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _listing(title: str, items: Sequence[str]) -> list[str]:
    """一个清单：标题带总数，最多列 MAX_ITEMS 项"""
    lines = [f"- {title}：{len(items)} 项"]
    lines += [f"  - {_cell(item)}" for item in items[:MAX_ITEMS]]
    if len(items) > MAX_ITEMS:
        lines.append(f"  - 等共 {len(items)} 项")
    return lines


def _changes(changes: dict[str, tuple[Any, Any]]) -> str:
    """属性变化写成「属性：旧 → 新」"""
    return "；".join(f"{name}：{old} → {new}" for name, (old, new) in changes.items())


def _summary(diff: ObjectDiff, group: Callable) -> list[str]:
    """血缘、关联这类条数多的对象只给总数，再按表汇总，变化多的在前"""
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for index, collection in enumerate((diff.added, diff.removed, diff.changed)):
        for key in collection:
            counts[group(key)][index] += 1
    rows = [f"{key}：新增 {a}、删除 {r}、变化 {c} 条"
            for key, (a, r, c) in sorted(counts.items(), key=lambda item: (-sum(item[1]), item[0]))]
    return [f"新增 {len(diff.added)}、删除 {len(diff.removed)}、变化 {len(diff.changed)} 条", *_listing("按表汇总", rows)]


def render_diff(diff: MetadataDiff, *, header: Sequence[str] = (), confirm: Sequence[str] = (),
                extra_sections: Sequence[tuple[str, Sequence[str]]] = ()) -> str:
    """生成有长度上限的 Markdown 差异报告，动态单元格转义竖线和换行。"""
    lines = ["# 元数据差异报告", "", *header, "", "## 需确认项", ""]
    lines += [f"{i}. {_cell(item)}" for i, item in enumerate(confirm[:MAX_ITEMS], 1)] or ["没有"]
    if len(confirm) > MAX_ITEMS:
        lines.append(f"等共 {len(confirm)} 项")
    lines += ["", "## 数量", "", "| 类别 | 基线 | 这一版 | 变化 |", "|---|---:|---:|---:|"]
    for category in dict.fromkeys([*diff.baseline_counts, *diff.candidate_counts]):
        old, new = diff.baseline_counts.get(category, 0), diff.candidate_counts.get(category, 0)
        lines.append(f"| {_cell(category)} | {old} | {new} | {new - old:+d} |")
    lines += ["", "## 表", ""]
    lines += _listing("新增", list(diff.tables.added)) + _listing("删除", list(diff.tables.removed))
    lines += _listing("属性变化", [f"{k}：{_changes(v)}" for k, v in diff.tables.changed.items()])
    lines += _listing("疑似改名", [f"{a} → {b}" for a, b in diff.table_renames])
    lines += ["", "## 字段", ""]
    grouped: dict[str, list[str]] = defaultdict(list)
    for action, collection in (("新增", diff.columns.added), ("删除", diff.columns.removed),
                               ("修改", diff.columns.changed)):
        for (table, column), value in collection.items():
            detail = f"：{_changes(value)}" if action == "修改" else ""
            grouped[table].append(f"{action} {column}{detail}")
    for (table, old), (_, new) in diff.column_renames:
        grouped[table].append(f"疑似改名 {old} → {new}")
    # 分组本身也限长，避免大批表的删除撑大报告。
    for table in sorted(grouped)[:MAX_ITEMS]:
        lines += _listing(table, grouped[table])
    if len(grouped) > MAX_ITEMS:
        lines.append(f"- 等共 {len(grouped)} 项（表）")
    if not grouped:
        lines.append("没有变化")
    lines += ["", "## 血缘", "", *_summary(diff.lineage, lambda k: k[0]),
              "", "## 关联关系", "", *_summary(diff.relations, lambda k: f"{k[0]} ↔ {k[2]}"),
              "", "## 加工脚本", ""]
    old_files = {k[0] for k in diff.scripts.removed}
    new_files = {k[0] for k in diff.scripts.added}
    # 同一文件内的增删语句同样属于文件内容变化。旧/新文件全集由差异之外的共同语句补足。
    added_files = new_files - diff.script_baseline_files
    removed_files = old_files - diff.script_candidate_files
    changed_files = ({k[0] for k in diff.scripts.changed} | old_files | new_files) - added_files - removed_files
    lines += _listing("新增文件", sorted(added_files)) + _listing("删除文件", sorted(removed_files))
    lines += _listing("内容变了的文件", sorted(changed_files))
    lines += _listing("能解析变成不能解析的语句", [f"{k[0]} → {k[1]} 第 {k[2]} 条"
                         for k, v in diff.scripts.changed.items() if v.get("parsed") == (True, False)])
    lines += ["", "## 样例", ""]
    for action, examples in (("新增", diff.examples.added.values()), ("删除", diff.examples.removed.values())):
        lines += _listing(action, [f"{e.requirement}（{e.source_file}）" for e in examples])
    lines += _listing("修改", [f"{old.requirement}：SQL 变了（摘要 {_sha(old.sql_text)[:SHORT_SHA]} → "
                               f"{_sha(new.sql_text)[:SHORT_SHA]}，{new.source_file}）"
                               for old, new in diff.example_changes])
    lines += _listing("失效", [f"{e.requirement}：{'；'.join(reasons) or '这一版拒收，未提供校验原因'}"
                               for e, reasons in diff.invalid_examples])
    for title, section in extra_sections:
        lines += ["", f"## {title}", "", *section]
    return "\n".join(lines) + "\n"
