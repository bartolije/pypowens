"""Async client for the Powens (ex-Budget Insight) data aggregation API.

Reference: https://docs.powens.com/
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any
from urllib.parse import urlencode

import httpx

from .exceptions import (
    PowensAPIError,
    PowensAuthError,
    PowensConfigError,
    PowensRateLimitError,
)
from .models import (
    Account,
    AccountsList,
    AuthToken,
    Category,
    Connection,
    Connector,
    Indicators,
    Transaction,
    User,
)

DEFAULT_TIMEOUT = 30.0
API_PATH = "/2.0"


def _resolve_host(domain: str) -> str:
    """Turn a Powens *domain* into a base host URL.

    Accepts ``"myapp-sandbox"``, ``"myapp-sandbox.biapi.pro"`` or a full URL.
    """
    domain = domain.strip().rstrip("/")
    if domain.startswith(("http://", "https://")):
        host = domain
    elif "." in domain:
        host = f"https://{domain}"
    else:
        host = f"https://{domain}.biapi.pro"
    # Drop a trailing /2.0 if the caller included it.
    if host.endswith(API_PATH):
        host = host[: -len(API_PATH)]
    return host


class PowensClient:
    """Asynchronous wrapper around the Powens API.

    Example::

        async with PowensClient("myapp-sandbox", client_id="...", client_secret="...") as p:
            token = await p.create_user()          # creates a user + permanent token
            connectors = await p.list_connectors(country_codes="fr")
            accounts = await p.list_accounts()
            txns = await p.list_transactions(limit=200)
    """

    def __init__(
        self,
        domain: str,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        access_token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not domain:
            raise PowensConfigError("A Powens domain is required (e.g. 'myapp-sandbox').")
        self._host = _resolve_host(domain)
        self.client_id = client_id
        self.client_secret = client_secret
        self._access_token = access_token
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout)

    @classmethod
    def from_env(cls, **kwargs: Any) -> PowensClient:
        """Build a client from ``POWENS_*`` environment variables.

        Reads ``POWENS_DOMAIN``, ``POWENS_CLIENT_ID``, ``POWENS_CLIENT_SECRET``
        and ``POWENS_ACCESS_TOKEN``. Keyword arguments override the environment.
        Empty environment variables are treated as unset.
        """

        def _env(name: str, override: Any) -> Any:
            if override is not None:
                return override
            value = os.environ.get(name)
            return value.strip() if value and value.strip() else None

        domain = _env("POWENS_DOMAIN", kwargs.pop("domain", None))
        if not domain:
            raise PowensConfigError("POWENS_DOMAIN is not set.")
        return cls(
            domain,
            client_id=_env("POWENS_CLIENT_ID", kwargs.pop("client_id", None)),
            client_secret=_env("POWENS_CLIENT_SECRET", kwargs.pop("client_secret", None)),
            access_token=_env("POWENS_ACCESS_TOKEN", kwargs.pop("access_token", None)),
            **kwargs,
        )

    # ------------------------------------------------------------------ state

    @property
    def access_token(self) -> str | None:
        """The bearer token currently used for authenticated requests."""
        return self._access_token

    @access_token.setter
    def access_token(self, value: str | None) -> None:
        self._access_token = value

    @property
    def base_url(self) -> str:
        return f"{self._host}{API_PATH}"

    # ---------------------------------------------------------------- plumbing

    def _url(self, endpoint_or_href: str) -> str:
        if endpoint_or_href.startswith(("http://", "https://")):
            return endpoint_or_href
        if endpoint_or_href.startswith("/"):
            # A pagination href already carries the /2.0 prefix.
            return f"{self._host}{endpoint_or_href}"
        return f"{self._host}{API_PATH}/{endpoint_or_href}"

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> Any:
        headers: dict[str, str] = {"Accept": "application/json"}
        if auth:
            if not self._access_token:
                raise PowensAuthError(
                    401,
                    code="noToken",
                    message="No access token set. Call create_user()/renew_token() first.",
                )
            headers["Authorization"] = f"Bearer {self._access_token}"

        clean_params = (
            None if params is None else {k: v for k, v in params.items() if v is not None}
        )
        response = await self._http.request(
            method, self._url(endpoint), params=clean_params, json=json, headers=headers
        )
        return self._handle(response)

    @staticmethod
    def _handle(response: httpx.Response) -> Any:
        if response.status_code == 204 or not response.content:
            if response.is_success:
                return None
        try:
            data: Any = response.json()
        except ValueError:
            data = None

        if response.is_success:
            return data

        code = message = None
        if isinstance(data, dict):
            code = data.get("code")
            message = (
                data.get("message")
                or data.get("description")
                or data.get("error_description")
                or data.get("error")
            )

        if response.status_code in (401, 403):
            exc: type[PowensAPIError] = PowensAuthError
        elif response.status_code == 429:
            exc = PowensRateLimitError
        else:
            exc = PowensAPIError
        raise exc(response.status_code, code=code, message=message, payload=data)

    async def _paginate(
        self,
        endpoint: str,
        *,
        item_key: str,
        params: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield raw items across pages, following ``_links.next`` hrefs."""
        data = await self._request("GET", endpoint, params=params, auth=auth)
        while isinstance(data, dict):
            for item in data.get(item_key) or []:
                yield item
            nxt = (data.get("_links") or {}).get("next")
            if isinstance(nxt, dict):
                nxt = nxt.get("href")
            if not nxt:
                break
            data = await self._request("GET", nxt, auth=auth)

    # ------------------------------------------------------------------- auth

    async def create_user(self) -> AuthToken:
        """Create a new Powens user and obtain a permanent token (``/auth/init``).

        Requires ``client_id`` and ``client_secret``. The returned token is stored
        on the client and used for subsequent authenticated calls.
        """
        self._require_app_credentials()
        data = await self._request(
            "POST",
            "auth/init",
            json={"client_id": self.client_id, "client_secret": self.client_secret},
            auth=False,
        )
        token = AuthToken.from_api(data)
        if token.access_token:
            self._access_token = token.access_token
        return token

    async def renew_token(self, id_user: int, *, revoke_previous: bool = False) -> AuthToken:
        """Get a fresh token for an existing user (``/auth/renew``)."""
        self._require_app_credentials()
        data = await self._request(
            "POST",
            "auth/renew",
            json={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "id_user": id_user,
                "revoke_previous": revoke_previous,
            },
            auth=False,
        )
        token = AuthToken.from_api(data)
        if token.access_token:
            self._access_token = token.access_token
        return token

    async def get_temporary_code(self, *, type: str = "singleAccess") -> dict[str, Any]:
        """Create a short-lived code, e.g. to hand off to the Webview (``/auth/token/code``)."""
        return await self._request("GET", "auth/token/code", params={"type": type})

    def build_webview_url(
        self,
        redirect_uri: str,
        temporary_code: str,
        *,
        connector_ids: list[int] | None = None,
        extra: dict[str, str] | None = None,
    ) -> str:
        """Build a Powens Webview *connect* URL to link a new bank.

        Combine with :meth:`get_temporary_code`. The ``redirect_uri`` must be
        whitelisted in the Powens console. Requires ``client_id``.

        Note: exact Webview parameters can vary by setup — see
        https://docs.powens.com/ (Webview).
        """
        if not self.client_id:
            raise PowensConfigError("client_id is required to build a Webview URL.")
        params: dict[str, str] = {
            "domain": self._host.replace("https://", "").replace("http://", ""),
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "code": temporary_code,
        }
        if connector_ids:
            params["connector_ids"] = ",".join(str(i) for i in connector_ids)
        if extra:
            params.update(extra)
        return f"https://webview.powens.com/connect?{urlencode(params)}"

    async def exchange_code(self, code: str) -> AuthToken:
        """Exchange an authorization ``code`` for a permanent token (``/auth/token/access``)."""
        self._require_app_credentials()
        data = await self._request(
            "POST",
            "auth/token/access",
            json={
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
            },
            auth=False,
        )
        token = AuthToken.from_api(data)
        if token.access_token:
            self._access_token = token.access_token
        return token

    async def revoke_token(self) -> None:
        """Revoke the current token (``DELETE /auth/token``) and forget it locally."""
        await self._request("DELETE", "auth/token")
        self._access_token = None

    # -------------------------------------------------------------- resources

    async def get_current_user(self) -> User:
        """Return the user tied to the current token (``GET /users/me``)."""
        return User.from_api(await self._request("GET", "users/me"))

    async def get_indicators(self, user_id: int | str = "me") -> Indicators:
        """Return computed banking indicators (``GET /users/{id}/indicators``).

        ``Indicators.available`` is ``False`` when the product is not enabled.
        """
        return Indicators.from_api(await self._request("GET", f"users/{user_id}/indicators"))

    async def list_categories(self) -> list[Category]:
        """List the bank category catalog (``GET /banks/categories``)."""
        data = await self._request(
            "GET", "banks/categories", auth=self._access_token is not None
        )
        items = data.get("bank_category") if isinstance(data, dict) else data
        return [Category.from_api(c) for c in (items or [])]

    async def list_connectors(
        self,
        *,
        country_codes: str | None = None,
        id_payment: str | None = None,
    ) -> list[Connector]:
        """List available connectors (banks/providers) — ``GET /connectors``.

        This endpoint does not require authentication; the token is sent if present.
        """
        params = {"country_codes": country_codes, "id_payment": id_payment}
        data = await self._request(
            "GET", "connectors", params=params, auth=self._access_token is not None
        )
        items = data.get("connectors") if isinstance(data, dict) else data
        return [Connector.from_api(c) for c in (items or [])]

    async def list_connections(
        self,
        user_id: int | str = "me",
        *,
        expand: str | None = "connector,accounts",
    ) -> list[Connection]:
        """List a user's bank connections (``GET /users/{id}/connections``)."""
        params = {"expand": expand}
        return [
            Connection.from_api(c)
            async for c in self._paginate(
                f"users/{user_id}/connections", item_key="connections", params=params
            )
        ]

    async def delete_connection(self, connection_id: int, user_id: int | str = "me") -> None:
        """Permanently delete a connection and all its data.

        This cannot be undone (``DELETE /users/{id}/connections/{connectionId}``).
        """
        await self._request("DELETE", f"users/{user_id}/connections/{connection_id}")

    async def list_accounts(
        self, user_id: int | str = "me", *, include_disabled: bool = False
    ) -> AccountsList:
        """List a user's bank accounts with total balances (``GET /users/{id}/accounts``)."""
        params = {"all": "" if include_disabled else None}
        data = await self._request("GET", f"users/{user_id}/accounts", params=params)
        return AccountsList.from_api(data if isinstance(data, dict) else {"accounts": data})

    async def get_account(self, account_id: int, user_id: int | str = "me") -> Account:
        """Return a single bank account (``GET /users/{id}/accounts/{accountId}``)."""
        return Account.from_api(
            await self._request("GET", f"users/{user_id}/accounts/{account_id}")
        )

    async def iter_transactions(
        self,
        user_id: int | str = "me",
        *,
        limit: int = 1000,
        min_date: str | None = None,
        max_date: str | None = None,
        last_update: str | None = None,
        income: bool | None = None,
        wording: str | None = None,
        include_deleted: bool = False,
    ) -> AsyncIterator[Transaction]:
        """Stream transactions across all pages (``GET /users/{id}/transactions``).

        Dates are ``YYYY-MM-DD`` strings. ``limit`` is the page size (max 1000).
        """
        params: dict[str, Any] = {
            "limit": limit,
            "min_date": min_date,
            "max_date": max_date,
            "last_update": last_update,
            "income": income,
            "wording": wording,
            "all": "" if include_deleted else None,
        }
        async for item in self._paginate(
            f"users/{user_id}/transactions", item_key="transactions", params=params
        ):
            yield Transaction.from_api(item)

    async def list_transactions(
        self,
        user_id: int | str = "me",
        *,
        limit: int = 1000,
        max_transactions: int | None = None,
        min_date: str | None = None,
        max_date: str | None = None,
        last_update: str | None = None,
        income: bool | None = None,
        wording: str | None = None,
        include_deleted: bool = False,
    ) -> list[Transaction]:
        """Fetch transactions as a list, following pagination.

        Set ``max_transactions`` to cap how many are retrieved; leave it ``None``
        to fetch everything matching the filters.
        """
        out: list[Transaction] = []
        async for txn in self.iter_transactions(
            user_id,
            limit=limit,
            min_date=min_date,
            max_date=max_date,
            last_update=last_update,
            income=income,
            wording=wording,
            include_deleted=include_deleted,
        ):
            out.append(txn)
            if max_transactions is not None and len(out) >= max_transactions:
                break
        return out

    # --------------------------------------------------------------- lifecycle

    def _require_app_credentials(self) -> None:
        if not self.client_id or not self.client_secret:
            raise PowensConfigError(
                "client_id and client_secret are required for this operation."
            )

    async def aclose(self) -> None:
        """Close the underlying HTTP client (only if this instance created it)."""
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> PowensClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
