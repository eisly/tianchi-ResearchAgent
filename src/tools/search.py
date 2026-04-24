
import logging
import os
import asyncio
import time
import uuid
import requests
from typing import Optional, Any

from langchain_community.tools import BaseTool, Tool
from langchain_community.tools import DuckDuckGoSearchRun, WikipediaQueryRun
from langchain_community.utilities import DuckDuckGoSearchAPIWrapper, ArxivAPIWrapper, WikipediaAPIWrapper, GoogleSerperAPIWrapper
try:
    from langchain_google_community import GoogleSearchAPIWrapper
except ImportError:
    try:
        from langchain_community.utilities import GoogleSearchAPIWrapper
    except ImportError:
        GoogleSearchAPIWrapper = None

from langchain_community.tools import ArxivQueryRun
from langchain_mcp_adapters.client import MultiServerMCPClient
from src.tools.scraper import scrape_web_page
import re

import wikipedia

logger = logging.getLogger(__name__)

# Global singletons
_SEARCH_TOOL = None
_ACADEMIC_SEARCH_TOOL = None
_WIKIPEDIA_TOOL = None
_TONGXIAO_SEARCH_TOOLS = None
_GOOGLE_SERPER_TOOL = None

# Global search call counter
_SEARCH_CALL_COUNTER = 0
_MAX_SEARCH_CALLS = int(os.getenv("MAX_SEARCH_CALLS_PER_STEP", "20"))
# Per-tool usage counter
_TOOL_USAGE_COUNTER = {}
_MAX_SINGLE_TOOL_CALLS = int(os.getenv("MAX_SINGLE_TOOL_CALLS_PER_STEP", "10"))

def reset_search_counter():
    """Reset the global search call counter and tool usage counters."""
    global _SEARCH_CALL_COUNTER, _TOOL_USAGE_COUNTER
    _SEARCH_CALL_COUNTER = 0
    _TOOL_USAGE_COUNTER = {}
    logger.info("Search call counters reset")

def _check_and_increment_search_counter(tool_name: str) -> Optional[str]:
    """
    Check if search limit is reached (global and per-tool). If not, increment counters.
    Returns error message if limit reached, None otherwise.
    """
    global _SEARCH_CALL_COUNTER, _TOOL_USAGE_COUNTER, _MAX_SEARCH_CALLS, _MAX_SINGLE_TOOL_CALLS
    
    # Initialize _TOOL_USAGE_COUNTER if not exists
    if not isinstance(_TOOL_USAGE_COUNTER, dict):
        _TOOL_USAGE_COUNTER = {}

    # 1. Check global limit
    if _SEARCH_CALL_COUNTER >= _MAX_SEARCH_CALLS:
        msg = (
            f"SYSTEM ALERT: You have reached the maximum of {_MAX_SEARCH_CALLS} TOTAL search calls for this step. "
            "Further searches are BLOCKED. You MUST STOP searching immediately. "
            "Synthesize the information you have collected so far and provide your best answer based on existing findings. "
            "Do NOT call any more tools."
        )
        logger.warning(f"[{tool_name}] Total search limit reached. Blocking call.")
        return msg

    # 2. Check per-tool limit
    current_tool_usage = _TOOL_USAGE_COUNTER.get(tool_name, 0)
    if current_tool_usage >= _MAX_SINGLE_TOOL_CALLS:
        msg = (
            f"SYSTEM ALERT: You have used the tool '{tool_name}' {_MAX_SINGLE_TOOL_CALLS} times, which is the limit for a single tool. "
            f"You MUST switch to a DIFFERENT search tool (e.g., use 'academic_search' or 'web_search' instead of '{tool_name}'). "
            "Using the same tool repeatedly is ineffective. TRY ANOTHER TOOL."
        )
        logger.warning(f"[{tool_name}] Per-tool limit reached ({_MAX_SINGLE_TOOL_CALLS}). Blocking call.")
        return msg
    
    # Increment counters
    _SEARCH_CALL_COUNTER += 1
    _TOOL_USAGE_COUNTER[tool_name] = current_tool_usage + 1
    
    logger.info(f"[{tool_name}] Search call {_SEARCH_CALL_COUNTER}/{_MAX_SEARCH_CALLS} (Tool usage: {_TOOL_USAGE_COUNTER[tool_name]}/{_MAX_SINGLE_TOOL_CALLS})")
    return None

