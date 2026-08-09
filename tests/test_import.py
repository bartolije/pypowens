"""Import de relevés CSV : parsing, déduplication, et fusion dans les agrégats.

Ce chemin existe parce qu'un connecteur peut manquer ou tomber, laissant un compte
entier — et ses abonnements — hors de l'analyse. Les tests portent donc autant sur le
parsing que sur le fait que les opérations importées comptent vraiment en aval.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest

from app import store
from app.enrich import internal_transfer_ids, merchant_key, resolve_category_txn
from app.importer import (
    ImportError_,
    fingerprint,
    infer_type,
    parse_amount,
    parse_date,
    parse_statement,
)
from app.recurring import detect_subscriptions
from pypowens import Transaction

# Un relevé bancaire type : séparateur ';', colonnes Débit/Crédit séparées.
# Libellés et montants fictifs — seule leur *forme* compte pour ces tests (préfixe
# d'opération, présence d'une date dans le libellé carte, casse). Le repo est public :
# aucun marchand, montant ni numéro de contrat réel n'a sa place ici.
RELEVE = b""""Date operation";"Date valeur";"Libelle";"Debit";"Credit"
"20/07/2026";"20/07/2026";"PRLV FOURNISSEUR ENERGIE";"42,00";""
"08/07/2026";"08/07/2026";"PRLV SALLE DE SPORT";"24,90";""
"04/07/2026";"04/07/2026";"ECH PRET 0000AB00000000";"1234,56";""
"01/07/2026";"01/07/2026";"F COTISATION CARTE VISA";"5,50";""
"23/06/2026";"23/06/2026";"CARTE 22/06 BOUTIQUE*EXEMPLE Ville";"2,40";""
"20/06/2026";"20/06/2026";"PRLV FOURNISSEUR ENERGIE";"42,00";""
"14/06/2026";"14/06/2026";"RET DAB 14/06 VILLE QUARTIER";"40,00";""
"10/06/2026";"10/06/2026";"VIR M NOM PRENOM";"";"900,00"
"""


def _statement(text: bytes = RELEVE, account_id: int = -1):
    return parse_statement(text, account_id=account_id)


# ------------------------------------------------------------------ primitives

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,70", Decimal("1.70")),
        ("1234,56", Decimal("1234.56")),
        ("1 048,63", Decimal("1048.63")),      # espace comme séparateur de milliers
        ("1 048,63", Decimal("1048.63")),      # espace insécable
        ("-12,00", Decimal("-12.00")),
        ("12,00 €", Decimal("12.00")),
        ("", None),
        ("n/a", None),
    ],
)
def test_parse_amount_handles_french_formats(raw, expected):
    assert parse_amount(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("20/07/2026", date(2026, 7, 20)),
        ("20-07-2026", date(2026, 7, 20)),
        ("2026-07-20", date(2026, 7, 20)),
        ("32/07/2026", None),
        ("", None),
    ],
)
def test_parse_date_handles_common_formats(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize(
    ("wording", "expected"),
    [
        ("CARTE 22/06 BOUTIQUE*EXEMPLE", "card"),
        ("PRLV FOURNISSEUR ENERGIE", "order"),
        ("ECH PRET 0000AB00000000", "loan_repayment"),
        ("RET DAB 14/06 VILLE", "withdrawal"),
        ("VIR M NOM", "transfer"),
        ("F COTISATION CARTE VISA", "bank"),
        ("ANNUL COTISATION", "payback"),
        ("QUELQUE CHOSE", "unknown"),
    ],
)
def test_infer_type_from_the_statement_prefix(wording, expected):
    """Le type décide de l'inclusion dans les abonnements : un PRLV mal typé disparaît."""
    assert infer_type(wording) == expected


# --------------------------------------------------------------------- parsing

def test_debit_is_negative_and_credit_positive():
    parsed = _statement()
    by_wording = {t.wording: t.value for t in parsed.transactions}
    assert by_wording["PRLV FOURNISSEUR ENERGIE"] == Decimal("-42.00")
    assert by_wording["VIR M NOM PRENOM"] == Decimal("900.00")


def test_all_rows_are_parsed_and_dated():
    parsed = _statement()
    assert len(parsed.transactions) == 8
    assert parsed.skipped == 0
    assert parsed.first_date == date(2026, 6, 10)
    assert parsed.last_date == date(2026, 7, 20)


