"""Local SQLite store — the app's own memory, next to Powens' live data.

Powens only ever answers "what is true now". Three things need history instead:

* **balance snapshots** — a real net-worth curve, instead of guessing a variation
  from an undocumented ``diff`` field;
* **category overrides** — a correction made once must stick, without editing code;
* **series state** — knowing a subscription is *new*, or that its price *went up*,
  requires remembering what it looked like last time.

Connections are short-lived and statements are tiny (a few thousand rows), so
plain synchronous ``sqlite3`` is used: every call here is sub-millisecond, well
below the cost of one Powens round-trip.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

SCHEMA = """
CREATE TABLE IF NOT EXISTS balance_snapshot (
    day        TEXT    NOT NULL,          -- YYYY-MM-DD
    account_id INTEGER NOT NULL,
    name       TEXT,
    type       TEXT,
    currency   TEXT    NOT NULL,
    balance    TEXT    NOT NULL,          -- Decimal serialized as text
    PRIMARY KEY (day, account_id)
);
CREATE INDEX IF NOT EXISTS idx_snapshot_day ON balance_snapshot (day);

CREATE TABLE IF NOT EXISTS category_override (
    merchant_key TEXT PRIMARY KEY,
    category     TEXT NOT NULL,
    updated      TEXT NOT NULL
);

-- Notes de périmètre acquittées. Un changement de périmètre est un fait
-- permanent : la courbe portera toujours ce saut. Une fois qu'on l'a compris,
-- répéter l'explication à chaque affichage devient du bruit — sans pour autant
-- justifier de trafiquer l'historique pour lisser la courbe.
CREATE TABLE IF NOT EXISTS perimeter_ack (
    day     TEXT PRIMARY KEY,
    label   TEXT,
    updated TEXT NOT NULL
);

-- Réglages modifiables depuis l'interface. L'environnement (.env) fournit les
-- valeurs par DÉFAUT ; ce que l'utilisateur change ici les remplace, sans
-- édition de fichier ni redémarrage. Les secrets (identifiants Powens) et le
-- bootstrap (hôte, port, chemin de la base) restent hors de portée : ils sont
-- nécessaires AVANT que cette base ne soit ouverte.
CREATE TABLE IF NOT EXISTS setting (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL,
    updated TEXT NOT NULL
);

-- Identité STABLE d'un compte, insensible au renumérotage de Powens. Quand une
-- connexion tombe, Powens supprime ses comptes et les recrée sous de nouveaux
-- ids : l'historique se scindait en deux, la courbe sautait du montant du
-- compte à l'aller comme au retour (vécu le 01/08 avec un prêt de -257 k€, qui
-- a porté quatre ids successifs). Cette table retient « telle signature = tel
-- id courant » et permet de recoller l'historique au passage suivant.
CREATE TABLE IF NOT EXISTS account_identity (
    signature  TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL,
    name       TEXT,
    updated    TEXT NOT NULL
);

-- Clôtures quotidiennes d'un indice de référence (via yfinance, par le
-- collecteur) : la page performance se compare à lui sans appel réseau.
CREATE TABLE IF NOT EXISTS benchmark_value (
    ticker TEXT NOT NULL,
    day    TEXT NOT NULL,
    close  TEXT NOT NULL,                 -- Decimal en texte
    PRIMARY KEY (ticker, day)
);

-- Renommage local d'un compte (le nom Powens est souvent générique : trois
-- « M BARTOLI JEREMIE » indistinguables). Appliqué à la lecture des comptes.
CREATE TABLE IF NOT EXISTS account_alias (
    account_id INTEGER PRIMARY KEY,
    name       TEXT NOT NULL
);

-- Fusion de marchands : deux clés normalisées qui désignent le même marchand
-- (libellés carte vs prélèvement) regroupées sous une clé cible unique.
CREATE TABLE IF NOT EXISTS merchant_alias (
    from_key TEXT PRIMARY KEY,
    to_key   TEXT NOT NULL
);

-- Enveloppes mensuelles par catégorie de dépense. Le suivi (« où j'en suis le
-- 20 du mois ») se calcule sur le mois COURANT, contrairement à /analyse qui
-- raisonne en mois complets.
CREATE TABLE IF NOT EXISTS budget (
    category TEXT PRIMARY KEY,
    monthly  TEXT NOT NULL,               -- Decimal en texte
    updated  TEXT NOT NULL
);

-- Comptes à garder dans le périmètre quoi qu'en dise Powens. Après une panne de
-- connexion, Powens recrée parfois un compte à l'état DÉSACTIVÉ (le prêt du
-- 02/08) : le bouton « Réintégrer » le remettait, mais il fallait recommencer à
-- chaque rechute. L'épingle porte l'identité STABLE du compte (IBAN, sinon
-- connexion + nom — voir account_signature), pas son id, que Powens régénère.
CREATE TABLE IF NOT EXISTS account_pin (
    signature TEXT PRIMARY KEY,
    name      TEXT,
    updated   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS series_state (
    series_key    TEXT PRIMARY KEY,       -- merchant key + periodicity
    merchant      TEXT NOT NULL,
    amount        TEXT NOT NULL,
    period_months REAL,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    acknowledged  INTEGER NOT NULL DEFAULT 0,
    -- Alerte persistante : posée à la détection (nouvel abonnement / hausse) et
    -- affichée jusqu'à acquittement — avant, un simple F5 la faisait disparaître.
    alert_kind    TEXT,                   -- 'new' | 'increase' | NULL
    alert_previous TEXT,                  -- montant d'avant la hausse
    alert_pct     REAL                    -- hausse en %
);

-- Requalification manuelle d'un mouvement pour le calcul de performance. Aucune
-- heuristique ne devine qu'un « Versement » est un apport et qu'un « Boost sur
-- versement » est un cadeau de l'assureur : la main de l'utilisateur tranche.
CREATE TABLE IF NOT EXISTS flow_override (
    transaction_id INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL,          -- external | trade | income
    updated        TEXT NOT NULL
);

-- Comptes et opérations importés depuis un relevé, pour ce qu'aucun connecteur ne
-- remonte. Les ids exposés à l'app sont négatifs (voir account_id() plus bas) :
-- l'espace positif appartient à Powens, et les deux jeux d'ids se croisent dans des
-- ensembles d'exclusion (virements internes, transactions d'une série).
--
-- ``powens_account_id`` rattache le compte importé au compte Powens qu'un connecteur
-- s'est mis à remonter depuis : le relevé garde alors le seul rôle qu'il joue encore,
-- l'historique *antérieur* à ce que le connecteur couvre. Voir link_imported_account().
CREATE TABLE IF NOT EXISTS imported_account (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    label             TEXT NOT NULL UNIQUE,
    type              TEXT NOT NULL,
    currency          TEXT NOT NULL,
    created           TEXT NOT NULL,
    powens_account_id INTEGER          -- NULL = compte autonome
);

