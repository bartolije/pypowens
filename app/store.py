"""Local SQLite store — the app's own memory, next to Powens' live data.

Powens only ever answers "what is true now". Three things need history instead:

* **balance snapshots** — a real net-worth curve, instead of guessing a variation
  from an undocumented ``diff`` field;
* **category overrides** — a correction made once must stick, without editing code;
* **series state** — knowing a subscription is *new*, or that its price *went up*,
  requires remembering what it looked like last time.

Connections are short-lived and statements are tiny (a few thousand rows), so
plain synchronous ``sqlite3`` is used: every call here is sub-millisecond, well
below the cost of one Powens round-trip.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

SCHEMA = """
CREATE TABLE IF NOT EXISTS balance_snapshot (
    day        TEXT    NOT NULL,          -- YYYY-MM-DD
    account_id INTEGER NOT NULL,
    name       TEXT,
    type       TEXT,
    currency   TEXT    NOT NULL,
    balance    TEXT    NOT NULL,          -- Decimal serialized as text
    PRIMARY KEY (day, account_id)
);
CREATE INDEX IF NOT EXISTS idx_snapshot_day ON balance_snapshot (day);

CREATE TABLE IF NOT EXISTS category_override (
    merchant_key TEXT PRIMARY KEY,
    category     TEXT NOT NULL,
    updated      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS series_state (
    series_key    TEXT PRIMARY KEY,       -- merchant key + periodicity
    merchant      TEXT NOT NULL,
    amount        TEXT NOT NULL,
    period_months REAL,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    acknowledged  INTEGER NOT NULL DEFAULT 0
);

-- Comptes et opérations importés depuis un relevé, pour ce qu'aucun connecteur ne
-- remonte. Les ids exposés à l'app sont négatifs (voir account_id() plus bas) :
-- l'espace positif appartient à Powens, et les deux jeux d'ids se croisent dans des
-- ensembles d'exclusion (virements internes, transactions d'une série).
--
-- ``powens_account_id`` rattache le compte importé au compte Powens qu'un connecteur
-- s'est mis à remonter depuis : le relevé garde alors le seul rôle qu'il joue encore,
-- l'historique *antérieur* à ce que le connecteur couvre. Voir link_imported_account().
CREATE TABLE IF NOT EXISTS imported_account (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    label             TEXT NOT NULL UNIQUE,
    type              TEXT NOT NULL,
    currency          TEXT NOT NULL,
    created           TEXT NOT NULL,
    powens_account_id INTEGER          -- NULL = compte autonome
);

CREATE TABLE IF NOT EXISTS imported_transaction (
    fingerprint TEXT PRIMARY KEY,         -- identité stable, deux exports se recouvrent
    account_id  INTEGER NOT NULL,         -- imported_account.id (positif, en base)
    day         TEXT    NOT NULL,
    value       TEXT    NOT NULL,         -- Decimal sérialisé, signé
    wording     TEXT    NOT NULL,
    type        TEXT    NOT NULL,
    source      TEXT    NOT NULL,         -- nom du fichier d'origine
    imported    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_imported_day ON imported_transaction (day);
"""


class SeriesLike(Protocol):
    key: str
    merchant: str
    amount: Decimal
    period_months: float
    periodicity: str


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (and migrate) the local database."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the connection is shared by the app, and Starlette may
    # serve requests from its portal thread. Safe here — one local user, and every
    # write below is a single short statement followed by a commit.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    try:
        db_path.chmod(0o600)  # balances are sensitive
    except OSError:
        pass
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Ajoute les colonnes apparues après coup.

    ``CREATE TABLE IF NOT EXISTS`` ne fait rien sur une table déjà présente : une base
    créée avant une colonne ne l'aurait jamais, et SQLite n'a pas de
    ``ADD COLUMN IF NOT EXISTS``.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(imported_account)")}
    if "powens_account_id" not in columns:
        conn.execute("ALTER TABLE imported_account ADD COLUMN powens_account_id INTEGER")
        conn.commit()


# ------------------------------------------------------------ balance history


class AccountLike(Protocol):
    id: int | None
    name: str | None
    type: str | None
    currency: str | None
    balance: Decimal | None


def record_snapshot(
    conn: sqlite3.Connection,
    accounts: Iterable[AccountLike],
    *,
    day: date | None = None,
    default_currency: str = "EUR",
) -> int:
    """Store today's balance for each account (idempotent: one row per day+account)."""
    day = day or date.today()
    rows = [
        (
            day.isoformat(),
            acc.id,
            acc.name,
            acc.type,
            (acc.currency or default_currency).upper(),
            str(acc.balance if acc.balance is not None else Decimal(0)),
        )
        for acc in accounts
        if acc.id is not None
    ]
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO balance_snapshot"
        " (day, account_id, name, type, currency, balance) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def net_worth_history(
    conn: sqlite3.Connection, *, currency: str = "EUR", limit: int = 180
) -> list[tuple[date, Decimal]]:
    """Daily net worth (one point per recorded day), oldest first."""
    cursor = conn.execute(
        "SELECT day, balance FROM balance_snapshot WHERE currency = ? ORDER BY day",
        (currency.upper(),),
    )
    totals: dict[str, Decimal] = {}
    for row in cursor:
        totals[row["day"]] = totals.get(row["day"], Decimal(0)) + Decimal(row["balance"])
    points = [(date.fromisoformat(day), total) for day, total in sorted(totals.items())]
    return points[-limit:]


