"""集成测试：RAG + SQL 端到端"""
import pytest
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)


def test_health():
    """健康检查"""
    r = client.get("/health")
    assert r.status_code == 200


def test_rag_query():
    """RAG 查询端点"""
    r = client.post("/rag/query", json={"question": "年假有多少天？"})
    assert r.status_code in (200, 503)  # 200=成功, 503=向量库空


def test_sql_query():
    """SQL 查询端点"""
    r = client.post("/sql/query", json={"question": "产品表有多少条记录？"})
    assert r.status_code == 200
    data = r.json()
    assert "answer" in data


def test_sql_schema():
    """数据库 Schema 端点"""
    r = client.get("/sql/schema")
    assert r.status_code == 200


def test_rag_documents():
    """文档列表端点"""
    r = client.get("/rag/documents")
    assert r.status_code in (200, 503)
