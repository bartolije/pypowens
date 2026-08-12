"""Cached data loaders (avoid re-pulling the full history on every request).

One cache entry per resource, never one per requested window: the transaction
history is fetched once for the *widest* window asked for so far, and narrower
windows are served by filtering in memory. Requesting 8 months then 24 months
used to trigger two full downloads and keep both lists alive forever.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import date, timedelta

from pypowens import (
    Account,
    AccountsList,
    Connection,
    Investment,
    PowensAPIError,
    PowensClient,
    Transaction,
)

from . import store
from .enrich import internal_transfer_ids

# Accounts where day-to-day spending actually happens. Investment (market/pea/per/
# lifeinsurance), savings (livret*/csl/ldds...) and loan accounts are excluded from
# spending analysis so that securities purchases and savings moves don't inflate it.
SPENDING_ACCOUNT_TYPES = frozenset({"checking", "card"})

# Plafond des fenêtres dérivées d'un paramètre d'URL : un ?date_from=1900-01-01
# demandait ~1500 mois, soit le téléchargement de TOUT l'historique Powens, mis
# en cache définitivement. 10 ans couvrent tout usage réel.
MAX_WINDOW_MONTHS = 120

_TXN_KEY = "transactions"
_INTERNAL_KEY = "internal"

_cache: dict[str, tuple[float, object]] = {}

# Un verrou par clé : deux requêtes simultanées en cache froid déclenchaient
# deux téléchargements complets de l'historique (thundering herd). Le second
# appelant attend le premier puis lit le cache.
_locks: dict[str, asyncio.Lock] = {}


def _lock(key: str) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


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


async def load_accounts(
    client: PowensClient, *, ttl: float = 120, conn: sqlite3.Connection | None = None
) -> AccountsList:
    """Comptes Powens, plus les comptes importés depuis un relevé quand ``conn`` est fourni.

    Les données importées sont ajoutées **après** le cache : une lecture SQLite locale
    coûte moins qu'un aller-retour réseau, et un nouvel import doit apparaître tout de
    suite au lieu d'attendre l'expiration du TTL.

    Un compte importé rattaché à un compte Powens n'apparaît pas : il n'est plus un compte,
    seulement l'historique ancien de celui de Powens.
    """
    cached = _get("accounts", ttl)
    if cached is None:
        async with _lock("accounts"):
            cached = _get("accounts", ttl)
            if cached is None:
                cached = await client.list_accounts(include_disabled=False)
                _set("accounts", cached)
    data: AccountsList = cached  # type: ignore[assignment]
    if conn is None:
        return data
    extra = [Account.from_api(raw) for raw in store.imported_accounts(conn)]
    if not extra:
        return data
    return AccountsList(
        accounts=[*data.accounts, *extra],
        balances=data.balances,
        coming_balances=data.coming_balances,
    )


async def load_connections(client: PowensClient, *, ttl: float = 120) -> list[Connection]:
    cached = _get("connections", ttl)
    if cached is not None:
        return cached  # type: ignore[return-value]
    async with _lock("connections"):
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
    async with _lock("investments"):
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

    async with _lock(_TXN_KEY):
        # Re-vérifier sous le verrou : une requête concurrente a pu remplir le cache.
        cached = _get(_TXN_KEY, ttl)
        if cached is not None and cached[0] >= months:
            return cached
        # Ne jamais rétrécir la fenêtre : après expiration du TTL, une demande
        # étroite (1 mois) écraserait l'entrée large (36 mois) et le prochain
        # /analyse retéléchargerait tout l'historique.
        stale = _cache.get(_TXN_KEY)
        if stale is not None:
            previous_months = stale[1][0]  # type: ignore[index]
            months = max(months, previous_months)

        min_date = _min_date(months)
        # ``coming`` transactions are forecast operations not yet debited: keeping
        # them would inflate the current month and create phantom recurring
        # occurrences.
        txns = [
            t
            async for t in client.iter_transactions(min_date=min_date.isoformat(), limit=1000)
            if not t.coming
        ]
        entry = (months, txns)
        _set(_TXN_KEY, entry)
        # A wider history invalidates the internal-transfer set computed on the old one.
        _cache.pop(_INTERNAL_KEY, None)
        _cache.pop(_INTERNAL_KEY + "+imports", None)
        return entry


def _ceilings(
    conn: sqlite3.Connection, powens_txns: list[Transaction]
) -> dict[int, date]:
    """Par compte importé rattaché, la première date que le connecteur couvre.

    Prise sur les opérations Powens elles-mêmes plutôt que sur une date saisie : c'est la
    seule borne qui suit la réalité de ce que le connecteur remonte. Un compte rattaché
    dont Powens ne remonte encore aucune opération n'a pas de borne — le relevé reste
    alors la seule source, et tout doit être conservé.
    """
    links = store.imported_links(conn)
    if not links:
        return {}
    first: dict[int, date] = {}
    for txn in powens_txns:
        if txn.date is None or txn.id_account is None:
            continue
        known = first.get(txn.id_account)
        if known is None or txn.date < known:
            first[txn.id_account] = txn.date
    return {
        db_id: first[powens_id] for db_id, powens_id in links.items() if powens_id in first
    }


def _imported(
    conn: sqlite3.Connection | None,
    floor: date | None,
    powens_txns: list[Transaction],
) -> list[Transaction]:
    if conn is None:
        return []
    return [
        Transaction.from_api(raw)
        for raw in store.imported_transactions(
            conn, since=floor, ceilings=_ceilings(conn, powens_txns)
        )
    ]


async def load_transactions(
    client: PowensClient,
    *,
    months: int = 24,
    ttl: float = 300,
    conn: sqlite3.Connection | None = None,
) -> list[Transaction]:
    """Transactions over the last ``months``, served from a single cached history.

    Imported statement rows are merged in when ``conn`` is given, so a bank that no
    connector reaches still feeds every aggregate downstream.
    """
    covered, all_txns = await _load_history(client, months=months, ttl=ttl)
    floor = _min_date(months)
    txns = all_txns
    if covered != months:
        txns = [t for t in txns if t.date is None or t.date >= floor]
    # La borne des comptes rattachés se calcule sur l'historique *complet* : la restreindre
    # à la fenêtre demandée la ferait remonter avec elle, et une fenêtre courte
    # réintroduirait les doublons qu'une fenêtre longue écarte.
    return [*txns, *_imported(conn, floor, all_txns)]


async def load_spending_transactions(
    client: PowensClient,
    *,
    months: int = 24,
    include_investment: bool = False,
    ttl: float = 300,
    conn: sqlite3.Connection | None = None,
) -> list[Transaction]:
    """Transactions restricted to real spending accounts (checking/card).

    Set ``include_investment=True`` to keep every account (investment, savings, …).
    """
    txns = await load_transactions(client, months=months, ttl=ttl, conn=conn)
    if include_investment:
        return txns
    accounts = await load_accounts(client, ttl=ttl, conn=conn)
    spending_ids = {a.id for a in accounts.accounts if a.type in SPENDING_ACCOUNT_TYPES}
    return [t for t in txns if t.id_account in spending_ids]


async def load_internal_ids(
    client: PowensClient,
    *,
    months: int = 24,
    ttl: float = 300,
    conn: sqlite3.Connection | None = None,
) -> set[int]:
    """Ids of internal transfers, computed over ALL accounts.

    Must run on the full account set: a transfer from the checking account to a
    savings/investment account only has its mirror leg on the other account, so
    restricting to spending accounts first would miss it. Imported rows take part for
    the same reason — a transfer between two banks only has both legs once both are
    loaded, which is precisely what an import unlocks.

    Le résultat est caché AUSSI quand des lignes importées participent : la détection
    miroir est quadratique par groupe de montant et se payait à chaque requête de
    /comptes, /analyse et /abonnements. La fraîcheur est garantie par ailleurs —
    toutes les routes qui modifient les imports appellent ``clear_cache()``.
    """
    _, all_txns = await _load_history(client, months=months, ttl=ttl)
    key = _INTERNAL_KEY + ("+imports" if conn is not None else "")
    cached = _get(key, ttl)
    if cached is not None:
        return cached  # type: ignore[return-value]
    extra = _imported(conn, None, all_txns)
    ids = internal_transfer_ids([*all_txns, *extra] if extra else all_txns)
    _set(key, ids)
    return ids
