"""Transaction enrichment: merchant normalization, local categorization and
internal-transfer detection.

Powens categories/counterparty are empty on this app, so everything is derived
from the transaction wording. Pure functions, no network — unit-testable.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Protocol


class Txn(Protocol):
    id: int | None
    id_account: int | None
    type: str | None
    value: Any
    date: Any
    wording: str | None
    simplified_wording: str | None
    original_wording: str | None


# --------------------------------------------------------------- merchant key

_CB_SUFFIX = re.compile(r"\s+CB\*.*$", re.IGNORECASE)
_LEADING_PREFIX = re.compile(
    r"^(PRLV SEPA|PRLV|VIR SEPA|VIR INST|VIR|PAIEMENT|PAYPAL\s*\*?|CB|ACHAT)\s+",
    re.IGNORECASE,
)
_NOISE_CUT = re.compile(
    r"\b(RUM|R[EÉ]F|REF|CONTRAT|CONTRACT|MANDAT|MDT|NUM[EÉ]RO|NUMERO|"
    r"FACT|ECH|ID EMETTEUR|ICS)\b.*",
    re.IGNORECASE,
)
_LONG_DIGITS = re.compile(r"\d{4,}")
_NON_NAME = re.compile(r"[^0-9A-Za-zÀ-ÿ&'\-\. ]")
_MULTISPACE = re.compile(r"\s+")


def clean_wording(text: str) -> str:
    """Strip references/noise from a wording, keeping the merchant part."""
    w = (text or "").strip()
    if "\\" in w:  # card format MERCHANT\CITY\ FR
        w = w.split("\\", 1)[0]
    w = _CB_SUFFIX.sub("", w)  # card format MERCHANT CB*1234
    w = _LEADING_PREFIX.sub("", w)
    w = _NOISE_CUT.sub("", w)
    w = _LONG_DIGITS.sub(" ", w)
    w = _NON_NAME.sub(" ", w)
    w = _MULTISPACE.sub(" ", w).strip(" -.")
    return w


def merchant_key(txn: Txn, *, max_tokens: int = 3) -> str:
    """Normalized merchant identifier used to group recurring transactions."""
    raw = txn.simplified_wording or txn.wording or txn.original_wording or ""
    cleaned = clean_wording(raw)
    tokens = [t for t in cleaned.upper().split() if len(t) > 1]
    key = " ".join(tokens[:max_tokens])
    return key or (raw or "").upper().strip()[:24] or "INCONNU"


# ----------------------------------------------------------------- categories

# Ordered: first matching rule wins. Keywords matched against the cleaned wording.
CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("Télécom / Internet", ("BOUYGUES", "SFR", "ORANGE", "SOSH", "FREE", "RED BY", "BYTEL",
                            "B&YOU")),
    ("Énergie / Eau", ("EDF", "ENGIE", "TOTALENERGIES", "TOTAL ENERGIES", "ENI", "EKWATEUR",
                       "VEOLIA", "SUEZ", "GRDF", "SAUR")),
    ("Assurance / Mutuelle", ("AXA", "MAIF", "MACIF", "MATMUT", "GMF", "APRIL", "ASSURANCE",
                              "MUTUELLE", "KEREIS", "SATEC", "MULTI IMPACT", "AM GESTION",
                              "ALLIANZ", "GENERALI", "MAAF", "GROUPAMA", "SWISSLIFE")),
    ("Streaming / Loisirs", ("NETFLIX", "SPOTIFY", "DEEZER", "DISNEY", "PRIME VIDEO",
                             "AMAZON PRIME", "CANAL", "YOUTUBE", "APPLE.COM", "ITUNES",
                             "PLAYSTATION", "XBOX", "STEAM", "MOLOTOV", "OCS", "AUDIBLE")),
    ("Logiciel / Abonnement", ("OPENAI", "ANTHROPIC", "GITHUB", "NOTION", "ADOBE", "MICROSOFT",
                               "DROPBOX", "ICLOUD", "OVH", "AWS", "GOOGLE", "LINKEDIN", "FIGMA",
                               "SLACK", "ZOOM")),
    ("Sport", ("BASIC FIT", "BASICFIT", "FITNESS", "NEONESS", "KEEPCOOL", "ONAIR", "SALLE DE SPORT",
               "DECATHLON")),
    ("Alimentation", ("CARREFOUR", "AUCHAN", "LECLERC", "LIDL", "INTERMARCHE", "MONOPRIX", "CASINO",
                      "FRANPRIX", "PICARD", "BIOCOOP", "GRAND FRAIS", "NATURALIA", "ALDI",
                      "COURSES U")),
    ("Restauration", ("DELIVEROO", "UBER EATS", "UBEREATS", "MCDO", "MC DONALD", "BURGER", "SUBWAY",
                      "KFC", "RESTAURANT", "BRASSERIE", "GLOBETRINQUEUR", "RIVIERE KWAI")),
    ("Transport", ("SNCF", "TRAINLINE", "UBER", "BLABLACAR", "RATP", "TCL", "ESSO", "SHELL", "BP ",
                   "VINCI", "AUTOROUTE", "NAVIGO", "BOLT", "PARKING", "STATION")),
    ("Santé", ("PHARMACIE", "DOCTOLIB", "LABORATOIRE", "MEDECIN", "DENTAIRE", "OPTIC", "HOPITAL",
               "CLINIQUE")),
    ("Impôts & taxes", ("DGFIP", "IMPOT", "TRESOR PUBLIC", "URSSAF", "TAXE", "FINANCES PUBLIQUES")),
    ("Logement", ("LOYER", "FONCIA", "NEXITY", "SYNDIC", "SERGIC", "ORPI", "IMMOBILIER")),
    ("Banque & frais", ("COTISATION", "FRAIS", "COMMISSION", "AGIOS", "INTERETS", "BOURSO")),
]


def categorize(text: str) -> str:
    """Best-effort category label from a wording (merchant key or raw)."""
    up = (text or "").upper()
    for label, keywords in CATEGORY_RULES:
        if any(kw in up for kw in keywords):
            return label
    return "Autre"


# ---------------------------------------------------- internal transfers

_INTERNAL_WORDING = re.compile(
    r"\bEPGN\b|VIREMENT (DE|DEPUIS|INTERNE)|VIR EPGN|DEPUIS COMPTE SUR LIVRE",
    re.IGNORECASE,
)


def internal_transfer_ids(transactions: list[Txn], *, day_tolerance: int = 3) -> set[int]:
    """Identify transfers moving money between the user's own accounts.

    Two signals:
    * a *mirror* transfer (opposite sign, same |amount|, different account, close date);
    * a wording that clearly marks an internal savings move (``EPGN``, ``Virement depuis``…).
    """
    ids: set[int] = set()
    transfers = [
        t for t in transactions
        if t.type == "transfer" and t.value is not None and t.date is not None and t.id is not None
    ]

    # Wording heuristic.
    for t in transfers:
        text = t.simplified_wording or t.wording or ""
        if _INTERNAL_WORDING.search(text):
            ids.add(t.id)

    # Mirror detection.
    by_amount: dict[float, list[Txn]] = defaultdict(list)
    for t in transfers:
        by_amount[round(abs(float(t.value)), 2)].append(t)
    for group in by_amount.values():
        debits = [t for t in group if float(t.value) < 0]
        credits = [t for t in group if float(t.value) > 0]
        used: set[int] = set()
        for d in debits:
            for c in credits:
                if c.id in used or d.id == c.id:
                    continue
                if d.id_account != c.id_account and abs((d.date - c.date).days) <= day_tolerance:
                    ids.add(d.id)
                    ids.add(c.id)
                    used.add(c.id)
                    break
    return ids
