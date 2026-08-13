"""Graduations des graphiques : une baisse doit être chiffrable, pas seulement visible.

Les montants d'un axe sont des montants comme les autres : ils doivent tomber sous le
masque global, sinon l'axe rend en clair ce que la courbe floute.
"""

from __future__ import annotations

import re

from app.helpers import format_axis, line_chart, nice_step


def _plain(text: str) -> str:
    """Normalise les espaces fines et insécables des montants formatés."""
    return re.sub(r"[\s\u202f\u00a0]+", " ", text)


def test_nice_step_rounds_to_readable_increments():
    """Un pas brut (amplitude / 4) donnerait des graduations du genre 1 736 €."""
    assert nice_step(6195, 4) == 2000.0
    assert nice_step(100, 4) == 25.0
    assert nice_step(8, 4) == 2.0
    assert nice_step(0.4, 4) == 0.1


def test_nice_step_survives_a_flat_series():
    assert nice_step(0) == 1.0
    assert nice_step(-5) == 1.0


def test_format_axis_abbreviates_by_step_not_by_value():
    # Pas de 2 000 sur des montants à six chiffres : les milliers suffisent.
    assert _plain(format_axis(178000, 2000)) == "178 k€"
    # Pas de 500 k : les graduations valent 1,0 M puis 1,5 M, la décimale est nécessaire.
    # (Une graduation est toujours un multiple du pas, jamais un 1,25 à arrondir.)
    assert _plain(format_axis(1500000, 500000)) == "1,5 M€"
    assert _plain(format_axis(2000000, 500000)) == "2,0 M€"
    # Pas serré : la décimale devient indispensable pour ne pas afficher deux fois 178 k€.
    assert _plain(format_axis(178200, 200)) == "178,2 k€"
    assert _plain(format_axis(850, 100)) == "850 €"


def test_line_chart_draws_graduated_values():
    points = [(f"{d:02d}/07", 180000 - d * 200) for d in range(1, 20)]
    svg = line_chart(points)
    assert svg.count('class="grid"') >= 3  # des lignes de repère, pas une seule
    assert "k€" in svg
    # Les graduations sont des montants : le masque global doit les couvrir.
    labels = re.findall(r'class="cv amount">([^<]+)</text>', svg)
    assert len(labels) >= 3, labels


def test_line_chart_labels_bracket_the_data():
    """La courbe doit rester dans le cadre : bornes arrondies au pas, vers l'extérieur."""
    svg = line_chart([("a", 174000.0), ("b", 178200.0)])
    labels = re.findall(r'class="cv amount">([^<]+)</text>', svg)
    numbers = [
        float(_plain(t).replace(" k€", "").replace(" ", "").replace(",", "."))
        for t in labels
    ]
    assert min(numbers) * 1000 <= 174000
    assert max(numbers) * 1000 >= 178200


def test_line_chart_is_not_stretched():
    """``preserveAspectRatio="none"`` écrasait horizontalement chaque libellé."""
    svg = line_chart([("a", 1.0), ("b", 2.0)])
    assert 'preserveAspectRatio="none"' not in svg
    assert 'preserveAspectRatio="xMinYMin meet"' in svg


def test_line_chart_still_refuses_a_single_point():
    assert "Pas encore assez d'historique" in line_chart([("a", 1.0)])


def test_a_flat_series_still_renders_an_axis():
    """Un fonds euros ne bouge pas : diviser par une amplitude nulle planterait."""
    svg = line_chart([("a", 21573.0), ("b", 21573.0), ("c", 21573.0)])
    assert 'class="grid"' in svg
    assert "polyline" in svg


def test_line_chart_can_overlay_a_benchmark():
    from app.helpers import line_chart

    svg = line_chart(
        [("01/08", 100.0), ("02/08", 110.0), ("03/08", 105.0)],
        benchmark=[100.0, None, 108.0],
        benchmark_label="MSCI World (IWDA)",
    )
    assert "stroke-dasharray" in svg
    assert "MSCI World (IWDA)" in svg
    # Un benchmark mal aligné (longueur différente) est ignoré, jamais une erreur.
    svg = line_chart([("a", 1.0), ("b", 2.0)], benchmark=[1.0], benchmark_label="X")
    assert "stroke-dasharray" not in svg


def test_benchmark_overlay_rebases_on_the_series_start():
    """La lecture : « si la même somme était sur l'indice »."""
    from datetime import date
    from decimal import Decimal

    from app.investments import _benchmark_overlay
    from app.performance import Point

    kept = [
        Point(day=date(2026, 8, 1), value=Decimal("1000")),
        Point(day=date(2026, 8, 3), value=Decimal("1050")),  # le 2 = week-end
    ]
    closes = [
        (date(2026, 8, 1), Decimal("100")),
        (date(2026, 8, 3), Decimal("104")),
    ]
    overlay = _benchmark_overlay(kept, closes)
    assert overlay == [1000.0, 1040.0]  # rebasé sur 1000, +4 % comme l'indice
    # Sans clôture archivée : pas d'overlay, pas d'erreur.
    assert _benchmark_overlay(kept, []) == []
