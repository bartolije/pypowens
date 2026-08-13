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

import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from pypowens import PowensClient

from . import store
from .config import Settings
from .data import MAX_WINDOW_MONTHS, load_internal_ids, load_spending_transactions
from .deps import get_client, get_settings, get_store
from .enrich import (
    EVERYDAY_CATEGORIES,
    SUBSCRIPTION_TYPES,
    categorize,
    merchant_key,
    resolve_category,
)
from .helpers import donut_chart, sparkline
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
    rail: str = "mixte"      # "SEPA" (prélèvement), "carte", or "mixte"
    spread: float = 0.0      # (max - min) / median amount — 0 when every charge is identical
    days_since_last: int = 0
    # Overdue by more than half a period: still listed (the contract may just be
    # late) but flagged, because a subscription that stopped being debited keeps
    # inflating the monthly total for as long as it is counted as active.
    stale: bool = False
    # Latest charge by this merchant, series or not. Amount clustering splits a
    # repriced contract in two — a yearly tax going from 1 435 to 2 115 € lands in a
    # cluster of one and is dropped — which would otherwise make the surviving half
    # look cancelled. Set by :func:`detect_subscriptions`.
    merchant_last: tuple[date, Decimal] | None = None
    account_ids: list[int] = field(default_factory=list)
    # Ids of the transactions forming the series: lets callers split real spending
    # into recurring vs one-off exactly, instead of comparing estimates.
    txn_ids: list[int] = field(default_factory=list)
    # Every occurrence, oldest first: what makes a series *traceable* rather than
    # just a current amount (price history, and the trend since it started).
    history: list[tuple[date, Decimal]] = field(default_factory=list)
    # Presentation-only: inline SVG trend of ``history``, filled in by the router.
    # Kept here rather than in a side dict because two series can share a merchant
    # key (same biller, different periodicity) and would collide as dict keys.
    spark: str = ""
    # Presentation-only, same reason: état "nouveau / hausse" issu de sync_series,
    # posé par le routeur (vide hors de la vue de référence).
    flags: dict[str, Any] = field(
        default_factory=lambda: {"new": False, "increase_pct": None, "previous_amount": None}
    )

    @property
    def repriced(self) -> bool:
        """Dormant series, but the merchant has charged since: a new amount, not a stop."""
        return bool(self.stale and self.merchant_last and self.merchant_last[0] > self.last_date)

    @property
    def first_amount(self) -> Decimal:
        return self.history[0][1] if self.history else self.amount

    @property
    def last_amount(self) -> Decimal:
        """The most recent charge — what is actually being paid right now.

        Distinct from :attr:`amount`, which is the median: the median resists a
        prorated or doubled first instalment, but understates a premium that has
        been raised, so both are shown.
        """
        return self.history[-1][1] if self.history else self.amount

    @property
    def drift_pct(self) -> float | None:
        """Change between the first and the last charge, in percent.

        ``None`` when there is nothing to compare (single occurrence, or a series
        that started at zero).
        """
        if len(self.history) < 2 or self.first_amount <= 0:
            return None
        return float((self.history[-1][1] - self.first_amount) / self.first_amount * 100)


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


def _cluster_by_amount(txns: list[Any], tol: float = _AMOUNT_TOL) -> list[list[Any]]:
    """Split a merchant group into amount-homogeneous clusters.

    Sort by |amount| and greedily start a new cluster whenever an amount jumps
    more than ``tol`` away from the running cluster median. This separates two
    distinct subscriptions billed by the same provider (e.g. a 4.99 and a 49.99
    plan) into different series.
    """
    ordered = sorted(txns, key=lambda t: abs(float(t.value)))
    clusters: list[list[Any]] = []
    current: list[Any] = []
    # ``ordered`` étant trié, les montants du cluster courant le sont aussi par
    # construction : la médiane se lit au milieu de la liste en O(1). L'ancien
    # ``statistics.median([...])`` recréait et retriait la liste à chaque
    # itération — O(k² log k) par marchand, payé à chaque affichage de page
    # (800 passages de supermercado sur 3 ans ≈ 800 tris de 400 éléments).
    amounts: list[float] = []
    for t in ordered:
        amt = abs(float(t.value))
        if current:
            mid = len(amounts) // 2
            med = amounts[mid] if len(amounts) % 2 else (amounts[mid - 1] + amounts[mid]) / 2
            if med > 0 and abs(amt - med) / med > tol:
                clusters.append(current)
                current = []
                amounts = []
        current.append(t)
        amounts.append(amt)
    if current:
        clusters.append(current)
    return clusters


