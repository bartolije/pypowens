"""Application configuration, loaded from environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

# Load the repo-root .env once at import time.
_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env")


def _data_dir() -> Path:
    """Répertoire des fichiers qui doivent survivre à un redéploiement.

    Chez un hébergeur conteneurisé, le système de fichiers est reconstruit à
    chaque déploiement : seul un volume persiste. Deux fichiers en dépendent, et
    les perdre ne se rattrape pas — la base porte un historique de soldes que
    Powens ne conserve pas, et l'état porte le token : sans lui, ``bootstrap``
    crée un NOUVEL utilisateur Powens, donc un compte vierge où plus aucune
    banque n'est connectée.

    Railway renseigne ``RAILWAY_VOLUME_MOUNT_PATH`` de lui-même dès qu'un volume
    est attaché au service : il n'y a aucun chemin à saisir nulle part. En local
    la variable n'existe pas et tout retombe à la racine du dépôt, inchangé.
    """
    mount = (
        os.environ.get("APP_DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or ""
    ).strip()
    return Path(mount) if mount else _REPO_ROOT


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
    # Intervalle de la collecte lancée par le processus web ; 0 la désactive.
    # Local : rien à régler, launchd déclenche déjà `python -m app.collector`.
    # Déployé : c'est le seul déclencheur possible, un volume ne se montant que
    # sur un service (donc pas sur un « cron job » voisin).
    collect_every_hours: float = 0.0

    @property
    def redirect_uri(self) -> str:
        """Adresse de retour du Webview, à déclarer telle quelle dans la console.

        ``host``/``port`` décrivent l'interface d'écoute, ce qui suffit en local
        mais ne veut plus rien dire dans un conteneur : on y écoute ``0.0.0.0``
        sur un port interne, quand la banque doit renvoyer l'utilisateur vers le
        domaine public. Railway publie celui-ci dans ``RAILWAY_PUBLIC_DOMAIN`` ;
        ``APP_PUBLIC_URL`` reste prioritaire, pour un domaine personnalisé ou
        tout autre hébergeur.
        """
        explicit = (os.environ.get("APP_PUBLIC_URL") or "").strip().rstrip("/")
        if explicit:
            return f"{explicit}/callback"
        railway = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip().rstrip("/")
        if railway:
            return f"https://{railway}/callback"
        return f"http://{self.host}:{self.port}/callback"

    @property
    def state_path(self) -> Path:
        """Persisted Powens id_user + token (overridable, notably for tests)."""
        return Path(os.environ.get("APP_STATE_PATH") or _data_dir() / ".powens_state.json")

    @property
    def db_path(self) -> Path:
        """Local SQLite store (balance history, overrides, series state)."""
        return Path(os.environ.get("APP_DB_PATH") or _data_dir() / ".powens_finance.db")


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"}


def auth_credentials() -> tuple[str, str] | None:
    """Identifiants attendus par l'authentification HTTP, ou ``None``.

    Lus à chaque appel plutôt que figés dans ``Settings`` : ce sont des secrets,
    ils n'ont donc rien à faire dans un objet journalisé ou affiché, ni dans les
    réglages modifiables depuis l'interface.
    """
    user = (os.environ.get("APP_AUTH_USER") or "").strip()
    password = os.environ.get("APP_AUTH_PASSWORD") or ""
    return (user, password) if user and password else None


def totp_secret() -> str:
    """Secret TOTP du second facteur (base32). Vide = MFA désactivé."""
    return (os.environ.get("APP_TOTP_SECRET") or "").strip()


def api_token() -> str:
    """Jeton des appels non interactifs (sauvegarde, supervision). Vide = aucun.

    Un script ne peut pas produire de code TOTP : sans porte qui lui soit
    propre, activer le MFA reviendrait soit à casser ``scripts/backup-prod.sh``,
    soit à laisser ouverte une entrée à un seul facteur — donc à décorer le MFA
    plutôt qu'à s'en servir. Ce jeton est cette porte : il n'ouvre rien d'autre
    que ce que le mot de passe ouvrait, mais il se révoque seul, sans toucher au
    mot de passe ni au second facteur.
    """
    return (os.environ.get("APP_API_TOKEN") or "").strip()


# Un secret de signature devinable se force hors ligne : qui le trouve FORGE un
# cookie de session valide, donc entre sans mot de passe et sans jamais voir le
# second facteur (il n'est demandé qu'au formulaire). D'où un plancher, et non
# un simple « non vide ».
_MIN_SECRET_LENGTH = 24
_MIN_SECRET_VARIETY = 8


def session_secret_error() -> str | None:
    """Pourquoi le secret de signature des sessions est trop faible, ou ``None``.

    Deux cas, selon ce qui sert de graine (cf. ``auth._session_key``) :
    ``APP_SESSION_SECRET`` quand elle est posée, sinon le mot de passe lui-même.
    Le message nomme donc la variable réellement en cause.
    """
    explicit = (os.environ.get("APP_SESSION_SECRET") or "").strip()
    variable, value = "APP_SESSION_SECRET", explicit
    if not explicit:
        credentials = auth_credentials()
        if credentials is None:
            return None  # usage local sans authentification : rien à signer
        variable, value = "APP_AUTH_PASSWORD", credentials[1]
    if len(value) < _MIN_SECRET_LENGTH or len(set(value)) < _MIN_SECRET_VARIETY:
        return (
            f"{variable} est trop faible pour signer les sessions "
            f"({len(value)} caractères, {len(set(value))} distincts ; au moins "
            f"{_MIN_SECRET_LENGTH} et {_MIN_SECRET_VARIETY} attendus) : un cookie "
            "de session serait forgeable. Générer avec "
            '`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.'
        )
    return None


def _check_host(host: str) -> str:
    """Refuse to serve bank data on a non-loopback interface without opting in.

    Servir sur autre chose que la loopback expose soldes et transactions ; il y
    faut donc une porte. Deux façons de la fournir : l'authentification intégrée
    (``APP_AUTH_USER``/``APP_AUTH_PASSWORD``), ou ``APP_ALLOW_REMOTE=1`` pour qui
    place sciemment un proxy authentifiant devant. Sans l'une des deux, mieux
    vaut un démarrage qui échoue qu'une app bancaire ouverte à tous.
    """
    if host in _LOOPBACK_HOSTS:
        return host
    if auth_credentials() is not None:
        return host
    if (os.environ.get("APP_ALLOW_REMOTE") or "").strip().lower() in {"1", "true", "yes"}:
        return host
    raise RuntimeError(
        f"APP_HOST={host!r} would expose the app (and all your bank data) beyond this "
        "machine. Set APP_AUTH_USER / APP_AUTH_PASSWORD to enable the built-in "
        "authentication, use 127.0.0.1, or set APP_ALLOW_REMOTE=1 if it sits behind "
        "an authenticating reverse proxy."
    )


def get_settings() -> Settings:
    domain = os.environ.get("POWENS_DOMAIN")
    if not domain:
        raise RuntimeError("POWENS_DOMAIN is not set. Copy .env.example to .env and fill it in.")
    return Settings(
        domain=domain,
        client_id=os.environ.get("POWENS_CLIENT_ID") or None,
        client_secret=os.environ.get("POWENS_CLIENT_SECRET") or None,
        access_token=(os.environ.get("POWENS_ACCESS_TOKEN") or "").strip() or None,
        host=_check_host((os.environ.get("APP_HOST") or "127.0.0.1").strip()),
        # ``PORT`` est imposé par la plupart des hébergeurs (Railway, Render,
        # Fly…), qui choisissent eux-mêmes le port d'écoute : le lire évite d'y
        # recopier une variable dont on ne décide pas la valeur.
        port=int(os.environ.get("APP_PORT") or os.environ.get("PORT") or "8000"),
        history_months=int(os.environ.get("APP_HISTORY_MONTHS", "36")),
        base_currency=(os.environ.get("APP_BASE_CURRENCY") or "EUR").strip().upper(),
        openfigi_api_key=os.environ.get("OPENFIGI_API_KEY") or None,
        silent_after_days=max(1, int(os.environ.get("APP_SILENT_DAYS", "3"))),
        benchmark_ticker=(os.environ.get("APP_BENCHMARK_TICKER") or "IWDA.AS").strip(),
        benchmark_label=(os.environ.get("APP_BENCHMARK_LABEL") or "MSCI World (IWDA)").strip(),
        collect_every_hours=max(0.0, float(os.environ.get("APP_COLLECT_EVERY_HOURS") or "0")),
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
