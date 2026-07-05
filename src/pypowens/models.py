"""Lightweight data models mapping Powens API objects.

Every model keeps the untouched API payload in :attr:`raw`, so fields not yet
mapped explicitly here remain reachable (``obj.raw["some_new_field"]``). This
keeps the wrapper resilient to Powens adding fields over time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        # Powens uses "YYYY-MM-DD HH:MM:SS" and ISO 8601 variants.
        return datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError:
        return None


def _parse_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


@dataclass(slots=True)
class AuthToken:
    """Token returned by ``/auth/init`` or ``/auth/renew``."""

    access_token: str
    token_type: str = "Bearer"
    id_user: int | None = None
    type: str | None = None
    expires_in: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> AuthToken:
        # /auth/init returns "auth_token"; /auth/renew returns "access_token".
        token = data.get("access_token") or data.get("auth_token") or ""
        return cls(
            access_token=token,
            token_type=data.get("token_type", "Bearer"),
            id_user=data.get("id_user"),
            type=data.get("type"),
            expires_in=data.get("expires_in"),
            raw=data,
        )


@dataclass(slots=True)
class User:
    id: int | None
    signin: datetime | None = None
    platform: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> User:
        return cls(
            id=data.get("id"),
            signin=_parse_datetime(data.get("signin")),
            platform=data.get("platform"),
            raw=data,
        )


@dataclass(slots=True)
class Connector:
    """A bank/provider connector."""

    id: int | None
    uuid: str | None = None
    name: str | None = None
    slug: str | None = None
    beta: bool = False
    color: str | None = None
    capabilities: list[str] = field(default_factory=list)
    products: list[str] = field(default_factory=list)
    hidden: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Connector:
        return cls(
            id=data.get("id"),
            uuid=data.get("uuid"),
            name=data.get("name"),
            slug=data.get("slug"),
            beta=bool(data.get("beta", False)),
            color=data.get("color"),
            capabilities=list(data.get("capabilities") or []),
            products=list(data.get("products") or []),
            hidden=data.get("hidden"),
            raw=data,
        )


@dataclass(slots=True)
class Connection:
    """A user's connection to a bank connector."""

    id: int | None
    id_user: int | None = None
    id_connector: int | None = None
    connector_uuid: str | None = None
    state: str | None = None
    active: bool = True
    error_message: str | None = None
    last_update: datetime | None = None
    created: datetime | None = None
    expire: datetime | None = None
    next_try: datetime | None = None
    connector: Connector | None = None
    accounts: list[Account] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Connection:
        connector = data.get("connector")
        accounts = data.get("accounts")
        return cls(
            id=data.get("id"),
            id_user=data.get("id_user"),
            id_connector=data.get("id_connector"),
            connector_uuid=data.get("connector_uuid"),
            state=data.get("state"),
            active=bool(data.get("active", True)),
            error_message=data.get("error_message") or data.get("error"),
            last_update=_parse_datetime(data.get("last_update")),
            created=_parse_datetime(data.get("created")),
            expire=_parse_datetime(data.get("expire")),
            next_try=_parse_datetime(data.get("next_try")),
            connector=Connector.from_api(connector) if isinstance(connector, dict) else None,
            accounts=[Account.from_api(a) for a in accounts] if isinstance(accounts, list) else [],
            raw=data,
        )


@dataclass(slots=True)
class Account:
    """A bank account belonging to a connection."""

    id: int | None
    id_connection: int | None = None
    name: str | None = None
    number: str | None = None
    iban: str | None = None
    currency: str | None = None
    type: str | None = None
    balance: Decimal | None = None
    coming: Decimal | None = None
    disabled: bool | None = None
    last_update: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Account:
        currency = data.get("currency")
        if isinstance(currency, dict):  # sometimes expanded as an object
            currency = currency.get("id")
        return cls(
            id=data.get("id"),
            id_connection=data.get("id_connection"),
            name=data.get("name"),
            number=data.get("number"),
            iban=data.get("iban"),
            currency=currency,
            type=data.get("type"),
            balance=_parse_decimal(data.get("balance")),
            coming=_parse_decimal(data.get("coming")),
            disabled=data.get("disabled"),
            last_update=_parse_datetime(data.get("last_update")),
            raw=data,
        )


@dataclass(slots=True)
class Transaction:
    """A single bank transaction."""

    id: int | None
    id_account: int | None = None
    date: date | None = None
    application_date: date | None = None
    value: Decimal | None = None
    type: str | None = None
    wording: str | None = None
    original_wording: str | None = None
    simplified_wording: str | None = None
    categories: list[Any] = field(default_factory=list)
    coming: bool = False
    active: bool = True
    deleted: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Transaction:
        return cls(
            id=data.get("id"),
            id_account=data.get("id_account"),
            date=_parse_date(data.get("date")),
            application_date=_parse_date(data.get("application_date")),
            value=_parse_decimal(data.get("value")),
            type=data.get("type"),
            wording=data.get("wording"),
            original_wording=data.get("original_wording"),
            simplified_wording=data.get("simplified_wording"),
            categories=list(data.get("categories") or []),
            coming=bool(data.get("coming", False)),
            active=bool(data.get("active", True)),
            deleted=_parse_datetime(data.get("deleted")),
            raw=data,
        )


@dataclass(slots=True)
class AccountsList:
    """Result of ``GET /users/{id}/accounts``."""

    accounts: list[Account]
    balances: dict[str, Decimal] = field(default_factory=dict)
    coming_balances: dict[str, Decimal] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> AccountsList:
        def _bal(d: Any) -> dict[str, Decimal]:
            if not isinstance(d, dict):
                return {}
            parsed = ((k, _parse_decimal(v)) for k, v in d.items())
            return {k: v for k, v in parsed if v is not None}

        return cls(
            accounts=[Account.from_api(a) for a in data.get("accounts") or []],
            balances=_bal(data.get("balances")),
            coming_balances=_bal(data.get("coming_balances")),
        )
