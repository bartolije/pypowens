"""Durcissements de l'authentification, chacun face à la faille qu'il ferme.

Chaque test nomme ce qui était possible avant lui : un frein annulé par un
en-tête, une déconnexion qui n'en était pas une, un cookie signé avec une graine
devinable, un formulaire soumis depuis un autre site.
"""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest
from starlette.requests import Request

from app import auth, config

BROWSER = {"Accept": "text/html,application/xhtml+xml", "Sec-Fetch-Mode": "navigate"}

PASSWORD = "mot-de-passe-de-test-tres-long-42"


@pytest.fixture
def secured(client, monkeypatch):
    monkeypatch.setenv("APP_AUTH_USER", "jb")
    monkeypatch.setenv("APP_AUTH_PASSWORD", PASSWORD)
    auth.reset_failures()
    client.cookies.clear()
    yield client
    client.cookies.clear()
    auth.reset_failures()


def _login(client, *, password=PASSWORD, user="jb", headers=None):
    return client.post(
        "/connexion",
        data={"username": user, "password": password},
        headers={**BROWSER, **(headers or {})},
        follow_redirects=False,
    )


def _request(headers: dict[str, str], *, host: str = "10.0.0.1") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "scheme": "http",
            "root_path": "",
            "server": ("testserver", 80),
            "client": (host, 1234),
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
    )


# ------------------------------------------------- F1 : le frein ne se contourne pas


def test_the_client_key_prefers_the_header_the_client_cannot_write():
    """``CF-Connecting-IP`` est écrasé par Cloudflare : non usurpable."""
    request = _request({"cf-connecting-ip": "198.51.100.9", "x-forwarded-for": "1.1.1.1"})
    assert auth._client_key(request) == "198.51.100.9"


def test_the_client_key_takes_the_rightmost_forwarded_entry():
    """La liste se lit de droite à gauche : la dernière entrée est celle ajoutée
    par le relais, la première celle envoyée par le client — donc forgeable."""
    request = _request({"x-forwarded-for": "1.1.1.1, 2.2.2.2, 203.0.113.7"})
    assert auth._client_key(request) == "203.0.113.7"


def test_the_client_key_falls_back_to_the_socket():
    assert auth._client_key(_request({})) == "10.0.0.1"


def test_rotating_the_forwarded_header_no_longer_resets_the_brake(secured):
    """La faille : le compteur était indexé sur l'entrée GAUCHE de
    ``X-Forwarded-For``, que le client écrit lui-même. En la changeant à chaque
    requête, il repartait de zéro — le frein anti-force-brute ne freinait rien.
    """
    proxy = "203.0.113.7"  # l'entrée ajoutée par le relais, la seule fiable
    for attempt in range(auth._MAX_FAILURES):
        response = _login(
            secured,
            password="au hasard",
            headers={"X-Forwarded-For": f"9.9.9.{attempt}, {proxy}"},
        )
        assert response.status_code == 401

    blocked = _login(secured, headers={"X-Forwarded-For": f"9.9.9.99, {proxy}"})
    assert blocked.status_code == 429, "une forge neuve ne doit plus rien rouvrir"


def test_a_distributed_attempt_is_caught_by_the_per_account_brake(secured):
    """Le frein par adresse ne voit rien d'une attaque répartie : une tentative
    par adresse ne déclenche aucun seuil, alors que le compte, lui, en subit des
    milliers. D'où un second frein, par compte."""
    for attempt in range(auth._MAX_ACCOUNT_FAILURES):
        response = _login(
            secured, password="au hasard", headers={"CF-Connecting-IP": f"203.0.113.{attempt}"}
        )
        assert response.status_code == 401, f"tentative {attempt}"

    from_a_fresh_address = _login(secured, headers={"CF-Connecting-IP": "198.51.100.42"})
    assert from_a_fresh_address.status_code == 429


def test_the_per_account_brake_does_not_touch_another_account(secured):
    for attempt in range(auth._MAX_ACCOUNT_FAILURES):
        _login(
            secured,
            user="jb",
            password="au hasard",
            headers={"CF-Connecting-IP": f"203.0.113.{attempt}"},
        )

    assert auth.account_retry_after("jb") > 0
    assert auth.account_retry_after("quelquun-dautre") == 0


# ------------------------------------------------- F2 : la graine de signature


