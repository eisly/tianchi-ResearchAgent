import json
import logging
import os
import asyncio
from functools import partial
from typing import Annotated, Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph import graph
from langgraph.errors import GraphRecursionError
from langgraph.types import Command, interrupt

from src.agents.agents import create_agent
from src.citations.extractor import extract_citations_from_messages, merge_citations
from src.config.agents import AGENT_LLM_MAP
from src.config.configuration import Configuration
from src.llms.llm import get_llm_by_type, get_llm_token_limit_by_type
from src.prompts.planner_model import Plan
from src.prompts.template import apply_prompt_template, get_system_prompt_template
# from src.tools import get_web_search_tool
# from src.tools.search import LoggedTavilySearch
from src.utils.context_manager import ContextManager, validate_message_content
from src.utils.json_utils import repair_json_output, sanitize_tool_response
# from ..config import SELECTED_SEARCH_ENGINE, SearchEngine
from .types import State
from .utils import (
    get_message_content,
    is_user_message,
)
from src.tools.search import  get_academic_search_tool, get_tongxiao_search_tools, get_wikipedia_tool, reset_search_counter, get_google_serper_tool
from src.tools.scraper import scrape_tool
from src.tools.wikidata import get_wikidata_tools

logger = logging.getLogger(__name__)

@tool
def handoff_to_planner(
        research_topic: Annotated[str, "The topic of the research task to be handed off."],
        locale: Annotated[str, "The user's detected language locale (e.g., en-US, zh-CN)."],
):
    """
    Handoff to planner agent to do plan.
    
    实现说明:
    这是一个"标记工具" (Marker Tool)。它本身不返回具体数据，而是作为一个信号，
    告诉 LLM (Basic Agent) 应该将控制权移交给 Planner 节点进行复杂任务规划。
    """
    # This tool is not returning anything: we're just using it
    # as a way for LLM to signal that it needs to hand off to planner agent
    return


@tool
def direct_response(
        message: Annotated[str, "The response message to send directly to user."],
        locale: Annotated[str, "The user's detected language locale (e.g., en-US, zh-CN)."],
):
    """
    Respond directly to user for greetings, small talk, or polite rejections. 
    Do NOT use this for research questions - use handoff_to_planner instead.
    
    实现说明:
    当用户的意图不需要复杂的搜索和规划（例如简单的问候、拒绝或闲聊）时，
    LLM 会调用此工具直接生成回复，避免启动繁重的研究流程。
    """
    return


def preserve_state_meta_fields(state: State) -> dict:
    """
    Extract meta/config fields that should be preserved across state transitions.

    实现说明:
    在 LangGraph 中，当使用 Command.update 更新状态时，如果不显式包含某些字段，
    它们可能会丢失或重置。此函数确保核心的元数据（语言环境、研究主题、资源列表）
    在节点跳转时得以保留。

    Args:
        state: Current state object

    Returns:
        Dict of meta fields to preserve
    """
    return {
        "locale": state.get("locale", "en-US"),
        "research_topic": state.get("research_topic", ""),
        "resources": state.get("resources", []),
    }


def validate_and_fix_plan(plan: dict, enforce_web_search: bool = False, enable_web_search: bool = True) -> dict:
    """
    Validate and fix a plan to ensure it meets requirements.

    实现步骤:
    1. **修复缺失的 step_type**: 
       遍历所有步骤，如果缺少 `step_type` 字段，根据 `need_search` 的值进行推断。
       (need_search=True -> "research", 否则 -> "analysis")
    
    2. **强制 Web Search**:
       如果启用了 `enforce_web_search` 且 `enable_web_search` 为真，
       检查计划中是否包含至少一个 `need_search=True` 的步骤。
       如果没有，强制将第一个步骤转换为研究步骤，或者如果没有步骤，添加一个默认的研究步骤。
       这防止模型生成完全基于幻觉的计划。

    Args:
        plan: The plan dict to validate
        enforce_web_search: If True, ensure at least one step has need_search=true
        enable_web_search: If False, skip web search enforcement (takes precedence)

    Returns:
        The validated/fixed plan dict
    """
    if not isinstance(plan, dict):
        return plan

    steps = plan.get("steps", [])

    # ============================================================
    # SECTION 1: Repair missing step_type fields (Issue #650 fix)
    # ============================================================
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            continue

        # Check if step_type is missing or empty
        if "step_type" not in step or not step.get("step_type"):
            # Infer step_type based on need_search value
            # Default to "analysis" for non-search steps (Issue #677: not all processing needs code)
            inferred_type = "research" if step.get("need_search", False) else "analysis"
            step["step_type"] = inferred_type
            logger.info(
                f"Repaired missing step_type for step {idx} ({step.get('title', 'Untitled')}): "
                f"inferred as '{inferred_type}' based on need_search={step.get('need_search', False)}"
            )

    # ============================================================
    # SECTION 2: Enforce web search requirements
    # Skip enforcement if web search is disabled (enable_web_search=False takes precedence)
    # ============================================================
    if enforce_web_search and enable_web_search:
        # Check if any step has need_search=true (only check dict steps)
        has_search_step = any(
            step.get("need_search", False)
            for step in steps
            if isinstance(step, dict)
        )

        if not has_search_step and steps:
            # Ensure first research step has web search enabled
            for idx, step in enumerate(steps):
                if isinstance(step, dict) and step.get("step_type") == "research":
                    step["need_search"] = True
                    logger.info(f"Enforced web search on research step at index {idx}")
                    break
            else:
                # Fallback: If no research step exists, convert the first step to a research step with web search enabled.
                # This ensures that at least one step will perform a web search as required.
                if isinstance(steps[0], dict):
                    steps[0]["step_type"] = "research"
                    steps[0]["need_search"] = True
                    logger.info(
                        "Converted first step to research with web search enforcement"
                    )
        elif not has_search_step and not steps:
            # Add a default research step if no steps exist
            logger.warning("Plan has no steps. Adding default research step.")
            plan["steps"] = [
                {
                    "need_search": True,
                    "title": "Initial Research",
                    "description": "Gather information about the topic",
                    "step_type": "research",
                }
            ]

    return plan


