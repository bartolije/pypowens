"""Tests for :class:`pypowens.PowensClient` using respx to mock httpx."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import respx

from pypowens import PowensAPIError, PowensAuthError, PowensClient
from pypowens.client import _resolve_host

BASE = "https://myapp-sandbox.biapi.pro/2.0"


def test_resolve_host_variants():
    assert _resolve_host("myapp-sandbox") == "https://myapp-sandbox.biapi.pro"
    assert _resolve_host("myapp-sandbox.biapi.pro") == "https://myapp-sandbox.biapi.pro"
    assert (
        _resolve_host("https://myapp-sandbox.biapi.pro/2.0/") == "https://myapp-sandbox.biapi.pro"
    )


@respx.mock
async def test_create_user_stores_token():
    respx.post(f"{BASE}/auth/init").mock(
        return_value=httpx.Response(
            200, json={"auth_token": "TOK", "type": "permanent", "id_user": 42, "expires_in": None}
        )
    )
    async with PowensClient("myapp-sandbox", client_id="cid", client_secret="secret") as p:
        token = await p.create_user()
        assert token.access_token == "TOK"
        assert token.id_user == 42
        assert p.access_token == "TOK"


@respx.mock
async def test_list_connectors():
    respx.get(f"{BASE}/connectors").mock(
        return_value=httpx.Response(
            200,
            json={
                "connectors": [
                    {"id": 1, "name": "Bank A", "slug": "bank-a", "capabilities": ["bank"]}
                ]
            },
        )
    )
    async with PowensClient("myapp-sandbox") as p:
        connectors = await p.list_connectors(country_codes="fr")
        assert len(connectors) == 1
        assert connectors[0].name == "Bank A"
        assert "bank" in connectors[0].capabilities


@respx.mock
async def test_list_accounts_parses_balances():
    respx.get(f"{BASE}/users/me/accounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "accounts": [
                    {"id": 10, "name": "Checking", "balance": "1234.56", "currency": {"id": "EUR"}}
                ],
                "balances": {"EUR": "1234.56"},
            },
        )
    )
    async with PowensClient("myapp-sandbox", access_token="TOK") as p:
        result = await p.list_accounts()
        assert result.accounts[0].balance == Decimal("1234.56")
        assert result.accounts[0].currency == "EUR"
        assert result.balances["EUR"] == Decimal("1234.56")


@respx.mock
async def test_list_transactions_follows_pagination():
    page1 = {
        "transactions": [{"id": 1, "value": "-10.00", "wording": "Coffee"}],
        "_links": {"next": {"href": "/2.0/users/me/transactions?offset=1&limit=1"}},
    }
    page2 = {
        "transactions": [{"id": 2, "value": "-20.00", "wording": "Lunch"}],
        "_links": {},
    }
    route = respx.get(f"{BASE}/users/me/transactions")
    route.side_effect = [httpx.Response(200, json=page1), httpx.Response(200, json=page2)]

    async with PowensClient("myapp-sandbox", access_token="TOK") as p:
        txns = await p.list_transactions(limit=1)
        assert [t.id for t in txns] == [1, 2]
        assert txns[0].value == Decimal("-10.00")


@respx.mock
async def test_max_transactions_caps_results():
    page1 = {
        "transactions": [{"id": 1}, {"id": 2}],
        "_links": {"next": {"href": "/2.0/users/me/transactions?offset=2"}},
    }
    respx.get(f"{BASE}/users/me/transactions").mock(return_value=httpx.Response(200, json=page1))
    async with PowensClient("myapp-sandbox", access_token="TOK") as p:
        txns = await p.list_transactions(max_transactions=1)
        assert len(txns) == 1


@respx.mock
async def test_api_error_is_raised():
    respx.get(f"{BASE}/users/me").mock(
        return_value=httpx.Response(400, json={"code": "badRequest", "message": "Nope"})
    )
    async with PowensClient("myapp-sandbox", access_token="TOK") as p:
        with pytest.raises(PowensAPIError) as excinfo:
            await p.get_current_user()
        assert excinfo.value.status_code == 400
        assert excinfo.value.code == "badRequest"


@respx.mock
async def test_api_error_uses_description_field():
    # Powens sometimes returns the human message under "description".
    respx.get(f"{BASE}/users/me/transactions").mock(
        return_value=httpx.Response(
            400, json={"code": "noAccount", "description": "At least one bank account is required."}
        )
    )
    async with PowensClient("myapp-sandbox", access_token="TOK") as p:
        with pytest.raises(PowensAPIError) as excinfo:
            await p.list_transactions()
        assert excinfo.value.code == "noAccount"
        assert excinfo.value.message == "At least one bank account is required."


async def test_missing_token_raises_auth_error():
    async with PowensClient("myapp-sandbox") as p:
        with pytest.raises(PowensAuthError):
            await p.get_current_user()


@respx.mock
async def test_get_indicators_handles_null():
    respx.get(f"{BASE}/users/me/indicators").mock(
        return_value=httpx.Response(200, json={"id_user": 5, "indicators": None})
    )
    async with PowensClient("myapp-sandbox", access_token="TOK") as p:
        ind = await p.get_indicators()
        assert ind.id_user == 5
        assert ind.available is False


@respx.mock
async def test_list_categories():
    respx.get(f"{BASE}/banks/categories").mock(
        return_value=httpx.Response(200, json={"bank_category": [{"id": 3, "name": "Insurance"}]})
    )
    async with PowensClient("myapp-sandbox", access_token="TOK") as p:
        cats = await p.list_categories()
        assert cats[0].name == "Insurance"


def test_build_webview_url():
    client = PowensClient("myapp-sandbox", client_id="cid")
    url = client.build_webview_url("http://127.0.0.1:8000/callback", "CODE123")
    assert url.startswith("https://webview.powens.com/en/connect?")
    assert "domain=myapp-sandbox.biapi.pro" in url
    assert "client_id=cid" in url
    assert "code=CODE123" in url


def test_webview_url_always_carries_a_language_segment():
    """``/connect`` without it is answered by CloudFront with a 503, not the Webview."""
    client = PowensClient("myapp-sandbox", client_id="cid")
    assert "/fr/connect?" in client.build_webview_url("http://x/cb", "C", lang="fr")
    assert "/en/connect?" in client.build_webview_url("http://x/cb", "C", lang="")
    assert "/fr/connect?" in client.build_webview_url("http://x/cb", "C", lang="/fr/")


def test_webview_url_can_preselect_connectors():
    client = PowensClient("myapp-sandbox", client_id="cid")
    url = client.build_webview_url("http://x/cb", "C", connector_ids=[2663, 2666])
    assert "connector_ids=2663%2C2666" in url


def test_from_env_treats_empty_values_as_unset(monkeypatch):
    monkeypatch.setenv("POWENS_DOMAIN", "myapp-sandbox")
    monkeypatch.setenv("POWENS_CLIENT_ID", "cid")
    monkeypatch.setenv("POWENS_CLIENT_SECRET", "secret")
    monkeypatch.setenv("POWENS_ACCESS_TOKEN", "")  # empty must not become ""
    client = PowensClient.from_env()
    assert client.access_token is None
    assert client.client_id == "cid"


# ------------------------------------------------- historique de valorisation


@respx.mock
async def test_list_investment_history_returns_dated_unit_values():
    respx.get(f"{BASE}/users/me/investments/35/history").mock(
        return_value=httpx.Response(
            200,
            json={
                "investmentvalues": [
                    {"id": 91, "id_investment": 35, "vdate": "2026-07-07", "unitvalue": 178.9},
                    {"id": 32, "id_investment": 35, "vdate": "2026-07-05", "unitvalue": 180.3},
                ],
                "total": 2,
            },
        )
    )
    async with PowensClient("myapp-sandbox", access_token="TOK") as p:
        values = await p.list_investment_history(35)
    # Renvoyé le plus ancien d'abord : une série de prix se lit dans le sens du temps.
    assert [str(v.vdate) for v in values] == ["2026-07-05", "2026-07-07"]
    assert values[0].unit_value == Decimal("180.3")
    assert values[0].id_investment == 35


@respx.mock
async def test_list_investment_history_can_be_scoped_to_an_account():
    route = respx.get(f"{BASE}/users/me/accounts/9/investments/35/history").mock(
        return_value=httpx.Response(200, json={"investmentvalues": [], "total": 0})
    )
    async with PowensClient("myapp-sandbox", access_token="TOK") as p:
        assert await p.list_investment_history(35, account_id=9, min_date="2026-07-01") == []
    assert route.called
    assert "min_date=2026-07-01" in str(route.calls[0].request.url)


@respx.mock
async def test_investment_history_without_a_date_sorts_last():
    """Une valeur sans ``vdate`` ne doit pas casser le tri de la série."""
    respx.get(f"{BASE}/users/me/investments/1/history").mock(
        return_value=httpx.Response(
            200,
            json={
                "investmentvalues": [
                    {"id": 2, "id_investment": 1, "vdate": None, "unitvalue": 1.0},
                    {"id": 1, "id_investment": 1, "vdate": "2026-07-05", "unitvalue": 2.0},
                ]
            },
        )
    )
    async with PowensClient("myapp-sandbox", access_token="TOK") as p:
        values = await p.list_investment_history(1)
    assert [v.id for v in values] == [1, 2]


@respx.mock
async def test_transactions_can_be_filtered_by_account():
    """account_id doit interroger l'endpoint par compte, pas filtrer en mémoire."""
    route = respx.get(f"{BASE}/users/me/accounts/42/transactions").mock(
        return_value=httpx.Response(
            200, json={"transactions": [{"id": 1, "value": "-5.00", "date": "2026-01-02"}]}
        )
    )
    async with PowensClient("myapp-sandbox", access_token="tok") as powens:
        txns = await powens.list_transactions(account_id=42)
    assert route.call_count == 1
    assert [t.id for t in txns] == [1]


