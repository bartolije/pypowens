"""Application configuration, loaded from environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
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
    # Jours sans synchro au-delà desquels une connexion saine est dite « muette ».
    silent_after_days: int = 3
    # Indice de référence de la page performance (ticker Yahoo Finance).
    # IWDA.AS = iShares Core MSCI World, coté en EUR à Amsterdam.
    benchmark_ticker: str = "IWDA.AS"
    benchmark_label: str = "MSCI World (IWDA)"

    @property
    def redirect_uri(self) -> str:
        return f"http://{self.host}:{self.port}/callback"

    @property
    def state_path(self) -> Path:
        """Persisted Powens id_user + token (overridable, notably for tests)."""
        return Path(os.environ.get("APP_STATE_PATH") or _REPO_ROOT / ".powens_state.json")

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
        silent_after_days=max(1, int(os.environ.get("APP_SILENT_DAYS", "3"))),
        benchmark_ticker=(os.environ.get("APP_BENCHMARK_TICKER") or "IWDA.AS").strip(),
        benchmark_label=(os.environ.get("APP_BENCHMARK_LABEL") or "MSCI World (IWDA)").strip(),
    )


# Réglages modifiables depuis l'interface : (clé, libellé, type). Tout ce qui
# n'est PAS ici reste piloté par l'environnement — soit parce que c'est un
# secret, soit parce qu'il est nécessaire avant l'ouverture de la base.
OVERRIDABLE: dict[str, tuple[str, type]] = {
    "base_currency": ("Devise de référence", str),
    "history_months": ("Fenêtre d'historique (mois)", int),
    "silent_after_days": ("Connexion muette au-delà de (jours)", int),
    "benchmark_ticker": ("Indice de comparaison (ticker Yahoo)", str),
    "benchmark_label": ("Nom affiché de l'indice", str),
}


def apply_overrides(settings: Settings, overrides: dict[str, str]) -> Settings:
    """Applique les réglages de la base par-dessus ceux de l'environnement.

    Une valeur illisible est ignorée plutôt que fatale : un réglage mal saisi
    ne doit pas empêcher l'application de démarrer.
    """
    texts: dict[str, str] = {}
    numbers: dict[str, int] = {}
    for key, (_, kind) in OVERRIDABLE.items():
        raw = (overrides.get(key) or "").strip()
        if not raw:
            continue
        if kind is int:
            try:
                numbers[key] = int(raw)
            except ValueError:
                continue  # saisie illisible : le défaut reste en place
        else:
            texts[key] = raw

    if "base_currency" in texts:
        texts["base_currency"] = texts["base_currency"].upper()
    # Bornes de bon sens : une fenêtre de 9 999 mois téléchargerait tout
    # l'historique Powens, un seuil de 0 jour crierait au loup en permanence.
    if "history_months" in numbers:
        numbers["history_months"] = max(1, min(120, numbers["history_months"]))
    if "silent_after_days" in numbers:
        numbers["silent_after_days"] = max(1, numbers["silent_after_days"])

    if not texts and not numbers:
        return settings
    return replace(settings, **texts, **numbers)  # type: ignore[arg-type]
