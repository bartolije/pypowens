"""Vocabulaire patrimoine partagé par les pages synthèse, patrimoine et détail.

``recap.py`` et ``synthese.py`` portaient chacun leur copie de la taxonomie des
comptes, du « aujourd'hui » en français et de la construction des lignes de
titres — et les copies divergeaient déjà (deux ``_family_of``, deux
``_today_fr``, deux blocs ``invest_rows`` aux clés identiques). C'est le
mécanisme exact par lequel deux pages finissent par afficher deux chiffres
différents pour la même grandeur : ce module est la copie unique.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from pypowens import Investment

# Families are rendered in this declared order (empty ones are skipped).
FAMILY_ORDER = [
    "Comptes courants",
    "Épargne",
    "Investissement",
    "Assurance-vie",
    "Retraite",
    "Crédits",
    "Autre",
]

# Powens account ``type`` -> family label.
TYPE_TO_FAMILY = {
    "checking": "Comptes courants",
    "card": "Comptes courants",
    "livret_a": "Épargne",
    "ldds": "Épargne",
    "csl": "Épargne",
    "cel": "Épargne",
    "pel": "Épargne",
    "savings": "Épargne",
    "cat": "Épargne",
    "market": "Investissement",
    "pea": "Investissement",
    "lifeinsurance": "Assurance-vie",
    "per": "Retraite",
    "loan": "Crédits",
    "mortgage": "Crédits",
    "consumercredit": "Crédits",
}

_MONTHS_FR = [
    "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
]


def today_fr() -> str:
    """``12 Août 2026`` — sans passer par la locale du process (non fiable)."""
    d = date.today()
    return f"{d.day:02d} {_MONTHS_FR[d.month - 1]} {d.year}"


def family_of(account_type: str | None) -> str:
    """Map a raw account type to its family label (unknown -> 'Autre')."""
    return TYPE_TO_FAMILY.get(account_type or "", "Autre")


def build_invest_rows(
    investments: list[Investment],
    account_names: dict[int | None, str],
    base_currency: str,
) -> tuple[list[dict[str, Any]], Decimal, float]:
    """Lignes de titres prêtes à afficher, plus-value totale et % sur prix de revient.

    Le ``diff_percent`` de l'API est une **fraction** (0.1699 = +16,99 %) : les
    lignes rendues portent des pourcents. Le pourcentage global rapporte la
    plus-value latente au prix de revient (valorisation − plus-value), jamais au
    patrimoine total.
    """
    rows: list[dict[str, Any]] = sorted(
        (
            {
                "id_account": inv.id_account,
                "account": account_names.get(inv.id_account, "—"),
                "label": inv.label or inv.code or "—",
                "code": inv.code,
                "quantity": inv.quantity,
                "valuation": inv.valuation,
                "diff": inv.diff,
                "diff_percent": (
                    inv.diff_percent * 100 if inv.diff_percent is not None else None
                ),
                "currency": inv.currency or base_currency,
            }
            for inv in investments
        ),
        key=lambda row: row["valuation"] or Decimal(0),
        reverse=True,
    )
    invest_diff = sum((inv.diff or Decimal(0) for inv in investments), Decimal(0))
    invest_valuation = sum(
        (inv.valuation or Decimal(0) for inv in investments), Decimal(0)
    )
    invest_cost = invest_valuation - invest_diff
    invest_diff_pct = float(invest_diff / invest_cost * 100) if invest_cost else 0.0
    return rows, invest_diff, invest_diff_pct
