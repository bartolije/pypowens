"""Collecte et archive ce que Powens n'historisera pas à notre place.

Deux choses sont périssables :

* le **solde** d'un compte sans lignes de titres (fonds euros, PER, livret, compte
  courant) : Powens ne répond qu'au présent, donc un jour non collecté est un jour perdu
  pour toujours ;
* les **valorisations** des lignes de titres : l'API les garde depuis la création de la
  connexion, mais rien ne promet qu'elle les gardera indéfiniment.

D'où un collecteur qui **rattrape** au lieu de supposer un passage quotidien : il ne
demande que ce qui manque depuis le dernier jour archivé. Un oubli d'une semaine se
rattrape donc tout seul pour les titres, et ne coûte que les soldes de la semaine.

Utilisable sans l'interface : ``python -m app.collector``.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from pypowens import Investment, InvestmentValue, PowensAPIError, PowensAuthError, PowensClient

from . import store
from .config import Settings, get_settings
from .data import SPENDING_ACCOUNT_TYPES
from .state import bootstrap_client, try_renew

_log = logging.getLogger(__name__)

# Comptes dont la valeur bouge sans qu'aucune opération ne l'explique.
INVESTMENT_TYPES = frozenset({"market", "pea", "per", "lifeinsurance"})

# Marge de sécurité lors du rattrapage : on redemande quelques jours déjà archivés, au
# cas où une VL publiée en séance ait été corrigée depuis. L'écriture est idempotente.
OVERLAP_DAYS = 3

# Historiques de lignes de titres demandés en parallèle.
HISTORY_CONCURRENCY = 4


@dataclass
class CollectReport:
    """Ce qu'un passage a réellement enregistré."""

    accounts: int = 0
    snapshots: int = 0
    lines: int = 0
    values: int = 0
    skipped: int = 0
    since: date | None = None

    def __str__(self) -> str:
        window = f" depuis le {self.since:%d/%m/%Y}" if self.since else ""
        return (
            f"{self.snapshots} solde(s) enregistré(s) sur {self.accounts} compte(s), "
            f"{self.values} valorisation(s) sur {self.lines} ligne(s){window}"
            + (f", {self.skipped} ligne(s) sans historique" if self.skipped else "")
        )


def _resume_from(conn: sqlite3.Connection) -> date | None:
    """Premier jour à redemander : le dernier archivé, moins le recouvrement de sûreté.

    ``None`` la première fois — on prend alors tout ce que l'API veut bien donner, ce qui
    remonte jusqu'à la création de la connexion.
    """
    span = store.investment_value_span(conn)
    if span is None:
        return None
    return span[1] - timedelta(days=OVERLAP_DAYS)


async def collect(
    client: PowensClient,
    conn: sqlite3.Connection,
    *,
    settings: Settings | None = None,
    day: date | None = None,
) -> CollectReport:
    """Enregistre les soldes du jour et rattrape l'historique de valorisation."""
    settings = settings or get_settings()
    report = CollectReport(since=_resume_from(conn))

    accounts = (await client.list_accounts(include_disabled=False)).accounts
    report.snapshots = store.record_snapshot(
        conn, accounts, day=day, default_currency=settings.base_currency
    )
    report.accounts = len(accounts)

    # Le solde d'un compte courant se reconstruit depuis ses opérations ; celui d'un
    # support d'investissement, non. On ne dépense donc des appels que pour ces derniers.
    holders = {a.id for a in accounts if (a.type or "") in INVESTMENT_TYPES}
    if not holders:
        return report

    investments = [
        i
        for i in await client.list_investments()
        if i.id_account in holders and i.id is not None and i.id_account is not None
    ]
    since = report.since.isoformat() if report.since else None
    # Une ligne = un appel à l'API : enchaînés, vingt lignes coûtaient vingt
    # allers-retours (six secondes). Quelques appels en parallèle suffisent, le
    # sémaphore évitant de déclencher la limitation de débit de Powens.
    gate = asyncio.Semaphore(HISTORY_CONCURRENCY)

    async def _history(inv: Investment) -> list[InvestmentValue] | None:
        investment_id = inv.id
        if investment_id is None:  # exclu par le filtre ci-dessus ; pour le typage
            return None
        async with gate:
            try:
                return await client.list_investment_history(investment_id, min_date=since)
            except PowensAPIError:
                # Une ligne sans historique (les liquidités, par exemple) ne doit pas
                # interrompre la collecte des autres.
                return None

    histories = await asyncio.gather(*(_history(inv) for inv in investments))
    for inv, values in zip(investments, histories, strict=True):
        report.lines += 1
        if not values:
            report.skipped += 1
            continue
        report.values += store.save_investment_values(
            conn,
            values,
            account_id=inv.id_account,  # type: ignore[arg-type]
            label=inv.label,
            code=inv.code,
        )
    return report


def _collect_benchmark(conn: sqlite3.Connection, settings: Settings) -> int:
    """Archive les clôtures de l'indice de référence (yfinance, best effort).

    Même logique de rattrapage que les VL : on repart du dernier jour archivé
    (moins le recouvrement), et la page performance lit ensuite en local.
    """
    ticker = settings.benchmark_ticker
    if not ticker:
        return 0
    try:
        import yfinance as yf  # noqa: PLC0415 — optionnel, comme dans classify
    except ImportError:
        _log.warning("yfinance absent — pas d'archivage de l'indice %s", ticker)
        return 0
    last = store.benchmark_last_day(conn, ticker)
    start = (last - timedelta(days=OVERLAP_DAYS)) if last else None
    try:
        history = yf.Ticker(ticker).history(
            start=start.isoformat() if start else None,
            period=None if start else "5y",
            auto_adjust=True,
        )
    except Exception:  # noqa: BLE001 — une source externe ne casse pas la collecte
        _log.warning("indice %s : téléchargement impossible", ticker, exc_info=True)
        return 0
    values = [
        (idx.date(), Decimal(str(round(float(close), 4))))
        for idx, close in history["Close"].items()
        if close == close  # écarte les NaN pandas
    ]
    return store.save_benchmark_values(conn, ticker, values)