class CustomDuckDuckGoWrapper:
    """
    A DuckDuckGo Search API wrapper using langchain_community.utilities.DuckDuckGoSearchAPIWrapper.
    It implements retry logic with different backends (api, html, lite) to handle rate limits or blocks.
    """
    def __init__(self, max_results: int = 5):
        self.max_results = max_results
        # Default wrapper
        self.wrapper = DuckDuckGoSearchAPIWrapper(max_results=max_results)
        self.search_tool = DuckDuckGoSearchRun(api_wrapper=self.wrapper)

    def _run_with_retry(self, query: str) -> str:
        """Try running search with different backends."""
        backends = ["api", "html", "lite"]
        last_error = None
        
        for backend in backends:
            try:
                # Create a fresh wrapper for each attempt to ensure clean state
                wrapper = DuckDuckGoSearchAPIWrapper(
                    max_results=self.max_results,
                    backend=backend
                )
                # logger.info(f"Attempting DuckDuckGo search with backend='{backend}'")
                
                # Use results() method to get structured data directly
                # This ensures we get links, titles, and snippets reliably
                results = wrapper.results(query, max_results=self.max_results)
                
                if not results:
                    continue # Try next backend if no results

                # Format results manually to ensure URLs are present and clear
                formatted_output = []
                for res in results:
                    link = res.get('link', '')
                    title = res.get('title', '')
                    snippet = res.get('snippet', '')
                    if link:
                        formatted_output.append(f"Title: {title}\nLink: {link}\nSnippet: {snippet}")
                
                if not formatted_output:
                     return "No results found."

                return "\n\n".join(formatted_output)

            except Exception as e:
                logger.warning(f"DuckDuckGo search failed with backend='{backend}': {e}")
                last_error = e
                # Wait a bit before retrying
                time.sleep(1)
        
        # If all failed
        logger.error(f"All DuckDuckGo search backends failed. Last error: {last_error}")
        return f"Error performing search: {str(last_error)}"

    def run(self, query: str) -> str:
        """
        Run query through DuckDuckGo Search API.
        Also automatically crawls the first valid result to provide more context.
        """
        # Check limit
        error_msg = _check_and_increment_search_counter("duckduckgo")
        if error_msg:
            return error_msg

        logger.info(f"正在使用duckduckgo搜索: {query}")
        search_result = self._run_with_retry(query)
        
        return _auto_crawl_result(search_result, "duckduckgo")

    async def arun(self, query: str) -> str:
        """Run query asynchronously."""
        # Check limit (check here too to avoid starting thread if limit reached)
        # Note: The counter increment in run() will happen again if we don't handle it carefully.
        # But since run() is called in executor, we should probably check here and NOT increment, 
        # OR let run() handle it. 
        # If we check here and return error, run() is not called.
        # If we proceed, run() is called.
        # To avoid double counting if we called _check_and_increment here, we should probably just rely on run().
        # BUT, run() is blocking. 
        # Let's just let run() handle it to keep it simple and thread-safe-ish.
        # However, for async tools generally, we want to fail fast.
        # But since CustomDuckDuckGoWrapper.arun just wraps self.run, self.run will do the check.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.run, query)

class CustomGoogleSearchWrapper:
    """
    A Google Search API wrapper using langchain_google_community.GoogleSearchAPIWrapper.
    """
    def __init__(self, max_results: int = 5):
        self.max_results = max_results
        if GoogleSearchAPIWrapper is None:
             raise ImportError("GoogleSearchAPIWrapper not found. Please install langchain-google-community or langchain-community.")
        self.wrapper = GoogleSearchAPIWrapper(k=max_results)

    def run(self, query: str) -> str:
        # Check limit
        error_msg = _check_and_increment_search_counter("google_search")
        if error_msg:
            return error_msg

        logger.info(f"正在使用Google搜索: {query}")
        try:
            # Use results() method to get structured data directly
            results = self.wrapper.results(query, num_results=self.max_results)
            
            if not results:
                 return "No results found."

            # Format results manually to ensure URLs are present and clear
            formatted_output = []
            for res in results:
                link = res.get('link', '')
                title = res.get('title', '')
                snippet = res.get('snippet', '')
                if link:
                    formatted_output.append(f"Title: {title}\nLink: {link}\nSnippet: {snippet}")
            
            search_result = "\n\n".join(formatted_output)
            return _auto_crawl_result(search_result, "google_search")
            
        except Exception as e:
            logger.error(f"Google search failed: {e}")
            return f"Error performing search: {str(e)}"

    async def arun(self, query: str) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.run, query)

