"""
按资料目录装配查询流水线，界面、接口、评估共用
- build_pipeline：读元数据库，建血缘图、召回、校验器、模型和执行层
- get_metadata、get_pipeline：进程内缓存。元数据页和元数据接口只用 get_metadata，没配模型 Key 也能用；
  元数据库文件变了（重新导入过）就重新读，流水线也跟着重新装配，不用重启
- warm_up：接口启动时在后台预热（装配流水线、加载向量模型）；装配期间元数据、会话历史照常能取
- get_history：当前资料目录的会话历史库（资料目录/build/history.db）
- reset_pipeline：测试里清缓存
- describe_lineage：一个字段的上游、下游链路；search_metadata：按关键字找表和字段；元数据接口共用
"""
import logging
import threading
import time
from pathlib import Path
from typing import Any

import config
from lineage.graph import Hop, LineageGraph
from metadata.importer import default_db_path
from metadata.index import BGE_NAME, BgeEmbedder, Embedder, HashEmbedder
from metadata.models import LAYERS, ColumnMeta, TableMeta
from metadata.store import MetadataStore
from nl2sql.evidence import JOIN_PREFIXES
from nl2sql.executor import Executor, get_executor
from nl2sql.history import HISTORY_FILE_NAME, ConversationStore
from nl2sql.llm import ChatModelClient, LLMClient
from nl2sql.retriever import Retriever
from nl2sql.validator import SqlValidator
from nl2sql.workflow import Pipeline, PipelineDeps

HASH_EMBEDDER_PREFIX = "hash-bigram-"        # HashEmbedder.name 的写法：hash-bigram-<维度>
IMPORT_COMMAND = "python -m metadata.importer"
SEARCH_LAYER_ORDER = tuple(reversed(LAYERS))  # 搜索结果里专题层排前面：查数最常用

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()                    # 保护下面几个缓存变量，只在读写缓存时短暂持有
_ASSEMBLE_LOCK = threading.Lock()            # 装配流水线时持有：第一次要导入模型库，机械盘上要一两分钟，
                                             # 只让要流水线的请求排队，元数据、会话历史的请求不受影响
_metadata: tuple[MetadataStore, LineageGraph] | None = None
_metadata_key: tuple[str, int] | None = None  # 缓存的元数据来自哪个文件、文件的修改时间
_pipeline: Pipeline | None = None
_history: ConversationStore | None = None


def embedder_for(store: MetadataStore) -> Embedder | None:
    """按元数据库建向量时用的向量器取一个同样的向量器

    bge-small-zh-v1.5 → BgeEmbedder()（不触发加载）；hash-bigram-* → 同维度的 HashEmbedder；
    没有向量 → None；其它名字 → None，召回会写明只走了关键字路。
    """
    name = store.embedder_name
    if name == BGE_NAME:
        return BgeEmbedder()
    if name is not None and name.startswith(HASH_EMBEDDER_PREFIX):
        try:
            return HashEmbedder(int(name[len(HASH_EMBEDDER_PREFIX):]))
        except ValueError:
            return None
    return None


def _import_hint(mart_dir: Path) -> str:
    """导入元数据的命令，资料目录带空格时加引号"""
    path = str(mart_dir)
    return f'{IMPORT_COMMAND} "{path}"' if " " in path else f"{IMPORT_COMMAND} {path}"


def _load_metadata(mart_dir: Path) -> tuple[MetadataStore, LineageGraph]:
    """读资料目录对应的元数据库并建血缘图

    Raises:
        FileNotFoundError: 元数据库不存在，信息里带导入命令
        RuntimeError: 文件不是元数据库，或者是旧版本导入器生成的
    """
    db_path = default_db_path(mart_dir)
    if not db_path.exists():
        raise FileNotFoundError(f"元数据库不存在: {db_path}。请先运行 {_import_hint(mart_dir)} 导入资料")
    store = MetadataStore(db_path)
    return store, LineageGraph(store)


