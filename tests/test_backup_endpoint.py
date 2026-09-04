"""GET /sauvegarde.db : copie cohérente de la base, téléchargeable."""

from __future__ import annotations

import sqlite3


def test_backup_download_is_a_consistent_sqlite_copy(client, tmp_path):
    from app import store

    store.pin_account(client.app.state.store, "iban:FR76TEST", "Compte témoin")

    response = client.get("/sauvegarde.db")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.sqlite3")
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="powens_finance-2026-06-15.db"'
    )
    assert response.headers["cache-control"] == "private, no-store"
    assert response.content.startswith(b"SQLite format 3\x00")

    copy = tmp_path / "copie.db"
    copy.write_bytes(response.content)
    conn = sqlite3.connect(copy)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT name FROM account_pin").fetchone()[0] == "Compte témoin"
    finally:
        conn.close()


def test_backup_download_requires_the_credentials_when_configured(client, monkeypatch):
    from app import auth

    monkeypatch.setenv("APP_AUTH_USER", "moi")
    monkeypatch.setenv("APP_AUTH_PASSWORD", "secret")
    auth.reset_failures()
    assert client.get("/sauvegarde.db").status_code == 401
    assert client.get("/sauvegarde.db", auth=("moi", "secret")).status_code == 200
