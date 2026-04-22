import requests
import logging
from typing import Optional, List
from langchain_core.tools import Tool

logger = logging.getLogger(__name__)

def search_wikidata(query: str) -> str:
    """Search for Wikidata items by name."""
    logger.info(f"Searching Wikidata for: {query}")
    url = "https://www.wikidata.org/w/api.php"
    params = {
        "action": "wbsearchentities",
        "format": "json",
        "language": "en",
        "search": query,
        "limit": 5
    }
    headers = {
        "User-Agent": "ResearchAgent/1.0"
    }
    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if "search" in data:
            results = []
            for item in data["search"]:
                item_id = item.get('id', 'N/A')
                label = item.get('label', 'N/A')
                description = item.get('description', 'No description')
                url = item.get('url', f"https://www.wikidata.org/wiki/{item_id}")
                results.append(f"ID: {item_id}\nLabel: {label}\nDescription: {description}\nURL: {url}\n")
            return "\n---\n".join(results) if results else "No results found."
        return "No results found."
    except Exception as e:
        logger.error(f"Error searching Wikidata: {e}")
        return f"Error searching Wikidata: {str(e)}"

def query_wikidata_sparql(query: str) -> str:
    """Execute a SPARQL query on Wikidata."""
    logger.info(f"Executing SPARQL query: {query}")
    url = "https://query.wikidata.org/sparql"
    # Wikidata requires a User-Agent header
    headers = {
        "User-Agent": "ResearchAgent/1.0",
        "Accept": "application/json"
    }
    try:
        response = requests.get(url, params={"query": query, "format": "json"}, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        if "results" in data and "bindings" in data["results"]:
            bindings = data["results"]["bindings"]
            if not bindings:
                return "No results found."
            
            # Format results
            output = []
            # Get headers from the first result
            keys = bindings[0].keys()
            output.append(" | ".join(keys))
            output.append("-" * (len(keys) * 10))
            
            for binding in bindings[:20]: # Limit to 20 results
                row = []
                for key in keys:
                    if key in binding:
                        row.append(str(binding[key]['value']))
                    else:
                        row.append("")
                output.append(" | ".join(row))
            
            result_str = "\n".join(output)
            if len(bindings) > 20:
                result_str += f"\n\n... and {len(bindings) - 20} more results."
            return result_str
            
        return "No results found."
    except Exception as e:
        logger.error(f"Error executing SPARQL query: {e}")
        return f"Error executing SPARQL query: {str(e)}"

def get_wikidata_tools() -> List[Tool]:
    """Returns a list of Wikidata tools."""
    logger.info("Initialized Wikidata tools")
    return [
        Tool(
            name="wikidata_search",
            description="Search for Wikidata entities (items) by name. Returns ID, label, description and URL. Use this to find the Q-code for an entity (e.g., 'Q42' for Douglas Adams) before using it in a SPARQL query.",
            func=search_wikidata
        ),
        Tool(
            name="wikidata_sparql",
            description="Execute a SPARQL query on Wikidata. Useful for complex filtering, aggregations, and structured data retrieval (e.g., 'Find all football players born in Africa...', or 'Find all companies founded by Person X'). Requires valid SPARQL syntax.",
            func=query_wikidata_sparql
        )
    ]
