"""Pure presentation helpers: formatting + dependency-free inline SVG charts.

No app imports here (safe to use everywhere). SVG is generated server-side so the
page stays self-contained (no JS charting lib).
"""

from __future__ import annotations

import html
import math
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
    bar_height: int = 18,
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
            f'rx="3" fill="{color}"><title>{_e(label)}: {value:,.2f} {unit}</title></rect>'
            f'<text x="{label_w + w + 6}" y="{y + bar_height * 0.7}" class="cv amount">'
            f'{value:,.0f} {unit}</text>'
        )
    height = len(items) * (bar_height + gap)
    return (
        f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
        f'width="100%" preserveAspectRatio="xMinYMin meet">{"".join(rows)}</svg>'
    )


def nice_step(span: float, target_ticks: int = 4) -> float:
    """Pas de graduation « rond » couvrant ``span`` en ~``target_ticks`` intervalles.

    Un pas brut (span / n) donne des graduations du genre 1 736 € : illisible. On remonte
    au multiple de 1, 2, 2,5, 5 ou 10 de la puissance de dix immédiatement supérieure.
    """
    if span <= 0:
        return 1.0
    raw = span / max(1, target_ticks)
    magnitude = 10.0 ** math.floor(math.log10(raw))
    for multiple in (1, 2, 2.5, 5, 10):
        if raw <= magnitude * multiple:
            return magnitude * multiple
    return magnitude * 10


def format_axis(value: float, step: float, unit: str = "€") -> str:
    """Montant abrégé pour une graduation : ``178,2 k€``, ``1,25 M€``, ``850 €``.

    La précision suit le **pas** et non la valeur : avec un pas de 2 000 sur des montants
    à six chiffres, « 178 k€ » et « 180 k€ » suffisent, alors qu'un pas de 200 exige la
    décimale sous peine d'afficher deux graduations identiques.
    """
    for divisor, suffix in ((1e6, "M"), (1e3, "k")):
        if abs(value) >= divisor or (step >= divisor and value):
            decimals = _decimals_for(step / divisor)
            text = f"{value / divisor:,.{decimals}f}".replace(",", _THIN_NBSP).replace(".", ",")
            return f"{text}{_NBSP}{suffix}{unit}"
    text = f"{value:,.{_decimals_for(step)}f}".replace(",", _THIN_NBSP).replace(".", ",")
    return f"{text}{_NBSP}{unit}"


def _decimals_for(step: float, maximum: int = 2) -> int:
    """Décimales juste nécessaires pour que deux graduations d'écart ``step`` diffèrent.

    Un pas de 2 (k€) n'a besoin d'aucune décimale — « 178,0 k€ » traîne un zéro inutile —
    alors qu'un pas de 0,2 en exige une.
    """
    if step <= 0:
        return 0
    return max(0, min(maximum, math.ceil(-math.log10(step))))