def previous_net_worth(
    conn: sqlite3.Connection, *, currency: str = "EUR", before: date | None = None
) -> tuple[date, Decimal] | None:
    """Most recent net worth recorded strictly before ``before`` (default: today)."""
    before = before or date.today()
    history = [p for p in net_worth_history(conn, currency=currency) if p[0] < before]
    return history[-1] if history else None


# -------------------------------------------------------- category overrides


def all_overrides(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        row["merchant_key"]: row["category"]
        for row in conn.execute("SELECT merchant_key, category FROM category_override")
    }


def set_override(conn: sqlite3.Connection, merchant_key: str, category: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO category_override (merchant_key, category, updated)"
        " VALUES (?, ?, ?)",
        (merchant_key.upper().strip(), category.strip(), date.today().isoformat()),
    )
    conn.commit()


def clear_override(conn: sqlite3.Connection, merchant_key: str) -> None:
    conn.execute(
        "DELETE FROM category_override WHERE merchant_key = ?", (merchant_key.upper().strip(),)
    )
    conn.commit()


# ---------------------------------------------------------- imported statements

# Les ids Powens sont des entiers positifs. Un compte importé est exposé à l'app avec
# l'opposé de son id en base, et ses opérations avec des ids négatifs distincts, pour
# qu'aucune collision ne soit possible entre les deux sources.
def account_id(db_id: int) -> int:
    return -db_id


