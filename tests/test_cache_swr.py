"""Cache « périmé servi, rafraîchi en fond » (stale-while-revalidate).

Une entrée dont le TTL est dépassé était rechargée EN LIGNE : la page attendait
Powens. Elle est désormais rendue telle quelle et remplacée par une tâche de
fond ; seule une entrée périmée depuis plus de STALE_GRACE redevient bloquante.
"""

from __future__ import annotations

import asyncio
import logging

import app.data as data


def _age(key: str, seconds: float) -> None:
    """Vieillit artificiellement une entrée (l'horloge des tests est figée)."""
    stored, value = data._cache[key]
    data._cache[key] = (stored - seconds, value)


async def _settle() -> None:
    """Laisse les rafraîchissements de fond se terminer."""
    for _ in range(10):
        await asyncio.sleep(0)
    if data._background:
        await asyncio.gather(*data._background, return_exceptions=True)


def _count(fake_client, name: str) -> dict[str, int]:
    calls = {"n": 0}
    original = getattr(fake_client, name)

    async def wrapped(*args, **kwargs):
        calls["n"] += 1
        return await original(*args, **kwargs)

    setattr(fake_client, name, wrapped)
    return calls


def _new_connection(cid: int) -> dict:
    return {
        "id": cid,
        "id_connector": 99,
        "state": None,
        "error_message": None,
        "last_update": "2026-06-14 06:00:00",
        "connector": {"id": 99, "name": f"Banque {cid}"},
        "accounts": [],
    }


async def test_expired_entry_is_served_immediately_then_refreshed(fake_client):
    data.clear_cache()
    calls = _count(fake_client, "list_connections")
    first = await data.load_connections(fake_client, ttl=120)
    assert calls["n"] == 1

    _age("connections", 121)
    fake_client._connections.append(_new_connection(7))
    second = await data.load_connections(fake_client, ttl=120)
    assert second is first, "la valeur périmée est rendue sans attendre"
    assert "connections" in data._refreshing

    await _settle()
    third = await data.load_connections(fake_client, ttl=120)
    assert len(third) == len(first) + 1
    assert calls["n"] == 2
    assert not data._refreshing


async def test_refresh_failure_keeps_the_stale_value(fake_client, caplog):
    data.clear_cache()
    first = await data.load_connections(fake_client, ttl=120)
    _age("connections", 121)

    async def _broken(*args, **kwargs):
        raise RuntimeError("Powens indisponible")

    fake_client.list_connections = _broken
    with caplog.at_level(logging.WARNING, logger="app.data"):
        assert await data.load_connections(fake_client, ttl=120) is first
        await _settle()
        # Toujours périmée : une nouvelle tentative de fond est planifiée, et échoue aussi
        assert await data.load_connections(fake_client, ttl=120) is first
        await _settle()
    assert "valeur périmée conservée" in caplog.text
    assert not data._refreshing


async def test_beyond_the_grace_period_the_reload_blocks(fake_client):
    data.clear_cache()
    calls = _count(fake_client, "list_connections")
    first = await data.load_connections(fake_client, ttl=120)
    _age("connections", 120 + data.STALE_GRACE + 1)
    fake_client._connections.append(_new_connection(8))

    second = await data.load_connections(fake_client, ttl=120)
    assert len(second) == len(first) + 1, "trop vieux pour être servi : rechargé en ligne"
    assert calls["n"] == 2
    assert not data._refreshing


async def test_ttl_zero_always_reloads_synchronously(fake_client):
    data.clear_cache()
    calls = _count(fake_client, "list_connections")
    await data.load_connections(fake_client, ttl=120)
    await data.load_connections(fake_client, ttl=0)
    assert calls["n"] == 2
    assert not data._refreshing


async def test_concurrent_stale_hits_refresh_once(fake_client):
    data.clear_cache()
    calls = _count(fake_client, "list_connections")
    first = await data.load_connections(fake_client, ttl=120)
    _age("connections", 121)

    results = await asyncio.gather(
        data.load_connections(fake_client, ttl=120),
        data.load_connections(fake_client, ttl=120),
        data.load_connections(fake_client, ttl=120),
    )
    assert all(r is first for r in results)
    await _settle()
    assert calls["n"] == 2, "un seul rafraîchissement pour trois lecteurs"


async def test_stale_history_keeps_the_widest_window_and_drops_derived_sets(fake_client):
    data.clear_cache()
    wide = await data.load_transactions(fake_client, months=24, ttl=300)
    await data.load_internal_ids(fake_client, months=24, ttl=300)
    assert "internal" in data._cache

    _age(data._TXN_KEY, 301)
    fake_client._txns.append(
        {
            "id": 4242,
            "id_account": 1,
            "date": "2026-06-10",
            "value": "-42.00",
            "type": "card",
            "wording": "NOUVELLE OPERATION",
            "simplified_wording": "NOUVELLE OPERATION",
            "original_wording": "NOUVELLE OPERATION",
            "coming": False,
        }
    )
    narrow = await data.load_transactions(fake_client, months=8, ttl=300)
    assert len(narrow) <= len(wide), "servi depuis l'historique périmé, filtré en mémoire"
    assert not any(t.id == 4242 for t in narrow)

    await _settle()
    assert data._cache[data._TXN_KEY][1][0] == 24, "la fenêtre large est conservée"
    assert "internal" not in data._cache, "le jeu dérivé est recalculé sur le nouvel historique"
    refreshed = await data.load_transactions(fake_client, months=8, ttl=300)
    assert any(t.id == 4242 for t in refreshed)


async def test_wider_window_than_cached_still_loads_inline(fake_client):
    data.clear_cache()
    await data.load_transactions(fake_client, months=8, ttl=300)
    _age(data._TXN_KEY, 301)
    await data.load_transactions(fake_client, months=24, ttl=300)
    assert data._cache[data._TXN_KEY][1][0] == 24
    assert not data._refreshing


async def test_warm_up_fills_every_cache(fake_client):
    data.clear_cache()
    await data.warm_up(fake_client, None, months=12)
    assert {"accounts", "connections", "investments", "transactions", "internal"} <= set(
        data._cache
    )
    data.clear_cache()
