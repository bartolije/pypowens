"""pypowens — an async Python wrapper for the Powens data aggregation API."""

from __future__ import annotations

from .client import PowensClient
from .exceptions import (
    PowensAPIError,
    PowensAuthError,
    PowensConfigError,
    PowensError,
    PowensRateLimitError,
)
from .models import (
    Account,
    AccountsList,
    AuthToken,
    Category,
    ClientConfig,
    Connection,
    Connector,
    Indicators,
    Investment,
    Transaction,
    User,
)

__version__ = "0.2.0"

__all__ = [
    "PowensClient",
    "PowensError",
    "PowensConfigError",
    "PowensAPIError",
    "PowensAuthError",
    "PowensRateLimitError",
    "AuthToken",
    "User",
    "Connector",
    "Connection",
    "Account",
    "AccountsList",
    "Transaction",
    "Investment",
    "Category",
    "ClientConfig",
    "Indicators",
    "__version__",
]
