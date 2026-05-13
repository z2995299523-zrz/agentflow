"""
AgentFlow 配置管理
所有配置从 .env 文件读取，支持 OpenAI 及国内模型
"""
import os
from dotenv import load_dotenv

load_dotenv()

# LLM 配置
LLM_API_BASE = os.getenv("LLM_API_BASE", "")  # 空字符串 = 用 OpenAI 官方
LLM_API_KEY = os.getenv("LLM_API_KEY", "sk-your-key-here")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-3.5-turbo")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")

# 数据库配置
DB_TYPE = os.getenv("DB_TYPE", "sqlite")
SQLITE_PATH = os.getenv("SQLITE_PATH", "./data/sample.db")

PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASSWORD = os.getenv("PG_PASSWORD", "")
PG_DATABASE = os.getenv("PG_DATABASE", "agentflow")

# 向量库配置
CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "./chroma_data")

# 服务配置
FASTAPI_HOST = os.getenv("FASTAPI_HOST", "0.0.0.0")
FASTAPI_PORT = int(os.getenv("FASTAPI_PORT", "8000"))
STREAMLIT_PORT = int(os.getenv("STREAMLIT_PORT", "8501"))

# 上传文件配置
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "./uploads")
MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "50"))


def get_llm_kwargs():
    """获取LLM初始化参数，兼容OpenAI和国内模型"""
    kwargs = {
        "model": LLM_MODEL,
        "api_key": LLM_API_KEY,
        "temperature": 0.7,
    }
    if LLM_API_BASE:
        kwargs["base_url"] = LLM_API_BASE
    return kwargs


def get_embedding_kwargs():
    """获取嵌入模型初始化参数"""
    kwargs = {
        "model": EMBEDDING_MODEL,
        "api_key": LLM_API_KEY,
    }
    if LLM_API_BASE:
        kwargs["base_url"] = LLM_API_BASE
    return kwargs


def get_db_uri():
    """获取数据库连接URI"""
    if DB_TYPE == "sqlite":
        return f"sqlite:///{SQLITE_PATH}"
    elif DB_TYPE == "postgresql":
        return f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DATABASE}"
    raise ValueError(f"不支持的数据库类型: {DB_TYPE}")
