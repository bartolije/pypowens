"""Notification macOS (centre de notifications) — best effort, jamais bloquant.

Le bandeau de santé ne vit que dans l'app : tant que l'onglet n'est pas ouvert,
une connexion en panne ou une hausse d'abonnement reste invisible. Le collecteur
launchd, lui, passe plusieurs fois par jour — c'est lui qui notifie.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

_log = logging.getLogger(__name__)


def _escape(text: str) -> str:
    """Échappement AppleScript : backslash puis guillemets."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def notify(title: str, message: str) -> bool:
    """Affiche une notification macOS. ``APP_NOTIFY=0`` coupe tout.

    Retourne ``False`` (sans lever) hors macOS, si désactivé, ou si osascript
    échoue : une notification ratée ne doit jamais faire échouer une collecte.
    """
    if (os.environ.get("APP_NOTIFY") or "1").strip().lower() in ("0", "false", "no"):
        return False
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
