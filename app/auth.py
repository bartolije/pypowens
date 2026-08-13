"""Authentification HTTP Basic — la seule porte entre Internet et les comptes.

Tant que l'app ne servait que la loopback, son absence d'authentification était
tenable : il fallait déjà être devant la machine. Dès qu'elle est publiée, ce
n'est plus vrai, et ce qu'il y a derrière la porte est l'historique complet des
soldes et des transactions.

Basic est un choix assumé : le navigateur mémorise les identifiants, donc aucun
formulaire ni session à écrire, et le canal est chiffré par HTTPS. Ce qui manque
à Basic, en revanche, c'est toute résistance à la force brute — d'où le
ralentissement progressif ci-dessous, sans lequel un mot de passe unique finit
par tomber. Basic ne protège pas davantage du CSRF, le navigateur rejouant les
identifiants sur une requête déclenchée par un autre site : c'est le contrôle
d'``Origin`` de ``main`` qui s'en charge, et il reste actif à distance.
"""

from __future__ import annotations

import base64
import binascii
import time
from collections.abc import Awaitable, Callable
from secrets import compare_digest

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from .config import auth_credentials

_REALM = "Powens Finance"

# Sondé par l'hébergeur avant de basculer le trafic sur un nouveau déploiement.
# Exempté faute de quoi ce contrôle recevrait un 401 et la mise en ligne
# échouerait ; il ne divulgue rien.
_EXEMPT_PATHS = frozenset({"/health"})

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


def _retry_after(key: str, *, now: float | None = None) -> int:
    """Secondes de blocage restantes, ``0`` si le client peut réessayer."""
    count, last = _failures.get(key, (0, 0.0))
    if count < _MAX_FAILURES:
        return 0
    remaining = _LOCKOUT_SECONDS - ((now or time.monotonic()) - last)
    if remaining <= 0:
        _failures.pop(key, None)  # fenêtre écoulée : le client repart à neuf
        return 0
    return int(remaining) + 1


def _record_failure(key: str, *, now: float | None = None) -> None:
    moment = now or time.monotonic()
    if len(_failures) > _MAX_TRACKED:
        # Sans cette purge, une rafale d'adresses distinctes ferait grossir le
        # dictionnaire indéfiniment.
        for stale, (_, last) in list(_failures.items()):
            if moment - last > _LOCKOUT_SECONDS:
                del _failures[stale]
    count, _ = _failures.get(key, (0, 0.0))
    _failures[key] = (count + 1, moment)


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


def _matches(given: tuple[str, str], expected: tuple[str, str]) -> bool:
    """Comparaison à temps constant, sans court-circuit entre les deux champs.

    Les deux comparaisons sont menées même si la première a échoué : s'arrêter
    au premier écart dirait, par le temps de réponse, que le nom d'utilisateur
    était bon. Comparaison sur les octets, ``compare_digest`` refusant les
    chaînes non ASCII — un mot de passe accentué lèverait sinon ``TypeError``.
    """
    user_ok = compare_digest(given[0].encode("utf-8"), expected[0].encode("utf-8"))
    password_ok = compare_digest(given[1].encode("utf-8"), expected[1].encode("utf-8"))
    return user_ok and password_ok


def _challenge() -> Response:
    """401 assorti du défi qui déclenche la fenêtre d'identification."""
    return PlainTextResponse(
        "Authentification requise.",
        status_code=401,
        headers={"WWW-Authenticate": f'Basic realm="{_REALM}", charset="UTF-8"'},
    )


async def basic_auth(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Middleware : exige les identifiants dès qu'ils sont configurés.

    Sans ``APP_AUTH_USER``/``APP_AUTH_PASSWORD``, on ne change rien — l'usage
    local reste sans friction, et ``config._check_host`` se charge d'interdire
    l'écoute hors loopback dans ce cas.
    """
    expected = auth_credentials()
    if expected is None or request.url.path in _EXEMPT_PATHS:
        return await call_next(request)

    key = _client_key(request)
    delay = _retry_after(key)
    if delay:
        return PlainTextResponse(
            "Trop de tentatives infructueuses. Réessayez plus tard.",
            status_code=429,
            headers={"Retry-After": str(delay)},
        )

    given = _submitted(request.headers.get("authorization") or "")
    if given is None or not _matches(given, expected):
        # Une visite arrive toujours sans en-tête : la compter comme un échec
        # épuiserait le quota avant même la fenêtre d'identification.
        if given is not None:
            _record_failure(key)
        return _challenge()

    _failures.pop(key, None)
    return await call_next(request)
