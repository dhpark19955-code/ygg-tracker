#!/usr/bin/env python3
"""
YGG Competitor Tracker — Data fetcher (Twelve Data edition)
===========================================================
Pulls quotes + statistics from Twelve Data (twelvedata.com), and recent
headlines from Google News RSS. Twelve Data free tier (800 requests/day,
8 req/min) handles all 3 international tickers reliably.

Requires env: TWELVEDATA_API_KEY  (set via GitHub Secrets)

Security notes
--------------
- API key is sent via Authorization header, never as a URL query parameter
- Any error / exception message is run through `_mask()` before being logged
  or stored in data.json, so the key cannot leak even if the API includes
  it in an error response
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import feedparser
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "data.json"

API_BASE = "https://api.twelvedata.com"
API_KEY = os.environ.get("TWELVEDATA_API_KEY", "").strip()
HTTP_TIMEOUT = 15
RETRY_MAX = 3
RETRY_BACKOFF_SEC = 2
RATE_LIMIT_SLEEP_SEC = 8  # free tier = 8 req/min, so 8s spacing is conservative

NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
NEWS_TIMEOUT_SEC = 15
NEWS_MAX = 4

# Twelve Data uses 'SYMBOL:EXCHANGE' for disambiguation
TICKERS: dict[str, dict[str, str]] = {
    "WEB": {
        "td_symbol": "WEB:ASX",
        "ccy": "AUD",
        "ccy_symbol": "A$",
        "name": "Web Travel Group",
        "exchange": "ASX",
        "news_q": '"Web Travel Group" OR WebBeds',
    },
    "TBO": {
        "td_symbol": "TBOTEK:NSE",
        "ccy": "INR",
        "ccy_symbol": "₹",
        "name": "TBO Tek",
        "exchange": "NSE",
        "news_q": '"TBO Tek"',
    },
    "HBX": {
        "td_symbol": "HBX:BME",
        "ccy": "EUR",
        "ccy_symbol": "€",
        "name": "HBX Group",
        "exchange": "BME",
        "news_q": '"HBX Group" OR Hotelbeds',
    },
}

FX_FALLBACK = {"AUD": 0.65, "INR": 0.012, "EUR": 1.08}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("fetcher")


# ---------------------------------------------------------------------------
# Security: masking
# ---------------------------------------------------------------------------

def _mask(s: Any) -> str:
    """Strip the API key from any string. Run on EVERYTHING before logging."""
    if s is None:
        return ""
    text = str(s)
    if API_KEY:
        text = text.replace(API_KEY, "***REDACTED***")
    # Belt-and-suspenders: also catch generic apikey=... patterns in URLs
    text = re.sub(r"(apikey|token)=[A-Za-z0-9_\-]+", r"\1=***", text, flags=re.IGNORECASE)
    return text


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

class TwelveDataError(Exception):
    """Raised when Twelve Data API call fails after retries."""


def _api_get(path: str, params: Optional[dict[str, Any]] = None) -> Any:
    """GET wrapper. Auth via header. Never lets the key into log/error text."""
    if not API_KEY:
        raise TwelveDataError("TWELVEDATA_API_KEY env var is empty")

    url = f"{API_BASE}{path}"
    headers = {"Authorization": f"apikey {API_KEY}"}

    last_status: Optional[int] = None
    for attempt in range(1, RETRY_MAX + 1):
        try:
            resp = requests.get(
                url, params=params or {}, headers=headers, timeout=HTTP_TIMEOUT
            )
            last_status = resp.status_code
        except requests.RequestException as e:
            # exception str() may contain URL — log type only, not message
            log.warning("API %s attempt %d/%d network err: %s",
                        path, attempt, RETRY_MAX, type(e).__name__)
            if attempt < RETRY_MAX:
                time.sleep(RETRY_BACKOFF_SEC * attempt)
            continue

        if resp.status_code == 429:
            wait = RETRY_BACKOFF_SEC * attempt
            log.warning("429 rate-limited on %s, sleeping %ds", path, wait)
            time.sleep(wait)
            continue

        if resp.status_code >= 400:
            body = _mask((resp.text or "")[:200])
            raise TwelveDataError(
                f"HTTP {resp.status_code} on {path}: {body}"
            )

        try:
            data = resp.json()
        except ValueError as e:
            raise TwelveDataError(f"Parse JSON failed on {path}: {_mask(e)}")

        # Twelve Data may return HTTP 200 with body status='error'
        if isinstance(data, dict) and data.get("status") == "error":
            code = data.get("code")
            msg = _mask(data.get("message", ""))
            raise TwelveDataError(f"API error code={code} on {path}: {msg}")

        return data

    raise TwelveDataError(
        f"GET {path} failed after {RETRY_MAX} attempts (last_status={last_status})"
    )


def _safe_call(fn, *args, **kwargs) -> tuple[Any, Optional[str]]:
    """Wrap a fetcher so a failure doesn't kill the whole run."""
    try:
        return fn(*args, **kwargs), None
    except Exception as e:
        err = _mask(e)
        log.warning("Sub-fetch failed: %s", err)
        return None, err


