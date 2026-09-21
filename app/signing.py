"""HMAC-SHA256 signature scheme (Stripe-style).

Header::

    t=<unix seconds>,v1=<hex hmac>

Signed payload::

    f"{t}.{raw_body}"

Verification is constant time. When ``SIGNATURE_TOLERANCE_SECONDS=0`` the
timestamp age check is disabled (useful for tests / offline replay).
"""
from __future__ import annotations

import hashlib
import hmac
import time

SCHEME = "v1"


class SignatureError(Exception):
    """Raised when the signature header is missing, malformed or invalid."""


def _compute(key: bytes, message: bytes) -> bytes:
    return hmac.new(key, message, hashlib.sha256).digest()


def sign(secret: str, raw_body: bytes, timestamp: int | None = None) -> str:
    """Build a ``t=...,v1=...`` signature header (used by clients / examples)."""
    ts = int(time.time() if timestamp is None else timestamp)
    mac = _compute(secret.encode(), f"{ts}.".encode() + raw_body).hex()
    return f"t={ts},{SCHEME}={mac}"


def parse_header(header: str | None) -> tuple[int, str]:
    if not header:
        raise SignatureError("missing signature header")
    ts: int | None = None
    sigs: list[str] = []
    for item in header.split(","):
        item = item.strip()
        if "=" not in item:
            continue
        key, _, value = item.partition("=")
        if key == "t":
            try:
                ts = int(value)
            except ValueError:
                raise SignatureError("invalid timestamp in signature header")
        elif key == SCHEME:
            sigs.append(value)
    if ts is None:
        raise SignatureError("signature header missing timestamp")
    if not sigs:
        raise SignatureError("signature header missing v1 signature")
    return ts, sigs[0]


def verify(
    secret: str,
    raw_body: bytes,
    header: str | None,
    tolerance_seconds: int,
    now: int | None = None,
) -> int:
    """Validate the header and return the signed timestamp.

    Raises :class:`SignatureError` on any failure.
    """
    ts, provided_hex = parse_header(header)

    if tolerance_seconds > 0:
        current = int(time.time() if now is None else now)
        age = current - ts
        if age < 0 or age > tolerance_seconds:
            raise SignatureError("signature timestamp outside tolerance window")

    expected = _compute(secret.encode(), f"{ts}.".encode() + raw_body)
    try:
        provided = bytes.fromhex(provided_hex)
    except ValueError:
        raise SignatureError("signature is not valid hex")
    if not hmac.compare_digest(expected, provided):
        raise SignatureError("signature mismatch")
    return ts