def test_imported_ids_stay_out_of_the_powens_id_space():
    """Les ids servent d'ensembles d'exclusion : une collision fausserait les séries."""
    assert all(t.id is not None and t.id < 0 for t in _statement().transactions)


def test_a_comma_separated_file_is_also_accepted():
    csv = (
        b'"Date operation","Libelle","Montant"\n'
        b'"20/07/2026","PRLV TRUC","-38.00"\n'
        b'"21/07/2026","VIR MACHIN","1500.00"\n'
    )
    parsed = parse_statement(csv, account_id=-1)
    assert [t.value for t in parsed.transactions] == [Decimal("-38.00"), Decimal("1500.00")]


def test_a_signed_amount_column_is_used_when_there_is_no_debit_credit_pair():
    csv = b'"Date";"Libelle";"Montant"\n"20/07/2026";"PRLV TRUC";"-38,00"\n'
    assert parse_statement(csv, account_id=-1).transactions[0].value == Decimal("-38.00")


def test_missing_columns_are_reported_with_the_headers_read():
    with pytest.raises(ImportError_, match="date et libellé"):
        parse_statement(b'"Colonne A";"Colonne B"\n"x";"y"\n', account_id=-1)


def test_a_file_without_a_single_usable_row_is_rejected():
    csv = b'"Date operation";"Libelle";"Debit";"Credit"\n"";"";"";""\n'
    with pytest.raises(ImportError_, match="Aucune opération"):
        parse_statement(csv, account_id=-1)


def test_latin1_encoded_file_is_decoded():
    csv = '"Date operation";"Libelle";"Debit";"Credit"\n"20/07/2026";"PRLV Énergie";"38,00";""\n'
    parsed = parse_statement(csv.encode("cp1252"), account_id=-1)
    assert "Énergie" in parsed.transactions[0].wording


# ---------------------------------------------------------------- fingerprints

def test_two_identical_operations_on_the_same_day_are_both_kept():
    """Deux stationnements à 1,30 € le même jour ne sont pas un doublon d'import."""
    csv = (
        b'"Date operation";"Libelle";"Debit";"Credit"\n'
        b'"28/07/2026";"CARTE 26/07 STATIONNEMENT";"1,30";""\n'
        b'"28/07/2026";"CARTE 26/07 STATIONNEMENT";"1,30";""\n'
    )
    parsed = parse_statement(csv, account_id=-1)
    assert len(parsed.transactions) == 2
    assert len(set(parsed.fingerprints)) == 2


def test_fingerprint_is_stable_across_parses():
    first, second = _statement(), _statement()
    assert first.fingerprints == second.fingerprints


def test_fingerprint_separates_accounts():
    assert fingerprint(-1, date(2026, 7, 1), Decimal("-10"), "X") != fingerprint(
        -2, date(2026, 7, 1), Decimal("-10"), "X"
    )


# ---------------------------------------------------------------------- store

@pytest.fixture
def conn(tmp_path):
    return store.connect(tmp_path / "store.db")


def test_reimporting_the_same_statement_adds_nothing(conn):
    db_id = store.upsert_imported_account(conn, "CCF")
    parsed = _statement(account_id=store.account_id(db_id))
    added, duplicates = store.save_imported(
        conn, db_id, parsed.transactions, parsed.fingerprints, source="a.csv"
    )
    assert (added, duplicates) == (8, 0)
    added, duplicates = store.save_imported(
        conn, db_id, parsed.transactions, parsed.fingerprints, source="a.csv"
    )
    assert (added, duplicates) == (0, 8)


def test_the_same_label_feeds_one_account(conn):
    first = store.upsert_imported_account(conn, "CCF")
    assert store.upsert_imported_account(conn, "  CCF  ") == first
    assert len(store.imported_accounts(conn)) == 1


