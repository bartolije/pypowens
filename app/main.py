"""FastAPI application wiring: lifespan, static, routers, error handling,
Webview connect flow."""

from __future__ import annotations

import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from pypowens import PowensAPIError, PowensAuthError, PowensClient, PowensRateLimitError

from . import (
    accounts,
    analysis,
    connections,
    detail,
    enrich,
    frequency,
    imports,
    investments,
    recap,
    recurring,
    settings_page,
    store,
    synthese,
    transactions,
)
from .config import Settings, apply_overrides, get_settings
from .data import clear_cache
from .deps import get_client
from .deps import get_settings as settings_dep
from .health import auto_sync_stuck_connections, connection_alerts
from .state import bootstrap_client, persist_token, try_renew
from .web import templates

# Query flag marking a request already replayed after a token renewal, so a
# permanently invalid token cannot bounce the browser in a redirect loop.
_RETRY_FLAG = "_retried"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # No-op si le point d'entrée (python -m app) a déjà configuré le logging ;
    # couvre le lancement direct par ``uvicorn app.main:app``.
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s"
    )
    settings = get_settings()
    app.state.settings = settings
    app.state.store = store.connect(settings.db_path)
    # Les réglages de l'interface l'emportent sur le .env : on ne peut les lire
    # qu'ICI, la base venant tout juste d'être ouverte (son chemin, lui, reste
    # forcément un réglage d'environnement).
    app.state.settings = apply_overrides(settings, store.settings_overrides(app.state.store))
    # Les fusions de marchands vivent dans enrich (module pur) : les hydrater
    # depuis le store au démarrage, puis à chaque édition (route /marchands).
    enrich.set_merchant_aliases(store.merchant_aliases(app.state.store))
    try:
        app.state.client = await bootstrap_client(settings)
    except BaseException:
        # Sans quoi un bootstrap raté (réseau, state corrompu) laisserait la
        # connexion SQLite ouverte derrière un « Application startup failed ».
        app.state.store.close()
        raise
    try:
        yield
    finally:
        await app.state.client.aclose()
        app.state.store.close()


app = FastAPI(title="Powens Finance", lifespan=lifespan)

# Hôtes loopback seulement ("testserver" est le TestClient). Un domaine hostile
# résolvant vers 127.0.0.1 (DNS rebinding) devenait same-origin et pouvait LIRE
# soldes et transactions — l'app n'ayant aucune authentification. Derrière un
# proxy authentifiant (APP_ALLOW_REMOTE=1), le Host est celui du proxy : à lui
# de filtrer.
_LOCAL_HOSTS = ["127.0.0.1", "localhost", "::1", "testserver"]
if not (os.environ.get("APP_ALLOW_REMOTE") or "").strip():
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_LOCAL_HOSTS)


@app.middleware("http")
async def _reject_cross_site_posts(request: Request, call_next):
    """Refuse les POST issus d'une autre origine (CSRF sans cookie ni token).

    Les 5 POST de l'app (catégorie, import, suppression, rattachement, synchro)
    n'ont aucune protection propre et l'app aucune authentification : n'importe
    quelle page web ouverte dans le navigateur pouvait soumettre un formulaire
    vers http://127.0.0.1:8000/… Les navigateurs modernes joignent toujours
    ``Origin`` aux POST cross-site ; une requête sans Origin (curl, tests) est
    locale par construction.
    """
    if request.method == "POST" and not (os.environ.get("APP_ALLOW_REMOTE") or "").strip():
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin:
            host = urlsplit(origin).hostname
            if host not in ("127.0.0.1", "localhost", "::1", "testserver"):
                return PlainTextResponse(
                    "Requête cross-site refusée : cette application n'accepte que "
                    "les formulaires émis par ses propres pages.",
                    status_code=403,
                )
    return await call_next(request)


