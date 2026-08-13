"""Connecteurs en authentification web : retrouver l'URL de la banque.

Certains connecteurs (Sumeria/Lydia, la plupart des néobanques) n'ont pas de
formulaire d'identifiants : ``auth_mechanism = webauth``. L'utilisateur doit
s'authentifier **sur le site de la banque**, qui renvoie ensuite vers Powens.

Powens ne publie pas cette URL dans un champ dédié : il la range dans
``error_message``, sous la forme ``"Redirecting to https://…"``. Envoyer
l'utilisateur vers le Webview Powens ne marche pas pour ces connecteurs — il
rebondit sur la page d'accueil de la banque, sans rien à faire. Il faut viser
l'URL d'autorisation directement.

Cette URL **expire** (deux heures pour Sumeria) : passé ce délai il faut
demander à Powens d'en régénérer une.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

# « Redirecting to https://... » — le préfixe est constant, l'URL suit.
_URL_IN_MESSAGE = re.compile(r"https?://\S+")


def authorize_url(connection: Any) -> str | None:
    """L'URL d'autorisation de la banque, ou ``None``.

    Ne retourne que du HTTPS : cette URL sert de cible de redirection, et une
    valeur inattendue ne doit pas devenir une redirection ouverte.
    """
    raw = getattr(connection, "raw", None) or {}
    message = str(raw.get("error_message") or "")
    match = _URL_IN_MESSAGE.search(message)
    if not match:
        return None
    url = match.group(0).rstrip(".,;)")
    return url if urlsplit(url).scheme == "https" else None


def is_expired(connection: Any, *, now: datetime | None = None) -> bool:
    """L'URL d'autorisation est-elle périmée ?

    ``expire`` absent = on ne sait pas : on considère l'URL utilisable plutôt
    que de refuser un parcours qui aurait pu aboutir.
    """
    raw = getattr(connection, "raw", None) or {}
    expire = raw.get("expire")
    if not expire:
        return False
    try:
        deadline = datetime.fromisoformat(str(expire).replace(" ", "T", 1))
    except ValueError:
        return False
    return (now or datetime.now()) >= deadline.replace(tzinfo=None)


def needs_webauth(connection: Any) -> bool:
    """La connexion attend-elle une authentification sur le site de la banque ?"""
    raw = getattr(connection, "raw", None) or {}
    return (raw.get("state") or "") == "webauthRequired"


# Un parcours d'authentification bancaire peut être réservé au mobile. Sumeria/
# Lydia, par exemple, répond 302 vers son site vitrine à un navigateur de bureau
# et sert la page de consentement à un téléphone : suivre le lien depuis un
# ordinateur mène à un cul-de-sac, sans le moindre message.
_MOBILE_UA = re.compile(r"iPhone|iPad|Android|Mobile", re.IGNORECASE)


def is_mobile(user_agent: str | None) -> bool:
    return bool(user_agent and _MOBILE_UA.search(user_agent))


def qr_svg(url: str, *, scale: int = 8) -> str:
    """QR code du lien d'autorisation, en SVG inline.

    Le seul moyen commode de passer un lien de 200 caractères d'un écran
    d'ordinateur à un téléphone. Rendu localement (segno est pur Python) :
    aucune requête vers un service de génération, alors que ce lien porte un
    jeton d'authentification bancaire.
    """
    import io

    import segno  # noqa: PLC0415 — dépendance de l'app, pas de la lib

    buffer = io.BytesIO()  # segno écrit des octets, même en SVG
    # Correction d'erreur BASSE, à dessein : ces liens font ~200 caractères,
    # et chaque niveau de correction ajoute des modules. Une correction moyenne
    # donnait 57×57 modules, soit 3,3 px chacun à l'écran — sous le seuil de
    # lecture d'un appareil photo, qui détecte le code (cadre jaune) sans
    # parvenir à le décoder. En 53×53 affichés plus grand, chaque module fait
    # près de 6 px.
    segno.make(url, error="l").save(
        buffer, kind="svg", scale=scale, dark="#111111", light="#ffffff",
        border=2, xmldecl=False, svgns=True,
    )
    return buffer.getvalue().decode("utf-8")
