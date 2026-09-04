"""Bandeau global de santé des connexions.

Né d'un cas réel : une connexion tombée en ``wrongpass`` a fait désactiver un
prêt immobilier de -257 k€ côté Powens — le patrimoine a « gagné » 257 k€ du
jour au lendemain, sans aucun signal hors de la page /patrimoine.
"""

from __future__ import annotations

import re


def _text(html: str) -> str:
    plain = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"[\s  ]+", " ", plain)


def _reset(fake_client):
    import app.data

    app.data.clear_cache()


def test_no_banner_when_everything_is_healthy(client):
    body = client.get("/comptes").text
    assert "alert-strip" not in body


def test_error_connection_raises_a_banner_on_every_page(client, fake_client):
    """Identifiants refusés : bandeau partout, avec le parcours de réparation."""
    fake_client._connections[0]["state"] = "wrongpass"
    _reset(fake_client)

    for path in ("/comptes", "/analyse", "/abonnements"):
        body = client.get(path).text
        assert "alert-strip" in body, path
        assert "Identifiants refusés par la banque" in body, path
        assert "/reconnecter/1" in body, path  # wrongpass = seule l'utilisateur peut réparer

    fake_client._connections[0]["state"] = None
    _reset(fake_client)


def test_silent_connection_is_flagged_with_a_sync_button(client, fake_client):
    """Une connexion muette en état « OK » est le cas le plus sournois."""
    fake_client._connections[0]["last_update"] = "2026-05-20 06:00:00"  # 26 j avant FROZEN_TODAY
    _reset(fake_client)

    body = client.get("/comptes").text
    assert "muette depuis" in body
    assert 'action="/synchroniser/1"' in body  # rejouable sans re-authentification

    fake_client._connections[0]["last_update"] = "2026-07-01 06:00:00"
    _reset(fake_client)


def test_disabled_account_with_balance_is_reported_as_excluded(client, fake_client):
    """Le prêt fantôme : désactivé côté Powens, son solde sort du total sans bruit."""
    fake_client._disabled_accounts.append(
        {
            "id": 27,
            "id_connection": 1,
            "name": "PRET IMMO MODULABLE",
            "type": "loan",
            "balance": "-256797.68",
            "currency": {"id": "EUR"},
            "disabled": "2026-06-01 17:59:47",
        }
    )
    _reset(fake_client)

    body = client.get("/comptes").text
    plain = _text(body)
    assert "PRET IMMO MODULABLE" in plain
    assert "exclu du patrimoine" in plain
    assert "256 797,68" in plain
    assert 'action="/comptes/27/reactiver"' in body  # le bouton Réintégrer

    fake_client._disabled_accounts.clear()
    _reset(fake_client)


def test_deleted_ghost_versions_are_not_double_counted(client, fake_client):
    """Powens recrée le même compte pendant une panne : seule la version non
    supprimée doit compter (le vrai prêt existait en 3 exemplaires)."""
    ghost = {
        "id": 20,
        "id_connection": 1,
        "name": "PRET IMMO MODULABLE",
        "type": "loan",
        "balance": "-256797.68",
        "currency": {"id": "EUR"},
        "disabled": "2026-05-31 16:47:30",
        "deleted": "2026-05-31 16:47:30",
    }
    live = {**ghost, "id": 27, "deleted": None, "disabled": "2026-06-01 17:59:47"}
    fake_client._disabled_accounts.extend([ghost, live])
    _reset(fake_client)

    body = client.get("/comptes").text
    assert body.count("reactiver") == 1  # un seul bouton, pas un par fantôme

    fake_client._disabled_accounts.clear()
    _reset(fake_client)


def test_banner_failure_never_breaks_the_page(client, fake_client, monkeypatch):
    """Le bandeau est best-effort : si Powens tousse, la page rend sans lui."""
    import app.main

    async def boom(*args, **kwargs):
        raise RuntimeError("powens down")

    monkeypatch.setattr(app.main, "connection_alerts", boom)
    _reset(fake_client)
    response = client.get("/comptes")
    assert response.status_code == 200
    assert "alert-strip" not in response.text


