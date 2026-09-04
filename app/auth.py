"""Authentification — page de connexion, session signée, et repli Basic.

Ce qui protège l'app est un **cookie de session signé**, posé par un vrai
formulaire (``/connexion``). Le HTTP Basic d'origine tenait la porte, mais il
la tenait mal : le navigateur ouvre une fenêtre système que les gestionnaires
de mots de passe ne remplissent pas, on ne peut pas se déconnecter, et le mot
de passe repart en clair (base64) à chaque requête.

L'en-tête ``Authorization: Basic`` reste **accepté** : c'est ce qui permet à un
script (``scripts/backup-prod.sh``) ou à ``curl`` de récupérer une page sans
parcours de connexion. Mais il n'est plus jamais *réclamé* à un navigateur —
sans le défi ``WWW-Authenticate``, la fenêtre système n'apparaît pas.

Le mot de passe reste ``APP_AUTH_USER`` / ``APP_AUTH_PASSWORD`` : rien à changer
chez l'hébergeur. La clé de signature en est dérivée à défaut de
``APP_SESSION_SECRET``, ce qui donne deux propriétés utiles : aucune variable
à poser, et changer le mot de passe déconnecte partout.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import time
from collections.abc import Awaitable, Callable
from secrets import compare_digest

from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from .config import auth_credentials

_REALM = "Powens Finance"

SESSION_COOKIE = "pf_session"
# Sept jours : assez pour ne pas se reconnecter tous les matins, assez court
# pour qu'un téléphone égaré ne reste pas ouvert indéfiniment.
SESSION_MAX_AGE = 7 * 24 * 3600
LOGIN_PATH = "/connexion"

# Chemins servis sans authentification :
# * ``/health`` est sondé par l'hébergeur AVANT de basculer le trafic — un 401
#   ferait passer un déploiement sain pour une panne ;
# * ``/connexion`` est la porte elle-même ;
# * les statiques (feuille de style, police, icône) doivent charger sur la page
#   de connexion, et ne disent rien de personne.
_EXEMPT_PATHS = frozenset({"/health", LOGIN_PATH, "/favicon.ico"})
_EXEMPT_PREFIXES = ("/static/",)

# Assez de tolérance pour des erreurs de frappe, trop peu pour une attaque.
_MAX_FAILURES = 10
_LOCKOUT_SECONDS = 300
_MAX_TRACKED = 1024

# Client → (échecs consécutifs, instant du dernier). En mémoire, donc remis à
# zéro au redémarrage : suffisant ici, l'app ne tournant qu'en un exemplaire.
_failures: dict[str, tuple[int, float]] = {}


def reset_failures() -> None:
    """Vide le compteur d'échecs (tests, et redémarrage à chaud)."""
    _failures.clear()


def _client_key(request: Request) -> str:
    """Identifie l'appelant, au mieux.

    Derrière un hébergeur, ``client.host`` est l'adresse du routeur interne :
    sans ``X-Forwarded-For``, tous les visiteurs partageraient un même compteur
    et le premier attaquant venu nous verrouillerait nous-mêmes. Cet en-tête est
    forgeable, ce qui permet à un attaquant d'échapper au ralentissement, pas
    d'en déclencher un contre autrui : c'est le compromis le moins mauvais.
    """
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    client = request.client
    return client.host if client else "?"


def retry_after(key: str, *, now: float | None = None) -> int:
    """Secondes de blocage restantes, ``0`` si le client peut réessayer."""
    count, last = _failures.get(key, (0, 0.0))
    if count < _MAX_FAILURES:
        return 0
    remaining = _LOCKOUT_SECONDS - ((now or time.monotonic()) - last)
    if remaining <= 0:
        _failures.pop(key, None)  # fenêtre écoulée : le client repart à neuf
        return 0
    return int(remaining) + 1


def record_failure(key: str, *, now: float | None = None) -> None:
    moment = now or time.monotonic()
    if len(_failures) > _MAX_TRACKED:
        # Sans cette purge, une rafale d'adresses distinctes ferait grossir le
        # dictionnaire indéfiniment.
        for stale, (_, last) in list(_failures.items()):
            if moment - last > _LOCKOUT_SECONDS:
                del _failures[stale]
    count, _ = _failures.get(key, (0, 0.0))
    _failures[key] = (count + 1, moment)


