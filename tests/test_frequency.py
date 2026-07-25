"""Label grouping (the /recurrences view) — untested until now."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.frequency import group_by_label


@dataclass
class Txn:
    id: int
    value: Decimal
    date: date
    id_account: int = 1
    type: str = "card"
    wording: str = "ENSEIGNE"
    simplified_wording: str | None = None
    original_wording: str | None = None


def _txns() -> list[Txn]:
    return [
        Txn(1, Decimal("-10.00"), date(2026, 1, 5), wording="ENSEIGNE"),
        Txn(2, Decimal("-20.00"), date(2026, 2, 5), wording="ENSEIGNE"),
        Txn(3, Decimal("-30.00"), date(2026, 3, 5), wording="ENSEIGNE"),
        Txn(4, Decimal("-99.00"), date(2026, 2, 8), wording="UNE SEULE FOIS"),
        Txn(5, Decimal("2500.00"), date(2026, 2, 27), wording="SALAIRE", type="transfer"),
        Txn(6, Decimal("2500.00"), date(2026, 3, 27), wording="SALAIRE", type="transfer"),
    ]


def test_groups_debits_and_computes_stats():
    groups = group_by_label(_txns())
    assert len(groups) == 1  # only ENSEIGNE reaches min_count=2
    group = groups[0]
    assert group.key == "ENSEIGNE"
    assert group.count == 3
    assert group.total == Decimal("60.00")
    assert group.avg == Decimal("20.00")
    assert group.min_amount == Decimal("10.00")
    assert group.max_amount == Decimal("30.00")
    assert group.first_date == date(2026, 1, 5)
    assert group.last_date == date(2026, 3, 5)


def test_min_count_can_include_singletons():
    labels = {g.key for g in group_by_label(_txns(), min_count=1)}
    assert "UNE SEULE FOIS" in labels


def test_credit_kind_returns_income_only():
    groups = group_by_label(_txns(), kind="credit")
    assert [g.key for g in groups] == ["SALAIRE"]
    assert groups[0].total == Decimal("5000.00")


def test_date_range_is_applied():
    groups = group_by_label(
        _txns(), date_from=date(2026, 2, 1), date_to=date(2026, 2, 28), min_count=1
    )
    keys = {g.key for g in groups}
    assert keys == {"ENSEIGNE", "UNE SEULE FOIS"}
    ens = next(g for g in groups if g.key == "ENSEIGNE")
    assert ens.count == 1


def test_internal_ids_are_excluded():
    assert group_by_label(_txns(), internal_ids={1, 2, 3}) == []


def test_overrides_are_applied_to_the_category():
    groups = group_by_label(_txns(), overrides={"ENSEIGNE": "Sport"})
    assert groups[0].category == "Sport"


def test_average_interval_and_span():
    group = group_by_label(_txns())[0]
    assert group.span_days == 59
    assert group.avg_interval_days == 30  # 59 days over 2 gaps


def test_single_occurrence_has_no_interval():
    group = group_by_label(_txns(), min_count=1)
    single = next(g for g in group if g.key == "UNE SEULE FOIS")
    assert single.avg_interval_days is None
