"""'Lignes récurrentes par libellé' — group transactions by normalized label over
a date range and rank by occurrence count and total amount.

This is the raw, catalog-free view: what keeps coming back and how much it weighs.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse

from pypowens import PowensClient

from . import store
from .config import Settings
from .data import MAX_WINDOW_MONTHS, load_internal_ids, load_spending_transactions
from .deps import get_client, get_settings, get_store
from .enrich import merchant_key, resolve_category
from .helpers import bar_chart
from .web import templates

router = APIRouter()

SORTS = {
    "count": ("Nb d'occurrences", lambda g: (g.count, g.total)),
    "total": ("Montant total", lambda g: (g.total, g.count)),
    "avg": ("Montant moyen", lambda g: (g.avg, g.count)),
    "recent": ("Plus récent", lambda g: (g.last_date, g.count)),
}


@dataclass
class LabelGroup:
    label: str
    key: str
    category: str
    count: int
    total: Decimal
    avg: Decimal
    min_amount: Decimal
    max_amount: Decimal
    first_date: date
    last_date: date
    account_ids: list[int]

    @property
    def span_days(self) -> int:
        return (self.last_date - self.first_date).days

    @property
    def avg_interval_days(self) -> int | None:
        """Rough average number of days between occurrences (None if a single one)."""
        if self.count < 2 or self.span_days <= 0:
            return None
        return round(self.span_days / (self.count - 1))


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def group_by_label(
    transactions: list[Any],
    *,
    internal_ids: set[int] | None = None,
    kind: str = "debit",
    date_from: date | None = None,
    date_to: date | None = None,
    min_count: int = 2,
    overrides: dict[str, str] | None = None,
) -> list[LabelGroup]:
    """Group transactions by normalized label, computing count/total/stats."""
    internal_ids = internal_ids or set()
    buckets: dict[str, list[Any]] = {}
    for t in transactions:
        if t.value is None or t.date is None or t.id in internal_ids:
            continue
        v = float(t.value)
        if kind == "debit" and v >= 0:
            continue
        if kind == "credit" and v <= 0:
            continue
        if date_from and t.date < date_from:
            continue
        if date_to and t.date > date_to:
            continue
        buckets.setdefault(merchant_key(t), []).append(t)

    groups: list[LabelGroup] = []
    for label, txns in buckets.items():
        if len(txns) < min_count:
            continue
        amounts = [abs(Decimal(str(t.value))) for t in txns]
        dates = sorted(t.date for t in txns)
        total = sum(amounts, Decimal("0"))
        groups.append(
            LabelGroup(
                label=label.title(),
                key=label,
                category=resolve_category(label, overrides),
                count=len(txns),
                total=total,
                avg=(total / len(txns)).quantize(Decimal("0.01")),
                min_amount=min(amounts),
                max_amount=max(amounts),
                first_date=dates[0],
                last_date=dates[-1],
                account_ids=sorted({t.id_account for t in txns if t.id_account is not None}),
            )
        )
    return groups


@router.get("/recurrences", response_class=HTMLResponse)
async def recurrences(
    request: Request,
    client: PowensClient = Depends(get_client),
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_store),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    sort: str = Query(default="total"),
    min_count: int = Query(default=2, ge=1),
    kind: str = Query(default="debit"),
    scope: str = Query(default="spending"),
) -> Response:
    """Recurring lines grouped by label over a date range, ranked by count/amount."""
    d_from = _parse_date(date_from)
    d_to = _parse_date(date_to)
    # Default range = year-to-date (since Jan 1 of the current year).
    if d_from is None:
        d_from = date(date.today().year, 1, 1)
    if sort not in SORTS:
        sort = "total"
    if kind not in ("debit", "credit"):
        kind = "debit"
    if scope not in ("spending", "all"):
        scope = "spending"

    # Load a window that covers the requested range (default: configured history).
    if d_from:
        months = min(MAX_WINDOW_MONTHS, max(1, math.ceil((date.today() - d_from).days / 30) + 1))
    else:
        months = settings.history_months
    txns = await load_spending_transactions(
        client, months=months, include_investment=(scope == "all"), conn=conn
    )
    internal = await load_internal_ids(client, months=months, conn=conn)

    groups = group_by_label(
        txns,
        internal_ids=internal,
        kind=kind,
        date_from=d_from,
        date_to=d_to,
        min_count=min_count,
        overrides=store.all_overrides(conn),
    )
    groups.sort(key=SORTS[sort][1], reverse=True)

    total_all = sum((g.total for g in groups), Decimal("0"))
    occ_all = sum(g.count for g in groups)
    top = sorted(groups, key=lambda g: g.total, reverse=True)[:15]
    chart = bar_chart(
        [(g.label[:22], float(g.total)) for g in top],
        color="#7c6cff",
    )

    return templates.TemplateResponse(
        request,
        "frequency.html",
        {
            "request": request,
            "active": "frequency",
            "groups": groups,
            "chart": chart,
            "total_all": total_all,
            "occ_all": occ_all,
            "nb_labels": len(groups),
            "sort": sort,
            "sorts": SORTS,
            "kind": kind,
            "scope": scope,
            "min_count": min_count,
            "date_from": d_from.isoformat(),
            "date_to": d_to.isoformat() if d_to else "",
        },
    )