def clear_failures(key: str) -> None:
    _failures.pop(key, None)


# Rétrocompatibilité interne (les tests historiques visent ces noms).
_retry_after = retry_after
_record_failure = record_failure


def _submitted(header: str) -> tuple[str, str] | None:
    """Décode un en-tête ``Authorization: Basic``, ``None`` s'il est inexploitable."""
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    user, separator, password = decoded.partition(":")
    return (user, password) if separator else None


def credentials_match(given: tuple[str, str], expected: tuple[str, str]) -> bool:
    """Comparaison à temps constant, sans court-circuit entre les deux champs.

    Les deux comparaisons sont menées même si la première a échoué : s'arrêter
    au premier écart dirait, par le temps de réponse, que le nom d'utilisateur
    était bon. Comparaison sur les octets, ``compare_digest`` refusant les
    chaînes non ASCII — un mot de passe accentué lèverait sinon ``TypeError``.
    """
    user_ok = compare_digest(given[0].encode("utf-8"), expected[0].encode("utf-8"))
    password_ok = compare_digest(given[1].encode("utf-8"), expected[1].encode("utf-8"))
    return user_ok and password_ok


_matches = credentials_match


# --------------------------------------------------------------- session signée
#
# Le jeton est ``<utilisateur base64>.<émis à>.<HMAC-SHA256>`` : la construction
# d'itsdangerous, écrite ici en trente lignes de bibliothèque standard plutôt
# qu'en dépendance de plus dans l'image. Le cookie ne porte aucun secret — il
# n'est pas chiffré, seulement signé — et sa durée de vie est dans la signature,
# donc non modifiable par le porteur.


def _session_key() -> bytes:
    """Clé de signature des sessions.

    ``APP_SESSION_SECRET`` si elle existe (pour garder les sessions ouvertes à
    travers un changement de mot de passe) ; sinon dérivée des identifiants, ce
    qui évite d'avoir une variable de plus à poser chez l'hébergeur et fait de
    tout changement de mot de passe une déconnexion générale.
    """
    explicit = (os.environ.get("APP_SESSION_SECRET") or "").strip()
    if explicit:
        return hashlib.sha256(explicit.encode("utf-8")).digest()
    expected = auth_credentials() or ("", "")
    seed = f"pypowens-session-v1|{expected[0]}|{expected[1]}"
    return hashlib.sha256(seed.encode("utf-8")).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue_session(user: str, *, issued_at: float | None = None) -> str:
    # ``or time.time()`` serait faux : un ``issued_at=0`` explicite (tests) est
    # falsy et retomberait sur l'heure courante, donc sur un jeton jamais expiré.
    moment = time.time() if issued_at is None else issued_at
    payload = f"{_b64(user.encode('utf-8'))}.{int(moment)}"
    signature = hmac.new(_session_key(), payload.encode("ascii"), hashlib.sha256).digest()
    return f"{payload}.{_b64(signature)}"


def read_session(token: str, *, now: float | None = None) -> str | None:
    """Utilisateur porté par un jeton valide et non expiré, sinon ``None``."""
    try:
        user_part, issued_part, signature = token.split(".")
        expected = hmac.new(
            _session_key(), f"{user_part}.{issued_part}".encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_unb64(signature), expected):
            return None
        if (now or time.time()) - int(issued_part) > SESSION_MAX_AGE:
            return None
        return _unb64(user_part).decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


