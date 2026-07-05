"""Token bootstrap and local persistence.

Priority for obtaining an access token:
1. ``POWENS_ACCESS_TOKEN`` from the environment / .env (user-provided token);
2. a previously persisted ``.powens_state.json``;
3. otherwise ``create_user()`` (needs client_id/secret) and persist it.

This avoids creating a brand-new Powens user on every run.
"""

from __future__ import annotations

import json
from pathlib import Path

from pypowens import PowensClient

from .config import Settings


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save_state(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))
    try:
        path.chmod(0o600)  # tokens are sensitive
    except OSError:
        pass


async def bootstrap_client(settings: Settings) -> PowensClient:
    """Return an authenticated :class:`PowensClient` (token resolved by priority)."""
    client = PowensClient(
        settings.domain,
        client_id=settings.client_id,
        client_secret=settings.client_secret,
    )

    # 1. Explicit token from the environment wins.
    if settings.access_token:
        client.access_token = settings.access_token
        return client

    # 2. Persisted state.
    state = _load_state(settings.state_path)
    if state.get("access_token"):
        client.access_token = state["access_token"]
        return client

    # 3. Create a new user and persist the token.
    token = await client.create_user()
    _save_state(
        settings.state_path,
        {"id_user": token.id_user, "access_token": token.access_token},
    )
    return client
