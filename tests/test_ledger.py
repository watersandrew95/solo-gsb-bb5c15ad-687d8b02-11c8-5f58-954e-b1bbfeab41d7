"""End-to-end tests for signature checks, idempotency, folding and replay."""
from __future__ import annotations

import time

from conftest import make_event, post_signed


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_income_and_balance(client):
    resp = post_signed(client, make_event("evt_1", amount=5000))
    assert resp.status_code == 200
    assert resp.json() == {"status": "received", "event_id": "evt_1", "duplicate": False}

    rows = client.get("/accounts/acct_1/balance").json()
    assert rows == [
        {"account_id": "acct_1", "currency": "USD", "balance": 5000, "version": 1}
    ]


def test_income_minus_refund_fold(client):
    post_signed(client, make_event("evt_1", amount=5000))
    post_signed(client, make_event("evt_2", event_type="refund", amount=1200))
    rows = client.get("/accounts/acct_1/balance").json()
    assert rows[0]["balance"] == 3800
    assert rows[0]["version"] == 2


def test_duplicate_event_id_is_idempotent(client):
    body = make_event("evt_dup", amount=5000)
    first = post_signed(client, body)
    second = post_signed(client, body)
    assert first.json()["duplicate"] is False
    assert second.json() == {"status": "duplicate", "event_id": "evt_dup", "duplicate": True}

    rows = client.get("/accounts/acct_1/balance").json()
    assert rows[0]["balance"] == 5000
    assert rows[0]["version"] == 1


def test_sender_idempotency_key_dedups(client):
    a = make_event("evt_a", amount=700, idempotency_key="order-42")
    # Same key reused by a different event id is a conflict.
    b = make_event("evt_b", amount=700, idempotency_key="order-42")
    assert post_signed(client, a).status_code == 200
    conflict = post_signed(client, b)
    assert conflict.status_code == 409
    # Re-delivery of the original body is a harmless duplicate.
    assert post_signed(client, a).json()["duplicate"] is True


def test_out_of_order_events_fold_to_same_balance(client):
    # Events delivered in reverse chronological order.
    now = time.time()
    events = [
        make_event("evt_late", event_type="refund", amount=900, occurred_at=now - 10),
        make_event("evt_first", amount=10000, occurred_at=now - 600),
        make_event("evt_mid", amount=2000, occurred_at=now - 300),
    ]
    for event in events:
        assert post_signed(client, event).status_code == 200

    rows = client.get("/accounts/acct_1/balance").json()
    # 10000 + 2000 - 900 regardless of delivery order.
    assert rows[0]["balance"] == 11100


def test_multiple_currencies_are_separate(client):
    post_signed(client, make_event("evt_1", amount=1000, currency="USD"))
    post_signed(client, make_event("evt_2", amount=800, currency="EUR"))
    post_signed(client, make_event("evt_3", amount=200, currency="usd"))  # normalized
    rows = client.get("/accounts/acct_1/balance").json()
    assert {r["currency"]: r["balance"] for r in rows} == {"USD": 1200, "EUR": 800}


def test_accounts_are_isolated(client):
    post_signed(client, make_event("evt_a", account_id="acct_a", amount=100))
    post_signed(client, make_event("evt_b", account_id="acct_b", amount=200))
    assert client.get("/accounts/acct_a/balance").json()[0]["balance"] == 100
    assert client.get("/accounts/acct_b/balance").json()[0]["balance"] == 200


def test_missing_signature_is_rejected(client):
    resp = client.post("/webhooks/events", json=make_event("evt_x"))
    assert resp.status_code == 401


def test_bad_signature_is_rejected(client):
    body = make_event("evt_x", amount=10)
    resp = post_signed(client, body, secret="whsec_wrong_secret")
    assert resp.status_code == 401


def test_stale_timestamp_is_rejected(client_with_clock):
    client, secret = client_with_clock
    stale_ts = int(time.time()) - 10_000
    body = make_event("evt_old", amount=10)
    resp = post_signed(client, body, ts=stale_ts, secret=secret)
    assert resp.status_code == 401


def test_invalid_payload_is_400(client):
    import json

    from app.signing import sign

    raw = b'{"id": "evt_bad", "amount": -5}'
    header = sign("whsec_test_secret", raw)
    resp = client.post(
        "/webhooks/events",
        content=raw,
        headers={"Stripe-Signature": header, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_reconcile_replay_window_and_differences(client):
    now = time.time()
    post_signed(client, make_event("evt_in_window_1", amount=5000, occurred_at=now - 60))
    post_signed(client, make_event("evt_in_window_2", event_type="refund",
                                   amount=1000, occurred_at=now - 30))
    post_signed(client, make_event("evt_old", amount=9999, occurred_at=now - 100_000))

    start = int(now - 3600)
    end = int(now + 3600)
    resp = client.post("/reconcile", json={"start": start, "end": end})
    report = resp.json()
    assert resp.status_code == 200
    assert report["event_count"] == 2
    assert report["replayed_balances"]["acct_1"]["USD"] == 4000
    assert report["integrity_issues"] == []

    # Compare against an external expected net-change for the window.
    good = client.post("/reconcile", json={
        "start": start, "end": end,
        "expected": {"acct_1": {"USD": 4000}},
    }).json()
    assert good["differences"] == []

    bad = client.post("/reconcile", json={
        "start": start, "end": end,
        "expected": {"acct_1": {"USD": 3000}},
    }).json()
    assert bad["differences"] == [
        {
            "account_id": "acct_1",
            "currency": "USD",
            "replayed": 4000,
            "expected": 3000,
            "delta": 1000,
        }
    ]


def test_reconcile_detects_balance_drift(client):
    import sqlite3

    post_signed(client, make_event("evt_1", amount=5000))
    # Corrupt the materialized balance directly, as a storage bug might.
    conn = sqlite3.connect(client.db_path)
    conn.execute("UPDATE balances SET balance = 4999 WHERE account_id = 'acct_1'")
    conn.commit()
    conn.close()

    start = int(time.time()) - 3600
    report = client.post("/reconcile", json={"start": start}).json()
    assert len(report["integrity_issues"]) == 1
    issue = report["integrity_issues"][0]
    assert issue["account_id"] == "acct_1"
    assert issue["drift"] == -1
