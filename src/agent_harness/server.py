import json
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal, TypedDict

import anyio
from mcp.server import MCPServer

mcp = MCPServer("Demo")

GOOGLE_NEWS_RSS = "https://news.google.com/rss"
FEED_PARAMS = "hl=en-US&gl=US&ceid=US:en"

YAHOO_CHART_BASE = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_CHART_URL = YAHOO_CHART_BASE + "?range=5d&interval=1d"
# With range=1d, chartPreviousClose is the prior session's close, so one call gives a live quote.
YAHOO_LIVE_URL = YAHOO_CHART_BASE + "?range=1d&interval=1d"
YAHOO_HISTORY_URL = YAHOO_CHART_BASE + "?range={range}&interval={interval}&includePrePost=false"
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

MARKET_OVERVIEW = {
    "indices": ["^GSPC", "^DJI", "^IXIC", "^RUT"],
    "volatility": ["^VIX"],
    "treasury_yields": ["^FVX", "^TNX", "^TYX"],
    "commodities": ["CL=F", "GC=F", "SI=F", "NG=F"],
    "currencies": ["DX-Y.NYB", "EURUSD=X", "JPY=X"],
    "crypto": ["BTC-USD", "ETH-USD"],
}

SECTOR_ETFS = {
    "XLK": "Technology",
    "XLF": "Financials",
    "XLV": "Health Care",
    "XLE": "Energy",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLI": "Industrials",
    "XLB": "Materials",
    "XLU": "Utilities",
    "XLRE": "Real Estate",
    "XLC": "Communication Services",
}

# Bar size per lookback, chosen to keep every history response under ~80 bars.
HISTORY_INTERVALS = {
    "1d": "5m",
    "5d": "30m",
    "1mo": "1d",
    "3mo": "1d",
    "6mo": "1wk",
    "ytd": "1wk",
    "1y": "1wk",
    "5y": "1mo",
}

MARKET_STATES = {"pre": "pre-market", "regular": "open", "post": "after-hours"}


class Quote(TypedDict):
    symbol: str
    name: str
    price: float
    change: float
    change_percent: float


class LiveQuote(Quote):
    previous_close: float
    day_high: float | None
    day_low: float | None
    volume: int | None
    fifty_two_week_high: float | None
    fifty_two_week_low: float | None
    currency: str
    market_state: str
    as_of: str


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


def _fetch_chart(url: str, attempts: int = 2) -> dict | None:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)["chart"]["result"][0]
        except urllib.error.HTTPError as exc:
            # Unknown symbols come back as 404; only throttling and server errors are worth a retry.
            if exc.code != 429 and exc.code < 500:
                return None
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            pass
        if attempt + 1 < attempts:
            time.sleep(0.5)
    return None


def _fetch_quote(symbol: str) -> Quote | None:
    result = _fetch_chart(YAHOO_CHART_URL.format(symbol=urllib.parse.quote(symbol)))
    if result is None:
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


def _market_state(meta: dict) -> str:
    if meta.get("instrumentType") == "CRYPTOCURRENCY":
        return "open"
    now = time.time()
    for period, state in MARKET_STATES.items():
        window = meta.get("currentTradingPeriod", {}).get(period, {})
        if window.get("start", 0) <= now < window.get("end", 0):
            return state
    return "closed"


def _iso(timestamp: int | None) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat() if timestamp else ""


def _positive(value: float | None, digits: int = 2) -> float | None:
    """Yahoo reports missing day ranges (e.g. on yields) as 0; surface those as null."""
    return round(value, digits) if value else None


def _fetch_live_quote(symbol: str) -> LiveQuote | None:
    result = _fetch_chart(YAHOO_LIVE_URL.format(symbol=urllib.parse.quote(symbol)))
    if result is None:
        return None

    meta = result["meta"]
    price = meta.get("regularMarketPrice")
    previous_close = meta.get("previousClose") or meta.get("chartPreviousClose")
    if price is None or not previous_close:
        return None
    digits = max(2, meta.get("priceHint", 2))
    return {
        # Echo the requested symbol: Yahoo sometimes answers under an alias (JPY=X -> USDJPY=X).
        "symbol": symbol,
        "name": meta.get("shortName") or meta.get("longName", ""),
        "price": round(price, digits),
        "previous_close": round(previous_close, digits),
        "change": round(price - previous_close, digits),
        "change_percent": round((price - previous_close) / previous_close * 100, 2),
        "day_high": _positive(meta.get("regularMarketDayHigh"), digits),
        "day_low": _positive(meta.get("regularMarketDayLow"), digits),
        "volume": meta.get("regularMarketVolume") or None,
        "fifty_two_week_high": _positive(meta.get("fiftyTwoWeekHigh"), digits),
        "fifty_two_week_low": _positive(meta.get("fiftyTwoWeekLow"), digits),
        "currency": meta.get("currency", ""),
        "market_state": _market_state(meta),
        "as_of": _iso(meta.get("regularMarketTime")),
    }


