import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Literal, TypedDict

import anyio
from mcp.server import MCPServer

mcp = MCPServer("Demo")

GOOGLE_NEWS_RSS = "https://news.google.com/rss"
FEED_PARAMS = "hl=en-US&gl=US&ceid=US:en"

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
SECTOR_TICKERS = {
    "tech": [
        "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "AVGO", "TSLA", "ORCL", "AMD",
        "CRM", "ADBE", "INTC", "QCOM", "CSCO", "IBM", "MU", "PLTR", "NOW", "TXN",
    ],
    "energy": [
        "XOM", "CVX", "COP", "EOG", "SLB", "OXY", "PSX", "MPC", "VLO", "WMB",
        "KMI", "OKE", "HAL", "BKR", "DVN", "FANG", "TRGP", "CTRA", "APA", "EQT",
    ],
}


class Quote(TypedDict):
    symbol: str
    name: str
    price: float
    change: float
    change_percent: float


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


def _fetch_quote(symbol: str) -> Quote | None:
    url = YAHOO_CHART_URL.format(symbol=urllib.parse.quote(symbol))
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.load(response)["chart"]["result"][0]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None

    meta = result["meta"]
    closes = [c for c in result["indicators"]["quote"][0].get("close", []) if c is not None]
    price = meta.get("regularMarketPrice")
    if price is None or len(closes) < 2:
        return None
    previous_close = closes[-2]
    return {
        "symbol": symbol,
        "name": meta.get("shortName", ""),
        "price": round(price, 2),
        "change": round(price - previous_close, 2),
        "change_percent": round((price - previous_close) / previous_close * 100, 2),
    }


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


@mcp.tool()
async def get_market_movers(sector: Literal["tech", "energy"], limit: int = 5) -> dict[str, list[Quote]]:
    """Get today's biggest gainers and losers among major US tech or energy stocks.

    Change is measured from the previous trading day's close, using Yahoo Finance data.

    Args:
        sector: Which sector to scan: "tech" or "energy".
        limit: Number of gainers and losers to return (1-10 each).
    """
    limit = max(1, min(limit, 10))
    tickers = SECTOR_TICKERS[sector]
    quotes: list[Quote | None] = [None] * len(tickers)

    async def fetch(i: int, symbol: str) -> None:
        quotes[i] = await anyio.to_thread.run_sync(_fetch_quote, symbol)

    async with anyio.create_task_group() as tg:
        for i, symbol in enumerate(tickers):
            tg.start_soon(fetch, i, symbol)

    ranked = sorted((q for q in quotes if q), key=lambda q: q["change_percent"], reverse=True)
    return {
        "gainers": [q for q in ranked if q["change_percent"] > 0][:limit],
        "losers": [q for q in reversed(ranked) if q["change_percent"] < 0][:limit],
    }


@mcp.resource("greeting://{name}")
def greeting(name: str) -> str:
    """Greet someone by name."""
    return f"Hello, {name}!"
