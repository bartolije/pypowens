"""Pure presentation helpers: formatting + dependency-free inline SVG charts.

No app imports here (safe to use everywhere). SVG is generated server-side so the
page stays self-contained (no JS charting lib).
"""

from __future__ import annotations

import html
from decimal import Decimal
from datetime import date

# Colour-blind-friendly categorical palette (used across charts for consistency).
PALETTE = [
    "#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#b07aa1",
    "#76b7b2", "#edc948", "#ff9da7", "#9c755f", "#bab0ac",
]

Number = int | float | Decimal | None


def format_money(value: Number, currency: str = "€", precision: int = 2) -> str:
    """French-formatted amount: ``-1 234,56 €``."""
    if value is None:
        return "—"
    value = float(value)
    neg = value < 0
    s = f"{abs(value):,.{precision}f}".replace(",", " ").replace(".", ",")
    return f"{'-' if neg else ''}{s} {currency}"


def mask_iban(iban: str | None) -> str:
    if not iban:
        return "—"
    iban = iban.replace(" ", "")
    if len(iban) <= 8:
        return iban
    return f"{iban[:4]} •••• {iban[-4:]}"


def month_key(d: date | None) -> str:
    return d.strftime("%Y-%m") if d else "?"


def month_label_fr(key: str) -> str:
    months = [
        "janv.", "févr.", "mars", "avr.", "mai", "juin",
        "juil.", "août", "sept.", "oct.", "nov.", "déc.",
    ]
    try:
        y, m = key.split("-")
        return f"{months[int(m) - 1]} {y[2:]}"
    except (ValueError, IndexError):
        return key


def _e(s: object) -> str:
    return html.escape(str(s))


def bar_chart(
    items: list[tuple[str, float]],
    *,
    width: int = 520,
    bar_height: int = 22,
    gap: int = 8,
    unit: str = "€",
    color: str = "#4e79a7",
) -> str:
    """Horizontal bar chart. ``items`` = list of (label, value)."""
    if not items:
        return '<p class="muted">Aucune donnée.</p>'
    max_val = max((abs(v) for _, v in items), default=1) or 1
    label_w = 150
    bar_area = width - label_w - 90
    rows = []
    for i, (label, value) in enumerate(items):
        y = i * (bar_height + gap)
        w = max(2, int(bar_area * abs(value) / max_val))
        rows.append(
            f'<text x="0" y="{y + bar_height * 0.7}" class="cl">{_e(label)}</text>'
            f'<rect x="{label_w}" y="{y}" width="{w}" height="{bar_height}" '
            f'rx="4" fill="{color}"><title>{_e(label)}: {value:,.2f} {unit}</title></rect>'
            f'<text x="{label_w + w + 6}" y="{y + bar_height * 0.7}" class="cv">'
            f'{value:,.0f} {unit}</text>'
        )
    height = len(items) * (bar_height + gap)
    return (
        f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
        f'width="100%" preserveAspectRatio="xMinYMin meet">{"".join(rows)}</svg>'
    )


def donut_chart(
    items: list[tuple[str, float]],
    *,
    size: int = 220,
    unit: str = "€",
) -> str:
    """Donut chart with legend. ``items`` = list of (label, value)."""
    items = [(lbl, abs(float(v))) for lbl, v in items if v]
    total = sum(v for _, v in items)
    if total <= 0:
        return '<p class="muted">Aucune donnée.</p>'
    cx = cy = size / 2
    r = size / 2 - 4
    inner = r * 0.58
    import math

    segments = []
    legend = []
    angle = -math.pi / 2
    for i, (label, value) in enumerate(items):
        frac = value / total
        end = angle + frac * 2 * math.pi
        large = 1 if frac > 0.5 else 0
        x1, y1 = cx + r * math.cos(angle), cy + r * math.sin(angle)
        x2, y2 = cx + r * math.cos(end), cy + r * math.sin(end)
        color = PALETTE[i % len(PALETTE)]
        segments.append(
            f'<path d="M {cx} {cy} L {x1:.2f} {y1:.2f} '
            f'A {r} {r} 0 {large} 1 {x2:.2f} {y2:.2f} Z" fill="{color}">'
            f'<title>{_e(label)}: {value:,.2f} {unit} ({frac * 100:.0f}%)</title></path>'
        )
        legend.append(
            f'<li><span class="dot" style="background:{color}"></span>'
            f'{_e(label)} <b>{frac * 100:.0f}%</b> '
            f'<span class="muted">{value:,.0f} {unit}</span></li>'
        )
        angle = end
    svg = (
        f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
        f'role="img">{"".join(segments)}'
        f'<circle cx="{cx}" cy="{cy}" r="{inner}" class="donut-hole"/></svg>'
    )
    return f'<div class="donut">{svg}<ul class="legend">{"".join(legend)}</ul></div>'