@app.middleware("http")
async def _health_banner(request: Request, call_next):
    """Alerte de santé des connexions sur TOUTES les pages (bandeau du layout).

    Un compte peut sortir du patrimoine du jour au lendemain (connexion en
    panne → compte désactivé) : tant que l'alerte ne vivait que sur
    /patrimoine, le chiffre des autres pages mentait sans signal. Ne doit
    JAMAIS casser une page : toute erreur ici rend simplement un bandeau vide.
    Les loaders sont cachés (TTL 120/300 s) — coût nul en croisière.
    """
    request.state.health_alerts = []
    if request.method == "GET" and not request.url.path.startswith(
        ("/static", "/export", "/callback")
    ):
        client = getattr(request.app.state, "client", None)
        conn = getattr(request.app.state, "store", None)
        if client is not None:
            try:
                request.state.health_alerts = await connection_alerts(client, conn)
                # Au plus une fois par tranche de 6 h : relancer les connexions
                # BLOQUÉES (saines, >24 h sans synchro, aucun next_try planifié).
                # Ouvrir l'app le matin suffit alors à réveiller une Trade
                # Republic figée, sans jamais toucher une connexion en erreur.
                last = getattr(request.app.state, "auto_sync_at", None)
                now = time.monotonic()
                if last is None or (now - last) > 6 * 3600:
                    request.app.state.auto_sync_at = now
                    if await auto_sync_stuck_connections(client):
                        clear_cache()
            except Exception:  # noqa: BLE001 — bandeau best-effort, jamais bloquant
                logging.getLogger(__name__).debug(
                    "bandeau de santé indisponible", exc_info=True
                )
    return await call_next(request)


app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
    name="static",
)

app.include_router(synthese.router)
app.include_router(accounts.router)
app.include_router(connections.router)
app.include_router(recap.router)
app.include_router(detail.router)
app.include_router(frequency.router)
app.include_router(recurring.router)
app.include_router(analysis.router)
app.include_router(transactions.router)
app.include_router(investments.router)
app.include_router(imports.router)
app.include_router(settings_page.router)


# --------------------------------------------------------------- error handling


def _error_page(
    request: Request,
    *,
    status_code: int,
    title: str,
    detail: str,
    hints: list[str] | None = None,
    technical: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "request": request,
            "active": None,
            "title": title,
            "detail": detail,
            "hints": hints or [],
            "technical": technical,
            "retry_url": str(request.url),
        },
        status_code=status_code,
    )


@app.exception_handler(PowensAuthError)
async def powens_auth_error(request: Request, exc: PowensAuthError) -> Response:
    """A 401/403 means the token died. Try to renew it once, then replay the page."""
    already_retried = request.query_params.get(_RETRY_FLAG) == "1"
    renewed = not already_retried and await try_renew(
        request.app.state.client, request.app.state.settings
    )
    if renewed:
        clear_cache()
        return RedirectResponse(
            str(request.url.include_query_params(**{_RETRY_FLAG: "1"})), status_code=303
        )

    return _error_page(
        request,
        status_code=401,
        title="Accès Powens refusé",
        detail="Le token d'accès n'est plus valide et n'a pas pu être renouvelé.",
        hints=[
            "Vérifier POWENS_CLIENT_ID / POWENS_CLIENT_SECRET dans .env.",
            "Si POWENS_ACCESS_TOKEN est renseigné dans .env, le régénérer depuis la "
            "console Powens ou le retirer pour laisser l'app en créer un.",
            "Un token révoqué côté console ne peut pas être renouvelé : supprimer "
            ".powens_state.json pour repartir d'un utilisateur neuf.",
        ],
        technical=str(exc),
    )


@app.exception_handler(PowensRateLimitError)
async def powens_rate_limit(request: Request, exc: PowensRateLimitError) -> HTMLResponse:
    wait = f" Réessayer dans {int(exc.retry_after)} s." if exc.retry_after else ""
    return _error_page(
        request,
        status_code=429,
        title="Powens limite les appels",
        detail=f"L'API a répondu 429 après plusieurs tentatives.{wait}",
        hints=["Les données en cache restent consultables sur les autres pages."],
        technical=str(exc),
    )


@app.exception_handler(PowensAPIError)
async def powens_api_error(request: Request, exc: PowensAPIError) -> HTMLResponse:
    return _error_page(
        request,
        status_code=502,
        title="Erreur côté Powens",
        detail="L'API Powens a renvoyé une réponse inattendue.",
        hints=["Si le problème persiste, vérifier l'état de l'app dans la console Powens."],
        technical=str(exc),
    )


