"""Performance des comptes d'investissement : TWR, MWR, et la série qui les porte.

Deux mesures, deux questions différentes, et le choix n'est pas cosmétique :

* **TWR** (*time-weighted return*) neutralise les versements — c'est la seule grandeur
  comparable à un indice ou à un ETF, puisqu'elle ne récompense pas le fait d'avoir
  versé au bon moment ;
* **MWR** (*money-weighted*, ici un XIRR) répond à « qu'est-ce que **mon** argent a
  rapporté », en tenant compte de la date de chaque versement.

Les deux exigent une valorisation datée et des flux datés, avec un piège : ne compter
comme flux que ce qui **traverse la frontière du compte**. Un dividende encaissé ou un
achat de titres reste à l'intérieur — les traiter comme des apports effacerait le gain
qu'ils représentent, ou en inventerait un.

Tout ici est pur (listes en entrée, valeurs en sortie) : le module ne connaît ni le
réseau ni SQLite, ce qui le rend testable sans l'un ni l'autre.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

# Types Powens qui font entrer ou sortir de l'argent du compte.
EXTERNAL_TYPES = frozenset({"deposit", "transfer", "withdrawal"})

# Types qui déplacent de la valeur *à l'intérieur* du compte, du cash vers des titres ou
# l'inverse. Ils ne changent pas ce que vaut le compte, donc n'entrent dans aucun calcul.
TRADE_TYPES = frozenset({"market_order", "arbitrage"})

_TRADE_WORDING = re.compile(
    r"\b(achat|vente|souscription|reprise|arbitrage|comptant)", re.IGNORECASE
)

# Sous ce seuil, annualiser un rendement n'a pas de sens : un mois de marché extrapolé
# sur un an produit des nombres spectaculaires et faux.
MIN_ANNUALIZE_DAYS = 90

# Part de la valorisation du compte que les lignes historisées doivent couvrir pour
# qu'une performance reconstruite veuille dire quelque chose. Un contrat dont une seule
# poche sur deux publie une VL donnerait sinon un chiffre parfaitement crédible et faux.
MIN_COVERAGE = 0.95

# ... sauf quand le libellé dit que c'est l'établissement qui donne. Un « boost sur
# versement » ou une « participation aux bénéfices » arrive typé ``deposit`` alors que
# c'est un gain : le classer en apport le retirerait de la performance.
_GAIN_WORDING = re.compile(
    r"\b(boost|participation|b[ée]n[ée]fice|int[ée]r[êe]t|prime|bonus|gratification)",
    re.IGNORECASE,
)

# Un jour d'écart suffit à rattacher un flux à une valorisation : les VL ne sont
# publiées que les jours ouvrés, un versement du samedi se lit le lundi.
FLOW_TOLERANCE_DAYS = 1


class TxnLike(Protocol):
    id: int | None
    id_account: int | None
    date: date | None
    value: Decimal | None
    type: str | None
    wording: str | None
    simplified_wording: str | None


@dataclass(frozen=True)
class Flow:
    """Un mouvement d'argent, vu depuis le compte.

    Trois natures, et les confondre fausse tout :

    * ``external`` — traverse la frontière du compte (versement, virement, retrait) : à
      retirer de la performance, ce n'est pas un gain ;
    * ``trade`` — convertit du cash en titres ou l'inverse (achat, vente, arbitrage) : ne
      change pas la valeur du compte, donc ne doit toucher aucun rendement ;
    * ``income`` — revenu ou frais encaissé en cash (dividende, coupon, intérêts d'un
      fonds euros, frais de gestion, prélèvements sociaux) : c'est de la performance.
    """

    day: date
    amount: Decimal  # signé : positif = entre dans le compte
    label: str
    txn_id: int | None = None
    kind: str = "external"

    @property
    def is_external(self) -> bool:
        return self.kind == "external"

    @property
    def is_trade(self) -> bool:
        return self.kind == "trade"

    @property
    def is_income(self) -> bool:
        return self.kind == "income"


@dataclass(frozen=True)
class Point:
    """Valorisation du compte à une date."""

    day: date
    value: Decimal
    reconstructed: bool = False


@dataclass
class Performance:
    """Le résultat, pour un compte et une période."""

    account_id: int
    start: date
    end: date
    start_value: Decimal
    end_value: Decimal
    external_flows: Decimal  # apports nets de la période
    dividends: Decimal
    fees: Decimal
    twr: float | None
    mwr: float | None
    points: list[Point] = field(default_factory=list)
    # Mouvements de titres tombés dans une fenêtre reconstruite : chacun est une entorse
    # à l'hypothèse de composition constante, donc à afficher plutôt qu'à taire.
    reconstruction_caveats: int = 0
    # Part de la valorisation du compte que la série couvre réellement (None = inconnue).
    coverage: float | None = None

    @property
    def gain(self) -> Decimal:
        """Ce que le compte a produit, apports retirés."""
        return self.end_value - self.start_value - self.external_flows

    @property
    def includes_cash(self) -> bool:
        return not any(p.reconstructed for p in self.points)

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    @property
    def mwr_reliable(self) -> bool:
        """Un XIRR sur quelques semaines extrapole un an de marché : à ne pas afficher."""
        return self.mwr is not None and self.days >= MIN_ANNUALIZE_DAYS

    @property
    def trustworthy(self) -> bool:
        """La série représente-t-elle assez du compte pour publier un rendement ?"""
        return self.coverage is None or self.coverage >= MIN_COVERAGE


def _wording(txn: TxnLike) -> str:
    return txn.simplified_wording or txn.wording or ""


def classify(txn: TxnLike, *, overrides: dict[int, str] | None = None) -> str:
    """Nature du mouvement : ``"external"``, ``"trade"`` ou ``"income"``.

    ``overrides`` (par id de transaction) a toujours le dernier mot : aucune heuristique
    ne devine à coup sûr qu'un « Versement » est un apport et qu'un « Boost sur
    versement » est un cadeau de l'assureur.
    """
    if overrides and txn.id is not None and txn.id in overrides:
        return overrides[txn.id]

    kind = (txn.type or "").lower()
    wording = _wording(txn)

    # Un mouvement de titres d'abord : « VENTE COMPTANT » arrive typée ``unknown`` avec
    # un montant positif, et la prendre pour un revenu inventerait de la performance.
    if kind in TRADE_TYPES or _TRADE_WORDING.search(wording):
        return "trade"
    if kind in EXTERNAL_TYPES:
        return "income" if _GAIN_WORDING.search(wording) else "external"
    return "income"


def qualify_flows(
    txns: list[TxnLike],
    *,
    account_id: int,
    overrides: dict[int, str] | None = None,
) -> list[Flow]:
    """Tous les mouvements du compte, chacun qualifié."""
    out = []
    for txn in txns:
        if txn.id_account != account_id or txn.date is None or txn.value is None:
            continue
        out.append(
            Flow(
                day=txn.date,
                amount=txn.value,
                label=_wording(txn),
                txn_id=txn.id,
                kind=classify(txn, overrides=overrides),
            )
        )
    return sorted(out, key=lambda f: f.day)


def reconstruct_series(
    values: list[dict[str, Any]], quantities: dict[int, Decimal]
) -> list[Point]:
    """Valorisation jour par jour, reconstruite depuis les VL archivées.

    ``quantities`` étant les quantités **d'aujourd'hui**, la série valorise le
    portefeuille actuel aux prix passés : elle est juste tant que la composition n'a pas
    changé sur la période, et les points sont marqués ``reconstructed`` pour que
    l'appelant puisse le dire. Elle ne contient que des titres — pas les liquidités, dont
    l'API ne publie aucune VL.
    """
    by_day: dict[date, Decimal] = {}
    for row in values:
        quantity = quantities.get(row["investment_id"])
        if quantity is None:
            continue
        by_day[row["day"]] = by_day.get(row["day"], Decimal(0)) + quantity * row["unit_value"]
    return [Point(day=day, value=by_day[day], reconstructed=True) for day in sorted(by_day)]


def twr(points: list[Point], flows: list[Flow], *, add_income: bool) -> float | None:
    """Rendement pondéré par le temps sur la série, par chaînage journalier.

    ``r = (V - V₋₁ - flux externes du jour) / V₋₁``, puis produit des ``1 + r``. Les flux
    du jour sont retirés du numérateur : un versement n'est pas une performance.

    Le dénominateur ignore le flux, ce qui revient à le placer **en fin de journée** — la
    convention du chaînage quotidien. Sur un pas d'un jour, l'écart avec la réalité est
    borné à une séance ; sur une série trouée il grandit, une raison de plus de collecter
    tous les jours plutôt qu'une fois par semaine.

    ``add_income`` distingue les deux natures de série :

    * **reconstruite** (quantités du jour appliquées aux prix passés, liquidités exclues) :
      un versement ou un achat n'y produit aucun saut — les titres achetés y figurent dès
      le premier point — donc rien n'est à retirer. En revanche les dividendes encaissés
      en cash sont hors périmètre et doivent être réinjectés, sinon ils apparaîtraient
      comme un gain évaporé ;
    * **soldes réels** : le cash et les mouvements y sont déjà, donc seuls les flux
      externes sont retirés, et rien n'est ajouté.

    Dans les deux cas les mouvements de titres sont ignorés : ils ne changent pas ce que
    vaut le compte.
    """
    if len(points) < 2:
        return None

    external: dict[date, Decimal] = {}
    income: dict[date, Decimal] = {}
    for flow in flows:
        if flow.is_external:
            external[flow.day] = external.get(flow.day, Decimal(0)) + flow.amount
        elif flow.is_income:
            income[flow.day] = income.get(flow.day, Decimal(0)) + flow.amount

    factor = 1.0
    for previous, current in zip(points, points[1:], strict=False):
        if previous.value <= 0:
            continue
        if add_income:
            numerator = (
                current.value
                - previous.value
                + _sum_window(income, previous.day, current.day)
            )
        else:
            numerator = (
                current.value
                - previous.value
                - _sum_window(external, previous.day, current.day)
            )
        factor *= 1.0 + float(numerator) / float(previous.value)
    return factor - 1.0


def _sum_window(by_day: dict[date, Decimal], after: date, until: date) -> Decimal:
    """Somme des montants tombant dans ``]after, until]``."""
    return sum(
        (amount for day, amount in by_day.items() if after < day <= until),
        Decimal(0),
    )


def xirr(
    cashflows: list[tuple[date, Decimal]],
    *,
    tolerance: float = 1e-7,
    max_iterations: int = 200,
) -> float | None:
    """Taux de rendement interne annualisé, par bissection.

    Bissection et non Newton : plus lente et parfaitement indifférente à la forme de la
    courbe, là où Newton diverge sur les séries de flux irrégulières. ``None`` quand les
    flux n'encadrent pas de solution (que des entrées, ou que des sorties).
    """
    flows = [(day, amount) for day, amount in cashflows if amount]
    if len(flows) < 2:
        return None
    if not (any(a > 0 for _, a in flows) and any(a < 0 for _, a in flows)):
        return None

    origin = min(day for day, _ in flows)

    def npv(rate: float) -> float:
        total = 0.0
        for day, amount in flows:
            years = (day - origin).days / 365.0
            base = 1.0 + rate
            if base <= 0:
                return float("inf")
            total += float(amount) / base**years
        return total

    low, high = -0.9999, 10.0
    npv_low, npv_high = npv(low), npv(high)
    if npv_low * npv_high > 0:
        return None
    for _ in range(max_iterations):
        middle = (low + high) / 2
        value = npv(middle)
        if abs(value) < tolerance:
            return middle
        if value * npv_low < 0:
            high, npv_high = middle, value
        else:
            low, npv_low = middle, value
    return (low + high) / 2


def compute(
    *,
    account_id: int,
    points: list[Point],
    flows: list[Flow],
    since: date | None = None,
    coverage: float | None = None,
) -> Performance | None:
    """Assemble la performance d'un compte sur la fenêtre couverte par ``points``.

    ``since`` restreint la fenêtre, ``coverage`` dit quelle part de la valorisation du
    compte la série représente. Renvoie ``None`` s'il reste moins de deux valorisations :
    sans deux points, il n'y a pas de variation à mesurer.
    """
    if since is not None:
        points = [p for p in points if p.day >= since]
    if len(points) < 2:
        return None

    start, end = points[0], points[-1]
    window = [f for f in flows if start.day < f.day <= end.day]
    reconstructed = any(p.reconstructed for p in points)

    external = sum((f.amount for f in window if f.is_external), Decimal(0))
    dividends = sum((f.amount for f in window if f.is_income and f.amount > 0), Decimal(0))
    fees = sum((f.amount for f in window if f.is_income and f.amount < 0), Decimal(0))

    # Un mouvement de titres pendant une fenêtre reconstruite change la composition, que
    # les quantités d'aujourd'hui ne rejouent pas.
    caveats = sum(1 for f in window if f.is_trade) if reconstructed else 0

    cashflows: list[tuple[date, Decimal]] = [(start.day, -start.value)]
    cashflows += [(f.day, -f.amount) for f in window if f.is_external]
    cashflows.append((end.day, end.value))

    return Performance(
        account_id=account_id,
        start=start.day,
        end=end.day,
        start_value=start.value,
        end_value=end.value,
        external_flows=external,
        dividends=dividends,
        fees=fees,
        twr=twr(points, window, add_income=reconstructed),
        mwr=xirr(cashflows),
        points=points,
        reconstruction_caveats=caveats,
        coverage=coverage,
    )


# Une poche de liquidités n'est pas un support : Powens la présente comme une ligne
# (``XX-liquidity`` chez Bourso) mais n'en publie aucune VL, et pour cause — du cash ne
# varie pas avec le marché.
_CASH_CODE = re.compile(r"liquidit|cash", re.IGNORECASE)
_CASH_LABEL = re.compile(r"^\s*(liquidit[ée]s?|cash|esp[èe]ces)\s*$", re.IGNORECASE)


def is_cash_line(*, code: str | None = None, label: str | None = None) -> bool:
    """Cette ligne est-elle une poche d'espèces plutôt qu'un support investi ?"""
    return bool(
        (code and _CASH_CODE.search(code)) or (label and _CASH_LABEL.match(label))
    )


def series_coverage(
    values: list[dict[str, Any]],
    valuations: dict[int, Decimal],
    account_value: Decimal | None,
    *,
    cash: Decimal | None = None,
) -> float | None:
    """Part de la partie **investie** du compte que les lignes historisées portent.

    Sans cette mesure, un contrat dont une poche sur deux est historisée afficherait une
    performance calculée sur la moitié du contrat, sans rien qui le signale.

    ``cash`` sort du dénominateur : sur un compte titres, les liquidités en attente
    d'emploi ne relèvent pas de la performance des titres, et les compter comme un trou
    dans la série ferait rejeter un compte parfaitement mesurable.
    """
    if not account_value or account_value <= 0:
        return None
    invested = account_value - (cash or Decimal(0))
    if invested <= 0:
        return None
    historised = {row["investment_id"] for row in values}
    covered = sum(
        (amount for inv_id, amount in valuations.items() if inv_id in historised),
        Decimal(0),
    )
    return float(covered / invested)