def test_imported_account_is_exposed_as_a_powens_payload(conn):
    db_id = store.upsert_imported_account(conn, "CCF", type="checking", currency="eur")
    parsed = _statement(account_id=store.account_id(db_id))
    store.save_imported(conn, db_id, parsed.transactions, parsed.fingerprints, source="a.csv")
    (payload,) = store.imported_accounts(conn)
    assert payload["id"] == store.account_id(db_id) < 0
    assert payload["type"] == "checking"
    assert payload["currency"] == {"id": "EUR"}
    # Le "solde" est une somme de flux : un relevé ne porte pas le solde courant.
    assert Decimal(payload["balance"]) == sum(t.value for t in parsed.transactions)


def test_deleting_an_imported_account_removes_its_operations(conn):
    db_id = store.upsert_imported_account(conn, "CCF")
    parsed = _statement(account_id=store.account_id(db_id))
    store.save_imported(conn, db_id, parsed.transactions, parsed.fingerprints, source="a.csv")
    assert store.delete_imported_account(conn, db_id) == 8
    assert store.imported_transactions(conn) == []
    assert store.imported_accounts(conn) == []


def test_imported_transactions_can_be_windowed(conn):
    db_id = store.upsert_imported_account(conn, "CCF")
    parsed = _statement(account_id=store.account_id(db_id))
    store.save_imported(conn, db_id, parsed.transactions, parsed.fingerprints, source="a.csv")
    recent = store.imported_transactions(conn, since=date(2026, 7, 1))
    assert len(recent) == 4
    assert all(row["date"] >= "2026-07-01" for row in recent)


# ------------------------------------------------------- fusion avec le pipeline

def _loaded(conn):
    return [Transaction.from_api(raw) for raw in store.imported_transactions(conn)]


def test_a_card_wording_is_grouped_by_merchant_not_by_date(conn):
    """« CARTE 22/06 MARCHAND » : sans nettoyage du préfixe, la clé devient la date."""
    db_id = store.upsert_imported_account(conn, "CCF")
    parsed = _statement(account_id=store.account_id(db_id))
    card = next(t for t in parsed.transactions if t.wording.startswith("CARTE"))
    key = merchant_key(card)
    assert "CARTE" not in key
    assert not re.search(r"\d{2}\s+\d{2}", key)
    assert "BOUTIQUE" in key


def test_the_mortgage_instalment_becomes_a_tracked_commitment(conn):
    """Une échéance de prêt est un engagement mensuel fixe, souvent la plus grosse ligne
    du relevé — et elle était totalement absente sans l'import."""
    db_id = store.upsert_imported_account(conn, "CCF")
    rows = "\n".join(
        f'"04/{month:02d}/2026";"04/{month:02d}/2026";"ECH PRET 0000AB00000000";"1234,56";""'
        for month in range(2, 8)
    )
    csv = (
        '"Date operation";"Date valeur";"Libelle";"Debit";"Credit"\n' + rows + "\n"
    ).encode()
    parsed = parse_statement(csv, account_id=store.account_id(db_id))
    store.save_imported(conn, db_id, parsed.transactions, parsed.fingerprints, source="a.csv")

    items = detect_subscriptions(_loaded(conn), today=date(2026, 7, 20))
    (pret,) = [i for i in items if "PRET" in i.key]
    assert pret.periodicity == "Mensuel"
    assert pret.amount == Decimal("1234.56")
    assert pret.category == "Logement / charges"


def test_a_cash_withdrawal_keeps_its_own_category(conn):
    db_id = store.upsert_imported_account(conn, "CCF")
    parsed = _statement(account_id=store.account_id(db_id))
    withdrawal = next(t for t in parsed.transactions if t.type == "withdrawal")
    assert resolve_category_txn(withdrawal) == "Retrait espèces"


def test_an_inter_bank_transfer_pair_is_detected_once_both_sides_are_loaded(conn):
    """L'intérêt d'importer : le miroir d'un virement vivait sur l'autre banque."""
    db_id = store.upsert_imported_account(conn, "CCF")
    csv = (
        b'"Date operation";"Libelle";"Debit";"Credit"\n'
        b'"01/07/2026";"vers NOM PRENOM - AUTRE BANQUE";"900,00";""\n'
    )
    parsed = parse_statement(csv, account_id=store.account_id(db_id))
    store.save_imported(conn, db_id, parsed.transactions, parsed.fingerprints, source="a.csv")

    powens_side = Transaction.from_api(
        {
            "id": 5000,
            "id_account": 1,
            "date": "2026-07-02",
            "value": "900.00",
            "type": "transfer",
            "wording": "VIR NOM PRENOM",
            "simplified_wording": "NOM PRENOM",
        }
    )
    both = [*_loaded(conn), powens_side]
    internal = internal_transfer_ids(both)
    assert {t.id for t in both} == internal  # les deux jambes sont exclues