def test_reactivating_the_account_clears_the_alert(client, fake_client):
    """Après réparation, Powens recrée le compte DÉSACTIVÉ : le bouton
    Réintégrer est le seul chemin pour le faire revenir dans le total."""
    fake_client._disabled_accounts.append(
        {
            "id": 28,
            "id_connection": 1,
            "name": "PRET IMMO MODULABLE",
            "type": "loan",
            "balance": "-255954.00",
            "currency": {"id": "EUR"},
            "disabled": "2026-06-01 09:59:27",
        }
    )
    _reset(fake_client)
    assert "exclu du patrimoine" in _text(client.get("/comptes").text)

    response = client.post("/comptes/28/reactiver", follow_redirects=False)
    assert response.status_code == 303

    body = _text(client.get("/comptes").text)
    assert "exclu du patrimoine" not in body

    fake_client._disabled_accounts.clear()
    _reset(fake_client)


# ---------------------------------------------------------- synchro d'ouverture


def test_stuck_connection_is_auto_synced_on_page_load(client, fake_client):
    """Saine, >24 h sans synchro, aucun next_try : Powens ne repassera jamais
    seul (le cas Trade Republic, figée deux semaines). Ouvrir l'app relance."""
    fake_client._connections[0]["last_update"] = "2026-06-10 06:00:00"  # 5 j avant
    _reset(fake_client)
    import app.main

    app.main.app.state.auto_sync_at = None

    client.get("/comptes")
    assert getattr(fake_client, "synced_connections", []) == [1]

    # Throttle : le rechargement suivant ne redéclenche pas.
    client.get("/comptes")
    assert fake_client.synced_connections == [1]

    fake_client._connections[0]["last_update"] = "2026-07-01 06:00:00"
    fake_client.synced_connections = []
    _reset(fake_client)


def test_connection_in_error_is_never_auto_synced(client, fake_client):
    """Relancer un webauthRequired en boucle peut déclencher des SCA : jamais."""
    fake_client._connections[0]["last_update"] = "2026-06-01 06:00:00"
    fake_client._connections[0]["state"] = "webauthRequired"
    _reset(fake_client)
    import app.main

    app.main.app.state.auto_sync_at = None

    client.get("/comptes")
    assert getattr(fake_client, "synced_connections", []) == []

    fake_client._connections[0]["state"] = None
    fake_client._connections[0]["last_update"] = "2026-07-01 06:00:00"
    _reset(fake_client)


def test_planned_next_try_is_not_doubled(client, fake_client):
    """Si Powens a déjà planifié une synchro (next_try futur), ne pas doubler."""
    fake_client._connections[0]["last_update"] = "2026-06-10 06:00:00"
    fake_client._connections[0]["next_try"] = "2026-06-15 23:00:00"  # ce soir
    _reset(fake_client)
    import app.main

    app.main.app.state.auto_sync_at = None

    client.get("/comptes")
    assert getattr(fake_client, "synced_connections", []) == []

    del fake_client._connections[0]["next_try"]
    fake_client._connections[0]["last_update"] = "2026-07-01 06:00:00"
    _reset(fake_client)


# ------------------------------------------------------------- alertes budget


def test_overrun_budget_shows_in_the_banner(client, fake_client):
    """Enveloppe 10 €, dépense 42 € ce mois-ci → alerte sur TOUTES les pages."""
    fake_client._txns.append(
        {
            "id": 950,
            "id_account": 1,
            "date": "2026-06-03",  # mois courant gelé (juin 2026)
            "value": "-42.00",
            "type": "card",
            "wording": "NETFLIX.COM",
            "simplified_wording": "NETFLIX.COM",
            "original_wording": "NETFLIX.COM",
            "coming": False,
        }
    )
    _reset(fake_client)
    client.get("/comptes")  # chauffe le cache (condition d'affichage du bandeau)
    client.post("/budgets", data={"categorie": "Streaming / Médias", "montant": "10"})

    body = client.get("/import").text  # une page SANS rapport avec l'analyse
    assert "Budget Streaming / Médias dépassé" in body
    assert "/analyse#budgets" in body

    client.post("/budgets", data={"categorie": "Streaming / Médias", "montant": ""})
    fake_client._txns.pop()
    _reset(fake_client)