@respx.mock
async def test_max_transactions_caps_the_page_size():
    """Demander 10 transactions ne doit pas télécharger une page de 1000."""
    captured = {}

    def record(request):
        captured.update(dict(request.url.params))
        return httpx.Response(
            200, json={"transactions": [{"id": 1, "value": "-1.00", "date": "2026-01-01"}]}
        )

    respx.get(f"{BASE}/users/me/transactions").mock(side_effect=record)
    async with PowensClient("myapp-sandbox", access_token="tok") as powens:
        await powens.list_transactions(max_transactions=10)
    assert captured.get("limit") == "10"


@respx.mock
async def test_update_account_reenables_a_disabled_account():
    """PUT /users/me/accounts/{id} avec disabled=False : le seul chemin pour
    réintégrer un compte que Powens a recréé désactivé après une panne."""
    captured = {}

    def record(request):
        import json as _json

        captured["body"] = _json.loads(request.content)
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"id": 28, "name": "PRET", "disabled": None})

    route = respx.put(f"{BASE}/users/me/accounts/28").mock(side_effect=record)
    async with PowensClient("myapp-sandbox", access_token="tok") as powens:
        account = await powens.update_account(28, disabled=False)
    assert route.call_count == 1
    assert captured["body"] == {"disabled": False}
    # Sans ?all, un compte désactivé est inadressable : PUT répond 404.
    assert "all" in captured["params"]
    assert account.id == 28