def _analyze(
    cluster: list[Any],
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

    rails = {t.type for t in txns}
    if rails == {"order"}:
        rail = "SEPA"
    elif rails <= {"card", "deferred_card"}:
        rail = "carte"
    else:
        rail = "mixte"

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
        rail=rail,
        spread=float((max(amounts) - min(amounts)) / amount) if amount else 0.0,
        days_since_last=(today - last_date).days,
        stale=last_date < today - interval_delta * 3 // 2,
        account_ids=sorted({t.id_account for t in txns if t.id_account is not None}),
        txn_ids=[t.id for t in txns if t.id is not None],
        history=[(t.date, _q(abs(Decimal(str(t.value))))) for t in txns],
    )


def detect_recurring(
    transactions: list[Any],
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

    kept: list[Any] = []
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

    groups: dict[str, list[Any]] = defaultdict(list)
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


# --------------------------------------------------- subscriptions (strict pass)

# A SEPA mandate is itself evidence of a contract, so regularity alone is enough.
_SEPA_MIN_CONFIDENCE = 0.55
# ...and past this many debits, regularity stops mattering at all: two contracts with
# the same biller (a flat and a garage at the same utility) interleave into one
# irregular-looking series, which is still very much a contract.
_SEPA_CERTAIN_OCCURRENCES = 6
# A card charge is not: groceries and fuel produce amount-homogeneous clusters that
# land in a periodicity bucket by luck. What no coincidence reproduces is the *same*
# amount every time, so card series must be near-identical to count.
_CARD_MIN_CONFIDENCE = 0.60
_CARD_MAX_SPREAD = 0.02
# A subscription merchant bills essentially nothing but the subscription, so its
# series covers nearly all of its charges. A shop visited fifteen times, where three
# purchases happen to land on similar amounts, does not — and that is exactly the
# false positive identical amounts alone cannot rule out.
_CARD_MIN_SHARE = 0.6
# With only two occurrences a year apart, "same amount" can still be chance. A real
# renewal falls within days of its anniversary; two restaurant visits do not.
_ANNIVERSARY_TOLERANCE_DAYS = 12
_LONG_PERIODS = (12.0, 24.0)


def is_subscription(item: RecurringItem, *, merchant_charges: int | None = None) -> bool:
    """Whether a detected series can be asserted to be a subscription or contract.

    :func:`detect_recurring` is deliberately permissive — :mod:`app.analysis` needs
    every repeating pattern to split recurring from one-off spending. A
    subscriptions *view* needs the opposite: no false positives, because a list
    where four lines out of five are supermarket runs cannot be acted on.

    ``merchant_charges`` is how many times this merchant was charged at all. Given,
    it rules out the series that survive every other test — a few similar purchases
    at a shop that was also visited a dozen other times.
    """
    if item.category in EVERYDAY_CATEGORIES:
        return False
    if item.rail == "SEPA":
        return (
            item.confidence >= _SEPA_MIN_CONFIDENCE
            or item.occurrences >= _SEPA_CERTAIN_OCCURRENCES
        )
    if item.confidence < _CARD_MIN_CONFIDENCE or item.spread > _CARD_MAX_SPREAD:
        return False
    if merchant_charges and item.occurrences / merchant_charges < _CARD_MIN_SHARE:
        return False
    if item.occurrences == 2 and item.period_months in _LONG_PERIODS:
        expected = item.period_months * 365.25 / 12
        actual = (item.last_date - item.first_date).days
        return abs(actual - expected) <= _ANNIVERSARY_TOLERANCE_DAYS
    return True


def detect_subscriptions(
    transactions: list[Any],
    *,
    today: date | None = None,
    internal_ids: set[int] | None = None,
    overrides: dict[str, str] | None = None,
) -> list[RecurringItem]:
    """Detected series restricted to actual subscriptions and contracts.

    Categories are resolved (so manual overrides count towards the everyday-spending
    exclusion) before filtering with :func:`is_subscription`.
    """
    allowed = SUBSCRIPTION_TYPES | {"bank", "fee"}
    items = detect_recurring(
        transactions,
        today=today,
        internal_ids=internal_ids,
        kind="debit",
        allowed_types=allowed,
    )
    # Counted over the same population the detector saw, so a series can never look
    # like it covers more of its merchant's charges than it does.
    excluded = internal_ids or set()
    eligible = [
        t
        for t in transactions
        if t.type in allowed
        and t.value is not None
        and t.value < 0
        and t.date is not None
        and t.id not in excluded
    ]
    charges = Counter(merchant_key(t) for t in eligible)
    last_charge: dict[str, tuple[date, Decimal]] = {}
    for t in eligible:
        key = merchant_key(t)
        seen = last_charge.get(key)
        if seen is None or t.date > seen[0]:
            last_charge[key] = (t.date, _q(abs(Decimal(str(t.value)))))

    for item in items:
        item.category = resolve_category(item.key, overrides)
        item.merchant_last = last_charge.get(item.key)
    kept = [item for item in items if is_subscription(item, merchant_charges=charges[item.key])]
    return _drop_fragments(kept)


# Amount clustering splits a variable-amount mandate (a toll account, a usage-based
# bill) into several thin series. Each looks credible on its own — with only two dates
# the interval variance is zero, so the confidence score peaks — and one contract ends
# up listed four times. A genuine second contract with the same biller (two energy
# meters) shows up as another *long* series, so only the two-point offcuts are dropped.
_FRAGMENT_OCCURRENCES = 2


def _drop_fragments(items: list[RecurringItem]) -> list[RecurringItem]:
    longest: dict[str, int] = {}
    for item in items:
        longest[item.key] = max(longest.get(item.key, 0), item.occurrences)
    return [
        item
        for item in items
        if item.occurrences > _FRAGMENT_OCCURRENCES or longest[item.key] == item.occurrences
    ]


# ------------------------------------------------------------------- router

router = APIRouter()


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


@router.post("/abonnements/acquitter")
async def acknowledge_alerts(
    conn: sqlite3.Connection = Depends(get_store),  # noqa: B008
) -> RedirectResponse:
    """Acquitte les alertes « nouveau / hausse » — elles persistent jusqu'ici."""
    store.acknowledge_alerts(conn)
    return RedirectResponse("/abonnements", status_code=303)


@router.get("/abonnements", response_class=HTMLResponse)
async def recurring_page(  # noqa: PLR0913 — signature dictée par FastAPI
    request: Request,
    client: PowensClient = Depends(get_client),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
    conn: sqlite3.Connection = Depends(get_store),  # noqa: B008
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    tout: int = Query(default=0, description="1 = show every repeating series, not just contracts"),
) -> Response:
    d_from = _parse_date(date_from)
    d_to = _parse_date(date_to)
    # Window covering the requested range (default: configured history).
    if d_from:
        months = min(MAX_WINDOW_MONTHS, max(1, math.ceil((date.today() - d_from).days / 30) + 1))
    else:
        months = settings.history_months

    txns = await load_spending_transactions(client, months=months, conn=conn)
    internal = await load_internal_ids(client, months=months, conn=conn)
    if d_from or d_to:
        txns = [
            t for t in txns
            if t.date and (not d_from or t.date >= d_from) and (not d_to or t.date <= d_to)
        ]

    # Apply stored manual categories before filtering: an override can move a series
    # in or out of the everyday-spending exclusion.
    overrides = store.all_overrides(conn)
    if tout:
        items = detect_recurring(txns, internal_ids=internal, allowed_types=SUBSCRIPTION_TYPES)
        for it in items:
            it.category = resolve_category(it.key, overrides)
    else:
        items = detect_subscriptions(txns, internal_ids=internal, overrides=overrides)

    # Diff against the previously known state to surface what is new or got more
    # expensive since the last visit — mais UNIQUEMENT depuis la vue de référence :
    # une fenêtre filtrée par dates (médianes partielles) ou la passe permissive
    # ``?tout=1`` (séries non contractuelles) écrirait un état faussé, qui
    # fabriquerait des hausses fantômes à la visite non filtrée suivante.
    reference_view = not (d_from or d_to or tout)
    changes = store.sync_series(conn, items) if reference_view else {}
    no_flags = {"new": False, "increase_pct": None, "previous_amount": None}
    alerts = [
        {
            "item": it,
            "new": changes[store.series_key(it)]["new"],
            "increase_pct": changes[store.series_key(it)]["increase_pct"],
            "previous_amount": changes[store.series_key(it)]["previous_amount"],
        }
        for it in items
        if reference_view
        and (
            changes[store.series_key(it)]["new"]
            or changes[store.series_key(it)]["increase_pct"]
        )
    ]
    for it in items:
        # Attachés à l'item : un dict indexé par ``it.key`` (le seul marchand)
        # faisait porter le badge d'une série sur l'autre dès qu'un marchand en
        # avait deux (deux contrats chez le même assureur).
        it.flags = changes.get(store.series_key(it), no_flags)
        it.spark = sparkline([float(a) for _, a in it.history])

    active = [it for it in items if not it.stale]
    total_monthly = sum((it.monthly_equiv for it in active), Decimal("0"))
    stale_monthly = sum((it.monthly_equiv for it in items if it.stale), Decimal("0"))

    # Grouped by expense type: the unit of decision when trimming subscriptions is
    # the category ("what do I pay for energy?"), not the individual line.
    grouped: dict[str, list[RecurringItem]] = defaultdict(list)
    for it in items:
        grouped[it.category].append(it)
    groups: list[dict[str, Any]] = [
        {
            "name": name,
            "items": sorted(rows, key=lambda i: (i.stale, -float(i.monthly_equiv))),
            "monthly": sum((i.monthly_equiv for i in rows if not i.stale), Decimal("0")),
            "count": sum(1 for i in rows if not i.stale),
            "stale_count": sum(1 for i in rows if i.stale),
        }
        for name, rows in grouped.items()
    ]
    groups.sort(key=lambda g: g["monthly"], reverse=True)
    for group in groups:
        group["annual"] = group["monthly"] * 12
        group["pct"] = float(group["monthly"] / total_monthly * 100) if total_monthly else 0.0
    donut = donut_chart([(g["name"], float(g["monthly"])) for g in groups if g["monthly"]])

    period_from = d_from or (date.today() - timedelta(days=int(settings.history_months * 30.5)))
    period_to = d_to or date.today()

    return templates.TemplateResponse(
        request,
        "recurring.html",
        {
            "request": request,
            "active": "recurring",
            "items": items,
            "groups": groups,
            "alerts": alerts,
            "total_monthly": total_monthly,
            "total_annual": total_monthly * 12,
            "stale_monthly": stale_monthly,
            "count": len(active),
            "stale_count": len(items) - len(active),
            "donut": donut,
            "period_from": period_from,
            "period_to": period_to,
            "date_from": date_from or "",
            "date_to": date_to or "",
            "tout": tout,
        },
    )