async def planner_node(
        state: State, config: RunnableConfig
) -> Command[Literal["research_team"]]:
    """
    规划器节点 (Planner Node)

    负责生成或更新研究计划。
    
    功能:
    1. 根据用户的输入或之前的反馈生成详细的研究步骤。
    2. 检查计划是否包含必要的网络搜索步骤。
    3. 决定是否已经收集了足够的信息 (has_enough_context)。
    4. 如果计划完成或迭代次数超限，结束流程。
    
    Args:
        state (State): 当前图的状态，包含对话历史、当前计划等。
        config (RunnableConfig): 运行时配置。

    Returns:
        Command: 包含状态更新和下一步路由指令 (goto "chatbot" 或 "research_team" 或 "__end__")。
    """
    logger.info("Planner generating full plan with locale: %s", state.get("locale", "en-US"))
    logger.info("[planner]: Started execution")
    logger.debug("planner is working.....")
    configurable = Configuration.from_runnable_config(config)
    plan_iterations = state["plan_iterations"] if state.get("plan_iterations", 0) else 0

    # Normal mode: use full conversation history
    # 正常模式：加载系统提示词并使用完整的对话历史作为上下文
    logger.debug("planner is working.....")
    messages = apply_prompt_template("planner", state, configurable, state.get("locale", "en-US"))

    if configurable.enable_deep_thinking:
        llm = get_llm_by_type("reasoning")
    elif AGENT_LLM_MAP["planner"] == "basic":
        llm = get_llm_by_type("basic")
    else:
        llm = get_llm_by_type(AGENT_LLM_MAP["planner"])

    # Log the model being used by the planner
    try:
        model_name = getattr(llm, "model_name", getattr(llm, "model", "Unknown"))
        logger.info(f"[planner] Using LLM model: {model_name}")
    except Exception as e:
        logger.warning(f"[planner] Could not determine LLM model name: {e}")

    # if the plan iterations is greater than the max plan iterations, return the reporter node
    # 检查计划迭代次数是否超过最大限制，如果超过则直接结束，防止死循环
    # 强制限制最大迭代次数为3次，防止无限循环
    max_iterations = min(configurable.max_plan_iterations, 3)
    if plan_iterations >= max_iterations:
        logger.warning(f"Planner reached max iterations ({max_iterations}). Forcing termination.")
        logger.info("[planner]: Finished execution")
        return Command(
            update=preserve_state_meta_fields(state),
            goto="chatbot"
        )
    full_response = ""
    if AGENT_LLM_MAP["planner"] == "basic" and not configurable.enable_deep_thinking:
        max_retries = 3
        retry_delay = 2
        for attempt in range(max_retries):
            try:
                response = await llm.ainvoke(messages)
                break
            except Exception as e:
                # Check for Aliyun content safety error or other API errors
                error_str = str(e)
                if "data_inspection_failed" in error_str or "inappropriate content" in error_str:
                    logger.warning(f"Aliyun content safety triggered on attempt {attempt + 1}: {e}")
                    # If it's a content safety error, retrying exactly same request might not help, 
                    # but sometimes it's flaky. If it persists, we might need to modify the prompt slightly 
                    # or just fail gracefully. For now, we retry.
                else:
                    logger.warning(f"LLM invocation failed on attempt {attempt + 1}: {e}")
                
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay * (attempt + 1))
                else:
                    # If all retries fail, re-raise the exception or handle gracefully
                    # Here we re-raise to let the graph handle it (or crash if unhandled)
                    # Ideally, we could return a fallback/error state
                    logger.error(f"All {max_retries} attempts failed for planner LLM.")
                    raise e
                    
        if hasattr(response, "model_dump_json"):
            full_response = response.model_dump_json(indent=4, exclude_none=True)
        else:
            full_response = get_message_content(response) or ""
    else:
        async for chunk in llm.astream(messages):
            full_response += chunk.content
    logger.debug(f"Current state messages: {state['messages']}")
    logger.debug(f"Planner response: {full_response}")

    # Clean the response first to handle markdown code blocks (```json, ```ts, etc.)
    # 清洗 LLM 的响应，去除 Markdown 代码块标记（如 ```json），提取纯 JSON 字符串
    cleaned_response = repair_json_output(full_response)

    # Validate explicitly that response content is valid JSON before proceeding to parse it
    # 在解析之前显式验证清洗后的响应是否是合法的 JSON 格式（以 { 或 [ 开头）
    if not cleaned_response.strip().startswith('{') and not cleaned_response.strip().startswith('['):
        logger.warning("Planner response does not appear to be valid JSON after cleanup")
        if plan_iterations > 0:
            return Command(
                update=preserve_state_meta_fields(state),
                goto="__end__"
            )
        else:
            return Command(
                update=preserve_state_meta_fields(state),
                goto="__end__"
            )

    try:
        curr_plan = json.loads(cleaned_response)
        # Need to extract the plan from the full_response
        # 处理可能的嵌套结构，提取真正的计划内容
        curr_plan_content = extract_plan_content(curr_plan)
        # load the current_plan
        # 再次解析提取出的内容，确保得到最终的字典对象
        curr_plan = json.loads(repair_json_output(curr_plan_content))
    except json.JSONDecodeError:
        logger.warning("Planner response is not a valid JSON")
        if plan_iterations > 0:
            return Command(
                update=preserve_state_meta_fields(state),
                goto="__end__"
            )
        else:
            return Command(
                update=preserve_state_meta_fields(state),
                goto="__end__"
            )

    # Validate and fix plan to ensure web search requirements are met
    # 验证并修复计划：确保包含了必要的 step_type 字段，并根据配置强制添加 Web Search 步骤
    if isinstance(curr_plan, dict):
        curr_plan = validate_and_fix_plan(curr_plan, configurable.enforce_web_search, configurable.enable_web_search)

    if isinstance(curr_plan, dict) and curr_plan.get("has_enough_context"):
        logger.info("Planner response has enough context.")
        logger.info("[planner]: Finished execution")
        new_plan = Plan.model_validate(curr_plan)
        # 如果上下文足够，直接跳转到 chatbot 生成最终回答
        return Command(
            update={
                **preserve_state_meta_fields(state),
                "messages": [AIMessage(content=full_response, name="planner")],
                "current_plan": new_plan,
                "locale": new_plan.locale,
            },
            goto="chatbot",
        )

    # Check for consistent conclusions in recent planner outputs to detect loops
    # 检查最近的 planner 输出是否包含一致的结论，如果有，提前终止
    # 这可以防止 Agent 在已经找到答案的情况下仍然继续研究
    try:
        # 简单的启发式检查：如果 current_plan.thought 包含 "answer is" 或类似的确定性陈述
        # 并且之前的计划也包含类似的陈述，可能已经陷入循环
        current_thought = curr_plan.get("thought", "").lower()
        if "answer is" in current_thought or "conclusion is" in current_thought or "identified as" in current_thought or "answer to the question is" in current_thought:
            logger.info("Planner seems to have found an answer. Checking for repetition.")
            # 这种情况下，如果已经是第1次或更多次迭代，我们可以更加激进地终止
            if plan_iterations >= 1:
                logger.info("Early termination: Planner has likely found the answer based on thought content.")
                # Force has_enough_context to true
                curr_plan["has_enough_context"] = True
                new_plan = Plan.model_validate(curr_plan)
                return Command(
                    update={
                        **preserve_state_meta_fields(state),
                        "messages": [AIMessage(content=full_response, name="planner")],
                        "current_plan": new_plan,
                        "locale": new_plan.locale,
                    },
                    goto="chatbot",
                )
    except Exception as e:
        logger.warning(f"Error checking for early termination: {e}")

    # Check if we have valid steps
    # 检查计划中是否包含有效的研究步骤
    if isinstance(curr_plan, dict) and curr_plan.get("steps"):
        new_plan = Plan.model_validate(curr_plan)

        # Increment plan iterations
        # 增加计划迭代计数器
        plan_iterations += 1

        # 跳转到 research_team 开始执行研究步骤
        logger.info("[planner]: Finished execution")
        return Command(
            update={
                **preserve_state_meta_fields(state),
                "messages": [AIMessage(content=full_response, name="planner")],
                "current_plan": new_plan,
                "locale": new_plan.locale,
                "plan_iterations": plan_iterations,
            },
            goto="research_team",
        )

    return Command(
        update={
            "messages": [AIMessage(content=full_response, name="planner")],
            "current_plan": full_response,
            **preserve_state_meta_fields(state),
        },
        goto="__end__",  # Fallback to END if plan is invalid
    )


