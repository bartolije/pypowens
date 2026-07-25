"""Cached data loaders (avoid re-pulling the full history on every request).

One cache entry per resource, never one per requested window: the transaction
history is fetched once for the *widest* window asked for so far, and narrower
windows are served by filtering in memory. Requesting 8 months then 24 months
used to trigger two full downloads and keep both lists alive forever.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

from pypowens import AccountsList, Connection, Investment, PowensAPIError, PowensClient, Transaction

from .enrich import internal_transfer_ids

# Accounts where day-to-day spending actually happens. Investment (market/pea/per/
# lifeinsurance), savings (livret*/csl/ldds...) and loan accounts are excluded from
# spending analysis so that securities purchases and savings moves don't inflate it.
SPENDING_ACCOUNT_TYPES = frozenset({"checking", "card"})

_TXN_KEY = "transactions"
_INTERNAL_KEY = "internal"

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


def _min_date(months: int) -> date:
    return date.today() - timedelta(days=int(months * 30.5))


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


async def load_investments(client: PowensClient, *, ttl: float = 300) -> list[Investment]:
    """Security lines held in investment accounts.

    Returns an empty list when the endpoint is unavailable on the app: the feature
    is a bonus on top of balances and must never break the recap page.
    """
    cached = _get("investments", ttl)
    if cached is not None:
        return cached  # type: ignore[return-value]
    try:
        data = await client.list_investments()
    except PowensAPIError:
        data = []
    _set("investments", data)
    return data


async def _load_history(
    client: PowensClient, *, months: int, ttl: float
) -> tuple[int, list[Transaction]]:
    """Return ``(covered_months, transactions)`` for the single cached history.

    Fetches only when the cache is cold or covers a narrower window than asked for.
    """
    cached: tuple[int, list[Transaction]] | None = _get(_TXN_KEY, ttl)  # type: ignore[assignment]
    if cached is not None and cached[0] >= months:
        return cached

    min_date = _min_date(months)
    # ``coming`` transactions are forecast operations not yet debited: keeping them
    # would inflate the current month and create phantom recurring occurrences.
    txns = [
        t
        async for t in client.iter_transactions(min_date=min_date.isoformat(), limit=1000)
        if not t.coming
    ]
    entry = (months, txns)
    _set(_TXN_KEY, entry)
    # A wider history invalidates the internal-transfer set computed on the old one.
    _cache.pop(_INTERNAL_KEY, None)
    return entry


async def load_transactions(
    client: PowensClient, *, months: int = 24, ttl: float = 300
) -> list[Transaction]:
    """Transactions over the last ``months``, served from a single cached history."""
    covered, txns = await _load_history(client, months=months, ttl=ttl)
    if covered == months:
        return txns
    floor = _min_date(months)
    return [t for t in txns if t.date is None or t.date >= floor]


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

    Computed once on the whole cached history — the result is a superset that stays
    valid for any narrower window, since ids are only ever used for exclusion.
    """
    _, all_txns = await _load_history(client, months=months, ttl=ttl)
    cached = _get(_INTERNAL_KEY, ttl)
    if cached is not None:
        return cached  # type: ignore[return-value]
    ids = internal_transfer_ids(all_txns)
    _set(_INTERNAL_KEY, ids)
    return ids