def _auto_crawl_result(search_result: str, tool_name: str) -> str:
    """
    Helper to automatically crawl the first URL found in search results.
    """
    try:
        # Extract URL logic (same as currently in CustomDuckDuckGoWrapper)
        # Look for 'Link: <url>' pattern first
        link_pattern = r'Link:\s*(https?://[^\s]+)'
        urls = re.findall(link_pattern, search_result)
        
        if not urls:
            # Fallback to general pattern
            url_pattern = r'https?://[^\s)\]"]+'
            urls = re.findall(url_pattern, search_result)
        
        if urls:
            first_url = urls[0].rstrip('.,;:)]}"\'')
            logger.info(f"正在自动爬取第一条结果 ({tool_name}): {first_url}")
            
            crawled_content = scrape_web_page(first_url)
            
            if len(crawled_content) > 1500:
                crawled_content = crawled_content[:1500] + "... [truncated]"
            
            return f"{search_result}\n\n--- [Automatic Crawl of Top Result: {first_url}] ---\n{crawled_content}"
    except Exception as e:
        logger.warning(f"Failed to automatically crawl top result in {tool_name}: {e}")
        
    return search_result


async def get_web_search_tool(max_search_results: int = 10) -> BaseTool:
    """
    Returns a web search tool powered by DuckDuckGo.
    """
    global _SEARCH_TOOL
    
    # Check if we are already using DuckDuckGo
    if _SEARCH_TOOL and hasattr(_SEARCH_TOOL, 'func') and hasattr(_SEARCH_TOOL.func, '__self__'):
        wrapper_instance = _SEARCH_TOOL.func.__self__
        if isinstance(wrapper_instance, CustomDuckDuckGoWrapper):
            return _SEARCH_TOOL

    logger.info("Using DuckDuckGo Search.")
    wrapper = CustomDuckDuckGoWrapper(max_results=max_search_results)
    
    # Create Tool
    tool = Tool(
        name="web_search",
        description="A search engine. Useful for when you need to answer questions about current events. Input should be a search query.",
        func=wrapper.run,
        coroutine=wrapper.arun
    )
    
    _SEARCH_TOOL = tool
    logger.info(f"Initialized web search tool with {type(wrapper).__name__}")
    return tool

async def get_academic_search_tool(max_results: int = 10) -> BaseTool:
    """
    Returns an academic search tool.
    Currently only uses ArXiv since SerpApi is disabled by user request.
    """
    global _ACADEMIC_SEARCH_TOOL
    
    if _ACADEMIC_SEARCH_TOOL:
        return _ACADEMIC_SEARCH_TOOL

    # 1. SerpApi (Google Scholar) is DISABLED by user request
    # if os.environ.get("SERPAPI_API_KEY"): ...
    
    # 2. Use ArXiv
    try:
        arxiv_wrapper = ArxivAPIWrapper(
            top_k_results=max_results,
            DOC_CONTENT_CHARS_MAX=4000
        )
        arxiv_tool = ArxivQueryRun(api_wrapper=arxiv_wrapper)
        
        # Wrap to avoid type hint issues with BaseTool.run and uuid
        # We also want to track usage
        original_run = arxiv_tool.run
        
        def wrapped_run(query: str, **kwargs) -> str:
            error_msg = _check_and_increment_search_counter("academic_search")
            if error_msg:
                return error_msg
            return original_run(query, **kwargs)
            
        arxiv_tool.func = wrapped_run
        
        _ACADEMIC_SEARCH_TOOL = arxiv_tool
        logger.info("Initialized academic search tool (ArXiv)")
        return arxiv_tool
    except Exception as e:
        logger.error(f"Failed to initialize academic search tool: {e}")
        return None

