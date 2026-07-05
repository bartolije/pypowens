"""Unit tests for the recurring-payments detector (synthetic data, no network)."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

# Make the repo-local `app` package importable under a bare `pytest` invocation
# (it is not installed like `pypowens`).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.recurring import RecurringItem, detect_recurring  # noqa: E402
from pypowens.models import Transaction  # noqa: E402

TODAY = date(2026, 7, 1)


def tx(id, account, d, value, wording, type="card"):
    return Transaction.from_api(
        {
            "id": id,
            "id_account": account,
            "date": d,
            "value": value,
            "simplified_wording": wording,
            "wording": wording,
            "type": type,
        }
    )


def series(*, start_id, account, wording, value, count, step_days, end, type="card"):
    """Build a dated series of `count` transactions ending on `end` (ISO str)."""
    end_date = date.fromisoformat(end)
    out = []
    for i in range(count):
        d = end_date - timedelta(days=step_days * (count - 1 - i))
        out.append(tx(start_id + i, account, d.isoformat(), value, wording, type))
    return out


def _by_key(items: list[RecurringItem]) -> dict[str, RecurringItem]:
    return {it.key: it for it in items}


# --------------------------------------------------------------------- cases


def test_netflix_monthly():
    txns = series(
        start_id=100, account=1, wording="NETFLIX.COM", value=-13.49,
        count=12, step_days=30, end="2026-06-20",
    )
    items = detect_recurring(txns, today=TODAY)
    assert len(items) == 1
    it = items[0]
    assert it.periodicity == "Mensuel"
    assert it.period_months == 1.0
    assert it.category == "Streaming / Loisirs"
    assert it.amount == Decimal("13.49")
    assert it.monthly_equiv == Decimal("13.49")
    assert it.occurrences == 12
    assert it.variable is False
    assert it.confidence >= 0.66
    assert it.next_date == it.last_date + timedelta(days=30)
    assert it.account_ids == [1]


def test_insurance_quarterly():
    txns = series(
        start_id=200, account=1, wording="PRLV SEPA AXA ASSURANCE", value=-180,
        count=8, step_days=91, end="2026-06-10", type="bank",
    )
    items = detect_recurring(txns, today=TODAY)
    assert len(items) == 1
    it = items[0]
    assert it.periodicity == "Trimestriel"
    assert it.period_months == 3.0
    assert it.category == "Assurance / Mutuelle"
    assert it.monthly_equiv == Decimal("60.00")
    assert it.occurrences == 8


def test_yearly_subscription_three_occurrences():
    txns = series(
        start_id=300, account=1, wording="PRLV OVHCLOUD", value=-69.90,
        count=3, step_days=365, end="2026-06-01", type="bank",
    )
    items = detect_recurring(txns, today=TODAY)
    assert len(items) == 1
    it = items[0]
    assert it.periodicity == "Annuel"
    assert it.period_months == 12.0
    assert float(it.monthly_equiv) == pytest.approx(5.83, abs=0.01)


def test_yearly_accepted_with_two_occurrences():
    """The min_occurrences=2 rule for long cadences (365d apart)."""
    txns = series(
        start_id=350, account=1, wording="PRLV AMAZON PRIME", value=-49.90,
        count=2, step_days=365, end="2026-05-15", type="bank",
    )
    items = detect_recurring(txns, today=TODAY)
    assert len(items) == 1
    assert items[0].periodicity == "Annuel"
    assert items[0].occurrences == 2


def test_one_off_purchase_not_detected():
    txns = [tx(400, 1, "2026-06-15", -59.99, "FNAC ACHAT")]
    assert detect_recurring(txns, today=TODAY) == []


def test_two_monthly_occurrences_not_detected():
    """2 occurrences at a short cadence must not qualify (needs >= 3)."""
    txns = series(
        start_id=410, account=1, wording="BOUTIQUE XYZ", value=-20,
        count=2, step_days=30, end="2026-06-20",
    )
    assert detect_recurring(txns, today=TODAY) == []


def test_variable_amount_flagged():
    values = [-10, -40, -12, -45]
    txns = [
        tx(500 + i, 1, (date(2026, 6, 20) - timedelta(days=30 * (3 - i))).isoformat(),
           v, "PRLV POWERCO", type="bank")
        for i, v in enumerate(values)
    ]
    items = detect_recurring(txns, today=TODAY)
    assert len(items) == 1
    it = items[0]
    assert it.periodicity == "Mensuel"
    assert it.variable is True
    assert it.occurrences == 4


def test_dual_pricing_same_biller_split():
    """A biller with two distinct amounts yields two separate series."""
    txns = (
        series(start_id=600, account=1, wording="APPLE.COM BILL", value=-2.99,
               count=6, step_days=30, end="2026-06-18")
        + series(start_id=700, account=1, wording="APPLE.COM BILL", value=-9.99,
                 count=6, step_days=30, end="2026-06-19")
    )
    items = detect_recurring(txns, today=TODAY)
    assert len(items) == 2
    amounts = sorted(it.amount for it in items)
    assert amounts == [Decimal("2.99"), Decimal("9.99")]
    assert all(it.periodicity == "Mensuel" for it in items)


def test_internal_transfer_excluded():
    transfers = series(
        start_id=800, account=1, wording="VIREMENT LIVRET A", value=-500,
        count=4, step_days=30, end="2026-06-20", type="transfer",
    )
    internal = {t.id for t in transfers}

    # Without exclusion it would be detected as a monthly series...
    detected = detect_recurring(transfers, today=TODAY)
    assert len(detected) == 1
    # ...but excluding its ids removes it entirely.
    assert detect_recurring(transfers, today=TODAY, internal_ids=internal) == []


def test_dead_subscription_dropped():
    """Series whose last payment is far in the past is not 'active'."""
    txns = series(
        start_id=900, account=1, wording="OLD GYM", value=-29.99,
        count=6, step_days=30, end="2025-06-01",  # >2 intervals before TODAY
    )
    assert detect_recurring(txns, today=TODAY) == []


def test_credit_kind_detects_income():
    txns = series(
        start_id=950, account=2, wording="VIR SALAIRE ACME", value=2500,
        count=6, step_days=30, end="2026-06-28", type="transfer",
    )
    debits = detect_recurring(txns, today=TODAY, kind="debit")
    assert debits == []
    credits = detect_recurring(txns, today=TODAY, kind="credit")
    assert len(credits) == 1
    assert credits[0].periodicity == "Mensuel"
    assert credits[0].amount == Decimal("2500.00")


def test_sorted_by_period_then_monthly_equiv():
    txns = (
        series(start_id=1000, account=1, wording="NETFLIX.COM", value=-13.49,
               count=12, step_days=30, end="2026-06-20")
        + series(start_id=1100, account=1, wording="PRLV SEPA AXA ASSURANCE", value=-180,
                 count=8, step_days=91, end="2026-06-10", type="bank")
        + series(start_id=1200, account=1, wording="SPOTIFY", value=-9.99,
                 count=10, step_days=30, end="2026-06-22")
    )
    items = detect_recurring(txns, today=TODAY)
    periods = [it.period_months for it in items]
    assert periods == sorted(periods)
    # Within the monthly group, higher €/mois comes first.
    monthly = [it for it in items if it.period_months == 1.0]
    assert monthly[0].monthly_equiv >= monthly[1].monthly_equiv
