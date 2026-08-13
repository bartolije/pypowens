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
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse

from pypowens import Account, PowensAPIError, PowensClient

from . import store
from .config import Settings
from .data import clear_cache, load_all_accounts, load_connections, load_transactions
from .deps import get_client, get_settings, get_store
from .recap import STATE_LABELS, USER_ACTION_STATES
from .wealth import family_of
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


def _readable_ink(hex_color: str) -> str:
    """Noir ou blanc, selon ce qui se lit sur ce fond.

    Un connecteur peut annoncer une couleur de marque très claire (l'un des
    tiens est blanc) : du texte blanc dessus serait invisible.
    """
    try:
        r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return "#ffffff"
    # Luminance perçue (ITU-R BT.601), suffisante pour un choix binaire.
    return "#111111" if (0.299 * r + 0.587 * g + 0.114 * b) > 0.6 else "#ffffff"


def _monogram(connector: Any) -> tuple[str, str, str]:
    """(initiales, fond, encre) d'une banque — l'équivalent d'un logo, en local.

    ``GET /connectors/{id}/logos`` renvoie une liste vide sur cette app, mais
    l'objet connecteur porte la **couleur de marque** et un **slug** : de quoi
    fabriquer une pastille reconnaissable, sans dépendre d'un CDN d'images ni
    faire fuiter la moindre requête vers l'extérieur.
    """
    raw = getattr(connector, "raw", None) or {}
    color = str(raw.get("color") or "").strip().lstrip("#")
    if len(color) != 6:
        color = "8a8a8a"
    slug = str(raw.get("slug") or "").strip()
    name = str(getattr(connector, "name", "") or "")
    initials = (slug or "".join(w[0] for w in name.split()[:2]) or "?")[:3].upper()
    return initials, f"#{color}", _readable_ink(color)


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
    initials: str = "?"
    color: str = "#8a8a8a"
    ink: str = "#ffffff"
    accounts: list[AccountRow] = field(default_factory=list)
    total: Decimal = Decimal(0)


@router.get("/connexions", response_class=HTMLResponse)
async def connections_page(
    request: Request,
    statut: str = "tous",
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
    now = datetime.now()

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
            age_days=(
                (now - connection.last_update).days if connection.last_update else None
            ),
            next_try=connection.next_try,
        )
        card.initials, card.color, card.ink = _monogram(connection.connector)
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

    # Les connexions en peine d'abord : c'est ce qu'on vient vérifier.
    cards.sort(key=lambda c: (c.ok, -(c.age_days or 0)))
    if statut == "erreur":
        cards = [c for c in cards if not c.ok]
    elif statut == "muette":
        cards = [
            c for c in cards
            if c.ok and (c.age_days or 0) > settings.silent_after_days
        ]

    imported = store.imported_summary(conn)
    orphans = [
        a for a in accounts if not any(a.id_connection == c.id for c in connections)
    ]

    # HTMX ne réclame que le fragment : filtrer ou synchroniser ne recharge
    # plus la page entière (en-tête, barre, bandeau et tous leurs calculs).
    template = (
        "_connections_list.html"
        if request.headers.get("HX-Request")
        else "connections.html"
    )
    return templates.TemplateResponse(
        request,
        template,
        {
            "request": request,
            "active": "connexions",
            "statut": statut,
            "cards": cards,
            "orphans": orphans,
            "imported": imported,
            "aliases": aliases,
            "currency": settings.base_currency,
            "history_months": settings.history_months,
            "silent_after_days": settings.silent_after_days,
            "today": date.today(),
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
    try:
        await client.update_connection(connection_id)
    except PowensAPIError:
        _log.warning("synchronisation refusée pour la connexion %s", connection_id)
    clear_cache()
    return await connections_page(
        request, statut=statut, client=client, settings=settings, conn=conn
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
    try:
        await client.update_account(account_id, disabled=False)
    except PowensAPIError:
        _log.warning("réintégration refusée pour le compte %s", account_id)
    clear_cache()
    return await connections_page(
        request, statut=statut, client=client, settings=settings, conn=conn
    )
