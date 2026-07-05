"""FastAPI application wiring: lifespan, static, routers, Webview connect flow."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from pypowens import PowensClient

from . import analysis, frequency, recap, recurring
from .config import Settings, get_settings
from .data import clear_cache
from .deps import get_client
from .deps import get_settings as settings_dep
from .state import bootstrap_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.client = await bootstrap_client(settings)
    try:
        yield
    finally:
        await app.state.client.aclose()


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


@app.get("/connect")
async def connect(
    client: PowensClient = Depends(get_client),
    settings: Settings = Depends(settings_dep),
) -> RedirectResponse:
    """Start the Powens Webview connect flow to link a new bank."""
    code = await client.get_temporary_code()
    url = client.build_webview_url(settings.redirect_uri, code["code"])
    return RedirectResponse(url)


@app.get("/callback")
async def callback() -> RedirectResponse:
    """Return point of the Webview: refresh caches and go back to the recap."""
    clear_cache()
    return RedirectResponse("/?connected=1")
