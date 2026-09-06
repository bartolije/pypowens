"""Tests de propriété (hypothesis) sur les parsers de relevés.

Les tests par l'exemple couvrent les formats qu'on a rencontrés ; ceux-ci
couvrent ceux qu'on n'a pas encore rencontrés. Les invariants énoncés valent
pour TOUTE entrée, y compris celles qu'une banque inventera demain — le
parseur ne doit jamais lever, jamais inventer un montant, jamais perdre le
signe.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from hypothesis import assume, given
from hypothesis import strategies as st

from app.enrich import clean_wording, merchant_key
from app.importer import fingerprint, infer_type, parse_amount, parse_date, parse_statement


class _Txn:
    """Transaction minimale pour merchant_key (Protocol structurel)."""

    def __init__(self, wording: str) -> None:
        self.id = 1
        self.id_account = 1
        self.type = "card"
        self.value = Decimal("-1")
        self.date = date(2026, 1, 1)
        self.wording = wording
        self.simplified_wording = wording
        self.original_wording = wording


# ------------------------------------------------------------- parse_amount


@given(st.text(max_size=40))
def test_parse_amount_never_raises(raw):
    """Une cellule quelconque donne un Decimal ou None — jamais une exception."""
    result = parse_amount(raw)
    assert result is None or isinstance(result, Decimal)


@given(
    st.integers(min_value=0, max_value=9_999_999),
    st.integers(min_value=0, max_value=99),
)
def test_french_format_roundtrips(units, cents):
    """« 1 234,56 » se relit exactement, séparateur de milliers compris."""
    expected = Decimal(f"{units}.{cents:02d}")
    assert parse_amount(f"{units},{cents:02d}") == expected
    # Milliers séparés par une espace, puis par une espace insécable : les deux
    # formes sortent des exports bancaires réels.
    grouped = f"{units:,}".replace(",", " ")
    assert parse_amount(f"{grouped},{cents:02d}") == expected
    assert parse_amount(f"{grouped.replace(' ', chr(160))},{cents:02d}") == expected


@given(
    st.integers(min_value=1, max_value=999),
    st.integers(min_value=0, max_value=999),
    st.integers(min_value=0, max_value=99),
)
def test_the_last_separator_is_always_the_decimal_one(thousands, units, cents):
    """Français (1.234,56) et anglo-saxon (1,234.56) donnent le même nombre.

    C'est l'invariant qui manquait : l'ancien code lisait 1,234.56 comme
    1.23456 — une corruption d'un facteur 1000, silencieuse.
    """
    expected = Decimal(f"{thousands}{units:03d}.{cents:02d}")
    assert parse_amount(f"{thousands}.{units:03d},{cents:02d}") == expected
    assert parse_amount(f"{thousands},{units:03d}.{cents:02d}") == expected


@given(st.decimals(min_value=0, max_value=10**6, places=2, allow_nan=False))
def test_sign_is_preserved_both_ways(value):
    assume(value > 0)
    text = f"{value}".replace(".", ",")
    assert parse_amount(text) == value
    assert parse_amount(f"-{text}") == -value
    assert parse_amount(f"({text})") == -value  # forme comptable


@given(st.text(max_size=20))
def test_parsed_amounts_are_always_finite(raw):
    """NaN/Infinity empoisonneraient toute somme de soldes en aval."""
    result = parse_amount(raw)
    assert result is None or result.is_finite()


# --------------------------------------------------------------- parse_date


@given(st.text(max_size=30))
def test_parse_date_never_raises(raw):
    result = parse_date(raw)
    assert result is None or isinstance(result, date)


@given(st.dates(min_value=date(1970, 1, 1), max_value=date(2099, 12, 31)))
def test_every_real_date_roundtrips_in_all_supported_forms(day):
    assert parse_date(day.strftime("%d/%m/%Y")) == day
    assert parse_date(day.strftime("%d-%m-%Y")) == day
    assert parse_date(day.isoformat()) == day


@given(st.integers(min_value=32, max_value=99), st.integers(min_value=13, max_value=99))
def test_impossible_dates_are_rejected_not_guessed(day, month):
    assert parse_date(f"{day}/{month}/2026") is None


# ------------------------------------------------------------- clean_wording


@given(st.text(max_size=60))
def test_clean_wording_never_raises_and_stays_shorter(raw):
    cleaned = clean_wording(raw)
    assert isinstance(cleaned, str)
    assert len(cleaned) <= len(raw) + 1  # jamais d'inflation du libellé


@given(st.text(max_size=60))
def test_merchant_key_is_always_usable(raw):
    """Un libellé, même vide ou illisible, donne toujours une clé non vide :
    elle sert d'identité de regroupement partout dans l'app."""
    key = merchant_key(_Txn(raw))
    assert isinstance(key, str) and key
    assert key == key.upper()


@given(st.text(max_size=60))
def test_merchant_key_is_deterministic(raw):
    assert merchant_key(_Txn(raw)) == merchant_key(_Txn(raw))


# --------------------------------------------------------------- infer_type


@given(st.text(max_size=40))
def test_infer_type_always_returns_a_known_type(raw):
    known = {
        "card",
        "order",
        "loan_repayment",
        "withdrawal",
        "transfer",
        "bank",
        "payback",
        "deposit",
        "check",
        "unknown",
    }
    assert infer_type(raw) in known


# -------------------------------------------------------------- fingerprint


@given(
    st.integers(min_value=-1000, max_value=1000),
    st.dates(min_value=date(2000, 1, 1), max_value=date(2099, 1, 1)),
    st.decimals(min_value=-(10**5), max_value=10**5, places=2, allow_nan=False),
    st.text(max_size=40),
)
def test_fingerprint_is_stable_and_specific(account, day, value, wording):
    """Même ligne = même empreinte (réimport idempotent) ; compte différent =
    empreinte différente (deux banques ne se mélangent pas)."""
    first = fingerprint(account, day, value, wording)
    assert first == fingerprint(account, day, value, wording)
    assert first != fingerprint(account + 1, day, value, wording)


# ----------------------------------------------------------- parse_statement


@given(
    st.lists(
        st.tuples(
            st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 1, 1)),
            st.text(
                alphabet=st.characters(whitelist_categories=("Lu", "Nd")),
                min_size=1,
                max_size=20,
            ),
            st.integers(min_value=1, max_value=99999),
        ),
        min_size=1,
        max_size=12,
    )
)
def test_a_wellformed_statement_never_loses_a_row(rows):
    """Toute ligne exploitable ressort : un relevé importé à moitié serait pire
    qu'un import refusé."""
    csv = '"Date operation";"Libelle";"Debit";"Credit"\n'
    for day, wording, cents in rows:
        csv += f'"{day.strftime("%d/%m/%Y")}";"{wording}";"{cents // 100},{cents % 100:02d}";""\n'
    parsed = parse_statement(csv.encode(), account_id=-1)
    assert len(parsed.transactions) == len(rows)
    assert parsed.skipped == 0
    # Empreintes toutes distinctes : deux jumelles du même jour restent deux.
    assert len(set(parsed.fingerprints)) == len(rows)
    # Un débit est toujours négatif.
    assert all(t.value < 0 for t in parsed.transactions)
