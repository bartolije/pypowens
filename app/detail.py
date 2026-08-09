"""Account detail page: balance history, investments, sync status."""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from pypowens import PowensClient

from . import store
from .config import Settings
from .data import load_accounts, load_connections, load_investments
from .deps import get_client, get_settings, get_store
from .helpers import currency_symbol, line_chart
from .recap import TYPE_TO_FAMILY
from .web import templates

router = APIRouter()

_MONTHS_FR = [
    "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
]

# Account types that hold investment positions.
_INVEST_TYPES = frozenset({"market", "pea", "lifeinsurance", "per"})


def _today_fr() -> str:
    d = date.today()
    return f"{d.day:02d} {_MONTHS_FR[d.month - 1]} {d.year}"


@router.get("/patrimoine/{account_id}", response_class=HTMLResponse)
async def account_detail(
    request: Request,
    account_id: int,
    client: PowensClient = Depends(get_client),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
    conn: sqlite3.Connection = Depends(get_store),  # noqa: B008
):
    base_currency = settings.base_currency
    accounts_list = await load_accounts(client, conn=conn)

    # Find the requested account.
    account = None
    for a in accounts_list.accounts:
        if a.id == account_id:
            account = a
            break
    if account is None:
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "request": request,
                "active": "recap",
                "title": "Compte introuvable",
                "detail": f"Aucun compte avec l'identifiant {account_id}.",
                "hints": ["Retourner au patrimoine pour choisir un compte existant."],
                "technical": None,
                "retry_url": "/patrimoine",
            },
            status_code=404,
        )

    currency = (account.currency or base_currency).upper()
    symbol = currency_symbol(currency)
    balance = account.balance or Decimal(0)
    is_invest = (account.type or "") in _INVEST_TYPES
    family = TYPE_TO_FAMILY.get(account.type or "", "Autre")

    # Connection info for sync badge.
    connections = await load_connections(client)
    last_update = None
    sync_ok = True
    for connection in connections:
        if connection.id == account.id_connection:
            last_update = connection.last_update
            sync_ok = not connection.state and not connection.error_message
            break
    if account.last_update:
        last_update = account.last_update

    # Per-account balance history.
    store.record_snapshot(conn, accounts_list.accounts, default_currency=base_currency)
    history = store.account_balance_history(conn, account_id=account_id, currency=currency)

    # Variation: compare current balance to the first recorded balance.
    if len(history) >= 2:
        first_date, first_balance = history[0]
        diff = balance - first_balance
        diff_pct = float(diff / first_balance * 100) if first_balance else 0.0
        diff_since = first_date.strftime("%d/%m/%Y")
    else:
        diff = Decimal(0)
        diff_pct = 0.0
        diff_since = None

    chart = line_chart(
        [(day.strftime("%d/%m"), float(value)) for day, value in history],
        unit=symbol,
        color="#e8a838",
    )

    # Investments for this account.
    invest_rows = []
    invest_total_valuation = Decimal(0)
    invest_total_diff = Decimal(0)
    invest_total_cost = Decimal(0)
    if is_invest:
        all_investments = await load_investments(client)
        for inv in all_investments:
            if inv.id_account != account_id:
                continue
            valuation = inv.valuation or Decimal(0)
            inv_diff = inv.diff or Decimal(0)
            invest_total_valuation += valuation
            invest_total_diff += inv_diff
            # Cost = valuation - diff (prix de revient).
            cost = valuation - inv_diff
            invest_total_cost += cost
            invest_rows.append(
                {
                    "label": inv.label or inv.code or "---",
                    "code": inv.code,
                    "quantity": inv.quantity,
                    "unit_value": inv.unit_value,
                    "valuation": valuation,
                    "diff": inv_diff,
                    "diff_percent": inv.diff_percent,
                    "currency": inv.currency or currency,
                }
            )
        invest_rows.sort(key=lambda r: r["valuation"], reverse=True)

    invest_diff_pct = (
        float(invest_total_diff / invest_total_cost * 100) if invest_total_cost else 0.0
    )

    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "request": request,
            "active": "recap",
            "account": account,
            "account_name": account.name or f"Compte #{account_id}",
            "family": family,
            "currency": currency,
            "symbol": symbol,
            "balance": balance,
            "is_invest": is_invest,
            "last_update": last_update,
            "sync_ok": sync_ok,
            "diff": diff,
            "diff_pct": diff_pct,
            "diff_since": diff_since,
            "chart": chart,
            "history_points": len(history),
            "invest_rows": invest_rows,
            "invest_total_valuation": invest_total_valuation,
            "invest_total_diff": invest_total_diff,
            "invest_total_cost": invest_total_cost,
            "invest_diff_pct": invest_diff_pct,
            "today": _today_fr(),
        },
    )
