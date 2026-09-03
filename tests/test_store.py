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
    assert len(history) <= 180  # borné pour le SVG
    assert history[0] == (start, Decimal(0))  # l'origine est conservée
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


# ------------------------------------------------------- alertes persistantes


def _sync(conn, *series):
    return store.sync_series(conn, list(series))


def test_alert_survives_reloads_until_acknowledged(conn):
    netflix = Series("NETFLIX", "Netflix", Decimal("13.49"))
    _sync(conn, netflix)  # premier passage : tout est nouveau, aucune alerte

    spotify = Series("SPOTIFY", "Spotify", Decimal("10.99"))
    changes = _sync(conn, netflix, spotify)
    assert changes[store.series_key(spotify)]["new"] is True

    # F5 : l'ancien diff one-shot faisait disparaître l'alerte ici.
    changes = _sync(conn, netflix, spotify)
    assert changes[store.series_key(spotify)]["new"] is True

    assert store.acknowledge_alerts(conn) == 1
    changes = _sync(conn, netflix, spotify)
    assert changes[store.series_key(spotify)]["new"] is False


def test_increase_alert_keeps_the_previous_amount(conn):
    netflix = Series("NETFLIX", "Netflix", Decimal("13.49"))
    _sync(conn, netflix)
    raised = Series("NETFLIX", "Netflix", Decimal("15.49"))
    changes = _sync(conn, raised)
    key = store.series_key(raised)
    assert changes[key]["increase_pct"] == pytest.approx(14.8, abs=0.1)
    assert changes[key]["previous_amount"] == Decimal("13.49")

    # Toujours visible après rechargement, avec le même « avant → après ».
    changes = _sync(conn, raised)
    assert changes[key]["increase_pct"] == pytest.approx(14.8, abs=0.1)
    assert changes[key]["previous_amount"] == Decimal("13.49")

    store.acknowledge_alerts(conn)
    assert _sync(conn, raised)[key]["increase_pct"] is None


def test_acknowledged_increase_does_not_retrigger_without_change(conn):
    netflix = Series("NETFLIX", "Netflix", Decimal("13.49"))
    _sync(conn, netflix)
    raised = Series("NETFLIX", "Netflix", Decimal("15.49"))
    _sync(conn, raised)
    store.acknowledge_alerts(conn)
    # Même montant au passage suivant : pas de nouvelle alerte fantôme.
    changes = _sync(conn, raised)
    key = store.series_key(raised)
    assert changes[key]["increase_pct"] is None
    assert changes[key]["new"] is False


# ------------------------------------------------- trous et périmètre de comptes


def _snap(conn, day, *accounts):
    store.record_snapshot(conn, list(accounts), day=day)


def test_temporary_absence_is_filled_with_last_known_balance(conn):
    """Le prêt fantôme : une connexion en panne fait disparaître un compte
    quelques jours — la courbe ne doit plus sauter de son montant."""
    d = date(2026, 8, 1)
    loan = Acc(2, name="Prêt", type="loan", balance=Decimal("-1000"))
    _snap(conn, d, Acc(1), loan)
    _snap(conn, d + timedelta(days=1), Acc(1))  # prêt absent
    _snap(conn, d + timedelta(days=2), Acc(1))  # toujours absent
    _snap(
        conn,
        d + timedelta(days=3),
        Acc(1),
        Acc(2, name="Prêt", type="loan", balance=Decimal("-990")),
    )
    history = store.net_worth_history(conn)
    # 100 - 1000 = -900 partout : l'absence des jours 2-3 est comblée à -1000.
    assert [v for _, v in history] == [
        Decimal("-900"),
        Decimal("-900"),
        Decimal("-900"),
        Decimal("-890"),
    ]


def test_fill_never_extends_before_first_or_after_last_appearance(conn):
    d = date(2026, 8, 1)
    _snap(conn, d, Acc(1))
    _snap(conn, d + timedelta(days=1), Acc(1), Acc(2, balance=Decimal("50")))
    _snap(conn, d + timedelta(days=2), Acc(1))  # le compte 2 ne revient jamais
    history = store.net_worth_history(conn)
    assert [v for _, v in history] == [Decimal("100"), Decimal("150"), Decimal("100")]


def test_perimeter_changes_flags_durable_entries_and_exits(conn):
    d = date(2026, 8, 1)
    _snap(conn, d, Acc(1, name="Courant"))
    # Le compte 2 ENTRE le 2, le compte 1 SORT après le 2 (effet visible le 3).
    _snap(
        conn,
        d + timedelta(days=1),
        Acc(1, name="Courant"),
        Acc(2, name="Livret", balance=Decimal("50")),
    )
    _snap(conn, d + timedelta(days=2), Acc(2, name="Livret", balance=Decimal("50")))

    changes = store.perimeter_changes(conn)
    assert len(changes) == 2
    entered, left = changes[0], changes[1]
    assert entered["day"] == d + timedelta(days=1)
    assert entered["entered"] == ["Livret"]
    assert entered["delta"] == Decimal("50")
    assert left["day"] == d + timedelta(days=2)
    assert left["left"] == ["Courant"]
    assert left["delta"] == Decimal("-100")


