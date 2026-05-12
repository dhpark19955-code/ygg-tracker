#!/usr/bin/env python3
"""
YGG Competitor Tracker — Data fetcher (Finnhub edition)
=======================================================
Pulls quotes / company profile / fundamentals / news for the 3 peers via
the Finnhub.io REST API. Free tier (60 calls/min) is more than enough.

Requires env: FINNHUB_API_KEY  (set via GitHub Secrets)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "data.json"

API_BASE = "https://finnhub.io/api/v1"
API_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()
HTTP_TIMEOUT = 15
RETRY_MAX = 3
RETRY_BACKOFF_SEC = 2
RATE_LIMIT_SLEEP_SEC = 1.2  # stay under free tier 60/min comfortably
NEWS_LOOKBACK_DAYS = 30
NEWS_MAX = 4

# Ticker registry. Finnhub uses Yahoo-style exchange suffixes.
TICKERS: dict[str, dict[str, str]] = {
    "WEB": {
        "symbol": "WEB.AX",
        "ccy": "AUD",
        "ccy_symbol": "A$",
        "name": "Web Travel Group",
        "exchange": "ASX",
    },
    "TBO": {
        "symbol": "TBOTEK.NS",
        "ccy": "INR",
        "ccy_symbol": "₹",
        "name": "TBO Tek",
        "exchange": "NSE",
    },
    "HBX": {
        "symbol": "HBX.MC",
        "ccy": "EUR",
        "ccy_symbol": "€",
        "name": "HBX Group",
        "exchange": "BME",
    },
}

# Approx FX (USD per 1 unit of local currency). Used for USD-normalized
# market cap display only — a few percent off is acceptable.
FX_FALLBACK = {"AUD": 0.65, "INR": 0.012, "EUR": 1.08}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("fetcher")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

class FinnhubError(Exception):
    """Raised when the Finnhub API call fails after retries."""


def _api_get(path: str, params: dict[str, Any]) -> Any:
    """GET wrapper with retry/backoff. Returns parsed JSON or raises."""
    if not API_KEY:
        raise FinnhubError("FINNHUB_API_KEY env var is empty")

    url = f"{API_BASE}{path}"
    full_params = dict(params)
    full_params["token"] = API_KEY

    last_err: Exception | None = None
    for attempt in range(1, RETRY_MAX + 1):
        try:
            resp = requests.get(url, params=full_params, timeout=HTTP_TIMEOUT)
            if resp.status_code == 429:
                wait = RETRY_BACKOFF_SEC * attempt
                log.warning("429 rate-limited on %s, sleeping %ds", path, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            last_err = e
            log.warning("API %s attempt %d/%d failed: %s",
                        path, attempt, RETRY_MAX, e)
            if attempt < RETRY_MAX:
                time.sleep(RETRY_BACKOFF_SEC * attempt)

    raise FinnhubError(f"GET {path} failed after {RETRY_MAX} attempts: {last_err}")


def _safe_call(fn, *args, **kwargs) -> tuple[Any, str | None]:
    """Run a fetch fn, return (result, error_msg). Never raises."""
    try:
        return fn(*args, **kwargs), None
    except Exception as e:
        log.warning("Sub-fetch failed: %s", e)
        return None, str(e)


def _num(v: Any) -> float | None:
    """Coerce to float; None for missing/invalid."""
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
    """GET /quote → current price + day's change."""
    j = _api_get("/quote", {"symbol": symbol})
    return {
        "price": _num(j.get("c")),
        "change": _num(j.get("d")),
        "change_pct_raw": _num(j.get("dp")),
        "prev_close": _num(j.get("pc")),
        "high": _num(j.get("h")),
        "low": _num(j.get("l")),
        "open": _num(j.get("o")),
    }


def fetch_profile(symbol: str) -> dict[str, Any]:
    """GET /stock/profile2 → company name, market cap (millions of local ccy)."""
    j = _api_get("/stock/profile2", {"symbol": symbol})
    if not isinstance(j, dict) or not j:
        return {}
    mcap_m = _num(j.get("marketCapitalization"))
    shares_m = _num(j.get("shareOutstanding"))
    return {
        "country": j.get("country"),
        "currency": j.get("currency"),
        "exchange": j.get("exchange"),
        "name": j.get("name"),
        "ipo": j.get("ipo"),
        "weburl": j.get("weburl"),
        "market_cap": mcap_m * 1e6 if mcap_m is not None else None,
        "shares_outstanding": shares_m * 1e6 if shares_m is not None else None,
    }


def fetch_metrics(symbol: str) -> dict[str, Any]:
    """GET /stock/metric → fundamentals: P/E, P/B, P/S, EV/EBITDA, 52W H/L."""
    j = _api_get("/stock/metric", {"symbol": symbol, "metric": "all"})
    m = (j or {}).get("metric") or {}
    if not isinstance(m, dict):
        return {}

    def pick(*keys: str) -> float | None:
        for k in keys:
            v = _num(m.get(k))
            if v is not None:
                return v
        return None

    return {
        "wk52_high": pick("52WeekHigh"),
        "wk52_low": pick("52WeekLow"),
        "pe": pick("peTTM", "peAnnual", "peExclExtraTTM",
                   "peNormalizedAnnual", "peExclExtraAnnual"),
        "pb": pick("pbAnnual", "pbQuarterly"),
        "ps": pick("psTTM", "psAnnual"),
        "ev_ebitda": pick("evToEbitdaAnnual", "currentEv/freeCashFlowTTM"),
        "ev_revenue": pick("evToRevenueAnnual"),
        "ebitda_margin": pick("ebitdaMargin5Y", "ebitdaMarginAnnual",
                              "ebitdaMarginTTM"),
        "operating_margin": pick("operatingMarginTTM", "operatingMarginAnnual"),
        "roe": pick("roeTTM", "roeRfy"),
        "revenue_growth": pick("revenueGrowthTTMYoy", "revenueGrowth3Y"),
        "beta": pick("beta"),
    }


