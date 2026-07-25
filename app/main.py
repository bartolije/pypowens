"""FastAPI application wiring: lifespan, static, routers, error handling,
Webview connect flow."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from pypowens import PowensAPIError, PowensAuthError, PowensClient, PowensRateLimitError

from . import analysis, frequency, recap, recurring, store, transactions
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

app.include_router(recap.router)
app.include_router(frequency.router)
app.include_router(recurring.router)
app.include_router(analysis.router)
app.include_router(transactions.router)


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


@app.get("/connect")
async def connect(
    client: PowensClient = Depends(get_client),
    settings: Settings = Depends(settings_dep),
) -> RedirectResponse:
    """Start the Powens Webview connect flow to link a new bank."""
    code = await client.get_temporary_code()
    url = client.build_webview_url(settings.redirect_uri, code["code"])
    return RedirectResponse(url)


@app.get("/callback", response_model=None)
async def callback(
    request: Request,
    client: PowensClient = Depends(get_client),
    connection_id: int | None = None,
    code: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> Response:
    """Return point of the Webview.

    Powens can come back three ways: success (often with ``connection_id``), with an
    ``authorization code`` to exchange, or with an ``error``. Previously all three
    landed on the same silent redirect, so a failed connection looked successful.
    """
    if error:
        return _error_page(
            request,
            status_code=400,
            title="Connexion bancaire non aboutie",
            detail=error_description or "Le Webview Powens a renvoyé une erreur.",
            hints=[
                "Reprendre depuis « + Banque » pour relancer le parcours.",
                "Un abandon dans le parcours bancaire produit aussi cette page.",
            ],
            technical=error,
        )

    if code:
        # Some setups hand back an authorization code to swap for a permanent token.
        await client.exchange_code(code)

    clear_cache()
    target = "/?connected=1"
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
    return RedirectResponse("/?synced=1", status_code=303)
