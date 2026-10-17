"""
按资料目录装配查询流水线，界面、接口、评估共用
- build_pipeline：读元数据库，建血缘图、召回、校验器、模型和执行层
- get_metadata、get_pipeline：进程内缓存。元数据页和元数据接口只用 get_metadata，没配模型 Key 也能用
- reset_pipeline：重新导入元数据之后、测试里清缓存
- describe_lineage：一个字段的上游、下游链路，元数据接口和元数据页共用
"""
import threading
from pathlib import Path
from typing import Any

import config
from lineage.graph import Hop, LineageGraph
from metadata.importer import default_db_path
from metadata.index import BGE_NAME, BgeEmbedder, Embedder, HashEmbedder
from metadata.store import MetadataStore
from nl2sql.evidence import JOIN_PREFIXES
from nl2sql.executor import Executor, get_executor
from nl2sql.llm import ChatModelClient, LLMClient
from nl2sql.retriever import Retriever
from nl2sql.validator import SqlValidator
from nl2sql.workflow import Pipeline, PipelineDeps

HASH_EMBEDDER_PREFIX = "hash-bigram-"        # HashEmbedder.name 的写法：hash-bigram-<维度>
IMPORT_COMMAND = "python -m metadata.importer"

_LOCK = threading.RLock()                    # get_pipeline 持锁时还会调 get_metadata，所以用可重入锁
_metadata: tuple[MetadataStore, LineageGraph] | None = None
_pipeline: Pipeline | None = None


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


def get_metadata() -> tuple[MetadataStore, LineageGraph]:
    """进程内缓存的元数据和血缘图，只读，不需要模型配置

    Raises:
        FileNotFoundError: 元数据库不存在，信息里带导入命令
    """
    global _metadata
    with _LOCK:
        if _metadata is None:
            _metadata = _load_metadata(Path(config.MART_DIR))
        return _metadata


def get_pipeline() -> Pipeline:
    """进程内缓存的流水线，线程安全，复用 get_metadata 的结果

    Raises:
        FileNotFoundError: 元数据库不存在
        ValueError: 没有配置模型的 Key
    """
    global _pipeline
    with _LOCK:
        if _pipeline is None:
            store, graph = get_metadata()
            _pipeline = _assemble(store, graph, Path(config.MART_DIR), None, None)
        return _pipeline


def reset_pipeline() -> None:
    """清掉 get_metadata、get_pipeline 的缓存"""
    global _metadata, _pipeline
    with _LOCK:
        _metadata = None
        _pipeline = None


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
        {"field": 模式.表.字段, "upstream": [跳, ...], "downstream": [跳, ...]}

    Raises:
        LookupError: 表或字段不在元数据里
    """
    if store.table(table) is None:
        raise LookupError(f"表不存在：{table}")
    if store.column(table, column) is None:
        raise LookupError(f"字段不存在：{_field_id(table, column)}")
    return {
        "field": _field_id(table, column),
        "upstream": [_hop_dict(hop) for hop in graph.upstream(table, column, depth)],
        "downstream": [_hop_dict(hop) for hop in graph.downstream(table, column, depth)],
    }
