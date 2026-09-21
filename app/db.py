"""SQLite persistence layer.

One connection per request, WAL journal, all balance mutations happen inside
a single ``BEGIN IMMEDIATE`` transaction together with the event insert so
that concurrent deliveries cannot double-spend an idempotency key.
"""
from __future__ import annotations

import sqlite3
from typing import Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id              TEXT PRIMARY KEY,
    account_id      TEXT NOT NULL,
    type            TEXT NOT NULL,
    amount          INTEGER NOT NULL,
    currency        TEXT NOT NULL,
    signed_amount   INTEGER NOT NULL,
    occurred_at     INTEGER NOT NULL,
    received_at     INTEGER NOT NULL,
    idem_key        TEXT NOT NULL,
    payload         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_account  ON events(account_id);
CREATE INDEX IF NOT EXISTS idx_events_occurred ON events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_idem     ON events(account_id, idem_key);

CREATE TABLE IF NOT EXISTS balances (
    account_id TEXT NOT NULL,
    currency   TEXT NOT NULL,
    balance    INTEGER NOT NULL,
    version    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, currency)
);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path or settings.db_path,
        timeout=30,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: yields a connection and always closes it."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