# ------------------------------------------------------------------ connect flow


@app.get("/connect", response_model=None)
async def connect(
    request: Request,
    client: PowensClient = Depends(get_client),
    settings: Settings = Depends(settings_dep),
    connector_id: int | None = None,
) -> Response:
    """Start the Powens Webview connect flow to link a new bank.

    Without ``connector_id`` the Webview shows the full bank list. With it, it opens
    straight on that connector — the way to add a second bank whose name is already
    known (``/connect?connector_id=2663``).

    The app's ``redirect_uri`` is checked against the whitelist first. Powens refuses
    an unlisted one with "the parameter must match the constraints defined in the
    administration console" and never says what it expected, so the check is the
    difference between a dead end and a page naming the value to add. It fails open:
    if the configuration cannot be read, the flow proceeds rather than being blocked
    by its own diagnostic.
    """
    refused = await _redirect_uri_check(request, client, settings)
    if refused is not None:
        return refused

    code = await client.get_temporary_code()
    url = client.build_webview_url(
        settings.redirect_uri,
        code["code"],
        connector_ids=[connector_id] if connector_id else None,
        lang="fr",
        extra={"state": _issue_webview_state(request)},
    )
    return RedirectResponse(url)


def _issue_webview_state(request: Request) -> str:
    """Jeton anti-CSRF du parcours Webview, à usage unique.

    Sans lui, ``/callback?code=…`` acceptait n'importe quel code de n'importe
    quelle origine : un lien piégé suffisait à échanger le code d'un attaquant et
    à faire basculer l'app entière sur SON utilisateur Powens — la prochaine
    banque connectée aurait atterri chez lui.
    """
    token = secrets.token_urlsafe(16)
    request.app.state.webview_state = token
    return token


async def _redirect_uri_check(
    request: Request, client: PowensClient, settings: Settings
) -> HTMLResponse | None:
    """Return an error page when the callback URL is not whitelisted, else ``None``."""
    try:
        config = await client.get_client_config()
    except (PowensAPIError, OSError):
        return None
    if config.allows(settings.redirect_uri):
        return None
    listed = ", ".join(config.redirect_uris) or "(aucune)"
    return _error_page(
        request,
        status_code=409,
        title="redirect_uri non autorisé",
        detail=(
            "Powens refusera le retour du Webview : l'URL de callback de cette app "
            "n'est pas déclarée dans la console."
        ),
        hints=[
            f"Ajouter {settings.redirect_uri} dans la console Powens "
            "(Configuration → Webview → redirect URIs).",
            f"URI actuellement déclarée(s) : {listed}",
            "Ou lancer l'app sur l'hôte/port déjà déclaré via APP_HOST / APP_PORT.",
        ],
        technical=f"client_id={client.client_id} · attendu={settings.redirect_uri}",
    )


@app.get("/reconnecter/{connection_id}", response_model=None)
async def reconnect(
    request: Request,
    connection_id: int,
    client: PowensClient = Depends(get_client),
    settings: Settings = Depends(settings_dep),
) -> Response:
    """Send the user back through the Webview to repair one connection.

    A connection left in ``webauthRequired`` is waiting on the *user* to complete the
    bank's own authentication; no amount of ``update_connection()`` clears it, so the
    "Synchroniser" button on such a connection can only ever look broken.
    """
    refused = await _redirect_uri_check(request, client, settings)
    if refused is not None:
        return refused

    code = await client.get_temporary_code()
    url = client.build_webview_url(
        settings.redirect_uri,
        code["code"],
        connection_id=connection_id,
        flow="reconnect",
        lang="fr",
        extra={"state": _issue_webview_state(request)},
    )
    return RedirectResponse(url)


