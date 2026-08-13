# Changelog

All notable changes to this project. Format loosely based on
[Keep a Changelog](https://keepachangelog.com/); versions follow the library
(`src/pypowens`), the bundled app being versionless.

## [0.2.0] — unreleased

### Déploiement du 13/08/2026 — l'app peut quitter le poste de travail

Voir [docs/DEPLOIEMENT.md](docs/DEPLOIEMENT.md). Rien ne change en local : toutes
les nouveautés sont inertes tant que leur variable n'est pas posée.

#### Added
- **Persistance sur volume** : `.powens_finance.db` et `.powens_state.json`
  suivent `APP_DATA_DIR`, sinon `RAILWAY_VOLUME_MOUNT_PATH` (injectée dès qu'un
  volume est attaché), sinon la racine du dépôt. Sans cela un redéploiement
  perdait l'historique des soldes *et* le token — ce dernier faisant créer un
  nouvel utilisateur Powens, donc un compte sans aucune banque connectée.
- **Authentification HTTP Basic** (`APP_AUTH_USER`/`APP_AUTH_PASSWORD`, module
  `app/auth.py`) : comparaison à temps constant, et verrouillage progressif d'un
  client après 10 échecs, Basic n'opposant rien à la force brute. `/health`,
  sondé par l'hébergeur, y échappe.
- **Collecte planifiée dans le processus web** (`APP_COLLECT_EVERY_HOURS`,
  `collector.scheduled`) : un volume ne se montant que sur un service, un « cron
  job » voisin n'aurait pas vu la base.
- **Notification par webhook** (`APP_NOTIFY_URL`, `APP_NOTIFY_TOKEN`) : POST JSON
  `{title, message}`, pour Home Assistant, Gotify ou ntfy. La notification macOS
  ne prévenait personne sur un serveur — or une connexion tombée non vue coûte
  autant de jours de soldes.
- `Dockerfile`, `.dockerignore` et `railway.json` ; `PORT` est lu quand
  `APP_PORT` est absent, comme l'imposent la plupart des hébergeurs.

#### Fixed
- **`redirect_uri` suit le domaine public** (`APP_PUBLIC_URL`, sinon
  `RAILWAY_PUBLIC_DOMAIN`) : il était bâti sur `host:port`, soit `0.0.0.0` dans
  un conteneur — une adresse de retour inutilisable pour la banque.
- **Le contrôle CSRF ne dépend plus de `APP_ALLOW_REMOTE`** : il se désactivait
  précisément là où il devenait nécessaire. Le navigateur rejouant tout seul les
  identifiants Basic sur une requête cross-site, l'authentification ne l'a jamais
  remplacé. L'origine est désormais comparée à celle de la page.
- `python -m app` n'ouvre plus de navigateur quand il écoute au-delà de la
  loopback.

### Audit du 12/08/2026 (résumé — le détail est dans les six commits d'implémentation)

#### Library — breaking-ish
- **Les retries ne rejouent plus les POST non idempotents** : un timeout sur
  `POST /auth/init` pouvait créer des utilisateurs Powens en double
  (facturables) ; un rejeu de `/auth/token/access` brûle un code à usage
  unique. Un 429 reste rejouable partout ; 5xx/réseau seulement sur
  GET/HEAD/PUT/DELETE. `Retry-After` est plafonné à 60 s, avec ±20 % de jitter.
- **La pagination refuse un `_links.next` hors de l'hôte de l'API** (le bearer
  permanent y serait attaché) et journalise ses troncatures (`MAX_PAGES`).
- `yfinance` sort des dépendances du wheel (extra `[app]`) : la lib ne dépend
  que de `httpx`.

#### Library — fixed/added
- `_parse_decimal` rejette `NaN`/`Infinity` (un seul NaN rendait toute somme
  NaN, silencieusement). `AuthToken.access_token` est masqué du `repr`.
- `build_webview_url` : `extra` ne peut plus écraser `client_id`/`redirect_uri`/
  `code` ; `domain` dérivé via `urlsplit`.
- `iter_transactions`/`list_transactions` acceptent `account_id` (endpoint par
  compte) ; `max_transactions` plafonne la taille de page demandée.
- Logger `pypowens` (retries, pagination, création d'utilisateur).

#### Application (résumé)
- Données : WAL + busy_timeout, backup quotidien (`.backups/`), état token
  atomique et fail-fast, logging. Chiffres corrigés : `diff_percent` ×100,
  fenêtres d'historique, TWR à VL partielles, moyennes par mois couverts,
  `parse_amount` anglo-saxon. Webview : jeton `state` anti-CSRF + persistance
  du token échangé. Sécurité locale : TrustedHost, refus des POST cross-site,
  police auto-hébergée. Produit : onglet Passifs, alertes d'abonnements
  persistantes avec acquittement, requalification des flux de performance,
  export CSV, navigation mobile, mots-clés de catégorisation en mot entier.

### Library (`pypowens`)

#### Added
- `list_investments()` — security lines held in market/PEA/life-insurance accounts
  (`GET /users/{id}/investments`), with the new `Investment` model.
- `get_client_config()` — the application's own configuration (`GET /clients/{id}`),
  with the new `ClientConfig` model. Its `redirect_uris` is the only way to know why
  the Webview refuses a callback: it reports a mismatch without naming what it wanted.
- `update_connection()` — force a connection refresh instead of waiting for the
  next automatic sync (`PUT /users/{id}/connections/{id}`).
- `list_investment_history()` — dated unit values of a security line
  (`GET /users/{id}/investments/{id}/history`), with the new `InvestmentValue` model.
  The only price history the API exposes, and it only covers the period since the
  connection was created: `min_date` narrows the window, never widens it. Returned
  oldest-first, because a price series is read in the direction of time.
- Automatic retries with exponential backoff on 429 and 5xx responses and on
  network errors, honouring `Retry-After`. Tunable via `max_retries` / `backoff`
  (set `max_retries=0` to opt out).
- `PowensAPIError.retry_after` exposes the header value in seconds.

#### Fixed
- **`build_webview_url()` produced an unusable URL.** The Webview lives at
  `/{lang}/connect`; the built URL omitted the language segment, and
  `webview.powens.com/connect` is answered by a CloudFront 503 (its viewer function
  cannot parse the path). The whole connect flow was therefore unreachable, failing
  in a way that looks like a Powens outage. `lang` is now a parameter (default
  `"en"`), and an empty value falls back rather than emitting a broken path.
- `build_webview_url()` gained `connector_ids` plumbing from the app, so a second,
  already-known bank can be opened directly instead of scrolling the full list, plus
  `flow` / `connection_id` for the Webview's `reconnect` screen.
- Pagination could loop forever if the API echoed the same `_links.next` href;
  repeated hrefs and a `MAX_PAGES` ceiling now stop it.
- `Transaction.date` / `Investment.vdate` annotations shadowed the `date` type,
  breaking type introspection. The library now passes `mypy --strict`.

### Application

#### Added
- **Readable-at-work defaults.** Amounts are **masked by default** (only an opt-out
  is persisted, applied before first paint so nothing flashes on screen), and net
  worth is no longer the landing page: `/patrimoine` is the last tab.
- **New default page** `/` — the current accounts (checking/card only, base currency
  totalled) followed by the **spending history by date**: one block per day with its
  own total, a type badge per line, and month / type / debits-or-all filters. Internal
  transfers are listed but never counted. Falls back to the last month with operations
  so the page is never empty on the 1st.
- **Subscriptions grouped by expense type**, with a €/month and €/year subtotal per
  type, the price history of each series (drift since the first charge + trend line),
  the payment rail (SEPA mandate vs card) and an "en sommeil" badge for series that
  stopped being debited — those stop counting towards the monthly commitment.
- `is_subscription()` / `detect_subscriptions()` — a strict pass over
  `detect_recurring()`. On real statements the permissive detector reported 82
  "subscriptions", mostly supermarket and fuel runs that cluster by amount; the strict
  pass keeps 18 actual contracts. A SEPA mandate qualifies on its own; a card series
  must charge a near-identical amount, and a two-occurrence annual one must fall on its
  anniversary. Everyday-spending types are excluded outright.
- **Expense-type taxonomy** rebuilt for optimisation decisions: energy, bank fees,
  fuel, car and motorbike are now separate types, and cash withdrawals get their own
  (their wording is the dispenser's address, never the word "retrait"). Supermarket
  fuel pumps (`DAC`) are counted as fuel, not groceries.
- **CSV statement import** (`/import`) — a missing or broken connector no longer means
  a missing account. Parses `;`/`,` files with either a Debit/Credit pair or a signed
  Montant column, in UTF-8/CP1252/Latin-1, infers the operation type from the statement
  prefix (`CARTE`, `PRLV`, `ECH`, `RET DAB`…), and merges the rows into every aggregate.
  Rows carry a `(account, date, amount, wording, same-day rank)` fingerprint so
  overlapping exports never duplicate — while genuinely identical same-day operations
  stay two. Imported ids are negative, keeping the positive space to Powens: the two
  sets meet inside exclusion sets (internal transfers, series membership).
  On a real statement this multiplied the tracked monthly commitment sevenfold — the
  mortgage instalment alone being the single largest line, and it was simply missing.
- **Performance page** (`/performance`) — what each investment account actually returned,
  over 1 month to 5 years or since the archive begins. Reports **TWR** (neutralises
  deposits, the only figure comparable to an index) and **MWR** (an XIRR: what *your*
  money earned, given when you paid it in), per account, alongside every holding with its
  cost basis, unit price, unrealized gain and portfolio weight.
  Three lessons the real data taught, each one a wrong-but-plausible figure avoided:
  a **share purchase is not a loss** (counting it as a flow showed −5.4 % on an account
  down 1.1 % — it merely turns cash into securities, hence three flow natures rather than
  a boolean); a **"boost sur versement" is a gift from the insurer, not a deposit**, while
  a sale arrives typed `unknown` with a positive amount, so the wording decides and a
  manual override settles the rest; and a **series covering half a contract yields a
  credible lie** — a capital-guaranteed euro fund showed −0.40 % because only one of its
  two pockets publishes a unit value, so nothing is published below 95 % coverage.
  Cash pockets (`XX-liquidity`) are excluded from that coverage instead of counted as
  gaps, and MWR is withheld under 90 days: annualising a month of market is theatre.
- **Daily collector** (`python -m app.collector`, plus a `launchd` agent via
  `scripts/install-collector.sh`) — archives balance snapshots and unit values.
  It **resumes from the last archived day** rather than assuming it runs daily, so a
  weekly run stays viable and a missed week fills itself in. launchd rather than cron:
  it catches up on wake instead of skipping the slot on a sleeping machine.
- **Statement-to-connector merge.** An imported statement can be *linked* to the Powens
  account a connector eventually started to cover (`imported_account.powens_account_id`,
  set from `/import`). Until now both sources coexisted: the overlapping period counted
  twice in every aggregate, and the balance showed up on two accounts — inflating the
  available total by the account's own balance. A linked statement stops being an account
  of its own, its rows are served under the Powens account id (otherwise the old history
  would fall outside the pages filtered on current accounts), and they are capped at the
  first date the connector actually reports — computed from the Powens operations
  themselves, never entered by hand. Balance snapshots taken while the imported account
  counted for itself are dropped, so the net-worth curve keeps no bump. Linking mutates
  no operation: picking the wrong target is fixed by changing it, and unlinking gives the
  imported account its autonomy back.
- `loan_repayment` counts as a subscription rail: a mortgage instalment is a fixed
  contractual monthly commitment, usually the largest line of the list.
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
- `/connect` preflights its `redirect_uri` against the whitelist and, when absent,
  renders the value to declare and the ones currently declared instead of dead-ending
  on a Powens error page. It fails open, so a diagnostic never blocks the flow.
- `/connect?connector_id=…` opens the Webview straight on one bank.
- `/reconnecter/{id}` sends the user back through the Webview to finish a connection
  stuck in `webauthRequired` (the bank is waiting on *them*). Such connections now
  offer that instead of "Synchroniser", which could never clear the state.
- Error pages for expired tokens (with a single automatic renewal + replay),
  rate limiting, and upstream API failures — these used to be raw 500s.
- Guard refusing to bind a non-loopback `APP_HOST` without `APP_ALLOW_REMOTE=1`,
  since the app has no authentication.
- `APP_BASE_CURRENCY`, `APP_DB_PATH` settings.

#### Fixed
- **Net worth summed accounts across currencies.** Only accounts in
  `APP_BASE_CURRENCY` are totalled; others are listed separately and excluded.
- **Debt was charted as a positive share of wealth.** Connecting a mortgage put a
  −256 k€ loan into the repartition donut, which takes absolute values, as a "25 %
  slice" — and gave it a negative allocation bar, rendered zero-width. The donut now
  covers assets only, with total assets and total debt stated next to the (unchanged,
  correctly netted) net worth.
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
  `code` is exchanged. Parameters are now read off the query string instead of being
  declared as typed arguments: Powens varies what it sends, and a declared
  `connection_id: int` turned an empty `?connection_id=` into a 422 that reads as
  "the redirect is broken". A return carrying neither an id, a code nor an error now
  renders a diagnostic page listing what did arrive, and naming the `redirect_uri`
  to whitelist in the console.
- "Mask amounts" left every figure inside the SVG charts (bar labels, donut
  legend and centre, hover tooltips) readable — and, since tooltips quoting an amount
  live in a `title` attribute that CSS cannot blur, those now only become real tooltips
  once amounts are revealed.
- **`CARTE 22/07 MERCHANT` wordings grouped by date, not by merchant.** Statement exports
  label card payments that way (Powens strips it), so the merchant key came out as
  "CARTE 22 07" and every card payment of the same day merged into one series.
- **Variable-amount mandates were listed several times.** Amount clustering splits a toll
  or usage-based account into thin series, and a two-point series scores maximum
  confidence (a single interval has zero variance), so one contract appeared four times.
  Two-occurrence offcuts are dropped when the same merchant also yields a longer series —
  a genuine second contract with the same biller (two energy meters) is itself long.

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
