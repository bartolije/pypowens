"""Accessibilité et macros de templates.

Chaque assertion ici verrouille un manque relevé par l'audit : graphiques
muets aux lecteurs d'écran, tri inatteignable au clavier, focus invisible,
et sept constructions manuelles de query string aux variantes divergentes.
"""

from __future__ import annotations

from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def test_svg_titles_are_preserved_not_removed():
    """Le <title> d'une forme SVG est son nom accessible : le supprimer rendait
    les graphiques muets. L'infobulle native est neutralisée en CSS."""
    js = (_STATIC / "app.js").read_text()
    assert "t.remove()" not in js
    css = (_STATIC / "style.css").read_text()
    assert ".chart title, .donut title { pointer-events: none; }" in css


def test_sortable_headers_are_keyboard_reachable():
    js = (_STATIC / "app.js").read_text()
    assert "th.tabIndex = 0" in js
    assert 'aria-sort' in js
    assert '"keydown"' in js


def test_clickable_rows_are_focusable_and_activable():
    js = (_STATIC / "app.js").read_text()
    assert "initClickableRows" in js
    assert 'tr.setAttribute("role", "link")' in js


def test_focus_visible_and_reduced_motion_are_styled():
    css = (_STATIC / "style.css").read_text()
    assert ":focus-visible" in css
    assert "prefers-reduced-motion" in css


def test_muted_text_meets_contrast_floor():
    """#555 sur #0d0d0d valait 3,2:1, sous le seuil AA de 4,5:1."""
    css = (_STATIC / "style.css").read_text()
    assert "--text-muted: #555555" not in css

    def _luminance(hex_color: str) -> float:
        channels = []
        for i in (0, 2, 4):
            c = int(hex_color[i : i + 2], 16) / 255
            channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
        r, g, b = channels
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    muted = css.split("--text-muted: #")[1][:6]
    page = css.split("--bg-page: #")[1][:6]
    lighter, darker = sorted((_luminance(muted), _luminance(page)), reverse=True)
    ratio = (lighter + 0.05) / (darker + 0.05)
    assert ratio >= 4.5, f"contraste {ratio:.2f}:1 sous le seuil AA"


def test_skip_link_and_main_landmark(client):
    body = client.get("/comptes").text
    assert 'class="skip-link"' in body
    assert 'href="#contenu"' in body
    assert '<main class="content" id="contenu">' in body


def test_mask_toggle_announces_its_state(client):
    body = client.get("/comptes").text
    assert 'aria-pressed="false"' in body
    assert 'aria-label="Masquer ou afficher les montants"' in body


def test_decorative_icons_are_hidden_from_screen_readers(client):
    body = client.get("/comptes").text
    # Les icônes de navigation sont décoratives : le libellé texte suffit.
    assert body.count('aria-hidden="true"') >= 8


# ------------------------------------------------------------------- macros

def test_period_pills_preserve_filters_and_mark_the_current_one(client):
    body = client.get("/patrimoine", params={"type": "Épargne", "group": "1"}).text
    # Le filtre et le groupement survivent à un changement de période…
    assert "type=%C3%89pargne&amp;group=1&amp;period=3m" in body
    # …et la période active est annoncée.
    assert 'aria-current="true"' in body


def test_filter_pills_toggle_without_losing_the_other_filters(client):
    body = client.get(
        "/patrimoine", params={"type": "Épargne", "institution": "Ma Banque"}
    ).text
    # Retirer le type garde l'établissement (et inversement) ; Jinja encode
    # l'espace en %20 (urlencode de Werkzeug, pas quote_plus).
    assert "/patrimoine?institution=Ma%20Banque&amp;period=tout" in body
    assert "/patrimoine?type=%C3%89pargne&amp;period=tout" in body
    assert 'aria-pressed="true"' in body


def test_qs_macro_omits_empty_values(client):
    """Aucun ?type=&institution= parasite quand rien n'est filtré."""
    body = client.get("/patrimoine").text
    assert "type=&" not in body
    assert "institution=&" not in body
    assert "?&" not in body


# ------------------------------------------- architecture de la navigation

def test_sidebar_keeps_only_the_consultation_pages(client):
    """Administration (connexions, import, réglages) hors du bloc quotidien."""
    body = client.get("/comptes").text
    sidebar = body[body.index('class="sidebar-nav"'):body.index("</nav>")]
    for page in ("/", "/patrimoine", "/comptes", "/analyse", "/abonnements", "/performance"):
        assert f'href="{page}"' in sidebar, page
    for admin in ("/connexions", "/import", "/recurrences"):
        assert f'href="{admin}"' not in sidebar, admin


def test_bank_accounts_live_in_the_top_right_corner(client):
    body = client.get("/comptes").text
    topbar = body[body.index('class="topbar-right"'):body.index("</header>")]
    assert 'href="/connexions"' in topbar
    assert "Mes comptes bancaires" in topbar


def test_recurrences_stays_reachable_from_subscriptions(client):
    assert 'href="/recurrences"' in client.get("/abonnements").text


def test_import_stays_reachable_from_connections(client):
    assert 'href="/import"' in client.get("/connexions").text
