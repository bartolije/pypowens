"""Page performance et collecteur, par les routes.

Ce qui est vérifié ici : la page ne publie un rendement que quand la série le permet, et
le collecteur rattrape au lieu de supposer un passage quotidien.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app import store
from app.collector import collect
from app.config import get_settings


def _text(html: str) -> str:
    plain = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"[\s  ]+", " ", plain)


# ------------------------------------------------------------------- la page

def test_performance_page_renders_without_any_archive(client):
    """Aucune VL archivée : la page doit le dire, pas planter ni inventer un chiffre."""
    body = _text(client.get("/performance").text)
    assert "Aucune valorisation archivée" in body
    assert "app.collector" in body


def test_performance_page_lists_investment_accounts_only(client):
    body = _text(client.get("/performance").text)
    assert "PEA" in body
    # Le compte courant n'a pas de performance à montrer.
    assert "Compte courant" not in body


def test_period_buttons_are_offered_and_the_choice_is_honoured(client):
    body = client.get("/performance?periode=6m").text
    assert 'href="/performance?periode=1m"' in body
    assert "Évolution sur 6 mois" in _text(body)


def test_an_unknown_period_falls_back_instead_of_erroring(client):
    response = client.get("/performance?periode=n-importe-quoi")
    assert response.status_code == 200
    assert "Évolution sur la période" in _text(response.text)


def test_the_page_explains_twr_versus_mwr(client):
    """Un rendement sans sa définition est un nombre : la page doit porter la méthode."""
    body = _text(client.get("/performance").text)
    assert "neutralise les versements" in body
    assert "mon argent a rapporté" in body


def test_nav_exposes_the_page(client):
    assert 'href="/performance">Performance' in client.get("/").text


# -------------------------------------------------------------- le collecteur

@pytest.fixture
def conn(tmp_path):
    return store.connect(tmp_path / "store.db")


async def test_collect_archives_snapshots_and_unit_values(fake_client, conn, monkeypatch):
    monkeypatch.setenv("POWENS_DOMAIN", "test-sandbox")
    report = await collect(fake_client, conn, settings=get_settings())
    # Quatre comptes dans le jeu de test, dont deux d'investissement (livret exclu).
    assert report.snapshots == 4
    assert report.values > 0
    assert store.investment_value_span(conn) is not None


async def test_collect_resumes_from_the_last_archived_day(fake_client, conn, monkeypatch):
    """Le rattrapage : un second passage ne redemande pas tout l'historique."""
    monkeypatch.setenv("POWENS_DOMAIN", "test-sandbox")
    await collect(fake_client, conn, settings=get_settings())
    second = await collect(fake_client, conn, settings=get_settings())
    assert second.since is not None
    # La fenêtre demandée repart du dernier jour connu, moins la marge de sûreté.
    assert second.since <= date.today()


async def test_collect_is_idempotent_on_the_same_day(fake_client, conn, monkeypatch):
    monkeypatch.setenv("POWENS_DOMAIN", "test-sandbox")
    await collect(fake_client, conn, settings=get_settings())
    before = len(store.investment_values(conn))
    await collect(fake_client, conn, settings=get_settings())
    assert len(store.investment_values(conn)) == before


async def test_collect_survives_a_line_without_history(fake_client, conn, monkeypatch):
    """Les liquidités n'ont pas de VL : cela ne doit pas interrompre les autres lignes."""
    monkeypatch.setenv("POWENS_DOMAIN", "test-sandbox")

    from pypowens import PowensAPIError

    async def _no_history(investment_id: int, *args, **kwargs):
        raise PowensAPIError(404, code="notFound", message="no history")

    fake_client.list_investment_history = _no_history
    report = await collect(fake_client, conn, settings=get_settings())
    assert report.skipped == report.lines
    assert report.snapshots == 4  # les soldes, eux, sont bien enregistrés


# ------------------------------------------------------- requalification d'un flux

def test_a_flow_can_be_requalified_and_reset(conn):
    store.set_flow_override(conn, 4242, "income")
    assert store.flow_overrides(conn) == {4242: "income"}
    store.set_flow_override(conn, 4242, "external")
    assert store.flow_overrides(conn) == {4242: "external"}
    # Une nature inconnue rend la main à l'heuristique.
    store.set_flow_override(conn, 4242, "")
    assert store.flow_overrides(conn) == {}


def test_investment_values_can_be_filtered(conn):
    class Value:
        def __init__(self, day: date, unit: str) -> None:
            self.id_investment = 1
            self.vdate = day
            self.unit_value = Decimal(unit)

    today = date.today()
    store.save_investment_values(
        conn,
        [Value(today - timedelta(days=3), "10"), Value(today, "12")],
        account_id=9,
        label="ETF MONDE",
        code="FR0000000000",
    )
    assert len(store.investment_values(conn, account_id=9)) == 2
    assert store.investment_values(conn, account_id=7) == []
    recent = store.investment_values(conn, since=today)
    assert [row["unit_value"] for row in recent] == [Decimal("12")]


def test_the_two_measures_are_named_apart(client):
    """« Tout est en vert chez ma banque mais tu me dis rouge » : deux mesures, deux noms.

    Le gain depuis l'achat cumule des années de versements ; l'évolution ne couvre que la
    fenêtre choisie. Des signes opposés sont le cas courant, pas une incohérence.
    """
    body = _text(client.get("/performance").text)
    assert "Gain depuis l'achat" in body
    assert "Évolution sur" in body
    assert "ne disent pas la même chose" in body


def test_a_window_longer_than_the_archive_says_so(client):
    """Afficher 26 jours sous l'étiquette « 5 ans » serait un chiffre juste au mauvais nom."""
    import app.data

    class Value:
        def __init__(self, day: date, unit: str) -> None:
            self.id_investment = 1  # la ligne du jeu de test, portée par le compte 3
            self.vdate = day
            self.unit_value = Decimal(unit)

    today = date.today()
    store.save_investment_values(
        client.app.state.store,
        [Value(today - timedelta(days=2), "350"), Value(today, "350")],
        account_id=3,
        label="ETF MONDE",
        code="FR0000000000",
    )
    app.data.clear_cache()

    body = _text(client.get("/performance?periode=5a").text)
    assert "Fenêtre limitée" in body
    assert "l'archive ne remonte qu'au" in body
