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
| 11 | Webhooks, budgets par catégorie | ⏳ |

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
