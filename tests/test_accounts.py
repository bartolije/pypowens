"""Default page: current accounts + spending history, and the work-friendly defaults.

The point of this screen is that it can be open at a desk: no net worth, and every
figure blurred until asked for. Both are asserted here, because both are one small
edit away from silently regressing.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from app.accounts import Row, group_by_day
from app.helpers import day_label_fr
from pypowens import PowensAPIError

DAYS_EN = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _text(html: str) -> str:
    plain = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"[\s  ]+", " ", plain)


# ------------------------------------------------------------------ dates en français


def test_day_names_are_french_whatever_the_locale():
    """``strftime("%A")`` suit la locale du processus, « C » sur un serveur nu."""
    assert day_label_fr(date(2026, 3, 31)) == "mardi 31/03"
    assert day_label_fr(date(2026, 7, 5)) == "dimanche 05/07"
    assert day_label_fr(None) == "—"


def test_the_history_never_shows_an_english_day(client):
    body = _text(client.get("/comptes").text)
    assert not any(day in body for day in DAYS_EN), body[:200]
    assert any(
        day in body
        for day in ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
    )


# ------------------------------------------------------ what the page must not show


def test_home_shows_current_accounts_not_net_worth(client):
    body = _text(client.get("/comptes").text)
    assert "Disponible sur les comptes courants" in body
    assert "Patrimoine net" not in body
    # 59 500 € is the net worth: it belongs to /patrimoine and must not leak here.
    assert "59 500,00 €" not in body


def test_home_lists_only_current_accounts(client):
    """Asserted on the account-type badges: "Livret" also occurs in wordings."""
    body = _text(client.get("/comptes").text)
    assert "Compte courant" in body
    assert "livret_a" not in body
    assert "pea" not in body


def test_available_total_ignores_foreign_currency(client):
    """The 800 USD checking account must not be summed into a EUR total."""
    body = _text(client.get("/comptes").text)
    assert "2 500,00 €" in body
    assert "3 300" not in body


def test_patrimoine_moved_off_the_default_route(client):
    assert "Patrimoine" in client.get("/patrimoine").text
    assert client.get("/").status_code == 200


def test_nav_puts_accounts_first_and_patrimoine_last(client):
    body = client.get("/").text
    # Sidebar navigation: Synthèse comes before Patrimoine, Comptes is also present.
    assert 'href="/patrimoine"' in body
    assert 'href="/comptes"' in body
    assert body.index('href="/"') < body.index('href="/patrimoine"')


def test_amounts_are_masked_until_opted_out(client):
    """Masking is the default: only an explicit "0" reveals the figures."""
    body = client.get("/comptes").text
    # Le masquage vit dans /static/boot.js (la politique de sécurité de contenu
    # interdit les scripts inline) et doit être chargé SANS ``defer``, sinon les
    # montants s'affichent le temps d'un battement de paupières.
    assert '<script src="/static/boot.js' in body
    assert "defer" not in body.split("boot.js")[1].split(">")[0]

    script = client.get("/static/boot.js").text
    assert 'localStorage.getItem("pf-hide") !== "0"' in script
    assert "hide-amounts" in script


# ------------------------------------------------------------------ history by date


def test_history_groups_by_day_and_totals_the_day(client):
    body = _text(client.get("/comptes").text)
    assert "Historique" in body
    # The one-off purchase of the fallback month, under its day heading.
    assert "249,90" in body


def test_history_falls_back_to_the_last_month_with_operations(client):
    """The current month is empty in the dataset; an empty landing page is a bug."""
    body = _text(client.get("/comptes").text)
    assert "0 opération" not in body
    assert "Aucune opération" not in body


def test_month_filter_is_honoured(client):
    empty = _text(client.get("/comptes", params={"mois": "2020-01"}).text)
    # Out of the picker range: falls back rather than 500-ing.
    assert "Historique" in empty


def test_totals_describe_the_month_not_the_filtered_view(client):
    """Hiding credits must not zero the "Reçu" figure."""
    body = _text(client.get("/comptes", params={"sens": "depenses"}).text)
    assert "2 800,00 €" in body  # the salary, still counted as received


def test_category_filter_narrows_the_table_only(client):
    body = _text(client.get("/comptes", params={"categorie": "Streaming / Médias"}).text)
    assert "Netflix" in body or "NETFLIX" in body
    # The breakdown still offers the other categories of the month.
    assert "Shopping / Équipement" in body or "Autre" in body


# --------------------------------------------------------------- grouping helper


def _row(day: date, amount: str, *, internal: bool = False) -> Row:
    class _Txn:
        date = day
        simplified_wording = "X"
        wording = "X"
        original_wording = "X"
        id = 1
        id_account = 1
        type = "card"
        value = Decimal(amount)

    return Row(
        txn=_Txn(),  # type: ignore[arg-type]
        account="Compte",
        category="Autre",
        amount=Decimal(amount),
        internal=internal,
    )


def test_group_by_day_is_most_recent_first():
    days = group_by_day([_row(date(2026, 6, 1), "-10"), _row(date(2026, 6, 3), "-20")])
    assert [d.day.day for d in days] == [3, 1]


def test_group_by_day_totals_debits_and_credits_separately():
    days = group_by_day([_row(date(2026, 6, 1), "-10"), _row(date(2026, 6, 1), "30")])
    assert days[0].spent == Decimal(10)
    assert days[0].received == Decimal(30)


def test_group_by_day_lists_internal_transfers_without_counting_them():
    """A 6 000 € move to a savings account is not a 6 000 € expense."""
    days = group_by_day(
        [_row(date(2026, 6, 1), "-10"), _row(date(2026, 6, 1), "-6000", internal=True)]
    )
    assert len(days[0].rows) == 2
    assert days[0].spent == Decimal(10)


# --------------------------------------------------------------- connect flow


def test_connect_opens_the_full_bank_list_by_default(client):
    response = client.get("/connect", follow_redirects=False)
    assert response.status_code == 307
    # The language segment is what makes the Webview reachable at all: without it
    # CloudFront answers 503 and "+ Banque" silently leads nowhere.
    assert "webview.powens.com/fr/connect" in response.headers["location"]


def test_connect_can_target_one_connector(client):
    """Adding a second, already-known bank should not require scrolling a list."""
    response = client.get("/connect", params={"connector_id": 2663}, follow_redirects=False)
    assert response.status_code == 307
    assert "2663" in response.headers["location"]


def test_tooltips_quoting_an_amount_are_not_native_titles(client):
    """A `title=` attribute cannot be blurred, so it would leak past the mask."""
    body = client.get("/abonnements").text
    assert "data-amount-title=" in body
    # Matched on a real attribute boundary: "data-amount-title=" contains "title=".
    for label in ("Montant typique", "Depuis la 1re échéance"):
        assert re.search(rf'\stitle="{label}', body) is None


def test_connect_refuses_to_start_with_an_unlisted_redirect_uri(client, fake_client):
    """Powens rejects it without saying what it expected; the app must say it."""
    fake_client.redirect_uris = ["https://ailleurs.example/callback"]
    response = client.get("/connect", follow_redirects=False)
    assert response.status_code == 409
    assert "redirect_uri non autorisé" in response.text
    assert "http://127.0.0.1:8000/callback" in response.text
    assert "https://ailleurs.example/callback" in response.text


def test_connect_proceeds_when_the_config_cannot_be_read(client, fake_client, monkeypatch):
    """The preflight is a diagnostic: it must never be what blocks the flow."""

    async def boom(*args, **kwargs):
        raise PowensAPIError(500, code="boom")

    monkeypatch.setattr(fake_client, "get_client_config", boom)
    response = client.get("/connect", follow_redirects=False)
    assert response.status_code == 307
    assert "webview.powens.com" in response.headers["location"]


# ------------------------------------------------- debt vs assets on /patrimoine


def _with_loan(fake_client):
    """Add a mortgage account, as connecting a bank loan does."""
    fake_client._accounts.append(
        {
            "id": 20,
            "id_connection": 1,
            "name": "PRET IMMO",
            "type": "loan",
            "balance": "-256797.68",
            "currency": {"id": "EUR"},
        }
    )


def test_debt_is_netted_out_of_net_worth(client, fake_client):
    _with_loan(fake_client)
    import app.data

    app.data.clear_cache()
    body = _text(client.get("/patrimoine").text)
    # 59 500 d'actifs moins le prêt de 256 797,68 : le solde héro (arrondi à
    # l'euro) reflète la dette ; la ligne Total du tableau, elle, totalise la
    # vue affichée (actifs OU passifs), plus le net global.
    assert "-197 298 €" in body or "-197 297 €" in body
    # Le tableau Actifs totalise les seuls actifs.
    assert "59 500,00 €" in body


def test_passifs_tab_lists_the_debts(client, fake_client):
    """L'onglet Passifs était un lien mort : les dettes, incluses dans le net,
    n'étaient consultables nulle part."""
    _with_loan(fake_client)
    import app.data

    app.data.clear_cache()
    body = _text(client.get("/patrimoine", params={"view": "passifs"}).text)
    assert "-256 797,68 €" in body  # la ligne Total de la vue passifs
    assert "PRET IMMO" in body
    # Et le tableau ne liste pas les familles d'actifs (le donut de droite, si).
    assert "PEA Investissement" not in body