def upsert_imported_account(
    conn: sqlite3.Connection,
    label: str,
    *,
    type: str = "checking",
    currency: str = "EUR",
) -> int:
    """Crée (ou retrouve) le compte importé nommé ``label``, et renvoie son id en base."""
    label = label.strip() or "Compte importé"
    conn.execute(
        "INSERT OR IGNORE INTO imported_account (label, type, currency, created)"
        " VALUES (?, ?, ?, ?)",
        (label, type, currency.upper(), date.today().isoformat()),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM imported_account WHERE label = ?", (label,)).fetchone()
    return int(row["id"])


def link_imported_account(
    conn: sqlite3.Connection, db_id: int, powens_account_id: int | None
) -> None:
    """Rattache (ou détache, avec ``None``) un compte importé à un compte Powens.

    Rattacher veut dire : ce relevé et ce compte sont la même chose. Le compte importé
    cesse d'exister comme compte à part — son solde ne s'ajoute plus à rien — et ses
    opérations sont reversées au compte Powens, mais seulement celles que le connecteur
    ne couvre pas (voir :func:`imported_transactions`).

    Les relevés de solde déjà pris pour le compte importé sont effacés : ils ont été
    enregistrés à une époque où il comptait pour lui-même, et les laisser laisserait une
    bosse du montant du doublon dans la courbe de patrimoine.
    """
    conn.execute(
        "UPDATE imported_account SET powens_account_id = ? WHERE id = ?",
        (powens_account_id, db_id),
    )
    if powens_account_id is not None:
        conn.execute(
            "DELETE FROM balance_snapshot WHERE account_id = ?", (account_id(db_id),)
        )
    conn.commit()


def imported_links(conn: sqlite3.Connection) -> dict[int, int]:
    """``{id en base: id du compte Powens}`` pour les seuls comptes rattachés."""
    return {
        int(row["id"]): int(row["powens_account_id"])
        for row in conn.execute(
            "SELECT id, powens_account_id FROM imported_account"
            " WHERE powens_account_id IS NOT NULL"
        )
    }


def imported_accounts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Comptes importés autonomes, sous la forme d'un payload d'API Powens.

    Les comptes rattachés sont volontairement absents : leur solde est celui du compte
    Powens correspondant, et les exposer tous les deux compterait l'argent deux fois.
    """
    return [
        {
            "id": account_id(row["id"]),
            "id_connection": None,
            "name": row["label"],
            "type": row["type"],
            "balance": str(_imported_balance(conn, row["id"])),
            "currency": {"id": row["currency"]},
            "iban": None,
        }
        for row in conn.execute(
            "SELECT id, label, type, currency FROM imported_account"
            " WHERE powens_account_id IS NULL ORDER BY label"
        )
    ]


def _imported_balance(conn: sqlite3.Connection, db_id: int) -> Decimal:
    """Somme des opérations importées du compte.

    Un relevé ne porte pas le solde courant, seulement des mouvements : le total est
    donc une somme de flux, pas une photographie. Il est affiché comme tel.
    """
    total = Decimal(0)
    for row in conn.execute(
        "SELECT value FROM imported_transaction WHERE account_id = ?", (db_id,)
    ):
        total += Decimal(row["value"])
    return total


def save_imported(
    conn: sqlite3.Connection,
    db_id: int,
    transactions: Sequence[Any],
    fingerprints: Sequence[str],
    *,
    source: str,
) -> tuple[int, int]:
    """Enregistre les opérations parsées. Renvoie ``(ajoutées, doublons ignorés)``.

    L'empreinte est la clé primaire : réimporter un relevé qui recouvre le précédent
    est sans effet, ce qui est le cas d'usage normal (on exporte par périodes qui se
    chevauchent).
    """
    today = date.today().isoformat()
    added = 0
    for txn, digest in zip(transactions, fingerprints, strict=True):
        cursor = conn.execute(
            "INSERT OR IGNORE INTO imported_transaction"
            " (fingerprint, account_id, day, value, wording, type, source, imported)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                digest,
                db_id,
                txn.date.isoformat(),
                str(txn.value),
                txn.wording,
                txn.type,
                source,
                today,
            ),
        )
        added += cursor.rowcount or 0
    conn.commit()
    return added, len(fingerprints) - added


def imported_transactions(
    conn: sqlite3.Connection,
    *,
    since: date | None = None,
    ceilings: Mapping[int, date] | None = None,
) -> list[dict[str, Any]]:
    """Opérations importées, sous forme de payloads d'API Powens.

    Les ids sont négatifs et dérivés du ``rowid``, donc stables d'un appel à l'autre —
    des séries détectées y font référence.

    ``ceilings`` donne, par compte importé rattaché (id en base), la première date que le
    connecteur couvre : les opérations de ce jour et des suivants sont écartées, puisque
    Powens les remonte désormais lui-même. Sans cette borne, la période de recouvrement
    entre le relevé et le connecteur serait comptée deux fois dans toutes les analyses.

    Une opération d'un compte rattaché est présentée sous l'id du compte Powens : c'est
    bien le même compte, et les pages qui ne regardent que les comptes courants doivent
    continuer à voir cet historique.
    """
    sql = (
        "SELECT t.rowid AS rowid, t.account_id AS account_id, t.day AS day,"
        " t.value AS value, t.wording AS wording, t.type AS type,"
        " a.powens_account_id AS powens_account_id"
        " FROM imported_transaction t JOIN imported_account a ON a.id = t.account_id"
    )
    params: tuple[Any, ...] = ()
    if since is not None:
        sql += " WHERE t.day >= ?"
        params = (since.isoformat(),)
    sql += " ORDER BY t.day"

    out: list[dict[str, Any]] = []
    for row in conn.execute(sql, params):
        ceiling = (ceilings or {}).get(row["account_id"])
        if ceiling is not None and row["day"] >= ceiling.isoformat():
            continue
        linked = row["powens_account_id"]
        out.append(
            {
                "id": -int(row["rowid"]),
                "id_account": int(linked) if linked is not None else account_id(row["account_id"]),
                "date": row["day"],
                "value": row["value"],
                "type": row["type"],
                "wording": row["wording"],
                "simplified_wording": row["wording"],
                "original_wording": row["wording"],
                "coming": False,
            }
        )
    return out


def imported_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Un récapitulatif par compte importé, pour la page d'import."""
    return [
        dict(row)
        for row in conn.execute(
            "SELECT a.id, a.label, a.type, a.currency, a.powens_account_id,"
            " COUNT(t.fingerprint) AS operations,"
            " MIN(t.day) AS first_day, MAX(t.day) AS last_day,"
            " GROUP_CONCAT(DISTINCT t.source) AS sources"
            " FROM imported_account a"
            " LEFT JOIN imported_transaction t ON t.account_id = a.id"
            " GROUP BY a.id ORDER BY a.label"
        )
    ]


