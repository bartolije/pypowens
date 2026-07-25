"""Récapitulatif patrimoine: net worth, accounts by family, connection health."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from pypowens import Account, PowensClient

from . import store
from .config import Settings
from .data import load_accounts, load_connections, load_investments
from .deps import get_client, get_settings, get_store
from .helpers import PALETTE, currency_symbol, donut_chart, line_chart
from .web import templates

router = APIRouter()

# Families are rendered in this declared order (empty ones are skipped).
FAMILY_ORDER = [
    "Comptes courants",
    "Épargne",
    "Investissement",
    "Assurance-vie",
    "Retraite",
    "Crédits",
    "Autre",
]

# Powens account ``type`` -> family label.
TYPE_TO_FAMILY = {
    "checking": "Comptes courants",
    "card": "Comptes courants",
    "livret_a": "Épargne",
    "ldds": "Épargne",
    "csl": "Épargne",
    "cel": "Épargne",
    "pel": "Épargne",
    "savings": "Épargne",
    "cat": "Épargne",
    "market": "Investissement",
    "pea": "Investissement",
    "lifeinsurance": "Assurance-vie",
    "per": "Retraite",
    "loan": "Crédits",
    "mortgage": "Crédits",
    "consumercredit": "Crédits",
}


def _family_of(account_type: str | None) -> str:
    """Map a raw account type to its family label (unknown -> 'Autre')."""
    return TYPE_TO_FAMILY.get(account_type or "", "Autre")


def _currency_of(account: Account, default: str) -> str:
    return (account.currency or default).upper()


@router.get("/", response_class=HTMLResponse)
async def recap(
    request: Request,
    client: PowensClient = Depends(get_client),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
    conn: sqlite3.Connection = Depends(get_store),  # noqa: B008
):
    accounts_list = await load_accounts(client)
    connections = await load_connections(client)

    # Net worth can only sum accounts sharing one currency (no FX rates here).
    # Accounts in another currency are listed apart and excluded from the total.
    base_currency = settings.base_currency
    accounts = [
        a for a in accounts_list.accounts if _currency_of(a, base_currency) == base_currency
    ]
    foreign = sorted(
        (a for a in accounts_list.accounts if _currency_of(a, base_currency) != base_currency),
        key=lambda a: (_currency_of(a, base_currency), -(a.balance or Decimal(0))),
    )
    foreign_totals: dict[str, Decimal] = {}
    for acc in foreign:
        code = _currency_of(acc, base_currency)
        foreign_totals[code] = foreign_totals.get(code, Decimal(0)) + (acc.balance or Decimal(0))

    # Group accounts by family + compute subtotals and net worth (Decimal).
    grouped: dict[str, list[Account]] = {name: [] for name in FAMILY_ORDER}
    subtotals: dict[str, Decimal] = {name: Decimal(0) for name in FAMILY_ORDER}
    net = Decimal(0)
    for acc in accounts:
        fam = _family_of(acc.type)
        grouped[fam].append(acc)
        balance = acc.balance or Decimal(0)
        subtotals[fam] += balance
        net += balance

    # Sort accounts within each family by balance, largest first (last column).
    for name in FAMILY_ORDER:
        grouped[name].sort(key=lambda a: a.balance or Decimal(0), reverse=True)

    families = [
        {"name": name, "accounts": grouped[name], "subtotal": subtotals[name]}
        for name in FAMILY_ORDER
        if grouped[name]
    ]

    # Donut of the repartition by family (floats for the chart helper).
    symbol = currency_symbol(base_currency)
    donut = donut_chart(
        [(fam["name"], float(fam["subtotal"])) for fam in families],
        unit=symbol,
        center_top="Total",
        center_bottom=f"{net / 1000:,.0f} k{symbol}".replace(",", " "),
    )

    # Colored allocation bars (share of net worth per family), palette aligned
    # with the donut order.
    total_pct = float(net) or 1.0
    allocation = [
        {
            "name": fam["name"],
            "subtotal": fam["subtotal"],
            "pct": float(fam["subtotal"]) / total_pct * 100,
            "color": PALETTE[i % len(PALETTE)],
        }
        for i, fam in enumerate(families)
    ]

    # Record today's balances, then measure the variation against the last recorded
    # day. This replaces the previous guess based on the undocumented "diff" field,
    # which only reflected investment revaluation.
    store.record_snapshot(conn, accounts_list.accounts, default_currency=base_currency)
    history = store.net_worth_history(conn, currency=base_currency)
    previous = store.previous_net_worth(conn, currency=base_currency)
    if previous is not None:
        previous_date, previous_net = previous
        net_diff = net - previous_net
        net_diff_pct = float(net_diff / previous_net * 100) if previous_net else 0.0
        diff_since = previous_date.strftime("%d/%m/%Y")
    else:
        net_diff = Decimal(0)
        net_diff_pct = 0.0
        diff_since = None

    net_chart = line_chart(
        [(day.strftime("%d/%m"), float(value)) for day, value in history],
        unit=symbol,
    )

    # Security lines behind the investment accounts (best effort — see loader).
    investments = await load_investments(client)
    account_names = {a.id: (a.name or f"#{a.id}") for a in accounts_list.accounts}
    invest_rows = sorted(
        (
            {
                "account": account_names.get(inv.id_account, "—"),
                "label": inv.label or inv.code or "—",
                "code": inv.code,
                "quantity": inv.quantity,
                "valuation": inv.valuation,
                "diff": inv.diff,
                "diff_percent": inv.diff_percent,
                "currency": inv.currency or base_currency,
            }
            for inv in investments
        ),
        key=lambda row: row["valuation"] or Decimal(0),
        reverse=True,
    )
    invest_diff = sum((inv.diff or Decimal(0) for inv in investments), Decimal(0))

    # A healthy Powens connection has no state and no error message.
    conns = []
    for connection in connections:
        name = (
            connection.connector.name
            if connection.connector and connection.connector.name
            else "Banque"
        )
        message = connection.error_message or connection.state
        conns.append(
            {
                "id": connection.id,
                "name": name,
                "nb_accounts": len(connection.accounts),
                "last_update": (
                    connection.last_update.strftime("%d/%m/%Y %H:%M")
                    if connection.last_update
                    else "—"
                ),
                "ok": not message,
                "message": message or "",
            }
        )

    return templates.TemplateResponse(
        request,
        "recap.html",
        {
            "request": request,
            "active": "recap",
            "net": net,
            "net_currency": base_currency,
            "net_diff": net_diff,
            "net_diff_pct": net_diff_pct,
            "diff_since": diff_since,
            "net_chart": net_chart,
            "history_points": len(history),
            "allocation": allocation,
            "n_accounts": len(accounts),
            "n_connections": len(connections),
            "families": families,
            "donut": donut,
            "connections": conns,
            "invest_rows": invest_rows,
            "invest_diff": invest_diff,
            "foreign_accounts": foreign,
            "foreign_totals": foreign_totals,
            "has_accounts": bool(accounts_list.accounts),
        },
    )
