"""FastAPI application wiring: lifespan, static, routers, error handling,
Webview connect flow."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from pypowens import PowensAPIError, PowensAuthError, PowensClient, PowensRateLimitError

from . import (
    accounts,
    analysis,
    detail,
    frequency,
    imports,
    investments,
    recap,
    recurring,
    store,
    synthese,
    transactions,
)
from .config import Settings, get_settings
from .data import clear_cache
from .deps import get_client
from .deps import get_settings as settings_dep
from .state import bootstrap_client, try_renew
from .web import templates

# Query flag marking a request already replayed after a token renewal, so a
# permanently invalid token cannot bounce the browser in a redirect loop.
_RETRY_FLAG = "_retried"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.store = store.connect(settings.db_path)
    app.state.client = await bootstrap_client(settings)
    try:
        yield
    finally:
        await app.state.client.aclose()
        app.state.store.close()


app = FastAPI(title="Powens Finance", lifespan=lifespan)
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
    name="static",
)

app.include_router(synthese.router)
app.include_router(accounts.router)
app.include_router(recap.router)
app.include_router(detail.router)
app.include_router(frequency.router)
app.include_router(recurring.router)
app.include_router(analysis.router)
app.include_router(transactions.router)
app.include_router(investments.router)
app.include_router(imports.router)


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
async def powens_auth_error(request: Request, exc: PowensAuthError) -> HTMLResponse:
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
    )
    return RedirectResponse(url)


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
        # Some setups hand back an authorization code to swap for a permanent token.
        await client.exchange_code(code)
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
