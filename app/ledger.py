"""Ledger domain logic: idempotent ingestion, folding and replay.

Amounts are signed at the edge:

* ``income`` -> ``+amount``
* ``refund`` -> ``-amount``

The current balance is therefore a pure fold (sum) of signed amounts, which
makes out-of-order delivery harmless: an event arriving late is inserted and
folded in the same way, and the balance converges regardless of arrival order.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

from .models import EventIn

# Event type -> multiplier applied to the (always positive) amount.
SIGNED_AMOUNTS: dict[str, int] = {
    "income": 1,
    "refund": -1,
}


class IdempotencyConflict(Exception):
    """Same account + idempotency_key reused with a different event id."""


def to_epoch(dt: datetime) -> int:
    """Interpret naive datetimes as UTC and return unix seconds."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def ingest_event(
    conn: sqlite3.Connection, event: EventIn, raw_payload: str
) -> tuple[sqlite3.Row, bool]:
    """Persist one event and fold it into the balance.

    Returns ``(row, duplicate)``. Duplicate deliveries (same event id or the
    same account-scoped idempotency key) are acknowledged but never applied
    twice.
    """
    signed_amount = SIGNED_AMOUNTS[event.type] * event.amount
    occurred_epoch = to_epoch(event.occurred_at)
    idem_key = event.idempotency_key or event.id
    received_epoch = int(time.time())

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT * FROM events WHERE id = ?", (event.id,)
        ).fetchone()
        if existing is not None:
            conn.execute("COMMIT")
            return existing, True

        if event.idempotency_key:
            prior = conn.execute(
                "SELECT * FROM events WHERE account_id = ? AND idem_key = ?",
                (event.account_id, event.idempotency_key),
            ).fetchone()
            if prior is not None:
                if prior["id"] != event.id:
                    raise IdempotencyConflict(
                        f"idempotency_key {event.idempotency_key!r} already used "
                        f"by event {prior['id']!r}"
                    )
                conn.execute("COMMIT")
                return prior, True

        conn.execute(
            """
            INSERT INTO events (id, account_id, type, amount, currency,
                                signed_amount, occurred_at, received_at,
                                idem_key, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.account_id,
                event.type,
                event.amount,
                event.currency,
                signed_amount,
                occurred_epoch,
                received_epoch,
                idem_key,
                raw_payload,
            ),
        )
        conn.execute(
            """
            INSERT INTO balances (account_id, currency, balance, version)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(account_id, currency) DO UPDATE SET
                balance = balance + excluded.balance,
                version = version + 1
            """,
            (event.account_id, event.currency, signed_amount),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    row = conn.execute("SELECT * FROM events WHERE id = ?", (event.id,)).fetchone()
    return row, False


def get_balances(conn: sqlite3.Connection, account_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT account_id, currency, balance, version FROM balances "
        "WHERE account_id = ? ORDER BY currency",
        (account_id,),
    ).fetchall()


def list_events(
    conn: sqlite3.Connection, account_id: str, limit: int = 100
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM events WHERE account_id = ? "
        "ORDER BY occurred_at DESC, id LIMIT ?",
        (account_id, limit),
    ).fetchall()


def _fold_rows(rows: list[sqlite3.Row]) -> dict[str, dict[str, int]]:
    folded: dict[str, dict[str, int]] = {}
    for row in rows:
        folded.setdefault(row["account_id"], {})
        acc = folded[row["account_id"]]
        acc[row["currency"]] = acc.get(row["currency"], 0) + row["signed_amount"]
    return folded


def reconcile(
    conn: sqlite3.Connection, start_epoch: int, end_epoch: int | None
) -> dict:
    """Replay events in ``[start, end)`` and verify materialized balances.

    1. Fold the window from raw events (the replay).
    2. Fold *all* events and compare to the live ``balances`` table to detect
       any drift between the event log and the materialized view, including
       any damage an out-of-order insertion could theoretically have caused.
    """
    if end_epoch is None:
        window_rows = conn.execute(
            "SELECT * FROM events WHERE occurred_at >= ? "
            "ORDER BY occurred_at, id",
            (start_epoch,),
        ).fetchall()
    else:
        window_rows = conn.execute(
            "SELECT * FROM events WHERE occurred_at >= ? AND occurred_at < ? "
            "ORDER BY occurred_at, id",
            (start_epoch, end_epoch),
        ).fetchall()

    window_fold = _fold_rows(window_rows)

    all_rows = conn.execute(
        "SELECT account_id, currency, SUM(signed_amount) AS total "
        "FROM events GROUP BY account_id, currency"
    ).fetchall()
    global_fold: dict[str, dict[str, int]] = {}
    for r in all_rows:
        global_fold.setdefault(r["account_id"], {})[r["currency"]] = r["total"]

    stored_rows = conn.execute(
        "SELECT account_id, currency, balance FROM balances"
    ).fetchall()
    stored: dict[str, dict[str, int]] = {}
    for r in stored_rows:
        stored.setdefault(r["account_id"], {})[r["currency"]] = r["balance"]

    integrity_issues: list[dict] = []
    accounts = set(global_fold) | set(stored)
    for account_id in sorted(accounts):
        currencies = set(global_fold.get(account_id, {})) | set(
            stored.get(account_id, {})
        )
        for currency in sorted(currencies):
            global_total = global_fold.get(account_id, {}).get(currency, 0)
            stored_balance = stored.get(account_id, {}).get(currency, 0)
            drift = stored_balance - global_total
            if drift != 0:
                in_window = window_fold.get(account_id, {}).get(currency, 0)
                # Net change the stored balance implies for this window.
                stored_delta = stored_balance - (global_total - in_window)
                integrity_issues.append(
                    {
                        "account_id": account_id,
                        "currency": currency,
                        "folded_window": in_window,
                        "stored_delta": stored_delta,
                        "drift": drift,
                    }
                )

    return {
        "window_start": start_epoch,
        "window_end": end_epoch if end_epoch is not None else int(time.time()),
        "event_count": len(window_rows),
        "replayed_balances": window_fold,
        "integrity_issues": integrity_issues,
    }