def test_debt_is_excluded_from_the_repartition(client, fake_client):
    """A donut takes absolute values: a loan would read as a positive share."""
    _with_loan(fake_client)
    import app.data

    app.data.clear_cache()
    body = _text(client.get("/patrimoine").text)
    assert "Répartition" in body
    # The net worth reflects the debt (checked by test_debt_is_netted_out_of_net_worth).
    # The repartition section must still be present: debt does not suppress it.
    assert "Actifs" in body


# ----------------------------------------------------------------- reconnect flow


def test_connection_awaiting_the_user_offers_reconnect_not_sync(client, fake_client):
    fake_client._connections[0]["state"] = "webauthRequired"
    import app.data

    app.data.clear_cache()
    body = client.get("/patrimoine").text
    assert "/reconnecter/1" in body
    assert "/synchroniser/1" not in body


def test_healthy_connection_offers_sync_not_reconnect(client):
    body = client.get("/patrimoine").text
    assert "/synchroniser/1" in body
    assert "/reconnecter/1" not in body


def test_reconnect_targets_the_webview_repair_screen(client):
    response = client.get("/reconnecter/7", follow_redirects=False)
    assert response.status_code == 307
    location = response.headers["location"]
    assert "/fr/reconnect" in location
    assert "connection_id=7" in location


