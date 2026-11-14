"""End-to-end smoke test: offline-sync intake -> worker -> Duplicate Guard. Development only.

Needs the dedup service, the sync API and `rpcms-sync-worker` running with the same environment
(source secrets.sh). Uses RPCMS_SYNC_JWT_KEY from the environment to sign a test officer token; the
claims (sub, exp, partner_id) are the ones this service requires. Writes only test records.

    python scripts/e2e_smoke.py [--sync http://localhost:8002] [--guard http://localhost:8001]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync", default="http://localhost:8002")
    ap.add_argument("--guard", default="http://localhost:8001")
    ap.add_argument("--partner", default="SMOKE-TEST")
    ap.add_argument("--wait", type=int, default=45, help="seconds to wait for the worker")
    a = ap.parse_args()
    key = os.environ.get("RPCMS_SYNC_JWT_KEY")
    if not key:
        print("RPCMS_SYNC_JWT_KEY is not set (run: source ~/rpcms/secrets.sh)")
        return 2
    now = datetime.now(timezone.utc)
    token = jwt.encode({"sub": "smoke-officer", "partner_id": a.partner, "exp": now + timedelta(minutes=10)}, key, algorithm="HS256")
    h = {"Authorization": f"Bearer {token}"}
    run = uuid.uuid4().hex[:8]
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= bool(cond)
        print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))

    with httpx.Client(timeout=15) as c:
        for name, url in (("sync health", a.sync + "/healthz"), ("guard health", a.guard + "/healthz")):
            try:
                check(name, c.get(url).status_code == 200)
            except httpx.HTTPError as e:
                check(name, False, type(e).__name__)
                return 1
        check("no token is refused", c.get(a.sync + "/api/v1/sync/summary").status_code in (401, 403))
        body = {"clientBatchId": f"smoke-{run}", "deviceId": f"dev-{run}", "records": [{
            "clientRecordId": f"r1-{run}", "progresId": f"UGA-SMOKE-{run}", "assistanceType": "CASH-FOR-FOOD",
            "cycle": f"2026-11-{run}", "recordId": f"rec-{run}", "capturedAt": (now - timedelta(minutes=1)).isoformat(),
            "amount": 25000, "currency": "UGX"}]}
        r = c.post(a.sync + "/api/v1/sync/batches", json=body, headers=h)
        check("batch accepted (202)", r.status_code == 202, f"{r.status_code} {r.text[:200]}")
        r2 = c.post(a.sync + "/api/v1/sync/batches", json=body, headers=h)
        check("same batch again is a replay (200)", r2.status_code == 200, f"{r2.status_code}")
        state = None
        end = time.time() + a.wait
        while time.time() < end:
            recs = c.get(a.sync + "/api/v1/sync/records", headers=h).json()
            mine = [x for x in recs if x.get("clientRecordId") == f"r1-{run}"]
            state = mine[0].get("state") if mine else None
            if state and state not in ("PENDING",):
                break
            time.sleep(2)
        check("worker forwarded the record to the guard (state SYNCED)", state == "SYNCED", f"state={state}; is rpcms-sync-worker running?")
        s = c.get(a.sync + "/api/v1/sync/summary", headers=h)
        check("summary readable", s.status_code == 200, s.text[:200])
    print("\nRESULT:", "ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
