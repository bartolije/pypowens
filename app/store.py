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
from collections.abc import Iterable, Sequence
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
    conn.commit()
    try:
        db_path.chmod(0o600)  # balances are sensitive
    except OSError:
        pass
    return conn


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