async def get_wikipedia_tool() -> BaseTool:
    """
    Returns a Wikipedia search tool.
    """
    global _WIKIPEDIA_TOOL
    
    if _WIKIPEDIA_TOOL:
        return _WIKIPEDIA_TOOL

    try:
        wikipedia_wrapper = WikipediaAPIWrapper(
            top_k_results=3,
            doc_content_chars_max=4000
        )
        wikipedia_tool = WikipediaQueryRun(api_wrapper=wikipedia_wrapper)
        
        # Wrap for usage tracking
        original_run = wikipedia_tool.run
        
        def wrapped_run(query: str, **kwargs) -> str:
            error_msg = _check_and_increment_search_counter("wikipedia")
            if error_msg:
                return error_msg
            return original_run(query, **kwargs)
            
        wikipedia_tool.func = wrapped_run
        
        _WIKIPEDIA_TOOL = wikipedia_tool
        logger.info("Initialized Wikipedia tool")
        return wikipedia_tool
    except Exception as e:
        logger.error(f"Failed to initialize Wikipedia tool: {e}")
        return None

async def get_tongxiao_search_tools() -> list[BaseTool]:
    """
    Returns search tools from Tongxiao MCP server.
    Requires 'TONGXIAO_API_KEY' or 'tongxiao_api_key' in environment variables.
    """
    global _TONGXIAO_SEARCH_TOOLS
    
    if _TONGXIAO_SEARCH_TOOLS:
        return _TONGXIAO_SEARCH_TOOLS

    api_key = os.environ.get("tongxiao_api_key")
    if not api_key:
        logger.warning("TONGXIAO_API_KEY not found. Tongxiao MCP search tools will not be available.")
        return []

    # Handle Windows npx command
    command = "npx.cmd" if os.name == 'nt' else "npx"

    try:
        client = MultiServerMCPClient(
            {
                "tongxiao-common-search": {
                    "transport": "stdio",
                    "command": command,
                    "args": [
                        "--quiet",
                        "-y",
                        "@tongxiao/common-search-mcp-server"
                    ],
                    "env": {
                        "TONGXIAO_API_KEY": api_key,
                        "PATH": os.environ.get("PATH", ""),
                        "NPM_CONFIG_LOGLEVEL": "silent"
                    }
                }
            }
        )
        
        # Initialize tools
        tools = await client.get_tools()
        
        if tools:
            # Wrap tools to add logging
            wrapped_tools = []
            for t in tools:
                # Add logging wrapper to the tool's function/coroutine
                if hasattr(t, 'func') and t.func:
                    original_func = t.func
                    def wrapped_func(*args, **kwargs):
                        # Check limit
                        error_msg = _check_and_increment_search_counter(t.name)
                        if error_msg:
                            if getattr(t, 'response_format', 'content') == 'content_and_artifact':
                                return error_msg, None
                            return error_msg

                        logger.info(f"正在使用通晓搜索服务: {t.name}")
                        try:
                            result = original_func(*args, **kwargs)
                            if isinstance(result, str):
                                new_result = _auto_crawl_result(result, t.name)
                                if getattr(t, 'response_format', 'content') == 'content_and_artifact':
                                    return new_result, None
                                return new_result
                            return result
                        except Exception as e:
                            error_msg = f"Error executing {t.name}: {str(e)}"
                            logger.error(error_msg)
                            final_msg = f"Tool execution failed: {error_msg}. Please try another tool."
                            if getattr(t, 'response_format', 'content') == 'content_and_artifact':
                                return final_msg, None
                            return final_msg
                    t.func = wrapped_func
                    
                if hasattr(t, 'coroutine') and t.coroutine:
                    original_coroutine = t.coroutine
                    async def wrapped_coroutine(*args, **kwargs):
                        # Check limit
                        error_msg = _check_and_increment_search_counter(t.name)
                        if error_msg:
                            if getattr(t, 'response_format', 'content') == 'content_and_artifact':
                                return error_msg, None
                            return error_msg

                        logger.info(f"正在使用通晓搜索服务: {t.name}")
                        try:
                            result = await original_coroutine(*args, **kwargs)
                            if isinstance(result, str):
                                new_result = _auto_crawl_result(result, t.name)
                                if getattr(t, 'response_format', 'content') == 'content_and_artifact':
                                    return new_result, None
                                return new_result
                            return result
                        except Exception as e:
                            error_msg = f"Error executing {t.name}: {str(e)}"
                            logger.error(error_msg)
                            final_msg = f"Tool execution failed: {error_msg}. Please try another tool."
                            if getattr(t, 'response_format', 'content') == 'content_and_artifact':
                                return final_msg, None
                            return final_msg
                    t.coroutine = wrapped_coroutine
                    
                wrapped_tools.append(t)

            _TONGXIAO_SEARCH_TOOLS = wrapped_tools
            logger.info(f"Initialized Tongxiao MCP search tools: {[t.name for t in tools]}")
            return wrapped_tools
        else:
            logger.warning("Tongxiao MCP server returned no tools.")
            return []
            
    except Exception as e:
        logger.error(f"Failed to initialize Tongxiao MCP search tools: {e}")
        return []

