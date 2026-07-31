"""Import de relevés bancaires CSV, pour les comptes qu'aucun connecteur ne remonte.

Powens ne couvre pas tout : un connecteur en panne ou absent laisse un compte entier
hors de l'analyse, et avec lui ses abonnements. Un relevé exporté depuis la banque suffit
à combler le trou, parce que tout le pipeline en aval (normalisation des libellés,
catégorisation, détection d'abonnements) travaille sur des transactions, sans se soucier
de leur provenance.

Le module ne fait que **parser** : il produit des :class:`pypowens.Transaction`, le type
que le reste de l'app manipule déjà, ce qui garantit qu'aucun code aval n'a besoin de
savoir qu'une opération vient d'un fichier. La persistance vit dans :mod:`app.store`.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from pypowens import Transaction

# Encodages tentés dans l'ordre : les banques françaises exportent encore beaucoup
# en Windows-1252, et latin-1 ne peut pas échouer (sert de filet).
_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")

# Préfixes de libellé → type d'opération Powens. L'ordre compte : le premier motif
# qui matche gagne, et « F COTISATION » doit passer avant un éventuel « F ».
#
# Ce mapping est ce qui rend les abonnements détectables : `detect_subscriptions()`
# ne regarde que certains rails (prélèvement, carte, frais), donc un « PRLV » mal
# typé disparaît purement et simplement de la page Abonnements.
_TYPE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^CARTE\b", re.I), "card"),
    (re.compile(r"^PRLV\b", re.I), "order"),
    (re.compile(r"^ECH\b", re.I), "loan_repayment"),
    (re.compile(r"^(RET|RETRAIT)\b", re.I), "withdrawal"),
    (re.compile(r"^ANNUL\b", re.I), "payback"),
    (re.compile(r"^(VIR|VERS|APPORT)\b", re.I), "transfer"),
    (re.compile(r"^(F |FRAIS|TENUE|REGUL|COTISATION|AGIOS)", re.I), "bank"),
    (re.compile(r"^(CHEQUE|CHQ)\b", re.I), "check"),
]

_HEADER_ALIASES = {
    "date": ("date operation", "date d'operation", "date", "date comptable"),
    "wording": ("libelle", "libelle operation", "nature de l'operation", "description"),
    "debit": ("debit", "montant debit"),
    "credit": ("credit", "montant credit"),
    "amount": ("montant", "montant eur", "montant de l'operation"),
}


class ImportError_(ValueError):
    """Le fichier n'est pas exploitable (colonnes introuvables, aucune ligne lisible)."""


@dataclass
class ParsedStatement:
    """Résultat du parsing, avant toute écriture en base."""

    transactions: list[Transaction] = field(default_factory=list)
    fingerprints: list[str] = field(default_factory=list)
    skipped: int = 0
    detected_columns: dict[str, str] = field(default_factory=dict)

    @property
    def first_date(self) -> date | None:
        days = [t.date for t in self.transactions if t.date]
        return min(days) if days else None

    @property
    def last_date(self) -> date | None:
        days = [t.date for t in self.transactions if t.date]
        return max(days) if days else None


def _decode(payload: bytes) -> str:
    for encoding in _ENCODINGS:
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("latin-1", errors="replace")


