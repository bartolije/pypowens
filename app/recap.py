"""Récapitulatif patrimoine: net worth, accounts by family, connection health."""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from pypowens import Account, PowensClient

from . import store
from .classify import classify_investments
from .config import Settings
from .data import load_accounts, load_connections, load_investments
from .deps import get_client, get_settings, get_store
from .helpers import PALETTE, currency_symbol, donut_chart, line_chart, treemap
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


# Connection states that only the user can clear, by going back through the bank's
# own authentication in the Webview. Retrying the sync on these is pointless.
USER_ACTION_STATES = frozenset(
    {"webauthRequired", "SCARequired", "additionalInformationNeeded", "wrongpass",
     "actionNeeded", "decoupled", "validating"}
)

# Readable equivalents of the states Powens reports. Without this the page prints
# ``error_message`` verbatim, which for a webauth connection is the bank's full
# authorize URL — unreadable, and it carries the flow's ``state`` token.
STATE_LABELS = {
    "webauthRequired": "Authentification à terminer sur le site de la banque",
    "SCARequired": "Validation forte (SCA) à effectuer",
    "additionalInformationNeeded": "Information complémentaire demandée par la banque",
    "decoupled": "Validation à confirmer dans l'application de la banque",
    "validating": "Validation en cours côté banque",
    "wrongpass": "Identifiants refusés par la banque",
    "actionNeeded": "Action requise sur le site de la banque",
    "passwordExpired": "Mot de passe expiré chez la banque",
    "websiteUnavailable": "Site de la banque indisponible",
    "rateLimiting": "Trop de tentatives — banque temporairement bloquante",
    "bug": "Erreur du connecteur Powens",
}


_MONTHS_FR = [
    "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
]


def _today_fr() -> str:
    d = date.today()
    return f"{d.day:02d} {_MONTHS_FR[d.month - 1]} {d.year}"


def _family_of(account_type: str | None) -> str:
    """Map a raw account type to its family label (unknown -> 'Autre')."""
    return TYPE_TO_FAMILY.get(account_type or "", "Autre")


def _currency_of(account: Account, default: str) -> str:
    return (account.currency or default).upper()


