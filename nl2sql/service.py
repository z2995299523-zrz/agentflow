"""
按资料目录装配查询流水线，界面、接口、评估共用
- build_pipeline：读元数据库，建血缘图、召回、校验器、模型和执行层
- get_metadata、get_pipeline：进程内缓存。元数据页和元数据接口只用 get_metadata，没配模型 Key 也能用
- reset_pipeline：重新导入元数据之后、测试里清缓存
"""
from pathlib import Path

from lineage.graph import LineageGraph
from metadata.index import Embedder
from metadata.store import MetadataStore
from nl2sql.llm import LLMClient
from nl2sql.workflow import Pipeline


def embedder_for(store: MetadataStore) -> Embedder | None:
    """按元数据库建向量时用的向量器取一个同样的向量器

    bge-small-zh-v1.5 → BgeEmbedder()（不触发加载）；hash-bigram-* → 同维度的 HashEmbedder；
    没有向量 → None；其它名字 → None，召回会写明只走了关键字路。
    """
    raise NotImplementedError("由 W2-D 实现")


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
    """
    raise NotImplementedError("由 W2-D 实现")


def get_metadata() -> tuple[MetadataStore, LineageGraph]:
    """进程内缓存的元数据和血缘图，只读，不需要模型配置"""
    raise NotImplementedError("由 W2-D 实现")


def get_pipeline() -> Pipeline:
    """进程内缓存的流水线，线程安全，复用 get_metadata 的结果"""
    raise NotImplementedError("由 W2-D 实现")


def reset_pipeline() -> None:
    """清掉 get_metadata、get_pipeline 的缓存"""
    raise NotImplementedError("由 W2-D 实现")
