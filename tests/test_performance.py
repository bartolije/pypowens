"""Performance des comptes d'investissement.

Les pièges testés ici sont ceux qui rendent un chiffre de performance faux sans le
rendre invraisemblable : un versement compté comme un gain, un dividende compté comme
un versement, un TWR qui récompense le timing des apports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.performance import (
    Flow,
    Point,
    classify,
    compute,
    is_cash_line,
    qualify_flows,
    reconstruct_series,
    series_coverage,
    twr,
    xirr,
)


@dataclass
class Txn:
    """Le minimum que :mod:`app.performance` lit d'une transaction."""

    id: int | None = 1
    id_account: int | None = 9
    date: date | None = None
    value: Decimal | None = None
    type: str | None = None
    wording: str | None = None
    simplified_wording: str | None = None


# ------------------------------------------------------ qualification des flux

def test_a_versement_is_external_but_a_boost_is_not():
    """Powens type les deux en ``deposit`` : seul le libellé les sépare."""
    versement = Txn(type="deposit", wording="Versement", value=Decimal(50000))
    boost = Txn(type="deposit", wording="Boost sur versement au titre de l'opération 2025")
    assert classify(versement) == "external"
    assert classify(boost) == "income"


def test_participation_aux_benefices_is_a_gain_not_a_deposit():
    """Les intérêts d'un fonds euros : le cœur de sa performance, à ne pas neutraliser."""
    txn = Txn(type="deposit", wording="Participation aux bénéfices du fonds euro")
    assert classify(txn) == "income"


def test_trades_dividends_and_fees_stay_inside_the_account():
    for kind, wording in (
        ("market_order", "ACHAT COMPTANT"),
        ("profit", "COUPONS"),
        ("market_fee", "Frais de gestion"),
        ("unknown", "VENTE COMPTANT"),
        ("arbitrage", "Arbitrage en sortie"),
    ):
        assert classify(Txn(type=kind, wording=wording)) != "external", kind


def test_a_transfer_into_the_account_is_external():
    assert classify(Txn(type="transfer", wording="Compte ORD 30 janv. 2026")) == "external"


def test_an_override_wins_over_every_heuristic():
    """Aucune règle ne devinera tous les libellés : la main de l'utilisateur tranche."""
    boost = Txn(id=77, type="deposit", wording="Boost sur versement")
    assert classify(boost, overrides={77: "external"}) == "external"
    versement = Txn(id=78, type="deposit", wording="Versement")
    assert classify(versement, overrides={78: "income"}) == "income"


def test_qualify_flows_keeps_only_the_account_asked_for():
    txns = [
        Txn(id=1, id_account=9, date=date(2026, 7, 6), value=Decimal(100), type="deposit"),
        Txn(id=2, id_account=7, date=date(2026, 7, 6), value=Decimal(200), type="deposit"),
        Txn(id=3, id_account=9, date=None, value=Decimal(300), type="deposit"),
    ]
    flows = qualify_flows(txns, account_id=9)
    assert [f.txn_id for f in flows] == [1]


# --------------------------------------------------------------- série de valeurs

def test_reconstruct_series_values_todays_holdings_at_past_prices():
    values = [
        {"investment_id": 1, "day": date(2026, 7, 5), "unit_value": Decimal(100)},
        {"investment_id": 2, "day": date(2026, 7, 5), "unit_value": Decimal(10)},
        {"investment_id": 1, "day": date(2026, 7, 6), "unit_value": Decimal(110)},
        {"investment_id": 2, "day": date(2026, 7, 6), "unit_value": Decimal(10)},
    ]
    points = reconstruct_series(values, {1: Decimal(2), 2: Decimal(5)})
    assert [(p.day.day, p.value) for p in points] == [(5, Decimal(250)), (6, Decimal(270))]
    assert all(p.reconstructed for p in points)


def test_reconstruct_ignores_lines_whose_quantity_is_unknown():
    """Une ligne vendue depuis garde son historique de VL : la compter inventerait un actif."""
    values = [{"investment_id": 99, "day": date(2026, 7, 5), "unit_value": Decimal(100)}]
    assert reconstruct_series(values, {1: Decimal(2)}) == []


