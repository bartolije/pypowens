#!/usr/bin/env bash
# Copie HORS SITE de la base de production.
#
# L'historique des soldes n'existe que dans .powens_finance.db, sur le volume de
# l'hébergeur ; les copies quotidiennes du collecteur y restent aussi. Ce script
# tire une copie cohérente via GET /sauvegarde.db (route authentifiée) sur le
# poste de travail, vérifie son intégrité, et garde 90 jours.
#
# Usage :
#   PYPOWENS_URL=https://finance.exemple.fr APP_AUTH_USER=… APP_AUTH_PASSWORD=… \
#     scripts/backup-prod.sh [dossier]          # défaut : ~/Backups/pypowens
# ou, pour launchd/cron : les trois variables dans ~/.config/pypowens/backup.env (chmod 600).
set -euo pipefail

CONF="${HOME}/.config/pypowens/backup.env"
if [ -f "$CONF" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$CONF"
  set +a
fi
: "${PYPOWENS_URL:?PYPOWENS_URL manquante (ex. https://finance.exemple.fr)}"
: "${APP_AUTH_USER:?APP_AUTH_USER manquante}"
: "${APP_AUTH_PASSWORD:?APP_AUTH_PASSWORD manquante}"

DIR="${1:-${HOME}/Backups/pypowens}"
mkdir -p "$DIR" && chmod 700 "$DIR"
OUT="$DIR/powens_finance-$(date +%F).db"
TMP="$OUT.part"

curl -fsS --max-time 180 -u "${APP_AUTH_USER}:${APP_AUTH_PASSWORD}" \
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
