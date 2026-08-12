"""Local store: balance history, category overrides, series tracking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app import store


@dataclass
class Acc:
    id: int | None
    name: str | None = "Compte"
    type: str | None = "checking"
    currency: str | None = "EUR"
    balance: Decimal | None = Decimal("100")


@dataclass
class Series:
    key: str
    merchant: str
    amount: Decimal
    period_months: float = 1.0
    periodicity: str = "Mensuel"


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "t.db")
    yield connection
    connection.close()


# --------------------------------------------------------------- balance history

def test_snapshot_is_idempotent_per_day(conn):
    accounts = [Acc(1, balance=Decimal("100")), Acc(2, balance=Decimal("50"))]
    today = date(2026, 7, 25)
    store.record_snapshot(conn, accounts, day=today)
    store.record_snapshot(conn, accounts, day=today)  # same day again
    history = store.net_worth_history(conn)
    assert history == [(today, Decimal("150"))]


def test_net_worth_history_sums_accounts_per_day(conn):
    day1, day2 = date(2026, 7, 1), date(2026, 7, 2)
    store.record_snapshot(conn, [Acc(1, balance=Decimal("100"))], day=day1)
    store.record_snapshot(conn, [Acc(1, balance=Decimal("130"))], day=day2)
    assert store.net_worth_history(conn) == [(day1, Decimal("100")), (day2, Decimal("130"))]


def test_history_separates_currencies(conn):
    day = date(2026, 7, 1)
    store.record_snapshot(
        conn,
        [Acc(1, balance=Decimal("100")), Acc(2, currency="USD", balance=Decimal("900"))],
        day=day,
    )
    assert store.net_worth_history(conn, currency="EUR") == [(day, Decimal("100"))]
    assert store.net_worth_history(conn, currency="USD") == [(day, Decimal("900"))]


def test_previous_net_worth_ignores_today(conn):
    today = date(2026, 7, 25)
    store.record_snapshot(conn, [Acc(1, balance=Decimal("100"))], day=today - timedelta(days=2))
    store.record_snapshot(conn, [Acc(1, balance=Decimal("120"))], day=today)
    assert store.previous_net_worth(conn, before=today) == (
        today - timedelta(days=2),
        Decimal("100"),
    )


def test_previous_net_worth_is_none_on_first_run(conn):
    today = date(2026, 7, 25)
    store.record_snapshot(conn, [Acc(1)], day=today)
    assert store.previous_net_worth(conn, before=today) is None


# ------------------------------------------------------------------- overrides

def test_override_roundtrip(conn):
    store.set_override(conn, "mon marchand", "Sport")
    assert store.all_overrides(conn) == {"MON MARCHAND": "Sport"}
    store.clear_override(conn, "MON MARCHAND")
    assert store.all_overrides(conn) == {}


def test_resolve_category_prefers_override():
    from app.enrich import resolve_category

    assert resolve_category("CARREFOUR") == "Alimentation"
    assert resolve_category("CARREFOUR", {"CARREFOUR": "Autre"}) == "Autre"


# -------------------------------------------------------------- series tracking

def test_first_run_flags_nothing_as_new(conn):
    """Everything is unknown on day one — flagging it all would be noise."""
    items = [Series("NETFLIX", "Netflix", Decimal("13.49"))]
    changes = store.sync_series(conn, items, today=date(2026, 7, 1))
    assert changes["NETFLIX|Mensuel"]["new"] is False


def test_series_appearing_later_is_new(conn):
    day1 = date(2026, 7, 1)
    store.sync_series(conn, [Series("NETFLIX", "Netflix", Decimal("13.49"))], today=day1)
    changes = store.sync_series(
        conn,
        [
            Series("NETFLIX", "Netflix", Decimal("13.49")),
            Series("SPOTIFY", "Spotify", Decimal("11.99")),
        ],
        today=day1 + timedelta(days=1),
    )
    assert changes["SPOTIFY|Mensuel"]["new"] is True
    assert changes["NETFLIX|Mensuel"]["new"] is False


def test_price_increase_is_detected_with_previous_amount(conn):
    day1 = date(2026, 7, 1)
    store.sync_series(conn, [Series("NETFLIX", "Netflix", Decimal("13.49"))], today=day1)
    changes = store.sync_series(
        conn,
        [Series("NETFLIX", "Netflix", Decimal("15.99"))],
        today=day1 + timedelta(days=30),
    )
    flag = changes["NETFLIX|Mensuel"]
    assert flag["previous_amount"] == Decimal("13.49")
    assert flag["increase_pct"] == pytest.approx(18.5, abs=0.5)


def test_small_variation_is_not_flagged(conn):
    day1 = date(2026, 7, 1)
    store.sync_series(conn, [Series("EDF", "Edf", Decimal("100.00"))], today=day1)
    changes = store.sync_series(
        conn, [Series("EDF", "Edf", Decimal("100.50"))], today=day1 + timedelta(days=30)
    )
    assert changes["EDF|Mensuel"]["increase_pct"] is None


def test_price_drop_is_not_an_increase(conn):
    day1 = date(2026, 7, 1)
    store.sync_series(conn, [Series("EDF", "Edf", Decimal("100.00"))], today=day1)
    changes = store.sync_series(
        conn, [Series("EDF", "Edf", Decimal("80.00"))], today=day1 + timedelta(days=30)
    )
    assert changes["EDF|Mensuel"]["increase_pct"] is None


def test_periodicity_change_is_a_distinct_series(conn):
    """A yearly plan is not the same commitment as the monthly one."""
    day1 = date(2026, 7, 1)
    store.sync_series(conn, [Series("GYM", "Gym", Decimal("30"))], today=day1)
    changes = store.sync_series(
        conn,
        [Series("GYM", "Gym", Decimal("300"), period_months=12.0, periodicity="Annuel")],
        today=day1 + timedelta(days=1),
    )
    assert changes["GYM|Annuel"]["new"] is True


# ------------------------------------------------------------------- backup

def test_backup_writes_a_dated_copy_with_data(conn, tmp_path):
    day = date(2026, 8, 1)
    store.record_snapshot(conn, [Acc(1, balance=Decimal("100"))], day=day)
    conn.commit()

    written = store.backup(conn, tmp_path / "t.db", day=day)
    assert written is not None
    assert written.parent.name == ".backups"
    assert written.name == "t-2026-08-01.db"

    # La copie est une vraie base, lisible, avec les données du jour.
    copy = store.connect(written)
    try:
        assert store.net_worth_history(copy) == [(day, Decimal("100"))]
    finally:
        copy.close()


def test_backup_is_once_per_day(conn, tmp_path):
    day = date(2026, 8, 1)
    assert store.backup(conn, tmp_path / "t.db", day=day) is not None
    assert store.backup(conn, tmp_path / "t.db", day=day) is None  # déjà faite


def test_backup_rotation_keeps_most_recent(conn, tmp_path):
    for offset in range(5):
        store.backup(conn, tmp_path / "t.db", day=date(2026, 8, 1) + timedelta(days=offset), keep=3)
    names = sorted(p.name for p in (tmp_path / ".backups").glob("*.db"))
    assert names == ["t-2026-08-03.db", "t-2026-08-04.db", "t-2026-08-05.db"]


# ------------------------------------------------------------- history windows

def test_net_worth_history_tout_keeps_the_origin(conn):
    """« TOUT » doit partir du premier jour archivé, pas des 180 derniers.

    L'ancien ``points[-limit:]`` tronquait la fenêtre ET faisait glisser la
    « variation depuis le… » chaque jour au lieu de la mesurer depuis l'origine.
    """
    start = date(2025, 1, 1)
    for offset in range(400):
        store.record_snapshot(
            conn, [Acc(1, balance=Decimal(offset))], day=start + timedelta(days=offset)
        )
    history = store.net_worth_history(conn)
    assert len(history) <= 180                        # borné pour le SVG
    assert history[0] == (start, Decimal(0))          # l'origine est conservée
    assert history[-1] == (start + timedelta(days=399), Decimal(399))


def test_net_worth_history_since_filters_before_sampling(conn):
    start = date(2026, 1, 1)
    for offset in range(10):
        store.record_snapshot(
            conn, [Acc(1, balance=Decimal(offset))], day=start + timedelta(days=offset)
        )
    history = store.net_worth_history(conn, since=start + timedelta(days=5))
    assert history[0][0] == start + timedelta(days=5)
    assert len(history) == 5


def test_nan_balance_cannot_poison_the_history():
    """Un NaN de l'API ne doit jamais atteindre une somme de soldes."""
    from pypowens.models import _parse_decimal

    assert _parse_decimal("NaN") is None
    assert _parse_decimal("Infinity") is None
    assert _parse_decimal("-Infinity") is None
    assert _parse_decimal("12.5") == Decimal("12.5")
