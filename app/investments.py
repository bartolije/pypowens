"""Page performance (``/performance``) : ce que les supports ont rapporté.

Assemble ce que :mod:`app.performance` calcule et ce que :mod:`app.collector` archive.
La page dit toujours **d'où sort le chiffre** — série reconstruite ou soldes réels,
couverture, mouvements de titres non rejoués — parce qu'un rendement sans son périmètre
est juste un nombre crédible.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from pypowens import Account, Investment, PowensClient

from . import performance as perf
from . import store
from .collector import INVESTMENT_TYPES
from .config import Settings
from .data import load_accounts, load_transactions
from .deps import get_client, get_settings, get_store
from .helpers import line_chart
from .web import templates

router = APIRouter()

# Fenêtres proposées. « Tout » vaut None : on prend alors ce que l'archive couvre.
PERIODS: dict[str, tuple[str, int | None]] = {
    "1m": ("1 mois", 30),
    "3m": ("3 mois", 91),
    "6m": ("6 mois", 182),
    "1a": ("1 an", 365),
    "5a": ("5 ans", 1826),
    "tout": ("Tout", None),
}
DEFAULT_PERIOD = "tout"


@dataclass
class Line:
    """Une ligne de titre, prête à afficher."""

    label: str
    code: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    unit_value: Decimal | None
    valuation: Decimal
    diff: Decimal | None
    diff_percent: float | None
    share: float | None
    is_cash: bool


@dataclass
class Card:
    """Un compte d'investissement, avec sa performance et ses lignes."""

    account: Account
    lines: list[Line]
    invested: Decimal
    cash: Decimal
    cost: Decimal
    unrealized: Decimal
    performance: perf.Performance | None
    chart: str
    points: int


def _lines_of(investments: list[Investment]) -> list[Line]:
    rows = [
        Line(
            label=inv.label or f"#{inv.id}",
            code=inv.code,
            quantity=inv.quantity,
            unit_price=inv.unit_price,
            unit_value=inv.unit_value,
            valuation=inv.valuation or Decimal(0),
            diff=inv.diff,
            diff_percent=float(inv.diff_percent) if inv.diff_percent is not None else None,
            share=float(inv.portfolio_share) if inv.portfolio_share is not None else None,
            is_cash=perf.is_cash_line(code=inv.code, label=inv.label),
        )
        for inv in investments
    ]
    return sorted(rows, key=lambda r: r.valuation, reverse=True)


def _chart(points: list[perf.Point]) -> str:
    """Courbe de valorisation. Les libellés restent lisibles : un point sur cinq suffit."""
    if len(points) < 2:
        return ""
    step = max(1, len(points) // 40)
    kept = points[::step] + ([points[-1]] if len(points) % step else [])
    return line_chart([(f"{p.day:%d/%m}", float(p.value)) for p in kept])


@router.get("/performance", response_class=HTMLResponse)
async def performance_page(
    request: Request,
    client: PowensClient = Depends(get_client),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
    conn: sqlite3.Connection = Depends(get_store),  # noqa: B008
    periode: str = Query(default=DEFAULT_PERIOD),
):
    if periode not in PERIODS:
        periode = DEFAULT_PERIOD
    _, days = PERIODS[periode]
    since = date.today() - timedelta(days=days) if days else None

    accounts = (await load_accounts(client, conn=conn)).accounts
    holders = [a for a in accounts if (a.type or "") in INVESTMENT_TYPES]
    investments = await client.list_investments()
    txns = await load_transactions(client, months=settings.history_months, conn=conn)
    overrides = store.flow_overrides(conn)

    cards: list[Card] = []
    for account in sorted(holders, key=lambda a: a.balance or Decimal(0), reverse=True):
        held = [i for i in investments if i.id_account == account.id]
        lines = _lines_of(held)
        cash = sum((r.valuation for r in lines if r.is_cash), Decimal(0))
        invested = sum((r.valuation for r in lines if not r.is_cash), Decimal(0))
        cost = sum(
            (
                (r.unit_price or Decimal(0)) * (r.quantity or Decimal(0))
                for r in lines
                if not r.is_cash
            ),
            Decimal(0),
        )
        unrealized = sum((r.diff or Decimal(0) for r in lines if not r.is_cash), Decimal(0))

        values = store.investment_values(conn, account_id=account.id)
        quantities = {
            i.id: i.quantity for i in held if i.id is not None and i.quantity is not None
        }
        valuations = {
            i.id: i.valuation for i in held if i.id is not None and i.valuation is not None
        }
        points = perf.reconstruct_series(values, quantities)
        computed = perf.compute(
            account_id=account.id or 0,
            points=points,
            flows=perf.qualify_flows(
                txns, account_id=account.id or 0, overrides=overrides
            ),
            since=since,
            coverage=perf.series_coverage(
                values, valuations, account.balance, cash=cash
            ),
        )
        cards.append(
            Card(
                account=account,
                lines=lines,
                invested=invested,
                cash=cash,
                cost=cost,
                unrealized=unrealized,
                performance=computed,
                chart=_chart(computed.points) if computed else "",
                points=len(points),
            )
        )

    span = store.investment_value_span(conn)
    return templates.TemplateResponse(
        request,
        "performance.html",
        {
            "request": request,
            "active": "performance",
            "currency": settings.base_currency,
            "cards": cards,
            "periods": [(key, label) for key, (label, _) in PERIODS.items()],
            "period": periode,
            "period_label": PERIODS[periode][0],
            "archive_span": span,
            "min_coverage": perf.MIN_COVERAGE,
            "min_annualize_days": perf.MIN_ANNUALIZE_DAYS,
        },
    )