def test_an_acknowledged_perimeter_change_stops_being_reported(conn):
    """Un changement de périmètre est un fait permanent : la courbe portera
    toujours ce saut. Une fois compris, répéter l'explication est du bruit —
    sans pour autant justifier de trafiquer l'historique pour lisser la courbe.
    """
    d = date(2026, 8, 1)
    _snap(conn, d, Acc(1, name="Courant"))
    _snap(
        conn,
        d + timedelta(days=1),
        Acc(1, name="Courant"),
        Acc(2, name="Livret", balance=Decimal("50")),
    )

    assert len(store.perimeter_changes(conn)) == 1
    store.acknowledge_perimeter(conn, d + timedelta(days=1), "Livret")
    assert store.perimeter_changes(conn) == []

    # Le solde archivé, lui, n'a pas bougé d'un centime : la courbe est intacte.
    assert [v for _, v in store.net_worth_history(conn)] == [Decimal("100"), Decimal("150")]

    store.forget_perimeter_ack(conn, (d + timedelta(days=1)).isoformat())
    assert len(store.perimeter_changes(conn)) == 1


def test_temporary_absence_is_not_a_perimeter_change(conn):
    """Une absence comblée ne doit PAS être signalée comme changement."""
    d = date(2026, 8, 1)
    _snap(conn, d, Acc(1), Acc(2, balance=Decimal("50")))
    _snap(conn, d + timedelta(days=1), Acc(1))
    _snap(conn, d + timedelta(days=2), Acc(1), Acc(2, balance=Decimal("50")))
    assert store.perimeter_changes(conn) == []


# ------------------------------------------------------------------- budgets


def test_budget_roundtrip_and_removal(conn):
    store.set_budget(conn, "Restauration", Decimal("300"))
    store.set_budget(conn, "Carburant", Decimal("120.50"))
    assert store.budgets(conn) == {"Restauration": Decimal("300"), "Carburant": Decimal("120.50")}
    store.set_budget(conn, "Restauration", Decimal("350"))  # écrasement
    assert store.budgets(conn)["Restauration"] == Decimal("350")
    store.set_budget(conn, "Carburant", None)  # retrait
    store.set_budget(conn, "Restauration", Decimal("0"))  # 0 = retrait aussi
    assert store.budgets(conn) == {}


# ------------------------------------------------------------------ benchmark


def test_benchmark_roundtrip_and_resume_point(conn):
    d = date(2026, 8, 1)
    saved = store.save_benchmark_values(
        conn, "IWDA.AS", [(d, Decimal("106.10")), (d + timedelta(days=1), Decimal("106.90"))]
    )
    assert saved == 2
    assert store.benchmark_last_day(conn, "IWDA.AS") == d + timedelta(days=1)
    history = store.benchmark_history(conn, "IWDA.AS", since=d + timedelta(days=1))
    assert history == [(d + timedelta(days=1), Decimal("106.90"))]
    # Idempotent : réécrire le même jour ne duplique pas.
    store.save_benchmark_values(conn, "IWDA.AS", [(d, Decimal("106.10"))])
    assert len(store.benchmark_history(conn, "IWDA.AS")) == 2


def test_pending_subscription_alerts_counts_unacknowledged(conn):
    netflix = Series("NETFLIX", "Netflix", Decimal("13.49"))
    _sync(conn, netflix)
    _sync(conn, netflix, Series("SPOTIFY", "Spotify", Decimal("10.99")))  # nouveau
    assert store.pending_subscription_alerts(conn) == 1
    store.acknowledge_alerts(conn)
    assert store.pending_subscription_alerts(conn) == 0


# ------------------------------------- identité stable / renumérotage Powens


class _Acc:
    """Compte avec sa payload brute, comme Account.from_api en produit une."""

    def __init__(
        self, account_id, name, *, iban=None, connection=8, balance="-100", type="loan", number=None
    ):
        self.id = account_id
        self.name = name
        self.type = type
        self.currency = "EUR"
        self.balance = Decimal(balance)
        self.raw = {"iban": iban, "id_connection": connection, "number": number}


def test_signature_prefers_iban_then_connection_and_name():
    # Deux comptes du même nom chez la même banque : seul l'IBAN les sépare.
    a = _Acc(1, "M BARTOLI JEREMIE", iban="FR7630004037438933")
    b = _Acc(2, "M BARTOLI JEREMIE", iban="FR7630004089361704")
    assert store.account_signature(a) != store.account_signature(b)
    # Sans IBAN : connexion + nom.
    assert store.account_signature(_Acc(3, "PRET IMMO", connection=8)) == "conn:8|PRET IMMO"
    # Un compte importé (id négatif) n'a pas de signature : jamais renuméroté.
    assert store.account_signature(_Acc(-1, "CCF")) is None


