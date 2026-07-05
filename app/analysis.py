"""Spending analysis page (``GET /analyse``).

All money aggregation happens here in Python (Decimal), and only floats are
handed to the dependency-free SVG chart helpers. The template just renders the
pre-computed values and the ``|safe`` chart markup.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .data import load_internal_ids, load_spending_transactions
from .deps import get_client, get_settings
from .enrich import CONSUMPTION_TYPES, categorize, merchant_key
from .helpers import bar_chart, donut_chart, month_key, month_label_fr
from .recurring import detect_recurring
from .web import templates

router = APIRouter()

# Rolling window (complete calendar months) used for tiles and the monthly chart.
WINDOW = 12
TOP_CATEGORIES = 8
_SPEND_COLOR = "#d6484f"  # matches --neg
_INCOME_COLOR = "#2f9e6f"  # matches --pos


def _recent_months(n: int) -> list[str]:
    """Return the ``YYYY-MM`` keys of the last ``n`` *complete* months, oldest first."""
    today = date.today()
    y, m = today.year, today.month
    keys: list[str] = []
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        keys.append(f"{y:04d}-{m:02d}")
    keys.reverse()
    return keys


@router.get("/analyse", response_class=HTMLResponse)
async def analysis(
    request: Request,
    client=Depends(get_client),  # noqa: B008
    settings=Depends(get_settings),  # noqa: B008
):
    txns = await load_spending_transactions(client, months=settings.history_months)
    internal = await load_internal_ids(client, months=settings.history_months)
    spend = [
        t
        for t in txns
        if t.value and t.value < 0 and t.id not in internal and t.type in CONSUMPTION_TYPES
    ]
    # Income keeps transfers (salary is a SEPA transfer), only internal moves excluded.
    income = [t for t in txns if t.value and t.value > 0 and t.id not in internal]

    ctx: dict[str, object] = {"request": request, "active": "analysis", "has_spend": bool(spend)}

    if not spend:
        return templates.TemplateResponse(request, "analysis.html", ctx)

    # --- 1 & 2. Monthly income vs spending over the last 12 full months --------
    spend_by_month: dict[str, Decimal] = defaultdict(Decimal)
    income_by_month: dict[str, Decimal] = defaultdict(Decimal)
    for t in spend:
        spend_by_month[month_key(t.date)] += abs(t.value)
    for t in income:
        income_by_month[month_key(t.date)] += t.value

    recent = _recent_months(WINDOW)
    total_spend = sum((spend_by_month.get(k, Decimal(0)) for k in recent), Decimal(0))
    total_income = sum((income_by_month.get(k, Decimal(0)) for k in recent), Decimal(0))
    avg_spend = (total_spend / WINDOW).quantize(Decimal("0.01"))
    avg_income = (total_income / WINDOW).quantize(Decimal("0.01"))
    avg_savings = avg_income - avg_spend

    spend_bars = [(month_label_fr(k), float(spend_by_month.get(k, Decimal(0)))) for k in recent]
    income_bars = [(month_label_fr(k), float(income_by_month.get(k, Decimal(0)))) for k in recent]
    spend_chart = bar_chart(spend_bars, color=_SPEND_COLOR)
    income_chart = bar_chart(income_bars, color=_INCOME_COLOR)

    # --- 3. Spending by category (top 8 + "Autre") ----------------------------
    cat_totals: dict[str, Decimal] = defaultdict(Decimal)
    for t in spend:
        cat_totals[categorize(merchant_key(t))] += abs(t.value)
    ordered = sorted(cat_totals.items(), key=lambda kv: kv[1], reverse=True)
    top = ordered[:TOP_CATEGORIES]
    if ordered[TOP_CATEGORIES:]:
        rest_total = sum((v for _, v in ordered[TOP_CATEGORIES:]), Decimal(0))
        merged = dict(top)
        merged["Autre"] = merged.get("Autre", Decimal(0)) + rest_total
        top = sorted(merged.items(), key=lambda kv: kv[1], reverse=True)

    total_spend_all = sum((v for _, v in ordered), Decimal(0)) or Decimal(1)
    cat_rows = [
        {"name": name, "total": val, "pct": float(val / total_spend_all * 100)}
        for name, val in top
    ]
    cat_donut = donut_chart([(name, float(val)) for name, val in top])

    # --- 4. Recurring vs one-off (monthly view) -------------------------------
    recurring_items = detect_recurring(
        txns, internal_ids=internal, kind="debit", allowed_types=CONSUMPTION_TYPES
    )
    recurring_monthly = sum((r.monthly_equiv for r in recurring_items), Decimal(0))
    ponctuel_monthly = avg_spend - recurring_monthly
    if ponctuel_monthly < 0:
        ponctuel_monthly = Decimal(0)
    rec_donut = donut_chart(
        [("Récurrent", float(recurring_monthly)), ("Ponctuel", float(ponctuel_monthly))]
    )

    # --- 5. Optional Powens indicators (null on this app) ---------------------
    try:
        indic = await client.get_indicators()
    except Exception:
        indic = None
    indicator_rows = (
        list(indic.indicators.items())[:4] if indic is not None and indic.available else []
    )

    ctx.update(
        {
            "avg_spend": avg_spend,
            "avg_income": avg_income,
            "avg_savings": avg_savings,
            "income_chart": income_chart,
            "spend_chart": spend_chart,
            "cat_donut": cat_donut,
            "cat_rows": cat_rows,
            "rec_donut": rec_donut,
            "recurring_monthly": recurring_monthly,
            "ponctuel_monthly": ponctuel_monthly,
            "recurring_count": len(recurring_items),
            "indicator_rows": indicator_rows,
        }
    )
    return templates.TemplateResponse(request, "analysis.html", ctx)
