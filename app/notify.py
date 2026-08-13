"""Notification hors de l'app — best effort, jamais bloquant.

Le bandeau de santé ne vit que dans l'app : tant que l'onglet n'est pas ouvert,
une connexion en panne ou une hausse d'abonnement reste invisible. Le collecteur,
lui, passe plusieurs fois par jour — c'est lui qui notifie.

Deux canaux, dans cet ordre :

* ``APP_NOTIFY_URL`` — un POST JSON ``{"title": …, "message": …}``, qui convient
  tel quel à Home Assistant (``/api/services/notify/notify``), Gotify, ntfy ou
  un webhook maison. Le seul qui vaille dès que l'app tourne ailleurs que sur
  le poste de travail : un serveur distant n'a pas d'écran devant lequel
  quelqu'un serait assis.
* à défaut, le centre de notifications macOS, pour l'exécution locale.

Une connexion bancaire finit toujours par tomber — la DSP2 impose une
ré-authentification périodique. Passer plusieurs semaines sans le voir, c'est
autant de jours sans collecte, et un solde non collecté est perdu pour de bon.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from urllib.parse import urlsplit

_log = logging.getLogger(__name__)

_DISABLED = ("0", "false", "no")


def _escape(text: str) -> str:
    """Échappement AppleScript : backslash puis guillemets."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _notify_macos(title: str, message: str) -> bool:
    """Centre de notifications macOS, via osascript."""
    if sys.platform != "darwin":
        return False
    script = (
        f'display notification "{_escape(message[:220])}" '
        f'with title "{_escape(title)}"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script], check=True, capture_output=True, timeout=10
        )
        return True
    except (OSError, subprocess.SubprocessError):
        _log.warning("notification macOS impossible", exc_info=True)
        return False


def _notify_webhook(url: str, title: str, message: str) -> bool:
    """POST JSON vers un service de notification.

    ``APP_NOTIFY_TOKEN`` devient un en-tête ``Authorization: Bearer`` — c'est ce
    qu'attend l'API de Home Assistant.
    """
    import httpx  # noqa: PLC0415 — déjà tiré par la lib, importé au besoin

    headers = {}
    token = (os.environ.get("APP_NOTIFY_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = httpx.post(
            url,
            json={"title": title, "message": message},
            headers=headers,
            timeout=10.0,
        )
        response.raise_for_status()
        return True
    except httpx.HTTPStatusError as error:
        # Jamais l'exception entière ni l'URL complète : celle-ci porte parfois
        # un jeton en paramètre, et ces journaux partent chez l'hébergeur.
        _log.warning(
            "notification refusée par %s (HTTP %s)",
            urlsplit(url).netloc,
            error.response.status_code,
        )
        return False
    except httpx.HTTPError:
        _log.warning("notification impossible vers %s", urlsplit(url).netloc)
        return False


def notify(title: str, message: str) -> bool:
    """Pousse une notification. ``APP_NOTIFY=0`` coupe tout.

    Retourne ``False`` (sans lever) si aucun canal n'est disponible ou si l'envoi
    échoue : une notification ratée ne doit jamais faire échouer une collecte.
    """
    if (os.environ.get("APP_NOTIFY") or "1").strip().lower() in _DISABLED:
        return False
    url = (os.environ.get("APP_NOTIFY_URL") or "").strip()
    if url:
        return _notify_webhook(url, title, message)
    return _notify_macos(title, message)
