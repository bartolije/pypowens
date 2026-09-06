"""Page « Connexions » (``/connexions``) : l'état des lieux, sans attendre la panne.

Le bandeau de santé ne parle que quand quelque chose casse. Il manquait la vue
inverse : voir en un coup d'œil ce que chaque banque remonte, depuis quand, et
si l'information qu'on lit est fraîche — avant de se poser la question.

Trois âges cohabitent et ne disent pas la même chose :

* la **synchro** (``last_update`` de la connexion) : quand Powens a interrogé la
  banque pour la dernière fois ;
* la **dernière opération** vue sur le compte : une banque peut être
  parfaitement synchronisée et n'avoir aucun mouvement depuis trois semaines ;
* le **dernier solde archivé** localement : ce que notre courbe connaît.

Un écart entre les deux premiers est normal. Un écart entre le premier et le
troisième signale que le collecteur ne tourne plus.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse

from pypowens import Account, PowensAPIError, PowensClient

from . import store
from .config import Settings
from .data import clear_cache, load_all_accounts, load_connections, load_transactions
from .deps import get_client, get_settings, get_store
from .recap import STATE_LABELS, USER_ACTION_STATES
from .wealth import family_of, monogram
from .web import templates

router = APIRouter()
_log = logging.getLogger(__name__)


@dataclass
class AccountRow:
    account: Account
    family: str
    disabled: bool
    last_txn: date | None
    txn_count: int
    last_snapshot: date | None


@dataclass
class ConnectionCard:
    id: int | None
    name: str
    state: str
    state_label: str
    ok: bool
    needs_user: bool
    last_update: datetime | None
    age_days: int | None
    next_try: datetime | None
    never_synced: bool = False
    initials: str = "?"
    color: str = "#8a8a8a"
    ink: str = "#ffffff"
    accounts: list[AccountRow] = field(default_factory=list)
    total: Decimal = Decimal(0)


# Powens refuse une synchro forcée quand la connexion vient d'en subir une (ou
# qu'une est déjà en cours). C'est une limite normale du service, pas une panne
# — mais un bouton qui ne fait rien sans rien dire est pire qu'un bouton absent.
SYNC_REFUSED = (
    "Powens refuse une synchronisation forcée pour l'instant : la connexion "
    "vient d'être rafraîchie, ou une synchro est déjà en cours. "
    "La prochaine automatique est indiquée ci-dessous."
)


@router.get("/connexions", response_class=HTMLResponse)
async def connections_page(
    request: Request,
    statut: str = "tous",
    message: str = "",
    client: PowensClient = Depends(get_client),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
    conn: sqlite3.Connection = Depends(get_store),  # noqa: B008
) -> Response:
    connections = await load_connections(client)
    # Comptes DÉSACTIVÉS inclus : ce sont précisément ceux qu'on veut voir ici,
    # puisqu'ils ont disparu des totaux sans rien dire.
    all_accounts = await load_all_accounts(client)
    accounts = [a for a in all_accounts.accounts if not a.raw.get("deleted")]
    aliases = store.account_aliases(conn)

    # Dernière opération par compte, sur la fenêtre d'historique configurée.
    txns = await load_transactions(client, months=settings.history_months, conn=conn)
    last_txn: dict[int | None, date] = {}
    txn_count: dict[int | None, int] = {}
    for txn in txns:
        if txn.date is None:
            continue
        known = last_txn.get(txn.id_account)
        if known is None or txn.date > known:
            last_txn[txn.id_account] = txn.date
        txn_count[txn.id_account] = txn_count.get(txn.id_account, 0) + 1

    last_snapshot = store.last_snapshot_days(conn)
    today = date.today()

    cards: list[ConnectionCard] = []
    for connection in connections:
        state = connection.state or ""
        card = ConnectionCard(
            id=connection.id,
            name=(
                connection.connector.name
                if connection.connector and connection.connector.name
                else "Banque"
            ),
            state=state,
            # Jamais l'error_message brut : sur un connecteur webauth c'est
            # l'URL d'autorisation complète, jeton `state` inclus.
            state_label=STATE_LABELS.get(state)
            or state
            or ("Erreur signalée par la banque" if connection.error_message else "Synchronisé"),
            ok=not state and not connection.error_message,
            needs_user=state in USER_ACTION_STATES,
            last_update=connection.last_update,
            # Jours CALENDAIRES, pas des tranches de 24 h : une synchro d'hier
            # 20 h vue à 15 h aujourd'hui fait 18 h d'écart — donc `.days == 0`
            # sur un timedelta, et un « aujourd'hui » faux à l'écran. L'humain
            # compte les jours au changement de date, pas au bout de 24 h.
            age_days=(
                (today - connection.last_update.date()).days if connection.last_update else None
            ),
            next_try=connection.next_try,
            never_synced=connection.last_update is None,
        )
        card.initials, card.color, card.ink = monogram(connection.connector)
        for account in accounts:
            if account.id_connection != connection.id:
                continue
            card.accounts.append(
                AccountRow(
                    account=account,
                    family=family_of(account.type),
                    disabled=bool(account.raw.get("disabled")),
                    last_txn=last_txn.get(account.id),
                    txn_count=txn_count.get(account.id, 0),
                    last_snapshot=(
                        last_snapshot.get(account.id) if account.id is not None else None
                    ),
                )
            )
        card.accounts.sort(key=lambda r: abs(r.account.balance or Decimal(0)), reverse=True)
        card.total = sum(
            (
                r.account.balance or Decimal(0)
                for r in card.accounts
                if not r.disabled
                and (r.account.currency or settings.base_currency) == settings.base_currency
            ),
            Decimal(0),
        )
        cards.append(card)

    # Les connexions en peine d'abord — erreurs, puis jamais synchronisées,
    # puis les plus anciennes : c'est ce qu'on vient vérifier.
    cards.sort(key=lambda c: (c.ok, not c.never_synced, -(c.age_days or 0)))
    if statut == "erreur":
        cards = [c for c in cards if not c.ok]
    elif statut == "muette":
        cards = [c for c in cards if c.ok and (c.age_days or 0) > settings.silent_after_days]

    imported = store.imported_summary(conn)
    orphans = [a for a in accounts if not any(a.id_connection == c.id for c in connections)]

    # HTMX ne réclame que le fragment : filtrer ou synchroniser ne recharge
    # plus la page entière (en-tête, barre, bandeau et tous leurs calculs).
    template = "_connections_list.html" if request.headers.get("HX-Request") else "connections.html"
    return templates.TemplateResponse(
        request,
        template,
        {
            "request": request,
            "active": "connexions",
            "statut": statut,
            "message": message,
            "cards": cards,
            "orphans": orphans,
            "imported": imported,
            "aliases": aliases,
            "currency": settings.base_currency,
            "history_months": settings.history_months,
            "silent_after_days": settings.silent_after_days,
            "today": today,
        },
    )


@router.post("/connexions/{connection_id}/synchroniser", response_class=HTMLResponse)
async def sync_from_page(
    connection_id: int,
    request: Request,
    statut: str = "tous",
    client: PowensClient = Depends(get_client),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
    conn: sqlite3.Connection = Depends(get_store),  # noqa: B008
) -> Response:
    """Relance une connexion et rend la liste à jour, sans quitter la page."""
    message = ""
    try:
        await client.update_connection(connection_id)
    except PowensAPIError as exc:
        _log.warning(
            "synchronisation refusée pour la connexion %s (HTTP %s)",
            connection_id,
            exc.status_code,
        )
        message = (
            SYNC_REFUSED
            if exc.status_code == 409
            else f"Synchronisation refusée par Powens (HTTP {exc.status_code})."
        )
    clear_cache()
    return await connections_page(
        request, statut=statut, message=message, client=client, settings=settings, conn=conn
    )


@router.post("/connexions/comptes/{account_id}/reactiver", response_class=HTMLResponse)
async def reactivate_from_page(
    account_id: int,
    request: Request,
    statut: str = "tous",
    client: PowensClient = Depends(get_client),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
    conn: sqlite3.Connection = Depends(get_store),  # noqa: B008
) -> Response:
    """Réintègre un compte que Powens a recréé désactivé."""
    message = ""
    try:
        await client.update_account(account_id, disabled=False)
    except PowensAPIError as exc:
        _log.warning(
            "réintégration refusée pour le compte %s (HTTP %s)", account_id, exc.status_code
        )
        message = f"Powens a refusé de réintégrer ce compte (HTTP {exc.status_code})."
    clear_cache()
    return await connections_page(
        request, statut=statut, message=message, client=client, settings=settings, conn=conn
    )