def line_chart(
    items: list[tuple[str, float]],
    *,
    width: int = 720,
    height: int = 220,
    unit: str = "€",
    color: str = "#635bff",
    y_ticks: int = 4,
    benchmark: list[float | None] | None = None,
    benchmark_label: str = "",
) -> str:
    """Time series as an SVG area+line chart. ``items`` = list of (label, value).

    The Y axis is scaled to the data range (not forced to zero) so a net-worth
    curve shows its actual movement instead of a flat line at the top — with graduated
    values and gridlines, without which a dip is visible but unquantifiable.

    Deliberately *not* stretched (``xMinYMin meet``): ``preserveAspectRatio="none"`` made
    the SVG fill its container by distorting it, which squashed every label horizontally.
    """
    points = [(label, float(value)) for label, value in items if value is not None]
    if len(points) < 2:
        return '<p class="muted">Pas encore assez d\'historique — repassez demain.</p>'

    # ``benchmark`` : mêmes indices x que ``items`` (None = pas de clôture ce
    # jour-là). Rebasé PAR L'APPELANT sur la valeur de départ de la série : la
    # lecture est « si la même somme était sur l'indice ».
    bench = list(benchmark) if benchmark and len(benchmark) == len(points) else None

    values = [v for _, v in points]
    if bench:
        values = values + [v for v in bench if v is not None]
    lo_data, hi_data = min(values), max(values)
    span = (hi_data - lo_data) or (abs(hi_data) or 1)

    # Graduations rondes calculées sur l'amplitude réelle, puis bornes élargies jusqu'à
    # la graduation suivante : la courbe ne touche jamais le bord du cadre.
    step = nice_step(span, y_ticks)
    lo = math.floor(lo_data / step) * step
    hi = math.ceil(hi_data / step) * step
    if hi == lo:
        hi = lo + step
    span = hi - lo

    labels = [format_axis(lo + i * step, step, unit) for i in range(int(round(span / step)) + 1)]
    # La gouttière gauche doit contenir la plus longue graduation, sinon elle déborde.
    pad_l = 14 + int(6.2 * max(len(text) for text in labels))
    pad_r, pad_t, pad_b = 10, 12, 24
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

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

    # Grille + graduations. Les montants portent `.amount` : le masque global doit les
    # couvrir, sans quoi l'axe rendrait en clair ce que la courbe floute.
    grid = ""
    for i, text in enumerate(labels):
        y = _y(lo + i * step)
        grid += (
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w}" y2="{y:.1f}" '
            f'class="grid"/>'
            f'<text x="{pad_l - 8}" y="{y + 3.5:.1f}" text-anchor="end" '
            f'class="cv amount">{_e(text)}</text>'
        )

    # Invisible hit-areas for tooltips (no visible dots — Finary-style clean line).
    dots = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="8" fill="transparent" '
        f'style="cursor:crosshair">'
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

    bench_svg = ""
    if bench:
        # Les None ne surviennent qu'en tête (avant la première clôture
        # archivée) : une seule polyligne des points datés suffit.
        bench_points = [
            f"{_x(i):.1f},{_y(v):.1f}" for i, v in enumerate(bench) if v is not None
        ]
        if len(bench_points) >= 2:
            bench_svg = (
                f'<polyline points="{" ".join(bench_points)}" fill="none" '
                f'stroke="#7c8db5" stroke-width="1.2" stroke-dasharray="4 3" '
                f'stroke-linejoin="round"/>'
            )
        if bench_svg and benchmark_label:
            bench_svg += (
                f'<text x="{width - pad_r}" y="{pad_t + 2}" text-anchor="end" '
                f'class="cv" fill="#7c8db5">- - {_e(benchmark_label)}</text>'
            )

    return (
        f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" width="100%" '
        f'preserveAspectRatio="xMinYMin meet">'
        f"{grid}"
        f'<polygon points="{area}" fill="{color}" opacity="0.14"/>'
        f"{bench_svg}"
        f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="1.5" '
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
        f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="1" '
        f'stroke-linejoin="round" stroke-linecap="round" opacity="{0.35 if flat else 1}"/>'
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="1.5" fill="{color}"/></svg>'
    )


