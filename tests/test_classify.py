"""Classification des titres (OpenFIGI + Yahoo) : fonctions pures et cache SQLite.

Module à zéro test avant l'audit, alors que ses régressions sont doublement
invisibles : la page les avale (try/except) et rien ne les exerçait.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import respx
import time_machine

from app import store
from app.classify import (
    _load_cache,
    _save_cache,
    _yahoo_ticker,
    classify_investments,
    country_from_isin,
)
from tests.conftest import FROZEN_TODAY


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "t.db")
    yield connection
    connection.close()


# ------------------------------------------------------------- fonctions pures

@pytest.mark.parametrize(
    ("isin", "expected"),
    [
        ("FR0000120073", "France"),
        ("US0378331005", "États-Unis"),
        ("IE00B4L5Y983", "Irlande"),
        ("ZZ0000000000", "ZZ"),  # préfixe inconnu : rendu tel quel, jamais d'erreur
    ],
)
def test_country_from_isin(isin, expected):
    assert country_from_isin(isin) == expected


@pytest.mark.parametrize(
    ("ticker", "exch", "expected"),
    [
        ("AI", "PA", "AI.PA"),        # Euronext Paris
        ("ASML", "AS", "ASML.AS"),    # Amsterdam
        ("AAPL", "US", "AAPL"),       # place US : pas de suffixe
        ("XYZ", "??", "XYZ"),         # place inconnue : ticker inchangé
    ],
)
def test_yahoo_ticker_suffixes(ticker, exch, expected):
    assert _yahoo_ticker(ticker, exch) == expected


# -------------------------------------------------------------------- cache

def test_cache_roundtrip_and_30_day_expiry(conn):
    _save_cache(conn, "FR0000120073", {"sector": "Industrials", "country": "France"})
    assert "FR0000120073" in _load_cache(conn, ["FR0000120073"])

    # 29 jours plus tard : encore frais. 31 jours : expiré.
    with time_machine.travel(FROZEN_TODAY + timedelta(days=29), tick=False):
        assert "FR0000120073" in _load_cache(conn, ["FR0000120073"])
    with time_machine.travel(FROZEN_TODAY + timedelta(days=31), tick=False):
        assert _load_cache(conn, ["FR0000120073"]) == {}


async def test_classify_returns_cache_without_api_key(conn):
    _save_cache(conn, "FR0000120073", {"sector": "Industrials", "country": "France"})
    result = await classify_investments(["FR0000120073", "US0378331005"], conn, None)
    assert set(result) == {"FR0000120073"}  # pas de clé → pas d'appel réseau


# ------------------------------------------------------------------ OpenFIGI

@respx.mock
async def test_openfigi_shorter_response_does_not_crash(conn):
    """OpenFIGI peut renvoyer moins de résultats que d'ISINs demandés."""
    respx.post("https://api.openfigi.com/v3/mapping").mock(
        return_value=httpx.Response(200, json=[{"data": []}])  # 1 réponse pour 2 ISINs
    )
    result = await classify_investments(
        ["FR0000120073", "US0378331005"], conn, "test-key"
    )
    # Dégradé mais jamais cassé : fallback pays par préfixe ISIN.
    assert result["FR0000120073"]["country"] == "France"
    assert result["US0378331005"]["country"] == "États-Unis"


# ------------------------------------------------------------- traduction FR

def test_sectors_and_countries_come_out_in_french(conn):
    from app.classify import translate_classification

    entry = {"sector": "Technology", "country": "United States", "name": "X"}
    translated = translate_classification(entry)
    assert translated["sector"] == "Technologie"
    assert translated["country"] == "États-Unis"
    # Inconnu ou déjà traduit : identité, jamais d'erreur.
    already_fr = translate_classification({"sector": "Technologie", "country": None})
    assert already_fr["sector"] == "Technologie"


async def test_cached_english_entries_are_translated_on_read(conn):
    """Le cache 30 jours contient de l'anglais (entrées d'avant la traduction) :
    la traduction s'applique à la lecture, sans migration."""
    _save_cache(conn, "US0378331005", {"sector": "Technology", "country": "United States"})
    result = await classify_investments(["US0378331005"], conn, None)
    assert result["US0378331005"]["sector"] == "Technologie"
    assert result["US0378331005"]["country"] == "États-Unis"