# ------------------------------------- rattachement à un compte Powens (fusion)
#
# Un relevé est importé quand aucun connecteur ne remonte le compte. Le jour où un
# connecteur s'y met, les deux sources se recouvrent : sans rattachement, la période
# commune est comptée deux fois et le solde apparaît sur deux comptes.

def _linked(conn, *, label: str = "CCF", powens_id: int = 1) -> int:
    db_id = store.upsert_imported_account(conn, label)
    parsed = _statement(account_id=store.account_id(db_id))
    store.save_imported(conn, db_id, parsed.transactions, parsed.fingerprints, source="a.csv")
    store.link_imported_account(conn, db_id, powens_id)
    return db_id


def test_a_linked_account_is_no_longer_a_account_of_its_own(conn):
    """Son solde est celui du compte Powens : l'exposer deux fois double l'argent."""
    db_id = _linked(conn)
    assert store.imported_accounts(conn) == []
    assert store.imported_links(conn) == {db_id: 1}
    # Le compte reste visible sur la page d'import, avec sa cible.
    (summary,) = store.imported_summary(conn)
    assert summary["powens_account_id"] == 1


def test_operations_of_a_linked_account_are_served_under_the_powens_id(conn):
    """Sinon l'historique ancien tombe hors des pages filtrées sur les comptes courants."""
    _linked(conn)
    rows = store.imported_transactions(conn)
    assert rows and {row["id_account"] for row in rows} == {1}
    assert all(row["id"] < 0 for row in rows)  # identité propre conservée


def test_the_period_covered_by_the_connector_is_dropped(conn):
    """La borne est la 1re date remontée par Powens : au-delà, c'est un doublon."""
    db_id = _linked(conn)
    kept = store.imported_transactions(conn, ceilings={db_id: date(2026, 7, 1)})
    assert [row["date"] for row in kept] == [
        "2026-06-10",
        "2026-06-14",
        "2026-06-20",
        "2026-06-23",
    ]
    # Le jour de la borne appartient au connecteur, pas au relevé.
    assert all(row["date"] < "2026-07-01" for row in kept)


def test_a_ceiling_only_applies_to_its_own_account(conn):
    first = _linked(conn, label="CCF")
    second = store.upsert_imported_account(conn, "Autre banque")
    parsed = _statement(account_id=store.account_id(second))
    store.save_imported(conn, second, parsed.transactions, parsed.fingerprints, source="b.csv")

    rows = store.imported_transactions(conn, ceilings={first: date(2026, 1, 1)})
    assert {row["id_account"] for row in rows} == {store.account_id(second)}


def test_unlinking_gives_the_account_back_its_autonomy(conn):
    db_id = _linked(conn)
    store.link_imported_account(conn, db_id, None)
    (payload,) = store.imported_accounts(conn)
    assert payload["id"] == store.account_id(db_id)
    assert store.imported_links(conn) == {}
    assert {row["id_account"] for row in store.imported_transactions(conn)} == {
        store.account_id(db_id)
    }


def test_linking_forgets_the_balance_snapshots_of_the_imported_account(conn):
    """Ils ont été pris quand le compte comptait pour lui-même : ils feraient une bosse."""

    class Acc:
        def __init__(self, account_id: int, balance: str) -> None:
            self.id, self.balance = account_id, Decimal(balance)
            self.name, self.type, self.currency = "x", "checking", "EUR"

    db_id = store.upsert_imported_account(conn, "CCF")
    store.record_snapshot(
        conn,
        [Acc(store.account_id(db_id), "930.70"), Acc(1, "2500.00")],
        day=date(2026, 7, 30),
    )
    store.link_imported_account(conn, db_id, 1)

    remaining = {
        row["account_id"]
        for row in conn.execute("SELECT account_id FROM balance_snapshot")
    }
    assert remaining == {1}


