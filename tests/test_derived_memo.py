"""Mémo des calculs dérivés du cache (data.derived) et ses effets sur les pages."""

from __future__ import annotations

import app.data as data


def test_derived_recomputes_only_when_the_cache_generation_moves():
    data.clear_cache()
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return {"n": calls["n"]}

    assert data.derived(("k",), compute) == {"n": 1}
    assert data.derived(("k",), compute) == {"n": 1}, "même génération : pas de recalcul"
    data._set("anything", object())
    assert data.derived(("k",), compute) == {"n": 2}, "une écriture invalide"
    data.clear_cache()
    assert data.derived(("k",), compute) == {"n": 3}, "un vidage invalide"
    assert data.derived(("autre",), compute) == {"n": 4}, "clé différente : calcul distinct"


def test_derived_copy_protects_the_memo_from_callers():
    data.clear_cache()
    first = data.derived(("liste",), lambda: [{"a": 1}], copy=True)
    first[0]["a"] = 99
    assert data.derived(("liste",), lambda: [{"a": 1}], copy=True) == [{"a": 1}]


def test_derived_memo_stays_bounded():
    data.clear_cache()
    for i in range(data._DERIVED_MAX + 10):
        data.derived(("k", i), lambda i=i: i)
        data._set("bump", i)  # chaque entrée appartient à une génération passée
    assert len(data._derived) <= data._DERIVED_MAX + 1


def test_subscriptions_are_detected_once_per_cache_generation(client, monkeypatch):
    from app import recurring

    calls = {"n": 0}
    original = recurring.detect_recurring

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(recurring, "detect_recurring", counting)
    assert client.get("/abonnements").status_code == 200
    assert client.get("/abonnements").status_code == 200
    assert calls["n"] == 1, "seconde visite servie par le mémo"

    # Une correction de catégorie vide le cache : la détection reprend.
    response = client.post(
        "/categorie",
        data={"label": "NETFLIX.COM", "category": "Streaming / Médias", "back": "/abonnements"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert client.get("/abonnements").status_code == 200
    assert calls["n"] == 2


def test_performance_reads_the_valuation_table_once(client, monkeypatch):
    from app import store

    calls = {"n": 0}
    original = store.investment_values

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "investment_values", counting)
    assert client.get("/performance").status_code == 200
    assert calls["n"] == 1
