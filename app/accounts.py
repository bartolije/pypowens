"""Default page (``GET /``): current accounts, then spending history by date.

This is the screen that gets looked at every day, so it deliberately shows only
what is spendable — the current accounts — and never the net worth. Wealth lives
on ``/patrimoine``, one deliberate click away, so the page can be open at a desk
without a balance sheet on screen.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from pypowens import Account, PowensClient, Transaction

from . import store
from .config import Settings
from .data import load_accounts, load_connections, load_internal_ids, load_transactions
from .deps import get_client, get_settings, get_store
from .enrich import all_categories, merchant_key, resolve_category_txn, split_wording
from .helpers import month_key, month_label_fr
from .wealth import category_emoji, monogram, rail
from .web import templates

router = APIRouter()

# Accounts holding money that can be spent today. Savings, investment and loan
# accounts are wealth, not cash flow — they belong to /patrimoine.
CURRENT_ACCOUNT_TYPES = frozenset({"checking", "card"})
# Months offered in the picker. Bounded by what the cached history covers.
PICKER_MONTHS = 18


@dataclass
class Row:
    """One transaction, ready to render."""

    txn: Transaction
    account: str
    category: str
    amount: Decimal
    internal: bool
    # Présentation : le libellé coupé en (essentiel, références), le
    # pictogramme du type de dépense, et la pastille de la banque d'origine.
    # Calculés côté serveur pour que le template reste déclaratif.
    label: str = ""
    detail: str = ""
    emoji: str = "•"
    rail_emoji: str = ""
    rail_label: str = ""
    bank_initials: str = ""
    bank_color: str = "#8a8a8a"
    bank_ink: str = "#ffffff"


@dataclass
class Day:
    """Transactions of a single day, with that day's net total."""

    day: date
    rows: list[Row] = field(default_factory=list)
    spent: Decimal = Decimal(0)
    received: Decimal = Decimal(0)


def _month_options(months: int) -> list[tuple[str, str]]:
    """``(YYYY-MM, "juil. 26")`` for the current month and the previous ones."""
    today = date.today()
    year, month = today.year, today.month
    out: list[tuple[str, str]] = []
    for _ in range(months):
        key = f"{year:04d}-{month:02d}"
        out.append((key, month_label_fr(key)))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return out


def _shift(key: str, delta: int) -> str:
    year, month = int(key[:4]), int(key[5:7])
    total = year * 12 + (month - 1) + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def group_by_day(rows: list[Row]) -> list[Day]:
    """Bucket rows per calendar day, most recent first, with per-day totals.

    Internal transfers are listed (moving money to a savings account is worth
    seeing) but excluded from the day's totals, otherwise a 6 000 € transfer to a
    livret reads as a 6 000 € expense.
    """
    days: dict[date, Day] = {}
    for row in rows:
        if row.txn.date is None:
            continue
        day = days.setdefault(row.txn.date, Day(day=row.txn.date))
        day.rows.append(row)
        if row.internal:
            continue
        if row.amount < 0:
            day.spent += -row.amount
        else:
            day.received += row.amount
    for day in days.values():
        day.rows.sort(key=lambda r: abs(r.amount), reverse=True)
    return [days[key] for key in sorted(days, reverse=True)]


