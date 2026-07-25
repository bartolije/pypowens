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

429, 5xx and network errors are retried with exponential backoff (3 attempts by
default, `Retry-After` honoured). Configure with `PowensClient(..., max_retries=…,
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
dans le dossier `app/` (non incluse dans le wheel publié) :

- **Récap** (`/`) — patrimoine net, **courbe d'évolution** (historisée en local),
  comptes groupés par famille, lignes de titres détenues, comptes en devises
  étrangères listés à part, état des connexions + bouton de synchronisation.
- **Récurrences** (`/recurrences`) — vue brute par libellé normalisé : nombre
  d'occurrences, total, moyenne, intervalle moyen.
- **Abonnements** (`/abonnements`) — détection des prélèvements/paiements récurrents
  avec périodicité (mensuel → biennal), montant, **€/mois**, et **alertes** sur les
  nouveaux abonnements et les hausses de prix depuis la dernière visite.
- **Analyse** (`/analyse`) — revenus vs dépenses par mois, répartition par catégorie,
  ventilation exacte récurrent / ponctuel. Toutes les figures portent sur la même
  fenêtre (12 mois complets), affichée en tête de page.
- **Détail** (`/transactions?label=…`) — les opérations derrière un libellé, leur
  évolution mensuelle, et la correction de catégorie (mémorisée localement).

```bash
uv pip install -e ".[app]"     # dépendances de l'app
# .env : POWENS_DOMAIN, POWENS_CLIENT_ID/SECRET, POWENS_ACCESS_TOKEN
python -m app                  # http://127.0.0.1:8000
```

Le token est résolu par priorité : `POWENS_ACCESS_TOKEN` (.env) → `.powens_state.json`
→ `create_user()`. En cas de 401, l'app tente **un** renouvellement automatique
(`renew_token`) avant d'afficher une page d'aide.
Pour connecter une banque via le Webview Powens, whitelister le
`redirect_uri` `http://127.0.0.1:8000/callback` dans la console Powens.

### Données locales (jamais versionnées)

| Fichier | Contenu |
|---|---|
| `.env` | identifiants d'app et token |
| `.powens_state.json` | `id_user` + token persistés |
| `.powens_finance.db` | SQLite : historique des soldes, catégories forcées, état des séries |
| `categories.local.json` | règles de catégorisation propres à vos relevés (voir l'exemple) |

L'app **n'a aucune authentification** : elle refuse de démarrer sur une interface
autre que loopback sans `APP_ALLOW_REMOTE=1`. Voir `.env.example` pour les réglages
(`APP_HISTORY_MONTHS`, `APP_BASE_CURRENCY`, `APP_DB_PATH`…).

> La catégorisation native Powens et le produit *indicators* n'étant pas
> alimentés sur toutes les apps, la catégorisation est faite localement
> (mots-clés + overrides) et l'analyse repose sur les transactions.

## Roadmap

- Webhooks payload models.
- Documents, transfers/payments.
- Budgets et objectifs par catégorie, à partir de l'historique local.

## Disclaimer

Unofficial wrapper, not affiliated with Powens. API reference:
<https://docs.powens.com/>.

## License

MIT © Jérémie Bartoli