-- Valorisations quotidiennes des lignes de titres, archivées au fil des passages.
-- L'API ne les garde qu'à partir de la création de la connexion, et rien ne garantit
-- qu'elle les garde indéfiniment : une série de prix perdue ne se rachète pas.
CREATE TABLE IF NOT EXISTS investment_value (
    investment_id INTEGER NOT NULL,
    day           TEXT    NOT NULL,        -- YYYY-MM-DD
    account_id    INTEGER NOT NULL,
    label         TEXT,
    code          TEXT,                    -- ISIN, pour rapprocher d'une source externe
    unit_value    TEXT    NOT NULL,        -- Decimal sérialisé
    PRIMARY KEY (investment_id, day)
);
CREATE INDEX IF NOT EXISTS idx_invvalue_account_day ON investment_value (account_id, day);

CREATE TABLE IF NOT EXISTS investment_classification (
    isin          TEXT PRIMARY KEY,
    sector        TEXT,
    country       TEXT,
    security_type TEXT,
    name          TEXT,
    ticker        TEXT,
    updated       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS imported_transaction (
    fingerprint TEXT PRIMARY KEY,         -- identité stable, deux exports se recouvrent
    account_id  INTEGER NOT NULL,         -- imported_account.id (positif, en base)
    day         TEXT    NOT NULL,
    value       TEXT    NOT NULL,         -- Decimal sérialisé, signé
    wording     TEXT    NOT NULL,
    type        TEXT    NOT NULL,
    source      TEXT    NOT NULL,         -- nom du fichier d'origine
    imported    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_imported_day ON imported_transaction (day);

-- Ancrage SERVEUR de l'authentification : génération de sessions valides, et
-- dernier pas de temps TOTP consommé. Ces deux valeurs vivent en base et non en
-- mémoire parce qu'un redémarrage ne doit RIEN rouvrir : une session révoquée
-- resterait révoquée, un code à six chiffres déjà utilisé ne resservirait pas.
CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


_log = logging.getLogger(__name__)


class SeriesLike(Protocol):
    key: str
    merchant: str
    amount: Decimal
    period_months: float
    periodicity: str


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (and migrate) the local database."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the connection is shared by the app, and Starlette may
    # serve requests from its portal thread. Safe here — one local user, and every
    # write below is a single short statement followed by a commit.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Le collecteur launchd et l'app web écrivent le même fichier, potentiellement au
    # même moment. En mode rollback avec busy_timeout=0 (les défauts), la collision
    # lève "database is locked" immédiatement — côté collecteur, le solde du jour est
    # alors perdu pour toujours (Powens ne répond qu'au présent).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    try:
        db_path.chmod(0o600)  # balances are sensitive
    except OSError:
        pass
    return conn


# Migrations ordonnées, suivies par ``PRAGMA user_version``. Chaque entrée est
# (numéro, instruction) ; en ajouter une NOUVELLE à la fin suffit — plus de bloc
# ``if`` par colonne copié-collé, et l'état d'une base se lit d'une requête.
_MIGRATIONS: list[tuple[int, str]] = [
    (1, "ALTER TABLE imported_account ADD COLUMN powens_account_id INTEGER"),
    (2, "ALTER TABLE series_state ADD COLUMN alert_kind TEXT"),
    (3, "ALTER TABLE series_state ADD COLUMN alert_previous TEXT"),
    (4, "ALTER TABLE series_state ADD COLUMN alert_pct REAL"),
]


def _column_exists(conn: sqlite3.Connection, statement: str) -> bool:
    """Une migration ``ADD COLUMN`` est-elle déjà appliquée ?

    Nécessaire pour les bases d'avant le versionnage (``user_version = 0``) :
    une table peut y être fraîche (créée à l'instant par le SCHEMA, donc à jour)
    pendant qu'une autre est ancienne — l'état se constate colonne par colonne.
    """
    m = re.match(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", statement)
    if not m:
        return False
    table, column = m.groups()
    return column in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """Rejoue les migrations manquantes (``CREATE TABLE IF NOT EXISTS`` ne fait
    rien sur une table déjà présente, et SQLite n'a pas d'``ADD COLUMN IF NOT
    EXISTS``)."""
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    for version, statement in _MIGRATIONS:
        if version <= current:
            continue
        if not _column_exists(conn, statement):
            conn.execute(statement)
        current = version
    conn.execute(f"PRAGMA user_version = {current}")
    conn.commit()


# ------------------------------------------------------------------- backup


def backup(
    conn: sqlite3.Connection,
    db_path: Path,
    *,
    day: date | None = None,
    keep: int = 30,
) -> Path | None:
    """Copie de sûreté quotidienne de la base, avec rotation.

    Cette base est la seule copie au monde des soldes archivés : Powens ne répond
    qu'au présent, donc une base perdue ne se reconstruit pas. La copie passe par
    l'API ``sqlite3`` de backup en ligne (cohérente même pendant des écritures,
    compatible WAL) — jamais par une copie de fichier.

    Une copie par jour au plus : si celle du jour existe déjà, ne rien faire.
    Retourne le chemin écrit, ou ``None`` si la copie du jour existait.
    """
    day = day or date.today()
    backup_dir = db_path.parent / ".backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{db_path.stem.lstrip('.') or 'finance'}-{day.isoformat()}.db"
    if target.exists():
        return None

    dest = sqlite3.connect(target)
    try:
        with dest:
            conn.backup(dest)
    finally:
        dest.close()
    try:
        target.chmod(0o600)  # même sensibilité que la base d'origine
    except OSError:
        pass

    # Rotation : ne garder que les `keep` copies les plus récentes.
    copies = sorted(backup_dir.glob(f"{db_path.stem.lstrip('.') or 'finance'}-*.db"))
    for old in copies[:-keep]:
        old.unlink(missing_ok=True)
    return target


# ------------------------------------------------------------------ réglages


def settings_overrides(conn: sqlite3.Connection) -> dict[str, str]:
    """Réglages posés depuis l'interface, qui l'emportent sur l'environnement."""
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM setting")}


def set_setting(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    """Pose un réglage, ou le retire (valeur vide) pour revenir au défaut .env."""
    if value is None or not str(value).strip():
        conn.execute("DELETE FROM setting WHERE key = ?", (key,))
    else:
        conn.execute(
            "INSERT OR REPLACE INTO setting (key, value, updated) VALUES (?, ?, ?)",
            (key, str(value).strip(), date.today().isoformat()),
        )
    conn.commit()


# ------------------------------------------------------------ authentification


SESSION_EPOCH_KEY = "session_epoch"
TOTP_COUNTER_KEY = "mfa_last_counter"


def session_epoch(conn: sqlite3.Connection) -> int:
    """Génération courante des sessions.

    Chaque cookie porte la génération sous laquelle il a été émis et n'est
    accepté que tant qu'elle correspond. Incrémenter ce compteur (cf.
    :func:`revoke_sessions`) rend d'un coup inutilisable TOUT cookie émis avant
    — c'est ce qui fait d'une déconnexion une vraie révocation, et permet de
    couper un cookie volé sans changer le mot de passe.
    """
    row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (SESSION_EPOCH_KEY,)).fetchone()
    return int(row["value"]) if row else 0


def revoke_sessions(conn: sqlite3.Connection) -> int:
    """Ferme toutes les sessions ouvertes ; retourne la nouvelle génération."""
    conn.execute(
        "INSERT INTO app_meta (key, value) VALUES (?, '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1",
        (SESSION_EPOCH_KEY,),
    )
    conn.commit()
    return session_epoch(conn)


def claim_totp_counter(conn: sqlite3.Connection, counter: int) -> bool:
    """Consomme un pas de temps TOTP ; ``False`` s'il avait déjà servi.

    Un code à six chiffres reste valide une demi-minute : intercepté (épaule,
    hameçonnage, journal de proxy), il ouvrirait une seconde session dans cette
    fenêtre. Le pas consommé est donc mémorisé, et un pas antérieur ou égal est
    refusé.

    L'écriture est CONDITIONNELLE, en une seule instruction : deux requêtes
    portant le même code et arrivant ensemble ne peuvent pas passer toutes les
    deux le contrôle, ce qu'un « lire puis écrire » laisserait faire.
    """
    cursor = conn.execute(
        "INSERT INTO app_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value "
        "  WHERE CAST(app_meta.value AS INTEGER) < CAST(excluded.value AS INTEGER)",
        (TOTP_COUNTER_KEY, str(counter)),
    )
    conn.commit()
    return cursor.rowcount > 0


# --------------------------------------------------- identité stable des comptes


def account_signature(account: Any) -> str | None:
    """Identité d'un compte qui SURVIT au renumérotage de Powens.

    Deux stratégies, dans cet ordre :

    * l'**IBAN** quand il existe — c'est l'identité bancaire, unique et stable,
      et le seul moyen de distinguer deux comptes du même nom (deux comptes
      courants « M BARTOLI JEREMIE » chez la même banque) ;
    * sinon **(connexion, nom)**. Ni le ``number`` ni le ``type`` ne peuvent
      servir : sur un prêt observé, le ``number`` était un hash régénéré à
      chaque recréation (``5bb6c9e6…`` puis ``8519e6d0…``) et le ``type``
      oscillait entre ``loan`` et ``mortgage``. Le nom, lui, n'a pas bougé.

    ``None`` quand aucune identité fiable ne se dégage (compte importé, id
    négatif, nom vide) : ces comptes gardent leur id comme identité.
    """
    account_id = getattr(account, "id", None)
    if account_id is None or account_id < 0:
        return None  # comptes importés : jamais renumérotés par Powens
    raw = getattr(account, "raw", None) or {}
    iban = str(raw.get("iban") or "").strip()
    if iban:
        return f"iban:{iban.upper()}"
    name = " ".join(str(getattr(account, "name", "") or "").split()).upper()
    if not name:
        return None
    return f"conn:{raw.get('id_connection')}|{name}"


def remap_account(conn: sqlite3.Connection, old_id: int, new_id: int) -> int:
    """Réattribue TOUT l'historique local d'un compte renuméroté.

    Quatre tables référencent un id de compte ; en oublier une laisserait des
    données orphelines (des VL rattachées à un compte qui n'existe plus, un
    relevé importé décroché de sa cible). Retourne le nombre de snapshots
    déplacés.
    """
    if old_id == new_id:
        return 0
    # Jours couverts par les DEUX ids : la version récente prime (Powens vient
    # de la publier), l'ancienne est supprimée pour éviter la collision de clé.
    conn.execute(
        "DELETE FROM balance_snapshot WHERE account_id = ?"
        " AND day IN (SELECT day FROM balance_snapshot WHERE account_id = ?)",
        (old_id, new_id),
    )
    moved = (
        conn.execute(
            "UPDATE balance_snapshot SET account_id = ? WHERE account_id = ?", (new_id, old_id)
        ).rowcount
        or 0
    )
    conn.execute(
        "UPDATE investment_value SET account_id = ? WHERE account_id = ?", (new_id, old_id)
    )
    conn.execute(
        "UPDATE imported_account SET powens_account_id = ? WHERE powens_account_id = ?",
        (new_id, old_id),
    )
    # Le renommage local suit le compte, sauf si le nouvel id en a déjà un.
    conn.execute(
        "DELETE FROM account_alias WHERE account_id = ?"
        " AND EXISTS (SELECT 1 FROM account_alias WHERE account_id = ?)",
        (old_id, new_id),
    )
    conn.execute("UPDATE account_alias SET account_id = ? WHERE account_id = ?", (new_id, old_id))
    conn.commit()
    _forget_series_cache()
    _forget_values_cache()
    return moved


def sync_account_identities(
    conn: sqlite3.Connection, accounts: Iterable[Any]
) -> list[tuple[int, int]]:
    """Détecte les renumérotages et recolle l'historique. Retourne ``[(ancien, nouveau)]``.

    Appelée à chaque archivage : le prochain passage du collecteur répare donc
    tout seul ce qu'une panne de connexion a cassé.

    Garde-fou : si deux comptes COURANTS partagent la même signature, elle
    n'identifie plus rien — ils sont laissés à leur id, sans quoi la fusion
    mélangerait deux comptes distincts.
    """
    signatures: dict[str, list[Any]] = {}
    for account in accounts:
        signature = account_signature(account)
        if signature is not None:
            signatures.setdefault(signature, []).append(account)

    known = {
        row["signature"]: int(row["account_id"])
        for row in conn.execute("SELECT signature, account_id FROM account_identity")
    }
    today = date.today().isoformat()
    remapped: list[tuple[int, int]] = []

    for signature, matched in signatures.items():
        if len(matched) > 1:
            _log.warning(
                "signature ambiguë (%s) partagée par %s — identification par id",
                signature,
                [a.id for a in matched],
            )
            continue
        account = matched[0]
        current = account.id
        previous = known.get(signature)
        if previous is not None and previous != current:
            moved = remap_account(conn, previous, current)
            remapped.append((previous, current))
            _log.warning(
                "compte renuméroté par Powens (%s) : id %s → %s, %d jour(s) recollé(s)",
                signature,
                previous,
                current,
                moved,
            )
        conn.execute(
            "INSERT OR REPLACE INTO account_identity (signature, account_id, name, updated)"
            " VALUES (?, ?, ?, ?)",
            (signature, current, getattr(account, "name", None), today),
        )
    conn.commit()
    return remapped


# ------------------------------------------------------- comptes épinglés


def pinned_accounts(conn: sqlite3.Connection) -> dict[str, str]:
    """``{signature: nom}`` des comptes à réintégrer d'office s'ils sont désactivés."""
    return {
        row["signature"]: (row["name"] or "")
        for row in conn.execute("SELECT signature, name FROM account_pin")
    }


def pin_account(conn: sqlite3.Connection, signature: str, name: str | None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO account_pin (signature, name, updated) VALUES (?, ?, ?)",
        (signature, name or "", date.today().isoformat()),
    )
    conn.commit()


def unpin_account(conn: sqlite3.Connection, signature: str) -> None:
    conn.execute("DELETE FROM account_pin WHERE signature = ?", (signature,))
    conn.commit()


# ------------------------------------------------------------ balance history


class AccountLike(Protocol):
    id: int | None
    name: str | None
    type: str | None
    currency: str | None
    balance: Decimal | None


def record_snapshot(
    conn: sqlite3.Connection,
    accounts: Iterable[AccountLike],
    *,
    day: date | None = None,
    default_currency: str = "EUR",
) -> int:
    """Store today's balance for each account (idempotent: one row per day+account)."""
    # AVANT d'écrire : si Powens a renuméroté un compte depuis le dernier
    # passage, recoller son historique — sinon la ligne du jour partirait
    # sous un nouvel id et la courbe se scinderait en deux (cf. le prêt du
    # 01/08, quatre ids successifs pour un même emprunt).
    sync_account_identities(conn, accounts)
    day = day or date.today()
    rows = [
        (
            day.isoformat(),
            acc.id,
            acc.name,
            acc.type,
            (acc.currency or default_currency).upper(),
            str(acc.balance if acc.balance is not None else Decimal(0)),
        )
        for acc in accounts
        if acc.id is not None
    ]
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO balance_snapshot"
        " (day, account_id, name, type, currency, balance) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def period_to_since(period: str) -> date | None:
    """Convert a period code to a ``since`` date, or ``None`` for 'tout'."""
    today = date.today()
    code = (period or "tout").lower().strip()
    if code == "tout":
        return None
    if code == "1j":
        return today - timedelta(days=1)
    if code == "7j":
        return today - timedelta(days=7)
    if code == "ytd":
        return today.replace(month=1, day=1)
    if code == "1a":
        return _subtract_months(today, 12)
    if code == "1m":
        return _subtract_months(today, 1)
    if code == "3m":
        return _subtract_months(today, 3)
    if code == "6m":
        return _subtract_months(today, 6)
    return None


def _subtract_months(d: date, months: int) -> date:
    """Subtract *months* from *d*, clamping the day to the target month's last day."""
    month = d.month - months
    year = d.year
    while month < 1:
        month += 12
        year -= 1
    import calendar

    max_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, max_day))


def _downsample(points: list[tuple[date, Decimal]], limit: int) -> list[tuple[date, Decimal]]:
    """Réduit à ``limit`` points répartis uniformément, premier et dernier gardés.

    Tronquer aux ``limit`` derniers jours (l'ancien ``points[-limit:]``) mentait
    deux fois : « TOUT » ne montrait jamais que ~6 mois, et la « variation depuis
    le JJ/MM » se comparait à une origine qui glissait chaque jour au lieu de
    rester le début de la fenêtre demandée.
    """
    if limit <= 1 or len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]


