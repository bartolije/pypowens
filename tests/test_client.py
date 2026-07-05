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
    assert _resolve_host("https://myapp-sandbox.biapi.pro/2.0/") == "https://myapp-sandbox.biapi.pro"


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


async def test_missing_token_raises_auth_error():
    async with PowensClient("myapp-sandbox") as p:
        with pytest.raises(PowensAuthError):
            await p.get_current_user()
