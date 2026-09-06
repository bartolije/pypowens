"""Ce qui change quand l'app quitte le poste de travail.

Publier l'app déplace trois hypothèses qui allaient de soi en local : les
fichiers ne survivent plus au redémarrage, n'importe qui peut frapper à la
porte, et la banque doit savoir où renvoyer l'utilisateur. Chacune se paie
comptant — un état perdu, ce sont toutes les connexions bancaires à refaire.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app import auth, collector
from app.config import _REPO_ROOT, get_settings
from app.notify import notify


@pytest.fixture(autouse=True)
def _env_de_deploiement(monkeypatch):
    """Part d'un poste vierge, et remet le compteur d'échecs à zéro.

    Ce compteur vit dans le module : sans remise à zéro, un test qui épuise les
    tentatives verrouillerait le suivant.
    """
    for variable in (
        "APP_AUTH_USER",
        "APP_AUTH_PASSWORD",
        "APP_DATA_DIR",
        "APP_PUBLIC_URL",
        "APP_NOTIFY",
        "APP_NOTIFY_URL",
        "APP_NOTIFY_TOKEN",
        "APP_COLLECT_EVERY_HOURS",
        "RAILWAY_VOLUME_MOUNT_PATH",
        "RAILWAY_PUBLIC_DOMAIN",
        "PORT",
    ):
        monkeypatch.delenv(variable, raising=False)
    auth.reset_failures()
    yield
    auth.reset_failures()


# --------------------------------------------------------------- persistance


def test_le_volume_porte_la_base_et_le_token(monkeypatch, tmp_path):
    """Les deux fichiers doivent atterrir sur le volume, pas seulement la base.

    L'état porte le token Powens : le perdre fait créer un NOUVEL utilisateur au
    démarrage suivant, donc un compte vierge sans aucune banque connectée.
    """
    monkeypatch.delenv("APP_DB_PATH", raising=False)
    monkeypatch.delenv("APP_STATE_PATH", raising=False)
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path))

    settings = get_settings()

    assert settings.db_path == tmp_path / ".powens_finance.db"
    assert settings.state_path == tmp_path / ".powens_state.json"


def test_sans_volume_rien_ne_bouge(monkeypatch):
    """L'exécution locale reste exactement celle d'avant."""
    monkeypatch.delenv("APP_DB_PATH", raising=False)
    monkeypatch.delenv("APP_STATE_PATH", raising=False)

    settings = get_settings()

    assert settings.db_path == _REPO_ROOT / ".powens_finance.db"
    assert settings.state_path == _REPO_ROOT / ".powens_state.json"


def test_un_chemin_explicite_l_emporte_sur_le_volume(monkeypatch, tmp_path):
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path / "volume"))
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "choisi.db"))

    assert get_settings().db_path == tmp_path / "choisi.db"


# ------------------------------------------------------------------ callback


def test_le_callback_suit_le_domaine_public(monkeypatch):
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "finance.up.railway.app")

    assert get_settings().redirect_uri == "https://finance.up.railway.app/callback"


def test_un_domaine_choisi_prime_sur_celui_de_l_hebergeur(monkeypatch):
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "finance.up.railway.app")
    monkeypatch.setenv("APP_PUBLIC_URL", "https://argent.exemple.fr/")

    assert get_settings().redirect_uri == "https://argent.exemple.fr/callback"


def test_en_local_le_callback_reste_la_loopback():
    assert get_settings().redirect_uri == "http://127.0.0.1:8000/callback"


def test_le_port_impose_par_l_hebergeur_est_repris(monkeypatch):
    monkeypatch.delenv("APP_PORT", raising=False)
    monkeypatch.setenv("PORT", "4242")

    assert get_settings().port == 4242


# ------------------------------------------------------- écoute non loopback


def test_ecouter_au_dela_de_la_loopback_exige_une_porte(monkeypatch):
    monkeypatch.setenv("APP_HOST", "0.0.0.0")

    with pytest.raises(RuntimeError, match="APP_AUTH_USER"):
        get_settings()


def test_l_authentification_autorise_l_ecoute_publique(monkeypatch):
    monkeypatch.setenv("APP_HOST", "0.0.0.0")
    monkeypatch.setenv("APP_AUTH_USER", "jb")
    monkeypatch.setenv("APP_AUTH_PASSWORD", "secret")

    assert get_settings().host == "0.0.0.0"


# ------------------------------------------------------------ authentification


def test_sans_identifiants_configures_l_app_reste_ouverte(client):
    """L'usage local ne doit gagner aucune friction."""
    assert client.get("/").status_code == 200


def test_les_identifiants_sont_exiges_des_qu_ils_existent(client, monkeypatch):
    monkeypatch.setenv("APP_AUTH_USER", "jb")
    monkeypatch.setenv("APP_AUTH_PASSWORD", "secret")

    refuse = client.get("/")
    assert refuse.status_code == 401
    assert refuse.headers["www-authenticate"].startswith("Basic")

    assert client.get("/", auth=("jb", "secret")).status_code == 200
    assert client.get("/", auth=("jb", "SECRET")).status_code == 401
    assert client.get("/", auth=("autre", "secret")).status_code == 401


def test_la_sonde_de_l_hebergeur_echappe_a_l_authentification(client, monkeypatch):
    """Un 401 sur /health ferait passer un déploiement sain pour une panne."""
    monkeypatch.setenv("APP_AUTH_USER", "jb")
    monkeypatch.setenv("APP_AUTH_PASSWORD", "secret")

    assert client.get("/health").status_code == 200


