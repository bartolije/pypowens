"""Drill-down page, category override form, connection sync, Webview callback."""

from __future__ import annotations

import re


def _text(html: str) -> str:
    plain = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"[\s  ]+", " ", plain)


def test_drilldown_lists_the_transactions_behind_a_label(client):
    response = client.get("/transactions", params={"label": "NETFLIX.COM"})
    assert response.status_code == 200
    body = _text(response.text)
    assert "Netflix.Com" in body
    assert "13,49" in body  # the actual amounts, auditable
    assert "opération" in body


def test_drilldown_marks_internal_transfers(client):
    response = client.get("/transactions", params={"label": "EPGN LIVRET"})
    assert response.status_code == 200
    assert "virement interne" in response.text


def test_subscriptions_link_to_the_drilldown(client):
    assert "/transactions?label=" in client.get("/abonnements").text


def test_override_changes_the_category_everywhere(client):
    """A manual category must survive and apply to the analysis page."""
    before = _text(client.get("/transactions", params={"label": "NETFLIX.COM"}).text)
    assert "Streaming / Loisirs" in before

    posted = client.post(
        "/categorie",
        data={"label": "NETFLIX.COM", "category": "Sport", "back": "/"},
        follow_redirects=False,
    )
    assert posted.status_code == 303

    after = _text(client.get("/transactions", params={"label": "NETFLIX.COM"}).text)
    assert "Sport" in after
    assert "manuel" in after
    # And the aggregate page reflects it too.
    assert "Sport" in _text(client.get("/analyse").text)


def test_override_can_be_reset_to_auto(client):
    client.post(
        "/categorie",
        data={"label": "NETFLIX.COM", "category": "Sport", "back": "/"},
        follow_redirects=False,
    )
    client.post(
        "/categorie",
        data={"label": "NETFLIX.COM", "category": "__auto__", "back": "/"},
        follow_redirects=False,
    )
    body = _text(client.get("/transactions", params={"label": "NETFLIX.COM"}).text)
    assert "Streaming / Loisirs" in body
    assert "manuel" not in body


def test_recap_shows_investment_lines(client):
    body = _text(client.get("/").text)
    assert "Lignes détenues" in body
    assert "ETF MONDE" in body


def test_recap_records_history_and_offers_sync(client):
    body = client.get("/").text
    assert "Évolution du patrimoine" in _text(body)
    assert "/synchroniser/1" in body


def test_sync_endpoint_redirects(client):
    response = client.post("/synchroniser/1", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/?synced=1"


# ------------------------------------------------------------------- callback

def test_callback_success_redirects_with_connection_id(client):
    response = client.get(
        "/callback", params={"connection_id": 42}, follow_redirects=False
    )
    assert response.status_code == 307
    assert "connection_id=42" in response.headers["location"]


def test_callback_error_is_reported_not_swallowed(client):
    """A failed Webview used to look exactly like a success."""
    response = client.get(
        "/callback",
        params={"error": "connection_failed", "error_description": "Identifiants refusés"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "Connexion bancaire non aboutie" in response.text
    assert "Identifiants refusés" in response.text
