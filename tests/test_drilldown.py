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
    assert "Streaming / Médias" in before

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
    assert "Streaming / Médias" in body
    assert "manuel" not in body


def test_recap_shows_investment_lines(client):
    body = _text(client.get("/patrimoine").text)
    assert "Ma performance" in body
    assert "ETF MONDE" in body


def test_recap_records_history_and_offers_sync(client):
    body = client.get("/patrimoine").text
    assert "Performance" in _text(body)
    assert "/synchroniser/1" in body


def test_sync_endpoint_redirects(client):
    response = client.post("/synchroniser/1", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/patrimoine?synced=1"


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


def test_callback_without_usable_parameters_is_reported(client):
    """An empty return used to 422 (typed connection_id) or look like a success."""
    response = client.get("/callback", follow_redirects=False)
    assert response.status_code == 400
    assert "Retour du Webview incomplet" in response.text
    # The page must name the redirect_uri to whitelist in the Powens console.
    assert "127.0.0.1:8000/callback" in response.text


def test_callback_tolerates_an_empty_connection_id(client):
    client.app.state.webview_state = "expected-state"
    response = client.get(
        "/callback",
        params={"connection_id": "", "code": "abc", "state": "expected-state"},
        follow_redirects=False,
    )
    assert response.status_code == 307
    assert response.headers["location"] == "/patrimoine?connected=1"


def test_callback_refuses_a_code_without_the_issued_state(client):
    """Un lien piégé /callback?code=… ne doit JAMAIS échanger le code.

    Sans jeton ``state``, n'importe quelle page pouvait faire basculer l'app sur
    l'utilisateur Powens d'un attaquant (fixation de session).
    """
    client.app.state.webview_state = "expected-state"
    response = client.get(
        "/callback", params={"code": "attacker-code"}, follow_redirects=False
    )
    assert response.status_code == 400
    assert "non reconnu" in response.text

    # État déjà consommé ou jamais émis : même refus.
    client.app.state.webview_state = None
    response = client.get(
        "/callback", params={"code": "abc", "state": "stale"}, follow_redirects=False
    )
    assert response.status_code == 400


def test_callback_state_is_single_use(client):
    client.app.state.webview_state = "one-shot"
    first = client.get(
        "/callback", params={"code": "abc", "state": "one-shot"}, follow_redirects=False
    )
    assert first.status_code == 307
    replay = client.get(
        "/callback", params={"code": "abc", "state": "one-shot"}, follow_redirects=False
    )
    assert replay.status_code == 400


# ---------------------------------------------------------------------- export

def test_export_csv_streams_the_history_with_categories(client):
    response = client.get("/export.csv")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert 'attachment; filename="transactions-' in response.headers["content-disposition"]
    body = response.text
    assert "date;compte;libelle;categorie;montant;interne" in body
    assert "NETFLIX.COM" in body
    assert "Streaming / Médias" in body
    assert "-13,49" in body  # décimale en virgule, comme les relevés importés


# ------------------------------------------------------- virement interne manuel

def test_flagging_a_label_as_internal_excludes_it_everywhere(client):
    """CCF → Bourso puis Bourso → 3 comptes : l'heuristique miroir ne voit pas
    ces flux (jambes typées différemment, montants éclatés). Le marquage manuel
    « Virement interne » est la soupape — et il doit exclure PARTOUT."""
    # Avant : Netflix est un abonnement détecté et compte dans les dépenses.
    assert "NETFLIX.COM" in client.get("/abonnements").text

    posted = client.post(
        "/categorie",
        data={"label": "NETFLIX.COM", "category": "Virement interne", "back": "/"},
        follow_redirects=False,
    )
    assert posted.status_code == 303

    # Après : plus un abonnement…
    assert "NETFLIX.COM" not in client.get("/abonnements").text
    # …marqué comme virement interne sur le drill-down (listé, non compté)…
    assert "virement interne" in client.get(
        "/transactions", params={"label": "NETFLIX.COM"}
    ).text
    # …et retour à la normale quand on rend la main à l'heuristique.
    client.post(
        "/categorie",
        data={"label": "NETFLIX.COM", "category": "__auto__", "back": "/"},
        follow_redirects=False,
    )
    assert "NETFLIX.COM" in client.get("/abonnements").text


def test_the_category_form_offers_virement_interne(client):
    body = client.get("/transactions", params={"label": "NETFLIX.COM"}).text
    assert "Virement interne" in body


# ----------------------------------------------------- recherche, fusion, renommage

def test_search_finds_by_label_and_by_amount(client):
    body = _text(client.get("/recherche", params={"q": "netflix"}).text)
    assert "NETFLIX.COM" in body
    # Par montant exact, virgule française acceptée, les deux sens.
    body = _text(client.get("/recherche", params={"q": "13,49"}).text)
    assert "NETFLIX.COM" in body
    # Aucun résultat = dit clairement, pas une page vide.
    assert "Aucun résultat" in _text(client.get("/recherche", params={"q": "zzzz"}).text)


def test_merging_merchants_groups_them_everywhere(client, fake_client):
    """Le libellé carte et le prélèvement du même marchand = deux clés → une."""
    posted = client.post(
        "/marchands/fusionner",
        data={"source": "NETFLIX.COM", "cible": "NETFLIX"},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert "label=NETFLIX" in posted.headers["location"]

    # La clé fusionnée regroupe les opérations de la source.
    body = _text(client.get("/transactions", params={"label": "NETFLIX"}).text)
    assert "13,49" in body

    # Défusion : cible vide.
    client.post("/marchands/fusionner", data={"source": "NETFLIX.COM", "cible": ""})
    import app.data
    app.data.clear_cache()


def test_renaming_an_account_applies_everywhere(client):
    posted = client.post(
        "/comptes/1/renommer", data={"nom": "Compte joint"}, follow_redirects=False
    )
    assert posted.status_code == 303
    assert "Compte joint" in client.get("/patrimoine").text
    assert "Compte joint" in client.get("/comptes").text
    # Vide = retour au nom Powens.
    client.post("/comptes/1/renommer", data={"nom": ""})
    assert "Compte joint" not in client.get("/patrimoine").text


def test_detail_transactions_tab_is_paginated(client):
    body = client.get("/patrimoine/1", params={"tab": "transactions"}).text
    # 31 opérations dans le jeu de test : une seule page, pas de pagination.
    assert "page 1 /" not in body

    # La navigation apparaît dès que le compte dépasse la taille de page (100).
    from app.detail import _PAGE_SIZE
    assert _PAGE_SIZE == 100
