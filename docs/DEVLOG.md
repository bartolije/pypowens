# DEVLOG — Application finance Powens (sur `pypowens`)

Journal d'avancement pour reprise facile. **Aucune donnée personnelle ici** :
montants, soldes, IBAN, identifiants d'app, noms de fournisseurs réels et tokens
restent hors git (voir « Sécurité / secrets » en bas).

## Objectif

App web locale (FastAPI) par-dessus le wrapper `pypowens`, dans **ce même repo**
(dossier `app/`, hors wheel publié). Fonctions :
1. **Récap** patrimoine (comptes, soldes, connexions, état).
2. **Détecteur d'abonnements/prélèvements récurrents** (périodicité mensuel →
   biennal, libellé marchand, catégorie, montant, €/mois).
3. **Analyse des dépenses** (revenus/dépenses, catégories, récurrent vs ponctuel).

## Audit complet du 12/08/2026 — implémenté

Audit lib + app + tests (~60 findings), puis correction en 5 phases, une par
commit (`21c15a8` → `c38db6b`), 275 → 346 tests, couverture 84 → 88 %, mypy
étendu à `app/` :

0. **Données** : WAL + busy_timeout (collision app/collecteur = solde du jour
   perdu), backup quotidien avec rotation (`.backups/`), `.powens_state.json`
   atomique + fail-fast si corrompu (créait un utilisateur Powens en silence),
   logging partout.
1. **Chiffres faux** : `diff_percent` (fraction API) ×100 sur 3 pages,
   « TOUT » tronqué à 180 jours, TWR faussé par les jours à VL partielle
   (forward-fill), moyennes divisées par les mois couverts, `parse_amount`
   anglo-saxon (corruption ×1000), NaN, alertes d'abonnements corrompues par
   les vues filtrées, snapshots écrasés à chaque GET.
2. **Intégration** : plus de retry des POST non idempotents (double
   `create_user`), Retry-After plafonné + jitter, hôte des `_links.next`
   vérifié (fuite du bearer), `state` anti-CSRF sur le Webview + token échangé
   persisté, renouvellement du token dans le collecteur, yfinance en thread
   avec timeout (gelait l'event loop) et sorti des deps du wheel.
3. **Fluidité** : lru_cache sur les normalisations (3-4 exécutions/txn),
   médiane O(1) au clustering, single-flight sur le cache, fenêtre jamais
   rétrécie, `account_id` sur les endpoints transactions.
4. **Filet** : env de test isolé (le vrai .env fuyait), horloge figée
   (time-machine), `filterwarnings=error`, CI `uv sync --locked` + plancher de
   couverture + mypy app, TrustedHost + refus des POST cross-site + open
   redirect, police auto-hébergée, migrations `PRAGMA user_version`.
5. **Produit** : module `wealth.py` (recap/synthese fusionnés), onglet Passifs
   réel, alertes persistantes jusqu'à acquittement, requalification des flux
   depuis /performance, export CSV, nav mobile + `/recurrences` désorpheline,
   mots-clés courts en mot entier.

**Backlog restant de l'audit** : budgets par catégorie (la sidebar l'a promis
longtemps), webhooks Powens (remplacerait le polling du collecteur), endpoints
lib non couverts (transactionsclusters, pockets, loans, documents, DELETE
/users), accessibilité (tooltips SVG, focus-visible, aria), macros Jinja pour
les 3 sélecteurs de période, hypothesis sur les parsers, comparaison à un
indice par ISIN.

## Statut

