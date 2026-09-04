"""Le collecteur : archivage de l'indice, notifications, résilience du passage.

C'est le seul composant qui tourne SANS personne devant l'écran : ses pannes
sont muettes par nature, et un jour de solde non collecté est perdu pour
toujours. D'où des tests sur ses chemins d'échec autant que sur son succès.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app import collector, store
from app.config import Settings


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "collector.db")
    yield connection
    connection.close()


def _settings(**overrides) -> Settings:
    base = dict(
        domain="test-sandbox",
        client_id="cid",
        client_secret="secret",
        access_token="tok",
    )
    base.update(overrides)
    return Settings(**base)


# ------------------------------------------------------------------ benchmark


def test_benchmark_is_skipped_without_a_ticker(conn):
    assert collector._collect_benchmark(conn, _settings(benchmark_ticker="")) == 0


def test_benchmark_resumes_from_the_last_archived_day(conn, monkeypatch):
    """Le rattrapage ne redemande que ce qui manque (moins le recouvrement)."""
    store.save_benchmark_values(conn, "IWDA.AS", [(date(2026, 6, 1), Decimal("100"))])
    captured = {}

    class _FakeTicker:
        def __init__(self, symbol):
            captured["symbol"] = symbol

        def history(self, **kwargs):
            captured.update(kwargs)
            return {"Close": {}}

    monkeypatch.setitem(
        __import__("sys").modules, "yfinance", type("m", (), {"Ticker": _FakeTicker})
    )
    collector._collect_benchmark(conn, _settings())
    assert captured["symbol"] == "IWDA.AS"
    # Dernier jour archivé (01/06) moins OVERLAP_DAYS.
    expected = date(2026, 6, 1) - timedelta(days=collector.OVERLAP_DAYS)
    assert captured["start"] == expected.isoformat()


def test_benchmark_failure_never_breaks_the_run(conn, monkeypatch):
    class _Exploding:
        def __init__(self, symbol):
            pass

        def history(self, **kwargs):
            raise RuntimeError("yahoo down")

    monkeypatch.setitem(
        __import__("sys").modules, "yfinance", type("m", (), {"Ticker": _Exploding})
    )
    assert collector._collect_benchmark(conn, _settings()) == 0  # pas d'exception


def test_benchmark_without_yfinance_is_a_noop(conn, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _no_yfinance(name, *args, **kwargs):
        if name == "yfinance":
            raise ImportError("absent")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_yfinance)
    assert collector._collect_benchmark(conn, _settings()) == 0


# -------------------------------------------------------------------- collect


async def test_collect_reports_what_it_archived(fake_client, conn):
    report = await collector.collect(fake_client, conn, settings=_settings())
    assert report.accounts == 4  # le jeu de test
    assert report.snapshots == 4
    assert "solde(s) enregistré(s)" in str(report)


async def test_collect_survives_a_line_without_history(fake_client, conn, monkeypatch):
    """Les liquidités n'ont pas de VL : la ligne est comptée « sans historique »,
    la collecte des autres continue."""
    from pypowens import PowensAPIError

    async def _no_history(*args, **kwargs):
        raise PowensAPIError(404, code="notFound")

    monkeypatch.setattr(fake_client, "list_investment_history", _no_history)
    report = await collector.collect(fake_client, conn, settings=_settings())
    assert report.skipped >= 1
    assert report.snapshots == 4  # les soldes sont passés malgré tout


async def test_resume_point_uses_the_archived_span(fake_client, conn):
    assert collector._resume_from(conn) is None  # base vide : tout est à prendre
    await collector.collect(fake_client, conn, settings=_settings())
    resume = collector._resume_from(conn)
    assert resume is not None
    span = store.investment_value_span(conn)
    assert resume == span[1] - timedelta(days=collector.OVERLAP_DAYS)


async def test_investment_histories_are_fetched_in_parallel(fake_client, conn, monkeypatch):
    """Vingt lignes = vingt appels : enchaînés, ils coûtaient six secondes."""
    import asyncio

    from app import collector
    from pypowens import Investment, InvestmentValue

    async def six_lines(*args, **kwargs):
        return [
            Investment.from_api({"id": i, "id_account": 3, "label": f"L{i}", "code": f"FR{i:010d}"})
            for i in range(1, 7)
        ]

    in_flight = {"now": 0, "max": 0}

    async def history(investment_id, *args, **kwargs):
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        await asyncio.sleep(0)  # rend la main : les appels parallèles démarrent ici
        in_flight["now"] -= 1
        return [
            InvestmentValue.from_api(
                {
                    "id": investment_id,
                    "id_investment": investment_id,
                    "vdate": "2026-06-14",
                    "unitvalue": "10.00",
                }
            )
        ]

    monkeypatch.setattr(fake_client, "list_investments", six_lines)
    monkeypatch.setattr(fake_client, "list_investment_history", history)

    report = await collector.collect(fake_client, conn, settings=_settings())

    assert report.lines == 6 and report.values == 6
    assert in_flight["max"] == min(6, collector.HISTORY_CONCURRENCY)


async def test_run_once_reactivates_pinned_accounts_before_the_snapshot(
    fake_client, conn, monkeypatch
):
    """Un compte épinglé désactivé dans la nuit doit revenir AVANT le relevé du
    jour, sinon son solde manque à la courbe jusqu'à la prochaine visite."""
    import app.data
    from app import collector, health, store

    monkeypatch.setenv("APP_NOTIFY", "0")
    monkeypatch.setattr(collector, "_collect_benchmark", lambda *args, **kwargs: 0)
    health.reset_reactivation_throttle()
    app.data.clear_cache()
    store.pin_account(conn, "conn:1|PRET IMMO MODULABLE", "PRET IMMO MODULABLE")
    fake_client._disabled_accounts.append(
        {
            "id": 41,
            "id_connection": 1,
            "name": "PRET IMMO MODULABLE",
            "type": "loan",
            "balance": "-255954.00",
            "currency": {"id": "EUR"},
            "disabled": "2026-06-14 02:00:00",
        }
    )

    await collector.run_once(fake_client, conn, _settings())

    assert "disabled" not in fake_client._disabled_accounts[0]
    fake_client._disabled_accounts.clear()
    health.reset_reactivation_throttle()
    app.data.clear_cache()
