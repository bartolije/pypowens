"""The strict pass that separates real contracts from repeating spending.

``detect_recurring`` is intentionally permissive — the analysis page needs every
repeating pattern to split recurring from one-off. A subscriptions list needs the
opposite: on real statements the permissive pass returned 82 "subscriptions", most
of them supermarket runs that happened to cluster by amount. These tests pin the
signals that tell the two apart.
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.recurring import (  # noqa: E402
    detect_recurring,
    detect_subscriptions,
    is_subscription,
)
from tests.test_recurring import series, tx  # noqa: E402

TODAY = date(2026, 7, 1)


def _only(txns, **kwargs):
    items = detect_subscriptions(txns, today=TODAY, **kwargs)
    return {it.key: it for it in items}


# ------------------------------------------------------------------- kept


def test_identical_card_charges_are_a_subscription():
    txns = series(
        start_id=100,
        account=1,
        wording="DEEZER",
        value=-11.99,
        count=12,
        step_days=30,
        end="2026-06-20",
    )
    assert "DEEZER" in _only(txns)


def test_sepa_mandate_is_a_contract_even_when_the_amount_moves():
    """A prélèvement is signed evidence of a contract; insurers reprice yearly."""
    txns = [
        tx(200 + i, 1, d, v, "MON ASSUREUR", type="order")
        for i, (d, v) in enumerate(
            [
                ("2026-01-05", -94.86),
                ("2026-02-05", -94.86),
                ("2026-03-05", -94.86),
                ("2026-04-05", -108.31),
                ("2026-05-05", -108.31),
                ("2026-06-05", -108.31),
            ]
        )
    ]
    assert "MON ASSUREUR" in _only(txns)


def test_sepa_stays_a_contract_when_two_of_them_interleave():
    """A flat and a garage at the same utility bill on the same day each month.

    Amount clustering cannot split them, so the cadence looks irregular — but a
    biller debiting a mandate sixteen times is a contract regardless.
    """
    txns = []
    for i, month in enumerate(range(1, 7)):
        txns.append(tx(300 + i * 2, 1, f"2026-{month:02d}-12", -19.00, "MON ENERGIE", type="order"))
        txns.append(tx(301 + i * 2, 1, f"2026-{month:02d}-12", -27.19, "MON ENERGIE", type="order"))
    assert "MON ENERGIE" in _only(txns)


# ------------------------------------------------------------------ rejected


def test_varying_card_amounts_are_not_a_subscription():
    """Groceries: same merchant, regular cadence, amount all over the place."""
    txns = [
        tx(400 + i, 1, d, v, "SUPERMARCHE", type="card")
        for i, (d, v) in enumerate(
            [
                ("2026-01-10", -74.90),
                ("2026-02-10", -12.40),
                ("2026-03-10", -121.74),
                ("2026-04-10", -31.04),
                ("2026-05-10", -94.06),
                ("2026-06-10", -20.77),
            ]
        )
    ]
    assert _only(txns) == {}


def test_everyday_categories_are_never_subscriptions():
    """Two identical restaurant bills a year apart are not an annual renewal."""
    txns = series(
        start_id=500,
        account=1,
        wording="RESTAURANT DU COIN",
        value=-41.96,
        count=2,
        step_days=365,
        end="2026-06-20",
    )
    detected = detect_recurring(txns, today=TODAY)
    assert detected and detected[0].periodicity == "Annuel"
    assert _only(txns) == {}


def test_two_card_charges_must_land_on_the_anniversary():
    """Same amount twice, but 40 days off the anniversary: coincidence, not renewal."""
    loose = series(
        start_id=600,
        account=1,
        wording="BOUTIQUE ALPHA",
        value=-30.20,
        count=2,
        step_days=330,
        end="2026-06-20",
    )
    assert _only(loose) == {}

    tight = series(
        start_id=700,
        account=1,
        wording="BOUTIQUE BETA",
        value=-30.20,
        count=2,
        step_days=365,
        end="2026-06-20",
    )
    assert "BOUTIQUE BETA" in _only(tight)


def test_permissive_pass_still_sees_what_the_strict_pass_drops():
    """The analysis split depends on it, so the loose detector must stay loose."""
    txns = [
        tx(800 + i, 1, d, v, "SUPERMARCHE", type="card")
        for i, (d, v) in enumerate(
            [
                ("2026-01-10", -74.90),
                ("2026-02-10", -12.40),
                ("2026-03-10", -121.74),
                ("2026-04-10", -31.04),
                ("2026-05-10", -94.06),
                ("2026-06-10", -20.77),
            ]
        )
    ]
    assert detect_recurring(txns, today=TODAY) != []


# --------------------------------------------------------------- traceability


def test_history_and_drift_expose_the_price_trend():
    txns = [
        tx(900 + i, 1, d, v, "MON FOURNISSEUR", type="order")
        for i, (d, v) in enumerate(
            [("2026-04-05", -100.00), ("2026-05-05", -100.00), ("2026-06-05", -110.00)]
        )
    ]
    item = _only(txns)["MON FOURNISSEUR"]
    assert [amount for _, amount in item.history] == [
        Decimal("100.00"),
        Decimal("100.00"),
        Decimal("110.00"),
    ]
    assert item.first_amount == Decimal("100.00")
    assert item.drift_pct == 10.0


def test_drift_is_none_without_a_comparison_point():
    txns = series(
        start_id=950,
        account=1,
        wording="MON TRUC",
        value=-10.00,
        count=2,
        step_days=30,
        end="2026-06-20",
    )
    item = detect_recurring(txns, today=TODAY, min_occurrences=2)[0]
    item.history = item.history[:1]
    assert item.drift_pct is None


def test_an_overdue_series_is_flagged_stale():
    """``stale`` is the warning band before a series is dropped as dead.

    Overdue past 1.5 periods it is flagged and stops counting towards the monthly
    total; past 2 periods :func:`detect_recurring` drops it altogether. For a
    monthly series that band is days 46-60, which is what this pins.
    """
    overdue = series(
        start_id=1000,
        account=1,
        wording="ANCIEN ABO",
        value=-9.99,
        count=4,
        step_days=30,
        end="2026-05-10",
    )
    live = series(
        start_id=1100,
        account=1,
        wording="ABO EN COURS",
        value=-9.99,
        count=4,
        step_days=30,
        end="2026-06-20",
    )
    items = {it.key: it for it in detect_recurring(overdue + live, today=TODAY)}
    assert items["ANCIEN ABO"].stale is True
    assert items["ANCIEN ABO"].days_since_last == 52
    assert items["ABO EN COURS"].stale is False


def test_a_long_dead_series_is_dropped_entirely():
    dead = series(
        start_id=1500,
        account=1,
        wording="ABO MORT",
        value=-9.99,
        count=4,
        step_days=30,
        end="2026-04-20",
    )
    assert detect_recurring(dead, today=TODAY) == []


def test_rail_names_the_payment_channel():
    card = series(
        start_id=1200,
        account=1,
        wording="PAR CARTE",
        value=-5.00,
        count=4,
        step_days=30,
        end="2026-06-20",
    )
    sepa = series(
        start_id=1300,
        account=1,
        wording="PAR PRLV",
        value=-5.00,
        count=4,
        step_days=30,
        end="2026-06-20",
        type="order",
    )
    items = {it.key: it for it in detect_recurring(card + sepa, today=TODAY)}
    assert items["PAR CARTE"].rail == "carte"
    assert items["PAR PRLV"].rail == "SEPA"


def test_is_subscription_is_usable_on_its_own():
    item = detect_recurring(
        series(
            start_id=1400,
            account=1,
            wording="MON ABO",
            value=-9.99,
            count=6,
            step_days=30,
            end="2026-06-20",
        ),
        today=TODAY,
    )[0]
    assert is_subscription(item) is True
    item.category = "Alimentation"
    assert is_subscription(item) is False


def test_last_amount_is_the_current_price_not_the_median():
    """A raised premium must not be reported at its median across three years."""
    txns = [
        tx(1600 + i, 1, d, v, "MON ASSUREUR AUTO", type="order")
        for i, (d, v) in enumerate(
            [("2024-07-05", -1139.10), ("2025-07-07", -1190.39), ("2026-07-06", -1214.18)]
        )
    ]
    item = _only(txns)["MON ASSUREUR AUTO"]
    assert item.amount == Decimal("1190.39")  # median: robust
    assert item.last_amount == Decimal("1214.18")  # what is paid today
    assert item.first_amount == Decimal("1139.10")


def test_a_shop_visited_often_is_not_a_subscription():
    """Three similar purchases at a shop also visited a dozen other times.

    Identical amounts and a clean cadence are not enough here: what separates a
    subscription merchant is that it charges *only* the subscription.
    """
    series_txns = [
        tx(1700 + i, 1, d, -370.00, "EQUIPEMENT MOTO", type="card")
        for i, d in enumerate(["2025-08-28", "2026-01-15", "2026-05-15"])
    ]
    noise = [
        tx(1800 + i, 1, d, v, "EQUIPEMENT MOTO", type="card")
        for i, (d, v) in enumerate(
            [
                ("2025-09-26", -71.40),
                ("2025-10-12", -114.90),
                ("2025-11-08", -13.95),
                ("2026-02-03", -89.95),
                ("2026-03-19", -91.30),
                ("2026-04-22", -45.10),
                ("2026-06-18", -22.80),
            ]
        )
    ]
    assert "EQUIPEMENT MOTO" in _only(series_txns)
    assert _only(series_txns + noise) == {}


def test_share_is_not_applied_to_sepa_mandates():
    """An insurer may also debit adjustments; the mandate still proves the contract."""
    regular = [
        tx(1900 + i, 1, f"2026-{m:02d}-05", -100.00, "MON ASSUREUR SEPA", type="order")
        for i, m in enumerate(range(1, 7))
    ]
    adjustments = [
        tx(1950 + i, 1, d, v, "MON ASSUREUR SEPA", type="order")
        for i, (d, v) in enumerate(
            [
                ("2026-01-20", -8.22),
                ("2026-02-20", -14.50),
                ("2026-03-20", -31.00),
                ("2026-04-20", -7.10),
                ("2026-05-20", -22.40),
                ("2026-06-20", -19.90),
            ]
        )
    ]
    assert "MON ASSUREUR SEPA" in _only(regular + adjustments)


def test_a_repriced_contract_is_not_reported_as_cancelled():
    """Amount clustering splits a big repricing off into a series of one.

    The surviving half then looks dormant, so a yearly tax that went from 1 435 to
    2 115 € would read as "no longer debited" — while it is the largest charge of
    the three. The merchant's real last charge is carried alongside.
    """
    txns = [
        tx(2000 + i, 1, d, v, "MON TRESOR PUBLIC", type="order")
        for i, (d, v) in enumerate(
            [("2023-10-26", -1367.00), ("2024-10-25", -1435.00), ("2025-10-03", -2115.00)]
        )
    ]
    item = _only(txns)["MON TRESOR PUBLIC"]
    assert item.stale is True  # the 1 367/1 435 cluster stopped in 2024...
    assert item.repriced is True  # ...but the mandate was debited again in 2025
    assert item.merchant_last == (date(2025, 10, 3), Decimal("2115.00"))


def test_a_genuinely_dead_series_is_not_called_repriced():
    txns = [
        tx(2100 + i, 1, d, -9.99, "ABO FINI", type="order")
        for i, d in enumerate(["2025-11-05", "2025-12-05", "2026-01-05", "2026-02-05"])
    ]
    # 48 days after the last charge: inside the stale band, before the drop threshold.
    items = detect_recurring(txns, today=date(2026, 3, 25))
    assert items and items[0].stale is True
    assert items[0].repriced is False  # no merchant_last set, nothing charged since


def test_acquitter_route_clears_the_alert_banner(client):
    """POST /abonnements/acquitter : les alertes persistent jusqu'à ce clic."""
    response = client.post("/abonnements/acquitter", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/abonnements"
