#!/usr/bin/env python3
"""Send signed income/refund events to the ledger.

Uses only the standard library. Example::

    python examples/send_event.py income  acct_1 5000 USD evt_1
    python examples/send_event.py refund  acct_1 1200 USD evt_2
    python examples/send_event.py balance acct_1
    python examples/send_event.py replay
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.getenv("LEDGER_URL", "http://127.0.0.1:8000")
SECRET = os.getenv("WEBHOOK_SECRET", "whsec_dev_secret_change_me")


def sign(raw: bytes, ts: int) -> str:
    mac = hmac.new(SECRET.encode(), f"{ts}.".encode() + raw, hashlib.sha256)
    return f"t={ts},v1={mac.hexdigest()}"


def post_event(event_type: str, account_id: str, amount: int,
               currency: str, event_id: str) -> None:
    body = {
        "id": event_id,
        "account_id": account_id,
        "type": event_type,
        "amount": amount,
        "currency": currency,
        "occurred_at": _iso_now(),
    }
    raw = json.dumps(body, separators=(",", ":")).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/webhooks/events",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "Stripe-Signature": sign(raw, int(time.time())),
        },
        method="POST",
    )
    print(_read(req))


def get_balance(account_id: str) -> None:
    req = urllib.request.Request(
        f"{BASE_URL}/accounts/{account_id}/balance"
    )
    print(_read(req))


def replay() -> None:
    req = urllib.request.Request(
        f"{BASE_URL}/reconcile",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    print(_read(req))


def _read(req: urllib.request.Request) -> str:
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return f"{resp.status} {resp.read().decode()}"
    except urllib.error.HTTPError as exc:
        return f"{exc.code} {exc.read().decode()}"


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd in ("income", "refund"):
        _, event_type, account_id, amount, currency, event_id = sys.argv
        post_event(event_type, account_id, int(amount), currency.upper(), event_id)
    elif cmd == "balance":
        get_balance(sys.argv[2])
    elif cmd == "replay":
        replay()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
