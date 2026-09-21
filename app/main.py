"""FastAPI application: webhook intake, balance queries and reconciliation."""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .config import settings
from .db import get_conn, init_db
from .ledger import IdempotencyConflict, get_balances, ingest_event, list_events, reconcile, to_epoch
from .models import (
    BalanceOut,
    EventIn,
    EventOut,
    ReconcileIn,
    WebhookAck,
)
from .signing import SignatureError, verify


@asynccontextmanager
async def lifespan(_app: FastAPI):
    os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
    init_db()
    yield

app = FastAPI(
    title="Webhook Ledger API",
    version="1.0.0",
    description="Signed, idempotent income/refund webhooks with folding "
    "balances and event replay reconciliation.",
    lifespan=lifespan,
)


@app.exception_handler(SignatureError)
def _signature_handler(_request: Request, exc: SignatureError) -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": str(exc)})


@app.exception_handler(IdempotencyConflict)
def _idem_handler(_request: Request, exc: IdempotencyConflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": str(exc)})


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/webhooks/events", response_model=WebhookAck, status_code=200)
async def receive_event(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
    x_signature: str | None = Header(default=None, alias="X-Signature"),
    conn=Depends(get_conn),
) -> WebhookAck:
    raw = await request.body()

    verify(
        settings.webhook_secret,
        raw,
        stripe_signature or x_signature,
        settings.signature_tolerance_seconds,
    )

    try:
        payload = json.loads(raw)
        event = EventIn.model_validate(payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid event payload", "detail": str(exc)},
        )

    row, duplicate = ingest_event(conn, event, raw.decode("utf-8"))
    return WebhookAck(
        status="received" if not duplicate else "duplicate",
        event_id=row["id"],
        duplicate=duplicate,
    )


@app.get("/accounts/{account_id}/balance", response_model=list[BalanceOut])
def account_balance(account_id: str, conn=Depends(get_conn)) -> list[BalanceOut]:
    rows = get_balances(conn, account_id)
    return [
        BalanceOut(
            account_id=r["account_id"],
            currency=r["currency"],
            balance=r["balance"],
            version=r["version"],
        )
        for r in rows
    ]


@app.get("/accounts/{account_id}/events", response_model=list[EventOut])
def account_events(account_id: str, limit: int = 100, conn=Depends(get_conn)):
    limit = max(1, min(limit, 1000))
    rows = list_events(conn, account_id, limit)
    return [
        EventOut(
            id=r["id"],
            account_id=r["account_id"],
            type=r["type"],
            amount=r["amount"],
            currency=r["currency"],
            signed_amount=r["signed_amount"],
            occurred_at=r["occurred_at"],
            received_at=r["received_at"],
            idem_key=r["idem_key"],
        )
        for r in rows
    ]


@app.post("/reconcile")
def reconcile_report(body: ReconcileIn | None = None, conn=Depends(get_conn)) -> dict:
    body = body or ReconcileIn()
    now = datetime.now(timezone.utc)
    start = body.start or (now - timedelta(seconds=settings.replay_window_seconds))
    end = body.end
    start_epoch = to_epoch(start)
    end_epoch = to_epoch(end) if end is not None else None

    if end_epoch is not None and end_epoch < start_epoch:
        return JSONResponse(
            status_code=400,
            content={"error": "end must be greater than or equal to start"},
        )

    report = reconcile(conn, start_epoch, end_epoch)

    differences: list[dict] = []
    if body.expected is not None:
        replayed = report["replayed_balances"]
        accounts = set(body.expected) | set(replayed)
        for account_id in sorted(accounts):
            expected_currencies = body.expected.get(account_id, {})
            replayed_currencies = replayed.get(account_id, {})
            for currency in sorted(set(expected_currencies) | set(replayed_currencies)):
                exp = expected_currencies.get(currency, 0)
                got = replayed_currencies.get(currency, 0)
                if exp != got:
                    differences.append(
                        {
                            "account_id": account_id,
                            "currency": currency,
                            "replayed": got,
                            "expected": exp,
                            "delta": got - exp,
                        }
                    )
    report["differences"] = differences
    return report
