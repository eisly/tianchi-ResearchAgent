
import logging
import requests
import io
import asyncio
from langchain_core.tools import Tool
from functools import lru_cache

logger = logging.getLogger(__name__)

def _scrape_pdf(url: str) -> str:
    """Helper function to scrape PDF content and metadata."""
    try:
        try:
            import PyPDF2
        except ImportError:
            try:
                import pypdf as PyPDF2
            except ImportError:
                return "Error: PyPDF2 or pypdf library not found. Please install 'pypdf' to process PDF files."

        logger.info(f"Downloading PDF from: {url}")
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        
        with io.BytesIO(response.content) as f:
            reader = PyPDF2.PdfReader(f)
            num_pages = len(reader.pages)
            
            # Extract text from first few pages
            text = f"# PDF Document Metadata\n\n- **Total Pages**: {num_pages}\n- **Source URL**: {url}\n\n## Content Preview\n\n"
            
            # Read first 5 pages max to save token and time
            limit = min(5, num_pages)
            for i in range(limit):
                try:
                    page = reader.pages[i]
                    page_text = page.extract_text() or "[No text extracted]"
                    text += f"### Page {i+1}\n{page_text}\n\n"
                except Exception as page_error:
                    text += f"### Page {i+1}\n[Error extracting text: {page_error}]\n\n"
                
            if num_pages > limit:
                text += f"\n...(Remaining {num_pages - limit} pages omitted)...\n"
                
            return text
            
    except Exception as e:
        logger.error(f"Failed to process PDF {url}: {e}")
        return f"Error processing PDF {url}: {str(e)}"

@lru_cache(maxsize=50)
def scrape_web_page(url: str) -> str:
    """
    Scrape the content of a web page and return it as markdown text using Jina Reader API.
    Uses LRU cache to avoid re-fetching same URLs.
    
    Args:
        url (str): The URL of the web page to scrape.
        
    Returns:
        str: The text content of the page, formatted as markdown.
    """
    # Check if it's a PDF
    if url.lower().endswith(".pdf"):
        return _scrape_pdf(url)

    logger.info(f"正在使用 Jina Reader 爬取网页: {url}")
    
    # Jina Reader API endpoint
    jina_url = f"https://r.jina.ai/{url}"
    
    try:
        # Use a reasonable timeout
        response = requests.get(jina_url, timeout=30)
        response.raise_for_status()
        
        text = response.text
        
        # Limit content length to avoid overwhelming the LLM context
        # Jina returns clean markdown, so we can allow a bit more content than raw HTML text
        max_length = 8000
        if len(text) > max_length:
            text = text[:max_length] + "\n\n...(content truncated due to length)..."
            
        return text

    except requests.RequestException as e:
        logger.warning(f"Failed to scrape {url} with Jina: {e}. Trying BeautifulSoup fallback...")
        try:
            from bs4 import BeautifulSoup
            fallback_response = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            fallback_response.raise_for_status()
            soup = BeautifulSoup(fallback_response.text, "html.parser")
            
            # Remove scripts and styles
            for script in soup(["script", "style"]):
                script.extract()
                
            text = soup.get_text(separator='\n')
            lines = (line.strip() for line in text.splitlines())
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            text = '\n'.join(chunk for chunk in chunks if chunk)
            
            max_length = 8000
            if len(text) > max_length:
                text = text[:max_length] + "\n\n...(content truncated due to length)..."
            
            return text
        except Exception as fallback_err:
            logger.error(f"Fallback failed for {url}: {fallback_err}")
            return f"Error scraping {url}: Jina error ({str(e)}), Fallback error ({str(fallback_err)})"
    except Exception as e:
        logger.error(f"Error processing content from {url}: {e}")
        return f"Error processing content from {url}: {str(e)}"

async def scrape_web_page_async(url: str) -> str:
    """Async wrapper for scrape_web_page"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, scrape_web_page, url)

# Define the tool
scrape_tool = Tool(
    name="crawl_tool",
    description="Useful for scraping detailed content from a specific web page URL. Input should be a valid URL starting with http:// or https://. Use this tool when you need to read the full content of a search result.",
    func=scrape_web_page,
    coroutine=scrape_web_page_async
)