def _make_executor(kind: str) -> Executor:
    """按配置建执行层；duckdb 用模拟专题库，不存在或过期时先重新生成

    Raises:
        ValueError: 执行层的名字不支持
    """
    if kind == "duckdb":
        from data.mock_mart.build_mock import DEFAULT_DB_PATH, ensure_built
        ensure_built()
        return get_executor(kind, DEFAULT_DB_PATH, config.SQL_DIALECT)
    return get_executor(kind, None, config.SQL_DIALECT)


def _assemble(store: MetadataStore, graph: LineageGraph, mart_dir: Path, llm: LLMClient | None,
              executor_kind: str | None) -> Pipeline:
    """用已经读好的元数据装配流水线。容易失败的先做（没配 Key、执行层名字不对），建召回索引放在最后"""
    if llm is None:
        llm = ChatModelClient(timeout=config.LLM_TIMEOUT)
    executor = _make_executor(executor_kind or config.EXECUTOR)
    retriever = Retriever(store, graph, embedder_for(store))
    validator = SqlValidator(store, dialect=config.SQL_DIALECT)
    return Pipeline(PipelineDeps(store=store, graph=graph, retriever=retriever, validator=validator, llm=llm,
                                 executor=executor, mart_dir=mart_dir))


def build_pipeline(mart_dir: Path | None = None, *, llm: LLMClient | None = None,
                   executor_kind: str | None = None) -> Pipeline:
    """装配一条查询流水线

    Args:
        mart_dir: 资料目录，默认 config.MART_DIR
        llm: 模型，默认 ChatModelClient(timeout=config.LLM_TIMEOUT)
        executor_kind: 执行层，默认 config.EXECUTOR；duckdb 用模拟库

    Returns:
        流水线

    Raises:
        FileNotFoundError: 元数据库不存在，信息里带导入命令
        ValueError: 没有配置模型的 Key，或者执行层的名字不支持
    """
    mart_dir = Path(mart_dir) if mart_dir is not None else Path(config.MART_DIR)
    store, graph = _load_metadata(mart_dir)
    return _assemble(store, graph, mart_dir, llm, executor_kind)


def _db_key(mart_dir: Path) -> tuple[str, int] | None:
    """元数据库的路径和修改时间（纳秒）；文件不存在时为 None"""
    db_path = default_db_path(mart_dir)
    try:
        return str(db_path), db_path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def get_metadata() -> tuple[MetadataStore, LineageGraph]:
    """进程内缓存的元数据和血缘图，只读，不需要模型配置

    每次取的时候比对元数据库文件的路径和修改时间：重新导入过（导入器写完整个文件再替换），就重新读，
    并清掉流水线的缓存，下次 get_pipeline 用新的元数据重新装配。

    Raises:
        FileNotFoundError: 元数据库不存在，信息里带导入命令
    """
    global _metadata, _metadata_key, _pipeline
    with _LOCK:
        mart_dir = Path(config.MART_DIR)
        key = _db_key(mart_dir)
        if _metadata is None or key != _metadata_key:
            _metadata = _load_metadata(mart_dir)
            _metadata_key = key
            _pipeline = None
        return _metadata


def get_pipeline() -> Pipeline:
    """进程内缓存的流水线，线程安全，复用 get_metadata 的结果；元数据重新读过就重新装配

    装配在 _ASSEMBLE_LOCK 里做，不占 _LOCK：装配期间 get_metadata、get_history 照常能取。
    装配时元数据被重新读过（或者缓存被清掉了），装好的这条只给这一次用，不进缓存。

    Raises:
        FileNotFoundError: 元数据库不存在
        ValueError: 没有配置模型的 Key
    """
    global _pipeline
    with _ASSEMBLE_LOCK:
        store, graph = get_metadata()
        with _LOCK:
            if _pipeline is not None and _metadata is not None and _metadata[0] is store:
                return _pipeline
        pipeline = _assemble(store, graph, Path(config.MART_DIR), None, None)
        with _LOCK:
            if _metadata is not None and _metadata[0] is store:
                _pipeline = pipeline
        return pipeline


