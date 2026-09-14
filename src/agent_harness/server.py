import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import anyio
from mcp.server import MCPServer

mcp = MCPServer("Demo")

GOOGLE_NEWS_RSS = "https://news.google.com/rss"
FEED_PARAMS = "hl=en-US&gl=US&ceid=US:en"


def _fetch_google_news(url: str, limit: int) -> list[dict[str, str]]:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=10) as response:
        root = ET.fromstring(response.read())

    articles = []
    for item in root.iter("item"):
        published = item.findtext("pubDate", "")
        articles.append(
            {
                "title": item.findtext("title", ""),
                "source": item.findtext("source", ""),
                "published": parsedate_to_datetime(published).isoformat() if published else "",
                "link": item.findtext("link", ""),
            }
        )
    articles.sort(key=lambda a: a["published"], reverse=True)
    return articles[:limit]


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@mcp.tool()
async def get_financial_news(limit: int = 10, query: str | None = None) -> list[dict[str, str]]:
    """Get the latest top financial news headlines from Google News.

    Args:
        limit: Maximum number of articles to return (1-50).
        query: Optional topic to search within the past day (e.g. "Federal Reserve", "NVDA").
            If omitted, returns the top stories from Google News' Business section.
    """
    limit = max(1, min(limit, 50))
    if query:
        q = urllib.parse.quote(f"{query} finance when:1d")
        url = f"{GOOGLE_NEWS_RSS}/search?q={q}&{FEED_PARAMS}"
    else:
        url = f"{GOOGLE_NEWS_RSS}/headlines/section/topic/BUSINESS?{FEED_PARAMS}"
    return await anyio.to_thread.run_sync(_fetch_google_news, url, limit)


@mcp.resource("greeting://{name}")
def greeting(name: str) -> str:
    """Greet someone by name."""
    return f"Hello, {name}!"
