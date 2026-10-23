"""Page de connexion : le formulaire, la page du code, la déconnexion.

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
        {
            "request": request,
            "active": None,
            "suite": suite,
            "error": error,
        },
        status_code=status,
    )


def _refused(request: Request, *, suite: str, error: str) -> Response:
    """Retour à la première page : le défi éventuel est servi, on l'efface."""
    response = _page(request, suite=suite, error=error, status=401)
    auth.close_challenge(response)
    return response


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
    delay = auth.retry_after(key) or auth.account_retry_after(username)
    if delay:
        minutes = max(1, round(delay / 60))
        return _page(
            request,
            suite=suite,
            error=f"Trop de tentatives. Réessayez dans {minutes} minute(s).",
            status=429,
        )

    password_ok = auth.credentials_match((username, password), expected)

    if auth.mfa_enabled():
        # Juste ou faux, le mot de passe mène à la page du code : le verdict
        # reste côté serveur (cf. ``auth._Challenges``). Chaque passage compte
        # comme un échec jusqu'à la connexion complète, qui remet les compteurs
        # à zéro : sinon le frein, lui, dirait quels mots de passe étaient
        # justes. Et chaque essai de code coûte toujours une tentative.
        auth.record_failure(key)
        auth.record_account_failure(username)
        response = RedirectResponse(auth.LOGIN_CODE_PATH, status_code=303)
        auth.open_challenge(response, request, password_ok=password_ok, suite=suite)
        return response

    if not password_ok:
        auth.record_failure(key)
        auth.record_account_failure(username)
        return _page(request, suite=suite, error="Identifiants incorrects.", status=401)

    auth.clear_failures(key)
    auth.clear_account_failures(username)
    response = RedirectResponse(_target(suite), status_code=303)
    auth.start_session(response, request, expected[0])
    return response


@router.get(auth.LOGIN_CODE_PATH, response_class=HTMLResponse)
async def code_page(request: Request) -> Response:
    """Seconde page : le code seul. Sans défi en cours, retour à la première."""
    if auth_credentials() is None or auth.current_user(request) is not None:
        return RedirectResponse("/", status_code=303)
    if not auth.challenge_pending(request):
        return RedirectResponse(auth.LOGIN_PATH, status_code=303)
    return templates.TemplateResponse(
        request, "login_code.html", {"request": request, "active": None}
    )


@router.post(auth.LOGIN_CODE_PATH, response_class=HTMLResponse)
async def code_submit(request: Request, code: str = Form(default="")) -> Response:
    expected = auth_credentials()
    if expected is None:
        return RedirectResponse("/", status_code=303)

    challenge = auth.take_challenge(request)
    if challenge is None:
        return _refused(request, suite="", error="Connexion expirée : recommencez.")
    password_ok, suite = challenge

    # Vérifié même après un mot de passe faux, comme quand les deux facteurs
    # partageaient un formulaire : le code est consommé dans tous les cas et le
    # refus suit le même chemin.
    code_ok = auth.verify_totp(getattr(request.app.state, "store", None), code)
    if not (password_ok and code_ok):
        # Un message unique : dire lequel des facteurs est faux dirait à un
        # attaquant que le mot de passe, lui, est bon — et qu'il ne reste que
        # six chiffres à trouver.
        return _refused(request, suite=suite, error="Identifiants ou code incorrects.")

    auth.clear_failures(auth._client_key(request))
    auth.clear_account_failures(expected[0])
    response = RedirectResponse(_target(suite), status_code=303)
    auth.start_session(response, request, expected[0])
    auth.close_challenge(response)
    return response


@router.post("/deconnexion")
async def logout(request: Request) -> Response:
    """Ferme la session. En POST : un GET serait déclenchable par une image."""
    response = RedirectResponse(auth.LOGIN_PATH, status_code=303)
    # Avec la requête : la déconnexion révoque le jeton côté serveur, elle ne se
    # contente pas de l'oublier côté navigateur.
    auth.end_session(response, request)
    return response
