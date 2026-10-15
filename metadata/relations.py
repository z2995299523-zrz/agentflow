"""
关联关系推导
四种来源按优先级合并：手工填写、加工脚本里出现过的等值关联、经直取血缘传递、同名粒度键。
同一对表按字段映射去重，每对最多保留 MAX_MAPPINGS_PER_PAIR 种，最优的排第一。

骨架：接口已定，实现由第二份计划的 D 任务补上
"""
from metadata.checks import ReportSection
from metadata.models import FieldRef, Mart, Relation, TableMeta

SECTION_RELATIONS = "关联关系推导"
GENERIC_KEY_NAMES = frozenset({"id", "uuid", "guid", "row_id", "rowid", "pk_id"})
MAX_DOMAIN_SIZE = 50              # 键域超过这么多字段时不从它传递
MAX_PROPAGATION_PER_JOIN = 400    # 一条脚本关联传递出的候选超过这么多对时整条跳过
MAX_GRAIN_FANIN = 150             # 一张表被超过这么多张表按同名粒度键关联时跳过
MAX_MAPPINGS_PER_PAIR = 3         # 同一对表最多保留几种字段映射


def cardinality(left: TableMeta, left_cols: list[str], right: TableMeta, right_cols: list[str]) -> str:
    """按粒度判断对应关系

    关联键覆盖了某一侧表的全部粒度字段，这一侧就唯一；表没有粒度字段时这一侧记为未知。

    Returns:
        N:1、1:1、1:N、N:N 或「未知」
    """
    raise NotImplementedError


def key_domains(mart: Mart) -> dict[FieldRef, int]:
    """求键域：被直取血缘连在一起的字段取值范围相同，属于同一个键域

    Returns:
        字段 → 键域编号
    """
    raise NotImplementedError


def derive_relations(mart: Mart) -> tuple[list[Relation], ReportSection]:
    """推导全部关联关系，包含手工填写的

    Args:
        mart: 已经解析过加工脚本、合并过血缘的资料

    Returns:
        (已定向、去重、排序的关联关系, 报告节)
    """
    return list(mart.relations), ReportSection(SECTION_RELATIONS, ())


def relation_rank(rel: Relation, tables: dict[str, TableMeta]) -> tuple:
    """关联关系的排序键：来源优先级、对应关系安全度、是否同名键、字典序，越小越优"""
    raise NotImplementedError
