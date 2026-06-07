# apps/predictions/global_cues.py
# Free APIs: yfinance, newsapi, investing.com scraper

import logging
from datetime import datetime, date
from typing import Optional
import requests

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# MARKET DATA FETCHER (yfinance - free)
# ─────────────────────────────────────────────
def fetch_global_markets() -> dict:
    """
    Fetch global market data using yfinance.
    Completely free, no API key needed.
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance not installed. Run: pip install yfinance")
        return {}

    symbols = {
        "sp500":    "^GSPC",
        "dow":      "^DJI",
        "nasdaq":   "^IXIC",
        "nikkei":   "^N225",
        "hangseng": "^HSI",
        "vix_us":   "^VIX",
        "vix_india":"^NSEBANK",
        "crude_oil":"CL=F",
        "gold":     "GC=F",
        "dxy":      "DX-Y.NYB",
        "gift_nifty":"^NSEI",
    }

    result = {}
    for name, ticker in symbols.items():
        try:
            data = yf.Ticker(ticker)
            hist = data.history(period="2d")
            if len(hist) >= 2:
                prev_close = float(hist["Close"].iloc[-2])
                last_close = float(hist["Close"].iloc[-1])
                chg_pct = ((last_close - prev_close) / prev_close) * 100
                result[name] = {
                    "close": round(last_close, 2),
                    "prev_close": round(prev_close, 2),
                    "chg_pct": round(chg_pct, 2),
                }
            elif len(hist) == 1:
                result[name] = {
                    "close": round(float(hist["Close"].iloc[-1]), 2),
                    "prev_close": None,
                    "chg_pct": 0.0,
                }
        except Exception as e:
            logger.warning("Failed to fetch %s (%s): %s", name, ticker, e)
            result[name] = None

    return result


# ─────────────────────────────────────────────
# FII/DII DATA (NSE website - free)
# ─────────────────────────────────────────────
def fetch_fii_dii() -> dict:
    """Fetch FII/DII data from NSE."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.nseindia.com",
        }
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=headers, timeout=10)

        url = "https://www.nseindia.com/api/fiidiiTradeReact"
        resp = session.get(url, headers=headers, timeout=10)

        if resp.status_code == 200:
            data = resp.json()
            fii_net = 0.0
            dii_net = 0.0
            for item in data:
                category = item.get("category", "").upper()
                net = float(item.get("netValue", item.get("netTurnover", 0)) or 0)
                if "FII" in category or "FPI" in category:
                    fii_net += net
                elif "DII" in category:
                    dii_net += net
            return {"fii_net": round(fii_net, 2), "dii_net": round(dii_net, 2)}
    except Exception as e:
        logger.warning("FII/DII fetch failed: %s", e)

    return {"fii_net": None, "dii_net": None}


# ─────────────────────────────────────────────
# NEWS FETCHER
# ─────────────────────────────────────────────
def fetch_market_news(api_key: Optional[str] = None) -> list:
    """
    Fetch market news.
    Uses NewsAPI if key provided, else falls back to RSS feeds.
    Free tier: 100 requests/day on newsapi.org
    """
    news = []

    # Option 1: NewsAPI (free tier available)
    if api_key:
        try:
            url = "https://newsapi.org/v2/everything"
            params = {
                "q": "Nifty OR BSE OR NSE OR RBI OR Indian stock market",
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": 10,
                "apiKey": api_key,
            }
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                articles = resp.json().get("articles", [])
                for a in articles[:10]:
                    news.append({
                        "title": a.get("title", ""),
                        "source": a.get("source", {}).get("name", ""),
                        "published": a.get("publishedAt", ""),
                        "url": a.get("url", ""),
                        "sentiment": _quick_sentiment(a.get("title", "")),
                    })
                return news
        except Exception as e:
            logger.warning("NewsAPI failed: %s", e)

    # Option 2: Free RSS feeds (no API key needed)
    rss_feeds = [
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        "https://www.moneycontrol.com/rss/MCtopnews.xml",
        "https://feeds.feedburner.com/ndtvprofit-latest",
    ]

    try:
        import feedparser
        for feed_url in rss_feeds[:2]:
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:5]:
                    title = entry.get("title", "")
                    news.append({
                        "title": title,
                        "source": feed.feed.get("title", ""),
                        "published": entry.get("published", ""),
                        "url": entry.get("link", ""),
                        "sentiment": _quick_sentiment(title),
                    })
            except Exception as e:
                logger.warning("RSS feed failed %s: %s", feed_url, e)
    except ImportError:
        logger.warning("feedparser not installed. Run: pip install feedparser")

    return news[:10]


# ─────────────────────────────────────────────
# QUICK SENTIMENT (no ML needed)
# ─────────────────────────────────────────────
BULLISH_WORDS = [
    "surge", "rally", "gain", "rise", "high", "bullish", "positive",
    "growth", "record", "strong", "buy", "upgrade", "boost", "profit",
    "recovery", "optimism", "upbeat", "soar", "jump", "advance",
]