# Mémo de _per_account_series : la relecture de TOUT l'historique de soldes
# (des milliers de lignes converties en Decimal) coûtait ~15 ms par appel, et
# chaque page de synthèse l'appelait deux fois (courbe + notes de périmètre) —
# 85 % du temps de rendu de / et /patrimoine. La clé résume l'état de la table :
# nombre de lignes et plus grand rowid (INSERT OR REPLACE attribue un nouveau
# rowid), donc tout ajout ou remplacement de solde invalide l'entrée ; les
# UPDATE de remap_account l'invalident explicitement.
_SeriesCache = tuple[list[str], dict[int, dict[str, Decimal]], dict[int, str]]
_series_cache: dict[tuple[int, str, str, int, int], _SeriesCache] = {}


def _series_cache_key(conn: sqlite3.Connection, currency: str) -> tuple[int, str, str, int, int]:
    # La connexion et le fichier font partie de la clé : deux bases distinctes
    # (les tests en ouvrent une par cas) peuvent avoir le même nombre de lignes.
    database = str(conn.execute("PRAGMA database_list").fetchone()[2] or "")
    row = conn.execute("SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM balance_snapshot").fetchone()
    return (id(conn), database, currency.upper(), int(row[0]), int(row[1]))


def _forget_series_cache() -> None:
    _series_cache.clear()


