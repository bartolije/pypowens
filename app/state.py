"""Token bootstrap and local persistence.

Priority for obtaining an access token:
1. ``POWENS_ACCESS_TOKEN`` from the environment / .env (user-provided token);
2. a previously persisted ``.powens_state.json``;
3. otherwise ``create_user()`` (needs client_id/secret) and persist it.

This avoids creating a brand-new Powens user on every run.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from pypowens import PowensAPIError, PowensClient

from .config import Settings

_log = logging.getLogger(__name__)


class StateFileError(RuntimeError):
    """``.powens_state.json`` existe mais est illisible.

    Levée plutôt qu'avalée : un état corrompu traité comme « pas d'état » ferait
    créer un nouvel utilisateur Powens, orphelinant toutes les connexions
    bancaires déjà branchées — silencieusement.
    """


def _load_state(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise StateFileError(
            f"{path} existe mais n'est pas un JSON valide. Il contenait probablement "
            "l'id_user et le token d'un utilisateur Powens déjà branché : repartir de "
            "zéro créerait un nouvel utilisateur et perdrait toutes les connexions. "
            "Restaurer ou supprimer explicitement ce fichier avant de relancer."
        ) from exc
    if not isinstance(data, dict):
        raise StateFileError(f"{path} ne contient pas un objet JSON — même consigne.")
    return data


def _save_state(path: Path, data: dict[str, Any]) -> None:
    # Écriture atomique : ``write_text`` tronque puis écrit, donc un crash au
    # milieu laisserait un JSON tronqué que le prochain démarrage prendrait pour
    # un état corrompu. ``mkstemp`` crée par ailleurs le fichier en 0600 dès le
    # départ — pas de fenêtre où le token serait lisible par d'autres comptes.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data, indent=2))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


async def bootstrap_client(settings: Settings) -> PowensClient:
    """Return an authenticated :class:`PowensClient` (token resolved by priority)."""
    client = PowensClient(
        settings.domain,
        client_id=settings.client_id,
        client_secret=settings.client_secret,
    )

    # 1. Explicit token from the environment wins.
    if settings.access_token:
        _log.info("token repris de POWENS_ACCESS_TOKEN (.env)")
        client.access_token = settings.access_token
        return client

    # 2. Persisted state.
    state = _load_state(settings.state_path)
    if state.get("access_token"):
        _log.info("token repris de %s", settings.state_path)
        client.access_token = state["access_token"]
        return client

    # 3. Create a new user and persist the token.
    token = await client.create_user()
    _log.warning(
        "aucun token existant : NOUVEL utilisateur Powens créé (id_user=%s), "
        "persisté dans %s",
        token.id_user,
        settings.state_path,
    )
    _save_state(
        settings.state_path,
        {"id_user": token.id_user, "access_token": token.access_token},
    )
    return client


def persist_token(settings: Settings, *, access_token: str, id_user: int | None) -> None:
    """Écrit le token courant dans l'état local (échange de code, renouvellement).

    Sans cette écriture, un token obtenu via ``exchange_code`` ne survivait pas au
    redémarrage. L'``id_user`` connu est conservé si le nouveau n'est pas fourni.
    """
    try:
        current = _load_state(settings.state_path)
    except StateFileError:
        current = {}
    _save_state(
        settings.state_path,
        {
            "id_user": id_user if id_user is not None else current.get("id_user"),
            "access_token": access_token,
        },
    )


async def try_renew(client: PowensClient, settings: Settings) -> bool:
    """Attempt to mint a fresh token for the known user after a 401/403.

    Needs ``client_id``/``client_secret`` plus a persisted ``id_user`` (or one
    reachable from the current token). Returns ``True`` when the client now holds
    a new token. Never raises: a failed renewal just means "ask the user".
    """
    if not (settings.client_id and settings.client_secret):
        return False

    try:
        id_user = _load_state(settings.state_path).get("id_user")
    except StateFileError:
        _log.exception("état local illisible — renouvellement impossible")
        return False
    if id_user is None:
        return False

    try:
        token = await client.renew_token(int(id_user))
    except (PowensAPIError, OSError, ValueError, TypeError):
        _log.warning("renouvellement du token refusé pour id_user=%s", id_user)
        return False
    if not token.access_token:
        return False

    _log.info("token renouvelé pour id_user=%s", id_user)
    _save_state(
        settings.state_path,
        {"id_user": token.id_user or id_user, "access_token": token.access_token},
    )
    return True
