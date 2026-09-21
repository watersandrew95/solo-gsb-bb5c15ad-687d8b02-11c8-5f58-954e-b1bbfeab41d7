"""Pydantic schemas for request and response bodies."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

EventInType = Literal["income", "refund"]
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class EventIn(BaseModel):
    id: str = Field(..., min_length=1, max_length=128)
    account_id: str = Field(..., min_length=1, max_length=128)
    type: EventInType
    amount: int = Field(..., gt=0, description="positive minor units, e.g. cents")
    currency: str = Field(..., min_length=3, max_length=3)
    occurred_at: datetime = Field(..., description="event time, ISO-8601 / RFC3339")
    idempotency_key: Optional[str] = Field(
        default=None,
        max_length=256,
        description="optional sender-side dedup key, scoped per account",
    )

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v: str) -> str:
        v = v.upper()
        if not CURRENCY_RE.match(v):
            raise ValueError("currency must be a 3-letter ISO-4217 code, e.g. USD")
        return v


class EventOut(BaseModel):
    id: str
    account_id: str
    type: str
    amount: int
    currency: str
    signed_amount: int
    occurred_at: int
    received_at: int
    idem_key: str


class BalanceOut(BaseModel):
    account_id: str
    currency: str
    balance: int
    version: int


class WebhookAck(BaseModel):
    status: str
    event_id: str
    duplicate: bool


class ReconcileIn(BaseModel):
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    expected: Optional[dict[str, dict[str, int]]] = Field(
        default=None,
        description="account_id -> currency -> balance, compared to the "
        "balance folded from the replay window",
    )

    @field_validator("start", "end", mode="before")
    @classmethod
    def _coerce_epoch(cls, v):
        if v is None or isinstance(v, datetime):
            return v
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(int(v), tz=timezone.utc)
        if isinstance(v, str):
            text = v.strip()
            if text.isdigit():
                return datetime.fromtimestamp(int(text), tz=timezone.utc)
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        raise ValueError("must be an ISO-8601 timestamp or unix epoch")


class Difference(BaseModel):
    account_id: str
    currency: str
    replayed: int
    expected: int
    delta: int


class IntegrityIssue(BaseModel):
    account_id: str
    currency: str
    folded_window: int
    stored_delta: int
    drift: int


class ReconcileReport(BaseModel):
    window_start: int
    window_end: int
    event_count: int
    replayed_balances: dict[str, dict[str, int]]
    differences: list[Difference]
    integrity_issues: list[IntegrityIssue]