def extract_plan_content(plan_data: str | dict | Any) -> str:
    """
    Safely extract plan content from different types of plan data.

    实现步骤:
    1. **字符串处理**: 如果输入已经是字符串，直接返回。
    2. **对象处理**: 如果输入有 `content` 属性且是字符串 (如 AIMessage)，返回该内容。
    3. **字典处理**:
       - 如果字典包含 `content` 字段:
         - 如果 `content` 是字符串，直接返回。
         - 如果 `content` 是字典，将其转换为 JSON 字符串。
         - 如果 `content` 是列表 (多模态格式):
           - 遍历列表，寻找第一个有效的文本块 (type='text' 或纯字符串)。
           - 找到后返回该文本块。
           - 如果找不到有效文本，抛出 ValueError。
       - 如果字典不含 `content` 字段，假设整个字典就是计划数据，将其转换为 JSON 字符串。
    4. **其他类型**: 尝试转换为字符串并返回。

    Args:
        plan_data: The plan data which can be a string, AIMessage, or dict

    Returns:
        str: The plan content as a string (JSON string for dict inputs, or
    extracted/original string for other types)
    """
    if isinstance(plan_data, str):
        # If it's already a string, return as is
        return plan_data
    elif hasattr(plan_data, 'content') and isinstance(plan_data.content, str):
        # If it's an AIMessage or similar object with a content attribute
        logger.debug(f"Extracting plan content from message object of type {type(plan_data).__name__}")
        return plan_data.content
    elif isinstance(plan_data, dict):
        # If it's already a dictionary, convert to JSON string
        # Need to check if it's dict with content field (AIMessage-like)
        if "content" in plan_data:
            if isinstance(plan_data["content"], str):
                logger.debug("Extracting plan content from dict with content field")
                return plan_data["content"]
            if isinstance(plan_data["content"], dict):
                logger.debug("Converting content field dict to JSON string")
                return json.dumps(plan_data["content"], ensure_ascii=False)
            if isinstance(plan_data["content"], list):
                # Handle multimodal message format where content is a list
                # Extract text content from the list structure
                logger.debug(
                    f"Extracting plan content from multimodal list format with {len(plan_data['content'])} elements")
                for item in plan_data["content"]:
                    if isinstance(item, str) and item.strip():
                        # Return the first valid text content found
                        # We only take the first one because plan content should be a single JSON object
                        # Joining multiple text parts with newlines would produce invalid JSON
                        return item
                    elif isinstance(item, dict):
                        # Handle content block format like {"type": "text", "text": "..."}
                        if item.get("type") == "text" and "text" in item:
                            return item["text"]
                        elif "content" in item and isinstance(item["content"], str):
                            return item["content"]
                # No valid text content found - raise ValueError to trigger error handling
                # Do NOT use json.dumps() here as it would produce a JSON array that causes
                # Plan.model_validate() to fail with ValidationError (issue #845)
                raise ValueError(f"No valid text content found in multimodal list: {plan_data['content']}")
            else:
                logger.warning(
                    f"Unexpected type for 'content' field in plan_data dict: {type(plan_data['content']).__name__}, converting to string")
                return str(plan_data["content"])
        else:
            logger.debug("Converting plan dictionary to JSON string")
            return json.dumps(plan_data)
    else:
        # For any other type, try to convert to string
        logger.warning(f"Unexpected plan data type {type(plan_data).__name__}, attempting to convert to string")
        return str(plan_data)


