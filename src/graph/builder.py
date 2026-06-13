
from langgraph.checkpoint.memory import MemorySaver,InMemorySaver
from langgraph.graph import END, START, StateGraph

from src.prompts.planner_model import StepType

from .nodes import (
    planner_node,
    research_team_node,
    researcher_node,
)
from .types import State
from src.llms.llm import get_llm_by_type, get_llm_token_limit_by_type
import os
import logging
from langchain_core.messages import SystemMessage, HumanMessage
from src.utils.context_manager import ContextManager
from src.config.agents import AGENT_LLM_MAP

logger = logging.getLogger(__name__)

def chatbot(state: State):
    logger.info("[chatbot]: Started execution")
    locale = state.get("locale", "en-US")
    research_topic = state.get("research_topic", "Unknown Topic")
    
    # Prepend system message to enforce language and strictly limit output to the final answer only
    if locale == "zh-CN":
        system_content = f"""你是我的智能助手。请用{locale}回答。
任务：基于研究主题和上下文，提取最终答案。

**研究主题：{research_topic}**

上下文说明：
- 上下文包含按时间顺序排列的多个研究步骤的结果。
- **最高优先级原则（必须严格遵守）**：
  1. **[FINAL STEP]** 的信息是最终结论，必须无条件采纳。
  2. **聚焦目标**：请重点阅读 [FINAL STEP] 中的 `### 结论` 或 `### Conclusion` 章节。
  3. **最后出现原则**：如果 `### 结论` 中包含加粗（**...**）或代码块（`...`）标记的实体，请**直接提取该实体**作为最终答案。
  4. **忽略冲突**：如果 `### 结论` 中的答案与前面的分析（如Step 1, Step 2）或你自己的知识有冲突，**必须无条件信任加粗的实体**。
  5. **不要推理**：不要尝试修正或解释，直接复制粘贴那个加粗的词。

输出格式：
- 仅输出最终答案（单个名词、名称、数字或短语）。
- 不要输出列表。
- 不要包含问题或推理过程。
- 不要包含标点符号（除非是名称的一部分）。

示例：
主题：寻找突发事件
上下文：
[FINAL STEP]: 
...
### 结论
这起事件是**aaa事件**。
...
你的输出：aaa事件
"""
    else:
        system_content = f"""You are a helpful assistant. Please respond in {locale}.
TASK: Extract the final answer based on the Research Topic and Context.

**RESEARCH TOPIC: {research_topic}**

CONTEXT INSTRUCTIONS:
- The context contains results from multiple research steps listed in chronological order.
- **HIGHEST PRIORITY RULES**:
  1. Information in **[FINAL STEP]** is the final conclusion and MUST be adopted unconditionally.
  2. **TARGET FOCUS**: Pay special attention to the `### Conclusion` or `### 结论` section in [FINAL STEP].
  3. **LAST OCCURRENCE RULE**: If the `### Conclusion` section contains an entity marked in **Bold** or `Code Block`, **EXTRACT IT DIRECTLY** as the final answer.
  4. **IGNORE CONFLICTS**: If the answer in `### Conclusion` conflicts with previous steps or your own knowledge, **YOU MUST UNCONDITIONALLY TRUST THE BOLDED ENTITY**.
  5. **NO REASONING**: Do not attempt to correct or explain. Just copy-paste the bolded word.

OUTPUT FORMAT:
- Output ONLY the final answer (a single noun, name, number, or short phrase).
- Do NOT output a list.
- Do NOT include the question or the reasoning.
- Do NOT include punctuation like periods or newline characters unless part of the name.

Example:
Topic: Find the incident
Context:
[FINAL STEP]: 
...
### Conclusion
The incident is the **September 13th Incident**.
...
Your Output: September 13th Incident
"""
    # Create a fresh message list to avoid sending potentially problematic history or accumulating system messages
    # We only take the last HumanMessage from the state to ensure we are answering the current question
    # This minimizes the context window and reduces the chance of triggering content filters with old history
    
    # Filter for the last human message
    last_human_message = None
    for msg in reversed(state["messages"]):
        if isinstance(msg, HumanMessage):
            last_human_message = msg
            break
            
    # Include research results in the context
    context = ""
    current_plan = state.get("current_plan")
    if current_plan and hasattr(current_plan, "steps"):
        # Filter only executed steps
        executed_steps = [s for s in current_plan.steps if s.execution_res]
        total_steps = len(executed_steps)
        
        # Strategy: Prioritize the FINAL step.
        # If context is too long, we aggressively truncate earlier steps but keep the FINAL step intact.
        
        for i, step in enumerate(executed_steps):
            is_final = (i == total_steps - 1)
            step_label = "FINAL STEP" if is_final else f"Step {i+1}"
            
            res_content = step.execution_res
            
            # For non-final steps, truncate heavily if needed
            if not is_final:
                if len(res_content) > 2000:
                    res_content = res_content[:2000] + "\n...(intermediate content truncated)..."
            else:
                # For FINAL step, keep more content to ensure accuracy
                if len(res_content) > 10000:
                    res_content = res_content[:10000] + "\n...(content truncated)..."
            
            context += f"[{step_label}]: {step.title}\nResult: {res_content}\n\n"
    
    if context:
        logger.info(f"[chatbot]: Research context length: {len(context)} chars")
        # Ensure context is not too large before appending
        if len(context) > 20000:
             context = context[:20000] + "\n...(research context truncated)..."
        system_content += f"\n\nContext from research:\n{context}"

    if last_human_message:
        messages = [SystemMessage(content=system_content), last_human_message]
    else:
        # Fallback if no human message found (shouldn't happen in normal flow)
        messages = [SystemMessage(content=system_content)] + state["messages"][-1:]

    # Use ContextManager to compress messages to fit token limit
    # This prevents the "unstable response" issue caused by massive context
    llm_token_limit = get_llm_token_limit_by_type("basic")
    if not llm_token_limit:
        llm_token_limit = 100000 # Default fallback
    
    # Force lower limit for chatbot to improve speed (max 16k tokens)
    # 32k tokens is still too slow for final answer generation
    llm_token_limit = min(llm_token_limit, 16000)
        
    logger.info(f"[chatbot]: Compressing context with limit {llm_token_limit}")
    compressed_state = ContextManager(llm_token_limit).compress_messages(
        {"messages": messages}
    )
    messages = compressed_state.get("messages", [])
    logger.info(f"[chatbot]: Context compressed. Message count: {len(messages)}")

    logger.info("[chatbot]: Invoking LLM")
    # Use qwen-turbo for chatbot node to improve speed, as user requested
    # The chatbot node's task is simple: summarize the final answer from the research context.
    # qwen-turbo is faster and sufficient for this task, while qwen3.5-plus (basic) is used for research.
    from src.llms.llm import get_llm_by_type
    
    # We can explicitly request a "fast" model if defined, or just override here
    # Since "basic" maps to qwen3.5-plus usually, we want to bypass that map for this specific node
    try:
        # Use a lightweight approach to instantiate qwen-turbo directly
        from langchain_openai import ChatOpenAI
        import os
        import httpx
        
        # Reuse existing keys from environment variables
        api_key = os.getenv("BASIC_MODEL_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
        base_url = os.getenv("BASIC_MODEL_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        
        # Configure http client (bypass proxy for DashScope)
        http_client = None
        if base_url and ("dashscope" in base_url or "aliyuncs" in base_url):
            http_client = httpx.Client(trust_env=False)

        logger.info(f"[chatbot] Using qwen-turbo for faster response generation (Base URL: {base_url})")
        
        llm = ChatOpenAI(
            model="qwen-turbo",
            api_key=api_key,
            base_url=base_url,
            temperature=0.7,
            http_client=http_client,
        )
        logger.info(f"[chatbot] Using LLM model: {llm.model_name}")
        response = llm.invoke(messages)
        
    except Exception as e:
        logger.warning(f"[chatbot] Failed to use qwen-turbo, falling back to basic model: {e}")
        llm = get_llm_by_type("basic")
        logger.info(f"[chatbot] Using LLM model: {llm.model_name}")
        response = llm.invoke(messages)

    # Post-processing: Clean up the answer
    # If the model still outputs multiple lines despite the prompt, use smart extraction
    original_content = response.content
    content = original_content.strip()
    
    logger.info(f"[chatbot] Raw output: {repr(content)}")
    
    # Use splitlines() to handle various line endings (\n, \r, \r\n)
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    
    if len(lines) > 1:
        logger.info(f"[chatbot] Detected multiline output ({len(lines)} lines). Applying smart extraction.")
        
        # Strategy: Filter out obviously non-answer lines (URLs, References, Markdown links)
        # We want to keep the core answer text
        filtered_lines = [
            line for line in lines 
            if not (line.startswith("- [") or line.startswith("[") or "http://" in line or "https://" in line or line.startswith("Source:") or line.startswith("Reference:"))
        ]
        
        if filtered_lines:
            # Prefer the FIRST valid line (Answer First style) if available
            # But check if the last line looks like "Final Answer: X" (Reasoning First style)
            candidate = filtered_lines[0]
            
            # Check for explicit "Final Answer" marker in the last line
            if "Final Answer" in lines[-1]:
                candidate = lines[-1]
            
            # Clean up prefixes like "Answer:", "Final Answer:", "The answer is:"
            if ":" in candidate:
                # Heuristic: split only if prefix is short (e.g. "Answer:")
                parts = candidate.split(":", 1)
                if len(parts[0]) < 20: # "Final Answer" is 12 chars
                    candidate = parts[1].strip()
            
            content = candidate
        else:
            # Fallback: if everything looks like a link, just take the first line
            content = lines[0]
            
    # Final cleanup: Remove Markdown bolding and quotes if present
    # If the answer is wrapped in **...**, extract it.
    import re
    bold_match = re.search(r"\*\*(.*?)\*\*", content)
    if bold_match:
        content = bold_match.group(1).strip()
    
    # Remove leading/trailing quotes
    content = content.strip('"').strip("'")

    logger.info(f"[chatbot] Cleaned output: {repr(content)}")
    
    # Create a new message to ensure content is updated
    from langchain_core.messages import AIMessage
    response = AIMessage(content=content)

    logger.info("[chatbot]: Finished execution")
    return {"messages": [response]}

def continue_to_running_research_team(state: State):
    current_plan = state.get("current_plan")
    if not current_plan or not current_plan.steps:
        return "planner"

    if all(step.execution_res for step in current_plan.steps):
        return "chatbot"

    # Find first incomplete step
    incomplete_step = None
    for step in current_plan.steps:
        if not step.execution_res:
            incomplete_step = step
            break

    if not incomplete_step:
        return "chatbot"

    return "researcher"


def _build_base_graph():
    """Build and return the base state graph with all nodes and edges."""
    builder = StateGraph(State)
    builder.add_edge(START, "planner")
    builder.add_node("planner", planner_node)
    builder.add_node("research_team", research_team_node)
    builder.add_node("researcher", researcher_node)
    builder.add_node("chatbot", chatbot)
    builder.add_conditional_edges(
        "research_team",
        continue_to_running_research_team,
        ["planner", "researcher", "chatbot"],
    )
    builder.add_edge("chatbot", END)
    return builder


def build_graph_with_memory():
    """Build and return the agent workflow graph with memory."""
    # use persistent memory to save conversation history
    # TODO: be compatible with SQLite / PostgreSQL
    memory = MemorySaver()

    # build state graph
    builder = _build_base_graph()
    return builder.compile(checkpointer=memory)


def build_graph():
    """Build and return the agent workflow graph without memory."""
    # build state graph
    builder = _build_base_graph()
    return builder.compile()


graph = build_graph_with_memory()
