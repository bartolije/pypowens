"""Santé des connexions : les alertes affichées en bandeau sur TOUTES les pages.

Un patrimoine qui saute de +257 k€ parce qu'une connexion en panne a fait
sortir un prêt du périmètre — sans le moindre signal hors de la page
/patrimoine — rend le chiffre inutilisable. Trois situations méritent un
bandeau global :

* connexion en **erreur** (identifiants refusés, authentification à refaire…) :
  les soldes de ses comptes sont figés à la date de la panne ;
* connexion **muette** : plus synchronisée depuis :data:`SILENT_AFTER_DAYS`
  jours alors que son état est sain — le cas le plus sournois, rien ne
  s'affiche en erreur (Trade Republic figée deux semaines en état « OK ») ;
* comptes **désactivés** côté Powens mais porteurs d'un solde : exclus du
  total sans marqueur — le prêt immobilier fantôme du 02/08.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from pypowens import PowensClient

from .data import load_all_accounts, load_connections
from .recap import STATE_LABELS, USER_ACTION_STATES

# Powens synchronise chaque connexion au moins une fois par jour : trois jours
# de silence couvrent un week-end difficile sans crier au loup pour rien.
SILENT_AFTER_DAYS = 3

# Au-delà de 24 h sans synchro ET sans prochaine synchro planifiée (next_try),
# une connexion est BLOQUÉE : Powens ne repassera jamais de lui-même.
AUTO_SYNC_AFTER_HOURS = 24

_log = logging.getLogger(__name__)


async def auto_sync_stuck_connections(client: PowensClient) -> list[int]:
    """Relance les connexions bloquées — l'équivalent automatique du bouton
    « Synchroniser », déclenché à l'ouverture de l'app.

    C'est l'état exact où Trade Republic est restée figée deux semaines : état
    sain, ``next_try`` absent — rien ne la resynchroniserait jamais. Ne touche
    JAMAIS une connexion en erreur : relancer un ``webauthRequired`` en boucle
    peut déclencher des validations fortes chez la banque.
    """
    now = datetime.now()
    launched: list[int] = []
    for connection in await load_connections(client):
        if connection.id is None or connection.state or connection.error_message:
            continue
        last_update = connection.last_update
        if last_update is None or (now - last_update) < timedelta(
            hours=AUTO_SYNC_AFTER_HOURS
        ):
            continue
        next_try = connection.next_try
        if next_try is not None and next_try > now:
            continue  # Powens a déjà prévu de repasser : ne pas doubler.
        try:
            await client.update_connection(connection.id)
            launched.append(connection.id)
            _log.info("connexion %s bloquée : synchronisation relancée", connection.id)
        except Exception:  # noqa: BLE001 — best-effort, ne bloque jamais une page
            _log.warning(
                "relance de la connexion %s impossible", connection.id, exc_info=True
            )
    return launched


async def connection_alerts(
    client: PowensClient, conn: sqlite3.Connection | None = None
) -> list[dict[str, Any]]:
    """Les alertes de santé à afficher, prêtes pour le template.

    Chaque entrée : ``kind`` (error | silent | excluded), ``title``, ``detail``,
    et l'action possible — ``sync_id`` (bouton POST /synchroniser) ou
    ``action_url`` (lien, typiquement /reconnecter/{id}).
    """
    alerts: list[dict[str, Any]] = []
    connections = await load_connections(client)
    now = datetime.now()

    for connection in connections:
        name = (
            connection.connector.name
            if connection.connector and connection.connector.name
            else "Banque"
        )
        state = connection.state or ""
        if state or connection.error_message:
            needs_user = state in USER_ACTION_STATES
            alerts.append(
                {
                    "kind": "error",
                    "title": name,
                    "detail": STATE_LABELS.get(state)
                    or state
                    or "Erreur signalée par la banque",
                    "action_url": f"/reconnecter/{connection.id}" if needs_user else None,
                    "sync_id": None if needs_user else connection.id,
                    "action_label": "Reprendre" if needs_user else "Synchroniser",
                    "amount": None,
                }
            )
            continue

        last_update = connection.last_update
        if last_update is None:
            continue
        try:
            # La lib peut renvoyer des datetimes naïfs OU avec fuseau selon le
            # connecteur : on compare en naïf, une heure d'écart est sans enjeu.
            age = now - last_update.replace(tzinfo=None)
        except (TypeError, ValueError):
            continue
        if age > timedelta(days=SILENT_AFTER_DAYS):
            alerts.append(
                {
                    "kind": "silent",
                    "title": name,
                    "detail": f"muette depuis {age.days} jours (état pourtant sain)",
                    "action_url": None,
                    "sync_id": connection.id,
                    "action_label": "Synchroniser",
                    "amount": None,
                }
            )

    # Comptes désactivés côté Powens mais porteurs d'un solde : ils ne comptent
    # plus dans le patrimoine. Powens recrée parfois le même compte plusieurs
    # fois pendant une panne — seule la version non supprimée compte. Cas vécu :
    # après réparation de la connexion, le compte est RECRÉÉ DÉSACTIVÉ — sans le
    # bouton « Réintégrer » ci-dessous, rien dans l'UI ne permettait de le
    # faire revenir dans le total.
    all_accounts = await load_all_accounts(client)
    excluded = [
        a
        for a in all_accounts.accounts
        if a.raw.get("disabled") and not a.raw.get("deleted") and a.balance
    ]
    for account in excluded:
        alerts.append(
            {
                "kind": "excluded",
                "title": account.name or "Compte",
                "detail": "désactivé côté Powens — exclu du patrimoine affiché",
                "action_url": None,
                "sync_id": None,
                "reactivate_id": account.id,
                "action_label": "Réintégrer",
                "amount": account.balance or Decimal(0),
            }
        )
    return alerts