def _per_account_series(conn: sqlite3.Connection, currency: str) -> _SeriesCache:
    """(jours archivés triés, {compte: {jour: solde}}, {compte: dernier nom}).

    Mémorisé tant que la table n'a pas changé (voir ``_series_cache``). Les
    structures rendues sont partagées entre appelants : ne pas les modifier.
    """
    key = _series_cache_key(conn, currency)
    hit = _series_cache.get(key)
    if hit is not None:
        return hit
    per_account: dict[int, dict[str, Decimal]] = {}
    names: dict[int, str] = {}
    days_set: set[str] = set()
    for row in conn.execute(
        "SELECT day, account_id, name, balance FROM balance_snapshot"
        " WHERE currency = ? ORDER BY day",
        (currency.upper(),),
    ):
        per_account.setdefault(row["account_id"], {})[row["day"]] = Decimal(row["balance"])
        names[row["account_id"]] = row["name"] or f"Compte #{row['account_id']}"
        days_set.add(row["day"])
    result: _SeriesCache = (sorted(days_set), per_account, names)
    _series_cache.clear()  # une seule devise en pratique : ne jamais accumuler
    _series_cache[key] = result
    return result


def net_worth_history(
    conn: sqlite3.Connection,
    *,
    currency: str = "EUR",
    limit: int = 180,
    since: date | None = None,
) -> list[tuple[date, Decimal]]:
    """Daily net worth (one point per recorded day), oldest first.

    If *since* is given, only points on or after that date are returned. ``limit``
    borne le nombre de points par échantillonnage (jamais par troncature) — les
    montants restent sommés en :class:`Decimal`, pas en ``SUM()`` SQLite (REAL).

    Les trous d'un compte ENTRE deux apparitions sont comblés avec son dernier
    solde connu : une connexion en panne fait « disparaître » un compte quelques
    jours (cas vécu : prêt de -257 k€ désactivé du 01 au 13/08), et la courbe
    sautait de son montant à l'aller comme au retour, sans qu'un euro n'ait
    bougé. Un compte n'est jamais prolongé AVANT sa première apparition ni
    APRÈS sa dernière — connecter une nouvelle banque ou clore un compte reste
    un vrai changement de périmètre (voir :func:`perimeter_changes`).
    """
    days, per_account, _ = _per_account_series(conn, currency)
    if not days:
        return []
    totals: dict[str, Decimal] = dict.fromkeys(days, Decimal(0))
    for series in per_account.values():
        account_days = sorted(series)
        first, last = account_days[0], account_days[-1]
        current = Decimal(0)
        for day in days:
            if day < first or day > last:
                continue
            current = series.get(day, current)
            totals[day] += current
    points = [(date.fromisoformat(day), totals[day]) for day in days]
    if since is not None:
        points = [(d, v) for d, v in points if d >= since]
    return _downsample(points, limit)