# ----------------------------------------------------- réintégration permanente


def _disabled_loan(account_id: int) -> dict:
    return {
        "id": account_id,
        "id_connection": 1,
        "name": "PRET IMMO MODULABLE",
        "type": "loan",
        "balance": "-255954.00",
        "currency": {"id": "EUR"},
        "disabled": "2026-06-01 09:59:27",
    }


def test_reactivation_pins_the_account_and_keeps_the_history_cache(client, fake_client):
    """Réintégrer épingle l'identité stable du compte et n'efface plus tout le
    cache : l'historique reste servi (périmé, rafraîchi en fond)."""
    import app.data as data
    from app import store as st

    fake_client._disabled_accounts.append(_disabled_loan(29))
    _reset(fake_client)
    client.get("/analyse")  # chauffe l'historique des transactions
    assert data._TXN_KEY in data._cache

    response = client.post("/comptes/29/reactiver", follow_redirects=False)
    assert response.status_code == 303

    assert data._TXN_KEY in data._cache, "l'historique n'est pas jeté"
    assert data._TXN_KEY in data._stale_marks, "… mais déclaré périmé"
    assert "accounts" not in data._cache, "les listes de comptes sont rechargées"

    conn = client.app.state.store
    assert st.pinned_accounts(conn) == {"conn:1|PRET IMMO MODULABLE": "PRET IMMO MODULABLE"}

    body = client.get("/reglages").text
    assert "PRET IMMO MODULABLE" in body and 'value="epingle"' in body
    client.post(
        "/reglages/oublier",
        data={"quoi": "epingle", "label": "conn:1|PRET IMMO MODULABLE"},
        follow_redirects=False,
    )
    assert st.pinned_accounts(conn) == {}

    fake_client._disabled_accounts.clear()
    _reset(fake_client)


def test_pinned_account_comes_back_by_itself(client, fake_client):
    """Powens redésactive (ou recrée sous un autre id) : l'épingle le réintègre
    pendant le calcul du bandeau, sans clic."""
    from app import health
    from app import store as st

    health.reset_reactivation_throttle()
    st.pin_account(client.app.state.store, "conn:1|PRET IMMO MODULABLE", "PRET IMMO MODULABLE")
    fake_client._disabled_accounts.append(_disabled_loan(31))  # nouvel id après recréation
    _reset(fake_client)

    body = _text(client.get("/comptes").text)
    assert "exclu du patrimoine" not in body
    assert "disabled" not in fake_client._disabled_accounts[0], "le PUT a été rejoué"

    fake_client._disabled_accounts.clear()
    st.unpin_account(client.app.state.store, "conn:1|PRET IMMO MODULABLE")
    _reset(fake_client)


def test_auto_reactivation_is_throttled_when_powens_refuses(client, fake_client, monkeypatch):
    from app import health
    from app import store as st

    health.reset_reactivation_throttle()
    st.pin_account(client.app.state.store, "conn:1|PRET IMMO MODULABLE", "PRET IMMO MODULABLE")
    fake_client._disabled_accounts.append(_disabled_loan(32))
    _reset(fake_client)
    calls = {"n": 0}

    async def refuse(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("Powens refuse")

    monkeypatch.setattr(fake_client, "update_account", refuse)
    first = _text(client.get("/comptes").text)
    second = _text(client.get("/comptes").text)
    assert calls["n"] == 1, "une seule tentative par heure"
    assert "exclu du patrimoine" in first and "exclu du patrimoine" in second, "le bouton reste"

    fake_client._disabled_accounts.clear()
    st.unpin_account(client.app.state.store, "conn:1|PRET IMMO MODULABLE")
    health.reset_reactivation_throttle()
    _reset(fake_client)
