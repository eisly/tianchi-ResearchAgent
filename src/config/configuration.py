import logging
import os
from dataclasses import dataclass, field, fields
from typing import Any, Optional

from langchain_core.runnables import RunnableConfig

from src.config.loader import get_bool_env, get_int_env, get_str_env

logger = logging.getLogger(__name__)


def get_recursion_limit(default: int = 25) -> int:
    """Get the recursion limit from environment variable or use default."""
    env_value_str = get_str_env("AGENT_RECURSION_LIMIT", str(default))
    parsed_limit = get_int_env("AGENT_RECURSION_LIMIT", default)

    if parsed_limit > 0:
        return parsed_limit
    return default


@dataclass(kw_only=True)
class Configuration:
    """The configurable fields."""

    resources: list = field(default_factory=list)  # Resources (simplified to list)
    max_plan_iterations: int = 1  # Maximum number of plan iterations (Increased to support re-planning)
    max_step_num: int = 3  # Maximum number of steps in a plan
    max_search_results: int = 10  # Maximum number of search results (Increased from 3 to improve recall)
    mcp_settings: dict = None  # MCP settings, including dynamic loaded tools
    report_style: str = "academic"  # Report style (simplified to string)
    enable_deep_thinking: bool = False  # Whether to enable deep thinking
    enforce_web_search: bool = False  # Enforce at least one web search step
    enforce_researcher_search: bool = True  # Enforce researcher must use web search
    enable_web_search: bool = True  # Whether to enable web search
    enable_recursion_fallback: bool = True  # Enable graceful fallback

    @classmethod
    def from_runnable_config(
        cls, config: Optional[RunnableConfig] = None
    ) -> "Configuration":
        """Create a Configuration instance from a RunnableConfig."""
        configurable = (
            config["configurable"] if config and "configurable" in config else {}
        )
        values: dict[str, Any] = {
            f.name: os.environ.get(f.name.upper(), configurable.get(f.name))
            for f in fields(cls)
            if f.init
        }
        return cls(**{k: v for k, v in values.items() if v is not None})
