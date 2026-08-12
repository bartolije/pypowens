"""Lightweight data models mapping Powens API objects.

Every model keeps the untouched API payload in :attr:`raw`, so fields not yet
mapped explicitly here remain reachable (``obj.raw["some_new_field"]``). This
keeps the wrapper resilient to Powens adding fields over time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, TypeAlias

# ``Transaction`` and ``Investment`` both declare a field named ``date``, which
# shadows the imported type for every annotation that follows in the class body.
# Annotating with this alias keeps those annotations resolvable (and mypy happy).
DateType: TypeAlias = date


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    # NaN/Infinity parse without error but poison every downstream aggregate:
    # a single NaN turns a whole sum() of balances into NaN, silently.
    return parsed if parsed.is_finite() else None


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
    date: DateType | None = None
    application_date: DateType | None = None
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
class Investment:
    """A security line held in a market/PEA/life-insurance account.

    Balances alone say nothing about *what* is held: this is what makes an
    investment account auditable (line, quantity, unit price, gain).
    """

    id: int | None
    id_account: int | None = None
    label: str | None = None
    code: str | None = None          # ISIN when available
    code_type: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    unit_value: Decimal | None = None
    valuation: Decimal | None = None
    diff: Decimal | None = None      # unrealized gain/loss, absolute
    diff_percent: Decimal | None = None
    portfolio_share: Decimal | None = None
    currency: str | None = None
    vdate: DateType | None = None    # valuation date
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Investment:
        currency = data.get("currency")
        if isinstance(currency, dict):
            currency = currency.get("id")
        return cls(
            id=data.get("id"),
            id_account=data.get("id_account"),
            label=data.get("label"),
            code=data.get("code"),
            code_type=data.get("code_type"),
            quantity=_parse_decimal(data.get("quantity")),
            unit_price=_parse_decimal(data.get("unitprice")),
            unit_value=_parse_decimal(data.get("unitvalue")),
            valuation=_parse_decimal(data.get("valuation")),
            diff=_parse_decimal(data.get("diff")),
            diff_percent=_parse_decimal(data.get("diff_percent")),
            portfolio_share=_parse_decimal(data.get("portfolio_share")),
            currency=currency,
            vdate=_parse_date(data.get("vdate")),
            raw=data,
        )


@dataclass(slots=True)
class InvestmentValue:
    """One dated unit value of a security line.

    The only history Powens keeps about an investment. It starts the day the connection
    was created — never before — so it is worth archiving locally rather than re-fetching
    it as if it were a permanent record.
    """

    id: int | None
    id_investment: int | None = None
    vdate: DateType | None = None
    unit_value: Decimal | None = None
    original_currency: str | None = None
    original_unit_value: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> InvestmentValue:
        currency = data.get("original_currency")
        if isinstance(currency, dict):
            currency = currency.get("id")
        return cls(
            id=data.get("id"),
            id_investment=data.get("id_investment"),
            vdate=_parse_date(data.get("vdate")),
            unit_value=_parse_decimal(data.get("unitvalue")),
            original_currency=currency,
            original_unit_value=_parse_decimal(data.get("original_unitvalue")),
            raw=data,
        )


@dataclass(slots=True)
class Category:
    """A bank category from the ``/banks/categories`` catalog."""

    id: int | None
    name: str | None = None
    parent_id: int | None = None
    color: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Category:
        return cls(
            id=data.get("id"),
            name=data.get("name"),
            parent_id=data.get("id_parent_category") or data.get("parent_id"),
            color=data.get("color"),
            raw=data,
        )


@dataclass(slots=True)
class Indicators:
    """Result of ``GET /users/{id}/indicators``.

    ``indicators`` is ``None`` when the product is not enabled/computed for the
    app (checked via :attr:`available`).
    """

    id_user: int | None
    indicators: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def available(self) -> bool:
        return self.indicators is not None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Indicators:
        return cls(
            id_user=data.get("id_user"),
            indicators=data.get("indicators"),
            raw=data,
        )


@dataclass(slots=True)
class ClientConfig:
    """An application's own configuration (``GET /clients/{id}``).

    Mainly useful for :attr:`redirect_uris`: the Webview rejects any ``redirect_uri``
    absent from that list with a message that names no expected value, so being able
    to read the list is the difference between a diagnosable error and a guess.
    """

    id: int | None
    name: str | None = None
    redirect_uris: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def allows(self, redirect_uri: str) -> bool:
        """Whether ``redirect_uri`` is whitelisted (exact match, as Powens compares)."""
        return redirect_uri.strip() in self.redirect_uris

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> ClientConfig:
        uris = data.get("redirect_uris")
        if not isinstance(uris, list):
            single = data.get("redirect_uri")
            uris = [single] if single else []
        return cls(
            id=data.get("id"),
            name=data.get("name"),
            redirect_uris=[str(u) for u in uris if u],
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
