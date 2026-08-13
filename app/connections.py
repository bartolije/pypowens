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

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse

from pypowens import Account, PowensClient

from . import store
from .config import Settings
from .data import load_all_accounts, load_connections, load_transactions
from .deps import get_client, get_settings, get_store
from .recap import STATE_LABELS, USER_ACTION_STATES
from .wealth import family_of
from .web import templates

router = APIRouter()


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
    accounts: list[AccountRow] = field(default_factory=list)
    total: Decimal = Decimal(0)


@router.get("/connexions", response_class=HTMLResponse)
async def connections_page(
    request: Request,
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

    imported = store.imported_summary(conn)
    orphans = [
        a for a in accounts if not any(a.id_connection == c.id for c in connections)
    ]

    return templates.TemplateResponse(
        request,
        "connections.html",
        {
            "request": request,
            "active": "connexions",
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
