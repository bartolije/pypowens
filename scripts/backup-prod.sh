#!/usr/bin/env bash
# Copie HORS SITE de la base de production.
#
# L'historique des soldes n'existe que dans .powens_finance.db, sur le volume de
# l'hébergeur ; les copies quotidiennes du collecteur y restent aussi. Ce script
# tire une copie cohérente via GET /sauvegarde.db (route authentifiée) sur le
# poste de travail, vérifie son intégrité, et garde 90 jours.
#
# Authentification : APP_API_TOKEN de préférence — c'est la seule porte qui reste
# ouverte aux appels non interactifs quand le second facteur est actif (un script
# ne peut pas produire de code à six chiffres). À défaut, le couple
# APP_AUTH_USER / APP_AUTH_PASSWORD, qui ne fonctionne que sans MFA.
#
# Usage :
#   PYPOWENS_URL=https://finance.exemple.fr APP_API_TOKEN=… \
#     scripts/backup-prod.sh [dossier]          # défaut : ~/Backups/pypowens
# ou, pour launchd/cron : les variables dans ~/.config/pypowens/backup.env (chmod 600).
set -euo pipefail

CONF="${HOME}/.config/pypowens/backup.env"
if [ -f "$CONF" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$CONF"
  set +a
fi
: "${PYPOWENS_URL:?PYPOWENS_URL manquante (ex. https://finance.exemple.fr)}"
if [ -n "${APP_API_TOKEN:-}" ]; then
  AUTH=(-H "Authorization: Bearer ${APP_API_TOKEN}")
else
  : "${APP_AUTH_USER:?ni APP_API_TOKEN ni APP_AUTH_USER : aucun identifiant fourni}"
  : "${APP_AUTH_PASSWORD:?APP_AUTH_PASSWORD manquante}"
  AUTH=(-u "${APP_AUTH_USER}:${APP_AUTH_PASSWORD}")
  echo "avertissement : sans APP_API_TOKEN, cette copie échouera dès que le second" >&2
  echo "                facteur sera actif (cf. scripts/setup_mfa.py)." >&2
fi

DIR="${1:-${HOME}/Backups/pypowens}"
mkdir -p "$DIR" && chmod 700 "$DIR"
OUT="$DIR/powens_finance-$(date +%F).db"
TMP="$OUT.part"

curl -fsS --max-time 180 "${AUTH[@]}" \
  "${PYPOWENS_URL%/}/sauvegarde.db" -o "$TMP"

if ! sqlite3 "$TMP" "PRAGMA integrity_check;" | grep -qx ok; then
  echo "copie corrompue, rejetée : $TMP" >&2
  rm -f "$TMP"
  exit 1
fi
mv "$TMP" "$OUT" && chmod 600 "$OUT"

SNAPSHOTS=$(sqlite3 "$OUT" "SELECT COUNT(*) FROM balance_snapshot;")
LAST_DAY=$(sqlite3 "$OUT" "SELECT COALESCE(MAX(day), '-') FROM balance_snapshot;")
echo "$(date '+%F %T') — $OUT : ${SNAPSHOTS} soldes archivés (dernier jour ${LAST_DAY}), $(du -h "$OUT" | cut -f1)"

# Rotation : 90 copies quotidiennes.
ls -t "$DIR"/powens_finance-*.db 2>/dev/null | tail -n +91 | xargs -r rm -f
