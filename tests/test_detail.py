"""Page détail d'un compte (``/patrimoine/{id}``) : onglets, 404, positions.

226 lignes accessibles depuis chaque ligne de tableau de l'app — et aucune
requête de test ne les traversait avant l'audit.
"""

from __future__ import annotations

import re

_re_spaces = re.compile("[\\s\u00a0\u202f]+")


def _text(html: str) -> str:
    plain = re.sub(r"<[^>]+>", " ", html)
    #   /   : espaces (fines) insécables du format monétaire français.
    return _re_spaces.sub(" ", plain)


def test_unknown_account_renders_a_404_page(client):
    response = client.get("/patrimoine/99999")
    assert response.status_code == 404
    assert "Compte introuvable" in response.text


def test_apercu_tab_shows_balance_and_periods(client):
    response = client.get("/patrimoine/1")
    assert response.status_code == 200
    body = _text(response.text)
    assert "Compte courant" in body
    assert "2 500 €" in body  # solde héro, arrondi à l'euro
    for label in ("1J", "1M", "YTD", "TOUT"):
        assert label in body


def test_positions_tab_lists_holdings_for_an_investment_account(client):
    response = client.get("/patrimoine/3", params={"tab": "positions"})
    assert response.status_code == 200
    body = _text(response.text)
    assert "ETF MONDE" in body
    # diff_percent API = 0.0294 (fraction) → affiché +2,94 %, jamais +0,03 %.
    assert "+2.94" in body or "+2,94" in body


def test_transactions_tab_groups_by_day(client):
    response = client.get("/patrimoine/1", params={"tab": "transactions"})
    assert response.status_code == 200
    body = _text(response.text)
    assert "NETFLIX.COM" in body
    assert "opération" in body  # compteur affiché


def test_analyse_tab_shows_balance_stats(client):
    # Deux passages : le premier écrit le snapshot du jour, le second lit l'historique.
    client.get("/patrimoine/1")
    response = client.get("/patrimoine/1", params={"tab": "analyse"})
    assert response.status_code == 200
    body = _text(response.text)
    assert "Solde moyen" in body
    assert "Solde min" in body
    assert "Solde max" in body


def test_non_invest_account_has_no_positions_tab(client):
    response = client.get("/patrimoine/1")
    assert "tab=positions" not in response.text
    response = client.get("/patrimoine/3")
    assert "tab=positions" in response.text