def reporter_node(state: State, config: RunnableConfig):
    """
    报告生成器节点 (Reporter Node)

    负责根据收集到的信息撰写最终的研究报告。

    功能:
    1. 汇总所有研究步骤的执行结果。
    2. 整理引用的来源 (citations)。
    3. 根据指定的语言环境 (locale) 生成结构化的 Markdown 报告。
    
    Args:
        state (State): 当前图的状态，包含研究计划、执行结果、引用列表等。
        config (RunnableConfig): 运行时配置。

    Returns:
        dict: 更新的状态字典，包含生成的答案 (answer) 和引用信息。
    """
    logger.info("[reporter]: Started execution")
    logger.debug("reporter is working.....")
    configurable = Configuration.from_runnable_config(config)
    current_plan = state.get("current_plan")

    # Basic input for reporter
    # 准备报告生成器的基本输入：包含任务标题和描述的用户消息，以及语言环境设置
    input_ = {
        "messages": [
            HumanMessage(
                f"# Research Requirements\n\n## Task\n\n{current_plan.title}\n\n## Description\n\n{current_plan.thought}"
            )
        ],
        "locale": state.get("locale", "en-US"),
    }

    invoke_messages = apply_prompt_template("reporter", input_, configurable, input_.get("locale", "en-US"))

    # Get collected citations for the report
    # 获取整个研究过程中收集到的所有引用信息
    citations = state.get("citations", [])

    # If we have collected citations, provide them to the reporter
    # 如果有引用信息，将其格式化为 Markdown 列表，并作为 System Message 注入到上下文中
    # 这允许模型在生成报告时引用具体的来源
    if citations:
        citation_list = "\n\n## Available Source References (use these in References section):\n\n"
        for i, citation in enumerate(citations, 1):
            title = citation.get("title", "Untitled")
            url = citation.get("url", "")
            domain = citation.get("domain", "")
            description = citation.get("description", "")
            desc_truncated = description[:150] if description else ""
            citation_list += f"{i}. **{title}**\n   - URL: {url}\n   - Domain: {domain}\n"
            if desc_truncated:
                citation_list += f"   - Summary: {desc_truncated}...\n"
            citation_list += "\n"

        logger.info(f"Providing {len(citations)} collected citations to reporter")

        invoke_messages.append(
            HumanMessage(
                content=citation_list,
                name="system",
            )
        )

    # Use execution results from plan steps as context
    # 将每个研究步骤的执行结果（Research Findings）整理并注入到上下文中
    # 这是报告生成器撰写报告的核心素材来源
    if current_plan and current_plan.steps:
        results_context = "\n\n## Research Findings\n"
        for step in current_plan.steps:
            if step.execution_res:
                results_context += f"\n### Step: {step.title}\n{step.execution_res}\n"

        invoke_messages.append(
            HumanMessage(
                content=results_context,
                name="system"
            )
        )

    # Context compression
    # 上下文压缩：如果上下文过长，使用 ContextManager 进行压缩，以适应 LLM 的 Token 限制
    llm_token_limit = get_llm_token_limit_by_type(AGENT_LLM_MAP["reporter"])
    # We only compress if we have added a lot of context
    if len(invoke_messages) > 10 or any(len(m.content) > 1000 for m in invoke_messages):
        compressed_state = ContextManager(llm_token_limit).compress_messages(
            {"messages": invoke_messages}
        )
        invoke_messages = compressed_state.get("messages", [])

    logger.debug(f"Current invoke messages: {invoke_messages}")
    response = get_llm_by_type(AGENT_LLM_MAP["reporter"]).invoke(invoke_messages)
    response_content = response.content
    logger.debug(f"reporter response: {response_content}")
    logger.info("[reporter]: Finished execution")

    return {
        "answer": response_content,  # Write to answer field
        "citations": citations,
    }