def _num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Endpoint wrappers
# ---------------------------------------------------------------------------

def fetch_quote(symbol: str) -> dict[str, Any]:
    """GET /quote → price, change, 52W high/low (often included)."""
    j = _api_get("/quote", {"symbol": symbol})
    fifty_two = (j or {}).get("fifty_two_week") or {}
    return {
        "price": _num(j.get("close")),
        "open": _num(j.get("open")),
        "high_day": _num(j.get("high")),
        "low_day": _num(j.get("low")),
        "prev_close": _num(j.get("previous_close")),
        "change": _num(j.get("change")),
        "change_pct_raw": _num(j.get("percent_change")),  # already percent
        "wk52_high": _num(fifty_two.get("high")),
        "wk52_low": _num(fifty_two.get("low")),
        "currency": j.get("currency"),
        "name": j.get("name"),
        "exchange": j.get("exchange"),
    }


def fetch_statistics(symbol: str) -> dict[str, Any]:
    """GET /statistics → market cap, EV, P/E, P/B, P/S, EV/EBITDA, margins."""
    j = _api_get("/statistics", {"symbol": symbol})
    stats = (j or {}).get("statistics") or {}

    val = stats.get("valuations_metrics") or {}
    fin = stats.get("financials") or {}
    income = fin.get("income_statement") or {}
    margins = fin.get("margins") or {}
    price_summary = stats.get("stock_price_summary") or {}

    return {
        "market_cap": _num(val.get("market_capitalization")),
        "enterprise_value": _num(val.get("enterprise_value")),
        "pe": _num(val.get("trailing_pe")) or _num(val.get("forward_pe")),
        "pb": _num(val.get("price_to_book_mrq")),
        "ps": _num(val.get("price_to_sales_ttm")),
        "ev_revenue": _num(val.get("enterprise_to_revenue")),
        "ev_ebitda": _num(val.get("enterprise_to_ebitda")),
        "revenue": _num(income.get("revenue_ttm")),
        "ebitda": _num(income.get("ebitda")),
        "ebitda_margin": _num(margins.get("ebitda_margin"))
                          or _num(fin.get("ebitda_margin")),
        "operating_margin": _num(margins.get("operating_margin")),
        "profit_margin": _num(margins.get("profit_margin")),
        "wk52_high_stat": _num(price_summary.get("fifty_two_week_high")),
        "wk52_low_stat": _num(price_summary.get("fifty_two_week_low")),
    }


def fetch_news(query: str) -> list[dict[str, str]]:
    """Pull recent headlines via Google News RSS — independent of any API key."""
    url = NEWS_RSS.format(q=urllib.parse.quote_plus(query))
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (ygg-tracker)"},
            timeout=NEWS_TIMEOUT_SEC,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("News RSS failed for %r: %s", query, type(e).__name__)
        return []

    try:
        feed = feedparser.parse(resp.content)
    except Exception as e:
        log.warning("News RSS parse failed: %s", type(e).__name__)
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
        source = ""
        if " - " in title:
            title, source = title.rsplit(" - ", 1)

        items.append({
            "date": published,
            "title": title.strip(),
            "source": source.strip(),
            "url": getattr(entry, "link", ""),
            "summary": "",
        })
    return items


# ---------------------------------------------------------------------------
# Per-ticker assembly
# ---------------------------------------------------------------------------

