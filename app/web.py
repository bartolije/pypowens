"""Shared Jinja2 templates environment + filters."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from .helpers import format_money, mask_iban, month_label_fr

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
templates.env.filters["money"] = format_money
templates.env.filters["iban"] = mask_iban
templates.env.filters["monthlabel"] = month_label_fr
