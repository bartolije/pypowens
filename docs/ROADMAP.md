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
- [ ] **Marqueurs de changement de périmètre sur la courbe.** Le saut de
  +257 k€ du 02/08 restera à jamais dans l'historique : quand le NOMBRE de
  comptes archivés change d'un jour à l'autre, poser un marqueur visuel sur la
  courbe (« périmètre modifié : -1 compte ») au lieu de laisser croire à une
  performance. *Done : un point annoté sur `/patrimoine` aux dates où le
  périmètre a bougé ; tooltip nomme les comptes entrés/sortis.* (effort : ~½ j)
- [ ] **Réintégration rétroactive après réparation.** Quand la connexion CCF
  sera réparée, le prêt re-rentrera : la courbe re-sautera de -257 k€. Offrir
  une correction : recopier le dernier solde connu d'un compte désactivé dans
  les snapshots des jours d'absence (marqués `estimated`). *Done : après
  réparation, la courbe est plate sur l'épisode, pas en dents de scie ; les
  jours estimés sont distinguables en base.* (effort : 1 j, à concevoir avec
  soin — écrit dans balance_snapshot)
- [ ] **Synchro d'ouverture.** Pas de webhooks possibles (app loopback, pas
  d'URL publique) : au premier chargement du jour, déclencher
  `update_connection` sur les connexions dont `last_update` > 24 h, en tâche
  de fond non bloquante. *Done : ouvrir l'app le matin suffit à rafraîchir
  Trade Republic sans clic.* (effort : ½ j — attention aux états webauth, ne
  jamais déclencher de SCA en boucle)
- [ ] **Normaliser les datetimes de la lib** (§3.7 de l'audit, reporté) : le
  même champ peut être naïf ou aware selon le connecteur → tout `sorted()`/
  comparaison peut lever `TypeError` chez un consommateur. *Done :
  `_parse_datetime` renvoie de l'aware UTC systématiquement + test.* (~2 h)

## P1 — Lib : compléter la surface avant toute publication PyPI

- [ ] **`GET /users/{id}/transactionsclusters`** — la détection de récurrences
  NATIVE de Powens. Sert d'abord à évaluer le détecteur maison (page de
  comparaison en mode debug). *Done : méthode + modèle + test respx.* (~2 h)
- [ ] **`GET /users/{id}/pockets`** (poches PEA/AV : fonds euros vs UC) et
  **`GET /users/{id}/loans`** (échéancier, taux, capital restant dû — pour un
  vrai onglet Passifs). *Done : méthodes + modèles + tests ; /patrimoine/{id}
  d'un crédit affiche taux et capital restant.* (~½ j)
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
