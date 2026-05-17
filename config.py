"""
AgentFlow 配置管理
从 .env 文件读取，兼容 Hermes 已有的 DeepSeek / DashScope Key
"""
import os
from dotenv import load_dotenv

# 先加载项目自己的 .env，再加载 Hermes 的 .env（作为 fallback）
load_dotenv()  # 项目 .env
load_dotenv("D:/hermes-agent-home/.env", override=False)  # Hermes Key（不覆盖已有）


# ============================================
# LLM 配置（日常开发用 DeepSeek，便宜）
# ============================================
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "deepseek")  # deepseek / openai / dashscope

LLM_API_BASE = os.getenv("LLM_API_BASE", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")

LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

# Embedding 模型（向量化用 DeepSeek，兼容 OpenAI 格式）
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "deepseek")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")

# ============================================
# 数据库配置
# ============================================
DB_TYPE = os.getenv("DB_TYPE", "sqlite")
SQLITE_PATH = os.getenv("SQLITE_PATH", "./data/sample.db")

PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASSWORD = os.getenv("PG_PASSWORD", "")
PG_DATABASE = os.getenv("PG_DATABASE", "agentflow")

# ============================================
# 向量库配置
# ============================================
CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "./chroma_data")

# ============================================
# 服务配置
# ============================================
FASTAPI_HOST = os.getenv("FASTAPI_HOST", "0.0.0.0")
FASTAPI_PORT = int(os.getenv("FASTAPI_PORT", "8000"))
STREAMLIT_PORT = int(os.getenv("STREAMLIT_PORT", "8501"))

# 上传文件配置
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "./uploads")
MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "50"))


def get_llm_kwargs():
    """
    获取 LLM 初始化参数
    日常开发默认用 DeepSeek（便宜），可通过 .env 切换 OpenAI
    """
    provider = LLM_PROVIDER

    if provider == "deepseek":
        api_key = os.getenv("DEEPSEEK_API_KEY", LLM_API_KEY)
        base_url = LLM_API_BASE or "https://api.deepseek.com/v1"
        model = LLM_MODEL or "deepseek-chat"
    elif provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", LLM_API_KEY)
        base_url = LLM_API_BASE or ""
        model = LLM_MODEL or "gpt-3.5-turbo"
    elif provider == "dashscope":
        api_key = os.getenv("DASHSCOPE_API_KEY", LLM_API_KEY)
        base_url = LLM_API_BASE or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        model = LLM_MODEL or "qwen-plus"
    else:
        raise ValueError(f"不支持的 LLM Provider: {provider}")

    if not api_key:
        raise ValueError(
            f"未找到 {provider} 的 API Key。请在项目 .env 或 Hermes .env 中配置"
        )

    kwargs = {
        "model": model,
        "api_key": api_key,
        "temperature": 0.7,
    }
    if base_url:
        kwargs["base_url"] = base_url

    return kwargs


def get_embedding_kwargs():
    """
    获取 Embedding 模型初始化参数
    默认用 DashScope (Qwen text-embedding-v3)，便宜 + 中文效果好
    """
    provider = EMBEDDING_PROVIDER

    if provider == "dashscope":
        api_key = os.getenv("DASHSCOPE_API_KEY", LLM_API_KEY)
        base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        model = EMBEDDING_MODEL or "text-embedding-v3"
    elif provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", LLM_API_KEY)
        base_url = LLM_API_BASE or ""
        model = EMBEDDING_MODEL or "text-embedding-3-small"
    elif provider == "deepseek":
        api_key = os.getenv("DEEPSEEK_API_KEY", LLM_API_KEY)
        base_url = "https://api.deepseek.com/v1"
        model = EMBEDDING_MODEL or "text-embedding-3-small"
    else:
        raise ValueError(f"不支持的 Embedding Provider: {provider}")

    if not api_key:
        raise ValueError(
            f"未找到 {provider} 的 API Key。请在项目 .env 或 Hermes .env 中配置"
        )

    kwargs = {
        "model": model,
        "api_key": api_key,
    }
    if base_url:
        kwargs["base_url"] = base_url
    return kwargs


def get_db_uri():
    """获取数据库连接 URI"""
    if DB_TYPE == "sqlite":
        return f"sqlite:///{SQLITE_PATH}"
    elif DB_TYPE == "postgresql":
        return f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DATABASE}"
    raise ValueError(f"不支持的数据库类型: {DB_TYPE}")

