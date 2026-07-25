# Changelog

All notable changes to this project. Format loosely based on
[Keep a Changelog](https://keepachangelog.com/); versions follow the library
(`src/pypowens`), the bundled app being versionless.

## [0.2.0] — unreleased

### Library (`pypowens`)

#### Added
- `list_investments()` — security lines held in market/PEA/life-insurance accounts
  (`GET /users/{id}/investments`), with the new `Investment` model.
- `update_connection()` — force a connection refresh instead of waiting for the
  next automatic sync (`PUT /users/{id}/connections/{id}`).
- Automatic retries with exponential backoff on 429 and 5xx responses and on
  network errors, honouring `Retry-After`. Tunable via `max_retries` / `backoff`
  (set `max_retries=0` to opt out).
- `PowensAPIError.retry_after` exposes the header value in seconds.

#### Fixed
- Pagination could loop forever if the API echoed the same `_links.next` href;
  repeated hrefs and a `MAX_PAGES` ceiling now stop it.
- `Transaction.date` / `Investment.vdate` annotations shadowed the `date` type,
  breaking type introspection. The library now passes `mypy --strict`.

### Application

#### Added
- **Local store** (SQLite, gitignored): daily balance snapshots, category
  overrides and recurring-series state.
- **Net-worth curve** on the recap, computed from recorded snapshots — the
  variation badge now compares against a real previous reading.
- **Drill-down** page (`/transactions?label=…`): every operation behind a merchant
  key, its monthly totals, and the form to override its category.
- **Change alerts** on `/abonnements`: newly appeared series and price increases
  (with the previous amount), based on the stored series state.
- **Investment lines** on the recap, with unrealized gain/loss.
- **Per-connection "Synchroniser"** button.
- Error pages for expired tokens (with a single automatic renewal + replay),
  rate limiting, and upstream API failures — these used to be raw 500s.
- Guard refusing to bind a non-loopback `APP_HOST` without `APP_ALLOW_REMOTE=1`,
  since the app has no authentication.
- `APP_BASE_CURRENCY`, `APP_DB_PATH` settings.

#### Fixed
- **Net worth summed accounts across currencies.** Only accounts in
  `APP_BASE_CURRENCY` are totalled; others are listed separately and excluded.
- **`/analyse` mixed two windows**: tiles covered 12 months while the category
  breakdown and the recurring split covered the whole history. Everything now
  uses the same window, stated on the page.
- **Recurring vs one-off** was an estimate (`average − sum of €/month`) clamped at
  zero. It is now an exact split of observed spending, via the transaction ids of
  each detected series; the contractual commitment is shown as its own figure.
- Forecast (`coming`) transactions were counted as real spending.
- The transaction history was re-downloaded once per requested window and never
  evicted; a single history is now fetched for the widest window and filtered in
  memory.
- The Webview callback ignored its return parameters — a failed or abandoned
  connection looked like a success. Errors are reported and an authorization
  `code` is exchanged.
- "Mask amounts" left every figure inside the SVG charts (bar labels, donut
  legend and centre, hover tooltips) readable.

#### Changed
- Default history window 24 → 36 months, so yearly and biennial series have
  enough occurrences to be detected.
- Tabler is vendored locally instead of loaded from a CDN; its unused JS bundle
  and icon font are no longer requested.
- Personal merchant names left the versioned rules: they belong in
  `categories.local.json` (gitignored, see `categories.local.example.json`).
- Test suite: 25 → 99 tests, now covering routes, the store, enrichment, label
  grouping and robustness. CI runs lint, `mypy --strict` and tests on 3.11–3.13.

## [0.1.0] — 2026-07-05

First release: OAuth2 auth, connectors, connections, accounts, transactions with
pagination, plus the local FastAPI app (recap, subscriptions, analysis).