def acknowledged_perimeter_days(conn: sqlite3.Connection) -> dict[str, str]:
    """Jours dont le changement de périmètre a été acquitté."""
    return {
        row["day"]: (row["label"] or "")
        for row in conn.execute("SELECT day, label FROM perimeter_ack")
    }


def acknowledge_perimeter(conn: sqlite3.Connection, day: date, label: str = "") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO perimeter_ack (day, label, updated) VALUES (?, ?, ?)",
        (day.isoformat(), label, date.today().isoformat()),
    )
    conn.commit()


def forget_perimeter_ack(conn: sqlite3.Connection, day: str) -> None:
    """Réaffiche une note acquittée (depuis les Réglages)."""
    conn.execute("DELETE FROM perimeter_ack WHERE day = ?", (day,))
    conn.commit()


def perimeter_changes(conn: sqlite3.Connection, *, currency: str = "EUR") -> list[dict[str, Any]]:
    """Jours où le PÉRIMÈTRE des comptes archivés a durablement changé.

    Le comblement des trous (:func:`net_worth_history`) neutralise les absences
    temporaires ; restent les vrais événements, qui déplacent la courbe d'un
    montant qui n'a été ni gagné ni dépensé :

    * un compte ENTRE (nouvelle banque connectée) — jour de sa 1re apparition ;
    * un compte SORT définitivement (clos, déconnecté) — premier jour archivé
      APRÈS sa dernière apparition.

    Le premier jour d'archivage global est ignoré (tout « entre » ce jour-là).
    """
    days, per_account, names = _per_account_series(conn, currency)
    if len(days) < 2:
        return []
    acknowledged = acknowledged_perimeter_days(conn)
    global_first, global_last = days[0], days[-1]
    changes: dict[str, dict[str, Any]] = {}

    def _entry(day: str) -> dict[str, Any]:
        return changes.setdefault(
            day, {"day": date.fromisoformat(day), "entered": [], "left": [], "delta": Decimal(0)}
        )

    for account_id, series in per_account.items():
        account_days = sorted(series)
        first, last = account_days[0], account_days[-1]
        if first > global_first:
            entry = _entry(first)
            entry["entered"].append(names[account_id])
            entry["delta"] += series[first]
        if last < global_last:
            # L'effet se voit le premier jour archivé qui SUIT la dernière trace.
            following = next(d for d in days if d > last)
            entry = _entry(following)
            entry["left"].append(names[account_id])
            entry["delta"] -= series[last]
    return [changes[d] for d in sorted(changes) if d not in acknowledged]


def account_balance_history(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    currency: str = "EUR",
    limit: int = 180,
    since: date | None = None,
) -> list[tuple[date, Decimal]]:
    """Daily balance for a single account, oldest first."""
    sql = "SELECT day, balance FROM balance_snapshot WHERE account_id = ? AND currency = ?"
    params: list[object] = [account_id, currency.upper()]
    if since is not None:
        sql += " AND day >= ?"
        params.append(since.isoformat())
    cursor = conn.execute(sql + " ORDER BY day", params)
    points = [(date.fromisoformat(row["day"]), Decimal(row["balance"])) for row in cursor]
    return _downsample(points, limit)


def ensure_snapshot(
    conn: sqlite3.Connection,
    accounts: Iterable[AccountLike],
    *,
    day: date | None = None,
    default_currency: str = "EUR",
) -> int:
    """Écrit le relevé du jour SEULEMENT s'il n'existe pas encore.

    C'est la variante pour les pages web : le collecteur reste la mesure de
    référence (son dernier passage, 22 h 30, écrase les précédents), et un simple
    F5 ne doit pas remplacer sa mesure par un instantané pris à n'importe quelle
    heure. Sans collecteur installé, le premier affichage du jour fournit tout de
    même un point à la courbe.
    """
    day = day or date.today()
    row = conn.execute(
        "SELECT 1 FROM balance_snapshot WHERE day = ? LIMIT 1", (day.isoformat(),)
    ).fetchone()
    if row:
        return 0
    return record_snapshot(conn, accounts, day=day, default_currency=default_currency)


def last_snapshot_days(conn: sqlite3.Connection) -> dict[int, date]:
    """Dernier jour archivé par compte — dit si NOTRE courbe est à jour.

    Distinct de la date de synchro Powens : une banque peut répondre pendant
    que le collecteur, lui, ne tourne plus.
    """
    return {
        int(row["account_id"]): date.fromisoformat(row["last"])
        for row in conn.execute(
            "SELECT account_id, MAX(day) AS last FROM balance_snapshot GROUP BY account_id"
        )
    }


def previous_net_worth(
    conn: sqlite3.Connection, *, currency: str = "EUR", before: date | None = None
) -> tuple[date, Decimal] | None:
    """Most recent net worth recorded strictly before ``before`` (default: today).

    Interroge directement le dernier jour archivé : passer par
    :func:`net_worth_history` exposerait la veille à l'échantillonnage.
    """
    before = before or date.today()
    row = conn.execute(
        "SELECT day FROM balance_snapshot WHERE currency = ? AND day < ? ORDER BY day DESC LIMIT 1",
        (currency.upper(), before.isoformat()),
    ).fetchone()
    if row is None:
        return None
    total = Decimal(0)
    for r in conn.execute(
        "SELECT balance FROM balance_snapshot WHERE currency = ? AND day = ?",
        (currency.upper(), row["day"]),
    ):
        total += Decimal(r["balance"])
    return (date.fromisoformat(row["day"]), total)


# -------------------------------------------------------- category overrides


def all_overrides(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        row["merchant_key"]: row["category"]
        for row in conn.execute("SELECT merchant_key, category FROM category_override")
    }