def fetch_one(meta: dict[str, str]) -> dict[str, Any]:
    """Pull quote + statistics + news for a single ticker."""
    symbol = meta["td_symbol"]
    errors: list[str] = []

    quote, err = _safe_call(fetch_quote, symbol)
    if err: errors.append(f"quote: {err}")
    time.sleep(RATE_LIMIT_SLEEP_SEC)

    stats, err = _safe_call(fetch_statistics, symbol)
    if err: errors.append(f"statistics: {err}")
    time.sleep(RATE_LIMIT_SLEEP_SEC)

    news, err = _safe_call(fetch_news, meta["news_q"])
    if err: errors.append(f"news: {err}")
    # No sleep after news (it's Google, not Twelve Data)

    quote = quote or {}
    stats = stats or {}
    news = news or []

    price = quote.get("price")
    change = quote.get("change")
    change_pct_raw = quote.get("change_pct_raw")
    direction = "up" if (change or 0) > 0 else "down" if (change or 0) < 0 else "flat"
    change_pct = (
        change_pct_raw / 100
        if isinstance(change_pct_raw, (int, float))
        else None
    )

    # Prefer quote's 52W, fall back to statistics' version
    wk52_high = quote.get("wk52_high") or stats.get("wk52_high_stat")
    wk52_low = quote.get("wk52_low") or stats.get("wk52_low_stat")

    ebitda_margin_raw = stats.get("ebitda_margin")
    # If margin already a fraction (-1 to 1), keep it; else divide by 100
    if isinstance(ebitda_margin_raw, (int, float)):
        ebitda_margin = (
            ebitda_margin_raw
            if abs(ebitda_margin_raw) <= 1
            else ebitda_margin_raw / 100
        )
    else:
        ebitda_margin = None

    return {
        "ok": price is not None,
        "symbol": symbol,
        "price": price,
        "prev_close": quote.get("prev_close"),
        "change": change,
        "change_pct": change_pct,
        "direction": direction,
        "market_cap": stats.get("market_cap"),
        "enterprise_value": stats.get("enterprise_value"),
        "wk52_high": wk52_high,
        "wk52_low": wk52_low,
        "revenue": stats.get("revenue"),
        "ebitda": stats.get("ebitda"),
        "ebitda_margin": ebitda_margin,
        "pe": stats.get("pe"),
        "pb": stats.get("pb"),
        "ps": stats.get("ps"),
        "ev_revenue": stats.get("ev_revenue"),
        "ev_ebitda": stats.get("ev_ebitda"),
        "target_mean": None,  # not in Twelve Data free tier
        "target_upside": None,
        "errors": errors,
        "_news": news,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_payload() -> dict[str, Any]:
    tickers_out: dict[str, Any] = {}

    for key, meta in TICKERS.items():
        log.info("Fetching %s (%s) …", key, meta["td_symbol"])
        q = fetch_one(meta)
        news = q.pop("_news", [])
        fx = FX_FALLBACK.get(meta["ccy"], 1.0)
        usd_mcap = (
            q["market_cap"] * fx
            if isinstance(q.get("market_cap"), (int, float))
            else None
        )

        tickers_out[key] = {
            "name": meta["name"],
            "exchange": meta["exchange"],
            "yf_symbol": meta["td_symbol"],  # HTML reads this field
            "ccy": meta["ccy"],
            "ccy_symbol": meta["ccy_symbol"],
            "quote": q,
            "usd_market_cap": usd_mcap,
            "news": news,
        }

        if q.get("ok"):
            log.info("  ✓ %s: price=%s, mcap=%s, news=%d, err_count=%d",
                     meta["td_symbol"], q.get("price"),
                     q.get("market_cap"), len(news), len(q.get("errors", [])))
        else:
            # Already-masked errors get logged here
            log.warning("  ✗ %s: price missing. errors=%s",
                        meta["td_symbol"], q.get("errors"))

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fx_to_usd": FX_FALLBACK,
        "tickers": tickers_out,
        "schema_version": 3,
        "data_source": "twelvedata.com + google news rss",
    }


def main() -> int:
    if not API_KEY:
        log.error("TWELVEDATA_API_KEY env var is missing. "
                  "Set it in Settings → Secrets and variables → Actions.")
        return 3

    log.info("Starting fetch (Twelve Data) …")
    try:
        payload = build_payload()
    except TwelveDataError as e:
        log.error("Build payload failed: %s", _mask(e))
        return 1
    except Exception as e:
        log.error("Unexpected error: %s", _mask(e), exc_info=False)
        return 1

    try:
        OUTPUT.write_text(
            json.dumps(payload, indent=2, default=str, ensure_ascii=False)
        )
        log.info("Wrote %s (%d bytes)", OUTPUT, OUTPUT.stat().st_size)
    except OSError as e:
        log.error("Write failed: %s", _mask(e))
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