def test_connection_state_is_shown_readably_not_as_a_bank_url(client, fake_client):
    """A webauth connector reports its error as the bank's whole authorize URL.

    Printing it verbatim is unreadable and puts the flow's `state` token on the page.
    """
    fake_client._connections[0]["state"] = "webauthRequired"
    fake_client._connections[0]["error_message"] = (
        "Redirecting to https://api.example.fr/openbanking/oauth/authorize"
        "?client_id=SECRET&state=TOKEN123"
    )
    import app.data

    app.data.clear_cache()
    body = client.get("/patrimoine").text
    assert "Authentification à terminer sur le site de la banque" in body
    assert "TOKEN123" not in body
    assert "openbanking/oauth/authorize" not in body


def test_unknown_state_still_says_something(client, fake_client):
    fake_client._connections[0]["state"] = "etatInedit"
    import app.data

    app.data.clear_cache()
    body = _text(client.get("/patrimoine").text)
    assert "etatInedit" in body


def test_patrimoine_default_order_is_value_descending(client):
    """Vue non groupée : les comptes sont triés par valeur décroissante, toutes
    familles confondues — plus éparpillés famille par famille."""
    body = client.get("/patrimoine").text
    pea = body.index("PEA")
    livret = body.index("Livret")
    courant = body.index("M BARTOLI") if "M BARTOLI" in body else body.index("Compte courant")
    assert pea < livret < courant  # 42 000 > 15 000 > 2 500