def research_team_node(state: State):
    """
    研究团队协作节点 (Research Team Node)

    作为研究团队的入口和协调点。
    
    功能:
    1. 目前作为一个直通节点 (Pass-through)，用于逻辑上的分组。
    2. 后续通过条件边 (conditional_edges) 路由到具体的执行者 (如 researcher)。
    
    Args:
        state (State): 当前图的状态。
    """
    logger.info("[research_team]: Started execution")
    logger.debug("Entering research_team_node - coordinating research and coder agents")
    logger.info("[research_team]: Finished execution")
    pass


async def _handle_recursion_limit_fallback(
        messages: list,
        agent_name: str,
        current_step,
        state: State,
) -> list:
    """
    Handle GraphRecursionError with graceful fallback using LLM summary.
    
    实现步骤:
    1. **清理消息历史**:
       - 移除末尾悬挂的 Tool Calls (没有对应 Tool Message 的调用)，防止上下文不完整。
       - 移除末尾的 System Message。
    2. **准备提示词**:
       - 加载 fallback 专用的 prompt template。
       - 设置语言环境。
    3. **调用 LLM**:
       - 使用不带工具的 LLM 模型 (fallback_llm)。
       - 要求 LLM 根据现有对话历史生成一个总结或结论。
    4. **更新结果**:
       - 将生成的总结作为当前步骤的执行结果。
       - 将总结消息追加到消息历史中并返回。
       
    这确保了即使达到递归上限，Agent 也能给出一个"尽力而为"的回答，而不是直接报错。
    """
    logger.warning(
        f"Recursion limit reached for {agent_name} agent. "
        f"Attempting graceful fallback with {len(messages)} accumulated messages."
    )

    if len(messages) == 0:
        return messages

    cleared_messages = messages.copy()
    
    # Remove dangling AIMessage with tool_calls from the end
    while len(cleared_messages) > 0:
        last_msg = cleared_messages[-1]
        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            logger.warning(f"Removing dangling AIMessage with tool_calls during fallback: {last_msg.id}")
            cleared_messages.pop()
        else:
            break
            
    while len(cleared_messages) > 0 and cleared_messages[-1].type == "system":
        cleared_messages = cleared_messages[:-1]

    # Prepare state for prompt template
    fallback_state = {
        "locale": state.get("locale", "en-US"),
    }

    # Apply the recursion_fallback prompt template
    system_prompt = get_system_prompt_template(agent_name, fallback_state, None, fallback_state.get("locale", "en-US"))
    limit_prompt = get_system_prompt_template("recursion_fallback", fallback_state, None,
                                              fallback_state.get("locale", "en-US"))
    fallback_messages = cleared_messages + [
        SystemMessage(content=system_prompt),
        SystemMessage(content=limit_prompt)
    ]

    # Get the LLM without tools (strip all tools from binding)
    fallback_llm = get_llm_by_type(AGENT_LLM_MAP[agent_name])

    # Call the LLM with the updated messages
    fallback_response = fallback_llm.invoke(fallback_messages)
    fallback_content = fallback_response.content

    logger.info(
        f"Graceful fallback succeeded for {agent_name} agent. "
        f"Generated summary of {len(fallback_content)} characters."
    )

    # Sanitize response
    fallback_content = sanitize_tool_response(str(fallback_content))

    # Update the step with the fallback result
    current_step.execution_res = fallback_content

    # Return the accumulated messages plus the fallback response
    result_messages = list(cleared_messages)
    result_messages.append(AIMessage(content=fallback_content, name=agent_name))

    return result_messages


