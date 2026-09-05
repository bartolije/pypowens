"""Authentification — page de connexion, session signée révocable, second facteur.

Ce qui protège l'app est un **cookie de session signé**, posé par un vrai
formulaire (``/connexion``). Le HTTP Basic d'origine tenait la porte, mais il
la tenait mal : le navigateur ouvre une fenêtre système que les gestionnaires
de mots de passe ne remplissent pas, on ne peut pas se déconnecter, et le mot
de passe repart en clair (base64) à chaque requête.

Trois propriétés valent d'être connues avant de toucher à ce module :

* **le second facteur est optionnel mais non contournable.** Il s'active en
  posant ``APP_TOTP_SECRET`` ; dès lors, la seule entrée à un facteur qui
  subsiste est le jeton ``APP_API_TOKEN``, réservé aux appels non interactifs
  (cf. :func:`config.api_token`). L'en-tête ``Authorization: Basic`` avec le
  mot de passe, lui, cesse d'être accepté — sans quoi le MFA serait décoratif ;
* **une session se révoque côté serveur.** Le cookie porte la génération
  (``store.session_epoch``) sous laquelle il a été émis ; la déconnexion
  incrémente cette génération, ce qui invalide d'un coup tout cookie antérieur.
  Sans cet ancrage, se déconnecter ne faisait qu'oublier le cookie côté
  navigateur — un cookie volé restait valable jusqu'à son expiration ;
* **le frein anti-force-brute ne se contourne pas par un en-tête.** Voir
  :func:`_client_key`, et le second frein par compte qui rattrape ce que le
  premier, par adresse, ne peut pas voir.

Le mot de passe reste ``APP_AUTH_USER`` / ``APP_AUTH_PASSWORD``. La clé de
signature en est dérivée à défaut de ``APP_SESSION_SECRET``, ce qui donne deux
propriétés utiles : aucune variable à poser, et changer le mot de passe
déconnecte partout.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
import sqlite3
import time
from collections.abc import Awaitable, Callable
from secrets import compare_digest

from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from . import store, totp
from .config import api_token, auth_credentials, session_secret_error, totp_secret

_log = logging.getLogger(__name__)

_REALM = "Powens Finance"

SESSION_COOKIE = "pf_session"
# Vingt-quatre heures. Même révocable (cf. la génération de session), un cookie
# intercepté ne doit pas valoir une semaine d'accès aux comptes ; et la
# reconnexion quotidienne se fait en deux champs remplis par le gestionnaire de
# mots de passe. APP_SESSION_MAX_AGE_HOURS permet d'assouplir en connaissance
# de cause (un téléphone qui n'est jamais prêté, par exemple).
SESSION_MAX_AGE = 24 * 3600
LOGIN_PATH = "/connexion"


def session_max_age() -> int:
    """Durée de vie d'une session, en secondes (défaut : 24 h)."""
    raw = (os.environ.get("APP_SESSION_MAX_AGE_HOURS") or "").strip()
    if not raw:
        return SESSION_MAX_AGE
    try:
        hours = float(raw)
    except ValueError:
        _log.warning("APP_SESSION_MAX_AGE_HOURS=%r illisible : 24 h retenues", raw)
        return SESSION_MAX_AGE
    # Bornée : une valeur absurde (0, ou trois ans) ne doit ni fermer la session
    # à chaque page ni la rendre éternelle.
    return int(max(0.25, min(hours, 720)) * 3600)


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

# Filet par COMPTE, indépendant de l'adresse : une attaque répartie sur des
# centaines d'adresses ne déclenche jamais le frein par client (une tentative
# par adresse), alors qu'elle essaie bien des milliers de mots de passe sur le
# seul compte qui existe. Large à dessein — le propriétaire n'échoue jamais
# autant dans la demi-heure. Contrepartie assumée : un attaquant obstiné peut
# nous verrouiller nous-mêmes le temps de la fenêtre.
_MAX_ACCOUNT_FAILURES = 40
_ACCOUNT_LOCKOUT_SECONDS = 1800

_MAX_TRACKED = 1024


