"""
AgentFlow 配置管理
从 .env 文件读取，兼容 Hermes 已有的 DeepSeek / DashScope Key
"""
import os
from dataclasses import dataclass

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
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "deepseek")  # deepseek / openai / dashscope / mimo / zhipu

LLM_API_BASE = os.getenv("LLM_API_BASE", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")

LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-flash")

# 服务商：(Key 的环境变量, 默认地址, 默认模型)
PROVIDERS: dict[str, tuple[str, str, str]] = {
    "deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1", "deepseek-flash"),
    "openai": ("OPENAI_API_KEY", "", "gpt-3.5-turbo"),
    "dashscope": ("DASHSCOPE_API_KEY", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "mimo": ("MIMO_API_KEY", "https://api.xiaomimimo.com/v1", "mimo-v2.6-flash"),     # 小米 MiMo
    "zhipu": ("ZHIPUAI_API_KEY", "https://open.bigmodel.cn/api/paas/v4", "glm-4.7-flash"),   # 智谱
}


@dataclass(frozen=True)
class ModelChoice:
    """界面上可以选的一个模型"""

    name: str                                 # 模型名，查数请求里带的就是它
    label: str                                # 界面上显示的名字
    provider: str                             # PROVIDERS 之一


# 查数时可以按请求换的模型；不指定时用默认模型（LLM_PROVIDER、LLM_MODEL）。没配 Key 的不列，见 available_models
MODEL_CHOICES: tuple[ModelChoice, ...] = (
    ModelChoice("deepseek-flash", "DeepSeek Flash", "deepseek"),
    ModelChoice("qwen3.8-flash", "千问 qwen3.8-flash", "dashscope"),
    ModelChoice("mimo-v2.6-flash", "小米 mimo-v2.6-flash", "mimo"),
    ModelChoice("glm-4.7-flash", "智谱 GLM-4.7-Flash", "zhipu"),
    ModelChoice("gpt-6.1-terra", "GPT-6.1 Terra", "openai"),          # 服务器连不上 OpenAI，只有本机走代理能用
)

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


def default_model() -> str:
    """不指定模型时用的模型名：LLM_MODEL，没写时用服务商的默认模型"""
    return LLM_MODEL or PROVIDERS.get(LLM_PROVIDER, ("", "", ""))[2]


def model_choice(name: str) -> ModelChoice:
    """按模型名取 MODEL_CHOICES 里的一项

    Raises:
        ValueError: 不认识的模型
    """
    for choice in MODEL_CHOICES:
        if choice.name == name:
            return choice
    raise ValueError(f"不认识的模型：{name}，可选：{'、'.join(choice.name for choice in MODEL_CHOICES)}")


def _api_key(provider: str) -> str:
    """服务商的 Key；LLM_API_KEY 只给 LLM_PROVIDER 这个服务商兜底。服务商不认识时为空串"""
    if provider not in PROVIDERS:
        return ""
    return os.getenv(PROVIDERS[provider][0], LLM_API_KEY if provider == LLM_PROVIDER else "")


def _thinking_off(provider: str, model: str) -> dict | None:
    """关掉思考模式的请求参数：DeepSeek、小米 MiMo、智谱用 thinking，千问（DashScope）用 enable_thinking；
    OpenAI 的 GPT-5、GPT-6 系列用 reasoning_effort=none（关了推理温度才能设成 0），更早的模型不用"""
    if provider in ("deepseek", "mimo", "zhipu"):
        return {"thinking": {"type": "disabled"}}
    if provider == "dashscope":
        return {"enable_thinking": False}
    if provider == "openai" and model.startswith(("gpt-5", "gpt-6")):
        return {"reasoning_effort": "none"}
    return None


def available_models() -> list[ModelChoice]:
    """界面上能选的模型：默认模型排第一，其余按 MODEL_CHOICES 的顺序；没配 Key 的不列"""
    default = default_model()
    labels = {choice.name: choice.label for choice in MODEL_CHOICES}
    first = ModelChoice(default, labels.get(default, default), LLM_PROVIDER)
    rest = [choice for choice in MODEL_CHOICES if choice.name != default]
    return [choice for choice in (first, *rest) if _api_key(choice.provider)]


def get_llm_kwargs(temperature: float | None = None, model: str | None = None):
    """
    获取 LLM 初始化参数
    日常开发默认用 DeepSeek（便宜），可通过 .env 切换 OpenAI；model 指定 MODEL_CHOICES 里的别的模型时，
    按它的服务商取 Key 和地址。LLM_API_KEY、LLM_API_BASE 只给 LLM_PROVIDER 这个服务商用。
    思考模式显式关掉：DeepSeek、小米 MiMo 默认打开，开着时温度不生效、思考内容按输出计费、回复也慢；千问（DashScope）、智谱、
    GPT-5 以后的 OpenAI 模型同样关掉

    Args:
        temperature: 温度。None 时用 0.7；查询流水线传 0
        model: 模型名。None 或默认模型时用 LLM_PROVIDER、LLM_MODEL

    Raises:
        ValueError: 服务商不支持、模型不认识，或者没配 Key
    """
    if model is None or model == default_model():
        provider, name = LLM_PROVIDER, default_model()
    else:
        choice = model_choice(model)
        provider, name = choice.provider, choice.name
    if provider not in PROVIDERS:
        raise ValueError(f"不支持的 LLM Provider: {provider}")

    api_key = _api_key(provider)
    if not api_key:
        raise ValueError(
            f"未找到 {provider} 的 API Key。请在项目 .env 或 Hermes .env 中配置"
        )
    base_url = (LLM_API_BASE if provider == LLM_PROVIDER else "") or PROVIDERS[provider][1]
    extra_body = _thinking_off(provider, name)

    kwargs = {
        "model": name,
        "api_key": api_key,
        "temperature": 0.7 if temperature is None else temperature,
    }
    if base_url:
        kwargs["base_url"] = base_url
    if extra_body:
        kwargs["extra_body"] = extra_body

    return kwargs
