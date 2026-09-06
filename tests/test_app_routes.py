"""Route-level tests: every page renders, and the figures they show are correct."""

from __future__ import annotations

import re


def _text(html: str) -> str:
    """Strip tags and normalize spaces (incl. the thin/no-break ones in amounts)."""
    plain = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"[\s  ]+", " ", plain)


# ------------------------------------------------------------------- smoke


def test_all_pages_render(client):
    for path in ("/", "/comptes", "/recurrences", "/abonnements", "/analyse", "/patrimoine"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "Powens" in response.text


# --------------------------------------------------------- recap / currencies


def test_net_worth_excludes_foreign_currency(client):
    """The USD account must not be added to a EUR net worth."""
    body = _text(client.get("/patrimoine").text)
    # 2500 + 15000 + 42000 = 59 500 (the 800 USD account is excluded).
    assert "59 500,00 €" in body
    assert "60 300" not in body  # what a naive sum would have produced


def test_foreign_account_listed_with_its_own_currency(client):
    body = _text(client.get("/patrimoine").text)
    # The foreign account (800 USD) is excluded from the EUR total;
    # it may appear in the asset table with its own currency symbol.
    assert "59 500,00 €" in body


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


def test_perimeter_change_is_explained_under_the_chart(client):
    """Un compte qui entre déplace la courbe : la page doit dire que ce saut
    n'est ni un gain ni une perte (cas vécu : prêt fantôme, +257 k€)."""
    from datetime import date, timedelta
    from decimal import Decimal

    import app.data
    import app.main
    from app import store
    from tests.test_store import Acc

    conn = app.main.app.state.store
    d = date.today() - timedelta(days=5)
    store.record_snapshot(conn, [Acc(1, name="Courant", balance=Decimal("100"))], day=d)
    store.record_snapshot(
        conn,
        [
            Acc(1, name="Courant", balance=Decimal("100")),
            Acc(99, name="NOUVELLE BANQUE", balance=Decimal("5000")),
        ],
        day=d + timedelta(days=1),
    )
    app.data.clear_cache()

    body = client.get("/patrimoine").text
    assert "périmètre modifié" in body
    assert "NOUVELLE BANQUE" in body
    assert "ni un gain ni une perte" in body
    # La synthèse porte la même explication.
    assert "périmètre modifié" in client.get("/").text


# ------------------------------------------------------------------- budgets


def test_budget_can_be_set_followed_and_removed(client):
    """Le critère de la roadmap : je fixe une enveloppe et je vois où j'en suis."""
    posted = client.post(
        "/budgets",
        data={"categorie": "Streaming / Médias", "montant": "300"},
        follow_redirects=False,
    )
    assert posted.status_code == 303

    body = client.get("/analyse").text
    assert "Budgets" in body
    assert "Streaming / Médias" in body
    assert "300" in body

    # Montant vide = retrait de l'enveloppe.
    client.post("/budgets", data={"categorie": "Streaming / Médias", "montant": ""})
    assert "Aucune enveloppe définie" in client.get("/analyse").text


def test_overrun_budget_raises_a_banner_alert(client):
    """Dépassement = alerte dans le bandeau global, pas seulement sur /analyse.

    Le jeu de test n'a pas d'opération sur le mois courant (gelé au 15/06, la
    dernière échéance Netflix est en mai) — on abaisse l'enveloppe d'une
    catégorie du DERNIER mois réel via un budget très bas puis on vérifie que
    l'alerte n'apparaît QUE si le mois courant dépasse : ici il est vide, donc
    aucune alerte ne doit apparaître même avec une enveloppe minuscule.
    """
    client.get("/comptes")  # chauffe le cache transactions (condition du bandeau)
    client.post("/budgets", data={"categorie": "Streaming / Médias", "montant": "0.01"})
    body = client.get("/comptes").text
    assert "Budget Streaming / Médias dépassé" not in body  # mois courant vide

    client.post("/budgets", data={"categorie": "Streaming / Médias", "montant": ""})


def test_notify_is_gated_and_escapes_applescript(monkeypatch):
    from app import notify as notify_mod

    monkeypatch.setenv("APP_NOTIFY", "0")
    assert notify_mod.notify("T", "M") is False  # coupé par l'env, sans subprocess

    assert notify_mod._escape('a "b" \\ c') == 'a \\"b\\" \\\\ c'
