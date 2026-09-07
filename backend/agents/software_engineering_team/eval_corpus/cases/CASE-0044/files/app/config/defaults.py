"""Process-wide defaults."""

from __future__ import annotations

from typing import Any

REQUEST_TIMEOUT_SECONDS = 30

MAX_CONCURRENCY = 8

RETRY_POLICY: dict[str, Any] = {
    "max_attempts": 5,
    "initial_backoff_seconds": 1.0,
    "backoff_multiplier": 2.0,

DEFAULT_REGION = "us-east-1"