@app.get("/callback", response_model=None)
async def callback(
    request: Request,
    client: PowensClient = Depends(get_client),
) -> Response:
    """Return point of the Webview.

    Every parameter is read straight off the query string rather than declared as a
    typed argument: Powens varies what it sends by setup and by outcome, and a
    declared ``connection_id: int`` turns an empty ``?connection_id=`` into a 422
    validation error — which reads as "the redirect is broken" with nothing to go on.
    Anything unrecognised is shown on a diagnostic page instead of being swallowed.
    """
    params = dict(request.query_params)
    error = params.get("error") or params.get("error_code")
    code = params.get("code")
    raw_id = (params.get("connection_id") or params.get("id_connection") or "").strip()
    connection_id = int(raw_id) if raw_id.isdigit() else None
    reported = ", ".join(f"{k}={v}" for k, v in params.items() if k != "code") or "(aucun)"

    if error:
        return _error_page(
            request,
            status_code=400,
            title="Connexion bancaire non aboutie",
            detail=params.get("error_description")
            or params.get("error_message")
            or "Le Webview Powens a renvoyé une erreur.",
            hints=[
                "Reprendre depuis « + Banque » pour relancer le parcours.",
                "Un abandon dans le parcours bancaire produit aussi cette page.",
                f"Le redirect_uri attendu par la console Powens est "
                f"{request.app.state.settings.redirect_uri}",
            ],
            technical=f"{error} · paramètres reçus : {reported}",
        )

    if code:
        # Un code ne s'échange que si le retour porte le jeton ``state`` émis par
        # NOTRE départ vers le Webview — voir :func:`_issue_webview_state`.
        expected = getattr(request.app.state, "webview_state", None)
        if not expected or not secrets.compare_digest(params.get("state", ""), expected):
            return _error_page(
                request,
                status_code=400,
                title="Retour du Webview non reconnu",
                detail=(
                    "Ce retour porte un code d'autorisation mais pas le jeton de "
                    "sécurité émis au départ du parcours : il n'est pas échangé."
                ),
                hints=[
                    "Reprendre le parcours depuis « + Banque » (le jeton est à usage unique).",
                    "Ce blocage est attendu si l'URL de callback a été ouverte "
                    "depuis un lien externe.",
                ],
                technical=f"paramètres reçus : {reported}",
            )
        request.app.state.webview_state = None  # usage unique
        token = await client.exchange_code(code)
        # Persister : sinon ce token ne survit pas au redémarrage de l'app.
        if token.access_token:
            persist_token(
                request.app.state.settings,
                access_token=token.access_token,
                id_user=token.id_user,
            )
    elif connection_id is None:
        # Neither a code, nor an id, nor an error: the Webview came back with nothing
        # usable. Say so, with what it did send, rather than claiming success.
        return _error_page(
            request,
            status_code=400,
            title="Retour du Webview incomplet",
            detail="Powens est revenu sans identifiant de connexion ni code à échanger.",
            hints=[
                f"Vérifier que {request.app.state.settings.redirect_uri} est bien "
                "whitelisté dans la console Powens (Webview → redirect URIs).",
                "Vérifier sur /patrimoine si la connexion a malgré tout été créée.",
            ],
            technical=f"paramètres reçus : {reported}",
        )

    clear_cache()
    # Back to /patrimoine: that is where connections and their sync state are shown.
    target = "/patrimoine?connected=1"
    if connection_id is not None:
        target += f"&connection_id={connection_id}"
    return RedirectResponse(target)


@app.post("/synchroniser/{connection_id}")
async def synchronize(
    connection_id: int,
    client: PowensClient = Depends(get_client),
) -> RedirectResponse:
    """Ask Powens to refresh a connection now, then reload the recap."""
    await client.update_connection(connection_id)
    clear_cache()
    return RedirectResponse("/patrimoine?synced=1", status_code=303)


@app.post("/comptes/{account_id}/reactiver")
async def reactivate_account(
    account_id: int,
    client: PowensClient = Depends(get_client),
) -> RedirectResponse:
    """Réintègre un compte désactivé côté Powens dans le patrimoine.

    Après la réparation d'une connexion, Powens peut RECRÉER un compte à l'état
    désactivé : son solde sort de tous les agrégats et rien dans le Webview ne
    le réactive. C'est le bouton « Réintégrer » du bandeau de santé.
    """
    await client.update_account(account_id, disabled=False)
    clear_cache()
    return RedirectResponse("/patrimoine?reactivated=1", status_code=303)
