#!/usr/bin/env bash
# Installe la collecte quotidienne (soldes + valorisations) comme LaunchAgent macOS.
#
# Le plist versionné porte des chemins fictifs — le dépôt est public. Ce script les
# remplace par les chemins réels de cette machine, puis charge l'agent.
#
#     ./scripts/install-collector.sh            # installe et charge
#     ./scripts/install-collector.sh --dry-run  # affiche le plist résultant, n'installe rien
#     ./scripts/install-collector.sh --uninstall
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="fr.jbartoli.powens-collector"
TEMPLATE="$REPO/scripts/$LABEL.plist"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ "${1:-}" == "--uninstall" ]]; then
    launchctl unload "$TARGET" 2>/dev/null || true
    rm -f "$TARGET"
    echo "Agent désinstallé. Les données déjà archivées restent intactes."
    exit 0
fi

if [[ ! -x "$REPO/.venv/bin/python" ]]; then
    echo "Aucun environnement dans $REPO/.venv — créer le venv d'abord." >&2
    exit 1
fi

rendered="$(sed "s|/CHEMIN/VERS/pypowens|$REPO|g" "$TEMPLATE")"

if [[ "${1:-}" == "--dry-run" ]]; then
    echo "$rendered"
    exit 0
fi

mkdir -p "$(dirname "$TARGET")"
printf '%s\n' "$rendered" > "$TARGET"

# Recharger plutôt que charger : réinstaller par-dessus une version déjà chargée échoue.
launchctl unload "$TARGET" 2>/dev/null || true
launchctl load "$TARGET"

echo "Agent installé : $TARGET"
echo "Collecte quotidienne à 19 h 30 (rattrapée au réveil si la machine dormait)."
echo
echo "Vérifier tout de suite :   launchctl start $LABEL && sleep 20 && cat /tmp/powens-collector.log"
echo "Désinstaller :             $0 --uninstall"
