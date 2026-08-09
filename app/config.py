"""Application configuration, loaded from environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load the repo-root .env once at import time.
_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    domain: str
    client_id: str | None
    client_secret: str | None
    access_token: str | None
    host: str = "127.0.0.1"
    port: int = 8000
    # Sliding window (months) used when loading transactions for analysis.
    # 36 months, not 24: a biennial series needs more than two years of history to
    # show a second occurrence, and a yearly one needs three points to be credible.
    history_months: int = 36
    # Reference currency: net worth only sums accounts held in it (no FX rates
    # are fetched, so mixing currencies in a single total would be wrong).
    base_currency: str = "EUR"
    # OpenFIGI API key for investment classification (sector/country enrichment).
    # Optional: without it, classification is skipped silently.
    openfigi_api_key: str | None = None

    @property
    def redirect_uri(self) -> str:
        return f"http://{self.host}:{self.port}/callback"

    @property
    def state_path(self) -> Path:
        return _REPO_ROOT / ".powens_state.json"

    @property
    def db_path(self) -> Path:
        """Local SQLite store (balance history, overrides, series state)."""
        return Path(os.environ.get("APP_DB_PATH") or _REPO_ROOT / ".powens_finance.db")


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"}


def _check_host(host: str) -> str:
    """Refuse to serve bank data on a non-loopback interface without opting in.

    The app has no authentication whatsoever: binding it to 0.0.0.0 exposes every
    balance and transaction to the local network. ``APP_ALLOW_REMOTE=1`` overrides
    this, for someone who knowingly puts an authenticating proxy in front.
    """
    if host in _LOOPBACK_HOSTS:
        return host
    if (os.environ.get("APP_ALLOW_REMOTE") or "").strip().lower() in {"1", "true", "yes"}:
        return host
    raise RuntimeError(
        f"APP_HOST={host!r} would expose the app (and all your bank data) beyond this "
        "machine, and it has no authentication. Use 127.0.0.1, or set APP_ALLOW_REMOTE=1 "
        "if it sits behind an authenticating reverse proxy."
    )


def get_settings() -> Settings:
    domain = os.environ.get("POWENS_DOMAIN")
    if not domain:
        raise RuntimeError(
            "POWENS_DOMAIN is not set. Copy .env.example to .env and fill it in."
        )
    return Settings(
        domain=domain,
        client_id=os.environ.get("POWENS_CLIENT_ID") or None,
        client_secret=os.environ.get("POWENS_CLIENT_SECRET") or None,
        access_token=(os.environ.get("POWENS_ACCESS_TOKEN") or "").strip() or None,
        host=_check_host((os.environ.get("APP_HOST") or "127.0.0.1").strip()),
        port=int(os.environ.get("APP_PORT", "8000")),
        history_months=int(os.environ.get("APP_HISTORY_MONTHS", "36")),
        base_currency=(os.environ.get("APP_BASE_CURRENCY") or "EUR").strip().upper(),
        openfigi_api_key=os.environ.get("OPENFIGI_API_KEY") or None,
    )