def donut_chart(
    items: list[tuple[str, float]],
    *,
    size: int = 220,
    unit: str = "€",
    center_top: str | None = None,
    center_bottom: str | None = None,
    compact: bool = False,
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
        if compact:
            legend.append(
                f'<li><span class="dot" style="background:{color}"></span>'
                f'<span class="lg-name">{_e(label)}</span>'
                f'<span class="lg-pct">{frac * 100:.0f} %</span></li>'
            )
        else:
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


def treemap(
    items: list[tuple[str, float]],
    *,
    width: int = 400,
    height: int = 300,
    unit: str = "\u202f\u20ac",
) -> str:
    """Treemap visualization using a squarified slice-and-dice layout.

    ``items`` = list of (label, value). Returns an inline SVG string.
    Each rectangle shows the label, formatted value, and percentage.
    """
    items = [(lbl, abs(float(v))) for lbl, v in items if v]
    if not items:
        return '<p class="muted">Aucune donn\u00e9e.</p>'

    total = sum(v for _, v in items)
    if total <= 0:
        return '<p class="muted">Aucune donn\u00e9e.</p>'

    # Sort descending for a visually balanced layout.
    items.sort(key=lambda x: x[1], reverse=True)

    rects: list[tuple[str, float, float, float, float, float]] = []
    _squarify(items, total, 0.0, 0.0, float(width), float(height), rects)

    segments: list[str] = []
    for i, (label, value, x, y, w, h) in enumerate(rects):
        color = PALETTE[i % len(PALETTE)]
        pct = value / total * 100

        # Only render text when the rectangle is large enough.
        text = ""
        if w > 50 and h > 28:
            # Truncate label to fit.
            max_chars = max(3, int(w / 7))
            shown = label if len(label) <= max_chars else label[: max_chars - 1] + "\u2026"
            text += (
                f'<text x="{x + 6}" y="{y + 16}" class="tm-label">'
                f"{_e(shown)}</text>"
            )
        if w > 40 and h > 42:
            text += (
                f'<text x="{x + 6}" y="{y + 30}" class="tm-val amount">'
                f"{value:,.0f}{unit}</text>"
            )
        if w > 40 and h > 56:
            text += (
                f'<text x="{x + 6}" y="{y + 43}" class="tm-pct">'
                f"{pct:.0f}\u202f%</text>"
            )

        segments.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'rx="3" fill="{color}" opacity="0.85">'
            f"<title>{_e(label)}: {value:,.0f}{unit} ({pct:.1f}%)</title></rect>"
            f"{text}"
        )

    svg = (
        f'<svg viewBox="0 0 {width} {height}" class="chart treemap" role="img" '
        f'width="100%" preserveAspectRatio="xMinYMin meet">'
        f'{"".join(segments)}</svg>'
    )
    return svg


def _squarify(
    items: list[tuple[str, float]],
    total: float,
    x: float,
    y: float,
    w: float,
    h: float,
    out: list[tuple[str, float, float, float, float, float]],
) -> None:
    """Recursive slice-and-dice: alternate horizontal/vertical splits."""
    if not items:
        return
    if len(items) == 1:
        label, value = items[0]
        out.append((label, value, x, y, w, h))
        return

    # Find the split point that gives the best aspect ratio for the first group.
    cumulative = 0.0
    split = 1
    best_aspect = float("inf")
    for i, (_, v) in enumerate(items[:-1], 1):
        cumulative += v
        frac = cumulative / total
        if w >= h:
            # Vertical split: first group occupies a fraction of the width.
            dim1 = w * frac
            dim2 = h
        else:
            # Horizontal split: first group occupies a fraction of the height.
            dim1 = w
            dim2 = h * frac
        aspect = max(dim1 / dim2, dim2 / dim1) if dim1 > 0 and dim2 > 0 else float("inf")
        if aspect < best_aspect:
            best_aspect = aspect
            split = i

    left = items[:split]
    right = items[split:]
    left_total = sum(v for _, v in left)
    frac = left_total / total if total else 0.5

    if w >= h:
        # Vertical split.
        w1 = w * frac
        _layout_strip(left, left_total, x, y, w1, h, vertical=False, out=out)
        right_total = total - left_total
        if right:
            _squarify(right, right_total, x + w1, y, w - w1, h, out)
    else:
        # Horizontal split.
        h1 = h * frac
        _layout_strip(left, left_total, x, y, w, h1, vertical=True, out=out)
        right_total = total - left_total
        if right:
            _squarify(right, right_total, x, y + h1, w, h - h1, out)


def _layout_strip(
    items: list[tuple[str, float]],
    strip_total: float,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    vertical: bool,
    out: list[tuple[str, float, float, float, float, float]],
) -> None:
    """Lay items out in a strip, stacking along the shorter dimension."""
    offset = 0.0
    for label, value in items:
        frac = value / strip_total if strip_total else 1.0 / len(items)
        if vertical:
            iw = w * frac
            out.append((label, value, x + offset, y, iw, h))
            offset += iw
        else:
            ih = h * frac
            out.append((label, value, x, y + offset, w, ih))
            offset += ih