async def _execute_agent_step(
        state: State, agent, agent_name: str, config: RunnableConfig = None, system_messages: list = None
) -> Command[Literal["research_team"]]:
    """
    Helper function to execute a step using the specified agent.
    
    实现步骤:
    1. **重置搜索计数器**: 
       - 确保每个 Agent 步骤开始时，搜索工具的使用次数计数器归零。
       - 限制每个步骤最多使用 5 次搜索工具。
    
    2. **确定当前任务**:
       - 从 `current_plan` 中查找第一个未执行的步骤 (`execution_res` 为空)。
       - 如果所有步骤都已完成，返回到 `research_team` 节点（随后会被路由到 chatbot）。
    
    3. **构建上下文**:
       - 收集已完成步骤的执行结果，格式化为 `completed_steps_info`。
       - 获取当前的语言环境 `locale`。
    
    4. **构建 Agent 输入**:
       - 创建包含任务描述、当前步骤详情、语言要求的 HumanMessage。
       - 注入系统提示词 (system_messages)。
       - 如果是 Researcher，注入资源文件信息和引用格式要求。
    
    5. **预处理与优化**:
       - 验证消息内容格式。
       - 根据 LLM 的 Token 限制进行上下文压缩 (Context Compression)。
    
    6. **执行 Agent 循环**:
       - 调用 `agent.astream` 开始流式执行。
       - 处理 `GraphRecursionError`: 如果达到递归限制，尝试使用 fallback 机制生成总结。
       - 处理 `openai.BadRequestError`: 捕获内容风控错误 (400)，返回友好的错误提示。
       - 处理其他异常: 记录错误并将其作为步骤结果。
    
    7. **结果处理**:
       - 提取 Agent 的最终回答。
       - 提取引用信息 (Citations)。
       - 更新当前步骤的 `execution_res`。
       - 更新全局状态 (State)，包括消息历史和引用列表。
    
    Returns:
        Command: 包含状态更新和下一步路由指令。
    """
    # Reset search call counter at the start of each agent step
    reset_search_counter()
    
    logger.debug(f"[_execute_agent_step] Starting execution for agent: {agent_name}")
    logger.info(f"[{agent_name}]: Started execution")

    current_plan = state.get("current_plan")
    plan_title = current_plan.title
    # observations removed from state
    logger.debug(f"[_execute_agent_step] Plan title: {plan_title}")

    # Find the first unexecuted step
    # 查找第一个尚未执行的步骤 (execution_res 为空)
    # 同时收集所有已完成的步骤
    current_step = None
    completed_steps = []
    for idx, step in enumerate(current_plan.steps):
        if not step.execution_res:
            current_step = step
            logger.debug(f"[_execute_agent_step] Found unexecuted step at index {idx}: {step.title}")
            break
        else:
            completed_steps.append(step)

    if not current_step:
        logger.warning(f"[_execute_agent_step] No unexecuted step found in {len(current_plan.steps)} total steps")
        return Command(
            update=preserve_state_meta_fields(state),
            goto="research_team"
        )

    logger.info(f"[_execute_agent_step] Executing step: {current_step.title}, agent: {agent_name}")
    logger.debug(f"[_execute_agent_step] Completed steps so far: {len(completed_steps)}")

    # Format completed steps information
    # 格式化已完成步骤的信息，作为上下文提供给当前 Agent
    # 这确保了 Agent 知道之前的研究成果，避免重复工作
    completed_steps_info = ""
    if completed_steps:
        completed_steps_info = "# Completed Research Steps\n\n"
        for i, step in enumerate(completed_steps):
            completed_steps_info += f"## Completed Step {i + 1}: {step.title}\n\n"
            completed_steps_info += f"<finding>\n{step.execution_res}\n</finding>\n\n"

    # Prepare the input for the agent with completed steps info
    # 准备 Agent 的输入：包含完整的任务背景、已完成步骤的信息、当前步骤详情以及严格的语言要求
    locale = state.get('locale', 'en-US')
    
    # Check if we already have the answer in completed steps (optimization for redundant research)
    # 检查已完成的步骤是否已经包含了足够回答当前步骤的信息
    # 如果当前步骤只是对已知信息的交叉验证或总结，可以提示 Agent 优先使用已知信息
    optimization_prompt = ""
    if completed_steps:
        optimization_prompt = (
            "\n\nIMPORTANT: Review the 'Completed Research Steps' above carefully. "
            "If the information required for the 'Current Step' has already been gathered in previous steps, "
            "DO NOT perform new searches. Instead, synthesize the existing information to answer the current step immediately. "
            "Only use tools if you are missing specific critical details."
        )

    agent_input = {
        "messages": [
            HumanMessage(
                content=(
                    f"# RESEARCH CONTEXT\n\n## Overall Goal\n{plan_title}\n\n"
                    f"{completed_steps_info}"
                    f"# CURRENT TASK (FOCUS HERE)\n\n"
                    f"You are executing **Step {current_plan.steps.index(current_step) + 1}** of the plan.\n\n"
                    f"## Step Title\n{current_step.title}\n\n"
                    f"## Step Instructions\n{current_step.description}\n\n"
                    f"**MANDATORY INSTRUCTION**: Focus ONLY on executing this specific step. "
                    f"DO NOT re-do research from previous steps. DO NOT jump ahead to future steps. "
                    f"Your output must address the requirements of THIS step only.\n\n"
                    f"## Locale\n{locale}\n\n"
                    f"IMPORTANT: Your response MUST be written entirely in the language specified in the 'Locale' section above (which is {locale}). This applies to all sections of your response, including headers, content, and conclusions.{optimization_prompt}"
                )
            )
        ]
    }

    # Inject system messages if provided (Manual prompt injection)
    # 注入手动提供的系统提示词 (通常来自 Prompt Template)
    if system_messages:
        agent_input["messages"] = system_messages + agent_input["messages"]

    # Add citation reminder for researcher agent
    # 为研究员 Agent 添加特定的提示词：
    # 1. 如果有本地资源文件，强制要求使用 local_search_tool
    # 2. 强制要求使用 Markdown 链接格式引用来源，而不是行内引用
    if agent_name == "researcher":
        if state.get("resources"):
            resources_info = "**The user mentioned the following resource files:**\n\n"
            for resource in state.get("resources"):
                resources_info += f"- {resource.title} ({resource.description})\n"

            agent_input["messages"].append(
                HumanMessage(
                    content=resources_info
                            + "\n\n"
                            + "You MUST use the **local_search_tool** to retrieve the information from the resource files.",
                )
            )

        agent_input["messages"].append(
            HumanMessage(
                content="IMPORTANT: DO NOT include inline citations in the text. Instead, track all sources and include a References section at the end using link reference format. Include an empty line between each citation for better readability. Use this format for each reference:\n- [Source Title](URL)\n\n- [Another Source](URL)",
                name="system",
            )
        )

    # Invoke the agent
    # 设置 Agent 的递归深度限制，防止无限循环调用工具
    default_recursion_limit = 25
    try:
        env_value_str = os.getenv("AGENT_RECURSION_LIMIT", str(default_recursion_limit))
        parsed_limit = int(env_value_str)

        if parsed_limit > 0:
            recursion_limit = parsed_limit
            logger.info(f"Recursion limit set to: {recursion_limit}")
        else:
            recursion_limit = default_recursion_limit
    except ValueError:
        recursion_limit = default_recursion_limit

    # Log the model being used by the agent
    model_name = "Unknown"
    try:
        # Check for LangChain agent structures
        if hasattr(agent, "runnable") and hasattr(agent.runnable, "steps"):
             # For some LangChain agent structures
             for step in agent.runnable.steps:
                 if hasattr(step, "model_name"):
                     model_name = step.model_name
                     break
                 elif hasattr(step, "bound") and hasattr(step.bound, "model_name"):
                     model_name = step.bound.model_name
                     break
        # Common LangChain AgentExecutor structure
        elif hasattr(agent, "agent") and hasattr(agent.agent, "llm_chain") and hasattr(agent.agent.llm_chain, "llm"):
            model_name = agent.agent.llm_chain.llm.model_name
        # Newer LangGraph prebuilt agent structure
        elif hasattr(agent, "model") and hasattr(agent.model, "model_name"):
             model_name = agent.model.model_name
        elif hasattr(agent, "bound") and hasattr(agent.bound, "model_name"):
             model_name = agent.bound.model_name
        # Fallback: check if agent itself has model_name or model attribute
        elif hasattr(agent, "model_name"):
            model_name = agent.model_name
        
        logger.info(f"[{agent_name}] Using LLM model: {model_name}")
    except Exception as e:
        logger.warning(f"[{agent_name}] Could not determine LLM model name: {e}")

    logger.info(f"Agent input: {agent_input}")

    # Validate message content before invoking agent
    # 在调用 Agent 之前验证消息内容格式，防止无效的输入导致错误
    try:
        validated_messages = validate_message_content(agent_input["messages"])
        agent_input["messages"] = validated_messages
    except Exception as validation_error:
        logger.error(f"Error validating agent input messages: {validation_error}")

    # Apply context compression
    # 应用上下文压缩：如果消息历史过长，压缩中间部分以节省 Token 并避免超出模型限制
    llm_token_limit = get_llm_token_limit_by_type(AGENT_LLM_MAP[agent_name])
    if llm_token_limit:
        compressed_state = ContextManager(llm_token_limit, preserve_prefix_message_count=3).compress_messages(
            {"messages": agent_input["messages"]}
        )
        agent_input["messages"] = compressed_state.get("messages", [])

    max_retries = 2
    retry_count = 0
    accumulated_messages = []
    
    while retry_count <= max_retries:
        try:
            # 执行 Agent 循环 (Stream 模式)
            async for chunk in agent.astream(
                    input=agent_input,
                    config={"recursion_limit": recursion_limit},
                    stream_mode="values",
            ):
                if isinstance(chunk, dict) and "messages" in chunk:
                    accumulated_messages = chunk["messages"]
    
            result = {"messages": accumulated_messages}
            break # Success, exit retry loop
            
        except GraphRecursionError:
            # 处理递归限制错误：尝试使用 LLM 生成总结作为 fallback
            configurable = Configuration.from_runnable_config(config) if config else Configuration()
    
            if configurable.enable_recursion_fallback:
                try:
                    response_messages = await _handle_recursion_limit_fallback(
                        messages=accumulated_messages,
                        agent_name=agent_name,
                        current_step=current_step,
                        state=state,
                    )
                    result = {"messages": response_messages}
                    break # Fallback success, exit loop
                except Exception as fallback_error:
                    logger.error(f"Recursion fallback failed: {fallback_error}")
                    raise
            else:
                raise
        except Exception as e:
            # 处理其他异常：特别是内容风控 (400) 错误
            import traceback
            error_traceback = traceback.format_exc()
            logger.exception(f"Error executing {agent_name}: {e}")
    
            error_msg = str(e)
            if "400" in error_msg and ("inappropriate content" in error_msg or "data_inspection_failed" in error_msg):
                retry_count += 1
                if retry_count <= max_retries:
                    logger.warning(f"[{agent_name}] Content filter hit (attempt {retry_count}/{max_retries}). Retrying with neutral terms...")
                    # Prepare warning message
                    warning_msg = HumanMessage(
                        content="The previous response was blocked by content filters. Please try a different approach: rephrase your search or response using more neutral, academic, or indirect terms. Focus on factual and historical aspects without sensitive details."
                    )
                    # If accumulated_messages is not empty, use it as base, else use agent_input["messages"]
                    if accumulated_messages:
                        # Remove last AI message if it caused the error (though it might not be in the list if blocked)
                        agent_input["messages"] = accumulated_messages + [warning_msg]
                    else:
                        agent_input["messages"].append(warning_msg)
                    continue # Retry the loop
                else:
                    detailed_error = (
                        f"[ERROR] {agent_name.capitalize()} Agent Error (Content Safety Check)\n\n"
                        f"Step: {current_step.title}\n\n"
                        "The model refused to process the request due to sensitive content detected in the search results or query. "
                        "This is a safety mechanism of the AI provider. Please try rephrasing your query or skipping this step."
                    )
            else:
                detailed_error = f"[ERROR] {agent_name.capitalize()} Agent Error\n\nStep: {current_step.title}\n\nError Details:\n{error_msg}"
            
            current_step.execution_res = detailed_error
    
            return Command(
                update={
                    "messages": [
                        HumanMessage(
                            content=detailed_error,
                            name=agent_name,
                        )
                    ],
                    **preserve_state_meta_fields(state),
                },
                goto="research_team",
            )

    response_messages = result["messages"]
    response_content = response_messages[-1].content
    response_content = sanitize_tool_response(str(response_content))


    # Update the step with the execution result
    # 更新当前步骤的执行结果
    current_step.execution_res = response_content
    logger.info(f"Step '{current_step.title}' execution completed by {agent_name}")
    logger.info(f"[{agent_name}]: Finished execution")

    agent_messages = result.get("messages", [])

    # Extract citations
    # 从消息中提取引用信息，并合并到全局引用列表中
    existing_citations = state.get("citations", [])
    new_citations = extract_citations_from_messages(agent_messages)
    merged_citations = merge_citations(existing_citations, new_citations)

    return Command(
        update={
            **preserve_state_meta_fields(state),
            "messages": agent_messages,
            # "observations": observations + [response_content + validation_info], # Removed observations
            "citations": merged_citations,
        },
        goto="research_team",
    )


