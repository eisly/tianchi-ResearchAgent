import logging
import os
from pathlib import Path
from typing import Dict, List
import httpx

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from src.config.loader import load_yaml_config
from src.config.agents import LLMType

logger = logging.getLogger(__name__)

# Cache for LLM instances
_llm_cache: dict[LLMType, BaseChatModel] = {}


def _get_config_file_path() -> str:
    """Get the path to the configuration file."""
    return str((Path(__file__).parent.parent.parent / "conf.yaml").resolve())


def get_llm_by_type(llm_type: LLMType) -> BaseChatModel:
    """
    Get ChatOpenAI instance by type. Returns cached instance if available.
    """
    if llm_type in _llm_cache:
        return _llm_cache[llm_type]

    # Load simplified config (assuming BASIC_MODEL key structure in conf.yaml)
    # You can customize this to read specific keys as needed
    conf = load_yaml_config(_get_config_file_path())

    # Map type to config key (default to BASIC_MODEL for all types if simplified)
    config_key_map = {
        "reasoning": "REASONING_MODEL",
        "basic": "BASIC_MODEL",
        "vision": "VISION_MODEL",
        "code": "CODE_MODEL",
    }
    config_key = config_key_map.get(llm_type, "BASIC_MODEL")
    llm_conf = conf.get(config_key, {})

    # Minimal config extraction - add more fields if needed (api_key, base_url, etc.)
    # Priority: Env Var > Config File
    model_name = "qwen3.5-plus"
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        
    temperature = llm_conf.get("temperature", 0.7)

    logger.info(f"Initializing ChatOpenAI for {llm_type} with model={model_name}")

    # Check if using DashScope or similar domestic service that shouldn't use proxy
    http_client = None
    http_async_client = None
    
    if base_url and ("dashscope" in base_url or "aliyuncs" in base_url):
        # Bypass proxy for DashScope to avoid local proxy issues (127.0.0.1:7890)
        logger.info("DashScope/Aliyun detected, bypassing system proxy for LLM connections.")
        # Use trust_env=False to bypass proxy (and other env settings)
        http_client = httpx.Client(trust_env=False)
        http_async_client = httpx.AsyncClient(trust_env=False)

    # Prepare model_kwargs
    model_kwargs = {}
    # if "qwen" in model_name.lower():
    #     model_kwargs["enable_thinking"] = True

    # 如果是 Qwen 模型且可用 ChatDashScope，优先使用原生集成
    # 这可以避免用户对 openai.BadRequestError 的误解，且原生接口可能更稳定
    
    llm = ChatOpenAI(
            model=model_name,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            http_client=http_client,
            http_async_client=http_async_client,
            model_kwargs=model_kwargs
        )

    _llm_cache[llm_type] = llm
    return llm


def get_llm_token_limit_by_type(llm_type: str) -> int:
    """
    Simplified token limit getter.
    """
    # Return a safe default or read from config if strictly necessary
    return 100000


def get_configured_llm_models() -> dict[str, list[str]]:
    """
    Simplified model list getter.
    """
    return {"basic": ["qwen3.5-plus"]}
