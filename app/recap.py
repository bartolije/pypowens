"""Récapitulatif patrimoine: net worth, accounts by family, connection health."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from pypowens import Account, PowensClient

from .data import load_accounts, load_connections
from .deps import get_client
from .helpers import PALETTE, donut_chart
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


@router.get("/", response_class=HTMLResponse)
async def recap(request: Request, client: PowensClient = Depends(get_client)):  # noqa: B008
    accounts_list = await load_accounts(client)
    connections = await load_connections(client)
    accounts = accounts_list.accounts

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
    donut = donut_chart(
        [(fam["name"], float(fam["subtotal"])) for fam in families],
        center_top="Total",
        center_bottom=f"{net / 1000:,.0f} k€".replace(",", " "),
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

    # Net-worth variation from investment valuation change (raw "diff" field).
    net_diff = sum((Decimal(str(a.raw.get("diff") or 0)) for a in accounts), Decimal(0))
    base = net - net_diff
    net_diff_pct = float(net_diff / base * 100) if base else 0.0

    # A healthy Powens connection has no state and no error message.
    conns = []
    for conn in connections:
        name = conn.connector.name if conn.connector and conn.connector.name else "Banque"
        message = conn.error_message or conn.state
        conns.append(
            {
                "name": name,
                "nb_accounts": len(conn.accounts),
                "last_update": (
                    conn.last_update.strftime("%d/%m/%Y %H:%M") if conn.last_update else "—"
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
            "net_diff": net_diff,
            "net_diff_pct": net_diff_pct,
            "allocation": allocation,
            "n_accounts": len(accounts),
            "n_connections": len(connections),
            "families": families,
            "donut": donut,
            "connections": conns,
            "has_accounts": bool(accounts),
        },
    )