class _Brake:
    """Compteur d'échecs à fenêtre glissante, en mémoire.

    Remis à zéro au redémarrage : suffisant ici, l'app ne tournant qu'en un
    exemplaire, et un redémarrage n'est pas à la portée de l'attaquant.
    """

    def __init__(self, max_failures: int, lockout: int) -> None:
        self.max_failures = max_failures
        self.lockout = lockout
        self._failures: dict[str, tuple[int, float]] = {}

    def retry_after(self, key: str, *, now: float | None = None) -> int:
        """Secondes de blocage restantes, ``0`` si le client peut réessayer."""
        count, last = self._failures.get(key, (0, 0.0))
        if count < self.max_failures:
            return 0
        remaining = self.lockout - ((now or time.monotonic()) - last)
        if remaining <= 0:
            self._failures.pop(key, None)  # fenêtre écoulée : le client repart à neuf
            return 0
        return int(remaining) + 1

    def record_failure(self, key: str, *, now: float | None = None) -> None:
        moment = now or time.monotonic()
        if len(self._failures) > _MAX_TRACKED:
            # Sans cette purge, une rafale de clés distinctes ferait grossir le
            # dictionnaire indéfiniment.
            for stale, (_, last) in list(self._failures.items()):
                if moment - last > self.lockout:
                    del self._failures[stale]
        count, _ = self._failures.get(key, (0, 0.0))
        self._failures[key] = (count + 1, moment)

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)

    def reset(self) -> None:
        self._failures.clear()


_client_brake = _Brake(_MAX_FAILURES, _LOCKOUT_SECONDS)
_account_brake = _Brake(_MAX_ACCOUNT_FAILURES, _ACCOUNT_LOCKOUT_SECONDS)

# Vue en lecture conservée pour les tests historiques.
_failures = _client_brake._failures


def reset_failures() -> None:
    """Vide les compteurs d'échecs (tests, et redémarrage à chaud)."""
    _client_brake.reset()
    _account_brake.reset()


def _client_key(request: Request) -> str:
    """Identifie l'appelant pour le frein, sans se laisser dicter la réponse.

    Le piège est de faire confiance à un en-tête que le client écrit lui-même.
    ``X-Forwarded-For`` est une LISTE à laquelle chaque relais ajoute une entrée
    à droite : l'entrée la plus à GAUCHE est celle envoyée par le client, donc
    entièrement sous son contrôle — la changer à chaque requête faisait repartir
    le compteur de zéro et annulait purement et simplement le frein.

    Par ordre de confiance :

    * ``CF-Connecting-IP`` — posé par Cloudflare, qui ÉCRASE ce que le client a
      pu envoyer : non usurpable dès lors que l'app n'est joignable que par le
      proxy ;
    * la DERNIÈRE entrée de ``X-Forwarded-For`` — celle ajoutée par le relais le
      plus proche de nous, donc la seule que le client n'a pas écrite ;
    * l'adresse de la socket, en direct (usage local).
    """
    cloudflare = (request.headers.get("cf-connecting-ip") or "").strip()
    if cloudflare:
        return cloudflare
    forwarded = (request.headers.get("x-forwarded-for") or "").strip()
    if forwarded:
        return forwarded.split(",")[-1].strip()
    client = request.client
    return client.host if client else "?"


def retry_after(key: str, *, now: float | None = None) -> int:
    """Secondes de blocage restantes pour ce client, ``0`` s'il peut réessayer."""
    return _client_brake.retry_after(key, now=now)


def record_failure(key: str, *, now: float | None = None) -> None:
    _client_brake.record_failure(key, now=now)


def clear_failures(key: str) -> None:
    _client_brake.clear(key)


def account_retry_after(user: str, *, now: float | None = None) -> int:
    """Secondes de blocage restantes pour ce COMPTE, toutes adresses confondues."""
    return _account_brake.retry_after(user.strip().lower(), now=now)


def record_account_failure(user: str, *, now: float | None = None) -> None:
    _account_brake.record_failure(user.strip().lower(), now=now)


def clear_account_failures(user: str) -> None:
    _account_brake.clear(user.strip().lower())


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


# ------------------------------------------------------------- second facteur


def mfa_enabled() -> bool:
    """Un second facteur est-il configuré ?"""
    return bool(totp_secret())


def totp_secret_error() -> str | None:
    """Pourquoi le secret TOTP configuré est inutilisable, ou ``None``."""
    return totp.secret_error(totp_secret())


