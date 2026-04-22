import os
import random
import uuid
import sys
import json
import logging
import re
from datetime import datetime
from typing import AsyncIterator, List


print(sys.path)
# 确保项目根目录在 sys.path 中
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)
    
# 配置日志目录
log_dir = os.path.join(current_dir, "chat_log")
os.makedirs(log_dir, exist_ok=True)

from agentscope_runtime.engine import AgentApp, Runner
from agentscope_runtime.engine.schemas.agent_schemas import AgentRequest
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from src.graph.builder import build_graph_with_memory
from src.llms.llm import get_llm_by_type

# ==========================================
# FastAPIAppFactory 补丁
# 允许自定义请求/响应格式
# ==========================================
from agentscope_runtime.engine.deployers.utils.service_utils.fastapi_factory import FastAPIAppFactory
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from agentscope_runtime.engine.schemas.agent_schemas import AgentRequest
import uuid

# 覆盖默认的路由添加逻辑，支持自定义接口格式
def patched_add_routes(
    app: FastAPI,
    endpoint_path: str,
    request_model,
    stream_enabled,
    mode,
):
    # 健康检查接口
    @app.get("/health")
    async def health_check():
        status = {"status": "healthy", "mode": mode.value}
        if hasattr(app.state, "runner") and app.state.runner:
            status["runner"] = "ready"
        else:
            status["runner"] = "not_ready"
        return status

    

    # 自定义 Agent API 接口
    @app.post(endpoint_path)
    async def agent_api(request: dict):
        question = request.get("question")
        if not question:
            return JSONResponse(status_code=400, content={"error": "Missing 'question' field"})

        # 构建参数
        msgs = [HumanMessage(content=question)]
        session_id = str(uuid.uuid4())
        
        # 使用虚拟 AgentRequest 以满足 query_func 签名
        dummy_request = AgentRequest(
            input=[], 
            session_id=session_id,
            user_id="user"
        )
        
        runner = app.state.runner
        if not runner:
             return JSONResponse(status_code=503, content={"error": "Runner not initialized"})

        # 重要：使用流式响应以防止平台超时
        # 平台可能对初始响应字节有较短的超时限制
        # 我们需要快速生成 *一些东西*，即使只是空格或部分数据
        
        async def event_generator():
            import asyncio
            q = asyncio.Queue()
            finished = False
            final_answer = ""
            
            # 立即发送一条初始 Ping
            yield "event: Ping\n\n"
            
            async def heartbeat():
                while not finished:
                    try:
                        await asyncio.sleep(2)
                        if not finished and q.empty():
                            await q.put({"type": "heartbeat"})
                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        pass

            async def worker():
                nonlocal final_answer
                try:
                    # runner.query_handler 是绑定的 query_func 方法
                    async for msg, is_last in runner.query_handler(msgs=msgs, request=dummy_request):
                        # 我们寻找包含答案的 AIMessage
                        if isinstance(msg, AIMessage):
                            msg_id = getattr(msg, "id", "")
                            
                            # 忽略保活信号
                            if msg_id == "keep_alive":
                                continue
                            
                            # 处理最终答案
                            if msg_id == "final_answer":
                                # 确保 final_answer 字段与 [ai] 日志一致，并清理格式
                                final_answer = clean_ai_response(msg.content)
                                continue

                            # 兼容旧逻辑：如果内容不为空且未被标记，可能是最终答案
                            if msg.content:
                                # 对于未标记的消息，同样应用清理逻辑，确保一致性
                                cleaned = clean_ai_response(msg.content)
                                final_answer = cleaned
                    
                    await q.put({"type": "result"})
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    await q.put({"type": "error", "error": str(e)})

            # 启动任务
            t_heartbeat = asyncio.create_task(heartbeat())
            t_worker = asyncio.create_task(worker())

            try:
                while True:
                    item = await q.get()
                    
                    if item["type"] == "heartbeat":
                        yield "event: Ping\n\n"
                    
                    elif item["type"] == "result":
                        finished = True
                        # Send the final answer
                        yield f"event: Message\ndata: {json.dumps({'answer': final_answer}, ensure_ascii=False)}\n\n"
                        break
                        
                    elif item["type"] == "error":
                        finished = True
                        error_msg = f"Error: {item['error']}"
                        yield f"event: Message\ndata: {json.dumps({'answer': error_msg}, ensure_ascii=False)}\n\n"
                        break
            finally:
                finished = True
                t_heartbeat.cancel()

        # 使用 text/event-stream 实现标准的 SSE 流式响应
        from fastapi.responses import StreamingResponse
        return StreamingResponse(event_generator(), media_type="text/event-stream")

    # 根路径接口
    @app.get("/")
    async def root():
        return {
            "service": "ResearchAgent",
            "mode": mode.value,
            "endpoints": {
                "process": endpoint_path,
                "health": "/health",
            },
        }

    # 特定模式的接口
    FastAPIAppFactory._add_process_control_endpoints(app)

