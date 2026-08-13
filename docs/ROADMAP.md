# Roadmap — reste à faire après l'audit du 12/08/2026

L'audit initial (~60 findings) est **implémenté** : phases 0-5 livrées les
12-13/08 (voir `DEVLOG.md` § « Audit complet du 12/08/2026 » et le CHANGELOG).
Ce fichier est le plan du **restant**, priorisé, enrichi des leçons du terrain
des 12-13/08 (prêt fantôme, layout mobile, connexion muette).

Convention : chaque item porte son critère de done. Un item non coché sans
critère clair n'est pas prêt à être attaqué.

---

## P0 — Le chiffre ne doit jamais mentir (fiabilité du patrimoine)

- [x] ~~Bandeau global de santé des connexions~~ (livré le 13/08, `b34e822`) :
  erreur / muette / comptes désactivés hors total, sur toutes les pages.
- [x] ~~Marqueurs de changement de périmètre~~ (13/08) : notes datées sous la
  courbe de `/` et `/patrimoine` — « périmètre modifié (+X €) : entrée/sortie
  de … — ce saut n'est ni un gain ni une perte ». Seuls les changements
  DURABLES sont signalés (les absences temporaires sont comblées, cf. suivant).
- [x] ~~Réintégration rétroactive~~ (13/08, en LECTURE plutôt qu'en écriture :
  plus sûr) : `net_worth_history` comble les trous d'un compte ENTRE deux
  apparitions avec son dernier solde connu — l'épisode du prêt fantôme devient
  plat rétroactivement dès que le compte réapparaît, sans toucher à la base.
- [x] ~~Synchro d'ouverture~~ (13/08) : au plus une fois par 6 h, les
  connexions BLOQUÉES (saines, >24 h sans synchro, aucun `next_try` planifié
  côté Powens) sont relancées au chargement d'une page. Jamais les connexions
  en erreur (risque de SCA en boucle), jamais celles que Powens va repasser.
- [x] ~~Normaliser les datetimes de la lib~~ (13/08) : `_parse_datetime`
  renvoie systématiquement du NAÏF à heure murale préservée (les deux formes
  Powens portent la même heure locale ; une conversion UTC aurait mélangé les
  référentiels). Plus de `TypeError` sur tri/comparaison.

## P1 — Lib : compléter la surface avant toute publication PyPI

- [ ] **`GET /users/{id}/transactionsclusters`** — la détection de récurrences
  NATIVE de Powens. Sert d'abord à évaluer le détecteur maison (page de
  comparaison en mode debug). *Done : méthode + modèle + test respx.* (~2 h)
- [ ] **`GET /users/{id}/pockets`** (poches PEA/AV : fonds euros vs UC) et
  **`GET /users/{id}/loans`** (échéancier, taux, capital restant dû — pour un
  vrai onglet Passifs). *Done : méthodes + modèles + tests ; /patrimoine/{id}
  d'un crédit affiche taux et capital restant.* (~½ j)
- [x] ~~`PUT /users/{id}/accounts/{aid}`~~ (13/08, `update_account`) — avec le
  piège `?all` sans lequel un compte désactivé est inadressable (404) ; bouton
  « Réintégrer » branché sur le bandeau de santé.
- [ ] **`DELETE /users/{id}` et `GET /users`** — manque RGPD : la lib crée des
  utilisateurs mais ne peut ni les lister ni les supprimer (orphelins des
  §3.1/3.40 de l'audit). *Done : méthodes + doc « nettoyer les orphelins ».* (~2 h)
- [ ] **Documents** (`/documents`, `/documents/{id}/file`) — relevés et IFU
  téléchargeables depuis l'app. *(~½ j)*
- [ ] **Hygiène HTTP** : `User-Agent: pypowens/x.y`, conserver le corps
  non-JSON des erreurs (page CloudFront jetée aujourd'hui), exposer un
  `X-Request-Id` pour le support Powens. *(~2 h)*
- [ ] **Publication** : job CI build + `twine check` + publication sur tag,
  vérif que le wheel n'embarque pas `app/`. Aligner `__version__`/CHANGELOG.
  *(~2 h)*

## P2 — Produit

- [ ] **Budgets par catégorie** (le plus ancien manque déclaré) : enveloppe
  mensuelle par catégorie, barre de progression sur /analyse, alerte de
  dépassement dans le bandeau santé. Stockage : table `budget(categorie,
  montant)` + UI d'édition sur /analyse. *Done : je fixe 300 €/mois de
  restauration et je vois où j'en suis le 20 du mois.* (~1 j)
- [ ] **Comparaison à un indice** (étape 14 du DEVLOG) : série d'un ETF de
  référence via yfinance (déjà présent) superposée au TWR sur /performance.
  *Done : courbe « vous vs MSCI World » sur la même fenêtre.* (~1 j)
- [ ] **Recherche globale** (libellé/montant/date sur tout l'historique,
  serveur) + **fusion de merchant_keys** (deux clés = même marchand) +
  **renommage de comptes**. *(~1 j)*
- [ ] **Pagination de l'onglet Transactions** du détail de compte (24 mois
  rendus d'un bloc aujourd'hui). *(~2 h)*
- [ ] **Notifications hors-page** : le bandeau ne vit que dans l'app —
  brancher les alertes (santé + hausses d'abonnements) sur une notification
  macOS via le collecteur launchd. *(~½ j)*

## P3 — Qualité, accessibilité, dette

- [ ] **Accessibilité** : les tooltips maison suppriment les `<title>` SVG
  (muets au lecteur d'écran), pas de `:focus-visible`, lignes cliquables non
  focusables, `--text-muted` sous le contraste AA. *Done : navigation clavier
  complète + axe-core sans erreur bloquante.* (~1 j)
- [ ] **Macros Jinja** : le sélecteur de période existe en 3 exemplaires
  (synthese, recap, detail) ; les query strings se construisent à la main dans
  recap.html. *Done : une macro `period_pills()`, une `qs()`.* (~2 h)
- [ ] **Hypothesis sur les parsers** (`parse_amount`, `parse_date`,
  `parse_statement`) — invariants simples, gisement de cas tordus. *(~2 h)*
- [ ] **mypy strict sur app/** (le profil allégé actuel laisse passer les
  signatures non annotées) — module par module, commencer par store/
  performance. *(~1 j au fil de l'eau)*
- [ ] **Couverture 88 → 92 %** : collector.py (65 %), helpers graphiques
  (65 %), recap.py (76 %). Relever le plancher CI à chaque palier. *(au fil
  de l'eau)*
- [ ] **Seuil « muette » configurable** (`APP_SILENT_DAYS`, défaut 3) et
  affichage de l'âge de synchro par connexion sur /patrimoine. *(~1 h)*

## Ordre d'attaque recommandé

1. P0.2 (marqueurs de périmètre) — court, et c'est la suite logique du bandeau.
2. P0.4 (datetimes lib) — 2 h, élimine une classe de crash latente.
3. P1.1 + P1.2 (transactionsclusters, pockets/loans) — nourrit directement
   Passifs et valide le détecteur.
4. P2.1 (budgets) — la fonctionnalité la plus visible au quotidien.
5. Le reste au fil de l'eau, en gardant la discipline : chaque correctif
   arrive avec son test de non-régression, un commit, un push.