| Étape | Sujet | État |
|---|---|---|
| 1 | Extensions lib (`get_indicators`, `list_categories`, `build_webview_url`) | ✅ |
| 2 | Socle app (config, state token, deps, main, scaffold) | ✅ |
| 4 | Récap patrimoine | ✅ |
| 5 | Détecteur récurrents (`/abonnements`) + vue brute par libellé (`/recurrences`) | ✅ |
| 6 | Analyse dépenses (`/analyse`) | ✅ |
| 7 | UI (Tabler, thème sombre, masquage des montants) | ✅ |
| 3 | Webview : `/connect` + `/callback` (erreurs, `connection_id`, échange de code) | ✅ |
| 8 | Persistance locale (historisation des soldes, overrides de catégorie, alertes) | ✅ |
| 9 | Robustesse : retries 429/5xx, renouvellement de token, pages d'erreur, cache unifié | ✅ |
| 10 | Investissements (lignes de titres) + synchronisation manuelle des connexions | ✅ |
| 12 | Import de relevés CSV (`/import`) + rattachement à un compte du connecteur | ✅ |
| 13 | Performance des supports (`/performance`) : TWR, MWR, collecte quotidienne | ✅ |
| 11 | Webhooks, budgets par catégorie | ⏳ |
| 14 | Comparaison à un indice / ETF (source de prix externe par ISIN) | ⏳ |

## Découvertes données RÉELLES (vérifiées en live sur une app sandbox)

**Ces points conditionnent l'implémentation :**

- Volume observé : plusieurs milliers de transactions sur ~8 ans d'historique,
  large majorité de débits.
- Répartition des `type`, du plus fréquent au plus rare : `card` ≫ `transfer` >
  `order` > `bank`, `payback`, `withdrawal`, `profit`, `market_*`, `deposit`,
  `check`, `arbitrage`, `unknown`.
- ⚠️ **`categories` VIDE sur 100 % des transactions** → catégorisation native
  Powens non alimentée sur cette app → **catégoriseur local (mots-clés) obligatoire**.
- ⚠️ **`counterparty` = null partout** → normalisation marchand basée sur `wording`.
- ⚠️ **`indicators` = null** (produit non calculé) → la feature analyse s'appuie sur
  les transactions, `get_indicators` en bonus si un jour dispo.
- Types de comptes rencontrés : `checking`, `csl`, `ldds`, `livret_a`, `market`,
  `pea`, `lifeinsurance`, `per` (→ table `TYPE_TO_FAMILY` dans `app/recap.py`).
- `currency` arrive en **objet** `{id:"EUR",...}` et non en chaîne → normalisé
  dans `Account.from_api`.

