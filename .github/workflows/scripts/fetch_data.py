#!/usr/bin/env python3
"""
YGG Competitor Tracker — Data fetcher
=====================================
Fetches latest quote + valuation + news for WEB.AX / TBOTEK.NS / HBX.MC
and writes everything to data.json at the repo root.

Designed to run on GitHub Actions (no credentials required).
"""
from __future__ import annotations

import json
import logging
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import feedparser
import requests
import yfinance as yf

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "data.json"

# Ticker registry — (yfinance symbol, display ccy, search query for news)
TICKERS: dict[str, dict[str, str]] = {
    "WEB": {
        "yf": "WEB.AX",
        "ccy": "AUD",
        "ccy_symbol": "A$",
        "news_q": '"Web Travel Group" OR WebBeds',
        "name": "Web Travel Group",
        "exchange": "ASX",
    },
    "TBO": {
        "yf": "TBOTEK.NS",
        "ccy": "INR",
        "ccy_symbol": "₹",
        "news_q": '"TBO Tek"',
        "name": "TBO Tek",
        "exchange": "NSE",
    },
    "HBX": {
        "yf": "HBX.MC",
        "ccy": "EUR",
        "ccy_symbol": "€",
        "news_q": '"HBX Group" OR Hotelbeds',
        "name": "HBX Group",
        "exchange": "BME",
    },
}

# FX fallbacks — used only if yfinance can't provide a fresh rate
FX_FALLBACK = {"AUD": 0.65, "INR": 0.012, "EUR": 1.08}

# Google News RSS endpoint
NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
NEWS_MAX = 4
NEWS_TIMEOUT_SEC = 15

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("fetcher")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_get(d: dict[str, Any] | None, key: str, default: Any = None) -> Any:
    """Defensive .get() that survives None and missing keys."""
    if not isinstance(d, dict):
        return default
    val = d.get(key)
    return default if val is None else val


def fmt_money(n: float | int | None, ccy_symbol: str, scale_b: bool = False) -> str:
    """Format a numeric value with currency prefix."""
    if n is None or not isinstance(n, (int, float)):
        return "—"
    try:
        if scale_b:
            if abs(n) >= 1e9:
                return f"{ccy_symbol}{n/1e9:.2f}B"
            if abs(n) >= 1e6:
                return f"{ccy_symbol}{n/1e6:.0f}M"
        # Indian style for large INR amounts (Crores)
        if ccy_symbol == "₹" and abs(n) >= 1e7:
            return f"₹{n/1e7:,.0f} Cr"
        if abs(n) >= 1e3:
            return f"{ccy_symbol}{n:,.0f}"
        return f"{ccy_symbol}{n:,.2f}"
    except (TypeError, ValueError):
        return "—"


def fmt_pct(n: float | None, digits: int = 2) -> str:
    if n is None or not isinstance(n, (int, float)):
        return "—"
    return f"{n*100:+.{digits}f}%"


def get_fx_to_usd(ccy: str) -> float:
    """Try yfinance FX, fall back to the constant table."""
    if ccy == "USD":
        return 1.0
    pair = f"{ccy}USD=X"
    try:
        t = yf.Ticker(pair)
        # `fast_info` is the lightest path; .info is slow + sometimes blocked
        fi = getattr(t, "fast_info", None)
        if fi is not None:
            rate = getattr(fi, "last_price", None) or fi.get("last_price")  # type: ignore[union-attr]
            if rate and rate > 0:
                return float(rate)
    except Exception as e:
        log.warning("FX fetch failed for %s: %s — using fallback", ccy, e)
    return FX_FALLBACK.get(ccy, 1.0)


# ---------------------------------------------------------------------------
# Quote fetching
# ---------------------------------------------------------------------------

