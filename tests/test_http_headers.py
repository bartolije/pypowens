"""En-têtes HTTP : cache des statiques, pages jamais stockées, compression, sécurité."""

from __future__ import annotations


def test_versioned_static_assets_are_cached_for_a_year(client):
    response = client.get("/static/style.css?v=123")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    unversioned = client.get("/static/style.css")
    assert "immutable" not in unversioned.headers.get("cache-control", "")


def test_html_pages_are_never_stored_and_carry_security_headers(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"


def test_pages_are_gzipped_when_the_browser_accepts_it(client):
    response = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert response.headers.get("content-encoding") == "gzip"
    assert "Powens Finance" in response.text  # transparently decoded


def test_openapi_documentation_is_not_exposed(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_the_health_probe_never_touches_powens(client, monkeypatch):
    """La sonde de l'hébergeur passe toutes les 30 s : elle ne doit pas coûter un
    appel Powens (bandeau de santé) ni figurer parmi les pages à bandeau."""
    import app.main as main

    async def _boom(*args, **kwargs):
        raise AssertionError("connection_alerts appelé depuis /health")

    monkeypatch.setattr(main, "connection_alerts", _boom)
    assert client.get("/health").text == "ok"
    assert client.get("/favicon.ico", follow_redirects=False).status_code == 301
