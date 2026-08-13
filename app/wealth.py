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


# ---------------------------------------------------------------- monogrammes


def readable_ink(hex_color: str) -> str:
    """Noir ou blanc, selon ce qui se lit sur ce fond.

    Un connecteur peut annoncer une couleur de marque très claire (l'un des
    connecteurs réels est blanc) : du texte blanc dessus serait invisible.
    """
    try:
        r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return "#ffffff"
    # Luminance perçue (ITU-R BT.601), suffisante pour un choix binaire.
    return "#111111" if (0.299 * r + 0.587 * g + 0.114 * b) > 0.6 else "#ffffff"


def monogram(connector: Any) -> tuple[str, str, str]:
    """(initiales, fond, encre) d'une banque — l'équivalent d'un logo, en local.

    ``GET /connectors/{id}/logos`` renvoie une liste vide sur cette app, mais
    l'objet connecteur porte sa **couleur de marque** et un **slug** : de quoi
    fabriquer une pastille reconnaissable, sans dépendre d'un CDN d'images ni
    faire fuiter la moindre requête vers l'extérieur.
    """
    raw = getattr(connector, "raw", None) or {}
    color = str(raw.get("color") or "").strip().lstrip("#")
    if len(color) != 6:
        color = "8a8a8a"
    slug = str(raw.get("slug") or "").strip()
    name = str(getattr(connector, "name", "") or "")
    initials = (slug or "".join(w[0] for w in name.split()[:2]) or "?")[:3].upper()
    return initials, f"#{color}", readable_ink(color)


# ------------------------------------------------------- pictogrammes de type

# Un pictogramme par type de dépense : sur un relevé de cinquante lignes, l'œil
# repère une forme bien plus vite qu'il ne lit une étiquette.
CATEGORY_EMOJI: dict[str, str] = {
    "Alimentation": "🛒",
    "Restauration": "🍽️",
    "Carburant": "⛽",
    "Transport": "🚆",
    "Auto": "🚗",
    "Moto": "🏍️",
    "Logement / charges": "🏠",
    "Énergie / Eau": "⚡",
    "Télécom / Internet": "📶",
    "Streaming / Médias": "🎬",
    "Logiciel / Cloud": "💻",
    "Assurance / Mutuelle": "🛡️",
    "Santé": "⚕️",
    "Sport / Loisirs": "🏃",
    "Voyage / Vacances": "✈️",
    "Shopping / Équipement": "🛍️",
    "Éducation / Enfance": "🎓",
    "Impôts & taxes": "🏛️",
    "Frais bancaires": "🏦",
    "Retrait espèces": "💵",
    "Épargne / Investissement": "📈",
    "Dons / Associations": "🤝",
    "Animaux": "🐾",
    "Virement interne": "🔄",
    "Salaire / Revenus": "💰",
    "Autre": "•",
}


def category_emoji(category: str) -> str:
    return CATEGORY_EMOJI.get(category, "•")


# ------------------------------------------------------- moyen de paiement

# Le « rail » emprunté par l'argent : lire une ligne, c'est aussi savoir si
# elle est passée par la carte (donc annulable, contestable) ou par un mandat
# de prélèvement (donc contractuelle). Le type Powens le dit déjà.
RAIL_LABEL: dict[str, tuple[str, str]] = {
    "card": ("💳", "Carte"),
    "deferred_card": ("💳", "Carte à débit différé"),
    "transfer": ("↔", "Virement"),
    "order": ("🔁", "Prélèvement"),
    "loan_repayment": ("🏦", "Échéance de prêt"),
    "withdrawal": ("🏧", "Retrait"),
    "check": ("🧾", "Chèque"),
    "deposit": ("⬇", "Dépôt"),
    "payback": ("↩", "Remboursement"),
    "bank": ("🏛", "Frais bancaires"),
    "market_order": ("📈", "Ordre de bourse"),
    "market_fee": ("📈", "Frais de marché"),
    "arbitrage": ("⚖", "Arbitrage"),
    "profit": ("💹", "Gain"),
}


def rail(kind: str | None) -> tuple[str, str]:
    """(pictogramme, libellé) du moyen de paiement — vide si Powens ne dit rien."""
    return RAIL_LABEL.get((kind or "").lower(), ("", ""))
