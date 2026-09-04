"""Page « Réglages » (``/reglages``) : ce qui se pilotait jusqu'ici par .env.

Changer la devise de référence ou la fenêtre d'historique demandait d'éditer un
fichier puis de relancer l'application. Ces réglages vivent désormais en base,
avec l'environnement pour valeur par défaut : un champ vidé revient au .env.

La page rassemble aussi ce qui était éparpillé — budgets, catégories forcées,
renommages de comptes, fusions de marchands — parce que ce sont toutes des
décisions de l'utilisateur, et qu'on veut pouvoir les revoir au même endroit.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from pypowens import PowensClient

from . import enrich, store
from .config import OVERRIDABLE, apply_overrides, get_settings
from .data import clear_cache, load_all_accounts
from .deps import get_client, get_store
from .health import reactivate_pinned_accounts
from .web import templates

router = APIRouter()


def _rows(request: Request, conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Un réglage par ligne : sa valeur effective, et d'où elle vient."""
    overrides = store.settings_overrides(conn)
    effective = asdict(request.app.state.settings)
    defaults = asdict(get_settings())  # ce que dirait le .env seul
    return [
        {
            "key": key,
            "label": label,
            "value": effective.get(key),
            "default": defaults.get(key),
            "custom": key in overrides,
        }
        for key, (label, _) in OVERRIDABLE.items()
    ]


async def _pinnable(client: PowensClient, conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """``(signature, libellé)`` des comptes connus pas encore épinglés — désactivés compris.

    Best effort : si Powens ne répond pas, la liste est vide et la page rend quand même.
    """
    try:
        accounts = (await load_all_accounts(client)).accounts
    except Exception:  # noqa: BLE001
        return []
    pinned = store.pinned_accounts(conn)
    out: list[tuple[str, str]] = []
    for account in accounts:
        if account.raw.get("deleted"):
            continue
        signature = store.account_signature(account)
        if signature is None or signature in pinned:
            continue
        state = " — désactivé" if account.raw.get("disabled") else ""
        out.append((signature, f"{account.name or 'Compte'} ({account.type or '?'}){state}"))
    return sorted(out, key=lambda item: item[1])


@router.get("/reglages", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    client: PowensClient = Depends(get_client),  # noqa: B008
    conn: sqlite3.Connection = Depends(get_store),  # noqa: B008
) -> Response:
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "request": request,
            "active": "reglages",
            "rows": _rows(request, conn),
            "budgets": sorted(store.budgets(conn).items()),
            "overrides": sorted(store.all_overrides(conn).items()),
            "aliases": sorted(store.account_aliases(conn).items()),
            "merges": sorted(store.merchant_aliases(conn).items()),
            "perimeter_acks": sorted(store.acknowledged_perimeter_days(conn).items()),
            "pins": sorted(store.pinned_accounts(conn).items()),
            "pinnable": await _pinnable(client, conn),
            "categories": enrich.all_categories(),
            "db_path": request.app.state.settings.db_path,
        },
    )


@router.post("/reglages")
async def save_settings(
    request: Request,
    conn: sqlite3.Connection = Depends(get_store),  # noqa: B008
) -> Response:
    """Enregistre les réglages soumis, puis reconstruit ceux de l'application.

    Un champ vidé retire la surcharge : le .env reprend la main.
    """
    form = await request.form()
    for key in OVERRIDABLE:
        if key in form:
            store.set_setting(conn, key, str(form[key]))
    request.app.state.settings = apply_overrides(get_settings(), store.settings_overrides(conn))
    # La fenêtre d'historique et la devise changent ce que les loaders doivent
    # ramener : repartir d'un cache vide plutôt que de servir l'ancien périmètre.
    clear_cache()
    return RedirectResponse("/reglages?enregistre=1", status_code=303)


@router.post("/reglages/epingler")
async def pin_account(
    signature: str = Form(...),
    client: PowensClient = Depends(get_client),  # noqa: B008
    conn: sqlite3.Connection = Depends(get_store),  # noqa: B008
) -> RedirectResponse:
    """Épingle un compte sans attendre qu'il soit désactivé : Powens le
    désactive et le recrée au gré de ses pannes, l'épingle le fait revenir
    dans le total à chaque fois. S'il est désactivé à l'instant, il est
    réintégré tout de suite."""
    accounts = (await load_all_accounts(client)).accounts
    account = next((a for a in accounts if store.account_signature(a) == signature), None)
    if account is not None:
        store.pin_account(conn, signature, account.name)
        await reactivate_pinned_accounts(client, conn)
    return RedirectResponse("/reglages?epingle=1", status_code=303)


@router.post("/reglages/oublier")
async def forget_override(
    label: str = Form(...),
    quoi: str = Form(...),
    conn: sqlite3.Connection = Depends(get_store),  # noqa: B008
) -> RedirectResponse:
    """Retire une décision mémorisée (catégorie forcée, renommage, fusion)."""
    if quoi == "categorie":
        store.clear_override(conn, label)
    elif quoi == "renommage":
        store.set_account_alias(conn, int(label), "")
    elif quoi == "fusion":
        store.set_merchant_alias(conn, label, "")
        enrich.set_merchant_aliases(store.merchant_aliases(conn))
    elif quoi == "budget":
        store.set_budget(conn, label, None)
    elif quoi == "perimetre":
        store.forget_perimeter_ack(conn, label)
    elif quoi == "epingle":
        store.unpin_account(conn, label)
    clear_cache()
    return RedirectResponse("/reglages", status_code=303)