def test_reconstruct_carries_last_value_through_partial_days():
    """Un jour férié sur UNE place ne doit pas faire chuter le total de moitié.

    Deux ETF sur des calendriers différents : le 6, seul le n°1 publie. Sans
    report de la dernière VL connue, le total du jour valait 220 au lieu de 270,
    et le chaînage TWR enregistrait -18 % puis +18 % — qui, composés, ne se
    compensent pas.
    """
    values = [
        {"investment_id": 1, "day": date(2026, 7, 5), "unit_value": Decimal(100)},
        {"investment_id": 2, "day": date(2026, 7, 5), "unit_value": Decimal(10)},
        {"investment_id": 1, "day": date(2026, 7, 6), "unit_value": Decimal(110)},  # 2 muet
        {"investment_id": 1, "day": date(2026, 7, 7), "unit_value": Decimal(105)},
        {"investment_id": 2, "day": date(2026, 7, 7), "unit_value": Decimal(11)},
    ]
    points = reconstruct_series(values, {1: Decimal(2), 2: Decimal(5)})
    assert [(p.day.day, p.value) for p in points] == [
        (5, Decimal(250)),
        (6, Decimal(270)),  # 2×110 + 5×10 (VL du 5 reportée), pas 220
        (7, Decimal(265)),
    ]


def test_reconstruct_skips_days_before_a_line_first_value():
    """Avant la première VL d'une ligne, le total serait partiel — donc faux."""
    values = [
        {"investment_id": 1, "day": date(2026, 7, 5), "unit_value": Decimal(100)},
        {"investment_id": 1, "day": date(2026, 7, 6), "unit_value": Decimal(110)},
        {"investment_id": 2, "day": date(2026, 7, 6), "unit_value": Decimal(10)},
    ]
    points = reconstruct_series(values, {1: Decimal(2), 2: Decimal(5)})
    assert [(p.day.day, p.value) for p in points] == [(6, Decimal(270))]


# ------------------------------------------------------------------------- TWR

def _series(values: list[str], *, start: date = date(2026, 7, 1)) -> list[Point]:
    return [
        Point(day=start + timedelta(days=i), value=Decimal(v)) for i, v in enumerate(values)
    ]


def test_twr_is_the_plain_variation_without_any_flow():
    assert twr(_series(["100", "110"]), [], add_income=False) == pytest.approx(0.1)


def test_twr_ignores_a_deposit_entirely():
    """1 000 € versés puis 0 % de marché : la performance est nulle, pas +1 000 %."""
    points = _series(["100", "1100"])
    flows = [Flow(day=points[1].day, amount=Decimal(1000), label="Versement")]
    assert twr(points, flows, add_income=False) == 0.0


def test_twr_chains_sub_periods_so_the_timing_of_a_deposit_does_not_count():
    """Le TWR d'une même suite de rendements ne dépend pas du moment du versement.

    Les 1 000 € arrivent en fin de 2e journée (convention du chaînage quotidien) : ils ne
    participent donc pas au +10 % de cette journée, d'où 110 x 1,1 + 1 000.
    """
    early = twr(
        _series(["100", "110", "1121"]),
        [Flow(day=date(2026, 7, 3), amount=Decimal(1000), label="V")],
        add_income=False,
    )
    late = twr(_series(["100", "110", "121"]), [], add_income=False)
    assert early is not None and late is not None
    assert early == pytest.approx(late) == pytest.approx(0.21)


def test_twr_places_a_flow_at_the_end_of_the_day():
    """La convention retenue, énoncée : le dénominateur est la valeur de la veille.

    Sur un pas quotidien l'écart avec la réalité est borné à une séance ; sur une série
    trouée (série hebdomadaire, marché fermé), il grandit — d'où l'intérêt de collecter
    tous les jours.
    """
    points = _series(["100", "1100"])
    flow = [Flow(day=points[1].day, amount=Decimal(1000), label="V")]
    # 1 100 - 100 - 1 000 = 0 : le versement n'a pas eu le temps de rapporter.
    assert twr(points, flow, add_income=False) == 0.0


def test_twr_adds_income_back_on_a_reconstructed_series():
    """Série sans liquidités : un dividende encaissé sortirait du périmètre sinon."""
    points = _series(["100", "100"])
    dividend = [Flow(day=points[1].day, amount=Decimal(5), label="COUPONS", kind="income")]
    assert twr(points, dividend, add_income=False) == 0.0
    assert twr(points, dividend, add_income=True) == pytest.approx(0.05)


def test_twr_needs_two_points():
    assert twr(_series(["100"]), [], add_income=False) is None
    assert twr([], [], add_income=False) is None


