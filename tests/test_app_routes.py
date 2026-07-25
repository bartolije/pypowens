"""Route-level tests: every page renders, and the figures they show are correct."""

from __future__ import annotations

import re


def _text(html: str) -> str:
    """Strip tags and normalize spaces (incl. the thin/no-break ones in amounts)."""
    plain = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"[\s  ]+", " ", plain)


# ------------------------------------------------------------------- smoke

def test_all_pages_render(client):
    for path in ("/", "/recurrences", "/abonnements", "/analyse"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "Powens" in response.text


# --------------------------------------------------------- recap / currencies

def test_net_worth_excludes_foreign_currency(client):
    """The USD account must not be added to a EUR net worth."""
    body = _text(client.get("/").text)
    # 2500 + 15000 + 42000 = 59 500 (the 800 USD account is excluded).
    assert "59 500,00 €" in body
    assert "60 300" not in body  # what a naive sum would have produced
    assert "Comptes en devises étrangères" in body
    assert "800,00 $" in body


def test_foreign_account_listed_with_its_own_currency(client):
    body = client.get("/").text
    assert "exclus du patrimoine net" in body
    assert "Total USD" in _text(body)


# ------------------------------------------------------------------ analyse

def test_analysis_states_its_period(client):
    body = _text(client.get("/analyse").text)
    assert "12 derniers mois complets" in body


def test_analysis_recurring_split_is_exact(client):
    """Recurring + one-off must add up to the total spending of the window."""
    from decimal import Decimal

    import app.analysis as analysis_module

    captured: dict = {}
    original = analysis_module.templates.TemplateResponse

    def _capture(request, name, context=None, *args, **kwargs):
        captured.update(context or {})
        return original(request, name, context, *args, **kwargs)

    analysis_module.templates.TemplateResponse = _capture
    try:
        assert client.get("/analyse").status_code == 200
    finally:
        analysis_module.templates.TemplateResponse = original

    assert captured["recurring_spend"] + captured["ponctuel_spend"] == captured["total_spend"]
    # The monthly subscription is detected, so the recurring part is not zero.
    assert captured["recurring_spend"] > Decimal(0)
    # The one-off purchase lands in the one-off bucket.
    assert captured["ponctuel_spend"] > Decimal(0)


def test_coming_transaction_never_counted(client):
    """The forecast operation (-99.00) must appear nowhere."""
    for path in ("/analyse", "/recurrences", "/abonnements"):
        assert "99,00" not in _text(client.get(path).text), path


def test_internal_transfer_excluded_from_spending(client):
    """The 500 € savings move is not a expense."""
    body = _text(client.get("/analyse").text)
    assert "500,00 €" not in body


# -------------------------------------------------------------- abonnements

def test_subscription_detected(client):
    body = _text(client.get("/abonnements").text)
    assert "Netflix" in body
    assert "Mensuel" in body
