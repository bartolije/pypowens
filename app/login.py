"""Page de connexion : le formulaire, la session, la déconnexion.

Séparé de ``auth`` (qui porte le middleware) pour que ce module puisse importer
les gabarits sans que l'authentification dépende de Jinja.
"""

from __future__ import annotations

import base64
import binascii

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import auth
from .config import auth_credentials
from .web import templates

router = APIRouter()


def _target(encoded: str | None) -> str:
    """Page demandée avant la connexion, si elle est bien chez nous.

    Le paramètre revient du navigateur : sans ce contrôle, un lien
    ``/connexion?suite=<//site.piege>`` renverrait l'utilisateur ailleurs juste
    après une connexion réussie — au moment précis où il fait confiance à la page.
    """
    if not encoded:
        return "/"
    try:
        path = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return "/"
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    return path


def _page(request: Request, *, suite: str, error: str | None = None, status: int = 200) -> Response:
    return templates.TemplateResponse(
        request,
        "login.html",
        {"request": request, "active": None, "suite": suite, "error": error},
        status_code=status,
    )


@router.get(auth.LOGIN_PATH, response_class=HTMLResponse)
async def login_page(request: Request, suite: str | None = None) -> Response:
    """Formulaire de connexion. Déjà connecté, on ne le montre pas."""
    if auth_credentials() is None or auth.current_user(request) is not None:
        return RedirectResponse(_target(suite), status_code=303)
    return _page(request, suite=suite or "")


@router.post(auth.LOGIN_PATH, response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    suite: str = Form(default=""),
) -> Response:
    expected = auth_credentials()
    if expected is None:
        return RedirectResponse("/", status_code=303)

    key = auth._client_key(request)
    delay = auth.retry_after(key)
    if delay:
        minutes = max(1, round(delay / 60))
        return _page(
            request,
            suite=suite,
            error=f"Trop de tentatives. Réessayez dans {minutes} minute(s).",
            status=429,
        )

    if not auth.credentials_match((username, password), expected):
        auth.record_failure(key)
        # Un message unique : dire lequel des deux champs est faux dirait à un
        # attaquant que l'identifiant, lui, est bon.
        return _page(request, suite=suite, error="Identifiants incorrects.", status=401)

    auth.clear_failures(key)
    response = RedirectResponse(_target(suite), status_code=303)
    auth.start_session(response, request, expected[0])
    return response


@router.post("/deconnexion")
async def logout(request: Request) -> Response:
    """Ferme la session. En POST : un GET serait déclenchable par une image."""
    response = RedirectResponse(auth.LOGIN_PATH, status_code=303)
    auth.end_session(response)
    return response