def set_override(conn: sqlite3.Connection, merchant_key: str, category: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO category_override (merchant_key, category, updated)"
        " VALUES (?, ?, ?)",
        (merchant_key.upper().strip(), category.strip(), date.today().isoformat()),
    )
    conn.commit()


def clear_override(conn: sqlite3.Connection, merchant_key: str) -> None:
    conn.execute(
        "DELETE FROM category_override WHERE merchant_key = ?", (merchant_key.upper().strip(),)
    )
    conn.commit()


def pending_subscription_alerts(conn: sqlite3.Connection) -> int:
    """Alertes « nouveau / hausse » pas encore acquittées (pour la notification)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM series_state WHERE alert_kind IS NOT NULL AND acknowledged = 0"
    ).fetchone()
    return int(row["n"])


# ------------------------------------------------------------------ benchmark


def save_benchmark_values(
    conn: sqlite3.Connection, ticker: str, values: Iterable[tuple[date, Decimal]]
) -> int:
    """Archive des clôtures quotidiennes d'un indice (idempotent par jour)."""
    rows = [(ticker, day.isoformat(), str(close)) for day, close in values]
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO benchmark_value (ticker, day, close) VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def benchmark_history(
    conn: sqlite3.Connection, ticker: str, *, since: date | None = None
) -> list[tuple[date, Decimal]]:
    sql = "SELECT day, close FROM benchmark_value WHERE ticker = ?"
    params: list[object] = [ticker]
    if since is not None:
        sql += " AND day >= ?"
        params.append(since.isoformat())
    return [
        (date.fromisoformat(row["day"]), Decimal(row["close"]))
        for row in conn.execute(sql + " ORDER BY day", params)
    ]


def benchmark_last_day(conn: sqlite3.Connection, ticker: str) -> date | None:
    row = conn.execute(
        "SELECT MAX(day) AS last FROM benchmark_value WHERE ticker = ?", (ticker,)
    ).fetchone()
    return date.fromisoformat(row["last"]) if row and row["last"] else None


# ---------------------------------------------------------- alias & fusions


def account_aliases(conn: sqlite3.Connection) -> dict[int, str]:
    return {
        int(row["account_id"]): row["name"]
        for row in conn.execute("SELECT account_id, name FROM account_alias")
    }


def set_account_alias(conn: sqlite3.Connection, account_id: int, name: str) -> None:
    """Renomme localement un compte (nom vide = retour au nom Powens)."""
    name = name.strip()
    if not name:
        conn.execute("DELETE FROM account_alias WHERE account_id = ?", (account_id,))
    else:
        conn.execute(
            "INSERT OR REPLACE INTO account_alias (account_id, name) VALUES (?, ?)",
            (account_id, name),
        )
    conn.commit()


def merchant_aliases(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        row["from_key"]: row["to_key"]
        for row in conn.execute("SELECT from_key, to_key FROM merchant_alias")
    }


def set_merchant_alias(conn: sqlite3.Connection, from_key: str, to_key: str) -> None:
    """Fusionne ``from_key`` dans ``to_key`` (cible vide = défusionner).

    La cible est résolue à travers les fusions existantes (pas de chaîne à
    suivre à la lecture) et une fusion sur soi-même est un défusionnage.
    """
    from_key = from_key.upper().strip()
    to_key = to_key.upper().strip()
    existing = merchant_aliases(conn)
    to_key = existing.get(to_key, to_key)
    if not to_key or to_key == from_key:
        conn.execute("DELETE FROM merchant_alias WHERE from_key = ?", (from_key,))
    else:
        conn.execute(
            "INSERT OR REPLACE INTO merchant_alias (from_key, to_key) VALUES (?, ?)",
            (from_key, to_key),
        )
        # Rediriger les fusions qui pointaient sur from_key : jamais de chaîne.
        conn.execute("UPDATE merchant_alias SET to_key = ? WHERE to_key = ?", (to_key, from_key))
    conn.commit()


# ------------------------------------------------------------------- budgets


def budgets(conn: sqlite3.Connection) -> dict[str, Decimal]:
    """``{catégorie: enveloppe mensuelle}`` — vide si aucun budget défini."""
    return {
        row["category"]: Decimal(row["monthly"])
        for row in conn.execute("SELECT category, monthly FROM budget")
    }


def set_budget(conn: sqlite3.Connection, category: str, monthly: Decimal | None) -> None:
    """Pose (ou retire, si ``None``/≤0) l'enveloppe mensuelle d'une catégorie."""
    category = category.strip()
    if monthly is None or monthly <= 0:
        conn.execute("DELETE FROM budget WHERE category = ?", (category,))
    else:
        conn.execute(
            "INSERT OR REPLACE INTO budget (category, monthly, updated) VALUES (?, ?, ?)",
            (category, str(monthly), date.today().isoformat()),
        )
    conn.commit()


# ------------------------------------------------------------- flow overrides

FLOW_KINDS = ("external", "trade", "income")


def flow_overrides(conn: sqlite3.Connection) -> dict[int, str]:
    """``{id de transaction: nature}`` pour les mouvements requalifiés à la main."""
    return {
        int(row["transaction_id"]): row["kind"]
        for row in conn.execute("SELECT transaction_id, kind FROM flow_override")
    }


def set_flow_override(conn: sqlite3.Connection, transaction_id: int, kind: str) -> None:
    """Requalifie un mouvement. ``kind`` vide (ou inconnu) rend la main à l'heuristique."""
    if kind not in FLOW_KINDS:
        conn.execute("DELETE FROM flow_override WHERE transaction_id = ?", (transaction_id,))
    else:
        conn.execute(
            "INSERT OR REPLACE INTO flow_override (transaction_id, kind, updated) VALUES (?, ?, ?)",
            (transaction_id, kind, date.today().isoformat()),
        )
    conn.commit()


# ------------------------------------------------------ investment value history


class InvestmentValueLike(Protocol):
    id_investment: int | None
    vdate: date | None
    unit_value: Decimal | None


def save_investment_values(
    conn: sqlite3.Connection,
    values: Iterable[InvestmentValueLike],
    *,
    account_id: int,
    label: str | None = None,
    code: str | None = None,
) -> int:
    """Archive les valorisations d'une ligne. Idempotent (une ligne par titre et par jour).

    ``INSERT OR REPLACE`` plutôt que ``IGNORE`` : une VL du jour peut être provisoire au
    moment où on la lit et corrigée ensuite, et c'est la dernière lue qui fait foi.
    """
    rows = [
        (
            value.id_investment,
            value.vdate.isoformat(),
            account_id,
            label,
            code,
            str(value.unit_value),
        )
        for value in values
        if value.id_investment is not None
        and value.vdate is not None
        and value.unit_value is not None
    ]
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO investment_value"
        " (investment_id, day, account_id, label, code, unit_value) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