def fetch_quote(symbol: str) -> dict[str, Any]:
    """Pull the snapshot Yahoo Finance has for a single ticker.

    Returns a normalized dict. On total failure, returns a stub with `ok=False`.
    """
    try:
        t = yf.Ticker(symbol)

        # fast_info: price, market cap, 52w range (cheap)
        fi = getattr(t, "fast_info", None) or {}
        price = safe_get(fi, "last_price") or safe_get(fi, "lastPrice")
        prev = safe_get(fi, "previous_close") or safe_get(fi, "previousClose")
        mcap = safe_get(fi, "market_cap") or safe_get(fi, "marketCap")
        wk_high = safe_get(fi, "year_high") or safe_get(fi, "yearHigh")
        wk_low = safe_get(fi, "year_low") or safe_get(fi, "yearLow")

        # info: fundamentals (slow, sometimes 404s — wrap separately)
        info: dict[str, Any] = {}
        try:
            info = t.get_info() if hasattr(t, "get_info") else (t.info or {})
        except Exception as e:
            log.warning("%s .info unavailable: %s", symbol, e)

        revenue = safe_get(info, "totalRevenue")
        ebitda = safe_get(info, "ebitda")
        ev = safe_get(info, "enterpriseValue")
        pe = safe_get(info, "trailingPE")
        pb = safe_get(info, "priceToBook")
        ps = safe_get(info, "priceToSalesTrailing12Months")
        ev_rev = safe_get(info, "enterpriseToRevenue")
        ev_ebitda = safe_get(info, "enterpriseToEbitda")
        target = safe_get(info, "targetMeanPrice")
        ebitda_margin = (
            (ebitda / revenue) if (isinstance(ebitda, (int, float)) and isinstance(revenue, (int, float)) and revenue) else None
        )

        change = (price - prev) if (isinstance(price, (int, float)) and isinstance(prev, (int, float))) else None
        change_pct = (change / prev) if (change is not None and prev) else None
        direction = "up" if (change or 0) > 0 else "down" if (change or 0) < 0 else "flat"

        target_upside = (
            (target / price - 1) if (isinstance(target, (int, float)) and isinstance(price, (int, float)) and price)
            else None
        )

        return {
            "ok": True,
            "symbol": symbol,
            "price": price,
            "prev_close": prev,
            "change": change,
            "change_pct": change_pct,
            "direction": direction,
            "market_cap": mcap,
            "enterprise_value": ev,
            "wk52_high": wk_high,
            "wk52_low": wk_low,
            "revenue": revenue,
            "ebitda": ebitda,
            "ebitda_margin": ebitda_margin,
            "pe": pe,
            "pb": pb,
            "ps": ps,
            "ev_revenue": ev_rev,
            "ev_ebitda": ev_ebitda,
            "target_mean": target,
            "target_upside": target_upside,
        }
    except Exception as e:
        log.error("Quote fetch failed for %s: %s", symbol, e, exc_info=True)
        return {"ok": False, "symbol": symbol, "error": str(e)}


# ---------------------------------------------------------------------------
# News fetching
# ---------------------------------------------------------------------------

def fetch_news(query: str) -> list[dict[str, str]]:
    """Pull recent headlines from Google News RSS. Returns up to NEWS_MAX items."""
    url = NEWS_RSS.format(q=urllib.parse.quote_plus(query))
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (ygg-tracker)"},
            timeout=NEWS_TIMEOUT_SEC,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("News RSS request failed for %r: %s", query, e)
        return []

    try:
        feed = feedparser.parse(resp.content)
    except Exception as e:
        log.warning("News RSS parse failed for %r: %s", query, e)
        return []

    items: list[dict[str, str]] = []
    for entry in (feed.entries or [])[:NEWS_MAX]:
        published = ""
        if getattr(entry, "published_parsed", None):
            try:
                published = datetime(*entry.published_parsed[:6]).strftime("%Y.%m.%d")
            except (TypeError, ValueError):
                published = ""

        title = (getattr(entry, "title", "") or "").strip()
        # Google News titles end with " - Publisher"; strip the publisher
        source = ""
        if " - " in title:
            title, source = title.rsplit(" - ", 1)

        items.append({
            "date": published,
            "title": title.strip(),
            "source": source.strip(),
            "url": getattr(entry, "link", ""),
        })
    return items


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_payload() -> dict[str, Any]:
    fx = {ccy: get_fx_to_usd(ccy) for ccy in {meta["ccy"] for meta in TICKERS.values()}}
    log.info("FX rates (to USD): %s", fx)

    tickers_out: dict[str, Any] = {}

    for key, meta in TICKERS.items():
        log.info("Fetching %s (%s) …", key, meta["yf"])
        q = fetch_quote(meta["yf"])
        news = fetch_news(meta["news_q"])

        usd_mcap = (
            q["market_cap"] * fx.get(meta["ccy"], 1.0)
            if q.get("ok") and isinstance(q.get("market_cap"), (int, float))
            else None
        )

        tickers_out[key] = {
            "name": meta["name"],
            "exchange": meta["exchange"],
            "yf_symbol": meta["yf"],
            "ccy": meta["ccy"],
            "ccy_symbol": meta["ccy_symbol"],
            "quote": q,
            "usd_market_cap": usd_mcap,
            "news": news,
        }

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fx_to_usd": fx,
        "tickers": tickers_out,
        "schema_version": 1,
    }


def main() -> int:
    log.info("Starting fetch …")
    try:
        payload = build_payload()
    except Exception as e:
        log.error("Build payload failed: %s", e, exc_info=True)
        return 1

    try:
        OUTPUT.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
        log.info("Wrote %s (%d bytes)", OUTPUT, OUTPUT.stat().st_size)
    except OSError as e:
        log.error("Write failed: %s", e)
        return 2

    # Quick visibility in CI logs
    for k, v in payload["tickers"].items():
        q = v["quote"]
        if q.get("ok"):
            log.info("  %s %s: %s %s  (mcap=%s, news=%d)",
                     k, v["yf_symbol"],
                     v["ccy_symbol"], q.get("price"),
                     q.get("market_cap"), len(v["news"]))
        else:
            log.warning("  %s %s: FAILED — %s", k, v["yf_symbol"], q.get("error"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
