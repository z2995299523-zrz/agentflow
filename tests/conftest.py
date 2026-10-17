"""测试公共夹具"""
import os
from pathlib import Path

import pytest

# 单元测试一律用哈希向量器，不加载 BGE 模型。写成环境变量而不是夹具，子进程和导入期也管得到
os.environ.setdefault("METADATA_EMBEDDER", "hash")

from metadata.importer import enrich_mart, load_mart  # noqa: E402
from metadata.store import MetadataStore  # noqa: E402
from tests.helpers import TINY_MART, write_mart  # noqa: E402

MOCK_MART_DIR = Path(__file__).resolve().parents[1] / "data" / "mock_mart"


@pytest.fixture
def tiny_sheets() -> dict[str, list[list[str]]]:
    """最小合法资料的副本，测试可以随意修改"""
    return {name: [list(row) for row in rows] for name, rows in TINY_MART.items()}


@pytest.fixture
def tiny_mart(tmp_path: Path, tiny_sheets: dict[str, list[list[str]]]) -> Path:
    """写在临时目录里的最小合法资料"""
    return write_mart(tmp_path / "mart", tiny_sheets)


@pytest.fixture
def tiny_store(tiny_mart: Path) -> MetadataStore:
    """最小合法资料的只读入口，不写元数据库"""
    mart, _ = load_mart(tiny_mart)
    return MetadataStore.from_mart(mart)


@pytest.fixture(scope="session")
def mock_store() -> MetadataStore:
    """模拟专题库补全之后（解析加工脚本、合并血缘、推导关联、收样例）的只读入口，不算向量"""
    mart, _ = load_mart(MOCK_MART_DIR)
    mart, _, _ = enrich_mart(mart, MOCK_MART_DIR)
    return MetadataStore.from_mart(mart)


@pytest.fixture(scope="session")
def mock_graph(mock_store: MetadataStore):
    """模拟专题库的血缘图与关联图"""
    from lineage.graph import LineageGraph

    return LineageGraph(mock_store)


@pytest.fixture(scope="session")
def mock_retriever(mock_store: MetadataStore, mock_graph):
    """模拟专题库的候选召回。mock_store 没有向量，不传向量器，只走关键字路"""
    from nl2sql.retriever import Retriever

    return Retriever(mock_store, mock_graph, None)


@pytest.fixture(scope="session")
def mock_validator(mock_store: MetadataStore):
    """模拟专题库的静态校验器"""
    from nl2sql.validator import SqlValidator

    return SqlValidator(mock_store)


@pytest.fixture(scope="session")
def mock_db() -> Path:
    """完整的模拟专题库

    清单、加工脚本、造数程序都没变时直接复用已生成的库，否则重新生成（库文件在固态盘上十几秒，在机械盘上一分多钟）。
    """
    from data.mock_mart.build_mock import DEFAULT_DB_PATH, ensure_built

    ensure_built()
    return DEFAULT_DB_PATH