def test_a_guessable_password_refuses_to_start(monkeypatch):
    """Une graine devinable se force hors ligne, et qui la trouve FORGE un
    cookie de session : il entre sans mot de passe, et sans jamais voir le
    second facteur (il n'est demandé qu'au formulaire)."""
    monkeypatch.setenv("APP_AUTH_USER", "jb")
    monkeypatch.setenv("APP_AUTH_PASSWORD", "lya")
    monkeypatch.delenv("APP_SESSION_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="APP_AUTH_PASSWORD"):
        auth.check_configuration()


def test_a_repetitive_password_is_refused_too(monkeypatch):
    """Trente-deux caractères identiques font une longueur, pas une entropie."""
    monkeypatch.setenv("APP_AUTH_USER", "jb")
    monkeypatch.setenv("APP_AUTH_PASSWORD", "a" * 40)
    monkeypatch.delenv("APP_SESSION_SECRET", raising=False)

    with pytest.raises(RuntimeError):
        auth.check_configuration()


def test_a_strong_password_starts(monkeypatch):
    monkeypatch.setenv("APP_AUTH_USER", "jb")
    monkeypatch.setenv("APP_AUTH_PASSWORD", PASSWORD)
    monkeypatch.delenv("APP_SESSION_SECRET", raising=False)

    auth.check_configuration()  # ne lève pas


def test_an_explicit_session_secret_is_what_gets_checked(monkeypatch):
    """Quand ``APP_SESSION_SECRET`` est posée, c'est ELLE qui signe : le mot de
    passe peut alors être ce qu'il veut, et c'est elle qui doit tenir."""
    monkeypatch.setenv("APP_AUTH_USER", "jb")
    monkeypatch.setenv("APP_AUTH_PASSWORD", "court")

    monkeypatch.setenv("APP_SESSION_SECRET", "clef-de-signature-longue-et-variee-42")
    auth.check_configuration()

    monkeypatch.setenv("APP_SESSION_SECRET", "trop-court")
    with pytest.raises(RuntimeError, match="APP_SESSION_SECRET"):
        auth.check_configuration()


def test_a_local_run_without_authentication_is_left_alone(monkeypatch):
    """Rien à signer, donc rien à exiger : l'usage local ne gagne pas de friction."""
    monkeypatch.delenv("APP_AUTH_USER", raising=False)
    monkeypatch.delenv("APP_AUTH_PASSWORD", raising=False)
    monkeypatch.delenv("APP_SESSION_SECRET", raising=False)

    assert config.session_secret_error() is None
    auth.check_configuration()


# --------------------------------------- F3 : la déconnexion révoque vraiment


def test_signing_out_revokes_the_cookie_server_side(secured):
    """La faille : la déconnexion se contentait d'oublier le cookie côté
    navigateur. Qui en détenait une copie (journal de proxy, sauvegarde de
    session, téléphone perdu) restait connecté jusqu'à son expiration."""
    _login(secured)
    stolen = secured.cookies[auth.SESSION_COOKIE]
    assert secured.get("/", headers=BROWSER).status_code == 200

    secured.post("/deconnexion", headers=BROWSER, follow_redirects=False)

    secured.cookies.clear()
    secured.cookies.set(auth.SESSION_COOKIE, stolen)  # le voleur rejoue sa copie
    assert secured.get("/", headers=BROWSER, follow_redirects=False).status_code == 303


def test_a_cookie_can_be_cut_without_changing_the_password(secured):
    """C'est l'intérêt de l'ancrage serveur : couper l'accès tout de suite, sans
    toucher au mot de passe ni attendre l'expiration."""
    from app import store

    _login(secured)
    assert secured.get("/", headers=BROWSER).status_code == 200

    store.revoke_sessions(secured.app.state.store)

    assert secured.get("/", headers=BROWSER, follow_redirects=False).status_code == 303


def test_a_token_from_before_the_server_anchor_is_refused(secured):
    """Un jeton d'ancien format n'a pas de génération à comparer : rien ne dit
    qu'il n'a pas été révoqué. Une reconnexion, une fois."""
    payload = f"{auth._b64(b'jb')}.{int(time.time())}"
    signature = hmac.new(auth._session_key(), payload.encode(), hashlib.sha256).digest()
    legacy = f"{payload}.{auth._b64(signature)}"

    assert auth.read_session(legacy) is None

    secured.cookies.set(auth.SESSION_COOKIE, legacy)
    assert secured.get("/", headers=BROWSER, follow_redirects=False).status_code == 303


def test_a_session_issued_under_another_generation_is_refused(secured):
    assert auth.read_session(auth.issue_session("jb", epoch=3), epoch=3) == "jb"
    assert auth.read_session(auth.issue_session("jb", epoch=3), epoch=4) is None


# ------------------------------------------------------ durée de vie du cookie


def test_a_session_lasts_a_day_not_a_week():
    """Même révocable, un cookie intercepté ne doit pas valoir une semaine
    d'accès aux comptes bancaires."""
    assert auth.session_max_age() == 24 * 3600


def test_the_session_duration_can_be_relaxed_on_purpose(monkeypatch):
    monkeypatch.setenv("APP_SESSION_MAX_AGE_HOURS", "168")
    assert auth.session_max_age() == 168 * 3600


@pytest.mark.parametrize("value", ["0", "-5", "99999", "n'importe quoi"])
def test_an_absurd_duration_never_locks_anyone_out_nor_lasts_forever(monkeypatch, value):
    monkeypatch.setenv("APP_SESSION_MAX_AGE_HOURS", value)
    assert 900 <= auth.session_max_age() <= 720 * 3600


def test_the_cookie_expiry_follows_the_configured_duration(secured, monkeypatch):
    monkeypatch.setenv("APP_SESSION_MAX_AGE_HOURS", "1")
    response = _login(secured)
    assert "max-age=3600" in response.headers["set-cookie"].lower()


# ------------------------------------------------------- en-têtes de sécurité


def test_pages_ask_the_browser_never_to_come_back_in_clear(client):
    """Sans HSTS, le premier aller d'une session peut partir en HTTP clair — et
    le cookie avec."""
    header = client.get("/").headers["strict-transport-security"]
    assert "max-age=63072000" in header and "includeSubDomains" in header


def test_the_content_security_policy_forbids_foreign_and_inline_scripts(client):
    policy = client.get("/").headers["content-security-policy"]

    assert "script-src 'self'" in policy
    assert "unsafe-inline" not in policy.split("style-src")[0], "pas pour les scripts"
    assert "frame-ancestors 'none'" in policy
    assert "form-action 'self'" in policy
    assert "base-uri 'self'" in policy


def test_no_page_carries_an_inline_script_that_the_policy_would_block(client):
    for path in ("/", "/comptes", "/patrimoine", "/analyse", "/abonnements"):
        body = client.get(path).text
        for fragment in body.split("<script")[1:]:
            assert "src=" in fragment.split(">")[0], f"script inline dans {path}"


# ------------------------------------------------------------- anti-CSRF


def test_a_post_from_another_site_is_refused(secured):
    """Le cookie est ``SameSite=Lax``, ce qui laisse passer les POST issus d'une
    navigation de premier niveau (la fenêtre « Lax+POST » de Chrome)."""
    response = secured.post(
        "/deconnexion",
        headers={**BROWSER, "Origin": "https://site.piege"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_a_post_from_our_own_pages_passes(secured):
    response = secured.post(
        "/deconnexion",
        headers={**BROWSER, "Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_a_request_without_an_origin_passes(secured):
    """curl, un script, les tests : rien de tout cela n'est déclenché par une
    page piégée. Un navigateur, lui, joint TOUJOURS Origin en cross-site."""
    assert secured.post("/deconnexion", headers=BROWSER, follow_redirects=False).status_code == 303


def test_a_navigation_is_never_blocked_by_the_origin_check(secured):
    _login(secured)
    response = secured.get("/", headers={**BROWSER, "Origin": "https://site.piege"})
    assert response.status_code == 200


def test_the_origin_check_covers_more_than_post(secured):
    """Le contrôle porte sur la méthode, pas sur une liste de routes : une
    future route PUT ou DELETE est couverte d'avance."""
    response = secured.request(
        "DELETE", "/comptes", headers={**BROWSER, "Origin": "https://site.piege"}
    )
    assert response.status_code == 403


def test_the_login_form_itself_is_protected(secured):
    """Login CSRF : sans ce contrôle, un site tiers pouvait ouvrir une session
    dans le navigateur de la victime, et l'y faire travailler."""
    response = secured.post(
        "/connexion",
        data={"username": "jb", "password": PASSWORD},
        headers={**BROWSER, "Origin": "https://site.piege"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert auth.SESSION_COOKIE not in response.cookies
