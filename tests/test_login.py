"""Page de connexion : session signée, repli Basic, et plus aucune fenêtre système.

Le HTTP Basic tenait la porte, mais sa fenêtre système ne se laisse pas remplir
par un gestionnaire de mots de passe et ne permet pas de se déconnecter. Ce qui
compte ici : un navigateur ne doit JAMAIS recevoir ``WWW-Authenticate`` (c'est
lui qui déclenche la fenêtre), pendant que les scripts continuent de passer.
"""

from __future__ import annotations

import pytest

from app import auth

BROWSER = {"Accept": "text/html,application/xhtml+xml", "Sec-Fetch-Mode": "navigate"}


@pytest.fixture
def secured(client, monkeypatch):
    """App avec identifiants configurés, sans session ouverte."""
    monkeypatch.setenv("APP_AUTH_USER", "jb")
    monkeypatch.setenv("APP_AUTH_PASSWORD", "secret")
    auth.reset_failures()
    client.cookies.clear()
    yield client
    client.cookies.clear()
    auth.reset_failures()


def _login(client, user="jb", password="secret", **extra):
    return client.post(
        "/connexion",
        data={"username": user, "password": password, **extra},
        headers=BROWSER,
        follow_redirects=False,
    )


# ----------------------------------------------------------------- le parcours


def test_a_browser_is_sent_to_the_login_page_never_to_a_system_dialog(secured):
    response = secured.get("/patrimoine", headers=BROWSER, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/connexion?suite=")
    assert "www-authenticate" not in response.headers, "c'est lui qui ouvre la fenêtre système"


def test_the_login_page_is_fillable_by_a_password_manager(secured):
    body = secured.get("/connexion", headers=BROWSER).text

    assert 'action="/connexion"' in body and 'method="post"' in body
    assert 'name="username"' in body and 'autocomplete="username"' in body
    assert 'name="password"' in body and 'autocomplete="current-password"' in body
    assert 'type="password"' in body
    assert 'for="username"' in body and 'for="password"' in body


def test_signing_in_opens_a_session_and_returns_to_the_requested_page(secured):
    refused = secured.get("/analyse", headers=BROWSER, follow_redirects=False)
    suite = refused.headers["location"].split("suite=")[1]

    response = _login(secured, suite=suite)

    assert response.status_code == 303
    assert response.headers["location"] == "/analyse"
    assert auth.SESSION_COOKIE in response.cookies
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert "secure" not in cookie, "en clair sur 127.0.0.1, sinon le cookie est jeté"

    assert secured.get("/analyse", headers=BROWSER).status_code == 200


def test_a_wrong_password_says_so_without_opening_a_session(secured):
    response = _login(secured, password="au hasard")

    assert response.status_code == 401
    assert "Identifiants incorrects" in response.text
    assert auth.SESSION_COOKIE not in response.cookies
    assert secured.get("/", headers=BROWSER, follow_redirects=False).status_code == 303


def test_the_return_path_can_only_point_at_this_app(secured):
    import base64

    piege = base64.urlsafe_b64encode(b"//piege.exemple/vol").decode()
    response = _login(secured, suite=piege)

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_signing_out_closes_the_session(secured):
    _login(secured)
    assert secured.get("/", headers=BROWSER).status_code == 200
    assert "Déconnexion" in secured.get("/", headers=BROWSER).text

    response = secured.post("/deconnexion", headers=BROWSER, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/connexion"
    secured.cookies.clear()  # le navigateur applique l'expiration
    assert secured.get("/", headers=BROWSER, follow_redirects=False).status_code == 303


def test_an_open_session_skips_the_login_page(secured):
    _login(secured)
    response = secured.get("/connexion", headers=BROWSER, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"


# ------------------------------------------------------------------- le jeton


def test_a_tampered_or_foreign_token_is_worthless(secured):
    valid = auth.issue_session("jb")
    assert auth.read_session(valid) == "jb"
    assert auth.read_session(valid[:-2] + "xx") is None
    assert auth.read_session("n'importe quoi") is None
    assert auth.read_session(auth.issue_session("jb", issued_at=0)) is None, "expiré"

    secured.cookies.set(auth.SESSION_COOKIE, valid[:-2] + "xx")
    assert secured.get("/", headers=BROWSER, follow_redirects=False).status_code == 303


def test_changing_the_password_signs_everyone_out(secured, monkeypatch):
    _login(secured)
    assert secured.get("/", headers=BROWSER).status_code == 200

    monkeypatch.setenv("APP_AUTH_PASSWORD", "un-autre-secret")

    assert secured.get("/", headers=BROWSER, follow_redirects=False).status_code == 303


def test_an_explicit_secret_survives_a_password_change(secured, monkeypatch):
    monkeypatch.setenv("APP_SESSION_SECRET", "clé-de-signature-stable")
    _login(secured)
    assert secured.get("/", headers=BROWSER).status_code == 200

    monkeypatch.setenv("APP_AUTH_PASSWORD", "un-autre-secret")

    assert secured.get("/", headers=BROWSER).status_code == 200


def test_the_cookie_is_secure_behind_https(secured):
    response = secured.post(
        "/connexion",
        data={"username": "jb", "password": "secret"},
        headers={**BROWSER, "X-Forwarded-Proto": "https"},
        follow_redirects=False,
    )
    assert "secure" in response.headers["set-cookie"].lower()


# ------------------------------------------------------- scripts et exemptions


def test_a_script_still_gets_its_basic_challenge_and_passes(secured):
    """scripts/backup-prod.sh tire la base en curl -u : ne pas casser ça."""
    refused = secured.get("/sauvegarde.db")  # Accept: */*, pas de Sec-Fetch-Mode
    assert refused.status_code == 401
    assert refused.headers["www-authenticate"].startswith("Basic")

    assert secured.get("/sauvegarde.db", auth=("jb", "secret")).status_code == 200
    assert secured.get("/", auth=("jb", "secret")).status_code == 200


def test_an_htmx_request_is_redirected_not_challenged(secured):
    response = secured.get(
        "/connexions", headers={"HX-Request": "true", "Accept": "*/*"}, follow_redirects=False
    )
    assert response.status_code == 401
    assert response.headers["hx-redirect"].startswith("/connexion")
    assert "www-authenticate" not in response.headers


def test_the_login_page_keeps_its_stylesheet_and_the_probe_stays_open(secured):
    assert secured.get("/static/style.css", headers=BROWSER).status_code == 200
    assert secured.get("/health").status_code == 200


def test_without_credentials_nothing_asks_for_anything(client):
    """L'usage local ne gagne aucune friction, et pas de bouton Déconnexion."""
    body = client.get("/", headers=BROWSER).text
    assert "Déconnexion" not in body
    assert client.get("/connexion", headers=BROWSER, follow_redirects=False).status_code == 303


# ------------------------------------------------------------------- throttle


def test_repeated_form_failures_lock_the_client_out(secured):
    for _ in range(auth._MAX_FAILURES):
        assert _login(secured, password="au hasard").status_code == 401

    blocked = _login(secured)  # même avec le bon mot de passe
    assert blocked.status_code == 429
    assert "Trop de tentatives" in blocked.text
    assert auth.SESSION_COOKIE not in blocked.cookies


def test_a_successful_sign_in_clears_the_counter(secured):
    for _ in range(auth._MAX_FAILURES - 1):
        _login(secured, password="au hasard")

    assert _login(secured).status_code == 303

    secured.cookies.clear()
    for _ in range(auth._MAX_FAILURES - 1):
        assert _login(secured, password="au hasard").status_code == 401
