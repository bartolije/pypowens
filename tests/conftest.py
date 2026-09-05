"""Shared fixtures: a fake Powens client so the FastAPI app can be exercised
end-to-end (lifespan, routers, Jinja rendering) without any network call."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta

import pytest
import time_machine

from pypowens import (
    AccountsList,
    AuthToken,
    ClientConfig,
    Connection,
    Indicators,
    Investment,
    Transaction,
)

# Un 15 du mois : les pas de 30 jours de build_dataset dérivent d'environ un jour
# par mois, et un ancrage en début/fin de mois faisait retomber deux échéances
# dans le même mois calendaire ~25 % des jours de l'année — la fixture ne
# produisait alors que 13 mois distincts au lieu des 14 annoncés.
FROZEN_TODAY = date(2026, 6, 15)


@pytest.fixture(autouse=True)
def _frozen_clock():
    """Horloge figée : le jeu de test ne dépend plus du jour d'exécution de la CI."""
    with time_machine.travel(FROZEN_TODAY, tick=False):
        yield


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """Neutralise le .env réel du poste, chargé à l'import de app.config.

    Sans cela, les tests qui appellent ``get_settings()`` directement héritaient
    des vrais identifiants (POWENS_*, OPENFIGI_API_KEY) — risque de faux vert et
    d'appel réseau accidentel avec de vraies credentials.
    """
    for var in (
        "POWENS_CLIENT_ID",
        "POWENS_CLIENT_SECRET",
        "POWENS_ACCESS_TOKEN",
        "OPENFIGI_API_KEY",
        "APP_HOST",
        "APP_PORT",
        "APP_ALLOW_REMOTE",
        "APP_HISTORY_MONTHS",
        "APP_BASE_CURRENCY",
        # L'authentification aussi : sans cette purge, poser APP_TOTP_SECRET ou
        # APP_AUTH_USER dans le .env du poste changeait le comportement de la
        # suite (second facteur exigé, hôtes non filtrés) — un rouge inexplicable.
        "APP_AUTH_USER",
        "APP_AUTH_PASSWORD",
        "APP_SESSION_SECRET",
        "APP_SESSION_MAX_AGE_HOURS",
        "APP_TOTP_SECRET",
        "APP_API_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("POWENS_DOMAIN", "test-sandbox")
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "isolated.db"))
    monkeypatch.setenv("APP_STATE_PATH", str(tmp_path / "isolated-state.json"))


def _account(
    account_id: int,
    *,
    name: str,
    type: str,
    balance: str,
    currency: str = "EUR",
    diff: str | None = None,
) -> dict:
    return {
        "id": account_id,
        "id_connection": 1,
        "name": name,
        "type": type,
        "balance": balance,
        "currency": {"id": currency},
        "iban": f"FR7630006000011234567890{account_id:03d}",
        "diff": diff,
    }


def _txn(
    txn_id: int,
    *,
    account: int,
    day: date,
    value: str,
    type: str,
    wording: str,
    coming: bool = False,
) -> dict:
    return {
        "id": txn_id,
        "id_account": account,
        "date": day.isoformat(),
        "value": value,
        "type": type,
        "wording": wording,
        "simplified_wording": wording,
        "original_wording": wording,
        "coming": coming,
    }