def delete_imported_account(conn: sqlite3.Connection, db_id: int) -> int:
    """Supprime un compte importé et toutes ses opérations."""
    cursor = conn.execute("DELETE FROM imported_transaction WHERE account_id = ?", (db_id,))
    conn.execute("DELETE FROM imported_account WHERE id = ?", (db_id,))
    conn.commit()
    return cursor.rowcount or 0


# ------------------------------------------------------------- series tracking


def series_key(item: SeriesLike) -> str:
    """Stable identity of a recurring series: merchant + periodicity."""
    return f"{item.key}|{item.periodicity}"


def sync_series(
    conn: sqlite3.Connection,
    items: Sequence[SeriesLike],
    *,
    today: date | None = None,
    increase_threshold: float = 0.02,
) -> dict[str, dict[str, Any]]:
    """Compare detected series with what was seen before, then persist the new state.

    Returns, per :func:`series_key`: ``{"new": bool, "previous_amount": Decimal|None,
    "increase_pct": float|None, "first_seen": date}``. A series is *new* only if it
    was never recorded — so the very first run does not flag everything at once.
    """
    today = today or date.today()
    known = {
        row["series_key"]: row
        for row in conn.execute(
            "SELECT series_key, amount, first_seen FROM series_state"
        )
    }
    first_run = not known

    result: dict[str, dict[str, Any]] = {}
    rows = []
    for item in items:
        key = series_key(item)
        row = known.get(key)
        previous = Decimal(row["amount"]) if row else None
        increase_pct: float | None = None
        if previous is not None and previous > 0 and item.amount > previous:
            delta = float((item.amount - previous) / previous)
            if delta >= increase_threshold:
                increase_pct = delta * 100
        result[key] = {
            "new": row is None and not first_run,
            "previous_amount": previous,
            "increase_pct": increase_pct,
            "first_seen": date.fromisoformat(row["first_seen"]) if row else today,
        }
        rows.append(
            (
                key,
                item.merchant,
                str(item.amount),
                float(item.period_months),
                row["first_seen"] if row else today.isoformat(),
                today.isoformat(),
            )
        )

    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO series_state"
            " (series_key, merchant, amount, period_months, first_seen, last_seen)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    return result
