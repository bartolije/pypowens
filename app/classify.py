"""Investment classification enrichment via OpenFIGI + Yahoo Finance.

Best-effort: every external call is wrapped in try/except so the page renders
even when both APIs are down. Results are cached in SQLite for 30 days.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta
from typing import Any

import httpx

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ constants

_CACHE_DAYS = 30

ISIN_COUNTRY: dict[str, str] = {
    "FR": "France",
    "US": "États-Unis",
    "DE": "Allemagne",
    "NL": "Pays-Bas",
    "GB": "Royaume-Uni",
    "IE": "Irlande",
    "LU": "Luxembourg",
    "CH": "Suisse",
    "JP": "Japon",
    "CA": "Canada",
    "AU": "Australie",
    "IT": "Italie",
    "ES": "Espagne",
    "BE": "Belgique",
    "SE": "Suède",
    "DK": "Danemark",
    "NO": "Norvège",
    "FI": "Finlande",
    "TW": "Taïwan",
    "KR": "Corée du Sud",
    "HK": "Hong Kong",
    "SG": "Singapour",
    "XX": "Inconnu",
}

# Yahoo exchange suffixes keyed by OpenFIGI ``exchCode``.
_EXCHANGE_SUFFIX: dict[str, str] = {
    "PA": ".PA",
    "AS": ".AS",
    "LN": ".L",
    "SX": ".SW",
    "FH": ".HE",
    "SM": ".MC",
    "IM": ".MI",
    "BB": ".BR",
    "SS": ".ST",
    "CO": ".CO",
    "OL": ".OL",
}

# US exchanges: no suffix needed.
_US_EXCHANGES = frozenset({"US", "UN", "UQ", "UA", "UW", ""})


# -------------------------------------------------------------- ISIN fallback


def country_from_isin(isin: str) -> str:
    """Derive a human-readable country from the ISIN prefix (2-letter ISO)."""
    return ISIN_COUNTRY.get(isin[:2].upper(), isin[:2].upper())


# ---------------------------------------------------------------- OpenFIGI API


async def _fetch_openfigi(isins: list[str], api_key: str) -> dict[str, dict[str, Any]]:
    """Batch lookup ISINs via OpenFIGI (up to 100 per request)."""
    headers = {"X-OPENFIGI-APIKEY": api_key, "Content-Type": "application/json"}
    body = [{"idType": "ID_ISIN", "idValue": isin} for isin in isins]
    out: dict[str, dict[str, Any]] = {}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.openfigi.com/v3/mapping",
                json=body,
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            results = resp.json()
        for isin, result in zip(isins, results, strict=True):
            if "data" in result and result["data"]:
                d = result["data"][0]
                out[isin] = {
                    "name": d.get("name"),
                    "ticker": d.get("ticker"),
                    "exchCode": d.get("exchCode"),
                    "marketSector": d.get("marketSector"),
                    "securityType": d.get("securityType2") or d.get("securityType"),
                }
    except Exception:
        log.warning("OpenFIGI lookup failed for %d ISINs", len(isins), exc_info=True)
    return out


# ------------------------------------------------------------- Yahoo Finance


def _fetch_yahoo_sync(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch sector and country from Yahoo Finance via yfinance (sync)."""
    out: dict[str, dict[str, Any]] = {}
    try:
        import yfinance as yf  # noqa: PLC0415

        for ticker in tickers:
            try:
                t = yf.Ticker(ticker)
                info = t.info or {}
                if info.get("sector"):
                    out[ticker] = {
                        "sector": info.get("sector"),
                        "industry": info.get("industry"),
                        "country": info.get("country"),
                        "shortName": info.get("shortName"),
                    }
            except Exception:
                log.debug("yfinance failed for %s", ticker, exc_info=True)
    except ImportError:
        log.warning("yfinance not installed — skipping sector enrichment")
    except Exception:
        log.warning("Yahoo Finance session failed", exc_info=True)
    return out


# ------------------------------------------------------------ SQLite cache


def _load_cache(
    conn: sqlite3.Connection, isins: list[str]
) -> dict[str, dict[str, Any]]:
    """Load cached classifications, skipping entries older than ``_CACHE_DAYS``."""
    cutoff = (date.today() - timedelta(days=_CACHE_DAYS)).isoformat()
    out: dict[str, dict[str, Any]] = {}
    for isin in isins:
        row = conn.execute(
            "SELECT sector, country, security_type, name, ticker, updated"
            " FROM investment_classification WHERE isin = ?",
            (isin,),
        ).fetchone()
        if row and row["updated"] >= cutoff:
            out[isin] = {
                "sector": row["sector"],
                "country": row["country"],
                "security_type": row["security_type"],
                "name": row["name"],
                "ticker": row["ticker"],
            }
    return out


def _save_cache(
    conn: sqlite3.Connection, isin: str, data: dict[str, Any]
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO investment_classification"
        " (isin, sector, country, security_type, name, ticker, updated)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            isin,
            data.get("sector"),
            data.get("country"),
            data.get("security_type"),
            data.get("name"),
            data.get("ticker"),
            date.today().isoformat(),
        ),
    )
    conn.commit()


# --------------------------------------------------------- main entry point


def _yahoo_ticker(ticker: str, exch_code: str) -> str:
    """Build a Yahoo Finance ticker symbol from OpenFIGI ticker + exchange."""
    if exch_code in _US_EXCHANGES:
        return ticker
    suffix = _EXCHANGE_SUFFIX.get(exch_code)
    if suffix:
        return f"{ticker}{suffix}"
    return ticker


async def classify_investments(
    isins: list[str],
    conn: sqlite3.Connection,
    api_key: str | None,
) -> dict[str, dict[str, Any]]:
    """Return ``{isin: {sector, country, security_type, name, ticker}}`` for each ISIN.

    Cached results are returned immediately. Cache misses are enriched via
    OpenFIGI (instrument metadata) then Yahoo Finance (sector/country), and
    persisted for 30 days.
    """
    if not isins:
        return {}

    cached = _load_cache(conn, isins)
    missing = [isin for isin in isins if isin not in cached]

    if not missing or not api_key:
        return cached

    # 1. OpenFIGI for instrument metadata.
    figi_data = await _fetch_openfigi(missing, api_key)

    # 2. Yahoo Finance for sector/country (only for entries with a ticker).
    tickers_map: dict[str, str] = {}  # yahoo_ticker -> isin
    for isin in missing:
        figi = figi_data.get(isin)
        if figi and figi.get("ticker"):
            yt = _yahoo_ticker(figi["ticker"], figi.get("exchCode") or "")
            tickers_map[yt] = isin

    yahoo_data: dict[str, dict[str, Any]] = {}
    if tickers_map:
        yahoo_data = _fetch_yahoo_sync(list(tickers_map.keys()))

    # 3. Merge results.
    results = dict(cached)
    for isin in missing:
        figi = figi_data.get(isin, {})

        # Find yahoo data via ticker mapping.
        yahoo: dict[str, Any] = {}
        if figi.get("ticker"):
            for yt, mapped_isin in tickers_map.items():
                if mapped_isin == isin and yt in yahoo_data:
                    yahoo = yahoo_data[yt]
                    break

        sector = yahoo.get("sector") or figi.get("marketSector") or "Autre"
        country = yahoo.get("country") or country_from_isin(isin)

        entry = {
            "sector": sector,
            "country": country,
            "security_type": figi.get("securityType") or "Inconnu",
            "name": figi.get("name") or "",
            "ticker": figi.get("ticker") or "",
        }
        results[isin] = entry
        _save_cache(conn, isin, entry)

    return results