@router.get("/comptes", response_class=HTMLResponse)
async def accounts_page(
    request: Request,
    client: PowensClient = Depends(get_client),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
    conn: sqlite3.Connection = Depends(get_store),  # noqa: B008
    mois: str | None = Query(default=None, description="YYYY-MM, defaults to this month"),
    categorie: str | None = Query(default=None),
    sens: str = Query(default="depenses", description="depenses | tout"),
):
    accounts_list = await load_accounts(client, conn=conn)
    base_currency = settings.base_currency

    current: list[Account] = sorted(
        (a for a in accounts_list.accounts if a.type in CURRENT_ACCOUNT_TYPES),
        key=lambda a: a.balance or Decimal(0),
        reverse=True,
    )
    # Only accounts held in the base currency are totalled: no FX rate is fetched,
    # so mixing currencies in one figure would be plain wrong.
    in_base = [a for a in current if (a.currency or base_currency) == base_currency]
    available = sum((a.balance or Decimal(0) for a in in_base), Decimal(0))
    coming = sum((a.coming or Decimal(0) for a in in_base), Decimal(0))

    options = _month_options(PICKER_MONTHS)
    valid = {key for key, _ in options}
    if sens not in ("depenses", "tout"):
        sens = "depenses"

    # One cached history covers every month in the picker; the month is a filter.
    txns = await load_transactions(client, months=settings.history_months, conn=conn)
    internal = await load_internal_ids(client, months=settings.history_months, conn=conn)
    current_ids = {a.id for a in current}
    aliases = store.account_aliases(conn)
    account_names = {
        a.id: ((aliases.get(a.id) if a.id is not None else None) or a.name or f"#{a.id}")
        for a in accounts_list.accounts
    }
    overrides = store.all_overrides(conn)

    # Pastille de la banque d'origine, par compte : sur un historique qui mélange
    # six établissements, retrouver « d'où sort cette ligne » demandait de lire
    # un nom tronqué en fin de tableau.
    connections = await load_connections(client)
    account_bank: dict[int | None, tuple[str, str, str]] = {}
    for connection in connections:
        badge = monogram(connection.connector)
        for account in connection.accounts:
            account_bank[account.id] = badge

    # Default to the current month, but fall back to the last month that actually
    # has operations: on the 1st, or right after a sync that lags, the current month
    # is empty and an empty landing page reads as "the app is broken".
    if mois in valid:
        month = mois
    else:
        present = {
            month_key(t.date) for t in txns if t.id_account in current_ids and t.value
        } & valid
        month = options[0][0]
        if month not in present and present:
            month = max(present)

    month_rows = []
    for t in txns:
        if t.id_account not in current_ids or not t.value or month_key(t.date) != month:
            continue
        category = resolve_category_txn(t, overrides)
        label, detail = split_wording(
            t.simplified_wording or t.wording or t.original_wording or ""
        )
        initials, color, ink = account_bank.get(t.id_account, ("", "#8a8a8a", "#ffffff"))
        rail_emoji, rail_label = rail(t.type)
        month_rows.append(
            Row(
                txn=t,
                account=account_names.get(t.id_account, "—"),
                category=category,
                amount=t.value or Decimal(0),
                internal=t.id in internal,
                label=label or "—",
                detail=detail,
                emoji=category_emoji(category),
                rail_emoji=rail_emoji,
                rail_label=rail_label,
                bank_initials=initials,
                bank_color=color,
                bank_ink=ink,
            )
        )

    # Totals and the category breakdown describe the whole month, never the filtered
    # view: they are the reference the table is read against, so hiding credits or
    # picking one category must not move them.
    by_category: dict[str, Decimal] = defaultdict(Decimal)
    month_received = Decimal(0)
    for row in month_rows:
        if row.internal:
            continue
        if row.amount < 0:
            by_category[row.category] += -row.amount
        else:
            month_received += row.amount
    categories = sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)
    month_spent = sum(by_category.values(), Decimal(0))

    rows = month_rows if sens == "tout" else [r for r in month_rows if r.amount < 0]
    selected = categorie if categorie in by_category else None
    if selected:
        rows = [r for r in rows if r.category == selected]

    days = group_by_day(rows)
    index = [key for key, _ in options].index(month)

    return templates.TemplateResponse(
        request,
        "accounts.html",
        {
            "request": request,
            "active": "comptes",
            "currency": base_currency,
            "current_accounts": current,
            "available": available,
            "coming": coming,
            "days": days,
            "n_rows": len(rows),
            "month": month,
            "month_label": month_label_fr(month),
            "month_options": options,
            "prev_month": _shift(month, -1) if index + 1 < len(options) else None,
            "next_month": _shift(month, 1) if index > 0 else None,
            "categories": categories,
            "category_labels": all_categories(),
            "selected_category": selected,
            "month_spent": month_spent,
            "month_received": month_received,
            "month_net": month_received - month_spent,
            "sens": sens,
            "has_accounts": bool(accounts_list.accounts),
            "merchant_key": merchant_key,
        },
    )