FastAPIAppFactory._add_routes = patched_add_routes

import json
from functools import reduce
from typing import AsyncIterator, Tuple
from langchain_core.messages import (
    BaseMessage,
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from agentscope_runtime.engine.schemas.agent_schemas import (
    Message,
    TextContent,
    DataContent,
    FunctionCall,
    FunctionCallOutput,
    MessageType,
)
import agentscope_runtime.adapters.langgraph.stream as stream_module

# 适配 LangGraph 消息流
async def patched_adapt_langgraph_message_stream(
    source_stream: AsyncIterator[Tuple[BaseMessage, bool]],
) -> AsyncIterator[Message]:
    msg_id = None
    index = None
    
    message = None

    tool_started = False
    tool_call_chunk_msgs = []

    async for msg, last in source_stream:
        if isinstance(msg, HumanMessage):
            role = "user"
            content = msg.content if hasattr(msg, "content") else None
            
            if msg_id != getattr(msg, "id") or message is None:
                message = Message(type=MessageType.MESSAGE, role=role)
                msg_id = getattr(msg, "id")
                yield message.in_progress()
            
            if content:
                text_delta_content = TextContent(
                    delta=True,
                    index=None,
                    text=content,
                )
                
                if message is not None:
                    text_delta_content = message.add_delta_content(
                        new_content=text_delta_content,
                    )
                    yield text_delta_content
                    yield message.completed()
                else:
                    print(f"Warning: Message object is None for content: {content[:20]}...")
        elif isinstance(msg, AIMessage):
            role = "assistant"
            tool_calls = getattr(msg, "tool_calls", [])
            has_tool_call_chunk = (
                True if getattr(msg, "tool_call_chunks", "") else False
            )
            if tool_calls and not has_tool_call_chunk:
                plugin_call_message = Message(
                    type=MessageType.PLUGIN_CALL,
                    role=role,
                )
                for tool_call in tool_calls:
                    tool_call_args = (
                        tool_call.get("args")
                        if isinstance(tool_call.get("args"), str)
                        else json.dumps(tool_call.get("args"))
                    )
                    data_content = DataContent(
                        index=index,
                        data=FunctionCall(
                            call_id=tool_call.get("id"),
                            name=tool_call.get("name"),
                            arguments=tool_call_args,
                        ).model_dump(),
                    )
                    plugin_call_message.add_content(
                        data_content,
                    )
                    yield data_content.completed()
                yield plugin_call_message.completed()
            else:
                has_tool_call_chunk = (
                    True if getattr(msg, "tool_call_chunks", "") else False
                )
                is_last_chunk = (
                    True
                    if getattr(msg, "chunk_position", "") == "last"
                    else False
                )
                if tool_started:
                    if has_tool_call_chunk:
                        tool_call_chunk_msgs.append(msg)
                    if is_last_chunk:
                        tool_started = False
                        result = reduce(
                            lambda x, y: x + y,
                            tool_call_chunk_msgs,
                        )
                        tool_calls = result.tool_call_chunks
                        for tool_call in tool_calls:
                            call_id = tool_call.get("id", "")
                            plugin_call_message = Message(
                                type=MessageType.PLUGIN_CALL,
                                role=role,
                            )
                            tool_call_args = (
                                tool_call.get("args")
                                if isinstance(tool_call.get("args"), str)
                                else json.dumps(tool_call.get("args"))
                            )

                            data_content = DataContent(
                                index=index,
                                data=FunctionCall(
                                    call_id=call_id,
                                    name=tool_call.get("name"),
                                    arguments=tool_call_args,
                                ).model_dump(),
                            )

                            data_content = (
                                plugin_call_message.add_delta_content(
                                    new_content=data_content,
                                )
                            )
                            yield data_content.completed()
                            yield plugin_call_message.completed()
                else:
                    if has_tool_call_chunk:
                        tool_started = True
                        tool_call_chunk_msgs.append(msg)
                    else:
                        content = (
                            msg.content if hasattr(msg, "content") else None
                        )
                        if msg_id != getattr(msg, "id") or message is None:
                            index = None
                            message = Message(
                                type=MessageType.MESSAGE,
                                role=role,
                            )
                            msg_id = getattr(msg, "id")
                            yield message.in_progress()

                        if content:
                            text_delta_content = TextContent(
                                delta=True,
                                index=index,
                                text=content,
                            )
                            
                            if message is not None:
                                text_delta_content = message.add_delta_content(
                                    new_content=text_delta_content,
                                )
                                index = text_delta_content.index
                                yield text_delta_content
                            else:
                                print(f"Warning: Message object is None for AIMessage content")
                            
                        if last:
                            if message is not None:
                                yield message.completed()
        elif isinstance(msg, SystemMessage):
            role = "system"
            content = msg.content if hasattr(msg, "content") else None
            
            if msg_id != getattr(msg, "id") or message is None:
                message = Message(type=MessageType.MESSAGE, role=role)
                yield message.in_progress()
                msg_id = getattr(msg, "id")
            
            if content:
                text_delta_content = TextContent(
                    delta=True,
                    index=None,
                    text=content,
                )
                
                if message is not None:
                    text_delta_content = message.add_delta_content(
                        new_content=text_delta_content,
                    )
                    yield text_delta_content
        elif isinstance(msg, ToolMessage):
            role = "tool"
            content = msg.content if hasattr(msg, "content") else None
            
            if msg_id != getattr(msg, "id") or message is None:
                message = Message(type=MessageType.MESSAGE, role=role)
                yield message.in_progress()
                msg_id = getattr(msg, "id")
            
            plugin_output_message = Message(
                type=MessageType.PLUGIN_CALL_OUTPUT,
                role="tool",
            )
            tool_call_output = (
                msg.content
                if isinstance(msg.content, str)
                else json.dumps(msg.content, ensure_ascii=False)
            )
            function_output_data = FunctionCallOutput(
                call_id=msg.tool_call_id,
                name=msg.name,
                output=tool_call_output,
            )

            data_content = DataContent(
                data=function_output_data.model_dump(),
                msg_id=plugin_output_message.id,
            )
            yield data_content.completed()
            plugin_output_message.add_content(
                data_content,
            )
            yield plugin_output_message.completed()
        else:
            role = "assistant"
            content = msg.content if hasattr(msg, "content") else None
            if msg_id != getattr(msg, "id") or message is None:
                index = None
                message = Message(type=MessageType.MESSAGE, role=role)
                msg_id = getattr(msg, "id")
                yield message.in_progress()

            if content:
                text_delta_content = TextContent(
                    delta=True,
                    index=index,
                    text=content,
                )
                
                if message is not None:
                    text_delta_content = message.add_delta_content(
                        new_content=text_delta_content,
                    )
                    index = text_delta_content.index
                    yield text_delta_content
                else:
                    print(f"Warning: Message object is None for generic content")

            if last:
                if message is not None:
                    yield message.completed()

# Apply the patch
stream_module.adapt_langgraph_message_stream = patched_adapt_langgraph_message_stream
print("Monkey patch applied to agentscope_runtime.adapters.langgraph.stream")
# ==========================================

# 设置环境变量
# 强制禁用 LangSmith 以避免平台 403 错误
os.environ["LANGSMITH_OTEL_ENABLED"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGSMITH_OTEL_ONLY"] = "false"

short_term_memory: BaseCheckpointSaver = None
long_term_memory: BaseStore = None

# 创建 AgentApp 实例
agent_app = AgentApp(
    app_name="ResearchAgent",
    app_description="A LangGraph-based research assistant powered by ResearchAgent logic",
)

# 初始化 Agent
@agent_app.init
async def initialize(self):
    global short_term_memory
    global long_term_memory

    short_term_memory = MemorySaver()
    long_term_memory = InMemoryStore()

    self.graph = build_graph_with_memory()
    
    try:
        llm = get_llm_by_type("basic")
        print(f"Initialized ResearchAgent with LLM: {llm.model_name}")
    except Exception as e:
        print(f"Warning: Failed to initialize LLM: {e}")



# 格式化消息内容，美化 JSON 显示
def format_message_content(content):
    if isinstance(content, str):
        content = content.strip()
        if (content.startswith('{') and content.endswith('}')) or \
           (content.startswith('[') and content.endswith(']')):
            try:
                parsed = json.loads(content)
                return json.dumps(parsed, indent=2, ensure_ascii=False)
            except json.JSONDecodeError:
                pass
        
        if "```json" in content:
            try:
                start = content.find("```json") + 7
                end = content.find("```", start)
                if end != -1:
                    json_str = content[start:end].strip()
                    parsed = json.loads(json_str)
                    formatted_json = json.dumps(parsed, indent=2, ensure_ascii=False)
                    return content[:start] + "\n" + formatted_json + "\n" + content[end:]
            except:
                pass
                
    return content

# 清理 AI 响应，提取最终答案
def clean_ai_response(content_to_print):
    if not isinstance(content_to_print, str):
        return str(content_to_print)
        
    match = re.search(r"\[\[Final Answer:\s*(.*?)\]\]", content_to_print, re.DOTALL | re.IGNORECASE)
    if match:
        content_to_print = match.group(1).strip()
    else:
        # 如果没有明确的 Final Answer 标记，尝试提取结论部分的最后一句话
        # 或者直接返回整个结论段落（如果很短）
        # 现在的逻辑太激进了，导致很多时候把有效信息都过滤掉了
        
        # 1. 先去掉参考文献
        ref_markers = [
            "### 参考", "### References", "### 参考文献", 
            "## 参考", "## References", "## 参考文献",
            "Reference:", "References:", "参考:", "参考文献:",
            "Reference", "References", "参考", "参考文献",
            "Source:", "Sources:", "来源:",
            "**Reference**", "**References**", "**参考**", "**参考文献**"
        ]
        for marker in ref_markers:
            if marker in content_to_print:
                content_to_print = content_to_print.split(marker)[0].strip()
                break
        
        # 2. 尝试提取结论部分
        concl_markers = ["## 结论", "### 结论", "## Conclusion", "### Conclusion"]
        found_conclusion = False
        for marker in concl_markers:
            if marker in content_to_print:
                parts = content_to_print.split(marker)
                if len(parts) > 1:
                    conclusion_text = parts[-1].strip()
                    if conclusion_text: # 确保结论部分不为空
                        content_to_print = conclusion_text
                        found_conclusion = True
                        break
        
        # 3. 后处理：如果内容仍然过长，尝试提取核心句子
        # 假设：如果没有 Conclusion 章节，但首行很短且看起来像答案，则取首行
        lines = [line.strip() for line in content_to_print.split('\n') if line.strip()]
        
        if found_conclusion:
             # 如果有结论章节，且内容过长 (>200字符)，尝试取最后一句
             if len(content_to_print) > 200:
                 if lines:
                     content_to_print = lines[-1]
        else:
            # 如果没有结论章节
            if lines:
                # 策略 A: 如果第一行很短 (<50字符) 且后面跟着列表项或长文，取第一行
                # (适用于模型直接输出答案然后解释的情况)
                if len(lines[0]) < 50 and len(lines) > 1:
                    content_to_print = lines[0]
                # 策略 B: 如果最后一行很短 (<100字符)，取最后一行
                # (适用于模型在最后总结的情况)
                elif len(lines[-1]) < 100:
                    content_to_print = lines[-1]
                # 策略 C: 否则，如果内容实在太长 (>500字符)，强制截断或取最后一段
                elif len(content_to_print) > 500:
                    content_to_print = lines[-1] # 回退到取最后一段，避免返回整篇论文
            
    # 最后清理一下格式
    # 移除所有 Markdown 格式（如 **bold**）但保留内容
    content_to_print = re.sub(r"\*\*(.*?)\*\*", r"\1", content_to_print) # **text** -> text
    content_to_print = re.sub(r"`(.*?)`", r"\1", content_to_print)       # `text` -> text
    content_to_print = re.sub(r"__(.*?)__", r"\1", content_to_print)     # __text__ -> text
    content_to_print = re.sub(r"\*(.*?)\*", r"\1", content_to_print)     # *text* -> text
    
    # 移除可能残留的 Markdown 链接 [text](url) -> text
    content_to_print = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", content_to_print)

    # 最终强制策略：如果内容仍然包含多行，只取第一行
    # 这是为了解决 [ai] 消息包含正确答案但后续跟了引用列表，导致 answer 字段取值错误的问题
    lines = [line.strip() for line in content_to_print.split('\n') if line.strip()]
    if lines:
        content_to_print = lines[0]
    
    return content_to_print.strip()

# 处理用户查询
@agent_app.query(framework="langgraph")
async def query_func(
    self,
    msgs: List[BaseMessage],
    request: AgentRequest = None,
    **kwargs,
) -> AsyncIterator[tuple[BaseMessage, bool]]:
    session_id = request.session_id
    user_id = request.user_id
    print(f"Received query from user {user_id} with session {session_id}")
    
    timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    log_file_name = f"{timestamp}.log"
    log_file_path = os.path.join(log_dir, log_file_name)
    
    logger_name = f"AgentChat_{session_id}_{timestamp}"
    request_logger = logging.getLogger(logger_name)
    request_logger.setLevel(logging.INFO)
    
    if request_logger.handlers:
        request_logger.handlers.clear()
        
    file_handler = logging.FileHandler(log_file_path, mode='w', encoding='utf-8')
    formatter = logging.Formatter('%(asctime)s - [%(name)s] - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    request_logger.addHandler(file_handler)
    
    # Attach the file handler to the 'src' logger to capture logs from nodes.py and other modules
    src_logger = logging.getLogger("src")
    src_logger.setLevel(logging.INFO)
    src_logger.addHandler(file_handler)
    
    request_logger.info(f"Starting new session: {session_id}")
    request_logger.info(f"User Input: {msgs[-1].content if msgs else 'None'}")

    try:
        config = {"configurable": {"thread_id": session_id}}
        inputs = {"messages": msgs}

        last_yielded_msg_id = None

        async for event in self.graph.astream(inputs, config, stream_mode="values"):
            if "messages" in event:
                messages = event["messages"]
                if not messages:
                    continue
                    
                last_message = messages[-1]
                
                if isinstance(last_message, HumanMessage) and last_message.content == msgs[-1].content:
                    continue
                
                if last_message.id and last_message.id == last_yielded_msg_id:
                    continue
                    
                last_yielded_msg_id = last_message.id
                
                is_last_chunk = False 
                
                sender = last_message.name if hasattr(last_message, "name") and last_message.name else last_message.type
                
                if sender == "planner":
                    request_logger.info(f"[{sender} 📅]: Research Plan Generated")
                    try:
                        content_str = last_message.content
                        try:
                            content_json = json.loads(content_str)
                            if isinstance(content_json, dict) and "content" in content_json and isinstance(content_json["content"], str):
                                 try:
                                     nested_json = json.loads(content_json["content"].strip().strip("`").replace("json\n", ""))
                                     if isinstance(nested_json, dict) and "steps" in nested_json:
                                         content_json = nested_json
                                 except:
                                     pass
                        except:
                            content_json = None
    
                        if isinstance(content_json, dict) and "steps" in content_json:
                            request_logger.info(f"Topic: {content_json.get('title', 'N/A')}")
                            request_logger.info(json.dumps(content_json, indent=2, ensure_ascii=False))
                        else:
                            formatted_content = format_message_content(last_message.content)
                            request_logger.info(f"{formatted_content}")
                    except Exception as e:
                        formatted_content = format_message_content(last_message.content)
                        request_logger.info(f"{formatted_content}")
                else:
                    formatted_content = format_message_content(last_message.content)
                    request_logger.info(f"[{sender}]: {formatted_content}")
    
                for handler in request_logger.handlers:
                    handler.flush()

                if sender == "planner":
                    # 发送保活信号
                    yield AIMessage(content="", id="keep_alive"), False
                elif sender == "ai":
                    cleaned_content = clean_ai_response(last_message.content)
                    msg_id = last_message.id if hasattr(last_message, "id") else None
                    yield AIMessage(content=cleaned_content, id=msg_id), is_last_chunk
                else:
                    # 对其他步骤也发送保活信号
                    yield AIMessage(content="", id="keep_alive"), False
    except Exception as e:
        request_logger.error(f"Error during query execution: {str(e)}")
        raise e
    finally:
        # Remove the handler from src logger to prevent duplicates/leaks
        if 'src_logger' in locals():
            src_logger.removeHandler(file_handler)
            
        for handler in request_logger.handlers:
            handler.close()
            request_logger.removeHandler(handler)


# 获取短期记忆
@agent_app.endpoint("/short-term-memory/{session_id}", methods=["GET"])
async def get_short_term_memory(session_id: str):
    return {"status": "not_implemented_yet", "message": "Memory access requires refactoring builder.py"}


# 获取长期记忆
@agent_app.endpoint("/long-term-memory/{user_id}", methods=["GET"])
async def get_long_term_memory(user_id: str):
    namespace_for_long_term_memory = (user_id, "memories")
    long_term_mem = long_term_memory.search(namespace_for_long_term_memory)

    def serialize_search_item(item):
        return {
            "namespace": item.namespace,
            "key": item.key,
            "value": item.value,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "score": item.score,
        }

    serialized = [serialize_search_item(item) for item in long_term_mem]
    return serialized

if __name__ == '__main__':
    agent_app.run()