"""Synthèse (``GET /``): Finary-style dashboard — net worth, chart, performance cards."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from pypowens import PowensClient

from . import store
from .config import Settings
from .data import load_accounts, load_investments
from .deps import get_client, get_settings, get_store
from .helpers import PALETTE, currency_symbol, line_chart
from .wealth import FAMILY_ORDER, build_invest_rows, family_of, today_fr
from .web import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def synthese(
    request: Request,
    period: str = "tout",
    client: PowensClient = Depends(get_client),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
    conn: sqlite3.Connection = Depends(get_store),  # noqa: B008
):
    accounts_list = await load_accounts(client, conn=conn)
    base_currency = settings.base_currency

    # Only accounts in base currency count toward net worth.
    accounts = [
        a for a in accounts_list.accounts
        if (a.currency or base_currency).upper() == base_currency
    ]

    net = sum((a.balance or Decimal(0) for a in accounts), Decimal(0))

    # Allocation by family.
    subtotals: dict[str, Decimal] = {name: Decimal(0) for name in FAMILY_ORDER}
    for acc in accounts:
        fam = family_of(acc.type)
        subtotals[fam] += acc.balance or Decimal(0)

    total_assets = sum((v for v in subtotals.values() if v > 0), Decimal(0))
    assets: list[dict[str, Any]] = [
        {"name": name, "subtotal": subtotals[name]}
        for name in FAMILY_ORDER
        if subtotals[name] > 0
    ]
    allocation = [
        {
            "name": fam["name"],
            "pct": float(fam["subtotal"]) / (float(total_assets) or 1) * 100,
            "color": PALETTE[i % len(PALETTE)],
        }
        for i, fam in enumerate(assets)
    ]

    # Record (au plus une fois par jour — le collecteur est la référence) & retrieve.
    store.ensure_snapshot(conn, accounts_list.accounts, default_currency=base_currency)
    since = store.period_to_since(period)
    history = store.net_worth_history(conn, currency=base_currency, since=since)

    if history:
        first_date, first_net = history[0]
        net_diff = net - first_net
        net_diff_pct = float(net_diff / first_net * 100) if first_net else 0.0
        diff_since = first_date.strftime("%d/%m/%Y")
    else:
        net_diff = Decimal(0)
        net_diff_pct = 0.0
        diff_since = None

    symbol = currency_symbol(base_currency)
    net_chart = line_chart(
        [(day.strftime("%d/%m"), float(value)) for day, value in history],
        unit=symbol,
        color="#e8a838",
    )

    # Investment lines for "Ma performance" cards.
    investments = await load_investments(client)
    account_names = {a.id: (a.name or f"#{a.id}") for a in accounts_list.accounts}
    invest_rows, invest_diff, invest_diff_pct = build_invest_rows(
        investments, account_names, base_currency
    )

    return templates.TemplateResponse(
        request,
        "synthese.html",
        {
            "request": request,
            "active": "synthese",
            "net": net,
            "net_currency": base_currency,
            "net_diff": net_diff,
            "net_diff_pct": net_diff_pct,
            "diff_since": diff_since,
            "net_chart": net_chart,
            "history_points": len(history),
            "allocation": allocation,
            "invest_rows": invest_rows,
            "invest_diff": invest_diff,
            "invest_diff_pct": invest_diff_pct,
            "today": today_fr(),
            "has_accounts": bool(accounts_list.accounts),
            "period": period.lower(),
        },
    )
