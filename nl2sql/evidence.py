"""
③ 证据组装
把候选召回的结果展开成给模型看的证据：候选表的结构、指标字段的加工链路、同源字段对比、表之间的上下游、
关联路径、还可以上溯到的表、相似样例。全部是确定性代码，同样的输入必然得到同样的 Evidence。

对照实验的三组：A 组（仅模型结构）没有血缘、脚本、样例，关联路径只来自表结构推得出的关联；
B 组加血缘；C 组再加样例。
"""
from nl2sql.retriever import RecallQuery, RecallResult
from nl2sql.schemas import Evidence
from lineage.graph import LineageGraph
from metadata.store import MetadataStore

LINEAGE_DEPTH = 3
MAX_LINEAGE_FIELDS = 12          # 有同源不同口径的字段时可以超出，但总数不超过 20
MAX_STEPS_PER_FIELD = 12
MAX_COMPARE_FIELDS = 6           # 每个指标检索词参与两两比较的字段数
MAX_JOIN_HINTS = 20
MAX_JOIN_TABLES = 3              # 关联路径经过的中间表最多补几张
MAX_UPSTREAM_OPTIONS = 10
FULL_DESC_CHARS = 200
SHORT_DESC_CHARS = 60
ENUM_CHARS = 200
EXPR_CHARS = 800
COND_CHARS = 300
MAX_CONDS = 6


def build_evidence(store: MetadataStore, graph: LineageGraph, recall: RecallResult, query: RecallQuery, *,
                   use_lineage: bool, use_examples: bool, all_layers: bool = False,
                   join_graph: LineageGraph | None = None) -> Evidence:
    """由召回结果组装证据

    Args:
        store: 元数据库
        graph: 血缘图
        recall: ② 的结果
        query: ② 用的检索词，指标检索词决定展开哪些字段的血缘
        use_lineage: 是否给血缘、同源对比、表的上下游、上溯选项（B、C 组）
        use_examples: 是否给相似样例（C 组）
        all_layers: 召回是否在全部层级里排名（A 组），此时没有上溯选项
        join_graph: 求关联路径用的图，None 时用 graph。A 组传 structure_store 建出来的图

    Returns:
        证据
    """
    raise NotImplementedError("由 W1-A 实现")


def drop_examples(recall: RecallResult) -> RecallResult:
    """A、B 组用：去掉样例和样例带入的表，其余不变

    Args:
        recall: ② 的结果

    Returns:
        新的召回结果，不修改传入的对象
    """
    raise NotImplementedError("由 W1-A 实现")


def structure_store(store: MetadataStore) -> MetadataStore:
    """只有表结构的元数据：表、字段和 structural_relations 推出的关联，没有血缘、脚本、样例、向量

    A 组的关联路径用它建图。不能从元数据库里按来源筛：导入时同一种映射只留优先级最高的来源，
    表结构推得出的关联可能已经被血缘、脚本推出的同一映射顶掉了。

    Args:
        store: 完整的元数据库

    Returns:
        新的只读入口
    """
    raise NotImplementedError("由 W1-A 实现")