def warm_up() -> None:
    """预热：装配流水线，再让向量模型加载一次，第一问就不用等冷启动

    机械盘上第一次导入模型库、加载 BGE 要一两分钟。接口启动时在后台线程里调它；
    没导入元数据、没配 Key 时只记一条日志，等真正查数时再如实报错。
    """
    started = time.perf_counter()
    try:
        pipeline = get_pipeline()
        embedder = embedder_for(pipeline.deps.store)
        if embedder is not None:
            embedder.embed(["预热"])
    except Exception as error:  # noqa: BLE001 预热失败不影响接口启动
        logger.warning("预热没有完成：%s", error)
        return
    logger.info("预热完成，用时 %.1f 秒", time.perf_counter() - started)


def history_path(mart_dir: Path) -> Path:
    """会话历史库的路径：资料目录/build/history.db，和元数据库在同一个目录，不入库"""
    return default_db_path(mart_dir).parent / HISTORY_FILE_NAME


def get_history() -> ConversationStore:
    """当前资料目录的会话历史库，进程内缓存；资料目录换了就换一个库

    Raises:
        OSError: 目录建不了或者库文件写不了
    """
    global _history
    with _LOCK:
        path = history_path(Path(config.MART_DIR))
        if _history is None or _history.path != path:
            _history = ConversationStore(path)
        return _history


def reset_pipeline() -> None:
    """清掉 get_metadata、get_pipeline、get_history 的缓存"""
    global _metadata, _metadata_key, _pipeline, _history
    with _LOCK:
        _metadata = None
        _metadata_key = None
        _pipeline = None
        _history = None


def _field_id(table: str, column: str) -> str:
    """字段的写法：模式.表.字段"""
    return f"{table}.{column}"


def _hop_dict(hop: Hop) -> dict[str, Any]:
    """血缘的一跳写成可以直接转 JSON 的字典：过滤条件写成「WHERE 条件」，关联条件写成「LEFT JOIN 表 ON 条件」"""
    joins = []
    for item in hop.joins:
        head = f"{JOIN_PREFIXES.get(item.join_type, item.join_type)} {item.table}"
        joins.append(f"{head} ON {item.condition}" if item.condition else head)
    return {
        "depth": hop.depth, "target": _field_id(hop.target.table, hop.target.column),
        "source": _field_id(hop.source.table, hop.source.column), "transform": hop.transform,
        "script_expression": hop.script_expression, "is_passthrough": hop.is_passthrough, "origin": hop.origin,
        "script_name": hop.script_name, "source_external": hop.source_external, "source_known": hop.source_known,
        "filters": [f"{item.clause} {item.condition}" for item in hop.filters], "joins": joins,
    }


def describe_lineage(store: MetadataStore, graph: LineageGraph, table: str, column: str,
                     depth: int = 3) -> dict[str, Any]:
    """一个字段的上游链路（由哪些字段加工而来）和下游链路（加工出了哪些字段），可以直接转 JSON

    每一跳里，target 是被加工出来的字段，source 是它的来源；上游、下游都是这个方向。

    Args:
        store: 元数据
        graph: 血缘图
        table: 表标识（模式.表）
        column: 字段名
        depth: 最多走几跳

    Returns:
        {"field": 模式.表.字段, "upstream": [跳, ...], "downstream": [跳, ...],
         "fields": {模式.表.字段: 字段说明, ...}}；fields 含起点和各跳涉及的全部字段，画血缘图时用

    Raises:
        LookupError: 表或字段不在元数据里
    """
    if store.table(table) is None:
        raise LookupError(f"表不存在：{table}")
    if store.column(table, column) is None:
        raise LookupError(f"字段不存在：{_field_id(table, column)}")
    upstream = graph.upstream(table, column, depth)
    downstream = graph.downstream(table, column, depth)
    refs = {(table, column)}
    for hop in (*upstream, *downstream):
        refs.update(((hop.source.table, hop.source.column), (hop.target.table, hop.target.column)))
    return {
        "field": _field_id(table, column),
        "upstream": [_hop_dict(hop) for hop in upstream],
        "downstream": [_hop_dict(hop) for hop in downstream],
        "fields": {_field_id(*ref): _field_brief(store, *ref) for ref in sorted(refs)},
    }