@router.get("/patrimoine", response_class=HTMLResponse)
async def recap(
    request: Request,
    period: str = "tout",
    type: str | None = None,  # noqa: A002
    institution: str | None = None,
    group: str = "0",
    client: PowensClient = Depends(get_client),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
    conn: sqlite3.Connection = Depends(get_store),  # noqa: B008
):
    accounts_list = await load_accounts(client, conn=conn)
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

    # Collect unique family and connection names for filter pills.
    family_names = [fam["name"] for fam in families]

    # Build account_id -> connection_name mapping for institution filter.
    account_to_connection: dict[int, str] = {}
    for connection in connections:
        conn_name = (
            connection.connector.name
            if connection.connector and connection.connector.name
            else "Banque"
        )
        for acc in connection.accounts:
            if acc.id is not None:
                account_to_connection[acc.id] = conn_name
    connection_names = sorted(set(account_to_connection.values()))

    # Normalise filter values (None if empty string).
    type_filter = type if type else None
    institution_filter = institution if institution else None

    # Apply type filter: keep only matching families.
    if type_filter:
        families = [fam for fam in families if fam["name"] == type_filter]

    # Apply institution filter: keep only accounts whose connection matches.
    if institution_filter:
        filtered_families = []
        for fam in families:
            matching = [
                acc for acc in fam["accounts"]
                if account_to_connection.get(acc.id) == institution_filter
            ]
            if matching:
                sub = sum((a.balance or Decimal(0) for a in matching), Decimal(0))
                filtered_families.append(
                    {"name": fam["name"], "accounts": matching, "subtotal": sub}
                )
        families = filtered_families

    # A repartition can only describe parts of a positive whole, so debt is kept out
    # of it. Connecting a mortgage otherwise turns a 256 k€ loan into a "25 % share of
    # your wealth" — the donut helper takes absolute values, and a negative share
    # renders as a zero-width allocation bar.
    assets = [fam for fam in families if fam["subtotal"] > 0]
    debts = [fam for fam in families if fam["subtotal"] < 0]
    total_assets = sum((fam["subtotal"] for fam in assets), Decimal(0))
    total_debt = -sum((fam["subtotal"] for fam in debts), Decimal(0))

    symbol = currency_symbol(base_currency)
    donut = donut_chart(
        [(fam["name"], float(fam["subtotal"])) for fam in assets],
        unit=symbol,
        center_top="Actifs",
        center_bottom=f"{total_assets / 1000:,.0f} k{symbol}".replace(",", " "),
    )

    # Per-account donut for the right panel (Finary shows accounts, not families).
    account_items = sorted(
        ((a.name or "—", float(a.balance or 0)) for a in accounts if (a.balance or 0) > 0),
        key=lambda x: x[1],
        reverse=True,
    )
    top_account = account_items[0] if account_items else ("—", 0)
    top_pct = f"{top_account[1] / float(total_assets) * 100:.2f} %" if total_assets else ""
    account_donut = donut_chart(
        account_items[:8],
        unit=symbol,
        size=220,
        center_top=f"{top_account[1]:,.0f} {symbol}".replace(",", " "),
        center_bottom=top_pct,
        compact=True,
    )

    # Colored allocation bars (share of total assets per family), palette aligned
    # with the donut order.
    total_pct = float(total_assets) or 1.0
    allocation = [
        {
            "name": fam["name"],
            "subtotal": fam["subtotal"],
            "pct": float(fam["subtotal"]) / total_pct * 100,
            "color": PALETTE[i % len(PALETTE)],
        }
        for i, fam in enumerate(assets)
    ]

    # Record today's balances, then measure the variation against the last recorded
    # day. This replaces the previous guess based on the undocumented "diff" field,
    # which only reflected investment revaluation.
    store.record_snapshot(conn, accounts_list.accounts, default_currency=base_currency)
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
    diff_label = f"depuis le {diff_since}" if diff_since else None

    net_chart = line_chart(
        [(day.strftime("%d/%m"), float(value)) for day, value in history],
        unit=symbol,
        color="#e8a838",
    )

    # Security lines behind the investment accounts (best effort — see loader).
    investments = await load_investments(client)
    account_names = {a.id: (a.name or f"#{a.id}") for a in accounts_list.accounts}
    invest_rows = sorted(
        (
            {
                "id_account": inv.id_account,
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

    # Investment classification (sector / country treemaps).
    sector_treemap = ""
    country_treemap = ""
    isins = [row["code"] for row in invest_rows if row.get("code")]
    if isins:
        try:
            classification = await classify_investments(
                isins, conn, settings.openfigi_api_key
            )
        except Exception:
            classification = {}
        if classification:
            sector_agg: dict[str, float] = {}
            country_agg: dict[str, float] = {}
            for row in invest_rows:
                code = row.get("code")
                if not code or code not in classification:
                    continue
                val = float(row["valuation"] or 0)
                if val <= 0:
                    continue
                info = classification[code]
                sector = info.get("sector") or "Autre"
                country = info.get("country") or "Inconnu"
                sector_agg[sector] = sector_agg.get(sector, 0.0) + val
                country_agg[country] = country_agg.get(country, 0.0) + val
            if sector_agg:
                sector_items = sorted(
                    sector_agg.items(), key=lambda x: x[1], reverse=True
                )
                sector_treemap = treemap(sector_items, unit=symbol)
            if country_agg:
                country_items = sorted(
                    country_agg.items(), key=lambda x: x[1], reverse=True
                )
                country_treemap = treemap(country_items, unit=symbol)

    # A healthy Powens connection has no state and no error message. States that name
    # a user action are called out separately: a "Synchroniser" button on those can
    # never succeed, because the bank is waiting on the user, not on us.
    conns = []
    for connection in connections:
        name = (
            connection.connector.name
            if connection.connector and connection.connector.name
            else "Banque"
        )
        state = connection.state or ""
        # Never the raw error_message: on a webauth connector it is the bank's whole
        # authorize URL, ``state`` token included.
        message = STATE_LABELS.get(state) or state or (
            "Erreur signalée par la banque" if connection.error_message else ""
        )
        conns.append(
            {
                "id": connection.id,
                "name": name,
                "state": state,
                "nb_accounts": len(connection.accounts),
                "last_update": (
                    connection.last_update.strftime("%d/%m/%Y %H:%M")
                    if connection.last_update
                    else "—"
                ),
                "ok": not message,
                "needs_user": (connection.state or "") in USER_ACTION_STATES,
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
            "total_assets": total_assets,
            "total_debt": total_debt,
            "debt_families": debts,
            "net_currency": base_currency,
            "net_diff": net_diff,
            "net_diff_pct": net_diff_pct,
            "diff_since": diff_since,
            "diff_label": diff_label,
            "net_chart": net_chart,
            "history_points": len(history),
            "allocation": allocation,
            "n_accounts": len(accounts),
            "n_connections": len(connections),
            "families": families,
            "donut": donut,
            "account_donut": account_donut,
            "connections": conns,
            "invest_rows": invest_rows,
            "invest_diff": invest_diff,
            "sector_treemap": sector_treemap,
            "country_treemap": country_treemap,
            "foreign_accounts": foreign,
            "foreign_totals": foreign_totals,
            "has_accounts": bool(accounts_list.accounts),
            "today": _today_fr(),
            "period": period.lower(),
            "type_filter": type_filter,
            "institution": institution_filter,
            "group": group,
            "family_names": family_names,
            "connection_names": connection_names,
        },
    )
