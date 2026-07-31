"""Pure presentation helpers: formatting + dependency-free inline SVG charts.

No app imports here (safe to use everywhere). SVG is generated server-side so the
page stays self-contained (no JS charting lib).
"""

from __future__ import annotations

import html
from datetime import date
from decimal import Decimal

# Colour-blind-friendly categorical palette (used across charts for consistency).
PALETTE = [
    "#635bff", "#0ca678", "#f28e2b", "#e15759", "#7c8bff",
    "#12b886", "#f59f00", "#ff8787", "#9775fa", "#868e96",
]

Number = int | float | Decimal | None

# Narrow no-break space (thousands) and no-break space (before currency): keep an
# amount on a single line so it never wraps inside a table cell.
_THIN_NBSP = " "
_NBSP = " "


_CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "JPY": "¥"}


def currency_symbol(code: str | None) -> str:
    """Map an ISO code to a symbol, passing through anything already symbolic.

    ``"EUR"`` -> ``"€"``, ``"CHF"`` -> ``"CHF"``, ``"€"`` -> ``"€"``, ``None`` -> ``"€"``.
    """
    if not code:
        return "€"
    code = code.strip()
    return _CURRENCY_SYMBOLS.get(code.upper(), code)


def format_money(value: Number, currency: str = "€", precision: int = 2) -> str:
    """French-formatted amount that never line-wraps: ``-1 234,56 €``."""
    if value is None:
        return "—"
    value = float(value)
    neg = value < 0
    s = f"{abs(value):,.{precision}f}".replace(",", _THIN_NBSP).replace(".", ",")
    return f"{'-' if neg else ''}{s}{_NBSP}{currency}"


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


# Les noms de jours sont écrits ici plutôt que laissés à ``strftime("%A")`` : celui-ci
# suit la locale du processus, qui vaut « C » sur un serveur lancé sans environnement —
# d'où des « Friday » dans une interface française. Un ``setlocale`` global serait à la
# fois plus fragile (dépend des locales installées) et pas thread-safe.
_DAYS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def day_label_fr(d: date | None) -> str:
    """``"mardi 31/03"`` — le jour de la semaine, en français, quelle que soit la locale."""
    if d is None:
        return "—"
    return f"{_DAYS_FR[d.weekday()]} {d.strftime('%d/%m')}"


def _e(s: object) -> str:
    return html.escape(str(s))


def bar_chart(
    items: list[tuple[str, float]],
    *,
    width: int = 520,
    bar_height: int = 22,
    gap: int = 8,
    unit: str = "€",
    color: str = "#635bff",
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
            f'rx="5" fill="{color}"><title>{_e(label)}: {value:,.2f} {unit}</title></rect>'
            f'<text x="{label_w + w + 6}" y="{y + bar_height * 0.7}" class="cv amount">'
            f'{value:,.0f} {unit}</text>'
        )
    height = len(items) * (bar_height + gap)
    return (
        f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
        f'width="100%" preserveAspectRatio="xMinYMin meet">{"".join(rows)}</svg>'
    )


