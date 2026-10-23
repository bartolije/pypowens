"""Second facteur : ce qu'il ferme, et ce qu'il laisse volontairement ouvert.

Un MFA ne vaut que par ses portes de service. Deux comptent ici : l'en-tête
``Authorization: Basic`` (dont un navigateur comme un script disposent) et le
rejeu d'un code intercepté. Les deux sont testées, faute de quoi ce module
serait un champ de formulaire décoratif.
"""

from __future__ import annotations

import base64
import time

import pytest

from app import auth, totp

BROWSER = {"Accept": "text/html,application/xhtml+xml", "Sec-Fetch-Mode": "navigate"}

SECRET = totp.generate_secret()
# Assez longs et variés pour le plancher d'entropie (cf. config.session_secret_error).
PASSWORD = "mot-de-passe-de-test-tres-long-42"
TOKEN = "jeton-de-test-suffisamment-long-42"


def _code(*, offset: int = 0) -> str:
    """Le code attendu à l'instant présent (l'horloge est figée par conftest)."""
    return totp._hotp(SECRET, int(time.time()) // totp.PERIOD + offset)


@pytest.fixture
def secured(client, monkeypatch):
    """App avec identifiants et second facteur configurés, sans session ouverte."""
    monkeypatch.setenv("APP_AUTH_USER", "jb")
    monkeypatch.setenv("APP_AUTH_PASSWORD", PASSWORD)
    monkeypatch.setenv("APP_TOTP_SECRET", SECRET)
    auth.reset_failures()
    client.cookies.clear()
    yield client
    client.cookies.clear()
    auth.reset_failures()


def _password_step(client, *, password=PASSWORD, user="jb", suite=None):
    """Première page : identifiant et mot de passe."""
    data = {"username": user, "password": password}
    if suite is not None:
        data["suite"] = suite
    return client.post("/connexion", data=data, headers=BROWSER, follow_redirects=False)


def _code_step(client, code):
    """Seconde page : le code seul."""
    return client.post(
        "/connexion/code", data={"code": code}, headers=BROWSER, follow_redirects=False
    )


def _login(client, *, password=PASSWORD, code="", user="jb"):
    """Le parcours complet ; renvoie la réponse de la page du code."""
    _password_step(client, password=password, user=user)
    return _code_step(client, code)


# ------------------------------------------------------------------ le parcours


def test_the_password_page_never_shows_the_code_field(secured, monkeypatch):
    """ProtonPass classe un formulaire qui mêle mot de passe et code comme « pas
    une connexion » et n'y remplit que le code : la première page doit rester
    une connexion ordinaire, second facteur ou non."""
    body = secured.get("/connexion", headers=BROWSER).text
    assert 'name="code"' not in body
    assert "one-time-code" not in body
    assert 'autocomplete="username"' in body
    assert 'autocomplete="current-password"' in body


def test_the_password_leads_to_the_code_page(secured):
    response = _password_step(secured)

    assert response.status_code == 303
    assert response.headers["location"] == auth.LOGIN_CODE_PATH
    assert auth.CHALLENGE_COOKIE in response.cookies
    assert auth.SESSION_COOKIE not in response.cookies, "pas encore de session"
    cookie = response.headers["set-cookie"].lower()
    assert "path=/connexion" in cookie and "httponly" in cookie

    body = secured.get(auth.LOGIN_CODE_PATH, headers=BROWSER).text
    assert 'name="code"' in body
    assert 'autocomplete="one-time-code"' in body, "sinon ProtonPass ne le remplit pas"
    assert 'name="password"' not in body


def test_without_a_second_factor_the_login_stays_on_one_page(secured, monkeypatch):
    monkeypatch.delenv("APP_TOTP_SECRET")

    response = _password_step(secured)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert auth.SESSION_COOKIE in response.cookies
    assert auth.CHALLENGE_COOKIE not in response.cookies


def test_both_factors_open_the_session(secured):
    response = _login(secured, code=_code())

    assert response.status_code == 303
    assert auth.SESSION_COOKIE in response.cookies
    assert secured.get("/", headers=BROWSER).status_code == 200


def test_the_requested_page_survives_the_code_step(secured):
    """La page demandée avant la connexion suit le défi côté serveur."""
    suite = base64.urlsafe_b64encode(b"/transactions").decode()
    _password_step(secured, suite=suite)

    response = _code_step(secured, _code())

    assert response.status_code == 303
    assert response.headers["location"] == "/transactions"


def test_the_password_alone_is_not_enough(secured):
    _password_step(secured)

    assert secured.get("/", headers=BROWSER, follow_redirects=False).status_code == 303
    response = _code_step(secured, "")
    assert response.status_code == 401
    assert auth.SESSION_COOKIE not in response.cookies


def test_the_code_alone_is_not_enough(secured):
    response = _login(secured, password="au hasard", code=_code())

    assert response.status_code == 401
    assert auth.SESSION_COOKIE not in response.cookies


def test_the_password_page_never_says_whether_the_password_was_right(secured):
    """Juste ou faux, même réponse : la première page ne trahit rien."""
    right = _password_step(secured)
    secured.cookies.clear()
    wrong = _password_step(secured, password="au hasard")

    assert right.status_code == wrong.status_code == 303
    assert right.headers["location"] == wrong.headers["location"] == auth.LOGIN_CODE_PATH
    assert auth.CHALLENGE_COOKIE in wrong.cookies
    assert secured.get(auth.LOGIN_CODE_PATH, headers=BROWSER).status_code == 200


def test_the_error_message_never_says_which_factor_failed(secured):
    """Distinguer les deux dirait à l'attaquant qu'il ne lui reste que six
    chiffres à trouver."""
    wrong_password = _login(secured, password="au hasard", code=_code()).text
    wrong_code = _login(secured, code="000000").text

    assert "Identifiants ou code incorrects." in wrong_password
    assert "Identifiants ou code incorrects." in wrong_code


def test_a_neighbouring_code_still_works(secured):
    """Tolérance d'un pas : sinon un code juste est refusé au changement de pas."""
    assert _login(secured, code=_code(offset=-1)).status_code == 303


def test_a_challenge_serves_a_single_code_attempt(secured):
    """Un mot de passe = un essai de code : le défi ne se rejoue pas pour
    continuer à deviner les six chiffres."""
    challenge = _password_step(secured).cookies[auth.CHALLENGE_COOKIE]
    assert _code_step(secured, "000000").status_code == 401

    secured.cookies.clear()
    secured.cookies.set(auth.CHALLENGE_COOKIE, challenge, path="/connexion")
    replayed = _code_step(secured, _code())

    assert replayed.status_code == 401
    assert "Connexion expirée" in replayed.text
    assert auth.SESSION_COOKIE not in replayed.cookies


def test_the_code_page_requires_a_challenge(secured):
    page = secured.get(auth.LOGIN_CODE_PATH, headers=BROWSER, follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == auth.LOGIN_PATH

    response = _code_step(secured, _code())
    assert response.status_code == 401
    assert auth.SESSION_COOKIE not in response.cookies


def test_an_expired_challenge_is_refused(secured, monkeypatch):
    monkeypatch.setattr(auth._challenges, "lifetime", -1)
    _password_step(secured)

    page = secured.get(auth.LOGIN_CODE_PATH, headers=BROWSER, follow_redirects=False)
    assert page.status_code == 303
    response = _code_step(secured, _code())
    assert response.status_code == 401
    assert "Connexion expirée" in response.text


def test_pending_challenges_are_bounded(monkeypatch):
    monkeypatch.setattr(auth, "_MAX_TRACKED", 3)
    challenges = auth._Challenges()
    ids = [challenges.issue(True, "") for _ in range(5)]

    assert len(challenges._pending) == 3
    assert not challenges.pending(ids[0]), "le plus ancien part le premier"
    assert challenges.consume(ids[-1]) == (True, "")
    assert challenges.consume(ids[-1]) is None


# --------------------------------------------------------------------- anti-rejeu


def test_the_same_code_cannot_be_used_twice(secured):
    """Un code vaut trente secondes : intercepté, il ouvrirait une seconde
    session dans sa fenêtre. Le pas consommé est donc mémorisé."""
    code = _code()
    assert _login(secured, code=code).status_code == 303

    secured.cookies.clear()
    replayed = _login(secured, code=code)

    assert replayed.status_code == 401
    assert auth.SESSION_COOKIE not in replayed.cookies


def test_an_earlier_step_cannot_be_used_after_a_later_one(secured):
    assert _login(secured, code=_code()).status_code == 303
    secured.cookies.clear()

    assert _login(secured, code=_code(offset=-1)).status_code == 401


def test_the_replay_guard_survives_a_restart(secured):
    """Le compteur vit en base, pas en mémoire : redémarrer ne doit pas rouvrir
    la fenêtre d'un code déjà utilisé."""
    from app import store

    code = _code()
    assert _login(secured, code=code).status_code == 303

    conn = secured.app.state.store
    counter = conn.execute(
        "SELECT value FROM app_meta WHERE key = ?", (store.TOTP_COUNTER_KEY,)
    ).fetchone()
    assert counter is not None and int(counter["value"]) == int(time.time()) // totp.PERIOD


# -------------------------------------------------------------------- fail-closed


def test_a_malformed_secret_refuses_every_login_instead_of_raising(secured, monkeypatch):
    monkeypatch.setenv("APP_TOTP_SECRET", "pas du base32 !")

    response = _login(secured, code="123456")

    assert response.status_code == 401, "un secret illisible refuse, il ne casse pas"
    assert auth.SESSION_COOKIE not in response.cookies


def test_a_malformed_secret_is_announced_at_startup(monkeypatch):
    monkeypatch.setenv("APP_TOTP_SECRET", "pas du base32 !")
    assert any("APP_TOTP_SECRET" in w for w in auth.startup_warnings())


def test_an_mfa_without_api_token_is_announced_at_startup(monkeypatch):
    monkeypatch.setenv("APP_TOTP_SECRET", SECRET)
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    assert any("APP_API_TOKEN" in w for w in auth.startup_warnings())


def test_a_login_without_a_database_refuses_rather_than_trusting_the_code(secured):
    """Sans base, le rejeu est invérifiable : un MFA qu'on ne peut pas contrôler
    n'est pas un MFA."""
    assert auth.verify_totp(None, _code()) is False


# -------------------------------------------------------- la porte des scripts


def test_the_password_stops_opening_the_non_interactive_door(secured):
    """Sinon un simple ``curl -u`` contournerait le second facteur, qui n'est
    demandé qu'au formulaire."""
    response = secured.get("/sauvegarde.db", auth=("jb", PASSWORD))

    assert response.status_code == 401
    assert "APP_API_TOKEN" in response.text, "le script doit savoir quoi faire"


def test_the_api_token_opens_it_as_a_bearer(secured, monkeypatch):
    monkeypatch.setenv("APP_API_TOKEN", TOKEN)

    response = secured.get("/sauvegarde.db", headers={"Authorization": f"Bearer {TOKEN}"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.sqlite3"


def test_the_api_token_also_works_in_place_of_the_password(secured, monkeypatch):
    """``curl -u nom:jeton`` : les scripts et fichiers de conf existants tiennent."""
    monkeypatch.setenv("APP_API_TOKEN", TOKEN)

    assert secured.get("/sauvegarde.db", auth=("jb", TOKEN)).status_code == 200
    assert secured.get("/", auth=("jb", TOKEN)).status_code == 200


def test_a_wrong_token_opens_nothing(secured, monkeypatch):
    monkeypatch.setenv("APP_API_TOKEN", TOKEN)

    refused = secured.get("/sauvegarde.db", headers={"Authorization": "Bearer autre"})
    assert refused.status_code == 401


def test_a_short_token_is_ignored_rather_than_trusted(secured, monkeypatch):
    """Un jeton court se force hors ligne, et il n'a pas de frein propre."""
    monkeypatch.setenv("APP_API_TOKEN", "court")

    refused = secured.get("/sauvegarde.db", headers={"Authorization": "Bearer court"})
    assert refused.status_code == 401
    assert any("APP_API_TOKEN" in w for w in auth.startup_warnings())


def test_the_token_never_opens_a_session_cookie(secured, monkeypatch):
    """Un jeton de script ne doit pas laisser derrière lui une session de
    navigateur, qui elle survivrait à sa révocation."""
    monkeypatch.setenv("APP_API_TOKEN", TOKEN)

    response = secured.get("/", auth=("jb", TOKEN))

    assert auth.SESSION_COOKIE not in response.cookies


def test_the_token_works_without_any_second_factor_too(client, monkeypatch):
    """Le jeton est utile seul : révocable sans toucher au mot de passe."""
    monkeypatch.setenv("APP_AUTH_USER", "jb")
    monkeypatch.setenv("APP_AUTH_PASSWORD", PASSWORD)
    monkeypatch.delenv("APP_TOTP_SECRET", raising=False)
    monkeypatch.setenv("APP_API_TOKEN", TOKEN)
    auth.reset_failures()

    assert client.get("/", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert client.get("/", auth=("jb", PASSWORD)).status_code == 200, "toujours accepté sans MFA"


# ----------------------------------------------------------------------- frein


def test_guessing_the_six_digits_is_rate_limited_like_a_password(secured):
    """Six chiffres, c'est un million de possibilités : sans frein, une
    demi-heure de requêtes suffirait. Chaque essai de code coûte un passage
    par la page du mot de passe, qui est freinée."""
    for _ in range(auth._MAX_FAILURES):
        assert _login(secured, code="000000").status_code == 401

    blocked = _password_step(secured)
    assert blocked.status_code == 429
    assert auth.CHALLENGE_COOKIE not in blocked.cookies
    # Aucun défi émis : même le bon code ne mène nulle part
    response = _code_step(secured, _code())
    assert response.status_code == 401
    assert auth.SESSION_COOKIE not in response.cookies


def test_the_password_page_alone_is_rate_limited(secured):
    """Sans jamais aller au code, les passages s'épuisent quand même : le frein
    ne peut donc pas servir à trier les bons mots de passe des mauvais."""
    for _ in range(auth._MAX_FAILURES):
        assert _password_step(secured).status_code == 303

    assert _password_step(secured).status_code == 429


def test_a_complete_login_resets_the_brake(secured):
    for _ in range(auth._MAX_FAILURES - 1):
        _login(secured, password="au hasard", code="000000")
    assert _login(secured, code=_code()).status_code == 303

    secured.cookies.clear()
    for _ in range(auth._MAX_FAILURES):
        response = _password_step(secured, password="au hasard")
    assert response.status_code == 303, "compteur reparti de zéro après la connexion"
