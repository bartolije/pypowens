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

- [~] **transactionsclusters / pockets / loans / documents — REPORTÉS**
  (sondés en live le 13/08 : `transactionsclusters` 0, `pockets` 0,
  `documents` 0, `loans` 404 sous les deux formes). Powens ne les alimente pas
  sur cette app — même syndrome que `indicators`/`categories`. Les implémenter
  aujourd'hui serait du code mort. Re-sonder à l'occasion :
  `client._request("GET", "users/me/pockets")` etc.
- [x] ~~`PUT /users/{id}/accounts/{aid}`~~ (13/08, `update_account`) — avec le
  piège `?all` sans lequel un compte désactivé est inadressable (404) ; bouton
  « Réintégrer » branché sur le bandeau de santé.
- [—] **`DELETE /users` / `GET /users` (RGPD)** et **Publication PyPI** —
  ÉCARTÉS par décision du 13/08 : sans intérêt pour une app perso locale.
- [x] ~~Hygiène HTTP~~ (13/08) : `User-Agent: pypowens/x.y` sur chaque requête,
  extrait du corps non-JSON conservé dans le message d'erreur (fini le
  « [HTTP 503] unknown error » quand CloudFront renvoie une page HTML), et
  `request_id` Powens exposé sur les exceptions (payload JSON ou en-tête).
  Version unifiée dans `_version.py`.

## P2 — Produit — ✅ TERMINÉ le 13/08

- [x] ~~Budgets par catégorie~~ : carte sur /analyse (suivi du mois COURANT,
  barre rouge en dépassement, édition par ligne) + alerte de dépassement dans
  le bandeau global (seulement cache chaud — /import ne paie pas l'historique).
- [x] ~~Comparaison à un indice~~ : le collecteur archive les clôtures
  quotidiennes (APP_BENCHMARK_TICKER, défaut IWDA.AS, 5 ans au 1er passage) ;
  /performance superpose l'indice en pointillés, rebasé sur la valeur de
  départ — « si la même somme était sur l'indice ». Zéro réseau au rendu.
- [x] ~~Recherche globale~~ (/recherche + champ topbar : libellé ET montant
  exact), ~~fusion de marchands~~ (drill-down, appliquée à la sortie de
  merchant_key, sans chaîne), ~~renommage de comptes~~ (local, appliqué à la
  lecture, suit le compte partout).
- [x] ~~Pagination de l'onglet Transactions~~ (100/page).
- [x] ~~Notifications macOS~~ : le collecteur pousse (osascript, best effort,
  APP_NOTIFY=0 pour couper) — santé des connexions + alertes d'abonnements
  non acquittées.

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
