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
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from pypowens import PowensClient, Transaction

from . import enrich, store
from .config import Settings
from .data import (
    clear_cache,
    load_accounts,
    load_internal_ids,
    load_spending_transactions,
    load_transactions,
)
from .deps import get_client, get_settings, get_store
from .enrich import all_categories, merchant_key, resolve_category, resolve_category_txn
from .helpers import line_chart, month_key, month_label_fr
from .web import templates

router = APIRouter()


@dataclass
class Line:
    txn: Transaction
    account: str
    amount: Decimal
    is_internal: bool
    category: str = ""  # rempli par /recherche, inutile au drill-down


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
        client, months=months, include_investment=(scope == "all"), conn=conn
    )
    internal = await load_internal_ids(client, months=months, conn=conn)
    accounts = await load_accounts(client, conn=conn)
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
    # L'ensemble des virements internes (cache) dépend des overrides : marquer un
    # libellé « Virement interne » doit prendre effet au rechargement, pas au TTL.
    clear_cache()
    # Une valeur de formulaire ne choisit jamais un domaine : seuls les chemins
    # internes sont suivis ("//" est une URL schéma-relative vers un autre hôte).
    target = back if back.startswith("/") and not back.startswith("//") else "/"
    return RedirectResponse(target, status_code=303)


@router.get("/export.csv")
async def export_csv(
    client: PowensClient = Depends(get_client),
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_store),
) -> Response:
    """Tout l'historique en CSV : dates, comptes, libellés, catégories résolues.

    L'app importe des relevés mais ne restituait rien — les catégories corrigées
    à la main restaient enfermées dans la base locale.
    """
    txns = await load_transactions(client, months=settings.history_months, conn=conn)
    internal = await load_internal_ids(client, months=settings.history_months, conn=conn)
    overrides = store.all_overrides(conn)
    accounts = await load_accounts(client, conn=conn)
    names = {a.id: (a.name or f"#{a.id}") for a in accounts.accounts}

    import csv
    import io

    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(["date", "compte", "libelle", "categorie", "montant", "interne"])
    for t in sorted(txns, key=lambda t: t.date or date.min):
        if t.date is None or t.value is None:
            continue
        writer.writerow(
            [
                t.date.isoformat(),
                names.get(t.id_account, ""),
                t.simplified_wording or t.wording or "",
                resolve_category_txn(t, overrides),
                # Décimale en virgule : le fichier revient dans le même Excel
                # français que les relevés qu'on importe.
                str(t.value).replace(".", ","),
                "oui" if t.id in internal else "",
            ]
        )
    filename = f"transactions-{date.today().isoformat()}.csv"
    return Response(
        # BOM UTF-8 : sans lui, Excel (Windows) lit les accents en mojibake.
        "\ufeff" + out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/marchands/fusionner")
async def merge_merchants(
    source: str = Form(...),
    cible: str = Form(default=""),
    conn: sqlite3.Connection = Depends(get_store),
) -> RedirectResponse:
    """Fusionne un marchand dans un autre (cible vide = défusionner).

    Deux libellés du même marchand (carte « ENSEIGNE/VILLE » vs prélèvement
    « PRLV SEPA ENSEIGNE SAS ») produisent deux clés — donc deux lignes
    d'abonnement, deux historiques, deux corrections de catégorie. La fusion
    est appliquée à la sortie de merchant_key : tout l'aval la voit.
    """
    store.set_merchant_alias(conn, source, cible)
    enrich.set_merchant_aliases(store.merchant_aliases(conn))
    clear_cache()
    target = (cible or source).upper().strip()
    return RedirectResponse(f"/transactions?label={quote(target)}", status_code=303)


@router.get("/recherche", response_class=HTMLResponse)
async def search(
    request: Request,
    q: str = Query(default=""),
    client: PowensClient = Depends(get_client),
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_store),
):
    """Recherche plein texte (et par montant) sur tout l'historique local."""
    query = q.strip()
    results: list[Line] = []
    count = 0
    if query:
        txns = await load_transactions(client, months=settings.history_months, conn=conn)
        accounts = await load_accounts(client, conn=conn)
        names = {a.id: (a.name or f"#{a.id}") for a in accounts.accounts}
        internal = await load_internal_ids(client, months=settings.history_months, conn=conn)
        overrides = store.all_overrides(conn)

        needle = query.upper()
        # « 42,50 » ou « 42.50 » cherche aussi le montant exact (les deux sens).
        amount = None
        try:
            amount = abs(Decimal(query.replace(",", ".").replace(" ", "")))
        except ArithmeticError:
            pass

        def matches(t) -> bool:
            text = f"{t.simplified_wording or ''} {t.wording or ''} {t.original_wording or ''}"
            if needle in text.upper():
                return True
            return amount is not None and t.value is not None and abs(t.value) == amount

        found = [t for t in txns if matches(t)]
        found.sort(key=lambda t: t.date or date.min, reverse=True)
        count = len(found)
        results = [
            Line(
                txn=t,
                account=names.get(t.id_account, "—"),
                amount=t.value or Decimal(0),
                is_internal=t.id in internal,
                category=resolve_category_txn(t, overrides),
            )
            for t in found[:200]
        ]

    return templates.TemplateResponse(
        request,
        "recherche.html",
        {
            "request": request,
            "active": None,
            "q": query,
            "results": results,
            "count": count,
            "truncated": count > 200,
            "merchant_key": merchant_key,
        },
    )