async def _fetch_all[T](fetch: Callable[[str], T | None], symbols: list[str]) -> list[T]:
    """Run a blocking per-symbol fetch concurrently, preserving order and dropping failures."""
    results: list[T | None] = [None] * len(symbols)

    async def run(i: int, symbol: str) -> None:
        results[i] = await anyio.to_thread.run_sync(fetch, symbol)

    async with anyio.create_task_group() as tg:
        for i, symbol in enumerate(symbols):
            tg.start_soon(run, i, symbol)
    return [r for r in results if r is not None]


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
    quotes = await _fetch_all(_fetch_quote, SECTOR_TICKERS[sector])
    ranked = sorted(quotes, key=lambda q: q["change_percent"], reverse=True)
    return {
        "gainers": [q for q in ranked if q["change_percent"] > 0][:limit],
        "losers": [q for q in reversed(ranked) if q["change_percent"] < 0][:limit],
    }


@mcp.tool()
async def get_quotes(symbols: list[str]) -> dict[str, list]:
    """Get live quotes for up to 20 tickers: price, change vs. the previous close, day range,
    volume, 52-week range, and whether the market is open, pre-market, after-hours, or closed.

    Works for anything Yahoo Finance lists: stocks ("AAPL"), ETFs ("SPY"), indices ("^GSPC"),
    futures ("CL=F"), currencies ("EURUSD=X"), and crypto ("BTC-USD").

    Args:
        symbols: Ticker symbols, e.g. ["AAPL", "MSFT", "^VIX"].
    """
    symbols = list(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))[:20]
    quotes = await _fetch_all(_fetch_live_quote, symbols)
    found = {q["symbol"] for q in quotes}
    return {"quotes": quotes, "not_found": [s for s in symbols if s not in found]}


@mcp.tool()
async def get_market_overview() -> dict[str, list[LiveQuote]]:
    """Get a live snapshot of the overall market: major US indices, the VIX, Treasury yields
    (5/10/30-year, quoted in percent), oil, gold, silver, natural gas, the dollar index, EUR/USD,
    USD/JPY, Bitcoin, and Ether.
    """
    groups = list(MARKET_OVERVIEW.items())
    flat = [symbol for _, symbols in groups for symbol in symbols]
    by_symbol = {q["symbol"]: q for q in await _fetch_all(_fetch_live_quote, flat)}
    return {
        name: [by_symbol[s] for s in symbols if s in by_symbol]
        for name, symbols in groups
    }


@mcp.tool()
async def get_sector_performance() -> list[dict]:
    """Rank the 11 S&P 500 sectors by today's move, using the SPDR sector ETFs (XLK, XLF, ...).

    Useful for seeing where money is rotating: which sectors lead and lag the market today.
    """
    quotes = await _fetch_all(_fetch_live_quote, list(SECTOR_ETFS))
    ranked = sorted(quotes, key=lambda q: q["change_percent"], reverse=True)
    return [
        {
            "sector": SECTOR_ETFS[q["symbol"]],
            "etf": q["symbol"],
            "price": q["price"],
            "change_percent": q["change_percent"],
            "market_state": q["market_state"],
        }
        for q in ranked
    ]


def _fetch_history(symbol: str, period: str) -> dict | None:
    interval = HISTORY_INTERVALS[period]
    url = YAHOO_HISTORY_URL.format(symbol=urllib.parse.quote(symbol), range=period, interval=interval)
    result = _fetch_chart(url)
    if result is None:
        return None

    meta = result["meta"]
    digits = max(2, meta.get("priceHint", 2))
    ohlcv = result["indicators"]["quote"][0]
    bars = []
    for i, ts in enumerate(result.get("timestamp", [])):
        close = ohlcv["close"][i]
        if close is None:
            continue
        bars.append(
            {
                "time": _iso(ts),
                "open": round(ohlcv["open"][i] or close, digits),
                "high": round(ohlcv["high"][i] or close, digits),
                "low": round(ohlcv["low"][i] or close, digits),
                "close": round(close, digits),
                "volume": ohlcv["volume"][i] or 0,
            }
        )
    if not bars:
        return None

    start = bars[0]["open"]
    end = bars[-1]["close"]
    return {
        "symbol": symbol,
        "name": meta.get("shortName") or meta.get("longName", ""),
        "currency": meta.get("currency", ""),
        "period": period,
        "interval": interval,
        "summary": {
            "start": start,
            "end": end,
            "high": max(b["high"] for b in bars),
            "low": min(b["low"] for b in bars),
            "change_percent": round((end - start) / start * 100, 2) if start else None,
        },
        "bars": bars,
    }


@mcp.tool()
async def get_price_history(
    symbol: str,
    period: Literal["1d", "5d", "1mo", "3mo", "6mo", "ytd", "1y", "5y"] = "1d",
) -> dict:
    """Get price bars (OHLCV) for one ticker, plus a summary of the move over the period.

    The bar size scales with the period: 5-minute bars for "1d" (today's intraday action),
    30-minute for "5d", daily for "1mo"/"3mo", weekly up to "1y", monthly for "5y".
    Times are UTC ISO-8601.

    Args:
        symbol: Ticker symbol, e.g. "NVDA", "^GSPC", "BTC-USD".
        period: Lookback window.
    """
    history = await anyio.to_thread.run_sync(_fetch_history, symbol.strip().upper(), period)
    if history is None:
        raise ValueError(f"No price data found for {symbol!r}")
    return history


@mcp.resource("greeting://{name}")
def greeting(name: str) -> str:
    """Greet someone by name."""
    return f"Hello, {name}!"
