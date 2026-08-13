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