def test_datetimes_are_always_naive_and_comparable():
    """Le même champ arrivait naïf OU avec fuseau selon le connecteur : tout
    tri/comparaison levait TypeError chez le consommateur, aléatoirement."""
    from pypowens.models import _parse_datetime

    forms = [
        _parse_datetime("2026-01-01 10:00:00"),  # naïf (forme Powens)
        _parse_datetime("2026-01-01T11:00:00Z"),  # ISO aware UTC
        _parse_datetime("2026-01-01 12:00:00 +0200"),  # aware avec espace
        _parse_datetime("2026-01-01T13:00:00+02:00"),  # ISO aware
    ]
    assert all(dt is not None and dt.tzinfo is None for dt in forms)
    assert sorted(forms) == forms  # comparables entre eux, plus de TypeError
    # L'heure murale est préservée telle quelle (pas de conversion sournoise).
    assert forms[2].hour == 12


# ------------------------------------------------------------- hygiène HTTP


@respx.mock
async def test_every_request_carries_a_user_agent():
    captured = {}

    def record(request):
        captured["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, json={"id": 1})

    respx.get(f"{BASE}/users/me").mock(side_effect=record)
    async with PowensClient("myapp-sandbox", access_token="tok") as powens:
        await powens.get_current_user()
    assert captured["ua"].startswith("pypowens/")


@respx.mock
async def test_non_json_error_body_is_kept_as_excerpt():
    """Une page HTML CloudFront produisait « [HTTP 503] unknown error » : le
    corps, qui contenait la cause, était jeté."""
    respx.get(f"{BASE}/users/me").mock(
        return_value=httpx.Response(
            503,
            headers={"Content-Type": "text/html", "X-Request-Id": "cf-abc123"},
            text="<html><body><h1>503 ERROR</h1>The request could not be satisfied"
            " (CloudFront)</body></html>",
        )
    )
    async with PowensClient("myapp-sandbox", access_token="tok", max_retries=0) as powens:
        with pytest.raises(PowensAPIError) as err:
            await powens.get_current_user()
    assert "CloudFront" in (err.value.message or "")
    assert err.value.request_id == "cf-abc123"
    assert "cf-abc123" in str(err.value)


@respx.mock
async def test_request_id_from_the_json_payload_is_exposed():
    respx.get(f"{BASE}/users/me").mock(
        return_value=httpx.Response(
            404,
            json={
                "code": "notFound",
                "description": "Ressource was not found.",
                "request_id": "37a1bc0d",
            },
        )
    )
    async with PowensClient("myapp-sandbox", access_token="tok") as powens:
        with pytest.raises(PowensAPIError) as err:
            await powens.get_current_user()
    assert err.value.request_id == "37a1bc0d"
