#!/usr/bin/env python3
"""Enrôle (ou retire) le second facteur, et le jeton des appels non interactifs.

    uv run python scripts/setup_mfa.py                  # génère, affiche, ne persiste rien
    uv run python scripts/setup_mfa.py --env            # + écrit dans .env
    uv run python scripts/setup_mfa.py --railway        # + pose sur Railway et déploie
    uv run python scripts/setup_mfa.py --token-only --env --railway   # rejeton du seul jeton
    uv run python scripts/setup_mfa.py --disable --env --railway      # retire le MFA

Trois variables, posées ensemble parce qu'elles vont ensemble :

* ``APP_TOTP_SECRET`` — le second facteur de la page de connexion. Base32
  standard : à enrôler dans ProtonPass (nouvel élément « Authentification à deux
  facteurs » → coller l'URI ``otpauth://`` affichée, ou le secret seul), dans
  Google Authenticator, Aegis… Ensuite chaque connexion demande six chiffres ;
* ``APP_API_TOKEN`` — la porte des appels qui ne peuvent PAS produire de code :
  ``scripts/backup-prod.sh``, une supervision. Dès que le second facteur est
  actif, le mot de passe cesse d'ouvrir cette porte-là (sinon le MFA serait
  contournable par un simple ``curl -u``), donc ce jeton devient nécessaire ;
* ``APP_SESSION_SECRET`` — la clé qui signe les cookies de session. À défaut,
  c'est le mot de passe qui joue ce rôle, et l'app refuse alors de démarrer s'il
  est devinable (une graine faible se force hors ligne, et qui la trouve FORGE un
  cookie : il entre sans mot de passe, et sans jamais voir le second facteur).
  La poser découple les deux : le mot de passe existant reste valable tel quel,
  quelle que soit sa longueur. Contrepartie : changer le mot de passe ne
  déconnecte plus tout le monde — la déconnexion, elle, le fait toujours.
  ``--garder-clef-session`` pour ne pas y toucher.

En cas de perte de l'authenticator : relancer le script pour un nouveau secret,
ou ``--disable`` pour revenir à un mot de passe seul (accès Railway requis).
"""

from __future__ import annotations

import argparse
import re
import secrets
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app import totp  # noqa: E402

ENV_FILE = REPO / ".env"
TOTP_VARIABLE = "APP_TOTP_SECRET"
TOKEN_VARIABLE = "APP_API_TOKEN"
SESSION_VARIABLE = "APP_SESSION_SECRET"
ACCOUNT = "powens-finance"


def write_env(values: dict[str, str]) -> None:
    """Remplace (ou ajoute) les lignes correspondantes dans .env."""
    if not ENV_FILE.exists():
        sys.exit(f"{ENV_FILE} est absent : copier .env.example d'abord.")
    content = ENV_FILE.read_text()
    for name, value in values.items():
        line = f"{name}={value}"
        content, count = re.subn(rf"^{name}=.*$", line, content, flags=re.MULTILINE)
        if count == 0:
            content = content.rstrip("\n") + f"\n{line}\n"
    ENV_FILE.write_text(content)
    print(f"{ENV_FILE.name} mis à jour — redémarrer l'application locale.")


def write_railway(values: dict[str, str]) -> None:
    """Pose les variables sur Railway, puis déploie.

    ``--stdin`` : la valeur ne passe pas par la ligne de commande, donc ni par
    l'historique du shell ni par la liste des processus. ``--skip-deploys``
    ensuite : un déploiement par variable relancerait l'image ACTUELLE deux fois
    pour rien — ``railway up`` en fin de course envoie le code local et applique
    tout d'un coup.
    """
    for name, value in values.items():
        subprocess.run(
            ["railway", "variable", "set", "--stdin", name, "--skip-deploys"],
            input=value,
            text=True,
            check=True,
            cwd=REPO,
        )
        print(f"{name} posée sur Railway.")
    print("Déploiement (le code local part avec)…")
    subprocess.run(["railway", "up"], check=True, cwd=REPO)
    print("Déployé.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="setup_mfa.py",
        description="Enrôle ou retire le second facteur TOTP, et le jeton des scripts.",
    )
    parser.add_argument("--env", action="store_true", help="mettre à jour le .env local")
    parser.add_argument("--railway", action="store_true", help="mettre à jour le service Railway")
    parser.add_argument(
        "--disable", action="store_true", help="retirer le second facteur (secret vidé)"
    )
    parser.add_argument(
        "--token-only",
        action="store_true",
        help="régénérer le seul jeton d'API, sans toucher au second facteur",
    )
    parser.add_argument(
        "--garder-clef-session",
        action="store_true",
        help=(
            "ne pas poser APP_SESSION_SECRET : c'est alors le mot de passe qui "
            "signe les sessions, et il doit faire 24 caractères variés au moins"
        ),
    )
    args = parser.parse_args()

    values: dict[str, str] = {}

    if args.disable:
        values[TOTP_VARIABLE] = ""
        print("Second facteur retiré : APP_TOTP_SECRET sera vidé.")
        print("Le mot de passe redevient suffisant, y compris pour les scripts.")
    elif not args.token_only:
        secret = totp.generate_secret()
        values[TOTP_VARIABLE] = secret
        print("\n=== Second facteur (à enrôler dans ProtonPass) ===\n")
        print(f"  Secret (base32) : {secret}")
        print(f"  URI otpauth     : {totp.provisioning_uri(secret, ACCOUNT)}\n")
        print("  ProtonPass → nouvel élément 2FA → coller l'URI ci-dessus (ou le secret).")
        print("  Vérifier qu'un code s'affiche AVANT de fermer, puis persister.\n")

    if not args.disable:
        token = secrets.token_urlsafe(32)
        values[TOKEN_VARIABLE] = token
        print("=== Jeton des appels non interactifs (sauvegarde, supervision) ===\n")
        print(f"  {TOKEN_VARIABLE}={token}\n")
        print("  À reporter dans ~/.config/pypowens/backup.env (chmod 600) :")
        print(f"    {TOKEN_VARIABLE}={token}\n")

    # Pas sur --disable : on y ferme des portes, ce n'est pas le moment de
    # déconnecter tout le monde en changeant la clé de signature.
    if not (args.disable or args.garder_clef_session):
        values[SESSION_VARIABLE] = secrets.token_urlsafe(32)
        print("=== Clé de signature des sessions ===\n")
        print("  Générée : le mot de passe existant n'a plus à faire office de")
        print("  graine, il reste valable tel quel quelle que soit sa longueur.")
        print("  Les sessions ouvertes se ferment au déploiement (normal).\n")

    if args.env:
        write_env(values)
    if args.railway:
        write_railway(values)
    if not (args.env or args.railway):
        print("Rien n'a été persisté : relancer avec --env et/ou --railway.")


if __name__ == "__main__":
    main()
