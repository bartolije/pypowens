"""Cached data loaders (avoid re-pulling the full history on every request)."""

from __future__ import annotations

import time
from datetime import date, timedelta

from pypowens import AccountsList, Connection, PowensClient, Transaction

_cache: dict[str, tuple[float, object]] = {}


def _get(key: str, ttl: float):
    hit = _cache.get(key)
    if hit and (time.monotonic() - hit[0]) < ttl:
        return hit[1]
    return None


def _set(key: str, value: object) -> None:
    _cache[key] = (time.monotonic(), value)


def clear_cache() -> None:
    _cache.clear()


async def load_accounts(client: PowensClient, *, ttl: float = 120) -> AccountsList:
    cached = _get("accounts", ttl)
    if cached is not None:
        return cached  # type: ignore[return-value]
    data = await client.list_accounts(include_disabled=False)
    _set("accounts", data)
    return data


async def load_connections(client: PowensClient, *, ttl: float = 120) -> list[Connection]:
    cached = _get("connections", ttl)
    if cached is not None:
        return cached  # type: ignore[return-value]
    data = await client.list_connections(expand="connector,accounts")
    _set("connections", data)
    return data


async def load_transactions(
    client: PowensClient, *, months: int = 24, ttl: float = 300
) -> list[Transaction]:
    key = f"transactions:{months}"
    cached = _get(key, ttl)
    if cached is not None:
        return cached  # type: ignore[return-value]
    min_date = (date.today() - timedelta(days=int(months * 30.5))).isoformat()
    txns = [t async for t in client.iter_transactions(min_date=min_date, limit=1000)]
    _set(key, txns)
    return txns
