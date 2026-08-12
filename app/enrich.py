"""Transaction enrichment: merchant normalization, local categorization and
internal-transfer detection.

Powens categories/counterparty are empty on this app, so everything is derived
from the transaction wording. Pure functions, no network — unit-testable.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
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


# ------------------------------------------------------------- transaction types

# Everyday consumption (money actually spent), excluding transfers/investment.
CONSUMPTION_TYPES = frozenset(
    {"card", "deferred_card", "order", "withdrawal", "bank", "fee", "payment", "check"}
)
# Rails used by real subscriptions / recurring bills (prélèvement auto + carte).
# ``loan_repayment`` belongs here: a mortgage instalment is a fixed contractual
# monthly commitment, which is exactly what the subscriptions view exists to total —
# and usually the largest line on it.
SUBSCRIPTION_TYPES = frozenset({"order", "card", "deferred_card", "loan_repayment"})


# --------------------------------------------------------------- merchant key

_CB_SUFFIX = re.compile(r"\s+CB\*.*$", re.IGNORECASE)
# ``CARTE 22/07 MARCHAND`` is how statement exports label a card payment (Powens
# strips it, a CSV export does not). The date must go with the prefix: left in, the
# merchant key becomes "CARTE 22 07" and every card payment of the same day groups
# together instead of by merchant.
_LEADING_PREFIX = re.compile(
    r"^(CARTE\s+\d{2}[/\-.]\d{2}|CARTE|PRLV SEPA|PRLV|VIR SEPA|VIR INST|VIR|VERS|"
    r"ECH|RET DAB|RETRAIT|PAIEMENT|PAYPAL\s*\*?|CB|ACHAT)\s+",
    re.IGNORECASE,
)
# The trailing ``(?:\b|(?=\d))`` matters: banks glue the reference straight onto the
# keyword ("CONTRAT0000021673190104"), where a plain ``\b`` never matches — the same
# insurer then yields two merchant keys and its premium history splits in half.
# Requiring a digit (rather than dropping the boundary) keeps "REFECTOIRE" intact.
_NOISE_CUT = re.compile(
    r"\b(RUM|R[EÉ]F|REF|CONTRAT|CONTRACT|MANDAT|MDT|NUM[EÉ]RO|NUMERO|"
    r"FACT|ECH|ID EMETTEUR|ICS)(?:\b|(?=\d)).*",
    re.IGNORECASE,
)
_LONG_DIGITS = re.compile(r"\d{4,}")
_NON_NAME = re.compile(r"[^0-9A-Za-zÀ-ÿ&'\-\. ]")
_MULTISPACE = re.compile(r"\s+")


# Mémoïsés : ces normalisations (6 passes de regex) sont appelées 3 à 4 fois par
# transaction et par page (détecteur, passe stricte, template). Les libellés se
# répètent massivement — un marchand = des dizaines d'opérations — donc le cache
# transforme un coût par transaction en coût par libellé distinct.


@lru_cache(maxsize=16384)
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


@lru_cache(maxsize=16384)
def _merchant_key_of(raw: str, max_tokens: int) -> str:
    cleaned = clean_wording(raw)
    tokens = [t for t in cleaned.upper().split() if len(t) > 1]
    key = " ".join(tokens[:max_tokens])
    return key or raw.upper().strip()[:24] or "INCONNU"


def merchant_key(txn: Txn, *, max_tokens: int = 3) -> str:
    """Normalized merchant identifier used to group recurring transactions."""
    raw = txn.simplified_wording or txn.wording or txn.original_wording or ""
    return _merchant_key_of(raw, max_tokens)


# ----------------------------------------------------------------- categories

# Only well-known, generic brands live here (this file is versioned in a public
# repo). Merchants specific to your own statements — local shops, niche insurers,
# brokers — belong in ``categories.local.json`` (gitignored), which is loaded
# first so it can also override a default rule. See categories.local.example.json.
_LOCAL_RULES_PATH = Path(__file__).resolve().parent.parent / "categories.local.json"


def load_local_rules(path: Path | None = None) -> list[tuple[str, tuple[str, ...]]]:
    """Load private category rules from a JSON ``{"Category": ["KEYWORD", ...]}`` map.

    Missing or malformed file -> no local rule (the app must never fail to start
    because of it).
    """
    path = path or _LOCAL_RULES_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    rules: list[tuple[str, tuple[str, ...]]] = []
    for label, keywords in data.items():
        if str(label).startswith("_"):  # "_comment" and friends
            continue
        if isinstance(keywords, str):
            keywords = [keywords]
        if not isinstance(keywords, list):
            continue
        cleaned = tuple(str(k).upper().strip() for k in keywords if str(k).strip())
        if cleaned:
            rules.append((str(label), cleaned))
    return rules


# Ordered: first matching rule wins. Keywords matched against the cleaned wording.
#
# Order is load-bearing, not cosmetic. Two rules deliberately rely on it:
# ``TOTALENERGIES`` (Énergie) must be tested before the bare ``TOTAL`` of a fuel
# station, and ``AMAZON PRIME`` (Médias) before the bare ``AMAZON`` of a parcel.
# Moving a block up or down silently reclassifies spending.
CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    # Keywords are matched against the *merchant key*, which keeps only the first
    # few tokens — so they must stay short enough to survive that truncation
    # ("DIRECTION GENERALE", not "DIRECTION GENERALE DES FINANCES PUBLIQUES").
    ("Impôts & taxes", ("DGFIP", "DIRECTION GENERALE", "IMPOT", "TRESOR PUBLIC",
                        "URSSAF", "TAXE", "FINANCES PUBLIQUES")),
    ("Frais bancaires", ("OFFRE METAL", "COTISATION", "AGIOS", "FRAIS DE GESTION", "FRAIS DE TENUE",
                         "FRAIS BANC", "COMMISSION D", "DROITS DE GARDE")),
    ("Assurance / Mutuelle", ("AXA", "MAIF", "MACIF", "MATMUT", "GMF", "APRIL", "ASSURANCE",
                              "MUTUELLE", "ALLIANZ", "GENERALI", "MAAF", "GROUPAMA",
                              "SWISSLIFE", "PREVOYANCE", "ASSUR", "ORADEA", "HENNER")),
    ("Énergie / Eau", ("EDF", "ENGIE", "TOTALENERGIES", "TOTAL ENERGIES", "ENI", "EKWATEUR",
                       "ENEDIS", "VEOLIA", "SUEZ", "GRDF", "SAUR", "PRIMAGAZ")),
    ("Télécom / Internet", ("BOUYGUES", "SFR", "ORANGE", "SOSH", "FREE", "RED BY", "BYTEL",
                            "B&YOU", "PRIXTEL", "LYCAMOBILE")),
    ("Streaming / Médias", ("NETFLIX", "SPOTIFY", "DEEZER", "DISNEY", "PRIME VIDEO",
                            "AMAZON PRIME", "CANAL", "YOUTUBE", "MOLOTOV", "OCS", "AUDIBLE",
                            "PLAYSTATION", "XBOX", "STEAM", "INSTANT GAMING")),
    ("Logiciel / Cloud", ("OPENAI", "CHATGPT", "ANTHROPIC", "CLAUDE", "GITHUB", "NOTION", "ADOBE",
                          "MICROSOFT", "DROPBOX", "ICLOUD", "APPLE.COM", "ITUNES", "OVH", "AWS",
                          "GOOGLE", "LINKEDIN", "FIGMA", "SLACK", "ZOOM", "PROTON", "HOSTING",
                          "INSTANT INK", "HPI")),
    # After Énergie: a bare "TOTAL" here is a fuel station, TotalEnergies matched above.
    # "DAC" is how French statements mark a supermarket fuel pump (distributeur
    # automatique de carburant) — those rows are fuel, not groceries.
    ("Carburant", ("CARBU", "DAC AUCHAN", "AUCHAN DAC", "CARREFOUR DAC", "LECLERC DAC", "DAC ",
                   "STATION", "ESSO", "SHELL", "AVIA", "AGIP", "BP ", "TOTAL")),
    ("Moto", ("GEORIDE", "IN&MOTION", "INMOTION", "ASSUMOTO", "MOTOBLOUZ", "DAFY", "BECANERIE",
              "CARDY", "MAXXESS", "MOTARD", "CIRCUIT", "TRACK DAY")),
    ("Auto", ("AUTOROUTE", "VINCI", "ULYS", "PARKING", "NORAUTO", "FEU VERT", "MIDAS", "SPEEDY",
              "CONTROLE TECHNIQUE", "CARGLASS", "EUROMASTER", "PORSCHE", "MOTORS", "PEUGEOT",
              "RENAULT", "CITROEN", "VOLKSWAGEN", "GARAGE")),
    ("Alimentation", ("CARREFOUR", "AUCHAN", "LECLERC", "LIDL", "INTERMARCHE", "MONOPRIX", "CASINO",
                      "FRANPRIX", "PICARD", "BIOCOOP", "GRAND FRAIS", "NATURALIA", "ALDI",
                      "COURSES U", "SUPER U", "BOULANGERIE", "PATISSERIE", "FROMENTIER")),
    ("Restauration", ("DELIVEROO", "UBER EATS", "UBEREATS", "MCDO", "MC DONALD", "BURGER", "SUBWAY",
                      "KFC", "RESTAURANT", "BRASSERIE", "PIZZ", "SUSHI", "TRAITEUR", "BAR ",
                      "CAFE", "BISTRO")),
    ("Transport", ("SNCF", "TRAINLINE", "UBER", "BLABLACAR", "RATP", "TCL", "NAVIGO", "BOLT",
                   "VELOV", "AIR FRANCE", "HOPPER", "TRANSAVIA", "EASYJET")),
    ("Santé", ("PHARMACIE", "DOCTOLIB", "DOCTORA", "LABORATOIRE", "MEDECIN", "DENTAIRE", "OPTIC",
               "HOPITAL", "CLINIQUE")),
    # "PRET" arrive après le bloc Assurance, donc « assurance de prêt » est déjà classée
    # en assurance : ce qui tombe ici, c'est l'échéance de crédit immobilier elle-même.
    ("Logement / charges", ("LOYER", "FONCIA", "NEXITY", "SYNDIC", "SERGIC", "ORPI", "IMMOBILIER",
                            "REGIE", "COPRO", "PRET", "CREDIT LOGT", "EMPRUNT")),
    ("Maison / bricolage", ("LEROY MERLIN", "ADEO", "CASTORAMA", "BRICO", "IKEA", "DOMADOO",
                            "CONSUEL", "WELDOM", "MR.BRICOLAGE")),
    ("Sport / Loisirs", ("BASIC FIT", "BASICFIT", "FITNESS", "NEONESS", "KEEPCOOL", "ONAIR",
                         "ON AIR", "SALLE DE SPORT", "DECATHLON", "PARACHUT", "SPORT")),
    ("Shopping / Équipement", ("AMAZON", "AMZN", "FNAC", "BOULANGER", "DARTY", "CDISCOUNT",
                               "ZALANDO", "EMMA SLEEP", "APPLE")),
]

# Categories that describe day-to-day consumption rather than a contract. A
# subscriptions view must never present these as subscriptions: two visits to the
# same restaurant a year apart look exactly like an annual renewal.
EVERYDAY_CATEGORIES = frozenset(
    {"Alimentation", "Restauration", "Carburant", "Retrait espèces", "Shopping / Équipement"}
)

# Transaction types that name their own category better than any wording can: an
# ATM withdrawal is labelled with the dispenser's location, never with "retrait".
_TYPE_CATEGORY_OVERRIDE = {"withdrawal": "Retrait espèces"}
_TYPE_CATEGORY_FALLBACK = {"fee": "Frais bancaires", "market_fee": "Frais bancaires"}


# Local (private) rules take precedence over the generic ones.
_ACTIVE_RULES: list[tuple[str, tuple[str, ...]]] = load_local_rules() + CATEGORY_RULES


@lru_cache(maxsize=16384)
def _categorize_default(up: str) -> str:
    """Parcours des ~200 mots-clés, mémoïsé par libellé (règles fixes du process)."""
    for label, keywords in _ACTIVE_RULES:
        if any(kw in up for kw in keywords):
            return label
    return "Autre"


def categorize(
    text: str, *, rules: list[tuple[str, tuple[str, ...]]] | None = None
) -> str:
    """Best-effort category label from a wording (merchant key or raw)."""
    up = (text or "").upper()
    if rules is not None:  # jeu de règles ad hoc : pas de cache
        for label, keywords in rules:
            if any(kw in up for kw in keywords):
                return label
        return "Autre"
    return _categorize_default(up)


def resolve_category(merchant: str, overrides: dict[str, str] | None = None) -> str:
    """Category for a merchant key, letting a stored manual override win.

    Overrides come from the local SQLite store (see :mod:`app.store`): a correction
    made in the UI must survive restarts without touching the rule tables.
    """
    if overrides:
        found = overrides.get((merchant or "").upper())
        if found:
            return found
    return categorize(merchant)


def resolve_category_txn(txn: Txn, overrides: dict[str, str] | None = None) -> str:
    """Category of a transaction, using its ``type`` where the wording cannot help.

    Same precedence as :func:`resolve_category` (a manual override always wins),
    then the transaction type for the cases it names better than any keyword —
    a cash withdrawal carries the dispenser's address, not the word "retrait".
    """
    key = merchant_key(txn)
    if overrides:
        found = overrides.get(key.upper())
        if found:
            return found
    kind = txn.type or ""
    forced = _TYPE_CATEGORY_OVERRIDE.get(kind)
    if forced:
        return forced
    category = categorize(key)
    if category == "Autre":
        return _TYPE_CATEGORY_FALLBACK.get(kind, category)
    return category


def all_categories() -> list[str]:
    """Every category label the UI can offer, in rule order, plus ``"Autre"``."""
    labels: list[str] = []
    for label, _ in _ACTIVE_RULES:
        if label not in labels:
            labels.append(label)
    for label in (*_TYPE_CATEGORY_OVERRIDE.values(), *_TYPE_CATEGORY_FALLBACK.values()):
        if label not in labels:
            labels.append(label)
    labels.append("Autre")
    return labels


# ---------------------------------------------------- internal transfers

_INTERNAL_WORDING = re.compile(
    r"\bEPGN\b|VIREMENT (DE|DEPUIS|INTERNE)|VIR EPGN|DEPUIS COMPTE SUR LIVRE",
    re.IGNORECASE,
)


def internal_transfer_ids(transactions: Sequence[Txn], *, day_tolerance: int = 3) -> set[int]:
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
        if t.id is not None and _INTERNAL_WORDING.search(text):
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
                if d.id is None or c.id is None or c.id in used or d.id == c.id:
                    continue
                if d.id_account != c.id_account and abs((d.date - c.date).days) <= day_tolerance:
                    ids.add(d.id)
                    ids.add(c.id)
                    used.add(c.id)
                    break
    return ids
