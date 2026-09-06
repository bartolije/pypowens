"""Bootstrap du token et persistance locale (.powens_state.json).

C'est le code qui manipule les secrets et qui décide de créer — ou non — un
utilisateur Powens : le scénario le plus coûteux de l'app (un état pris pour
absent orpheline toutes les connexions bancaires) se joue ici.
"""

from __future__ import annotations

import json
import stat

import pytest

from app import state as state_mod
from app.config import Settings
from app.state import StateFileError, _load_state, _save_state, bootstrap_client, try_renew
from pypowens import PowensClient
from pypowens.models import AuthToken


def _settings(monkeypatch, tmp_path, **overrides) -> Settings:
    monkeypatch.setenv("APP_STATE_PATH", str(tmp_path / "state.json"))
    defaults = dict(
        domain="demo-sandbox",
        client_id="cid",
        client_secret="secret",
        access_token=None,
    )
    defaults.update(overrides)
    return Settings(**defaults)


# ------------------------------------------------------------------ state file


def test_load_state_missing_file_is_empty(tmp_path):
    assert _load_state(tmp_path / "absent.json") == {}


def test_load_state_corrupt_file_raises(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"id_user": 42, "access_to')  # écriture interrompue
    with pytest.raises(StateFileError):
        _load_state(path)


def test_load_state_non_object_raises(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('"just-a-string"')
    with pytest.raises(StateFileError):
        _load_state(path)


def test_save_state_roundtrip_and_permissions(tmp_path):
    path = tmp_path / "state.json"
    _save_state(path, {"id_user": 42, "access_token": "tok"})
    assert json.loads(path.read_text()) == {"id_user": 42, "access_token": "tok"}
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600  # le token ne doit jamais être lisible par d'autres comptes
    assert not list(tmp_path.glob("*.tmp"))  # pas de temporaire orphelin

    _save_state(path, {"id_user": 43, "access_token": "tok2"})  # écrasement
    assert json.loads(path.read_text())["id_user"] == 43


# ------------------------------------------------------------------- bootstrap


async def test_bootstrap_env_token_wins(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path, access_token="env-tok")
    client = await bootstrap_client(settings)
    try:
        assert client.access_token == "env-tok"
    finally:
        await client.aclose()


async def test_bootstrap_reads_persisted_state(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    _save_state(settings.state_path, {"id_user": 7, "access_token": "persisted-tok"})
    client = await bootstrap_client(settings)
    try:
        assert client.access_token == "persisted-tok"
    finally:
        await client.aclose()


async def test_bootstrap_creates_user_and_persists(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)

    async def fake_create_user(self):
        self.access_token = "fresh-tok"
        return AuthToken.from_api({"auth_token": "fresh-tok", "id_user": 99})

    monkeypatch.setattr(PowensClient, "create_user", fake_create_user)
    client = await bootstrap_client(settings)
    try:
        assert client.access_token == "fresh-tok"
        persisted = json.loads(settings.state_path.read_text())
        assert persisted == {"id_user": 99, "access_token": "fresh-tok"}
    finally:
        await client.aclose()


async def test_bootstrap_refuses_corrupt_state(monkeypatch, tmp_path):
    """Un état illisible doit ARRÊTER le démarrage, pas créer un nouvel utilisateur."""
    settings = _settings(monkeypatch, tmp_path)
    settings.state_path.write_text("{corrupt")

    created = False

    async def fake_create_user(self):
        nonlocal created
        created = True
        return AuthToken.from_api({"auth_token": "x", "id_user": 1})

    monkeypatch.setattr(PowensClient, "create_user", fake_create_user)
    with pytest.raises(StateFileError):
        await bootstrap_client(settings)
    assert created is False


# ------------------------------------------------------------------- try_renew


async def test_try_renew_without_credentials(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path, client_id=None, client_secret=None)
    client = PowensClient("demo-sandbox")
    try:
        assert await try_renew(client, settings) is False
    finally:
        await client.aclose()


async def test_try_renew_with_corrupt_state_returns_false(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    settings.state_path.write_text("{corrupt")
    client = PowensClient("demo-sandbox", client_id="cid", client_secret="secret")
    try:
        assert await try_renew(client, settings) is False
    finally:
        await client.aclose()


async def test_try_renew_success_updates_client_and_state(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    _save_state(settings.state_path, {"id_user": 7, "access_token": "old-tok"})

    async def fake_renew(self, id_user, *, revoke_previous=False):
        assert id_user == 7
        self.access_token = "new-tok"
        return AuthToken.from_api({"auth_token": "new-tok", "id_user": 7})

    monkeypatch.setattr(PowensClient, "renew_token", fake_renew)
    client = PowensClient("demo-sandbox", client_id="cid", client_secret="secret")
    try:
        assert await try_renew(client, settings) is True
        assert client.access_token == "new-tok"
        assert json.loads(settings.state_path.read_text())["access_token"] == "new-tok"
    finally:
        await client.aclose()


def test_state_module_has_no_silent_valueerror_swallow():
    """Garde-fou de non-régression : ValueError ne doit plus être avalée au load."""
    import inspect

    src = inspect.getsource(state_mod._load_state)
    assert "StateFileError" in src
