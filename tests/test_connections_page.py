"""Page « Connexions » : l'état des lieux sans attendre la panne.

Le bandeau ne parle que quand ça casse ; cette page répond à la question
inverse — « qu'est-ce qui rentre, depuis quand, et mon historique suit-il ? ».
"""

from __future__ import annotations

import re


def _text(html: str) -> str:
    plain = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"[\s  ]+", " ", plain)


def test_page_lists_connections_with_their_accounts(client):
    body = _text(client.get("/connexions").text)
    assert "Ma Banque" in body
    assert "Compte courant" in body
    assert "Livret" in body
    assert "PEA" in body


def test_each_account_shows_its_last_operation_and_count(client):
    body = _text(client.get("/connexions").text)
    assert "Dernière opération" in body
    assert "Opé." in body
    # Le compte courant du jeu de test porte des opérations : le compteur suit.
    assert re.search(r"\d+ ", body)


def test_the_three_dates_are_distinguished(client):
    """Synchro, dernière opération et dernier solde archivé ne répondent pas à
    la même question : la page doit les séparer explicitement."""
    body = _text(client.get("/connexions").text)
    assert "Dernière synchro" in body
    assert "Dernière opération" in body
    assert "Solde archivé" in body
    assert "trois questions différentes" in body.lower() or "Trois dates" in body


def test_a_broken_connection_is_shown_first_with_its_repair_path(client, fake_client):
    import app.data

    fake_client._connections[0]["state"] = "wrongpass"
    app.data.clear_cache()
    body = client.get("/connexions").text
    assert "Identifiants refusés par la banque" in body
    assert "/reconnecter/1" in body  # seule l'utilisateur peut réparer un wrongpass

    fake_client._connections[0]["state"] = None
    app.data.clear_cache()


def test_a_healthy_connection_offers_a_plain_sync(client):
    """Le bouton passe par HTMX : la liste se rafraîchit sans recharger la page."""
    body = client.get("/connexions").text
    assert 'hx-post="/connexions/1/synchroniser' in body
    assert 'hx-target="#connexions-liste"' in body


def test_disabled_accounts_are_listed_and_flagged(client, fake_client):
    """Un compte désactivé par Powens sort des totaux : c'est ici qu'on doit le
    voir, plutôt que de le découvrir par un trou dans la courbe."""
    import app.data

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
    app.data.clear_cache()

    body = client.get("/connexions").text
    assert "PRET IMMO MODULABLE" in body
    assert "hors total" in body

    fake_client._disabled_accounts.clear()
    app.data.clear_cache()


def test_missing_next_try_is_called_out(client, fake_client):
    """Sans prochaine synchro planifiée, Powens ne repassera jamais seul — c'est
    l'état exact où une connexion se fige en silence."""
    import app.data

    fake_client._connections[0].pop("next_try", None)
    app.data.clear_cache()
    assert "aucune synchro planifiée" in _text(client.get("/connexions").text)


def test_page_is_reachable_from_the_navigation(client):
    assert 'href="/connexions"' in client.get("/comptes").text
