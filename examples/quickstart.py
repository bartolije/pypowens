"""Minimal end-to-end example for pypowens.

Set the following environment variables (see .env.example) before running:

    POWENS_DOMAIN=myapp-sandbox
    POWENS_CLIENT_ID=...
    POWENS_CLIENT_SECRET=...
    # optional, to reuse an existing user token instead of creating a new user:
    POWENS_ACCESS_TOKEN=...

Run with:  python examples/quickstart.py
"""

from __future__ import annotations

import asyncio

from pypowens import PowensClient


async def main() -> None:
    async with PowensClient.from_env() as powens:
        # 1. Make sure we have a token. If none is provided, create a fresh user.
        if powens.access_token is None:
            token = await powens.create_user()
            print(f"Created user #{token.id_user}, token acquired.")

        # 2. List some connectors (banks) available in France.
        connectors = await powens.list_connectors(country_codes="fr")
        print(f"\n{len(connectors)} connectors available. First few:")
        for connector in connectors[:5]:
            print(f"  - {connector.id:>5}  {connector.name}")

        # 3. Show the user's existing connections and accounts.
        connections = await powens.list_connections()
        print(f"\n{len(connections)} connection(s).")

        accounts = await powens.list_accounts()
        print(f"{len(accounts.accounts)} account(s). Balances by currency: {accounts.balances}")

        # 4. Stream the 10 most recent transactions.
        print("\nRecent transactions:")
        txns = await powens.list_transactions(max_transactions=10)
        for txn in txns:
            print(f"  {txn.date}  {str(txn.value):>10}  {txn.wording}")


if __name__ == "__main__":
    asyncio.run(main())
