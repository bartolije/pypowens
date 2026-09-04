# pypowens

An **async Python wrapper** for the [Powens](https://www.powens.com/) (ex‑Budget Insight)
bank data aggregation API. Built on [`httpx`](https://www.python-httpx.org/), fully typed,
zero heavy dependencies.

> ⚠️ Alpha (`0.2.0`). Covers the **core aggregation** surface: OAuth2 auth,
> connectors, connections, accounts, transactions and investments.

## Features

- 🔑 OAuth2 flows: create a user + permanent token, renew, temporary code, code exchange, revoke.
- 🏦 List connectors (banks/providers).
- 🔗 List / delete user connections, and **force a refresh** (`update_connection`).
- 💳 List accounts with per-currency balances.
- 📜 List transactions with automatic **pagination** (follows `_links.next`) and filters.
- 📈 List **investments** (security lines, valuation, unrealized gain).
- 🔁 **Retries with backoff** on 429/5xx and network errors, honouring `Retry-After`.
- 🧱 Forgiving models — every object keeps its raw payload in `.raw`.
- ✅ Typed (`py.typed`, `mypy --strict`), async context manager, tested with `respx`.

## Installation

```bash
pip install pypowens
# or, from source with uv:
uv pip install -e ".[dev]"
```

Requires Python 3.11+.

## Quick start

```python
import asyncio
from pypowens import PowensClient

async def main():
    async with PowensClient(
        "myapp-sandbox",              # your Powens domain (or full host)
        client_id="...",
        client_secret="...",
    ) as powens:
        # Create a user and obtain a permanent token (stored on the client).
        token = await powens.create_user()
        print("user:", token.id_user)

        # Explore.
        connectors = await powens.list_connectors(country_codes="fr")
        accounts = await powens.list_accounts()
        transactions = await powens.list_transactions(max_transactions=50)

        for txn in transactions:
            print(txn.date, txn.value, txn.wording)

asyncio.run(main())
```

Or configure from the environment (`POWENS_DOMAIN`, `POWENS_CLIENT_ID`,
`POWENS_CLIENT_SECRET`, `POWENS_ACCESS_TOKEN` — see [`.env.example`](.env.example)):

```python
async with PowensClient.from_env() as powens:
    ...
```

See [`examples/quickstart.py`](examples/quickstart.py) for a full run.

## API surface

| Method | Powens endpoint |
| --- | --- |
| `create_user()` | `POST /auth/init` |
| `renew_token(id_user)` | `POST /auth/renew` |
| `get_temporary_code()` | `GET /auth/token/code` |
| `exchange_code(code)` | `POST /auth/token/access` |
| `revoke_token()` | `DELETE /auth/token` |
| `get_current_user()` | `GET /users/me` |
| `get_client_config()` | `GET /clients/{id}` |
| `list_connectors()` | `GET /connectors` |
| `list_connections()` | `GET /users/{id}/connections` |
| `delete_connection(id)` | `DELETE /users/{id}/connections/{cid}` |
| `update_connection(id)` | `PUT /users/{id}/connections/{cid}` |
| `list_accounts()` | `GET /users/{id}/accounts` |
| `get_account(id)` | `GET /users/{id}/accounts/{aid}` |
| `list_transactions()` / `iter_transactions()` | `GET /users/{id}/transactions` |
| `list_investments()` | `GET /users/{id}/investments` |

All authenticated calls send `Authorization: Bearer <access_token>`. The token is
set automatically after `create_user()` / `renew_token()` / `exchange_code()`, or you
can pass `access_token=...` to the constructor.

### Streaming transactions

`iter_transactions()` is an async generator that transparently follows pagination:

```python
async for txn in powens.iter_transactions(min_date="2026-01-01", income=True):
    ...
```

### Error handling and retries

429, 5xx and network errors are retried with exponential backoff and ±20 % jitter
(1 initial attempt + 3 retries by default, `Retry-After` honoured and capped at
60 s). A 429 is replayed on any method; 5xx and network errors are only replayed
on **idempotent** methods (GET/HEAD/PUT/DELETE) — a replayed `POST /auth/init`
could create duplicate users. Configure with `PowensClient(..., max_retries=…,
backoff=…)`; `max_retries=0` disables it. Exceptions are raised once the retries
are exhausted:

```python
from pypowens import PowensAPIError, PowensAuthError, PowensRateLimitError

try:
    await powens.get_current_user()
except PowensAuthError:           # 401 / 403 — not retried
    ...
except PowensRateLimitError as e: # 429 after retries
    print(e.retry_after)
except PowensAPIError as e:       # any other non-2xx
    print(e.status_code, e.code, e.message)
```

## Development

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[app,dev]"
pytest              # 99 tests, fully offline (respx + a fake client)
ruff check .        # lint
mypy src/pypowens   # strict types on the library
```

CI runs the three on Python 3.11, 3.12 and 3.13.

## Application (analyse de finances perso)

Le dépôt embarque une **application web locale** (FastAPI) construite sur le wrapper,
dans le dossier `app/` (non incluse dans le wheel publié).

Elle est pensée pour être **consultable au bureau** : les montants sont **masqués par
défaut** (bouton « 👁 Montants » pour révéler, choix mémorisé) et le patrimoine n'est
pas la page d'accueil mais le dernier onglet.

- **Comptes** (`/`) — solde disponible sur les comptes courants uniquement, puis
  l'**historique des dépenses par date** : un bloc par jour avec son total, badge de
  type de dépense, filtres mois / type / dépenses-ou-tout. Les virements internes sont
  affichés mais exclus des totaux.
- **Abonnements** (`/abonnements`) — contrats et abonnements **groupés par type de
  dépense** avec sous-total €/mois et €/an, historique de prix (dérive depuis la 1re
  échéance + courbe), voie de paiement (prélèvement SEPA ou carte), badge
  « en sommeil » quand une série n'est plus prélevée, et **alertes** sur les nouveaux
  abonnements et les hausses depuis la dernière visite. `?tout=1` élargit aux séries
  répétées incertaines.
- **Récurrences** (`/recurrences`) — vue brute par libellé normalisé : nombre
  d'occurrences, total, moyenne, intervalle moyen.
- **Analyse** (`/analyse`) — revenus vs dépenses par mois, répartition par catégorie,
  ventilation exacte récurrent / ponctuel. Toutes les figures portent sur la même
  fenêtre (12 mois complets), affichée en tête de page.
- **Patrimoine** (`/patrimoine`) — patrimoine net, **courbe d'évolution** (historisée
  en local), comptes groupés par famille, lignes de titres détenues, comptes en devises
  étrangères listés à part, état des connexions + bouton de synchronisation.
- **Import** (`/import`) — pour les comptes qu'aucun connecteur ne remonte : un relevé
  CSV exporté depuis la banque alimente l'historique, l'analyse et la détection
  d'abonnements comme les données Powens. Voir plus bas.
- **Détail** (`/transactions?label=…`) — les opérations derrière un libellé, leur
  évolution mensuelle, et la correction de catégorie (mémorisée localement).

### Fraîcheur des données et latence

Les données Powens sont mises en cache (comptes et connexions 2 min, historique
5 min). Une entrée périmée est **servie immédiatement et rafraîchie en arrière-plan** :
aucune page n'attend Powens après le préchauffage du démarrage. Au-delà d'une
heure sans rafraîchissement réussi, le chargement redevient bloquant et l'erreur
s'affiche. Les calculs dérivés (séries récurrentes, courbe de patrimoine,
valorisations) sont mémorisés tant que ni le cache ni les tables locales ne
changent. Les statiques sont versionnés et mis en cache un an ; les pages sont
compressées et jamais stockées par le navigateur.

### Ce qui compte comme abonnement

`detect_recurring()` repère toute série répétée — l'analyse en a besoin pour séparer
récurrent et ponctuel. La page Abonnements applique en plus `is_subscription()`, car
sur de vrais relevés la passe permissive remonte surtout des courses de supermarché
qui se regroupent par montant. Les signaux retenus :

| Voie | Critère |
|---|---|
| Prélèvement SEPA | c'est un mandat signé → suffit, dès que la cadence tient (ou ≥ 6 prélèvements, cas de deux contrats entrelacés chez le même émetteur) |
| Carte | montant **quasi identique** à chaque échéance (≤ 2 % d'écart) ; pour 2 occurrences annuelles, renouvellement à ± 12 j de la date anniversaire |
| Toutes | les types de dépense du quotidien (alimentation, restauration, carburant, retraits) ne sont jamais des abonnements |

```bash
uv pip install -e ".[app]"     # dépendances de l'app
# .env : POWENS_DOMAIN, POWENS_CLIENT_ID/SECRET, POWENS_ACCESS_TOKEN
python -m app                  # http://127.0.0.1:8000
```

Le token est résolu par priorité : `POWENS_ACCESS_TOKEN` (.env) → `.powens_state.json`
→ `create_user()`. En cas de 401, l'app tente **un** renouvellement automatique
(`renew_token`) avant d'afficher une page d'aide.

Pour connecter une banque via le Webview Powens, le `redirect_uri`
`http://127.0.0.1:8000/callback` doit être déclaré dans la console Powens
(Configuration → Webview → redirect URIs). `/connect` le vérifie via
`get_client_config()` **avant** d'envoyer vers le Webview : sinon Powens refuse le
retour avec « the parameter must match the constraints defined in the administration
console », sans jamais dire quelle valeur il attendait. `/connect?connector_id=…`
ouvre directement sur un établissement.

### Import de relevé CSV

Un connecteur absent ou en panne laisse un compte entier hors de l'analyse — et avec lui
ses abonnements. `/import` accepte un relevé exporté depuis la banque : les opérations
sont stockées en local et **fusionnées dans tous les agrégats**, parce que le pipeline
(normalisation des libellés, catégorisation, détection d'abonnements) travaille sur des
transactions sans se soucier de leur provenance.

- **Formats** : séparateur `;` ou `,`, une colonne date, une colonne libellé, puis
  Débit/Crédit séparés **ou** une colonne Montant signée. Encodages UTF-8, Windows-1252
  et Latin-1. Montants à la française (`1 048,63`).
- **Type d'opération déduit du préfixe** (`CARTE` → carte, `PRLV` → prélèvement,
  `ECH` → échéance de prêt, `RET DAB` → retrait…). C'est ce qui rend un abonnement
  détectable : la page Abonnements ne regarde que certains rails.
- **Réimport sans risque** : chaque ligne porte une empreinte
  `(compte, date, montant, libellé, rang du jour)`, donc deux exports qui se recouvrent
  ne créent pas de doublon. Le *rang du jour* évite l'inverse — deux stationnements
  identiques le même jour restent deux opérations.
- **Ids négatifs** : l'espace des ids positifs appartient à Powens. Les deux jeux se
  croisent dans des ensembles d'exclusion (virements internes, séries), une collision
  fausserait les totaux.
- Effet de bord utile : un virement entre deux de vos banques n'avait qu'une jambe
  visible, donc comptait comme une dépense. Importer la seconde le fait reconnaître
  comme virement interne et l'exclut des totaux.

### Données locales (jamais versionnées)

| Fichier | Contenu |
|---|---|
| `.env` | identifiants d'app et token |
| `.powens_state.json` | `id_user` + token persistés |
| `.powens_finance.db` | SQLite : historique des soldes, catégories forcées, état des séries |
| `categories.local.json` | règles de catégorisation propres à vos relevés (voir l'exemple) |

En local, l'app **n'a pas d'authentification**, et refuse de démarrer sur une
interface autre que loopback. Pour la publier il faut donc lui en donner une —
`APP_AUTH_USER` / `APP_AUTH_PASSWORD` — ou la placer derrière un proxy
authentifiant (`APP_ALLOW_REMOTE=1`) ; la marche à suivre complète, volume
persistant compris, est dans [docs/DEPLOIEMENT.md](docs/DEPLOIEMENT.md). Voir
`.env.example` pour les réglages (`APP_HISTORY_MONTHS`, `APP_BASE_CURRENCY`,
`APP_DB_PATH`…).

> La catégorisation native Powens et le produit *indicators* n'étant pas
> alimentés sur toutes les apps, la catégorisation est faite localement
> (mots-clés + overrides) et l'analyse repose sur les transactions.

### Connexion

Sans `APP_AUTH_USER` / `APP_AUTH_PASSWORD`, l'app ne demande rien : c'est l'usage
local, sur la loopback. Dès que ces deux variables existent, tout passe par
`/connexion` — un formulaire classique (remplissable par un gestionnaire de mots
de passe) qui pose un cookie de session signé HMAC-SHA256, valable sept jours,
`HttpOnly` / `SameSite=Lax`, `Secure` dès que la requête arrive en HTTPS. Un
bouton « Déconnexion » ferme la session ; dix échecs verrouillent le client cinq
minutes.

Un en-tête `Authorization: Basic` reste accepté pour les scripts et `curl`, mais
n'est plus jamais réclamé : le navigateur ne voit donc plus la fenêtre système.
La clé de signature est dérivée des identifiants, sauf si `APP_SESSION_SECRET`
est posée — sans elle, changer le mot de passe déconnecte partout.

### Sauvegarde hors site

`.powens_finance.db` est la seule copie au monde de l'historique des soldes :
Powens ne répond qu'au présent, et les copies quotidiennes du collecteur
(`.backups/`) vivent sur le même disque ou le même volume. La route
authentifiée `GET /sauvegarde.db` rend une copie cohérente (API de sauvegarde
en ligne de SQLite) ; `scripts/backup-prod.sh` la télécharge, vérifie son
intégrité et garde 90 jours dans `~/Backups/pypowens`. Pour l'automatiser sur
un Mac : `scripts/fr.jbartoli.powens-backup.plist` (launchd, 7 h 30), avec les
identifiants dans `~/.config/pypowens/backup.env`.

## Roadmap

- Webhooks payload models.
- Documents, transfers/payments.
- Budgets et objectifs par catégorie, à partir de l'historique local.

## Disclaimer

Unofficial wrapper, not affiliated with Powens. API reference:
<https://docs.powens.com/>.

## License

MIT © Jérémie Bartoli
