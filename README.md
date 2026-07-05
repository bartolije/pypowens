# pypowens

An **async Python wrapper** for the [Powens](https://www.powens.com/) (ex‑Budget Insight)
bank data aggregation API. Built on [`httpx`](https://www.python-httpx.org/), fully typed,
zero heavy dependencies.

> ⚠️ First release (`0.1.0`, alpha). Covers the **core aggregation** surface:
> OAuth2 auth, connectors, connections, accounts and transactions.

## Features

- 🔑 OAuth2 flows: create a user + permanent token, renew, temporary code, code exchange, revoke.
- 🏦 List connectors (banks/providers).
- 🔗 List / delete user connections (with `connector` and `accounts` expansion).
- 💳 List accounts with per-currency balances.
- 📜 List transactions with automatic **pagination** (follows `_links.next`) and filters.
- 🧱 Forgiving models — every object keeps its raw payload in `.raw`.
- ✅ Typed (`py.typed`), async context manager, tested with `respx`.

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
| `list_accounts()` | `GET /users/{id}/accounts` |
| `get_account(id)` | `GET /users/{id}/accounts/{aid}` |
| `list_transactions()` / `iter_transactions()` | `GET /users/{id}/transactions` |

All authenticated calls send `Authorization: Bearer <access_token>`. The token is
set automatically after `create_user()` / `renew_token()` / `exchange_code()`, or you
can pass `access_token=...` to the constructor.

### Streaming transactions

`iter_transactions()` is an async generator that transparently follows pagination:

```python
async for txn in powens.iter_transactions(min_date="2026-01-01", income=True):
    ...
```

### Error handling

```python
from pypowens import PowensAPIError, PowensAuthError, PowensRateLimitError

try:
    await powens.get_current_user()
except PowensAuthError:      # 401 / 403
    ...
except PowensRateLimitError: # 429
    ...
except PowensAPIError as e:  # any other non-2xx
    print(e.status_code, e.code, e.message)
```

## Development

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
pytest          # run tests (respx-mocked, no network)
ruff check .    # lint
```

## Roadmap

- Webview / connect flow helpers (temporary code → hosted URL → redirect handling).
- Categories, investments, documents, transfers/payments.
- Webhooks payload models.

## Disclaimer

Unofficial wrapper, not affiliated with Powens. API reference:
<https://docs.powens.com/>.

## License

MIT © Jérémie Bartoli