def _field_brief(store: MetadataStore, table: str, column: str) -> dict[str, Any]:
    """血缘图上一个字段的说明：所在表的中文名和层级、字段的中文名；known 为 False 表示不在字段清单里（外部表等）"""
    table_meta = store.table(table)
    column_meta = store.column(table, column)
    return {"table": table, "column": column, "table_cn": table_meta.table_cn if table_meta else "",
            "layer": table_meta.layer if table_meta else "",
            "column_cn": column_meta.column_cn if column_meta else "", "known": column_meta is not None}


def table_summary(store: MetadataStore, table: TableMeta) -> dict[str, Any]:
    """表清单里的一项：表标识、中文名、层级、类型、粒度字段、数据日期字段、说明、字段数"""
    return {"table_id": table.table_id, "table_cn": table.table_cn, "layer": table.layer,
            "table_type": table.table_type, "grain_columns": table.grain_columns, "date_column": table.date_column,
            "description": table.description, "column_count": len(store.columns(table.table_id))}


def column_summaries(store: MetadataStore, graph: LineageGraph, table: str) -> list[dict[str, Any]]:
    """一张表的字段清单，按表内顺序；每个字段带上直接来源、直接去向各有几条血缘

    Raises:
        LookupError: 表不在元数据里
    """
    if store.table(table) is None:
        raise LookupError(f"表不存在：{table}")
    return [{**column.model_dump(exclude={"table_id"}),
             "upstream_count": len(graph.upstream(table, column.column_name, 1)),
             "downstream_count": len(graph.downstream(table, column.column_name, 1))}
            for column in store.columns(table)]


def _match_rank(word: str, *names: str) -> int | None:
    """关键字和几个名字的匹配程度：0 完全一样，1 开头一样，2 包含；都不包含时为 None。word 已经是小写"""
    ranks = [0 if name == word else 1 if name.startswith(word) else 2
             for name in (item.lower() for item in names) if word in name]
    return min(ranks) if ranks else None


def search_metadata(store: MetadataStore, keyword: str, limit: int = 50) -> dict[str, Any]:
    """按关键字找表和字段，不区分大小写：表比表标识和中文名，字段比字段名和中文名

    排序：完全一样的在前，其次是开头一样的，再次是包含的；同一档里专题层在前，再按元数据里的顺序。

    Returns:
        {"tables": [表清单里的项, ...], "columns": [字段, ...], "table_total": 命中的表数, "column_total": 命中的字段数}，
        tables、columns 各自最多 limit 个
    """
    word = keyword.strip().lower()
    layer_rank = {layer: index for index, layer in enumerate(SEARCH_LAYER_ORDER)}
    tables: list[tuple[tuple[int, int], TableMeta]] = []
    columns: list[tuple[tuple[int, int], TableMeta, ColumnMeta]] = []
    for table in store.tables() if word else ():
        layer = layer_rank.get(table.layer, len(layer_rank))
        rank = _match_rank(word, table.table_id, table.table_cn)
        if rank is not None:
            tables.append(((rank, layer), table))
        for column in store.columns(table.table_id):
            rank = _match_rank(word, column.column_name, column.column_cn)
            if rank is not None:
                columns.append(((rank, layer), table, column))
    tables.sort(key=lambda item: item[0])                  # 排序是稳定的：同一档里保持元数据里的顺序
    columns.sort(key=lambda item: item[0])
    return {
        "tables": [table_summary(store, table) for _, table in tables[:limit]],
        "columns": [{"table_id": table.table_id, "table_cn": table.table_cn, "layer": table.layer,
                     "column_name": column.column_name, "column_cn": column.column_cn,
                     "data_type": column.data_type} for _, table, column in columns[:limit]],
        "table_total": len(tables), "column_total": len(columns),
    }
