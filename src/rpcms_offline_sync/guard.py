"""Client for the Duplicate Guard (rpcms-dedup) POST /v1/assistance-events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx


class TransientError(Exception):
    """Try again later (network problem, timeout, 429/5xx)."""


@dataclass(frozen=True)
class GuardResult:
    outcome: str  # RECORDED | REPLAY | CONFLICT | REJECTED | PERMANENT_ERROR
    event_id: str | None = None
    referral_id: str | None = None
    detail: str | None = None


class GuardClient(Protocol):
    def record(self, event: dict) -> GuardResult: ...


class HttpGuardClient:
    def __init__(self, http: httpx.Client):
        self._http = http  # base_url and X-API-Key header are set by the factory

    @classmethod
    def from_settings(cls, s) -> "HttpGuardClient":
        return cls(httpx.Client(base_url=s.guard_url, timeout=s.guard_timeout_seconds,
                                headers={"X-API-Key": s.guard_api_key}))

    def record(self, event: dict) -> GuardResult:
        try:
            r = self._http.post("/v1/assistance-events", json=event)
        except httpx.HTTPError as e:  # timeouts, connection errors
            raise TransientError(f"guard unreachable: {type(e).__name__}") from e
        if r.status_code in (200, 201, 202):
            body = r.json()
            outcome = {200: "REPLAY", 201: "RECORDED", 202: "CONFLICT"}[r.status_code]
            return GuardResult(outcome, body.get("event_id"), body.get("referral_id"))
        if r.status_code == 429 or r.status_code >= 500:
            raise TransientError(f"guard returned {r.status_code}")
        if r.status_code == 401:
            raise TransientError("guard rejected our API key (401): fix configuration")
        detail = r.text[:300]
        try:
            code = r.json().get("code")
        except ValueError:
            code = None
        if r.status_code == 409 and code == "DUPLICATE_ASSISTANCE":
            return GuardResult("REJECTED", detail="duplicate assistance")
        if r.status_code == 422:
            return GuardResult("REJECTED", detail=f"guard validation: {detail}")
        return GuardResult("PERMANENT_ERROR", detail=f"guard returned {r.status_code}: {detail}")
