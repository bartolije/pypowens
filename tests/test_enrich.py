"""Enrichment: wording cleanup, merchant keys, categorization, internal transfers.

These pure functions decide what every aggregate in the app groups together, so a
regression here silently rewrites every figure. They had no test at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.enrich import (
    categorize,
    clean_wording,
    internal_transfer_ids,
    load_local_rules,
    merchant_key,
    resolve_category,
)


@dataclass
class Txn:
    id: int | None = 1
    id_account: int | None = 1
    type: str | None = "card"
    value: Decimal | None = Decimal("-10")
    date: date | None = None
    wording: str | None = None
    simplified_wording: str | None = None
    original_wording: str | None = None


# ---------------------------------------------------------------- clean_wording

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ENSEIGNE\\LA-VILLE\\ FR", "ENSEIGNE"),          # card format: city dropped
        ("ENSEIGNE CB*8409", "ENSEIGNE"),                  # card format: CB suffix
        ("PRLV SEPA FOURNISSEUR", "FOURNISSEUR"),          # SEPA prefix stripped
        ("ASSUREUR CONTRAT 12345678", "ASSUREUR"),         # reference cut
        ("OPERATEUR RUM ABC-99", "OPERATEUR"),             # RUM and rest cut
        ("SOCIETE Numero de client : 4455", "SOCIETE"),    # "Numero ..." cut
    ],
)
def test_clean_wording_strips_noise(raw, expected):
    assert clean_wording(raw) == expected


def test_clean_wording_handles_empty():
    assert clean_wording("") == ""


# ----------------------------------------------------------------- merchant_key

def test_merchant_key_is_stable_across_wording_variants():
    """The same merchant billed twice with different references groups as one."""
    a = Txn(wording="FOURNISSEUR ENERGIE Numero de client : 111", simplified_wording=None)
    b = Txn(wording="FOURNISSEUR ENERGIE Numero de client : 999", simplified_wording=None)
    assert merchant_key(a) == merchant_key(b) == "FOURNISSEUR ENERGIE"


def test_merchant_key_caps_token_count():
    key = merchant_key(Txn(wording="UN DEUX TROIS QUATRE CINQ"))
    assert key == "UN DEUX TROIS"


def test_merchant_key_prefers_simplified_wording():
    txn = Txn(simplified_wording="SIMPLE", wording="AUTRE CHOSE")
    assert merchant_key(txn) == "SIMPLE"


def test_merchant_key_never_empty():
    assert merchant_key(Txn(wording="/// ---")) != ""
    assert merchant_key(Txn()) == "INCONNU"


# ------------------------------------------------------------------- categorize

def test_categorize_first_matching_rule_wins():
    assert categorize("CARREFOUR CITY") == "Alimentation"
    assert categorize("NETFLIX.COM") == "Streaming / Loisirs"
    assert categorize("QUELQUE CHOSE D INCONNU") == "Autre"


def test_categorize_is_case_insensitive():
    assert categorize("netflix.com") == categorize("NETFLIX.COM")


def test_local_rules_are_loaded_and_take_precedence(tmp_path):
    path = tmp_path / "categories.local.json"
    path.write_text(
        json.dumps({"_comment": "ignored", "Sport": ["CARREFOUR"]}), encoding="utf-8"
    )
    rules = load_local_rules(path)
    assert rules == [("Sport", ("CARREFOUR",))]
    # Local first, then the generic table: the local label wins.
    assert categorize("CARREFOUR", rules=rules) == "Sport"


def test_local_rules_missing_or_broken_file_is_ignored(tmp_path):
    assert load_local_rules(tmp_path / "nope.json") == []
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert load_local_rules(broken) == []


def test_resolve_category_uses_override_then_rules():
    assert resolve_category("NETFLIX.COM") == "Streaming / Loisirs"
    assert resolve_category("NETFLIX.COM", {"NETFLIX.COM": "Sport"}) == "Sport"
    assert resolve_category("NETFLIX.COM", {"AUTRE": "Sport"}) == "Streaming / Loisirs"


# -------------------------------------------------------- internal transfers

def _pair(day: date, amount: str, gap_days: int = 0) -> list[Txn]:
    return [
        Txn(id=1, id_account=1, type="transfer", value=Decimal(f"-{amount}"), date=day,
            wording="Virement"),
        Txn(id=2, id_account=2, type="transfer", value=Decimal(amount),
            date=day + timedelta(days=gap_days), wording="Virement"),
    ]


def test_mirror_transfer_pair_is_detected():
    day = date(2026, 7, 1)
    assert internal_transfer_ids(_pair(day, "500")) == {1, 2}


def test_mirror_detection_tolerates_a_few_days():
    day = date(2026, 7, 1)
    assert internal_transfer_ids(_pair(day, "500", gap_days=3)) == {1, 2}


def test_mirror_detection_rejects_a_wide_gap():
    day = date(2026, 7, 1)
    assert internal_transfer_ids(_pair(day, "500", gap_days=10)) == set()


def test_same_account_is_not_a_mirror():
    day = date(2026, 7, 1)
    txns = [
        Txn(id=1, id_account=1, type="transfer", value=Decimal("-500"), date=day, wording="V"),
        Txn(id=2, id_account=1, type="transfer", value=Decimal("500"), date=day, wording="V"),
    ]
    assert internal_transfer_ids(txns) == set()


def test_wording_heuristic_catches_lone_savings_move():
    """The mirror leg may sit outside the window; the wording still gives it away."""
    txns = [
        Txn(id=9, id_account=1, type="transfer", value=Decimal("-300"),
            date=date(2026, 7, 1), wording="EPGN - Livret"),
    ]
    assert internal_transfer_ids(txns) == {9}


def test_card_payments_are_never_internal():
    txns = [
        Txn(id=1, id_account=1, type="card", value=Decimal("-500"), date=date(2026, 7, 1),
            wording="ENSEIGNE"),
        Txn(id=2, id_account=2, type="card", value=Decimal("500"), date=date(2026, 7, 1),
            wording="ENSEIGNE"),
    ]
    assert internal_transfer_ids(txns) == set()
