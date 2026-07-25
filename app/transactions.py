"""Drill-down: the transactions behind a merchant key, and category overrides.

Every aggregate elsewhere in the app groups transactions by normalized merchant
key. Without this page those aggregates are unauditable — you see "Assurance:
1 240 €" and cannot check what went in. Same reason the override form lives here:
when a line is misclassified, it gets fixed where it is visible.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from pypowens import PowensClient, Transaction

from . import store
from .config import Settings
from .data import load_accounts, load_internal_ids, load_spending_transactions
from .deps import get_client, get_settings, get_store
from .enrich import all_categories, merchant_key, resolve_category
from .helpers import line_chart, month_key, month_label_fr
from .web import templates

router = APIRouter()


@dataclass
class Line:
    txn: Transaction
    account: str
    amount: Decimal
    is_internal: bool


@router.get("/transactions", response_class=HTMLResponse)
async def transactions_page(
    request: Request,
    client: PowensClient = Depends(get_client),
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_store),
    label: str = Query(..., description="Normalized merchant key to drill into"),
    scope: str = Query(default="spending"),
):
    """List every transaction whose merchant key matches ``label``."""
    wanted = label.upper().strip()
    months = settings.history_months
    txns = await load_spending_transactions(
        client, months=months, include_investment=(scope == "all")
    )
    internal = await load_internal_ids(client, months=months)
    accounts = await load_accounts(client)
    account_names = {a.id: (a.name or f"#{a.id}") for a in accounts.accounts}

    matched = [t for t in txns if merchant_key(t) == wanted]
    matched.sort(key=lambda t: t.date or date.min, reverse=True)

    lines = [
        Line(
            txn=t,
            account=account_names.get(t.id_account, "—"),
            amount=t.value or Decimal(0),
            is_internal=t.id in internal,
        )
        for t in matched
    ]
    counted = [line for line in lines if not line.is_internal]
    total = sum((abs(line.amount) for line in counted), Decimal(0))

    # Monthly totals, so a price change or a stopped subscription is visible.
    by_month: dict[str, Decimal] = {}
    for line in counted:
        by_month[month_key(line.txn.date)] = by_month.get(
            month_key(line.txn.date), Decimal(0)
        ) + abs(line.amount)
    chart = line_chart(
        [(month_label_fr(k), float(v)) for k, v in sorted(by_month.items())]
    )

    overrides = store.all_overrides(conn)
    return templates.TemplateResponse(
        request,
        "transactions.html",
        {
            "request": request,
            "active": None,
            "label": wanted,
            "display": wanted.title(),
            "category": resolve_category(wanted, overrides),
            "is_overridden": wanted in overrides,
            "categories": all_categories(),
            "lines": lines,
            "count": len(counted),
            "total": total,
            "average": (total / len(counted)).quantize(Decimal("0.01")) if counted else Decimal(0),
            "chart": chart,
            "scope": scope,
            "months": months,
        },
    )


@router.post("/categorie")
async def set_category(
    request: Request,
    conn: sqlite3.Connection = Depends(get_store),
    label: str = Form(...),
    category: str = Form(...),
    back: str = Form(default="/"),
) -> RedirectResponse:
    """Persist (or clear) a manual category for a merchant key, then go back."""
    if category == "__auto__":
        store.clear_override(conn, label)
    else:
        store.set_override(conn, label, category)
    return RedirectResponse(back or "/", status_code=303)
