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
