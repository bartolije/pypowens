"""Cached data loaders (avoid re-pulling the full history on every request)."""

from __future__ import annotations

import time
from datetime import date, timedelta

from pypowens import AccountsList, Connection, PowensClient, Transaction

from .enrich import internal_transfer_ids

# Accounts where day-to-day spending actually happens. Investment (market/pea/per/
# lifeinsurance), savings (livret*/csl/ldds...) and loan accounts are excluded from
# spending analysis so that securities purchases and savings moves don't inflate it.
SPENDING_ACCOUNT_TYPES = frozenset({"checking", "card"})

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


async def load_spending_transactions(
    client: PowensClient,
    *,
    months: int = 24,
    include_investment: bool = False,
    ttl: float = 300,
) -> list[Transaction]:
    """Transactions restricted to real spending accounts (checking/card).

    Set ``include_investment=True`` to keep every account (investment, savings, …).
    """
    txns = await load_transactions(client, months=months, ttl=ttl)
    if include_investment:
        return txns
    accounts = await load_accounts(client, ttl=ttl)
    spending_ids = {a.id for a in accounts.accounts if a.type in SPENDING_ACCOUNT_TYPES}
    return [t for t in txns if t.id_account in spending_ids]


async def load_internal_ids(
    client: PowensClient, *, months: int = 24, ttl: float = 300
) -> set[int]:
    """Ids of internal transfers, computed over ALL accounts.

    Must run on the full account set: a transfer from the checking account to a
    savings/investment account only has its mirror leg on the other account, so
    restricting to spending accounts first would miss it.
    """
    key = f"internal:{months}"
    cached = _get(key, ttl)
    if cached is not None:
        return cached  # type: ignore[return-value]
    all_txns = await load_transactions(client, months=months, ttl=ttl)
    ids = internal_transfer_ids(all_txns)
    _set(key, ids)
    return ids
