"""Exception hierarchy for :mod:`pypowens`."""

from __future__ import annotations

from typing import Any


class PowensError(Exception):
    """Base class for every error raised by the wrapper."""


class PowensConfigError(PowensError):
    """Raised when the client is misconfigured (missing domain, credentials, ...)."""


class PowensAPIError(PowensError):
    """Raised when the Powens API returns a non-2xx response.

    Powens error bodies usually look like::

        {"code": "invalidToken", "message": "The token is invalid", ...}
    """

    def __init__(
        self,
        status_code: int,
        *,
        code: str | None = None,
        message: str | None = None,
        payload: Any = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.payload = payload
        detail = message or code or "unknown error"
        super().__init__(f"[HTTP {status_code}] {detail}")


class PowensAuthError(PowensAPIError):
    """Raised on authentication/authorization failures (HTTP 401/403)."""


class PowensRateLimitError(PowensAPIError):
    """Raised when Powens rate-limits the client (HTTP 429)."""