def current_user(request: Request) -> str | None:
    """Utilisateur de la session en cours, ``None`` si personne n'est connecté."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    expected = auth_credentials()
    user = read_session(token)
    # Un jeton signé pour un AUTRE utilisateur que celui configuré ne vaut rien :
    # renommer APP_AUTH_USER doit fermer les sessions ouvertes.
    if user is None or (expected is not None and user != expected[0]):
        return None
    return user


def _https(request: Request) -> bool:
    """La requête a-t-elle voyagé en HTTPS ? (le proxy termine TLS avant nous)"""
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    return (forwarded or request.url.scheme) == "https"


def start_session(response: Response, request: Request, user: str) -> None:
    """Pose le cookie de session sur la réponse.

    ``Secure`` seulement en HTTPS : l'imposer en local (http://127.0.0.1) ferait
    silencieusement jeter le cookie par le navigateur, donc une page de
    connexion qui boucle sur elle-même.
    """
    response.set_cookie(
        SESSION_COOKIE,
        issue_session(user),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_https(request),
        path="/",
    )


def end_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


# ------------------------------------------------------------------ middleware


def _is_exempt(path: str) -> bool:
    return path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES)


def _from_a_browser(request: Request) -> bool:
    """Requête émise par un navigateur (navigation, fetch ou htmx) ?

    Tous les navigateurs actuels joignent ``Sec-Fetch-Mode``, y compris aux
    requêtes de fond ; htmx s'annonce en plus. Le distinguer d'un script est ce
    qui permet de ne JAMAIS renvoyer le défi ``WWW-Authenticate`` à un
    navigateur — c'est lui, et lui seul, qui déclenche la fenêtre système.
    """
    return bool(request.headers.get("sec-fetch-mode") or request.headers.get("hx-request"))


def _wants_html(request: Request) -> bool:
    return "text/html" in (request.headers.get("accept") or "")


def _login_redirect(request: Request) -> Response:
    """Renvoie vers la page de connexion, en gardant la page demandée en mémoire."""
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    url = LOGIN_PATH
    if target not in ("/", LOGIN_PATH) and request.method == "GET":
        url = f"{LOGIN_PATH}?suite={base64.urlsafe_b64encode(target.encode()).decode()}"
    response: Response = RedirectResponse(url, status_code=303)
    if request.headers.get("hx-request"):
        # htmx remplacerait sinon un fragment par la page de connexion entière.
        response = PlainTextResponse("", status_code=401, headers={"HX-Redirect": url})
    return response


def _refused(request: Request) -> Response:
    """Réponse à une requête non authentifiée."""
    if _wants_html(request) or _from_a_browser(request):
        return _login_redirect(request)
    # Script ou outil en ligne de commande : le défi Basic lui dit quoi envoyer.
    return PlainTextResponse(
        "Authentification requise.",
        status_code=401,
        headers={"WWW-Authenticate": f'Basic realm="{_REALM}", charset="UTF-8"'},
    )


def _too_many(delay: int) -> Response:
    return PlainTextResponse(
        "Trop de tentatives infructueuses. Réessayez plus tard.",
        status_code=429,
        headers={"Retry-After": str(delay)},
    )


async def require_login(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Middleware : exige une session (ou un Basic valide) dès que des
    identifiants sont configurés.

    Sans ``APP_AUTH_USER``/``APP_AUTH_PASSWORD``, on ne change rien — l'usage
    local reste sans friction, et ``config._check_host`` se charge d'interdire
    l'écoute hors loopback dans ce cas.
    """
    expected = auth_credentials()
    request.state.auth_enabled = expected is not None
    request.state.user = None
    if expected is None:
        return await call_next(request)

    user = current_user(request)
    request.state.user = user
    if _is_exempt(request.url.path):
        return await call_next(request)

    key = _client_key(request)
    delay = retry_after(key)
    if delay:
        return _too_many(delay)

    if user is not None:
        return await call_next(request)

    given = _submitted(request.headers.get("authorization") or "")
    if given is not None:
        # Repli pour les scripts : un Basic valide passe, sans poser de session.
        if credentials_match(given, expected):
            clear_failures(key)
            request.state.user = expected[0]
            return await call_next(request)
        # Une visite arrive toujours sans en-tête : la compter comme un échec
        # épuiserait le quota avant même la page de connexion.
        record_failure(key)

    return _refused(request)


# Ancien nom du middleware, conservé le temps que rien d'externe ne l'appelle.
basic_auth = require_login