def _normalise(text: str) -> str:
    """Minuscule sans accents ni ponctuation superflue, pour reconnaître un en-tête."""
    stripped = unicodedata.normalize("NFKD", text or "")
    ascii_only = "".join(c for c in stripped if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", ascii_only.replace('"', "").strip().lower())


def parse_amount(raw: str) -> Decimal | None:
    """Montant à la française : ``1 048,63`` → ``Decimal("1048.63")``.

    Gère l'espace et l'espace insécable comme séparateurs de milliers, la virgule
    décimale, et un signe éventuel. ``None`` si la cellule n'est pas un nombre.
    """
    if not raw:
        return None
    cleaned = raw.replace(" ", "").replace(" ", "").replace(" ", "")
    cleaned = cleaned.replace("€", "").replace("EUR", "").strip()
    if not cleaned:
        return None
    negative = cleaned.startswith("-") or (cleaned.startswith("(") and cleaned.endswith(")"))
    cleaned = cleaned.strip("()-+")
    # Virgule décimale française ; un point restant est un séparateur de milliers.
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return -value if negative else value


def parse_date(raw: str) -> date | None:
    """``JJ/MM/AAAA``, ``JJ-MM-AAAA`` ou ``AAAA-MM-JJ``."""
    raw = (raw or "").strip()
    m = re.fullmatch(r"(\d{2})[/\-.](\d{2})[/\-.](\d{4})", raw)
    if m:
        day, month, year = (int(g) for g in m.groups())
    else:
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw)
        if not m:
            return None
        year, month, day = (int(g) for g in m.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def infer_type(wording: str) -> str:
    for pattern, kind in _TYPE_RULES:
        if pattern.search(wording or ""):
            return kind
    return "unknown"


def fingerprint(
    account_id: int, day: date, value: Decimal, wording: str, occurrence: int = 0
) -> str:
    """Identité stable d'une opération importée, pour que deux exports se recouvrant
    n'en fassent pas deux. Les relevés se chevauchent presque toujours.

    ``occurrence`` distingue les opérations réellement identiques du même jour — deux
    stationnements à 1,30 €, deux cafés au même prix. Sans lui, elles se confondent
    avec un doublon d'import et l'une des deux disparaît : une perte silencieuse qui
    minore les dépenses. Le rang est reproductible d'un import à l'autre, donc la
    déduplication d'un relevé réimporté continue de fonctionner.
    """
    key = f"{account_id}|{day.isoformat()}|{value}|{_normalise(wording)}|{occurrence}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _find_columns(fieldnames: list[str]) -> dict[str, str]:
    """Associe nos rôles aux colonnes réelles du fichier."""
    normalised = {_normalise(name): name for name in fieldnames if name}
    found: dict[str, str] = {}
    for role, aliases in _HEADER_ALIASES.items():
        for alias in aliases:
            if alias in normalised:
                found[role] = normalised[alias]
                break
    if "date" not in found or "wording" not in found:
        raise ImportError_(
            "Colonnes date et libellé introuvables. En-têtes lus : "
            + ", ".join(fieldnames or ["(aucun)"])
        )
    if "amount" not in found and not ({"debit", "credit"} & set(found)):
        raise ImportError_(
            "Aucune colonne de montant (ni Débit/Crédit, ni Montant). En-têtes lus : "
            + ", ".join(fieldnames)
        )
    return found


def parse_statement(payload: bytes, *, account_id: int) -> ParsedStatement:
    """Parse un relevé CSV en transactions rattachées à ``account_id``.

    Le séparateur est déduit du fichier (``;`` ou ``,``). Les lignes illisibles sont
    comptées dans ``skipped`` plutôt que de faire échouer l'import complet : un relevé
    contient souvent une ligne de total ou une ligne vide en fin de fichier.
    """
    text = _decode(payload)
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    columns = _find_columns(list(reader.fieldnames or []))

    result = ParsedStatement(detected_columns=columns)
    # Rang de chaque opération parmi ses jumelles exactes du même jour.
    ranks: Counter[tuple[str, str, str]] = Counter()
    for index, row in enumerate(reader):
        day = parse_date(row.get(columns["date"], ""))
        wording = (row.get(columns["wording"]) or "").strip()
        value: Decimal | None = None
        if "amount" in columns:
            value = parse_amount(row.get(columns["amount"], ""))
        if value is None:
            debit = parse_amount(row.get(columns.get("debit", ""), "") or "")
            credit = parse_amount(row.get(columns.get("credit", ""), "") or "")
            # Colonnes séparées : le débit est une sortie, donc négatif.
            if debit is not None and debit != 0:
                value = -abs(debit)
            elif credit is not None and credit != 0:
                value = abs(credit)
        if day is None or value is None or not wording:
            result.skipped += 1
            continue

        twin = (day.isoformat(), str(value), _normalise(wording))
        occurrence = ranks[twin]
        ranks[twin] += 1
        digest = fingerprint(account_id, day, value, wording, occurrence)
        result.transactions.append(
            Transaction.from_api(
                {
                    # id négatif : l'espace positif appartient à Powens, et les ids
                    # servent d'ensembles d'exclusion (virements internes, séries).
                    "id": -(index + 1),
                    "id_account": account_id,
                    "date": day.isoformat(),
                    "value": str(value),
                    "type": infer_type(wording),
                    "wording": wording,
                    "simplified_wording": wording,
                    "original_wording": wording,
                    "coming": False,
                }
            )
        )
        result.fingerprints.append(digest)

    if not result.transactions:
        raise ImportError_("Aucune opération lisible dans le fichier.")
    return result