async def _setup_and_execute_agent_step(
        state: State,
        config: RunnableConfig,
        agent_type: str,
        default_tools: list,
) -> Command[Literal["research_team"]]:
    """
    Helper function to set up an agent with appropriate tools and execute a step.
    
    实现步骤:
    1. **工具配置**:
       - 加载默认工具。
       - 检查配置中的 MCP (Model Context Protocol) 设置，加载并配置外部 MCP 工具。
    
    2. **提示词准备**:
       - 根据 `agent_type` 和 `locale` 加载对应的系统提示词模板 (apply_prompt_template)。
    
    3. **创建 Agent**:
       - 使用 `create_agent` 工厂函数实例化 Agent 对象，绑定工具和 LLM。
    
    4. **执行**:
       - 调用 `_execute_agent_step` 运行 Agent。
    """
    configurable = Configuration.from_runnable_config(config)
    mcp_servers = {}
    enabled_tools = {}
    loaded_tools = default_tools[:]

    locale = state.get("locale", "en-US")

    # if configurable.mcp_settings:
    #     for server_name, server_config in configurable.mcp_settings["servers"].items():
    #         if (
    #                 server_config["enabled_tools"]
    #                 and agent_type in server_config["add_to_agents"]
    #         ):
    #             mcp_servers[server_name] = {
    #                 k: v
    #                 for k, v in server_config.items()
    #                 if k in ("transport", "command", "args", "url", "env", "headers")
    #             }
    #             for tool_name in server_config["enabled_tools"]:
    #                 enabled_tools[tool_name] = server_name

    # if mcp_servers:
    #     client = MultiServerMCPClient(mcp_servers)
    #     all_tools = await client.get_tools()
    #     for tool in all_tools:
    #         if tool.name in enabled_tools:
    #             tool.description = (
    #                 f"Powered by '{enabled_tools[tool.name]}'.\n{tool.description}"
    #             )
    #             loaded_tools.append(tool)

    # Render prompt template manually
    prompt_template = agent_type  # Template name matches agent type
    system_messages = apply_prompt_template(
        prompt_template, state, configurable, locale=locale
    )

    agent = create_agent(
        agent_type,
        agent_type,
        loaded_tools,
    )
    return await _execute_agent_step(state, agent, agent_type, config, system_messages=system_messages)


