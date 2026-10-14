"""测试公共夹具"""
from pathlib import Path

import pytest

from tests.helpers import TINY_MART, write_mart


@pytest.fixture
def tiny_sheets() -> dict[str, list[list[str]]]:
    """最小合法资料的副本，测试可以随意修改"""
    return {name: [list(row) for row in rows] for name, rows in TINY_MART.items()}


@pytest.fixture
def tiny_mart(tmp_path: Path, tiny_sheets: dict[str, list[list[str]]]) -> Path:
    """写在临时目录里的最小合法资料"""
    return write_mart(tmp_path / "mart", tiny_sheets)


@pytest.fixture(scope="session")
def mock_db() -> Path:
    """完整的模拟专题库

    清单、加工脚本、造数程序都没变时直接复用已生成的库，否则重新生成（十几秒）。
    """
    from data.mock_mart.build_mock import DEFAULT_DB_PATH, ensure_built

    ensure_built()
    return DEFAULT_DB_PATH
