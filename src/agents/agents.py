import logging
from typing import List
from langchain.agents import create_agent as langchain_create_agent
from src.config.agents import AGENT_LLM_MAP
from src.llms.llm import get_llm_by_type

logger = logging.getLogger(__name__)


# Create agents using configured LLM types
def create_agent(
        agent_name: str,
        agent_type: str,
        tools: list,
):
    """Factory function to create agents with consistent configuration.

    Args:
        agent_name: Name of the agent
        agent_type: Type of agent (researcher, coder, etc.)
        tools: List of tools available to the agent

    Returns:
        A configured agent graph
    """
    logger.debug(f"Creating agent '{agent_name}' of type '{agent_type}' with {len(tools)} tools")

    if agent_type not in AGENT_LLM_MAP:
        logger.warning(
            f"Agent type '{agent_type}' not found in AGENT_LLM_MAP. "
            f"Falling back to default LLM type 'basic' for agent '{agent_name}'."
        )
    llm_type = AGENT_LLM_MAP.get(agent_type, "basic")
    llm = get_llm_by_type(llm_type)
    logger.debug(f"Agent '{agent_name}' using LLM type: {llm_type}")
    
    try:
        model_name = getattr(llm, "model_name", "Unknown")
        logger.info(f"Creating agent '{agent_name}' with LLM model: {model_name}")
    except Exception:
        pass

    # Simplified creation without middleware
    agent = langchain_create_agent(
        name=agent_name,
        model=llm,
        tools=tools,
        middleware=[],
    )
    
    # Attach model name to the agent object for logging purposes
    try:
        if hasattr(llm, "model_name"):
            agent.model_name = llm.model_name
        elif hasattr(llm, "model"):
            agent.model_name = llm.model
    except Exception:
        pass

    logger.info(f"Agent '{agent_name}' created successfully")

    return agent