# Mémo des valorisations : /performance convertit des dizaines de milliers de
# lignes (date + Decimal) à chaque affichage alors que la table ne bouge qu'au
# passage du collecteur. Même principe que _series_cache : la clé résume l'état
# de la table (INSERT OR REPLACE renouvelle le rowid), remap_account invalide.
_values_cache: dict[tuple[Any, ...], list[dict[str, Any]]] = {}


def _forget_values_cache() -> None:
    _values_cache.clear()


def investment_values(
    conn: sqlite3.Connection,
    *,
    account_id: int | None = None,
    account_ids: Iterable[int] | None = None,
    since: date | None = None,
) -> list[dict[str, Any]]:
    """Valorisations archivées, les plus anciennes d'abord.

    ``account_ids`` restreint à plusieurs comptes en une seule lecture (la page
    performance) ; ``account_id`` en garde un seul. Le résultat est mémorisé
    tant que la table ne change pas : ne pas le modifier.
    """
    wanted = tuple(
        sorted(
            {int(a) for a in (account_ids or ())}
            | ({account_id} if account_id is not None else set())
        )
    )
    state = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM investment_value"
    ).fetchone()
    key = (id(conn), wanted, since, int(state[0]), int(state[1]))
    hit = _values_cache.get(key)
    if hit is not None:
        return hit

    sql = (
        "SELECT investment_id, day, account_id, label, code, unit_value"
        " FROM investment_value WHERE 1 = 1"
    )
    params: list[Any] = []
    if wanted:
        sql += f" AND account_id IN ({','.join('?' * len(wanted))})"
        params.extend(wanted)
    if since is not None:
        sql += " AND day >= ?"
        params.append(since.isoformat())
    sql += " ORDER BY day, investment_id"
    result = [
        {
            "investment_id": int(row["investment_id"]),
            "day": date.fromisoformat(row["day"]),
            "account_id": int(row["account_id"]),
            "label": row["label"],
            "code": row["code"],
            "unit_value": Decimal(row["unit_value"]),
        }
        for row in conn.execute(sql, params)
    ]
    if len(_values_cache) >= 8:
        _values_cache.clear()
    _values_cache[key] = result
    return result


def investment_value_span(conn: sqlite3.Connection) -> tuple[date, date] | None:
    """``(premier jour, dernier jour)`` archivés, ou ``None`` si la table est vide."""
    row = conn.execute("SELECT MIN(day) AS lo, MAX(day) AS hi FROM investment_value").fetchone()
    if not row or not row["lo"]:
        return None
    return date.fromisoformat(row["lo"]), date.fromisoformat(row["hi"])


# ---------------------------------------------------------- imported statements


# Les ids Powens sont des entiers positifs. Un compte importé est exposé à l'app avec
# l'opposé de son id en base, et ses opérations avec des ids négatifs distincts, pour
# qu'aucune collision ne soit possible entre les deux sources.
def account_id(db_id: int) -> int:
    return -db_id


def upsert_imported_account(
    conn: sqlite3.Connection,
    label: str,
    *,
    type: str = "checking",
    currency: str = "EUR",
) -> int:
    """Crée (ou retrouve) le compte importé nommé ``label``, et renvoie son id en base."""
    label = label.strip() or "Compte importé"
    conn.execute(
        "INSERT OR IGNORE INTO imported_account (label, type, currency, created)"
        " VALUES (?, ?, ?, ?)",
        (label, type, currency.upper(), date.today().isoformat()),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM imported_account WHERE label = ?", (label,)).fetchone()
    return int(row["id"])


def link_imported_account(
    conn: sqlite3.Connection, db_id: int, powens_account_id: int | None
) -> None:
    """Rattache (ou détache, avec ``None``) un compte importé à un compte Powens.

    Rattacher veut dire : ce relevé et ce compte sont la même chose. Le compte importé
    cesse d'exister comme compte à part — son solde ne s'ajoute plus à rien — et ses
    opérations sont reversées au compte Powens, mais seulement celles que le connecteur
    ne couvre pas (voir :func:`imported_transactions`).

    Les relevés de solde déjà pris pour le compte importé sont effacés : ils ont été
    enregistrés à une époque où il comptait pour lui-même, et les laisser laisserait une
    bosse du montant du doublon dans la courbe de patrimoine.
    """
    conn.execute(
        "UPDATE imported_account SET powens_account_id = ? WHERE id = ?",
        (powens_account_id, db_id),
    )
    if powens_account_id is not None:
        conn.execute("DELETE FROM balance_snapshot WHERE account_id = ?", (account_id(db_id),))
    conn.commit()


def imported_links(conn: sqlite3.Connection) -> dict[int, int]:
    """``{id en base: id du compte Powens}`` pour les seuls comptes rattachés."""
    return {
        int(row["id"]): int(row["powens_account_id"])
        for row in conn.execute(
            "SELECT id, powens_account_id FROM imported_account WHERE powens_account_id IS NOT NULL"
        )
    }


def imported_accounts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Comptes importés autonomes, sous la forme d'un payload d'API Powens.

    Les comptes rattachés sont volontairement absents : leur solde est celui du compte
    Powens correspondant, et les exposer tous les deux compterait l'argent deux fois.
    """
    return [
        {
            "id": account_id(row["id"]),
            "id_connection": None,
            "name": row["label"],
            "type": row["type"],
            "balance": str(_imported_balance(conn, row["id"])),
            "currency": {"id": row["currency"]},
            "iban": None,
        }
        for row in conn.execute(
            "SELECT id, label, type, currency FROM imported_account"
            " WHERE powens_account_id IS NULL ORDER BY label"
        )
    ]


def _imported_balance(conn: sqlite3.Connection, db_id: int) -> Decimal:
    """Somme des opérations importées du compte.

    Un relevé ne porte pas le solde courant, seulement des mouvements : le total est
    donc une somme de flux, pas une photographie. Il est affiché comme tel.
    """
    total = Decimal(0)
    for row in conn.execute(
        "SELECT value FROM imported_transaction WHERE account_id = ?", (db_id,)
    ):
        total += Decimal(row["value"])
    return total


def save_imported(
    conn: sqlite3.Connection,
    db_id: int,
    transactions: Sequence[Any],
    fingerprints: Sequence[str],
    *,
    source: str,
) -> tuple[int, int]:
    """Enregistre les opérations parsées. Renvoie ``(ajoutées, doublons ignorés)``.

    L'empreinte est la clé primaire : réimporter un relevé qui recouvre le précédent
    est sans effet, ce qui est le cas d'usage normal (on exporte par périodes qui se
    chevauchent).
    """
    today = date.today().isoformat()
    added = 0
    for txn, digest in zip(transactions, fingerprints, strict=True):
        cursor = conn.execute(
            "INSERT OR IGNORE INTO imported_transaction"
            " (fingerprint, account_id, day, value, wording, type, source, imported)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                digest,
                db_id,
                txn.date.isoformat(),
                str(txn.value),
                txn.wording,
                txn.type,
                source,
                today,
            ),
        )
        added += cursor.rowcount or 0
    conn.commit()
    return added, len(fingerprints) - added


def imported_transactions(
    conn: sqlite3.Connection,
    *,
    since: date | None = None,
    ceilings: Mapping[int, date] | None = None,
) -> list[dict[str, Any]]:
    """Opérations importées, sous forme de payloads d'API Powens.

    Les ids sont négatifs et dérivés du ``rowid``, donc stables d'un appel à l'autre —
    des séries détectées y font référence.

    ``ceilings`` donne, par compte importé rattaché (id en base), la première date que le
    connecteur couvre : les opérations de ce jour et des suivants sont écartées, puisque
    Powens les remonte désormais lui-même. Sans cette borne, la période de recouvrement
    entre le relevé et le connecteur serait comptée deux fois dans toutes les analyses.

    Une opération d'un compte rattaché est présentée sous l'id du compte Powens : c'est
    bien le même compte, et les pages qui ne regardent que les comptes courants doivent
    continuer à voir cet historique.
    """
    sql = (
        "SELECT t.rowid AS rowid, t.account_id AS account_id, t.day AS day,"
        " t.value AS value, t.wording AS wording, t.type AS type,"
        " a.powens_account_id AS powens_account_id"
        " FROM imported_transaction t JOIN imported_account a ON a.id = t.account_id"
    )
    params: tuple[Any, ...] = ()
    if since is not None:
        sql += " WHERE t.day >= ?"
        params = (since.isoformat(),)
    sql += " ORDER BY t.day"

    out: list[dict[str, Any]] = []
    for row in conn.execute(sql, params):
        ceiling = (ceilings or {}).get(row["account_id"])
        if ceiling is not None and row["day"] >= ceiling.isoformat():
            continue
        linked = row["powens_account_id"]
        out.append(
            {
                "id": -int(row["rowid"]),
                "id_account": int(linked) if linked is not None else account_id(row["account_id"]),
                "date": row["day"],
                "value": row["value"],
                "type": row["type"],
                "wording": row["wording"],
                "simplified_wording": row["wording"],
                "original_wording": row["wording"],
                "coming": False,
            }
        )
    return out


def imported_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Un récapitulatif par compte importé, pour la page d'import."""
    return [
        dict(row)
        for row in conn.execute(
            "SELECT a.id, a.label, a.type, a.currency, a.powens_account_id,"
            " COUNT(t.fingerprint) AS operations,"
            " MIN(t.day) AS first_day, MAX(t.day) AS last_day,"
            " GROUP_CONCAT(DISTINCT t.source) AS sources"
            " FROM imported_account a"
            " LEFT JOIN imported_transaction t ON t.account_id = a.id"
            " GROUP BY a.id ORDER BY a.label"
        )
    ]