async def researcher_node(
        state: State, config: RunnableConfig
) -> Command[Literal["research_team"]]:
    """
    研究员节点 (Researcher Node)

    负责执行具体的研究步骤。
    
    功能:
    1. 接收当前的具体研究步骤 (Step)。
    2. 使用网络搜索工具 (如 DuckDuckGo) 或本地 RAG 工具获取信息。
    3. 整理搜索结果并更新步骤的执行结果 (execution_res)。
    4. 处理工具调用的异常和风控限制。
    
    Args:
        state (State): 当前图的状态。
        config (RunnableConfig): 运行时配置，包含搜索设置等。

    Returns:
        Command: 包含状态更新和下一步路由指令 (通常返回 "research_team" 以继续下一个步骤)。
    """
    logger.debug("Researcher node is researching.")

    configurable = Configuration.from_runnable_config(config)
    tools = []

    if configurable.enable_web_search:

        # academic_tool = await get_academic_search_tool(configurable.max_search_results)
        # if academic_tool:
        #     tools.append(academic_tool)

        # wikipedia_tool = await get_wikipedia_tool()
        # if wikipedia_tool:
        #     tools.append(wikipedia_tool)

        google_serper_tool = await get_google_serper_tool(configurable.max_search_results)
        if google_serper_tool:
            tools.append(google_serper_tool)
        
        tongxiao_tools = await get_tongxiao_search_tools()
        if tongxiao_tools:
            tools.extend(tongxiao_tools)        

        tools.append(scrape_tool)
    else:
        logger.info("[researcher_node] Web search is disabled, using only local RAG")

    return await _setup_and_execute_agent_step(
        state,
        config,
        "researcher",
        tools,
    )