class CustomGoogleSerperWrapper:
    """
    A Google Serper API wrapper using langchain_community.utilities.GoogleSerperAPIWrapper.
    """
    def __init__(self, max_results: int = 5):
        self.max_results = max_results
        
        # Check for API key in environment, allow user specified key name 'serper-api-key'
        if not os.environ.get("SERPER_API_KEY") and os.environ.get("serper_api_key"):
             os.environ["SERPER_API_KEY"] = os.environ.get("serper-api-key")
             
        if not os.environ.get("SERPER_API_KEY"):
             logger.warning("SERPER_API_KEY not found in environment variables.")

        self.wrapper = GoogleSerperAPIWrapper(k=max_results)

    def run(self, query: str) -> str:
        # Check limit
        error_msg = _check_and_increment_search_counter("google_serper")
        if error_msg:
            return error_msg

        logger.info(f"正在使用Google Serper搜索: {query}")
        print(f"\n[Tool Call] Agent is calling google-serper-api with query: {query}")
        try:
            # GoogleSerperAPIWrapper.results() returns a dictionary with 'organic', 'peopleAlsoAsk', etc.
            results = self.wrapper.results(query)
            
            if not results or 'organic' not in results:
                 return "No results found."

            # Format results
            formatted_output = []
            for res in results.get('organic', [])[:self.max_results]:
                link = res.get('link', '')
                title = res.get('title', '')
                snippet = res.get('snippet', '')
                if link:
                    formatted_output.append(f"Title: {title}\nLink: {link}\nSnippet: {snippet}")
            
            search_result = "\n\n".join(formatted_output)
            return _auto_crawl_result(search_result, "google_serper")
            
        except Exception as e:
            logger.error(f"Google Serper search failed: {e}")
            return f"Error performing search: {str(e)}"

    async def arun(self, query: str) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.run, query)

async def get_google_serper_tool(max_search_results: int = 10) -> Optional[BaseTool]:
    """
    Returns a Google Serper search tool.
    """
    global _GOOGLE_SERPER_TOOL
    
    if _GOOGLE_SERPER_TOOL:
        return _GOOGLE_SERPER_TOOL

    # Check API key presence
    if not os.environ.get("SERPER_API_KEY") and not os.environ.get("serper-api-key"):
        logger.warning("SERPER_API_KEY or serper-api-key not found. Google Serper tool will not be available.")
        return None

    try:
        wrapper = CustomGoogleSerperWrapper(max_results=max_search_results)
        
        # Create Tool
        tool = Tool(
            name="google-serper-api",
            description="A search engine powered by Google Serper API. Useful for when you need to answer questions about current events. Input should be a search query.",
            func=wrapper.run,
            coroutine=wrapper.arun
        )
        
        _GOOGLE_SERPER_TOOL = tool
        logger.info(f"Initialized Google Serper tool")
        return tool
    except Exception as e:
        logger.error(f"Failed to initialize Google Serper tool: {e}")
        return None
