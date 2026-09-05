"""Codes à usage unique fondés sur le temps (TOTP, RFC 6238), sans dépendance.

Un secret unique vit dans ``APP_TOTP_SECRET`` (base32). L'algorithme est le
TOTP-SHA1 standard — six chiffres, pas de trente secondes — c'est-à-dire
exactement ce qu'attendent ProtonPass, Google Authenticator ou Aegis : le
secret s'y enrôle par l'URI ``otpauth://`` ou à la main, sans rien de
spécifique à cette application.

Écrit sur la bibliothèque standard (``hmac``, ``hashlib``, ``struct``) plutôt
qu'avec ``pyotp`` : une centaine de lignes contre une dépendance de plus dans
l'image, pour un algorithme figé depuis 2011 et vérifié ici sur les vecteurs de
test de la RFC.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

DIGITS = 6
PERIOD = 30  # secondes par pas de temps


def generate_secret(num_bytes: int = 20) -> str:
    """Un secret base32 neuf (160 bits par défaut), sans padding comme attendu."""
    return base64.b32encode(secrets.token_bytes(num_bytes)).decode("ascii").rstrip("=")


def _decode_secret(secret_b32: str) -> bytes:
    """Décode un secret base32, en tolérant minuscules, espaces et padding absent."""
    cleaned = secret_b32.strip().replace(" ", "").upper()
    padding = "=" * (-len(cleaned) % 8)
    return base64.b32decode(cleaned + padding, casefold=True)


def secret_error(secret_b32: str) -> str | None:
    """Pourquoi le secret configuré est inutilisable, ``None`` s'il est bon.

    Un secret vide n'est pas une erreur : c'est « MFA désactivé ».
    """
    if not secret_b32:
        return None
    try:
        raw = _decode_secret(secret_b32)
    except (binascii.Error, ValueError):
        return "APP_TOTP_SECRET n'est pas un secret base32 valide"
    if len(raw) < 10:
        return "APP_TOTP_SECRET est trop court (80 bits au moins attendus)"
    return None


def _hotp(secret_b32: str, counter: int, digits: int = DIGITS) -> str:
    key = _decode_secret(secret_b32)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10**digits)).zfill(digits)


def verify(
    secret_b32: str,
    code: str,
    *,
    window: int = 1,
    at: float | None = None,
    digits: int = DIGITS,
    period: int = PERIOD,
) -> int | None:
    """Vérifie ``code`` contre le secret.

    Retourne le PAS DE TEMPS qui correspond — l'appelant s'en sert pour refuser
    le rejeu du même pas (cf. ``store.claim_totp_counter``) — ou ``None`` si
    rien ne correspond. ``window`` accepte un pas d'avance ou de retard : les
    horloges d'un téléphone et d'un serveur ne sont jamais exactement d'accord,
    et sans cette tolérance un code juste serait refusé au changement de pas.
    La comparaison est à temps constant.
    """
    if not code:
        return None
    code = code.strip().replace(" ", "")
    if not code.isdigit() or len(code) != digits:
        return None
    now = at if at is not None else time.time()
    base = int(now) // period
    for step in range(-window, window + 1):
        counter = base + step
        if counter < 0:
            continue
        if hmac.compare_digest(_hotp(secret_b32, counter, digits), code):
            return counter
    return None


def provisioning_uri(secret_b32: str, account_name: str, issuer: str = "Powens Finance") -> str:
    """URI ``otpauth://`` à coller (ou scanner) dans ProtonPass pour l'enrôlement."""
    label = quote(f"{issuer}:{account_name}")
    params = urlencode(
        {
            "secret": secret_b32,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": DIGITS,
            "period": PERIOD,
        }
    )
    return f"otpauth://totp/{label}?{params}"
