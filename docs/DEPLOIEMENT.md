# Déploiement

L'app a été écrite pour la loopback : un poste allumé, un navigateur à côté, et
`launchd` qui déclenche le collecteur. La publier lève ces trois hypothèses à la
fois, et chacune se paie comptant.

## Ce qui ne doit jamais être éphémère

Chez un hébergeur conteneurisé, le système de fichiers est reconstruit à chaque
déploiement. Deux fichiers ne s'en remettraient pas :

| Fichier | Ce qu'on perd sans volume |
|---|---|
| `.powens_finance.db` | L'historique de soldes, que Powens ne conserve pas. Un jour non collecté est perdu pour toujours. Plus les imports CSV et les catégories corrigées à la main. |
| `.powens_state.json` | Le token et l'`id_user` Powens. Sans lui, `bootstrap_client` crée un **nouvel utilisateur** : un compte vierge, plus aucune banque connectée, tout à reconnecter. |

D'où un volume, et `app/config.py::_data_dir` qui y range les deux :
`APP_DATA_DIR` explicite, sinon `RAILWAY_VOLUME_MOUNT_PATH` — que Railway injecte
de lui-même dès qu'un volume est attaché — sinon la racine du dépôt en local.
Rien à configurer dans le cas courant.

## Railway, pas à pas

Depuis la racine du dépôt. Le volume et les variables **avant** le premier
déploiement : l'app démarrerait sinon sur une base vide.

```bash
railway init --name pypowens          # crée le projet
railway add --service pypowens        # crée le service
railway status --json                 # vérifier que le service est bien lié
railway volume add --mount-path /data # volume attaché au service lié
```

Puis les variables. Celles qui portent un secret se passent par `stdin`, pour ne
pas les laisser traîner dans l'historique du shell :

```bash
railway variable set POWENS_DOMAIN=<domaine>
railway variable set POWENS_CLIENT_ID --stdin        # colle la valeur, puis Ctrl-D
railway variable set POWENS_CLIENT_SECRET --stdin
railway variable set POWENS_ACCESS_TOKEN --stdin
railway variable set OPENFIGI_API_KEY --stdin        # facultatif

railway variable set APP_HOST=0.0.0.0                # écouter hors loopback
railway variable set APP_AUTH_USER=<identifiant>
railway variable set APP_AUTH_PASSWORD --stdin       # cf. « Le mot de passe » plus bas
railway variable set APP_TOTP_SECRET --stdin         # second facteur, cf. setup_mfa.py
railway variable set APP_API_TOKEN --stdin           # porte des scripts (sauvegarde)
railway variable set APP_COLLECT_EVERY_HOURS=12
```

Inutile d'en poser d'autres : `PORT` et `RAILWAY_PUBLIC_DOMAIN` sont fournis par
la plateforme et lus tels quels, `RAILWAY_VOLUME_MOUNT_PATH` aussi.

`APP_HOST=0.0.0.0` sans `APP_AUTH_USER`/`APP_AUTH_PASSWORD` fait **échouer le
démarrage**, volontairement : mieux vaut un déploiement mort qu'une app bancaire
ouverte à tous (`app/config.py::_check_host`).

### Reprendre l'historique existant

La base locale part sur le volume avant que quoi que ce soit ne tourne. Une copie
cohérente d'abord — `.backup` de SQLite sait le faire même si l'app écrit en même
temps, là où un `cp` peut capturer un WAL à moitié appliqué :

```bash
sqlite3 .powens_finance.db ".backup /tmp/finance-migration.db"

railway volume files upload /tmp/finance-migration.db /.powens_finance.db
railway volume files upload .powens_state.json /.powens_state.json
railway volume files list / --json          # vérifier les deux fichiers
```

> **Ne jamais réécrire ces fichiers pendant que l'app tourne** : SQLite garde la
> base ouverte, et l'écraser sous ses pieds la corrompt. Si l'envoi réclame un
> service déjà déployé, déployez d'abord, puis envoyez, puis `railway redeploy`
> immédiatement — le processus rouvrira les fichiers au démarrage suivant.

### Déployer

```bash
railway up
railway domain                 # attribue le domaine public
railway logs --service pypowens --lines 100
```

Le `Dockerfile` est utilisé tel quel (`railway.json` fixe `builder: DOCKERFILE`),
avec `numReplicas: 1` : **ne pas monter à deux exemplaires**, SQLite n'accepte
pas deux processus sur le même fichier.

