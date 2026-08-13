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

from . import enrich, store
from .config import OVERRIDABLE, apply_overrides, get_settings
from .data import clear_cache
from .deps import get_store
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


@router.get("/reglages", response_class=HTMLResponse)
async def settings_page(
    request: Request,
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
    request.app.state.settings = apply_overrides(
        get_settings(), store.settings_overrides(conn)
    )
    # La fenêtre d'historique et la devise changent ce que les loaders doivent
    # ramener : repartir d'un cache vide plutôt que de servir l'ancien périmètre.
    clear_cache()
    return RedirectResponse("/reglages?enregistre=1", status_code=303)


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
    clear_cache()
    return RedirectResponse("/reglages", status_code=303)
