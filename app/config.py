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
    history_months: int = 24

    @property
    def redirect_uri(self) -> str:
        return f"http://{self.host}:{self.port}/callback"

    @property
    def state_path(self) -> Path:
        return _REPO_ROOT / ".powens_state.json"


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
        host=os.environ.get("APP_HOST", "127.0.0.1"),
        port=int(os.environ.get("APP_PORT", "8000")),
        history_months=int(os.environ.get("APP_HISTORY_MONTHS", "24")),
    )