def fetch_news(symbol: str) -> list[dict[str, str]]:
    """GET /company-news → recent headlines."""
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=NEWS_LOOKBACK_DAYS)
    items_raw = _api_get("/company-news", {
        "symbol": symbol,
        "from": start.isoformat(),
        "to": today.isoformat(),
    })
    if not isinstance(items_raw, list):
        return []

    items_raw.sort(key=lambda x: x.get("datetime", 0), reverse=True)
    out: list[dict[str, str]] = []
    for it in items_raw[:NEWS_MAX]:
        try:
            ts = int(it.get("datetime") or 0)
            date_str = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y.%m.%d") if ts else ""
        except (TypeError, ValueError, OSError):
            date_str = ""
        out.append({
            "date": date_str,
            "title": (it.get("headline") or "").strip(),
            "source": (it.get("source") or "").strip(),
            "url": it.get("url") or "",
            "summary": (it.get("summary") or "")[:280],
        })
    return out


# ---------------------------------------------------------------------------
# Per-ticker assembly
# ---------------------------------------------------------------------------

def fetch_one(meta: dict[str, str]) -> dict[str, Any]:
    """Pull quote + profile + metrics + news for a single ticker."""
    symbol = meta["symbol"]
    errors: list[str] = []

    quote, err = _safe_call(fetch_quote, symbol)
    if err: errors.append(f"quote: {err}")
    time.sleep(RATE_LIMIT_SLEEP_SEC)

    profile, err = _safe_call(fetch_profile, symbol)
    if err: errors.append(f"profile: {err}")
    time.sleep(RATE_LIMIT_SLEEP_SEC)

    metrics, err = _safe_call(fetch_metrics, symbol)
    if err: errors.append(f"metrics: {err}")
    time.sleep(RATE_LIMIT_SLEEP_SEC)

    news, err = _safe_call(fetch_news, symbol)
    if err: errors.append(f"news: {err}")
    time.sleep(RATE_LIMIT_SLEEP_SEC)

    quote = quote or {}
    profile = profile or {}
    metrics = metrics or {}
    news = news or []

    price = quote.get("price")
    change = quote.get("change")
    change_pct_raw = quote.get("change_pct_raw")  # already in percent
    direction = "up" if (change or 0) > 0 else "down" if (change or 0) < 0 else "flat"

    # Normalize percent → fraction for HTML formatter
    change_pct = (
        change_pct_raw / 100
        if isinstance(change_pct_raw, (int, float))
        else None
    )

    ebitda_margin_raw = metrics.get("ebitda_margin")
    ebitda_margin = (
        ebitda_margin_raw / 100
        if isinstance(ebitda_margin_raw, (int, float))
        else None
    )

    return {
        "ok": price is not None,
        "symbol": symbol,
        "price": price,
        "prev_close": quote.get("prev_close"),
        "change": change,
        "change_pct": change_pct,
        "direction": direction,
        "market_cap": profile.get("market_cap"),
        "enterprise_value": None,  # Free tier doesn't expose EV directly
        "wk52_high": metrics.get("wk52_high"),
        "wk52_low": metrics.get("wk52_low"),
        "revenue": None,
        "ebitda": None,
        "ebitda_margin": ebitda_margin,
        "pe": metrics.get("pe"),
        "pb": metrics.get("pb"),
        "ps": metrics.get("ps"),
        "ev_revenue": metrics.get("ev_revenue"),
        "ev_ebitda": metrics.get("ev_ebitda"),
        "target_mean": None,
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
        log.info("Fetching %s (%s) …", key, meta["symbol"])
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
            "yf_symbol": meta["symbol"],  # keep this field name for HTML compatibility
            "ccy": meta["ccy"],
            "ccy_symbol": meta["ccy_symbol"],
            "quote": q,
            "usd_market_cap": usd_mcap,
            "news": news,
        }

        if q.get("ok"):
            log.info("  ✓ %s: price=%s, mcap=%s, news=%d, errors=%d",
                     meta["symbol"], q.get("price"), q.get("market_cap"),
                     len(news), len(q.get("errors", [])))
        else:
            log.warning("  ✗ %s: price missing. errors=%s",
                        meta["symbol"], q.get("errors"))

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fx_to_usd": FX_FALLBACK,
        "tickers": tickers_out,
        "schema_version": 2,
        "data_source": "finnhub.io",
    }


def main() -> int:
    if not API_KEY:
        log.error("FINNHUB_API_KEY env var is missing. "
                  "Set it in Settings → Secrets and variables → Actions.")
        return 3

    log.info("Starting fetch (Finnhub) …")
    try:
        payload = build_payload()
    except FinnhubError as e:
        log.error("Build payload failed: %s", e)
        return 1
    except Exception as e:
        log.error("Unexpected error: %s", e, exc_info=True)
        return 1

    try:
        OUTPUT.write_text(
            json.dumps(payload, indent=2, default=str, ensure_ascii=False)
        )
        log.info("Wrote %s (%d bytes)", OUTPUT, OUTPUT.stat().st_size)
    except OSError as e:
        log.error("Write failed: %s", e)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