def test_twr_skips_a_period_starting_from_zero():
    """Un compte à zéro n'a pas de rendement : diviser par lui planterait."""
    points = [
        Point(day=date(2026, 7, 1), value=Decimal(0)),
        Point(day=date(2026, 7, 2), value=Decimal(100)),
        Point(day=date(2026, 7, 3), value=Decimal(110)),
    ]
    assert twr(points, [], add_income=False) == pytest.approx(0.1)


# ------------------------------------------------------------------------ XIRR

def test_xirr_finds_a_simple_annual_return():
    rate = xirr([(date(2025, 1, 1), Decimal(-1000)), (date(2026, 1, 1), Decimal(1100))])
    assert rate is not None
    assert abs(rate - 0.10) < 0.01


def test_xirr_rewards_a_shorter_holding_period():
    """+10 % en six mois annualise plus haut que +10 % en un an."""
    half = xirr([(date(2026, 1, 1), Decimal(-1000)), (date(2026, 7, 1), Decimal(1100))])
    full = xirr([(date(2026, 1, 1), Decimal(-1000)), (date(2027, 1, 1), Decimal(1100))])
    assert half is not None and full is not None
    assert half > full


def test_xirr_returns_none_without_a_sign_change():
    assert xirr([(date(2026, 1, 1), Decimal(-100)), (date(2026, 7, 1), Decimal(-100))]) is None
    assert xirr([(date(2026, 1, 1), Decimal(100))]) is None


# --------------------------------------------------------------------- compute

def test_compute_separates_the_gain_from_the_deposits():
    points = _series(["100000", "105000", "160000"])
    flows = [
        Flow(day=points[2].day, amount=Decimal(50000), label="Versement"),
        Flow(day=points[2].day, amount=Decimal(300), label="COUPONS", kind="income"),
        Flow(day=points[2].day, amount=Decimal(-20), label="Frais", kind="income"),
    ]
    perf = compute(account_id=9, points=points, flows=flows)
    assert perf is not None
    assert perf.external_flows == Decimal(50000)
    assert perf.dividends == Decimal(300)
    assert perf.fees == Decimal(-20)
    # 160 000 - 100 000 - 50 000 : le versement n'est pas un gain.
    assert perf.gain == Decimal(10000)
    assert perf.includes_cash is True


def test_compute_restricts_the_window_and_reports_it():
    points = _series(["100", "110", "120", "130"])
    perf = compute(account_id=9, points=points, flows=[], since=points[2].day)
    assert perf is not None
    assert (perf.start, perf.end) == (points[2].day, points[3].day)
    assert perf.start_value == Decimal(120)


def test_compute_ignores_flows_outside_the_window():
    points = _series(["100", "110"])
    outside = [Flow(day=points[0].day - timedelta(days=5), amount=Decimal(50), label="V")]
    perf = compute(account_id=9, points=points, flows=outside)
    assert perf is not None and perf.external_flows == Decimal(0)


def test_compute_counts_trades_as_caveats_on_a_reconstructed_series():
    """Un achat pendant la fenêtre reconstruite = une composition qui a changé."""
    points = [
        Point(day=date(2026, 7, 5), value=Decimal(100), reconstructed=True),
        Point(day=date(2026, 7, 30), value=Decimal(110), reconstructed=True),
    ]
    flows = [
        Flow(day=date(2026, 7, 29), amount=Decimal(-7668), label="ACHAT COMPTANT", kind="trade"),
        Flow(day=date(2026, 7, 28), amount=Decimal(21), label="COUPONS", kind="income"),
    ]
    perf = compute(account_id=9, points=points, flows=flows)
    assert perf is not None
    assert perf.reconstruction_caveats == 1
    assert perf.includes_cash is False


# ------------------------------------------- ce qui rendait les chiffres réels faux

def test_a_share_purchase_does_not_dent_the_performance():
    """Le bug constaté : un achat de 7 668 € sur le PEA affichait -5,4 % au lieu de -1,1 %.

    Un achat convertit du cash en titres. Sur une série à quantités constantes, les titres
    achetés figurent dès le premier point : rien ne doit être retiré du rendement.
    """
    points = [
        Point(day=date(2026, 7, 5), value=Decimal(180000), reconstructed=True),
        Point(day=date(2026, 7, 31), value=Decimal(178000), reconstructed=True),
    ]
    trade = [
        Flow(day=date(2026, 7, 29), amount=Decimal(-7668), label="ACHAT COMPTANT", kind="trade")
    ]
    without = twr(points, [], add_income=True)
    with_trade = twr(points, trade, add_income=True)
    assert with_trade == without == pytest.approx(-0.0111, abs=1e-4)