def verify_totp(conn: sqlite3.Connection | None, code: str) -> bool:
    """Le code à six chiffres est-il valide, et pas déjà consommé ?

    Fail-closed de bout en bout : un secret malformé refuse la connexion (avec
    un WARNING au démarrage, cf. ``startup_warnings``) plutôt que de lever une
    500, et l'absence de base — donc l'impossibilité de vérifier le rejeu —
    refuse aussi. Un MFA qu'on ne peut pas contrôler n'est pas un MFA.
    """
    problem = totp_secret_error()
    if problem:
        _log.error("%s : connexion refusée", problem)
        return False
    counter = totp.verify(totp_secret(), code)
    if counter is None:
        return False
    if conn is None:
        _log.error("Base indisponible : impossible de vérifier le rejeu du code TOTP")
        return False
    if not store.claim_totp_counter(conn, counter):
        _log.warning("Code TOTP rejoué (pas de temps %d déjà consommé)", counter)
        return False
    return True


# --------------------------------------------------------------- session signée
#
# Le jeton est ``<utilisateur base64>.<émis à>.<génération>.<HMAC-SHA256>`` : la
# construction d'itsdangerous, écrite ici en trente lignes de bibliothèque
# standard plutôt qu'en dépendance de plus dans l'image. Le cookie ne porte
# aucun secret — il n'est pas chiffré, seulement signé — et sa durée de vie
# comme sa génération sont dans la signature, donc non modifiables par le
# porteur.


def _session_key() -> bytes:
    """Clé de signature des sessions.

    ``APP_SESSION_SECRET`` si elle existe (pour garder les sessions ouvertes à
    travers un changement de mot de passe) ; sinon dérivée des identifiants, ce
    qui évite d'avoir une variable de plus à poser chez l'hébergeur et fait de
    tout changement de mot de passe une déconnexion générale. Dans les deux cas,
    l'entropie de la graine est contrôlée au démarrage
    (``config.session_secret_error``) : une graine devinable rendrait tout ce
    module inutile, cookie forgeable et second facteur jamais demandé.
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


def issue_session(user: str, *, issued_at: float | None = None, epoch: int = 0) -> str:
    # ``or time.time()`` serait faux : un ``issued_at=0`` explicite (tests) est
    # falsy et retomberait sur l'heure courante, donc sur un jeton jamais expiré.
    moment = time.time() if issued_at is None else issued_at
    payload = f"{_b64(user.encode('utf-8'))}.{int(moment)}.{int(epoch)}"
    signature = hmac.new(_session_key(), payload.encode("ascii"), hashlib.sha256).digest()
    return f"{payload}.{_b64(signature)}"


def read_session(token: str, *, now: float | None = None, epoch: int = 0) -> str | None:
    """Utilisateur porté par un jeton valide, non expiré et non révoqué.

    ``None`` dès qu'un des trois manque. Un jeton d'avant l'ancrage serveur
    (trois segments) tombe ici aussi : il n'a pas de génération à comparer, donc
    rien ne garantit qu'il n'a pas été révoqué — une reconnexion, une fois.
    """
    try:
        user_part, issued_part, epoch_part, signature = token.split(".")
        expected = hmac.new(
            _session_key(),
            f"{user_part}.{issued_part}.{epoch_part}".encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_unb64(signature), expected):
            return None
        if (now or time.time()) - int(issued_part) > session_max_age():
            return None
        if int(epoch_part) != int(epoch):
            return None  # émis avant une déconnexion ou une révocation
        return _unb64(user_part).decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


def _store(request: Request) -> sqlite3.Connection | None:
    return getattr(request.app.state, "store", None)


def current_epoch(request: Request) -> int | None:
    """Génération de session attendue, ``None`` si elle est illisible.

    ``None`` veut dire « ne faire confiance à aucun cookie » : ne pas pouvoir
    lire la génération, c'est ne pas pouvoir savoir si une session a été
    révoquée.
    """
    conn = _store(request)
    if conn is None:
        return 0  # app montée sans base (tests unitaires) : rien n'a été révoqué
    try:
        return store.session_epoch(conn)
    except sqlite3.Error:
        _log.exception("Lecture de la génération de session impossible")
        return None


def current_user(request: Request) -> str | None:
    """Utilisateur de la session en cours, ``None`` si personne n'est connecté."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    epoch = current_epoch(request)
    if epoch is None:
        return None
    expected = auth_credentials()
    user = read_session(token, epoch=epoch)
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
    epoch = current_epoch(request) or 0
    max_age = session_max_age()
    response.set_cookie(
        SESSION_COOKIE,
        issue_session(user, epoch=epoch),
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=_https(request),
        path="/",
    )