def test_signature_ignores_number_and_type_which_powens_regenerates():
    """Cas vécu : le prêt a porté 4 ids, 2 types et 4 « number » différents —
    seuls le nom et la connexion sont restés constants."""
    before = _Acc(20, "PRET IMMO MODULABLE CCF", type="loan", number="5bb6c9e6a861")
    after = _Acc(28, "PRET IMMO MODULABLE CCF", type="mortgage", number="937b750339d5")
    assert store.account_signature(before) == store.account_signature(after)


def test_renumbering_is_detected_and_history_is_reattached(conn):
    """Le scénario complet : le compte disparaît, revient sous un autre id, et
    l'historique doit se recoller tout seul au passage suivant."""
    loan_before = _Acc(20, "PRET IMMO MODULABLE CCF")
    store.record_snapshot(conn, [Acc(1), loan_before], day=date(2026, 8, 1))
    store.record_snapshot(conn, [Acc(1)], day=date(2026, 8, 2))  # connexion en panne

    loan_after = _Acc(28, "PRET IMMO MODULABLE CCF", balance="-99")
    remapped = store.sync_account_identities(conn, [Acc(1), loan_after])
    assert remapped == [(20, 28)]

    # L'historique du 01/08 porte désormais le nouvel id : un seul compte.
    rows = list(
        conn.execute(
            "SELECT day, account_id FROM balance_snapshot WHERE name LIKE 'PRET%' ORDER BY day"
        )
    )
    assert [(r["day"], r["account_id"]) for r in rows] == [("2026-08-01", 28)]

    # Et le trou du 02/08 est comblé : plus de faux changement de périmètre.
    store.record_snapshot(conn, [Acc(1), loan_after], day=date(2026, 8, 3))
    assert store.perimeter_changes(conn) == []


def test_remap_moves_every_table_that_references_the_account(conn):
    class _Value:
        id_investment = 7
        vdate = date(2026, 8, 1)
        unit_value = Decimal("10")

    store.save_investment_values(conn, [_Value()], account_id=20, label="ETF", code="FR0000")
    db_id = store.upsert_imported_account(conn, "Relevé CCF")
    store.link_imported_account(conn, db_id, 20)
    store.set_account_alias(conn, 20, "Mon prêt")

    store.remap_account(conn, 20, 28)

    assert (
        conn.execute("SELECT COUNT(*) n FROM investment_value WHERE account_id = 28").fetchone()[
            "n"
        ]
        == 1
    )
    assert store.imported_links(conn) == {db_id: 28}
    assert store.account_aliases(conn) == {28: "Mon prêt"}


def test_ambiguous_signature_is_never_used_to_merge(conn):
    """Deux comptes distincts au même nom sans IBAN : les fusionner serait pire
    que de les laisser séparés."""
    twin_a = _Acc(30, "COMPTE", connection=9)
    twin_b = _Acc(31, "COMPTE", connection=9)
    assert store.sync_account_identities(conn, [twin_a, twin_b]) == []
    # Rien n'est mémorisé : la signature ambiguë n'identifie personne.
    assert (
        conn.execute(
            "SELECT COUNT(*) n FROM account_identity WHERE signature = 'conn:9|COMPTE'"
        ).fetchone()["n"]
        == 0
    )


def test_per_account_series_is_memoised_until_the_table_changes(conn):
    """Deux lectures de la même table ne relisent pas les milliers de lignes ;
    un nouveau solde (ou un remplacement, nouveau rowid) invalide le mémo."""
    from datetime import date
    from decimal import Decimal

    from app.store import _per_account_series, record_snapshot

    class _Acc:
        def __init__(self, i, balance):
            self.id, self.name, self.type, self.currency = i, f"C{i}", "checking", "EUR"
            self.balance = Decimal(balance)

    record_snapshot(conn, [_Acc(1, "100"), _Acc(2, "50")], day=date(2026, 6, 1))
    first = _per_account_series(conn, "EUR")
    assert _per_account_series(conn, "EUR") is first

    record_snapshot(conn, [_Acc(1, "110"), _Acc(2, "50")], day=date(2026, 6, 2))
    second = _per_account_series(conn, "EUR")
    assert second is not first
    assert second[0] == ["2026-06-01", "2026-06-02"]

    # Remplacement d'un solde du même jour : la ligne change de rowid → relecture
    record_snapshot(conn, [_Acc(1, "999"), _Acc(2, "50")], day=date(2026, 6, 2))
    third = _per_account_series(conn, "EUR")
    assert third is not second
    assert third[1][1]["2026-06-02"] == Decimal("999")
