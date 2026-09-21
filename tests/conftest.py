"""Pytest fixtures: isolated temp DB + signed-request helpers."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

# Must be set BEFORE app modules read the settings.
os.environ["LEDGER_DB_PATH"] = "/tmp/test_ledger_default.db"
os.environ["WEBHOOK_SECRET"] = "whsec_test_secret"
os.environ["SIGNATURE_TOLERANCE_SECONDS"] = "0"  # disable timestamp age check

from app.signing import sign  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    import app.config as config
    from app.db import init_db
    from app.main import app
    from fastapi.testclient import TestClient

    db_path = str(tmp_path / "test.db")
    # settings is a frozen dataclass shared by import across app modules.
    object.__setattr__(config.settings, "db_path", db_path)
    object.__setattr__(config.settings, "signature_tolerance_seconds", 0)
    init_db(db_path)

    with TestClient(app) as c:
        c.db_path = db_path
        yield c


@pytest.fixture()
def client_with_clock(tmp_path):
    """Like ``client`` but with a 300s timestamp tolerance enabled."""
    import app.config as config
    from app.db import init_db
    from app.main import app
    from fastapi.testclient import TestClient

    db_path = str(tmp_path / "clock.db")
    secret = "whsec_test_secret"
    object.__setattr__(config.settings, "db_path", db_path)
    object.__setattr__(config.settings, "signature_tolerance_seconds", 300)
    init_db(db_path)

    with TestClient(app) as c:
        c.db_path = db_path
        yield c, secret

    object.__setattr__(config.settings, "signature_tolerance_seconds", 0)


def make_event(
    event_id: str,
    account_id: str = "acct_1",
    event_type: str = "income",
    amount: int = 1000,
    currency: str = "USD",
    occurred_at: float | None = None,
    idempotency_key: str | None = None,
) -> dict:
    ts = time.time() if occurred_at is None else occurred_at
    body = {
        "id": event_id,
        "account_id": account_id,
        "type": event_type,
        "amount": amount,
        "currency": currency,
        "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
    }
    if idempotency_key is not None:
        body["idempotency_key"] = idempotency_key
    return body


def post_signed(client, body: dict, ts: int | None = None, secret: str | None = None):
    raw = json.dumps(body, separators=(",", ":")).encode()
    header = sign(secret or "whsec_test_secret", raw, ts or int(time.time()))
    return client.post(
        "/webhooks/events",
        content=raw,
        headers={"Stripe-Signature": header},
    )