async def _push_alerts(client: PowensClient, conn: sqlite3.Connection) -> None:
    """Pousse les alertes de santé hors de l'app.

    Elles ne vivent sinon que dans le bandeau : tant que l'onglet n'est pas
    ouvert, personne ne les voit. Le collecteur, lui, passe plusieurs fois par
    jour — c'est lui qui notifie.
    """
    try:
        from .health import connection_alerts  # import tardif (module routes)
        from .notify import notify

        alerts = await connection_alerts(client, conn)
        lines = [f"{a['title']} : {a['detail']}" for a in alerts[:3]]
        pending = store.pending_subscription_alerts(conn)
        if pending:
            lines.append(f"{pending} alerte(s) d'abonnement à examiner")
        if lines:
            # osascript ou webhook : synchrones, jusqu'à dix secondes — hors boucle.
            await asyncio.to_thread(notify, "Powens Finance", " · ".join(lines))
    except Exception:  # noqa: BLE001 — la notification ne casse jamais la collecte
        _log.warning("notification des alertes impossible", exc_info=True)


async def run_once(
    client: PowensClient, conn: sqlite3.Connection, settings: Settings
) -> CollectReport:
    """Une passe complète : copie de sûreté, indice, collecte, alertes.

    Extrait de ``main`` pour que le processus web puisse la rejouer à intervalle
    régulier (cf. ``scheduled``) avec sa propre connexion et son propre client.
    """
    # Copie de sûreté AVANT d'écrire : si ce passage tourne mal, l'état de la
    # veille reste récupérable dans .backups/.
    # Déclenchée depuis le processus web (``scheduled``), cette passe partage la
    # boucle d'événements avec les pages : la copie de la base et surtout
    # yfinance (réseau synchrone, plusieurs secondes) tournent donc dans un
    # thread, sans quoi chaque passage figeait l'application le temps du
    # téléchargement. La connexion SQLite est ouverte en mode partagé
    # (check_same_thread=False) et le module sérialise ses accès.
    try:
        written = await asyncio.to_thread(store.backup, conn, settings.db_path)
        if written:
            _log.info("copie de sûreté écrite : %s", written)
    except (sqlite3.Error, OSError):
        _log.exception("copie de sûreté impossible — la collecte continue sans")
    benchmark_count = await asyncio.to_thread(_collect_benchmark, conn, settings)
    if benchmark_count:
        _log.info(
            "indice %s : %d clôture(s) archivée(s)", settings.benchmark_ticker, benchmark_count
        )
    # Comptes épinglés que Powens aurait désactivés depuis le dernier passage :
    # les réintégrer AVANT le relevé, sinon le solde du jour leur manquerait.
    try:
        from .health import reactivate_pinned_accounts  # import tardif (module routes)

        restored = await reactivate_pinned_accounts(client, conn)
        if restored:
            _log.info("%d compte(s) épinglé(s) réintégré(s) avant le relevé", restored)
    except Exception:  # noqa: BLE001 — best-effort, la collecte continue
        _log.warning("réintégration des comptes épinglés impossible", exc_info=True)
    try:
        report = await collect(client, conn, settings=settings)
    except PowensAuthError:
        # Le token finit toujours par mourir. Sans ce renouvellement, le
        # collecteur s'arrêtait en silence — et chaque jour non collecté est
        # un solde perdu pour toujours.
        if not await try_renew(client, settings):
            raise
        _log.info("token renouvelé, reprise de la collecte")
        report = await collect(client, conn, settings=settings)
    await _push_alerts(client, conn)
    return report


# Laisser à l'app le temps de se réveiller avant la première passe, sans pour
# autant attendre un intervalle entier : un redéploiement en fin de journée ne
# doit pas coûter le solde du jour.
_FIRST_RUN_DELAY = 300


async def scheduled(
    client: PowensClient,
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    hours: float,
) -> None:
    """Collecte périodique, à l'intérieur du processus qui sert l'app.

    Chez un hébergeur, un volume ne se monte que sur un seul service : un « cron
    job » voisin ne verrait pas la base, et écrirait dans un système de fichiers
    jeté à la fin de son exécution. La planification doit donc vivre ici, faute
    de quoi une app déployée cesse purement et simplement d'archiver les soldes
    — précisément ce qu'un poste de travail éteint ne permettait déjà plus.

    Aucune erreur n'interrompt la boucle : une panne réseau ou une API en vrac
    ne doivent pas coûter tous les jours suivants.
    """
    delay = min(_FIRST_RUN_DELAY, hours * 3600)
    while True:
        await asyncio.sleep(delay)
        delay = hours * 3600
        try:
            report = await run_once(client, conn, settings)
            _log.info("collecte planifiée — %s", report)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — la boucle survit à tout le reste
            _log.exception("collecte planifiée en échec — nouvelle tentative au tour suivant")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s"
    )
    settings = get_settings()
    conn = store.connect(settings.db_path)
    client = await bootstrap_client(settings)
    try:
        report = await run_once(client, conn, settings)
    finally:
        await client.aclose()
        conn.close()
    print(f"{date.today():%d/%m/%Y} — {report}")


if __name__ == "__main__":
    asyncio.run(main())


__all__ = [
    "CollectReport",
    "HISTORY_CONCURRENCY",
    "INVESTMENT_TYPES",
    "SPENDING_ACCOUNT_TYPES",
    "collect",
    "main",
    "run_once",
    "scheduled",
]
