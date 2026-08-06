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

# `launchctl load` est déprécié et échoue en silence sur les macOS récents : il rend 0
# sans que le job apparaisse dans `launchctl list`. `bootstrap gui/<uid>` est la forme
# actuelle ; `load` reste en repli pour les versions antérieures.
DOMAIN="gui/$(id -u)"

unload_agent() {
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null \
        || launchctl unload "$TARGET" 2>/dev/null \
        || true
}

if [[ "${1:-}" == "--uninstall" ]]; then
    unload_agent
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
echo "Collecte à 12 h 30, 19 h 30 et 22 h 30, plus à chaque ouverture de session."
echo "(rattrapée au réveil si la machine dormait ; relancer le même jour est sans effet de bord)"
echo
echo "Vérifier tout de suite :   launchctl start $LABEL && sleep 20 && cat /tmp/powens-collector.log"
echo "Désinstaller :             $0 --uninstall"
