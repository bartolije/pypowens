"""Robustness: retries, pagination guard, single-window cache, error pages,
network-exposure guard."""

from __future__ import annotations

import httpx
import pytest
import respx

from pypowens import PowensAPIError, PowensClient, PowensRateLimitError

BASE = "https://test-sandbox.biapi.pro/2.0"


def _client(**kwargs) -> PowensClient:
    kwargs.setdefault("access_token", "tok")
    kwargs.setdefault("backoff", 0)  # keep tests instant
    return PowensClient("test-sandbox", **kwargs)


# ------------------------------------------------------------------- retries

@respx.mock
async def test_retries_then_succeeds_on_429():
    route = respx.get(f"{BASE}/users/me").mock(
        side_effect=[
            httpx.Response(429, json={"code": "rateLimit"}),
            httpx.Response(200, json={"id": 7}),
        ]
    )
    async with _client() as powens:
        user = await powens.get_current_user()
    assert user.id == 7
    assert route.call_count == 2


@respx.mock
async def test_gives_up_after_max_retries():
    route = respx.get(f"{BASE}/users/me").mock(
        return_value=httpx.Response(503, json={"code": "unavailable"})
    )
    async with _client(max_retries=2) as powens:
        with pytest.raises(PowensAPIError) as err:
            await powens.get_current_user()
    assert err.value.status_code == 503
    assert route.call_count == 3  # first attempt + 2 retries


@respx.mock
async def test_no_retry_on_client_error():
    route = respx.get(f"{BASE}/users/me").mock(
        return_value=httpx.Response(400, json={"code": "badRequest"})
    )
    async with _client() as powens:
        with pytest.raises(PowensAPIError):
            await powens.get_current_user()
    assert route.call_count == 1


@respx.mock
async def test_retry_after_header_is_exposed():
    respx.get(f"{BASE}/users/me").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "12"}, json={})
    )
    async with _client(max_retries=0) as powens:
        with pytest.raises(PowensRateLimitError) as err:
            await powens.get_current_user()
    assert err.value.retry_after == 12


@respx.mock
async def test_retries_network_error():
    route = respx.get(f"{BASE}/users/me").mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json={"id": 1})]
    )
    async with _client() as powens:
        assert (await powens.get_current_user()).id == 1
    assert route.call_count == 2


# ---------------------------------------------------------------- pagination

@respx.mock
async def test_pagination_stops_on_repeated_next_href():
    """A server echoing the same ``next`` link must not loop forever."""
    page = {
        "transactions": [{"id": 1, "value": "-1.00", "date": "2026-01-01"}],
        "_links": {"next": {"href": "/2.0/users/me/transactions?offset=1"}},
    }
    first = respx.get(f"{BASE}/users/me/transactions").mock(
        return_value=httpx.Response(200, json=page)
    )
    async with _client() as powens:
        txns = await powens.list_transactions()
    # 2 calls: the initial page, then the repeated href once — then it stops.
    assert first.call_count == 2
    assert len(txns) == 2


# --------------------------------------------------------------------- cache

async def test_history_fetched_once_for_widest_window(fake_client):
    """Asking 8 then 24 months used to download the whole history twice."""
    import app.data as data

    calls = {"n": 0}
    original = fake_client.iter_transactions

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    fake_client.iter_transactions = counting
    data.clear_cache()
    try:
        wide = await data.load_transactions(fake_client, months=24)
        narrow = await data.load_transactions(fake_client, months=8)
        await data.load_internal_ids(fake_client, months=24)
        assert calls["n"] == 1, "history must be fetched once"
        assert len(narrow) < len(wide), "narrower window must filter in memory"
        # A single history entry, plus the internal-transfer set: no per-window entry.
        assert set(data._cache) == {"transactions", "internal"}
    finally:
        data.clear_cache()


async def test_narrow_first_then_wide_refetches(fake_client):
    import app.data as data

    data.clear_cache()
    try:
        narrow = await data.load_transactions(fake_client, months=6)
        wide = await data.load_transactions(fake_client, months=24)
        assert len(wide) > len(narrow)
    finally:
        data.clear_cache()


# ------------------------------------------------------------- error handling

def test_auth_error_renders_error_page(client, monkeypatch, fake_client):
    """A dead token that cannot be renewed yields a helpful page, not a traceback."""
    from pypowens import PowensAuthError

    async def boom(*args, **kwargs):
        raise PowensAuthError(401, code="invalidToken", message="The token is invalid")

    monkeypatch.setattr(fake_client, "list_accounts", boom)
    import app.data

    app.data.clear_cache()
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 401
    assert "Accès Powens refusé" in response.text
    assert "HTTP 401" in response.text  # technical detail is surfaced


def test_auth_error_replays_after_successful_renew(client, monkeypatch, fake_client):
    """After a successful renewal the page is replayed once (303 + _retried flag)."""
    import app.data
    import app.main
    from pypowens import PowensAuthError

    calls = {"n": 0}
    original = fake_client.list_accounts

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PowensAuthError(401, code="invalidToken")
        return await original(*args, **kwargs)

    async def _renew(_client, _settings):
        return True

    monkeypatch.setattr(fake_client, "list_accounts", flaky)
    monkeypatch.setattr(app.main, "try_renew", _renew)
    app.data.clear_cache()

    redirect = client.get("/patrimoine", follow_redirects=False)
    assert redirect.status_code == 303
    assert "_retried=1" in redirect.headers["location"]

    replayed = client.get(redirect.headers["location"])
    assert replayed.status_code == 200
    assert "Patrimoine" in replayed.text


def test_rate_limit_renders_error_page(client, monkeypatch, fake_client):
    async def boom(*args, **kwargs):
        raise PowensRateLimitError(429, code="rateLimit", retry_after=30)

    monkeypatch.setattr(fake_client, "list_accounts", boom)
    import app.data

    app.data.clear_cache()
    response = client.get("/")
    assert response.status_code == 429
    assert "limite les appels" in response.text
    assert "30 s" in response.text


# ------------------------------------------------------------ exposure guard

def test_non_loopback_host_is_refused(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("POWENS_DOMAIN", "test-sandbox")
    monkeypatch.setenv("APP_HOST", "0.0.0.0")
    monkeypatch.delenv("APP_ALLOW_REMOTE", raising=False)
    with pytest.raises(RuntimeError, match="APP_ALLOW_REMOTE"):
        get_settings()


def test_non_loopback_host_allowed_with_optin(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("POWENS_DOMAIN", "test-sandbox")
    monkeypatch.setenv("APP_HOST", "0.0.0.0")
    monkeypatch.setenv("APP_ALLOW_REMOTE", "1")
    assert get_settings().host == "0.0.0.0"
