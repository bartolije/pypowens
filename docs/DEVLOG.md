# DEVLOG — Application finance Powens (sur `pypowens`)

Journal d'avancement pour reprise facile. Aucun secret ici (tokens/IBAN/soldes
restent hors git). Plan complet : `~/.claude/plans/indexed-exploring-snowflake.md`.

## Objectif

App web locale (FastAPI) par-dessus le wrapper `pypowens`, dans **ce même repo**
(dossier `app/`, hors wheel publié). Fonctions :
1. **Récap** patrimoine (comptes, soldes, connexions, état).
2. **Détecteur d'abonnements/prélèvements récurrents** (périodicité mensuel →
   biennal, libellé marchand, catégorie, montant, €/mois).
3. **Analyse des dépenses** (revenus/dépenses, catégories, récurrent vs ponctuel).

## Statut

| Étape | Sujet | État |
|---|---|---|
| 1 | Extensions lib (`get_indicators`, `list_categories`, `build_webview_url`) | ✅ fait (commit `1b9e6c2`) |
| 2 | Socle app (config, state token, deps, main, scaffold) | 🚧 en cours |
| 4 | Récap patrimoine | ⏳ |
| 5 | Détecteur récurrents | ⏳ |
| 6 | Analyse dépenses | ⏳ |
| 3+7 | Webview (non bloquant), UI/doc | ⏳ |

## Découvertes données RÉELLES (sandbox jbartoli, user 5, BoursoBank)

**Vérifié en live — ces points conditionnent l'implémentation :**

- **6 579 transactions**, historique **2018-08 → 2026-07** (~8 ans). 5 478 débits.
- Répartition `type` : `card`=4079, `transfer`=1578, `order`=597, puis bank,
  payback, withdrawal, profit, market_*, deposit, check, arbitrage, unknown.
- ⚠️ **`categories` VIDE sur 100 % des transactions** → catégorisation native
  Powens non alimentée ici → **catégoriseur local (mots-clés) obligatoire**.
- ⚠️ **`counterparty` = null partout** → normalisation marchand basée sur `wording`.
- ⚠️ **`indicators` = null** (produit non calculé) → feature analyse s'appuie sur
  les transactions, `get_indicators` en bonus si un jour dispo.
- **12 comptes**, patrimoine ~649 k€ : checking×2, csl×2, ldds, livret_a, market,
  pea×2, lifeinsurance×2, per. `currency` arrive en **objet** `{id:"EUR",...}`.

**Formats de `wording` (pour la normalisation marchand) :**
- Carte : `MARCHAND\VILLE\ FR` (ex `CARREFOUR\PELUSSIN\ FR`) OU `MARCHAND CB*1234`
  (ex `DELIVEROO CB*8409`). → clé marchand = 1er segment avant `\` ou ` CB*`.
- Prélèvement SEPA (`type=order`) : préfixe `PRLV SEPA` (dans `original`),
  ex `EDF clients particuliers ... Numero de client : ...`,
  `BOUYGUES TELECOM ...`, `AXA ...CONTRAT... RUM ...`, `Kereis France ...`,
  `SATEC REG ...`, `SAS MULTI IMPACT Assurance de pret (Contrat n ...)`,
  `AM GESTION-APRIL MOTO ...`. → nettoyer : réfs longues de chiffres, `RUM`,
  `Réf`, `Contrat/CONTRAT`, `Numero`, `Fact`, `--NNNN--` ; garder les 1ers mots.
- Virements internes (`type=transfer`) entre ses propres comptes :
  `EPGN -Voiture`, `EPGN - Livret`, `Virement depuis COMPTE SUR LIVRET`,
  `Vir Epgn - Livret Bourso+`. → **à EXCLURE** des dépenses et abonnements
  (détection par transaction miroir montant opposé/même date sur autre compte).

**Signaux récurrents utiles :** `type=order` corrèle fortement avec les
prélèvements récurrents (EDF, télécom, assurances). `type=card` contient aussi
des abonnements (à détecter par régularité). Fenêtre détecteur : 18-24 mois
glissants + n'afficher que les abos avec occurrence récente (≤ ~2 périodes).

## Endpoints confirmés live

- `GET /users/me` ✅ · `GET /users/me/connections` ✅ · `GET /users/me/accounts` ✅
- `GET /users/me/transactions` ✅ (pagination `_links.next`)
- `GET /users/me/indicators` → 200 mais `indicators:null`
- `GET /banks/categories` → 200 (`bank_category:[{id,name}]`) ; `GET /categories` → 404

## Infra / API (fournis par la console Powens)

- API URL : `https://jbartoli-sandbox.biapi.pro/2.0/`
- IPs inbound/outbound (allowlisting webhooks/prod) : 13.37.70.131, 13.38.157.67,
  15.188.101.71, 13.39.29.243, 15.188.68.198, 13.39.95.239.
- Clé publique de chiffrement (RSA JWK, kid `K4zTutSx0hOAnoiCZ2GlzhdHiWJyp83H-LAochBgNdk`)
  — publique, pour chiffrer données sensibles (transferts/paiements). Non requise
  pour l'agrégation lecture. (Stockée côté console, pas dans git.)

## Sécurité / secrets

- `.env` (dont `POWENS_ACCESS_TOKEN`) et `.powens_state.json` → **gitignorés**,
  jamais poussés. Repo public → ne jamais commiter token/IBAN/soldes.
- Token sandbox à régénérer côté console après les tests si besoin.

## Lancer / reprendre

```bash
cd ~/Development/pypowens
source .venv/bin/activate
uv pip install -e ".[app,dev]"     # deps app (fastapi/uvicorn/jinja2/dotenv)
pytest -q && ruff check .          # vérifs sans réseau
python -m app                       # lance l'app sur http://127.0.0.1:8000
```

`.env` doit contenir `POWENS_DOMAIN=jbartoli-sandbox`, `POWENS_CLIENT_ID`,
`POWENS_CLIENT_SECRET`, `POWENS_ACCESS_TOKEN`.
