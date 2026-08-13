"""Connecteurs en authentification web (Sumeria/Lydia et la plupart des néobanques).

Cas vécu : « Reprendre » envoyait vers le Webview Powens, qui pour ces
connecteurs rebondit sur la page d'accueil de la banque — sans rien à faire.
L'URL d'autorisation réelle est rangée par Powens dans ``error_message``.
"""

from __future__ import annotations

from datetime import datetime

from app import webauth

REAL = (
    "Redirecting to https://lydia-app.com/site/open/dispatch/openauth"
    "?redirect_uri=https%3A%2F%2Fbiapi.pro%2F2.0%2Fwebauth%2Fcallback"
    "&scope=aisp&client_id=019c673f-dc9b-74bd-afe8-1c1127e9a351&state=B2023jb"
)


class _Conn:
    def __init__(self, **raw):
        self.raw = raw
        self.id = raw.get("id", 22)


def test_authorize_url_is_extracted_from_the_error_message():
    url = webauth.authorize_url(_Conn(error_message=REAL))
    assert url is not None
    assert url.startswith("https://lydia-app.com/site/open/dispatch/openauth?")
    assert "state=B2023jb" in url  # le jeton d'état doit survivre intact


def test_only_https_is_ever_followed():
    """Cette URL sert de cible de redirection : pas de redirection ouverte."""
    assert webauth.authorize_url(_Conn(error_message="Redirecting to http://evil.example")) is None
    assert webauth.authorize_url(_Conn(error_message="javascript:alert(1)")) is None
    assert webauth.authorize_url(_Conn(error_message="")) is None
    assert webauth.authorize_url(_Conn()) is None


def test_expiry_is_honoured():
    conn = _Conn(expire="2026-08-13 16:25:09")
    assert webauth.is_expired(conn, now=datetime(2026, 8, 13, 16, 26)) is True
    assert webauth.is_expired(conn, now=datetime(2026, 8, 13, 15, 0)) is False
    # Sans date d'expiration, on tente le parcours plutôt que de le refuser.
    assert webauth.is_expired(_Conn()) is False
    assert webauth.is_expired(_Conn(expire="pas une date")) is False


def test_needs_webauth_only_for_that_state():
    assert webauth.needs_webauth(_Conn(state="webauthRequired")) is True
    assert webauth.needs_webauth(_Conn(state="wrongpass")) is False
    assert webauth.needs_webauth(_Conn()) is False


# ------------------------------------------------------------------- route

def test_reconnect_sends_to_the_bank_not_to_the_webview(client, fake_client):
    """Le test de non-régression du bug Sumeria."""
    import app.data

    fake_client._connections[0].update(
        {"state": "webauthRequired", "error_message": REAL, "expire": "2099-01-01 00:00:00"}
    )
    app.data.clear_cache()

    response = client.get("/reconnecter/1", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"].startswith("https://lydia-app.com/")
    assert "webview.powens.com" not in response.headers["location"]

    fake_client._connections[0].update({"state": None, "error_message": None})
    fake_client._connections[0].pop("expire", None)
    app.data.clear_cache()


def test_an_expired_link_is_regenerated_before_redirecting(client, fake_client):
    import app.data

    fake_client._connections[0].update(
        {"state": "webauthRequired", "error_message": REAL, "expire": "2020-01-01 00:00:00"}
    )
    fake_client.synced_connections = []
    app.data.clear_cache()

    client.get("/reconnecter/1", follow_redirects=False)
    # Powens a été sollicité pour produire un lien neuf.
    assert fake_client.synced_connections == [1]

    fake_client._connections[0].update({"state": None, "error_message": None})
    fake_client._connections[0].pop("expire", None)
    app.data.clear_cache()


def test_a_classic_connector_still_goes_through_the_webview(client, fake_client):
    """Les connecteurs à identifiants ne doivent pas changer de chemin."""
    import app.data

    fake_client._connections[0]["state"] = "wrongpass"
    app.data.clear_cache()

    response = client.get("/reconnecter/1", follow_redirects=False)
    assert response.status_code == 307
    assert "webview.powens.com" in response.headers["location"]

    fake_client._connections[0]["state"] = None
    app.data.clear_cache()