def end_session(response: Response, request: Request | None = None) -> None:
    """Ferme la session — vraiment.

    Oublier le cookie côté navigateur ne suffit pas : le jeton reste valable
    pour qui en détiendrait une copie. Incrémenter la génération le rend
    inutilisable, ici et partout ailleurs.
    """
    conn = _store(request) if request is not None else None
    if conn is not None:
        try:
            store.revoke_sessions(conn)
        except sqlite3.Error:
            _log.exception("Révocation des sessions impossible")
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


def _refused(request: Request, *, detail: str | None = None) -> Response:
    """Réponse à une requête non authentifiée."""
    if _wants_html(request) or _from_a_browser(request):
        return _login_redirect(request)
    # Script ou outil en ligne de commande : le défi Basic lui dit quoi envoyer.
    return PlainTextResponse(
        detail or "Authentification requise.",
        status_code=401,
        headers={"WWW-Authenticate": f'Basic realm="{_REALM}", charset="UTF-8"'},
    )


def _too_many(delay: int) -> Response:
    return PlainTextResponse(
        "Trop de tentatives infructueuses. Réessayez plus tard.",
        status_code=429,
        headers={"Retry-After": str(delay)},
    )


# Message renvoyé au script qui présente encore le mot de passe alors que le
# second facteur est actif : sans lui, la panne se lit « 401 » et rien d'autre.
_TOKEN_REQUIRED = (
    "Le second facteur est actif : le mot de passe n'ouvre plus l'accès non "
    "interactif. Utiliser APP_API_TOKEN (Authorization: Bearer <jeton>, ou "
    "curl -u <nom>:<jeton>)."
)


def _presented_token(request: Request, given: tuple[str, str] | None) -> str | None:
    """Jeton présenté par un appel non interactif, sous l'une de ses deux formes."""
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    # ``curl -u nom:jeton`` : le jeton prend la place du mot de passe, ce qui
    # évite de réécrire les scripts et les fichiers de configuration existants.
    return given[1] if given is not None else None


def _api_token_ok(presented: str | None) -> bool:
    expected = api_token()
    if not expected or not presented:
        return False
    # Plancher de longueur : un jeton court se force, et il n'a pas de frein
    # propre (les scripts ne doivent pas se verrouiller eux-mêmes).
    if len(expected) < 24:
        _log.error("APP_API_TOKEN est trop court (24 caractères au moins) : ignoré")
        return False
    return compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def startup_warnings() -> list[str]:
    """Anomalies de configuration à signaler au démarrage (sans bloquer)."""
    problems = []
    secret = totp_secret_error()
    if secret:
        problems.append(f"{secret} — le second facteur refusera toute connexion")
    token = api_token()
    if token and len(token) < 24:
        problems.append("APP_API_TOKEN est trop court (24 caractères au moins) : ignoré")
    if mfa_enabled() and not token:
        problems.append(
            "Second facteur actif sans APP_API_TOKEN : les appels non interactifs "
            "(scripts/backup-prod.sh) seront refusés"
        )
    return problems


def check_configuration() -> None:
    """Refuse de démarrer sur une configuration qui annulerait l'authentification.

    Seul cas fatal : une graine de signature devinable. Tout le reste
    (``APP_TOTP_SECRET`` illisible, jeton trop court) ferme des portes au lieu
    d'en ouvrir — un WARNING suffit, et l'app doit rester debout pour être
    corrigée. Une graine faible, elle, ouvre la porte principale en silence.
    """
    problem = session_secret_error()
    if problem:
        raise RuntimeError(problem)


async def require_login(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Middleware : exige une session (ou un jeton valide) dès que des
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
    presented = _presented_token(request, given)
    if _api_token_ok(presented):
        # Porte des appels non interactifs : un jeton valide passe, sans poser
        # de session et sans second facteur (un script ne peut pas en produire).
        clear_failures(key)
        request.state.user = expected[0]
        return await call_next(request)

    if given is not None:
        if mfa_enabled():
            # Le mot de passe seul n'ouvre plus rien : l'accepter ici rendrait le
            # second facteur cosmétique, puisqu'il n'est demandé qu'au formulaire.
            record_failure(key)
            record_account_failure(given[0])
            return _refused(request, detail=_TOKEN_REQUIRED)
        if credentials_match(given, expected):
            clear_failures(key)
            clear_account_failures(given[0])
            request.state.user = expected[0]
            return await call_next(request)
        # Une visite arrive toujours sans en-tête : la compter comme un échec
        # épuiserait le quota avant même la page de connexion.
        record_failure(key)
        record_account_failure(given[0])

    return _refused(request)


# Ancien nom du middleware, conservé le temps que rien d'externe ne l'appelle.
basic_auth = require_login