def test_the_migration_adds_the_column_to_an_existing_database(tmp_path):
    """Une base créée avant la colonne doit s'ouvrir sans perdre ses données."""
    import sqlite3

    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE imported_account (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " label TEXT NOT NULL UNIQUE, type TEXT NOT NULL, currency TEXT NOT NULL,"
        " created TEXT NOT NULL)"
    )
    legacy.execute(
        "INSERT INTO imported_account (label, type, currency, created)"
        " VALUES ('CCF', 'checking', 'EUR', '2026-07-30')"
    )
    legacy.commit()
    legacy.close()

    conn = store.connect(path)
    (row,) = conn.execute("SELECT label, powens_account_id FROM imported_account")
    assert (row["label"], row["powens_account_id"]) == ("CCF", None)


# ------------------------------------------------------- la fusion, par les routes
#
# Le scénario réel : un relevé de 18 mois importé la veille, puis un connecteur qui se
# met à remonter les 3 derniers mois du même compte.

def _text(html: str) -> str:
    plain = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"[\s  ]+", " ", plain)


def _months_ago(count: int) -> date:
    """Le 15 du mois situé ``count`` mois avant celui-ci."""
    total = date.today().year * 12 + date.today().month - 1 - count
    return date(total // 12, total % 12 + 1, 15)


# Le compte 1 du jeu de test couvre 14 mois, et le sélecteur de mois en propose 18 : à
# 16 mois, on est hors de ce que le connecteur remonte, mais toujours consultable.
OLD = _months_ago(16)
RECENT = _months_ago(2)

TWO_LINES = (
    '"Date operation";"Libelle";"Debit";"Credit"\n'
    f'"{OLD.strftime("%d/%m/%Y")}";"VIR ANCIENRELEVE";"";"100,00"\n'
    f'"{RECENT.strftime("%d/%m/%Y")}";"VIR RECENTRELEVE";"";"40,00"\n'
).encode()


def _upload(client, csv: bytes = TWO_LINES, libelle: str = "CCF — compte chèque"):
    return client.post(
        "/import",
        files={"fichier": ("releve.csv", csv, "text/csv")},
        data={"libelle": libelle, "type_compte": "checking", "devise": "EUR"},
    )


def test_an_unlinked_statement_doubles_the_available_total(client):
    """L'état de départ, celui qu'on veut corriger : deux comptes pour un seul."""
    assert _upload(client).status_code == 200
    body = _text(client.get("/comptes").text)
    assert "CCF — compte chèque" in body
    # 2 500 (Powens) + 140 (somme des flux du relevé) : le même argent, deux fois.
    assert "2 640,00 €" in body


def test_linking_the_statement_restores_a_single_balance(client):
    _upload(client)
    # Premier compte importé de la base de test, donc id 1.
    assert client.post("/import/rattacher/1", data={"compte_powens": "1"}).status_code == 200

    body = _text(client.get("/comptes").text)
    assert "2 500,00 €" in body
    assert "2 640,00 €" not in body
    # Le relevé n'est plus un compte, mais reste listé sur la page d'import.
    assert "CCF — compte chèque" not in _text(client.get("/comptes").text)
    assert "CCF — compte chèque" in _text(client.get("/import").text)


def test_linking_keeps_the_history_the_connector_does_not_cover(client):
    """Tout l'intérêt de la fusion : garder les mois d'avant sans doubler ceux d'après."""
    _upload(client)
    client.post("/import/rattacher/1", data={"compte_powens": "1"})

    old_month = client.get("/comptes", params={"mois": OLD.strftime("%Y-%m"), "sens": "tout"})
    assert "ANCIENRELEVE" in _text(old_month.text)

    recent_month = client.get("/comptes", params={"mois": RECENT.strftime("%Y-%m"), "sens": "tout"})
    assert "RECENTRELEVE" not in _text(recent_month.text)


def test_unlinking_through_the_route_puts_the_account_back(client):
    _upload(client)
    client.post("/import/rattacher/1", data={"compte_powens": "1"})
    client.post("/import/rattacher/1", data={"compte_powens": ""})
    assert "2 640,00 €" in _text(client.get("/comptes").text)


def test_the_import_page_offers_the_powens_accounts_as_targets(client):
    _upload(client)
    body = client.get("/import").text
    assert 'action="/import/rattacher/1"' in body
    assert "Compte courant" in body  # le compte 1 du jeu de test
