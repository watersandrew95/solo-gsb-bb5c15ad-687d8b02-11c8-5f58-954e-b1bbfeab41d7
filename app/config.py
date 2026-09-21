"""Application configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str = os.getenv("LEDGER_DB_PATH", "/data/ledger.db")
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "whsec_dev_secret_change_me")
    signature_tolerance_seconds: int = int(
        os.getenv("SIGNATURE_TOLERANCE_SECONDS", "300")
    )
    replay_window_seconds: int = int(os.getenv("REPLAY_WINDOW_SECONDS", "86400"))


settings = Settings()