def test_a_sale_typed_unknown_is_a_trade_not_an_income():
    """« VENTE COMPTANT » arrive typée ``unknown`` avec un montant positif."""
    assert classify(Txn(type="unknown", wording="VENTE COMPTANT ETR 31 mars 2026")) == "trade"
    assert classify(Txn(type="unknown", wording="REPRISE F.C.P. 6 janv. 2026")) == "trade"


def test_prelevements_sociaux_weigh_on_the_performance():
    assert classify(Txn(type="unknown", wording="Prélèvements sociaux")) == "income"


def test_coverage_flags_a_contract_only_half_historised():
    """Le fonds euros affichait -0,40 % : capital garanti, donc impossible.

    Une seule de ses deux poches publie une VL — la série ne portait que 21 573 € des
    53 817 € du contrat.
    """
    values = [{"investment_id": 1, "day": date(2026, 7, 5), "unit_value": Decimal(1)}]
    valuations = {1: Decimal(21573), 2: Decimal(32244)}
    coverage = series_coverage(values, valuations, Decimal(53817))
    assert coverage is not None and coverage == pytest.approx(0.4008, abs=1e-4)

    perf = compute(
        account_id=10,
        points=[
            Point(day=date(2026, 7, 5), value=Decimal(21573), reconstructed=True),
            Point(day=date(2026, 7, 30), value=Decimal(21488), reconstructed=True),
        ],
        flows=[],
        coverage=coverage,
    )
    assert perf is not None and perf.trustworthy is False


def test_full_coverage_is_trustworthy():
    values = [{"investment_id": 1, "day": date(2026, 7, 5), "unit_value": Decimal(1)}]
    coverage = series_coverage(values, {1: Decimal(1000)}, Decimal(1000))
    assert coverage == pytest.approx(1.0)
    perf = compute(
        account_id=9, points=_series(["100", "110"]), flows=[], coverage=coverage
    )
    assert perf is not None and perf.trustworthy is True


def test_coverage_is_unknown_without_an_account_value():
    assert series_coverage([], {}, None) is None
    assert series_coverage([], {}, Decimal(0)) is None


def test_cash_is_not_a_hole_in_the_series():
    """Le compte titres avait 23 062 € de liquidités : sans les sortir, il tombait à 91 %.

    Du cash en attente d'emploi ne relève pas de la performance des titres — le compter
    comme non historisé ferait rejeter un compte parfaitement mesurable.
    """
    values = [{"investment_id": 1, "day": date(2026, 7, 5), "unit_value": Decimal(1)}]
    valuations = {1: Decimal(243502)}
    naive = series_coverage(values, valuations, Decimal(266565))
    assert naive is not None and naive < 0.95  # ce que faisait la version précédente
    without_cash = series_coverage(
        values, valuations, Decimal(266565), cash=Decimal("23062.12")
    )
    assert without_cash is not None and without_cash == pytest.approx(1.0, abs=1e-4)


def test_cash_lines_are_recognised():
    assert is_cash_line(code="XX-liquidity") is True
    assert is_cash_line(label="Liquidités") is True
    assert is_cash_line(label="  Espèces ") is True
    # Un support investi dont le nom contient le mot ne doit pas être pris pour du cash.
    assert is_cash_line(label="Fonds Liquidités Dynamiques") is False
    assert is_cash_line(code="FR0000120073", label="AIR LIQUIDE") is False


def test_a_month_long_window_does_not_get_annualised():
    """-45,6 %/an extrapolé de 26 jours de marché : un chiffre spectaculaire et faux."""
    short = compute(account_id=9, points=_series(["100", "90"]), flows=[])
    assert short is not None
    assert short.days < 90
    assert short.mwr_reliable is False


def test_a_long_enough_window_can_be_annualised():
    points = [
        Point(day=date(2025, 1, 1), value=Decimal(1000)),
        Point(day=date(2026, 1, 1), value=Decimal(1100)),
    ]
    perf = compute(account_id=9, points=points, flows=[])
    assert perf is not None
    assert perf.mwr_reliable is True
    assert perf.mwr is not None and abs(perf.mwr - 0.10) < 0.01


def test_compute_needs_two_points():
    assert compute(account_id=9, points=_series(["100"]), flows=[]) is None
