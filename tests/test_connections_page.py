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


# ------------------------------------- connexion jamais synchronisée


def test_a_never_synced_connection_is_not_shown_as_healthy(client, fake_client):
    """Une connexion tout juste établie n'est ni en erreur ni synchronisée :
    un ✓ vert y était trompeur, et « jamais » n'est pas une bonne nouvelle."""
    import app.data

    fake_client._connections[0]["last_update"] = None
    app.data.clear_cache()

    body = client.get("/connexions").text
    assert "jamais synchronisée" in body
    assert "sync-pending" in body
    assert "sync-ok" not in body
    # La carte est dépliée : c'est ce qu'on vient regarder.
    assert '<details class="card mb-3 connection-card" open>' in body

    fake_client._connections[0]["last_update"] = "2026-07-01 06:00:00"
    app.data.clear_cache()


def test_the_age_macro_never_injects_markup_into_attributes(client):
    """« Synchronisé <span…>jamais</span> » dans un title= refermait l'attribut
    au premier guillemet et laissait « ">✓ » à l'écran."""
    body = client.get("/connexions").text
    assert 'title="Synchronisé <span' not in body
    assert '<span class="text-muted">jamais</span>"' not in body


def test_a_never_synced_connection_is_auto_relaunched(client, fake_client):
    """Sans last_update ET sans next_try, rien ne la synchroniserait jamais :
    c'est le cas qui mérite le plus la relance, et il était exclu."""
    import app.data
    import app.main

    fake_client._connections[0]["last_update"] = None
    fake_client._connections[0].pop("next_try", None)
    fake_client.synced_connections = []
    app.main.app.state.auto_sync_at = None
    app.data.clear_cache()

    client.get("/comptes")
    assert fake_client.synced_connections == [1]

    fake_client._connections[0]["last_update"] = "2026-07-01 06:00:00"
    fake_client.synced_connections = []
    app.data.clear_cache()


# ------------------------------------- âge en jours calendaires


def test_age_counts_calendar_days_not_24h_slices(client, fake_client):
    """Une synchro d'hier 20 h, lue aujourd'hui à 15 h, fait 18 h d'écart : un
    timedelta.days valait 0 et la page annonçait « aujourd'hui » un 13 août
    pour une synchro du 12. L'humain compte au changement de date."""
    import app.data

    # FROZEN_TODAY = 2026-06-15 ; la veille à 20 h 34.
    fake_client._connections[0]["last_update"] = "2026-06-14 20:34:00"
    app.data.clear_cache()

    body = _text(client.get("/connexions").text)
    assert "synchronisé hier" in body
    assert "aujourd'hui" not in body

    fake_client._connections[0]["last_update"] = "2026-07-01 06:00:00"
    app.data.clear_cache()


def test_a_silent_connection_is_not_shown_with_a_green_check(client, fake_client):
    """Sans erreur déclarée mais sans nouvelle depuis des jours, un ✓ vert
    affirmait « tout va bien » — c'est pourtant l'état qu'on vient chercher."""
    import app.data

    fake_client._connections[0]["last_update"] = "2026-06-01 06:00:00"  # 14 j
    app.data.clear_cache()

    body = client.get("/connexions").text
    assert "muette depuis 14 j" in body
    assert "sync-ok" not in body

    fake_client._connections[0]["last_update"] = "2026-07-01 06:00:00"
    app.data.clear_cache()


def test_the_badge_is_a_state_and_the_button_is_an_action(client):
    """Un « ✓ » nu à côté d'un bouton « Synchroniser » se lisait comme deux
    étiquettes, ou deux boutons : les deux doivent se distinguer au premier
    coup d'œil."""
    body = _text(client.get("/connexions").text)
    assert "✓ à jour" in body
    assert "Synchroniser maintenant" in body


# ------------------------------------- refus de synchronisation


def test_a_refused_sync_says_so_instead_of_doing_nothing(client, fake_client):
    """Powens répond 409 « Can't force synchronization » quand la connexion
    vient d'être rafraîchie. Le bouton semblait alors ne rien faire."""
    from pypowens import PowensAPIError

    async def refuse(connection_id, user_id="me"):
        raise PowensAPIError(
            409, code="conflict", message="Can't force synchronization of connection 1"
        )

    original = fake_client.update_connection
    fake_client.update_connection = refuse
    try:
        body = _text(client.post("/connexions/1/synchroniser", headers={"HX-Request": "true"}).text)
        assert "refuse une synchronisation forcée" in body
    finally:
        fake_client.update_connection = original


def test_a_successful_sync_shows_no_alarm(client, fake_client):
    body = _text(client.post("/connexions/1/synchroniser", headers={"HX-Request": "true"}).text)
    assert "refuse une synchronisation" not in body
