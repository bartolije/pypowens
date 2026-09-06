"""Page « Réglages » : ce qui se pilotait par .env devient modifiable.

Le point délicat n'est pas le formulaire, c'est la précédence : la base
l'emporte sur l'environnement, un champ vidé rend la main au défaut, et une
valeur illisible ne doit jamais empêcher l'application de démarrer.
"""

from __future__ import annotations

import html
import re

from app.config import Settings, apply_overrides


def _text(markup: str) -> str:
    # Déséchapper : Jinja rend les apostrophes en &#39;, et les libellés
    # français en sont pleins (« Fenêtre d'historique »).
    plain = re.sub(r"<[^>]+>", " ", html.unescape(markup))
    return re.sub(r"[\s  ]+", " ", plain)


def _settings(**kw) -> Settings:
    base = dict(domain="d", client_id=None, client_secret=None, access_token=None)
    base.update(kw)
    return Settings(**base)


# ------------------------------------------------------------- précédence


def test_database_overrides_win_over_the_environment():
    settings = _settings(base_currency="EUR", history_months=36)
    merged = apply_overrides(settings, {"base_currency": "chf", "history_months": "12"})
    assert merged.base_currency == "CHF"  # normalisé en majuscules
    assert merged.history_months == 12


def test_empty_or_unreadable_values_fall_back_to_the_default():
    settings = _settings(history_months=36, silent_after_days=3)
    merged = apply_overrides(settings, {"history_months": "", "silent_after_days": "beaucoup"})
    assert merged.history_months == 36
    assert merged.silent_after_days == 3


def test_absurd_values_are_clamped_not_accepted():
    """Une fenêtre de 9 999 mois téléchargerait tout l'historique Powens."""
    merged = apply_overrides(_settings(), {"history_months": "9999", "silent_after_days": "0"})
    assert merged.history_months == 120
    assert merged.silent_after_days == 1


def test_secrets_are_never_overridable():
    from app.config import OVERRIDABLE

    for secret in ("client_id", "client_secret", "access_token", "domain", "db_path"):
        assert secret not in OVERRIDABLE


# ------------------------------------------------------------------ page


def test_page_lists_every_overridable_setting(client):
    body = _text(client.get("/reglages").text)
    assert "Devise de référence" in body
    assert "Fenêtre d'historique" in body
    assert "Indice de comparaison" in body


def test_saving_a_setting_applies_it_immediately(client):
    posted = client.post(
        "/reglages",
        data={
            "history_months": "18",
            "base_currency": "EUR",
            "silent_after_days": "5",
            "benchmark_ticker": "IWDA.AS",
            "benchmark_label": "MSCI World",
        },
        follow_redirects=False,
    )
    assert posted.status_code == 303

    import app.main

    assert app.main.app.state.settings.history_months == 18
    assert app.main.app.state.settings.silent_after_days == 5
    # Et la page le montre comme personnalisé.
    assert "personnalisé" in client.get("/reglages").text

    # Vider le champ rend la main au .env.
    client.post(
        "/reglages",
        data={
            "history_months": "",
            "base_currency": "EUR",
            "silent_after_days": "3",
            "benchmark_ticker": "IWDA.AS",
            "benchmark_label": "MSCI World",
        },
    )
    assert app.main.app.state.settings.history_months == 36


def test_remembered_decisions_can_be_forgotten(client):
    """Catégories forcées, renommages, fusions et budgets se révoquent ici."""
    client.post("/categorie", data={"label": "NETFLIX.COM", "category": "Sport", "back": "/"})
    assert "NETFLIX.COM" in client.get("/reglages").text

    client.post("/reglages/oublier", data={"quoi": "categorie", "label": "NETFLIX.COM"})
    body = client.get("/reglages").text
    assert "Aucune correction" in body


def test_settings_page_is_reachable_from_the_sidebar(client):
    assert 'href="/reglages"' in client.get("/comptes").text
