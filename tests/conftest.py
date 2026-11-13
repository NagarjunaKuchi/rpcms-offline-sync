import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient

from rpcms_offline_sync.api import create_app, make_session_factory
from rpcms_offline_sync.config import Settings
from rpcms_offline_sync.guard import GuardResult, TransientError

KEY = "test-signing-key-0123456789-abcdef"


def make_settings(**kw):
    base = dict(jwt_key=KEY, guard_api_key="guard-api-key-0123456", idempotency_salt="salt-0123456789-abcd",
                database_url="sqlite:///:memory:")
    return Settings(**{**base, **kw})


@pytest.fixture
def settings():
    return make_settings()


@pytest.fixture
def sf(settings):
    return make_session_factory(settings)


@pytest.fixture
def client(settings, sf):
    return TestClient(create_app(settings, sf))


def auth(partner="NHU", officer="off-1"):
    claims = {"sub": officer, "exp": int(time.time()) + 300}
    if partner:
        claims["partner_id"] = partner
    return {"Authorization": "Bearer " + jwt.encode(claims, KEY, algorithm="HS256")}


def ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def rec(n, pid="UGA-00423318", captured=None, **kw):
    return {"clientRecordId": f"rec-{n}", "progresId": pid, "assistanceType": "BIDIBIDI CASH", "cycle": "NOV-C12",
            "recordId": f"DST-NHU-{n}", "capturedAt": captured or ago(60 - n), "amount": 50000, "currency": "UGX", **kw}


def batch(records, bid="batch-1", device="dev-9"):
    return {"clientBatchId": bid, "deviceId": device, "records": records}


class FakeGuard:
    """Scripted Duplicate Guard: pop outcomes from a queue; default RECORDED."""

    def __init__(self, script=None):
        self.script, self.events = list(script or []), []

    def record(self, event):
        self.events.append(event)
        step = self.script.pop(0) if self.script else GuardResult("RECORDED", f"EV-{len(self.events)}")
        if isinstance(step, Exception):
            raise step
        return step


__all__ = ["FakeGuard", "GuardResult", "TransientError"]
