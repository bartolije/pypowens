"""Page d'import de relevés (``/import``).

Le parsing vit dans :mod:`app.importer`, la persistance dans :mod:`app.store` ; ce
module ne fait que les relier à un formulaire et rendre le résultat lisible.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from pypowens import PowensAPIError, PowensClient

from . import store
from .data import clear_cache, load_accounts
from .deps import get_client, get_store
from .importer import ImportError_, parse_statement
from .web import templates

router = APIRouter()

# Un relevé, même sur plusieurs années, pèse quelques dizaines de Ko. Au-delà, c'est
# une erreur de fichier : autant le dire que de parser 200 Mo.
MAX_BYTES = 5 * 1024 * 1024


async def _powens_accounts(client: PowensClient) -> list[dict[str, object]]:
    """Comptes Powens proposés comme cible de rattachement.

    ``conn`` n'est pas passé à dessein : seuls les comptes de l'agrégateur peuvent
    accueillir un relevé. Une API muette ne doit pas empêcher d'importer un fichier, donc
    l'échec se traduit par une liste vide plutôt que par une page en erreur.
    """
    try:
        accounts = await load_accounts(client)
    except PowensAPIError:
        return []
    return [
        {"id": a.id, "label": f"{a.name or f'#{a.id}'} — {a.balance} {a.currency or ''}".strip()}
        for a in sorted(accounts.accounts, key=lambda a: (a.type or "", a.name or ""))
    ]


async def _page(
    request: Request,
    conn: sqlite3.Connection,
    client: PowensClient,
    *,
    message: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "import.html",
        {
            "request": request,
            "active": "import",
            "accounts": store.imported_summary(conn),
            "powens_accounts": await _powens_accounts(client),
            "message": message,
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/import", response_class=HTMLResponse)
async def import_page(
    request: Request,
    conn: sqlite3.Connection = Depends(get_store),  # noqa: B008
    client: PowensClient = Depends(get_client),  # noqa: B008
):
    return await _page(request, conn, client)


@router.post("/import", response_class=HTMLResponse)
async def import_statement(
    request: Request,
    conn: sqlite3.Connection = Depends(get_store),  # noqa: B008
    client: PowensClient = Depends(get_client),  # noqa: B008
    fichier: UploadFile = File(...),
    libelle: str = Form(...),
    type_compte: str = Form(default="checking"),
    devise: str = Form(default="EUR"),
):
    """Parse le relevé et l'enregistre sous le compte ``libelle``.

    Le compte est identifié par son libellé : réimporter un autre extrait du même
    relevé alimente le même compte, et l'empreinte de chaque ligne écarte les
    opérations déjà connues (les exports se recouvrent presque toujours).
    """
    payload = await fichier.read()
    if not payload:
        return await _page(request, conn, client, error="Fichier vide.", status_code=400)
    if len(payload) > MAX_BYTES:
        return await _page(
            request,
            conn,
            client,
            error=f"Fichier trop volumineux ({len(payload) // 1024} Ko, maximum "
            f"{MAX_BYTES // 1024} Ko).",
            status_code=400,
        )

    db_id = store.upsert_imported_account(conn, libelle, type=type_compte, currency=devise)
    try:
        parsed = parse_statement(payload, account_id=store.account_id(db_id))
    except ImportError_ as exc:
        return await _page(request, conn, client, error=str(exc), status_code=400)

    added, duplicates = store.save_imported(
        conn,
        db_id,
        parsed.transactions,
        parsed.fingerprints,
        source=fichier.filename or "relevé.csv",
    )
    # Les comptes importés entrent dans le total disponible : le cache doit tomber.
    clear_cache()

    span = ""
    if parsed.first_date and parsed.last_date:
        span = (
            f" du {parsed.first_date.strftime('%d/%m/%Y')} "
            f"au {parsed.last_date.strftime('%d/%m/%Y')}"
        )
    details = [f"{added} opération{'s' if added > 1 else ''} ajoutée{'s' if added > 1 else ''}"]
    if duplicates:
        details.append(f"{duplicates} déjà connue{'s' if duplicates > 1 else ''}")
    if parsed.skipped:
        details.append(f"{parsed.skipped} ligne{'s' if parsed.skipped > 1 else ''} ignorée")
    return await _page(
        request, conn, client, message=f"{libelle}{span} : " + ", ".join(details) + "."
    )


@router.post("/import/supprimer/{db_id}")
async def delete_import(
    db_id: int,
    conn: sqlite3.Connection = Depends(get_store),  # noqa: B008
) -> RedirectResponse:
    """Supprime un compte importé et ses opérations (local uniquement)."""
    store.delete_imported_account(conn, db_id)
    clear_cache()
    return RedirectResponse("/import", status_code=303)


@router.post("/import/rattacher/{db_id}")
async def link_import(
    db_id: int,
    conn: sqlite3.Connection = Depends(get_store),  # noqa: B008
    compte_powens: str = Form(default=""),
) -> RedirectResponse:
    """Rattache le compte importé à un compte Powens, ou le détache si le champ est vide.

    Le rattachement ne touche à aucune opération : c'est la lecture qui borne le relevé à
    ce que le connecteur ne couvre pas. Se tromper de compte se corrige donc en changeant
    la cible, sans rien réimporter.
    """
    target = compte_powens.strip()
    store.link_imported_account(conn, db_id, int(target) if target else None)
    clear_cache()
    return RedirectResponse("/import", status_code=303)