### Déclarer le callback chez Powens

`redirect_uri` suit désormais le domaine public (`RAILWAY_PUBLIC_DOMAIN`, ou
`APP_PUBLIC_URL` si vous mettez un domaine à vous). Cette URL doit être ajoutée
à la liste blanche de la console Powens :

```
https://<domaine-railway>/callback
```

Sans quoi le Webview refusera le retour — l'app le détecte et affiche la valeur
attendue plutôt que d'échouer obscurément (`app/main.py::_redirect_uri_check`).

## Le mot de passe

`APP_AUTH_USER` / `APP_AUTH_PASSWORD` ouvrent la **page de connexion**
(`/connexion`) : un vrai formulaire, que les gestionnaires de mots de passe
remplissent, et qui pose un cookie de session signé (HMAC-SHA256, sept jours,
`HttpOnly`, `SameSite=Lax`, `Secure` dès que le proxy annonce HTTPS). La fenêtre
système du HTTP Basic n'apparaît plus : le défi `WWW-Authenticate` n'est jamais
envoyé à un navigateur.

L'en-tête `Authorization: Basic` reste **accepté** pour les scripts et `curl`
tant que le second facteur n'est pas activé — accepté, mais plus jamais réclamé.
Au-delà, c'est `APP_API_TOKEN` qui prend le relais (voir plus bas).

Rien de neuf à poser chez l'hébergeur : la clé de signature des sessions est
dérivée des identifiants. Conséquence utile, changer le mot de passe déconnecte
partout. Pour garder les sessions ouvertes malgré un changement de mot de passe,
poser `APP_SESSION_SECRET` (valeur tirée au sort, jamais partagée).

Ce mot de passe est donc **deux choses à la fois** : ce qu'on tape, et la graine
qui signe les cookies. Une valeur devinable ne se contente pas de laisser entrer
par la porte — elle permet de FORGER un cookie de session, donc d'entrer sans
mot de passe et sans jamais voir le second facteur (il n'est demandé qu'au
formulaire). L'app **refuse de démarrer** en dessous de 24 caractères ou de 8
caractères distincts. Prenez une valeur tirée au sort, jamais réutilisée
ailleurs :

```bash
openssl rand -base64 24
```

Le frein reste utile pour ce qui passe par la porte : 10 échecs verrouillent un
client 5 minutes, et 40 échecs sur le même compte le verrouillent une demi-heure
quelle que soit l'adresse d'origine — une attaque répartie sur mille adresses ne
déclenche jamais le premier seuil, mais bien le second.

## Le second facteur (MFA)

Un code à six chiffres en plus du mot de passe, sur le modèle de n'importe quel
authenticator (TOTP, RFC 6238). Facultatif : sans `APP_TOTP_SECRET`, rien ne
change.

```bash
uv run python scripts/setup_mfa.py --railway   # génère, enrôle, pose, déploie
```

Le script affiche le secret et une URI `otpauth://` à coller dans ProtonPass
(nouvel élément « Authentification à deux facteurs »), **et** un
`APP_API_TOKEN`, **et** un `APP_SESSION_SECRET`. Vérifiez qu'un code s'affiche
dans ProtonPass avant de fermer la fenêtre.

Cette clé de signature générée est ce qui évite de devoir changer le mot de
passe : sans elle, c'est lui qui signe les cookies, et l'app refuserait de
démarrer s'il est en dessous du plancher (voir plus haut). Les sessions ouvertes
se ferment au déploiement — c'est le seul effet visible.

Deux points à connaître avant d'activer :

* **les appels non interactifs basculent sur le jeton.** Un script ne peut pas
  produire de code : dès que le second facteur est actif, `Authorization: Basic`
  avec le mot de passe est refusé (sinon un simple `curl -u` contournerait tout
  le mécanisme). Reportez `APP_API_TOKEN` dans
  `~/.config/pypowens/backup.env` — `scripts/backup-prod.sh` le préfère
  automatiquement. Une copie de sauvegarde qui échoue en 401 en disant
  « utiliser APP_API_TOKEN », c'est cette variable qui manque ;