def build_dataset(today: date | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    """Return (accounts, connections, transactions) as raw API payloads.

    Covers: two spending accounts, savings, one USD account (currency guard),
    a monthly subscription, a monthly salary, an internal transfer pair, a
    one-off purchase and one ``coming`` operation.
    """
    today = today or date.today()
    accounts = [
        _account(1, name="Compte courant", type="checking", balance="2500.00"),
        _account(2, name="Livret", type="livret_a", balance="15000.00", diff="0"),
        _account(3, name="PEA", type="pea", balance="42000.00", diff="1200.00"),
        _account(4, name="Compte USD", type="checking", balance="800.00", currency="USD"),
    ]
    connections = [
        {
            "id": 1,
            "id_connector": 40,
            "state": None,
            "error_message": None,
            "last_update": "2026-07-01 06:00:00",
            "connector": {"id": 40, "name": "Ma Banque"},
            "accounts": accounts[:3],
        }
    ]

    txns: list[dict] = []
    next_id = 100
    # 14 months of a monthly subscription + a monthly salary.
    for month_offset in range(1, 15):
        day = (today.replace(day=1) - timedelta(days=30 * month_offset)).replace(day=5)
        txns.append(
            _txn(next_id, account=1, day=day, value="-13.49", type="card", wording="NETFLIX.COM")
        )
        next_id += 1
        txns.append(
            _txn(
                next_id,
                account=1,
                day=day.replace(day=27),
                value="2800.00",
                type="transfer",
                wording="VIR SALAIRE EMPLOYEUR",
            )
        )
        next_id += 1

    last_month = (today.replace(day=1) - timedelta(days=15)).replace(day=12)
    # One-off purchase.
    txns.append(
        _txn(
            900,
            account=1,
            day=last_month,
            value="-249.90",
            type="card",
            wording="ENSEIGNE\\VILLE\\ FR",
        )
    )
    # Internal transfer (mirror pair, must be excluded everywhere).
    for txn_id, account, value in ((901, 1, "-500.00"), (902, 2, "500.00")):
        txns.append(
            _txn(
                txn_id,
                account=account,
                day=last_month,
                value=value,
                type="transfer",
                wording="EPGN - Livret",
            )
        )
    # Forecast operation: must never be counted.
    txns.append(
        _txn(903, account=1, day=today, value="-99.00", type="card", wording="A VENIR", coming=True)
    )
    return accounts, connections, txns


class FakeClient:
    """Minimal stand-in for :class:`pypowens.PowensClient`."""

    def __init__(self, accounts: list[dict], connections: list[dict], txns: list[dict]) -> None:
        self._accounts = accounts
        self._connections = connections
        self._txns = txns
        # Comptes désactivés côté Powens : servis SEULEMENT avec include_disabled=True,
        # comme le vrai endpoint. Alimente le bandeau « comptes hors du total ».
        self._disabled_accounts: list[dict] = []
        self.access_token = "fake-token"
        self.client_id = "cid"
        self.closed = False
        # Whitelisted callbacks, as the Powens console would hold them.
        self.redirect_uris = ["http://127.0.0.1:8000/callback"]

    async def list_accounts(self, *args, **kwargs) -> AccountsList:
        accounts = list(self._accounts)
        if kwargs.get("include_disabled"):
            accounts = [*accounts, *self._disabled_accounts]
        return AccountsList.from_api({"accounts": accounts, "balances": {"EUR": "59500.00"}})

    async def update_account(self, account_id, *, disabled=None, **kwargs):
        # Rejoue PUT /accounts/{id} : disabled=False réactive le compte.
        if disabled is False:
            for raw in self._disabled_accounts:
                if raw.get("id") == account_id:
                    raw.pop("disabled", None)
        return None

    async def list_connections(self, *args, **kwargs) -> list[Connection]:
        return [Connection.from_api(c) for c in self._connections]

    async def iter_transactions(self, *args, **kwargs) -> AsyncIterator[Transaction]:
        """Honours ``min_date`` like the real endpoint, so window logic is testable."""
        min_date = kwargs.get("min_date")
        floor = date.fromisoformat(min_date) if min_date else None
        for raw in self._txns:
            if floor and date.fromisoformat(raw["date"]) < floor:
                continue
            yield Transaction.from_api(raw)

    async def get_indicators(self, *args, **kwargs) -> Indicators:
        return Indicators.from_api({"id_user": 1, "indicators": None})

    async def list_investments(self, *args, **kwargs) -> list[Investment]:
        return [
            Investment.from_api(
                {
                    "id": 1,
                    "id_account": 3,
                    "label": "ETF MONDE",
                    "code": "FR0000000000",
                    "quantity": "120.0",
                    "valuation": "42000.00",
                    "diff": "1200.00",
                    # Fraction, comme l'API réelle (0.0294 = +2,94 %) : 1200/(42000-1200).
                    "diff_percent": "0.0294",
                    "currency": {"id": "EUR"},
                }
            )
        ]

    async def list_investment_history(self, investment_id: int, *args, **kwargs):
        """Deux séances de VL, de quoi mesurer une variation.

        ``min_date`` est honoré comme le vrai endpoint, mais ne remonte jamais plus loin
        que ce que l'API a collecté — c'est tout l'objet de l'archivage local.
        """
        from pypowens import InvestmentValue

        raw = [
            {
                "id": 1,
                "id_investment": investment_id,
                "vdate": (date.today() - timedelta(days=2)).isoformat(),
                "unitvalue": "100.00",
            },
            {
                "id": 2,
                "id_investment": investment_id,
                "vdate": (date.today() - timedelta(days=1)).isoformat(),
                "unitvalue": "110.00",
            },
        ]
        floor = kwargs.get("min_date")
        if floor:
            raw = [r for r in raw if r["vdate"] >= floor]
        return [InvestmentValue.from_api(r) for r in raw]

    async def update_connection(self, connection_id: int, *args, **kwargs):
        self.synced_connections = [*getattr(self, "synced_connections", []), connection_id]
        return None

    async def get_temporary_code(self, *args, **kwargs) -> dict:
        return {"code": "tmp-code"}

    async def get_client_config(self, *args, **kwargs) -> ClientConfig:
        return ClientConfig.from_api(
            {"id": 1, "name": "Test app", "redirect_uris": list(self.redirect_uris)}
        )

    async def exchange_code(self, code: str, *args, **kwargs) -> AuthToken:
        self.access_token = f"token-from-{code}"
        return AuthToken.from_api({"access_token": self.access_token, "id_user": 1})

    def build_webview_url(
        self,
        redirect_uri: str,
        code: str,
        *,
        connector_ids: list[int] | None = None,
        lang: str = "en",
        **kwargs,
    ) -> str:
        """Mirrors the real builder, including the mandatory language segment.

        The segment is reproduced here on purpose: dropping it yields a CloudFront
        503 instead of the Webview, so the app must be shown to pass one.
        """
        screen = kwargs.get("flow", "connect")
        url = f"https://webview.powens.com/{lang}/{screen}?code={code}"
        if connector_ids:
            url += "&connector_ids=" + ",".join(str(i) for i in connector_ids)
        if kwargs.get("connection_id") is not None:
            url += f"&connection_id={kwargs['connection_id']}"
        return url

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient(*build_dataset())


@pytest.fixture
def client(monkeypatch, fake_client, tmp_path):
    """A ``TestClient`` on the real app, wired to :class:`FakeClient`.

    The local store points at a throwaway file so tests never touch (nor read)
    the real balance history.
    """
    from fastapi.testclient import TestClient

    monkeypatch.setenv("POWENS_DOMAIN", "test-sandbox")
    monkeypatch.setenv("POWENS_CLIENT_ID", "cid")
    monkeypatch.setenv("POWENS_CLIENT_SECRET", "secret")
    monkeypatch.setenv("POWENS_ACCESS_TOKEN", "fake-token")
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "test.db"))
    # Sans quoi le callback Webview (persist_token) écrirait un vrai
    # .powens_state.json à la racine du dépôt pendant les tests.
    monkeypatch.setenv("APP_STATE_PATH", str(tmp_path / "state.json"))

    import app.data
    import app.main

    async def _bootstrap(_settings):
        return fake_client

    monkeypatch.setattr(app.main, "bootstrap_client", _bootstrap)
    # Le throttle 6 h de la synchro d'ouverture vit sur l'app singleton : sans
    # remise à zéro, seul le premier test de la session la verrait se déclencher.
    app.main.app.state.auto_sync_at = None
    app.data.clear_cache()
    with TestClient(app.main.app) as test_client:
        yield test_client
    app.data.clear_cache()
