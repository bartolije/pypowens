"""Recurring-payments / subscriptions detector.

Given a flat list of :class:`pypowens.Transaction` objects, group them by
merchant, isolate stable recurring series, classify their periodicity and expose
them as :class:`RecurringItem` records. Pure functions (no network) so the core
algorithm is fully unit-testable; the FastAPI router at the bottom simply wires
it to the ``/abonnements`` page.

Everything is derived from the transaction *wording* (Powens categories are
empty on this app) through the shared :mod:`app.enrich` helpers.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .data import load_internal_ids, load_spending_transactions
from .deps import get_client, get_settings
from .enrich import SUBSCRIPTION_TYPES, categorize, merchant_key
from .helpers import donut_chart
from .web import templates

# --------------------------------------------------------------------- model


@dataclass
class RecurringItem:
    """A single detected recurring payment (or recurring income)."""

    merchant: str            # human display name (Title Case of the merchant key)
    key: str                 # normalized merchant key (from enrich.merchant_key)
    category: str            # from enrich.categorize
    periodicity: str         # FR label (see _BUCKETS)
    period_months: float     # 0.25,1,2,3,4,6,12,24 (0 if Irrégulier)
    amount: Decimal          # typical (median) amount, POSITIVE
    monthly_equiv: Decimal   # amount / period_months (0 if Irrégulier)
    occurrences: int
    first_date: date
    last_date: date
    next_date: date | None   # estimated next occurrence (last_date + interval)
    confidence: float        # 0..1
    variable: bool           # True if amount varies a lot (high dispersion)
    account_ids: list[int] = field(default_factory=list)


# ------------------------------------------------------------------- tuning

# Periodicity buckets: (center_days, tolerance_days, FR label, months).
# First matching (ascending) window wins.
_BUCKETS: list[tuple[int, int, str, float]] = [
    (7, 3, "Hebdomadaire", 0.25),
    (30, 8, "Mensuel", 1),
    (61, 12, "Bimestriel", 2),
    (91, 15, "Trimestriel", 3),
    (122, 18, "Quadrimestriel", 4),
    (182, 25, "Semestriel", 6),
    (365, 40, "Annuel", 12),
    (730, 60, "Biennal", 24),
]

# Amounts within this fraction of a running cluster median are the same series.
_AMOUNT_TOL = 0.12
# Above this coefficient of variation the amount is considered "variable".
_VARIABLE_CV = 0.15
# Periodicities we still accept with only 2 occurrences (long cadence).
_LONG_CADENCE = ("Annuel", "Biennal")

_CENT = Decimal("0.01")


# ---------------------------------------------------------------- utilities


def _q(value: Decimal) -> Decimal:
    """Quantize a money amount to 2 decimals (round half up)."""
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _classify(interval_days: float) -> tuple[str, float]:
    """Map a median interval (in days) to a periodicity label + months."""
    for center, tol, label, months in _BUCKETS:
        if abs(interval_days - center) <= tol:
            return label, months
    return "Irrégulier", 0.0


def _cluster_by_amount(txns: list, tol: float = _AMOUNT_TOL) -> list[list]:
    """Split a merchant group into amount-homogeneous clusters.

    Sort by |amount| and greedily start a new cluster whenever an amount jumps
    more than ``tol`` away from the running cluster median. This separates two
    distinct subscriptions billed by the same provider (e.g. a 4.99 and a 49.99
    plan) into different series.
    """
    ordered = sorted(txns, key=lambda t: abs(float(t.value)))
    clusters: list[list] = []
    current: list = []
    for t in ordered:
        amt = abs(float(t.value))
        if current:
            med = statistics.median([abs(float(x.value)) for x in current])
            if med > 0 and abs(amt - med) / med > tol:
                clusters.append(current)
                current = []
        current.append(t)
    if current:
        clusters.append(current)
    return clusters


def _analyze(
    cluster: list,
    *,
    key: str,
    today: date,
    min_occurrences: int,
) -> RecurringItem | None:
    """Turn one amount-cluster into a :class:`RecurringItem`, or ``None``.

    Returns ``None`` when the cluster is not a credible recurring series
    (too few occurrences, irregular cadence, or long-dead).
    """
    if len(cluster) < 2:
        return None

    txns = sorted(cluster, key=lambda t: t.date)
    dates = [t.date for t in txns]
    intervals = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    if not intervals:
        return None

    median_interval = statistics.median(intervals)
    label, months = _classify(median_interval)
    if label == "Irrégulier":
        # Irregular series are noise for a subscriptions view -> drop.
        return None

    occ = len(txns)
    # Occurrence gate: normally >= min_occurrences, but accept a 2-point series
    # for long cadences (yearly / biennial) where 3 samples span years.
    if occ < min_occurrences and not (occ >= 2 and label in _LONG_CADENCE):
        return None

    # Amount statistics (positive Decimals).
    amounts = [abs(Decimal(str(t.value))) for t in txns]
    amount = statistics.median(amounts)
    floats = [float(a) for a in amounts]
    mean = statistics.fmean(floats)
    amount_cv = statistics.pstdev(floats) / mean if mean else 0.0
    variable = amount_cv > _VARIABLE_CV

    interval_delta = timedelta(days=round(median_interval))
    first_date, last_date = dates[0], dates[-1]

    # "Active" rule: keep only series whose last occurrence is recent enough,
    # i.e. within two of its own intervals from today. Drops dead subscriptions.
    if last_date < today - 2 * interval_delta:
        return None

    next_date = last_date + interval_delta

    # Confidence: interval regularity (50%), amount stability (30%),
    # occurrence count saturating at 6 (20%).
    reg = 1.0 - (statistics.pstdev(intervals) / median_interval) if median_interval else 0.0
    regularity = _clamp01(reg)
    amount_stability = _clamp01(1.0 - amount_cv)
    occ_factor = min(occ / 6.0, 1.0)
    confidence = round(0.5 * regularity + 0.3 * amount_stability + 0.2 * occ_factor, 2)

    return RecurringItem(
        merchant=key.title(),
        key=key,
        category=categorize(key),
        periodicity=label,
        period_months=float(months),
        amount=_q(amount),
        monthly_equiv=_q(amount / Decimal(str(months))),
        occurrences=occ,
        first_date=first_date,
        last_date=last_date,
        next_date=next_date,
        confidence=confidence,
        variable=variable,
        account_ids=sorted({t.id_account for t in txns if t.id_account is not None}),
    )


def detect_recurring(
    transactions: list,
    *,
    today: date | None = None,
    internal_ids: set[int] | None = None,
    min_occurrences: int = 3,
    kind: str = "debit",
    allowed_types: frozenset[str] | set[str] | None = None,
) -> list[RecurringItem]:
    """Detect recurring payments (``kind="debit"``) or income (``kind="credit"``).

    Steps: filter by sign/kind, drop internal transfers, group by merchant,
    sub-cluster by amount, then analyze each cluster. If a merchant yields no
    qualifying sub-cluster, fall back to analyzing the whole merchant group —
    this captures a single subscription whose amount fluctuates (flagged
    ``variable``) instead of shredding it into unusable singletons.

    Returned sorted by ``period_months`` (ascending) then ``monthly_equiv``
    (descending).
    """
    today = today or date.today()
    excluded = internal_ids or set()

    kept: list = []
    for t in transactions:
        if t.id is not None and t.id in excluded:
            continue
        if t.value is None or t.date is None:
            continue
        if allowed_types is not None and t.type not in allowed_types:
            continue
        value = t.value
        if kind == "credit":
            if value <= 0:
                continue
        elif value >= 0:  # kind == "debit"
            continue
        kept.append(t)

    groups: dict[str, list] = defaultdict(list)
    for t in kept:
        groups[merchant_key(t)].append(t)

    items: list[RecurringItem] = []
    for key, group in groups.items():
        found = [
            item
            for cluster in _cluster_by_amount(group)
            if (item := _analyze(cluster, key=key, today=today, min_occurrences=min_occurrences))
        ]
        if not found:
            whole = _analyze(group, key=key, today=today, min_occurrences=min_occurrences)
            if whole:
                found = [whole]
        items.extend(found)

    items.sort(key=lambda it: (it.period_months, -float(it.monthly_equiv)))
    return items


# ------------------------------------------------------------------- router

router = APIRouter()


@router.get("/abonnements", response_class=HTMLResponse)
async def recurring_page(
    request: Request,
    client=Depends(get_client),
    settings=Depends(get_settings),
):
    txns = await load_spending_transactions(client, months=settings.history_months)
    internal = await load_internal_ids(client, months=settings.history_months)
    items = detect_recurring(txns, internal_ids=internal, allowed_types=SUBSCRIPTION_TYPES)

    total_monthly = sum((it.monthly_equiv for it in items), Decimal("0"))
    total_annual = total_monthly * 12

    by_category: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for it in items:
        by_category[it.category] += it.monthly_equiv
    donut = donut_chart(
        sorted(((cat, float(v)) for cat, v in by_category.items()), key=lambda x: -x[1])
    )

    return templates.TemplateResponse(
        request,
        "recurring.html",
        {
            "request": request,
            "active": "recurring",
            "items": items,
            "total_monthly": total_monthly,
            "total_annual": total_annual,
            "count": len(items),
            "donut": donut,
        },
    )
