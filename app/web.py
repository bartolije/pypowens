"""Shared Jinja2 templates environment + filters."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from .helpers import currency_symbol, day_label_fr, format_money, mask_iban, month_label_fr

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _money_html(value, currency: str = "€", precision: int = 2) -> Markup:
    """Money filter used in templates: wraps the amount in ``.amount`` so the
    global mask applies. ``currency`` accepts an ISO code (``"CHF"``) or a symbol."""
    symbol = currency_symbol(currency)
    return Markup(f'<span class="amount">{format_money(value, symbol, precision)}</span>')


def _static_version() -> int:
    """Version des statiques, dérivée du mtime de style.css.

    Servie en ``?v=`` sur les liens CSS : sans Cache-Control, les navigateurs
    gardent l'ancienne feuille en cache mémoire — un déploiement livrait alors
    du HTML neuf stylé par du CSS périmé (mise en page cassée jusqu'à un
    rechargement forcé).
    """
    try:
        return int((Path(__file__).resolve().parent / "static" / "style.css").stat().st_mtime)
    except OSError:
        return 0


templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
templates.env.filters["money"] = _money_html
templates.env.filters["iban"] = mask_iban
templates.env.filters["monthlabel"] = month_label_fr
templates.env.filters["daylabel"] = day_label_fr
templates.env.globals["static_v"] = _static_version()
