"""
AgentFlow API — FastAPI 统一接口
提供 RAG 知识库问答 + Text-to-SQL 数据分析的 REST API
"""
import os
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException
from pydantic import BaseModel, Field

from config import UPLOAD_DIR, CHROMA_PERSIST_DIR
from rag.loader import load_single_file, split_documents
from rag.vectorstore import create_vectorstore, load_vectorstore, get_document_count
from rag.chain import ask, ask_with_detail
from sql.agent import SQLQueryAgent
from sql.visualizer import QueryVisualizer


# ─── 应用初始化 ──────────────────────────────────

app = FastAPI(
    title="AgentFlow API",
    description="企业级 AI 智能助手 — RAG + Text-to-SQL",
    version="2.0.0",
)

# 确保上传目录存在
Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)

# 懒加载单例
_sql_agent: Optional[SQLQueryAgent] = None


def get_sql_agent() -> SQLQueryAgent:
    """获取 SQL Agent 单例（延迟初始化，避免启动时连接数据库）"""
    global _sql_agent
    if _sql_agent is None:
        _sql_agent = SQLQueryAgent()
    return _sql_agent


# ─── 请求/响应模型 ───────────────────────────────

class QueryRequest(BaseModel):
    question: str = Field(..., description="自然语言问题", min_length=1)


class RAGQueryResponse(BaseModel):
    answer: str
    sources: list[str]
    context_details: Optional[list[dict]] = None


class SQLQueryResponse(BaseModel):
    question: str
    sql: str
    answer: str
    success: bool
    error: Optional[str] = None


class VisualizeResponse(BaseModel):
    question: str
    sql: str
    answer: str
    chart_base64: str
    chart_type: str
    title: str
    success: bool
    error: Optional[str] = None


class DocumentInfo(BaseModel):
    filename: str
    chunks: int


# ─── RAG 路由 ────────────────────────────────────

@app.post("/rag/upload")
async def upload_document(file: UploadFile = File(...)):
    """上传文档到知识库（支持 PDF/Word/TXT/Markdown）"""
    # 校验文件类型
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".pdf", ".docx", ".txt", ".md"):
        raise HTTPException(400, f"不支持的文件类型: {ext}，支持 PDF/Word/TXT/Markdown")

    # 保存文件
    safe_name = f"{uuid.uuid4().hex[:8]}_{file.filename}"
    filepath = Path(UPLOAD_DIR) / safe_name
    content = await file.read()
    filepath.write_bytes(content)

    try:
        # 加载文档
        docs = load_single_file(str(filepath))
        if not docs:
            os.remove(filepath)
            raise HTTPException(400, "文档内容为空")

        # 分块
        chunks = split_documents(docs)

        # 创建/更新向量库（追加模式）
        create_vectorstore(chunks, persist_dir=CHROMA_PERSIST_DIR)

        return {
            "success": True,
            "filename": file.filename,
            "chunks": len(chunks),
            "message": f"已上传 {file.filename}，分割为 {len(chunks)} 个文本块"
        }
    except HTTPException:
        raise
    except Exception as e:
        if filepath.exists():
            os.remove(filepath)
        raise HTTPException(500, f"文档处理失败: {str(e)}")


@app.post("/rag/query", response_model=RAGQueryResponse)
async def query_rag(request: QueryRequest):
    """知识库问答（带来源引用）"""
    try:
        result = ask_with_detail(request.question)
        if not result.get("answer"):
            raise HTTPException(404, "未找到相关内容，请先上传文档")
        return RAGQueryResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"查询失败: {str(e)}")


@app.get("/rag/documents", response_model=list[DocumentInfo])
async def list_documents():
    """列出知识库中的文档及其文本块数量"""
    try:
        # 扫描上传目录
        upload_dir = Path(UPLOAD_DIR)
        files = []
        if upload_dir.exists():
            for f in upload_dir.iterdir():
                if f.is_file():
                    # 去掉随机前缀还原原始文件名
                    display_name = f.name.split("_", 1)[-1] if "_" in f.name else f.name
                    files.append({"filename": display_name, "chunks": 0})

        # 追加向量库统计
        try:
            count = get_document_count()
        except Exception:
            count = 0

        return [
            DocumentInfo(filename=f["filename"], chunks=count // max(len(files), 1))
            for f in files
        ] if files else []
    except Exception as e:
        raise HTTPException(500, f"获取文档列表失败: {str(e)}")


@app.delete("/rag/documents/{filename}")
async def delete_document(filename: str):
    """删除指定文档（需重建向量库以完全清除）"""
    try:
        filepath = Path(UPLOAD_DIR) / filename
        if filepath.exists():
            os.remove(filepath)
            return {"success": True, "message": f"已删除 {filename}"}
        raise HTTPException(404, f"文件不存在: {filename}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"删除失败: {str(e)}")


# ─── SQL 路由 ────────────────────────────────────

@app.post("/sql/query", response_model=SQLQueryResponse)
async def query_sql(request: QueryRequest):
    """自然语言查询数据库（Text-to-SQL）"""
    try:
        agent = get_sql_agent()
        result = agent.query(request.question)
        if not result["success"]:
            raise HTTPException(400, result.get("error", "查询失败"))
        return SQLQueryResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"查询失败: {str(e)}")


@app.post("/sql/visualize", response_model=VisualizeResponse)
async def visualize_sql(request: QueryRequest):
    """查询数据库并自动生成图表（Base64 PNG）"""
    try:
        agent = get_sql_agent()

        # 第一步：Text-to-SQL
        result = agent.query(request.question)
        if not result["success"]:
            raise HTTPException(400, result.get("error", "查询失败"))
        if not result["sql"]:
            raise HTTPException(400, "未能生成 SQL 查询语句")

        # 第二步：执行 SQL 获取数据
        import pandas as pd
        from config import get_db_uri
        from sql.agent import SafeSQLDatabase

        db = SafeSQLDatabase.from_uri(get_db_uri())
        df = pd.read_sql_query(result["sql"], db._engine)

        if df.empty:
            raise HTTPException(404, "查询结果为空，无法生成图表")

        # 第三步：自动选择图表类型并生成
        chart = QueryVisualizer.visualize(df)
        if not chart["success"]:
            raise HTTPException(500, chart.get("error", "图表生成失败"))

        return VisualizeResponse(
            question=request.question,
            sql=result["sql"],
            answer=result["answer"],
            chart_base64=chart["image_base64"],
            chart_type=chart["chart_type"],
            title=chart["title"],
            success=True,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"可视化失败: {str(e)}")


@app.get("/sql/schema")
async def get_schema():
    """获取数据库表结构信息"""
    try:
        agent = get_sql_agent()
        schema = agent.get_schema()
        return {"success": True, "schema": schema}
    except Exception as e:
        raise HTTPException(500, f"获取表结构失败: {str(e)}")


# ─── 健康检查 ────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "AgentFlow API"}