def test_un_mot_de_passe_accentue_ne_fait_pas_exploser_la_comparaison(client, monkeypatch):
    """``compare_digest`` refuse les chaînes non ASCII : d'où la comparaison sur octets."""
    monkeypatch.setenv("APP_AUTH_USER", "jb")
    monkeypatch.setenv("APP_AUTH_PASSWORD", "clé-très-sûre")

    assert client.get("/", auth=("jb", "clé-très-sûre")).status_code == 200
    assert client.get("/", auth=("jb", "cle-tres-sure")).status_code == 401


def test_les_tentatives_repetees_finissent_par_verrouiller(client, monkeypatch):
    """Basic n'oppose rien à la force brute : le ralentissement est tout ce qu'il y a."""
    monkeypatch.setenv("APP_AUTH_USER", "jb")
    monkeypatch.setenv("APP_AUTH_PASSWORD", "secret")

    for _ in range(auth._MAX_FAILURES):
        assert client.get("/", auth=("jb", "au hasard")).status_code == 401

    bloque = client.get("/", auth=("jb", "secret"))  # même le bon mot de passe
    assert bloque.status_code == 429
    assert int(bloque.headers["retry-after"]) > 0


def test_une_visite_sans_identifiants_ne_consomme_pas_le_quota(client, monkeypatch):
    """Le navigateur frappe toujours une première fois sans en-tête."""
    monkeypatch.setenv("APP_AUTH_USER", "jb")
    monkeypatch.setenv("APP_AUTH_PASSWORD", "secret")

    for _ in range(auth._MAX_FAILURES + 5):
        assert client.get("/").status_code == 401

    assert client.get("/", auth=("jb", "secret")).status_code == 200


# --------------------------------------------------------------------- CSRF


def test_un_post_venu_d_ailleurs_reste_refuse_meme_authentifie(client, monkeypatch):
    """Le navigateur rejoue les identifiants Basic sur une requête cross-site."""
    monkeypatch.setenv("APP_AUTH_USER", "jb")
    monkeypatch.setenv("APP_AUTH_PASSWORD", "secret")

    response = client.post(
        "/budgets",
        data={"categorie": "Streaming / Médias", "montant": "10"},
        headers={"Origin": "https://piege.exemple"},
        auth=("jb", "secret"),
    )

    assert response.status_code == 403


def test_un_post_emis_par_l_app_passe(client, monkeypatch):
    monkeypatch.setenv("APP_AUTH_USER", "jb")
    monkeypatch.setenv("APP_AUTH_PASSWORD", "secret")

    response = client.post(
        "/budgets",
        data={"categorie": "Streaming / Médias", "montant": ""},
        headers={"Origin": "http://testserver"},
        auth=("jb", "secret"),
    )

    assert response.status_code == 200


# -------------------------------------------------------------- notification


def test_la_notification_part_en_webhook(monkeypatch):
    """Aucun écran devant un serveur : osascript n'y notifierait personne."""
    envoye: dict = {}

    def _faux_post(url, *, json, headers, timeout):
        envoye.update(url=url, json=json, headers=headers)
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setenv("APP_NOTIFY_URL", "https://home.exemple.fr/api/notify")
    monkeypatch.setenv("APP_NOTIFY_TOKEN", "jeton")
    monkeypatch.setattr(httpx, "post", _faux_post)

    assert notify("Titre", "Message") is True
    assert envoye["json"] == {"title": "Titre", "message": "Message"}
    assert envoye["headers"]["Authorization"] == "Bearer jeton"


def test_une_notification_qui_echoue_ne_casse_pas_la_collecte(monkeypatch):
    def _panne(*_args, **_kwargs):
        raise httpx.ConnectError("réseau injoignable")

    monkeypatch.setenv("APP_NOTIFY_URL", "https://home.exemple.fr/api/notify")
    monkeypatch.setattr(httpx, "post", _panne)

    assert notify("Titre", "Message") is False


def test_la_notification_reste_coupable_par_l_environnement(monkeypatch):
    monkeypatch.setenv("APP_NOTIFY", "0")
    monkeypatch.setenv("APP_NOTIFY_URL", "https://home.exemple.fr/api/notify")

    assert notify("Titre", "Message") is False


# -------------------------------------------------------- collecte planifiée


async def test_la_collecte_planifiee_survit_a_une_panne(monkeypatch):
    """Une API en vrac ne doit pas coûter tous les jours suivants."""
    passages = []

    async def _sans_attendre(_delay):
        if len(passages) >= 2:
            raise asyncio.CancelledError

    async def _en_panne(_client, _conn, _settings):
        passages.append(1)
        raise RuntimeError("Powens indisponible")

    monkeypatch.setattr(asyncio, "sleep", _sans_attendre)
    monkeypatch.setattr(collector, "run_once", _en_panne)

    with pytest.raises(asyncio.CancelledError):
        await collector.scheduled(None, None, None, hours=12)

    assert len(passages) == 2  # la boucle a repris après l'échec


async def test_la_premiere_passe_n_attend_pas_un_intervalle_entier(monkeypatch):
    """Un redéploiement en fin de journée ne doit pas coûter le solde du jour."""
    attentes = []

    async def _note_l_attente(delay):
        attentes.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", _note_l_attente)

    with pytest.raises(asyncio.CancelledError):
        await collector.scheduled(None, None, None, hours=12)

    assert attentes == [collector._FIRST_RUN_DELAY]
