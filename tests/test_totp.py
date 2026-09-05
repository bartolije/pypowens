"""Le module TOTP, mesuré contre la RFC plutôt que contre lui-même.

Un second facteur écrit à la main ne vaut que s'il produit exactement les mêmes
codes que ProtonPass ou Google Authenticator. Les six vecteurs ci-dessous sont
ceux de l'annexe B de la RFC 6238 : s'ils passent, l'implémentation est
interopérable ; s'ils cassent, le secret enrôlé dans le téléphone ne servira
plus à rien.
"""

from __future__ import annotations

import base64

import pytest

from app import totp

# Le secret de la RFC : "12345678901234567890" en ASCII, ici en base32.
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode().rstrip("=")

RFC_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


@pytest.mark.parametrize(("moment", "expected"), RFC_VECTORS)
def test_the_rfc_6238_test_vectors_match(moment, expected):
    assert totp._hotp(RFC_SECRET, moment // totp.PERIOD, digits=8) == expected


@pytest.mark.parametrize(("moment", "expected"), RFC_VECTORS)
def test_verify_accepts_the_code_of_its_own_time_step(moment, expected):
    assert totp.verify(RFC_SECRET, expected, at=moment, window=0, digits=8) == (
        moment // totp.PERIOD
    )


def test_verify_returns_the_time_step_so_the_caller_can_refuse_a_replay():
    code = totp._hotp(RFC_SECRET, 1234567890 // 30)
    assert totp.verify(RFC_SECRET, code, at=1234567890) == 1234567890 // 30


def test_a_neighbouring_step_is_accepted_but_not_a_distant_one():
    """Les horloges d'un téléphone et d'un serveur ne sont jamais d'accord à la
    seconde : sans tolérance, un code juste serait refusé au changement de pas."""
    now = 1234567890
    previous = totp._hotp(RFC_SECRET, now // 30 - 1)
    following = totp._hotp(RFC_SECRET, now // 30 + 1)
    faraway = totp._hotp(RFC_SECRET, now // 30 + 5)

    assert totp.verify(RFC_SECRET, previous, at=now) is not None
    assert totp.verify(RFC_SECRET, following, at=now) is not None
    assert totp.verify(RFC_SECRET, faraway, at=now) is None


@pytest.mark.parametrize("code", ["", "  ", "12345", "1234567", "abcdef", "12 34 5", None])
def test_anything_that_is_not_six_digits_is_refused(code):
    assert totp.verify(RFC_SECRET, code or "", at=59) is None


def test_spaces_inside_a_pasted_code_are_tolerated():
    """ProtonPass copie « 123 456 » : refuser l'espace ferait accuser le code."""
    now = 1234567890
    code = totp._hotp(RFC_SECRET, now // 30)
    assert totp.verify(RFC_SECRET, f"{code[:3]} {code[3:]}", at=now) is not None


def test_a_secret_survives_lower_case_spaces_and_missing_padding():
    """Un secret recopié à la main arrive rarement propre."""
    messy = f"  {RFC_SECRET.lower()[:8]} {RFC_SECRET.lower()[8:]}  "
    now = 1234567890
    code = totp._hotp(RFC_SECRET, now // 30)
    assert totp.verify(messy, code, at=now) is not None


def test_a_generated_secret_is_usable_and_unique():
    first, second = totp.generate_secret(), totp.generate_secret()
    assert first != second
    assert totp.secret_error(first) is None
    assert totp.verify(first, totp._hotp(first, 100), at=100 * 30) == 100


def test_an_empty_secret_is_not_an_error_it_is_mfa_disabled():
    assert totp.secret_error("") is None


@pytest.mark.parametrize(
    "secret",
    ["pas du base32 !", "1234", base64.b32encode(b"court").decode().rstrip("=")],
)
def test_an_unusable_secret_is_named_as_such(secret):
    problem = totp.secret_error(secret)
    assert problem and "APP_TOTP_SECRET" in problem


def test_the_provisioning_uri_carries_what_an_authenticator_needs():
    secret = totp.generate_secret()
    uri = totp.provisioning_uri(secret, "moi")

    assert uri.startswith("otpauth://totp/Powens%20Finance%3Amoi?")
    assert f"secret={secret}" in uri
    assert "algorithm=SHA1" in uri and "digits=6" in uri and "period=30" in uri