def line_chart(
    items: list[tuple[str, float]],
    *,
    width: int = 720,
    height: int = 200,
    unit: str = "€",
    color: str = "#635bff",
) -> str:
    """Time series as an SVG area+line chart. ``items`` = list of (label, value).

    The Y axis is scaled to the data range (not forced to zero) so a net-worth
    curve shows its actual movement instead of a flat line at the top.
    """
    points = [(label, float(value)) for label, value in items if value is not None]
    if len(points) < 2:
        return '<p class="muted">Pas encore assez d\'historique — repassez demain.</p>'

    pad_l, pad_r, pad_t, pad_b = 8, 8, 12, 22
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    values = [v for _, v in points]
    lo, hi = min(values), max(values)
    span = (hi - lo) or (abs(hi) or 1)
    lo -= span * 0.12
    hi += span * 0.12
    span = hi - lo

    def _x(i: int) -> float:
        return pad_l + plot_w * i / (len(points) - 1)

    def _y(value: float) -> float:
        return pad_t + plot_h * (1 - (value - lo) / span)

    coords = [(_x(i), _y(v)) for i, (_, v) in enumerate(points)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = (
        f"{coords[0][0]:.1f},{pad_t + plot_h:.1f} "
        + line
        + f" {coords[-1][0]:.1f},{pad_t + plot_h:.1f}"
    )

    dots = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" fill="{color}">'
        f"<title>{_e(label)}: {value:,.2f} {unit}</title></circle>"
        for (x, y), (label, value) in zip(coords, points, strict=True)
    )
    # Only first / middle / last labels, otherwise the axis becomes unreadable.
    ticks = ""
    for i in (0, len(points) // 2, len(points) - 1):
        anchor = "start" if i == 0 else "end" if i == len(points) - 1 else "middle"
        ticks += (
            f'<text x="{_x(i):.1f}" y="{height - 6}" text-anchor="{anchor}" '
            f'class="cv">{_e(points[i][0])}</text>'
        )

    return (
        f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" width="100%" '
        f'preserveAspectRatio="none">'
        f'<polygon points="{area}" fill="{color}" opacity="0.14"/>'
        f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f"{dots}{ticks}</svg>"
    )


def sparkline(
    values: list[float],
    *,
    width: int = 84,
    height: int = 22,
    color: str = "#635bff",
) -> str:
    """Tiny inline trend line, for a table cell. Empty string below two points.

    Deliberately unlabelled: it answers "is this going up?" at a glance, while the
    figures themselves live in the surrounding columns (and stay maskable there).
    """
    points = [float(v) for v in values]
    if len(points) < 2:
        return ""
    lo, hi = min(points), max(points)
    span = (hi - lo) or (abs(hi) or 1)
    step = width / (len(points) - 1)
    coords = " ".join(
        f"{i * step:.1f},{height - 3 - (v - lo) / span * (height - 6):.1f}"
        for i, v in enumerate(points)
    )
    last_x = width
    last_y = height - 3 - (points[-1] - lo) / span * (height - 6)
    flat = hi == lo
    return (
        f'<svg viewBox="0 0 {width + 3} {height}" width="{width + 3}" height="{height}" '
        f'class="spark" aria-hidden="true">'
        f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="1.5" '
        f'stroke-linejoin="round" stroke-linecap="round" opacity="{0.35 if flat else 1}"/>'
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="2" fill="{color}"/></svg>'
    )


def donut_chart(
    items: list[tuple[str, float]],
    *,
    size: int = 220,
    unit: str = "€",
    center_top: str | None = None,
    center_bottom: str | None = None,
) -> str:
    """Donut chart with legend. ``items`` = list of (label, value).

    Optional ``center_top`` / ``center_bottom`` render two lines inside the hole.
    """
    items = [(lbl, abs(float(v))) for lbl, v in items if v]
    total = sum(v for _, v in items)
    if total <= 0:
        return '<p class="muted">Aucune donnée.</p>'
    cx = cy = size / 2
    r = size / 2 - 4
    inner = r * 0.60
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
            f'<span class="lg-name">{_e(label)}</span>'
            f'<span class="lg-pct">{frac * 100:.0f} %</span>'
            f'<span class="lg-val amount">{value:,.0f} {unit}</span></li>'
        )
        angle = end
    center = ""
    if center_top or center_bottom:
        top = (
            f'<text x="{cx}" y="{cy - 4}" text-anchor="middle" class="donut-c1">'
            f"{_e(center_top)}</text>"
            if center_top
            else ""
        )
        bottom = (
            f'<text x="{cx}" y="{cy + 16}" text-anchor="middle" class="donut-c2 amount">'
            f"{_e(center_bottom)}</text>"
            if center_bottom
            else ""
        )
        center = top + bottom
    svg = (
        f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
        f'role="img">{"".join(segments)}'
        f'<circle cx="{cx}" cy="{cy}" r="{inner}" class="donut-hole"/>{center}</svg>'
    )
    return f'<div class="donut">{svg}<ul class="legend">{"".join(legend)}</ul></div>'
