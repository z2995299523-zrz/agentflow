"""
AgentFlow 配置管理
从 .env 文件读取，兼容 Hermes 已有的 DeepSeek / DashScope Key
"""
import os

# ⚠️ 必须在所有其他 import 之前设置（huggingface_hub 读取此变量）
# huggingface.co 国内被墙，用镜像加速 BGE 模型下载
if not os.getenv("HF_ENDPOINT"):
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

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

# ============================================
# NL2SQL 专题库配置
# ============================================
# 专题库资料目录（四张清单、加工脚本、样例）。切换到真实资料只改这一项
MART_DIR = os.getenv("MART_DIR", "./data/mock_mart")
# 生成 SQL 的方言：postgres（GaussDB 与 PostgreSQL 共有的语法）
SQL_DIALECT = os.getenv("SQL_DIALECT", "postgres")
# 执行层：none 只生成不执行；duckdb 在模拟专题库上只读执行
EXECUTOR = os.getenv("EXECUTOR", "none")
# 查询流水线单次模型调用的超时，秒
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "120"))

# ============================================
# 服务配置
# ============================================
FASTAPI_HOST = os.getenv("FASTAPI_HOST", "0.0.0.0")
FASTAPI_PORT = int(os.getenv("FASTAPI_PORT", "8000"))


def get_llm_kwargs(temperature: float | None = None):
    """
    获取 LLM 初始化参数
    日常开发默认用 DeepSeek（便宜），可通过 .env 切换 OpenAI

    Args:
        temperature: 温度。None 时用 0.7；查询流水线传 0
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
        "temperature": 0.7 if temperature is None else temperature,
    }
    if base_url:
        kwargs["base_url"] = base_url

    return kwargs
