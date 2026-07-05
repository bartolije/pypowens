"""Shared Jinja2 templates environment + filters."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from .helpers import format_money, mask_iban, month_label_fr

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _money_html(value, currency: str = "€", precision: int = 2) -> Markup:
    """Money filter used in templates: wraps the amount so it can be masked
    globally (``.amount``) without touching charts (which call format_money directly)."""
    return Markup(f'<span class="amount">{format_money(value, currency, precision)}</span>')


templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
templates.env.filters["money"] = _money_html
templates.env.filters["iban"] = mask_iban
templates.env.filters["monthlabel"] = month_label_fr