def delete_imported_account(conn: sqlite3.Connection, db_id: int) -> int:
    """Supprime un compte importé et toutes ses opérations."""
    cursor = conn.execute("DELETE FROM imported_transaction WHERE account_id = ?", (db_id,))
    conn.execute("DELETE FROM imported_account WHERE id = ?", (db_id,))
    conn.commit()
    return cursor.rowcount or 0


# ------------------------------------------------------------- series tracking


def series_key(item: SeriesLike) -> str:
    """Stable identity of a recurring series: merchant + periodicity."""
    return f"{item.key}|{item.periodicity}"


def sync_series(
    conn: sqlite3.Connection,
    items: Sequence[SeriesLike],
    *,
    today: date | None = None,
    increase_threshold: float = 0.02,
) -> dict[str, dict[str, Any]]:
    """Compare detected series with what was seen before, then persist the new state.

    Returns, per :func:`series_key`: ``{"new": bool, "previous_amount": Decimal|None,
    "increase_pct": float|None, "first_seen": date}``. A series is *new* only if it
    was never recorded — so the very first run does not flag everything at once.
    """
    today = today or date.today()
    known = {
        row["series_key"]: row
        for row in conn.execute(
            "SELECT series_key, amount, first_seen, acknowledged,"
            " alert_kind, alert_previous, alert_pct FROM series_state"
        )
    }
    first_run = not known

    result: dict[str, dict[str, Any]] = {}
    rows = []
    for item in items:
        key = series_key(item)
        row = known.get(key)
        previous = Decimal(row["amount"]) if row else None
        increase_pct: float | None = None
        if previous is not None and previous > 0 and item.amount > previous:
            delta = float((item.amount - previous) / previous)
            if delta >= increase_threshold:
                increase_pct = delta * 100

        # L'alerte est un ÉTAT, pas un événement : posée à la détection, elle
        # reste affichée jusqu'à l'acquittement explicite. L'ancien diff one-shot
        # la faisait disparaître au premier rechargement de page.
        alert: tuple[str | None, str | None, float | None, int]
        if row is None and not first_run:
            alert = ("new", None, None, 0)
        elif increase_pct is not None:
            alert = ("increase", str(previous), increase_pct, 0)
        elif row is not None and row["alert_kind"] and not row["acknowledged"]:
            alert = (row["alert_kind"], row["alert_previous"], row["alert_pct"], 0)
        else:
            alert = (None, None, None, 1 if row is not None and row["acknowledged"] else 0)
        kind, alert_previous, alert_pct, acknowledged = alert

        result[key] = {
            "new": kind == "new",
            "previous_amount": (
                Decimal(alert_previous) if alert_previous is not None else previous
            ),
            "increase_pct": alert_pct if kind == "increase" else None,
            "first_seen": date.fromisoformat(row["first_seen"]) if row else today,
        }
        rows.append(
            (
                key,
                item.merchant,
                str(item.amount),
                float(item.period_months),
                row["first_seen"] if row else today.isoformat(),
                today.isoformat(),
                acknowledged,
                kind,
                alert_previous,
                alert_pct,
            )
        )

    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO series_state"
            " (series_key, merchant, amount, period_months, first_seen, last_seen,"
            "  acknowledged, alert_kind, alert_previous, alert_pct)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    return result


def acknowledge_alerts(conn: sqlite3.Connection) -> int:
    """Acquitte toutes les alertes en attente ; retourne le nombre acquitté."""
    cursor = conn.execute(
        "UPDATE series_state SET acknowledged = 1 WHERE alert_kind IS NOT NULL AND acknowledged = 0"
    )
    conn.commit()
    return cursor.rowcount or 0