BEARISH_WORDS = [
    "fall", "drop", "decline", "crash", "low", "bearish", "negative",
    "loss", "weak", "sell", "downgrade", "concern", "risk", "fear",
    "recession", "inflation", "crisis", "plunge", "slump", "retreat",
]


def _quick_sentiment(text: str) -> float:
    """Simple keyword-based sentiment. Returns -1 to +1."""
    if not text:
        return 0.0
    text_lower = text.lower()
    bull_count = sum(1 for w in BULLISH_WORDS if w in text_lower)
    bear_count = sum(1 for w in BEARISH_WORDS if w in text_lower)
    total = bull_count + bear_count
    if total == 0:
        return 0.0
    return round((bull_count - bear_count) / total, 2)


def compute_news_sentiment(news_list: list) -> float:
    """Average sentiment from news list."""
    if not news_list:
        return 0.0
    sentiments = [n.get("sentiment", 0.0) for n in news_list]
    return round(sum(sentiments) / len(sentiments), 3)


# ─────────────────────────────────────────────
# GLOBAL SCORE CALCULATOR
# ─────────────────────────────────────────────
def compute_global_score(markets: dict, fii_dii: dict, news_sentiment: float) -> float:
    """
    Combine all global cues into single score.
    Returns -100 (very bearish) to +100 (very bullish).
    """
    score = 0.0
    weight_total = 0.0

    # US Markets (highest weight - 40%)
    us_markets = ["sp500", "nasdaq", "dow"]
    us_weights = [0.20, 0.12, 0.08]
    for mkt, w in zip(us_markets, us_weights):
        data = markets.get(mkt)
        if data and data.get("chg_pct") is not None:
            chg = data["chg_pct"]
            # Normalize: +2% = +100, -2% = -100
            normalized = max(-100, min(100, chg * 50))
            score += normalized * w
            weight_total += w

    # Asian Markets (20%)
    asian_markets = [("nikkei", 0.10), ("hangseng", 0.10)]
    for mkt, w in asian_markets:
        data = markets.get(mkt)
        if data and data.get("chg_pct") is not None:
            chg = data["chg_pct"]
            normalized = max(-100, min(100, chg * 50))
            score += normalized * w
            weight_total += w

    # VIX (inverse - fear index, 10%)
    vix_data = markets.get("vix_us")
    if vix_data and vix_data.get("chg_pct") is not None:
        vix_chg = vix_data["chg_pct"]
        # VIX up = market bearish, VIX down = market bullish
        normalized = max(-100, min(100, -vix_chg * 30))
        score += normalized * 0.10
        weight_total += 0.10

    # Crude Oil (5%) - high crude = bearish for India
    crude_data = markets.get("crude_oil")
    if crude_data and crude_data.get("chg_pct") is not None:
        crude_chg = crude_data["chg_pct"]
        normalized = max(-100, min(100, -crude_chg * 30))
        score += normalized * 0.05
        weight_total += 0.05

    # Gold (5%) - high gold = safe haven = slightly bearish
    gold_data = markets.get("gold")
    if gold_data and gold_data.get("chg_pct") is not None:
        gold_chg = gold_data["chg_pct"]
        normalized = max(-100, min(100, -gold_chg * 20))
        score += normalized * 0.05
        weight_total += 0.05

    # FII/DII (10%)
    fii_net = fii_dii.get("fii_net")
    if fii_net is not None:
        # FII > 2000 Cr = very bullish, < -2000 = very bearish
        normalized = max(-100, min(100, fii_net / 20))
        score += normalized * 0.10
        weight_total += 0.10

    # News Sentiment (10%)
    news_normalized = news_sentiment * 100
    score += news_normalized * 0.10
    weight_total += 0.10

    if weight_total > 0:
        final_score = score / weight_total
    else:
        final_score = 0.0

    return round(final_score, 2)


# ─────────────────────────────────────────────
# MAIN FETCH FUNCTION
# ─────────────────────────────────────────────
def fetch_all_global_cues(news_api_key: Optional[str] = None) -> dict:
    """Fetch all global cues and return structured data."""
    logger.info("Fetching global market cues...")

    markets = fetch_global_markets()
    fii_dii = fetch_fii_dii()
    news = fetch_market_news(news_api_key)
    news_sentiment = compute_news_sentiment(news)
    global_score = compute_global_score(markets, fii_dii, news_sentiment)

    return {
        "markets": markets,
        "fii_dii": fii_dii,
        "fii_net": fii_dii.get("fii_net"),
        "dii_net": fii_dii.get("dii_net"),
        "news": news,
        "news_sentiment": news_sentiment,
        "global_score": global_score,
        "fetched_at": datetime.now().isoformat(),
    }