**Formats de `wording` (pour la normalisation marchand) :**
- Carte : `MARCHAND\VILLE\ FR` (ex `ENSEIGNE\LA-VILLE\ FR`) OU `MARCHAND CB*1234`
  (ex `ENSEIGNE CB*0000`). → clé marchand = 1er segment avant `\` ou ` CB*`.
- Prélèvement SEPA (`type=order`) : préfixe `PRLV SEPA` (dans `original`), puis
  un libellé émetteur suivi de références. Formes rencontrées :
  `<FOURNISSEUR ÉNERGIE> ... Numero de client : ...`,
  `<OPÉRATEUR TÉLÉCOM> ...`, `<ASSUREUR> ...CONTRAT... RUM ...`,
  `<COURTIER> ...`, `<SAS ...> Assurance de pret (Contrat n ...)`,
  `<GESTIONNAIRE>-<PRODUIT> ...`. → nettoyer : réfs longues de chiffres, `RUM`,
  `Réf`, `Contrat/CONTRAT`, `Numero`, `Fact`, `--NNNN--` ; garder les 1ers mots.
- Virements internes (`type=transfer`) entre comptes du même utilisateur :
  `EPGN -<libellé>`, `EPGN - <libellé>`, `Virement depuis COMPTE SUR LIVRET`,
  `Vir Epgn - <libellé>`. → **à EXCLURE** des dépenses et abonnements
  (détection par transaction miroir montant opposé/même date sur autre compte).

**Signaux récurrents utiles :** `type=order` corrèle fortement avec les
prélèvements récurrents (énergie, télécom, assurances). `type=card` contient aussi
des abonnements (à détecter par régularité). Fenêtre détecteur : 18-24 mois
glissants + n'afficher que les abos avec occurrence récente (≤ ~2 périodes).

## Import de relevés, et fusion avec un connecteur

`/import` charge un CSV de relevé pour les comptes qu'aucun connecteur ne remonte :
les opérations alimentent l'historique, l'analyse et la détection d'abonnements comme
celles de Powens (ids négatifs, empreinte par ligne, réimport idempotent).

Le jour où un connecteur se met à remonter ce compte, les deux sources se recouvrent.
Sans rien faire, la période commune est comptée **deux fois** dans les dépenses, et le
solde apparaît sur deux comptes — donc un « disponible » gonflé du montant du compte.

D'où le **rattachement** (`imported_account.powens_account_id`, posé depuis `/import`) :

- le compte importé cesse d'être un compte — il disparaît de la liste et des totaux,
  son solde étant désormais celui du compte Powens ;
- ses opérations sont servies **sous l'id du compte Powens**, sinon l'historique ancien
  tomberait hors des pages filtrées sur les comptes courants ;
- elles sont bornées à la **première date remontée par le connecteur** (calculée sur les
  opérations Powens, pas saisie) : au-delà, c'est un doublon ; en dessous, le relevé
  reste la seule source et doit être conservé ;
- les relevés de solde déjà pris pour le compte importé sont effacés, sans quoi la courbe
  de patrimoine garderait une bosse du montant du doublon.

Rattacher ne modifie aucune opération : c'est la lecture qui borne. Se tromper de compte
cible se corrige en changeant la cible, et détacher rend son autonomie au compte importé.

## Performance des supports — ce que l'API permet, et ce qu'elle ne permet pas

**Ce que Powens donne**, par ligne de titre : ISIN, quantité, prix de revient
(`unitprice`), VL du jour (`unitvalue`), valorisation, plus-value latente, poids dans le
portefeuille. Plus les flux, typés utilement (`market_order`, `profit`, `market_fee`,
`deposit`, `transfer`, `arbitrage`).

**L'historique de valorisation existe** — `GET users/{id}/investments/{id}/history`,
exposé par `list_investment_history()` — mais **il démarre à la création de la
connexion** : `min_date` peut resserrer la fenêtre, jamais l'élargir. Et
`accounts/{id}/balances` est un piège : il rejoue les flux connus depuis le solde
actuel, donc il affiche un solde figé sur toute la période antérieure à la première
transaction connue. Inutilisable pour un compte titres, dont la valeur bouge avec le
marché.

**Aucune donnée de marché** : ni indices, ni VL d'un support non détenu. La comparaison
à un benchmark demande une source externe interrogée par ISIN (étape 14).

### Les pièges rencontrés, tous vérifiés sur des données réelles

1. **Un achat de titres n'est pas une contre-performance.** Le compter comme un flux à
   retirer affichait −5,4 % sur un compte qui n'avait perdu que 1,1 %. Un achat convertit
   du cash en titres : il ne change pas ce que vaut le compte. D'où trois natures de flux
   (`external` / `trade` / `income`) au lieu d'un booléen.
2. **Un « Boost sur versement » arrive typé `deposit`** alors que c'est un cadeau de
   l'assureur, donc de la performance. Idem « Participation aux bénéfices » d'un fonds
   euros. Symétriquement, « VENTE COMPTANT » arrive typée `unknown` avec un montant
   positif : la prendre pour un revenu inventerait du gain. Le libellé départage, et un
   `flow_override` permet de trancher à la main.
3. **Une série qui ne couvre qu'une partie du contrat produit un chiffre crédible et
   faux** : un fonds euros affichait −0,40 % (capital garanti !) parce qu'une seule de ses
   deux poches publie une VL. D'où `series_coverage()` et un seuil de 95 %, en dessous
   duquel rien n'est publié.
4. **Les liquidités ne sont pas un trou dans la série.** Powens les présente comme une
   ligne (`XX-liquidity`) sans VL, ce qui faisait tomber un compte titres à 91 % de
   couverture et le rendait non publiable. Elles sortent du dénominateur.
5. **Annualiser un mois de marché donne des nombres spectaculaires et faux** (−45 %/an
   sur 26 jours) : le MWR n'est affiché qu'au-delà de 90 jours.

### Ce qui est périssable, et le collecteur

Les VL sont conservées par Powens depuis le branchement, mais les **soldes des comptes
sans lignes de titres** (fonds euros, PER, livret) ne vivent que dans nos snapshots : un
jour non collecté est perdu. `python -m app.collector` **rattrape depuis le dernier jour
archivé** (avec 3 jours de recouvrement, une VL de séance pouvant être corrigée), donc un
passage hebdomadaire reste viable. `scripts/install-collector.sh` installe un LaunchAgent
— launchd et non cron, parce qu'il rattrape au réveil si la machine dormait.

Ce rattrapage ne vaut que pour les VL : un solde manqué ne se récupère jamais, Powens ne
répondant qu'au présent. Un créneau quotidien unique s'est révélé trop juste (le 01/08/2026
est perdu, machine éteinte ce soir-là) ; l'agent tente donc **12 h 30, 19 h 30 et 22 h 30**,
plus un passage à l'ouverture de session (`RunAtLoad`). Les tentatives surnuméraires sont
gratuites : `record_snapshot` réécrit la ligne du jour, et la collecte ne lit que des
données déjà agrégées côté Powens — aucune synchro bancaire n'est forcée.

Pas d'hébergement distant : l'app n'a aucune authentification, et l'exposer voudrait dire
sortir le token Powens et tous les soldes de la machine.

## Latence : cache périmé servi, mémos, collecteur hors boucle (03/09/2026)

Mesure en local (app réelle + faux client, 12 comptes, 5 300 opérations,
22 000 soldes et 20 000 VL archivés — `pyp_perf.py` dans le scratch de la
session) : les treize routes GET totalisaient 173 ms p50 à cache chaud, la
première page 44 ms parse compris ; la lib parse 0,8 µs par transaction. Le code
Python n'était donc pas le problème : le temps perçu venait du **réseau**.

- **Où partaient les secondes** : à l'expiration d'un TTL (120 s comptes /
  connexions, 300 s historique), la requête suivante rechargeait EN LIGNE —
  deux secondes pour l'historique, toutes les cinq minutes, et le bandeau de
  santé (toutes les pages) rechargeait comptes + connexions toutes les deux
  minutes. Réponse : `data._cached` sert la valeur périmée et rafraîchit en
  tâche de fond (une seule par clé, verrou partagé) ; au-delà de
  `STALE_GRACE` (1 h) on redevient bloquant pour que Powens en panne se voie.
  `ttl <= 0` recharge toujours (test `test_expired_cache_never_shrinks_the_window`).
- **Collecteur dans le processus web** : `_collect_benchmark` (yfinance,
  synchrone, plusieurs secondes), `store.backup` et `notify` (webhook 10 s max)
  tournaient DANS la boucle d'événements toutes les 6 h → pages figées pendant
  ce temps. Passés en `asyncio.to_thread` (connexion SQLite partagée,
  `check_same_thread=False`, module sérialisé). Historiques de lignes demandés
  quatre par quatre (sémaphore) au lieu d'un par un.
- **Mémos** : `_per_account_series` (85 % du rendu de `/` et `/patrimoine`,
  appelée deux fois par page) et `investment_values` mémorisées par état de
  table (`COUNT(*)`, `MAX(rowid)` — `INSERT OR REPLACE` renouvelle le rowid ;
  `remap_account` invalide explicitement ; la connexion et le fichier font
  partie de la clé car les tests ouvrent une base par cas). `data.derived`
  mémorise la détection récurrente par génération du cache (incrémentée à
  chaque `_set` et `clear_cache`).
- **HTTP** : gzip, `Cache-Control: immutable` sur `/static/*?v=`, `no-store`
  sur les pages, `nosniff`/`X-Frame-Options`/`Referrer-Policy`, `/docs`
  fermé.
- Résultat local : 173 → 59 ms pour les treize routes ; `/` 33 → 5,
  `/patrimoine` 35 → 8, `/performance` 15 → 5, `/abonnements` 12 → 3,
  `/analyse` 17 → 10, `/comptes` 13 → 8 ms. Non mesurable ici : la disparition
  des attentes réseau en croisière, qui est le vrai gain.

Piège rencontré : lire TOUTES les VL en une fois (au lieu d'une lecture par
compte via l'index) a d'abord RALENTI `/performance` (15 → 25 ms) — la
conversion date + Decimal de lignes hors périmètre coûtait plus que les
lectures indexées évitées. La bonne version restreint aux comptes porteurs
(`account_ids`) ET mémorise.

## Endpoints confirmés live

- `GET /users/me` ✅ · `GET /users/me/connections` ✅ · `GET /users/me/accounts` ✅
- `GET /users/me/transactions` ✅ (pagination `_links.next`)
- `GET /users/me/indicators` → 200 mais `indicators:null`
- `GET /banks/categories` → 200 (`bank_category:[{id,name}]`) ; `GET /categories` → 404

## Infra / API

Tout ce qui suit est **propre à chaque application** et se lit dans la console
Powens (<https://console.powens.com/>) — rien n'est recopié ici :

- API URL : `https://<votre-domaine>.biapi.pro/2.0/` (→ `POWENS_DOMAIN`).
- IPs inbound/outbound à allowlister (webhooks/prod) : voir la console.
- Clé publique de chiffrement (RSA JWK) : voir la console. Publique, utile pour
  chiffrer des données sensibles (transferts/paiements). **Non requise** pour
  l'agrégation en lecture.

## Authentification durcie (04/09/2026)

Six points, portés de l'audit mené sur `suivi-location` puis vérifiés ici en
lançant l'app en local (parcours `curl` complet, contournements compris). À
connaître avant de toucher à `app/auth.py` :

1. **Le frein se contournait par un en-tête.** `X-Forwarded-For` est une liste à
   laquelle chaque relais ajoute une entrée à DROITE : la première est celle du
   client, donc forgeable. Lire `CF-Connecting-IP` d'abord (Cloudflare l'écrase),
   sinon la dernière entrée du XFF, sinon la socket.
2. **Second frein par compte** (40 / 30 min) : une attaque répartie sur mille
   adresses ne déclenche jamais le frein par adresse.
3. **Le cookie porte une génération** (`app_meta.session_epoch`) : la
   déconnexion l'incrémente, ce qui invalide tout cookie antérieur. Sans cela,
   se déconnecter n'était qu'un oubli côté navigateur. Session : 24 h.
4. **Le mot de passe est aussi la graine de signature** (à défaut de
   `APP_SESSION_SECRET`) : devinable, il permet de FORGER un cookie, donc
   d'entrer sans jamais voir le second facteur. D'où un plancher au démarrage,
   et `setup_mfa.py` qui pose une `APP_SESSION_SECRET` dédiée pour découpler.
5. **Le MFA aurait été décoratif sans fermer le Basic** : le second facteur n'est
   demandé qu'au formulaire, donc `curl -u user:motdepasse` l'aurait contourné.
   D'où `APP_API_TOKEN`, la seule porte à un facteur qui subsiste — pour les
   scripts, qui ne peuvent pas produire de code.
6. **Anti-CSRF** : sur toute méthode mutante, AVANT l'authentification (sinon un
   refus cross-site ressort en 303 vers la page de connexion, indistinguable
   d'une visite), et sur `Origin` seul (`Referer` est légitimement absent).

Piège de mise en service : activer le MFA sans poser `APP_API_TOKEN` casse
`scripts/backup-prod.sh` (401 explicite). Le démarrage l'annonce en `WARNING`.

## Sécurité / secrets

- `.env` (dont `POWENS_ACCESS_TOKEN`), `.powens_state.json` et
  `categories.local.json` → **gitignorés**, jamais poussés.
- Repo public → ne jamais commiter : token, `client_id`/`client_secret`, domaine
  d'app, IBAN, soldes, montants réels, noms de fournisseurs/marchands personnels.
  Les règles de catégorisation propres à ses propres relevés vont dans
  `categories.local.json` (voir `categories.local.example.json`).
- Token sandbox à régénérer côté console après les tests si besoin.

## Lancer / reprendre

```bash
cd ~/Development/pypowens
source .venv/bin/activate
uv pip install -e ".[app,dev]"     # deps app (fastapi/uvicorn/jinja2/dotenv)
pytest -q && ruff check .          # vérifs sans réseau
python -m app                       # lance l'app sur http://127.0.0.1:8000
```

`.env` doit contenir `POWENS_DOMAIN`, `POWENS_CLIENT_ID`, `POWENS_CLIENT_SECRET`
et (optionnel) `POWENS_ACCESS_TOKEN` — voir `.env.example`.