* **la perte de l'authenticator ferme la porte.** Le secret n'existe que dans
  ProtonPass et dans la variable Railway : gardez-en une copie là où vous gardez
  vos mots de passe. À défaut, `railway variable set APP_TOTP_SECRET --stdin`
  (valeur vide) ou `scripts/setup_mfa.py --disable --railway` rouvre l'accès —
  l'accès Railway est alors le vrai facteur de secours.

Un code n'est utilisable **qu'une fois** : le pas de temps consommé est mémorisé
en base (`app_meta`), donc un code intercepté ne resservira pas dans sa fenêtre
de trente secondes. Un secret illisible refuse toutes les connexions plutôt que
d'ouvrir : le démarrage l'annonce en `WARNING`.

## Révoquer un accès

La déconnexion incrémente une génération de session stockée en base, ce qui rend
inutilisable **tout** cookie émis avant elle. Un téléphone perdu, un cookie
recopié depuis un journal de proxy : se déconnecter suffit, sans changer le mot
de passe ni attendre les 24 heures d'expiration. Pour couper à distance sans
navigateur :

```bash
railway ssh -- sqlite3 /data/.powens_finance.db \
  "INSERT INTO app_meta (key, value) VALUES ('session_epoch', '1')
   ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1;"
```

Et pour couper la porte des scripts sans toucher au reste :
`railway variable set APP_API_TOKEN=` (valeur vide).

## La collecte

`APP_COLLECT_EVERY_HOURS` fait tourner la collecte **dans le processus web**
(`app/collector.py::scheduled`). Ce n'est pas un choix d'élégance : un volume
Railway ne se monte que sur un seul service, donc un « cron job » voisin
écrirait dans un système de fichiers jeté à la fin de son exécution.

Douze heures suffisent — la collecte archive un solde par jour et par compte, et
les banques ne se rafraîchissent de toute façon qu'une à quelques fois par jour.
La première passe a lieu cinq minutes après le démarrage, pour qu'un
redéploiement en fin de journée ne coûte pas le solde du jour.

## Les alertes

Une connexion bancaire finit toujours par tomber : la DSP2 impose une
ré-authentification périodique, et la connexion passe en `webauthRequired`. Sur
un serveur, la notification macOS ne notifie personne — il faut un canal qui
sorte de la machine, sans quoi une panne peut passer des semaines inaperçue,
c'est-à-dire autant de jours de soldes perdus :

```bash
railway variable set APP_NOTIFY_URL=https://<home-assistant>/api/services/notify/notify
railway variable set APP_NOTIFY_TOKEN --stdin
```

Le corps envoyé est `{"title": …, "message": …}`, ce qu'attendent Home Assistant,
Gotify et la plupart des webhooks. `APP_NOTIFY=0` coupe tout.

## Ailleurs que sur Railway

Le `Dockerfile` ne suppose rien de Railway. Sur un Beelink, un NAS ou une
Raspberry, le volume devient un montage et le domaine public une variable :

```bash
docker build -t pypowens .
docker run -d --name pypowens \
  -p 8000:8000 \
  -v /srv/pypowens:/data \
  -e APP_DATA_DIR=/data \
  -e APP_HOST=0.0.0.0 \
  -e APP_PORT=8000 \
  -e APP_PUBLIC_URL=https://finance.exemple.fr \
  --env-file .env \
  pypowens
```

Chez soi, le plus sobre reste de ne rien publier du tout : un réseau privé
(Tailscale, Cloudflare Tunnel) donne l'accès depuis le téléphone sans exposer
quoi que ce soit — l'authentification reste alors une seconde barrière, pas la
seule.


## Sauvegarde hors du volume

Le volume est la seule copie de l'historique. Un redéploiement ne le touche pas
(`.dockerignore` exclut la base, le schéma n'a que des `CREATE TABLE IF NOT
EXISTS` et des `ADD COLUMN`), mais un volume perdu ou un compte fermé perdrait
tout. Depuis le 04/09/2026, `GET /sauvegarde.db` (authentifié) rend une copie
cohérente ; `scripts/backup-prod.sh` la tire sur le poste de travail chaque jour
(launchd : `scripts/fr.jbartoli.powens-backup.plist`). Première exécution à la
main pour vérifier :

```bash
PYPOWENS_URL=https://finance.jbartoli.fr APP_AUTH_USER=… APP_AUTH_PASSWORD=… scripts/backup-prod.sh
```

La sortie annonce le nombre de soldes archivés et le dernier jour : ce sont les
deux chiffres à comparer avec ce que montre `/connexions`.
