from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import FakeGuard, GuardResult, TransientError, ago, auth, batch, make_settings, rec
from rpcms_offline_sync.api import create_app, make_session_factory
from rpcms_offline_sync.service import process_due


def submit(client, body, **kw):
    return client.post("/api/v1/sync/batches", json=body, headers=auth(**kw))


def states(client, **kw):
    r = client.get("/api/v1/sync/records", headers=auth(**kw))
    return {x["clientRecordId"]: x["state"] for x in r.json()}


def later(minutes=1):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def test_submit_is_durable_replayable_and_content_checked(client):
    body = batch([rec(1), rec(2)])
    r = submit(client, body)
    assert r.status_code == 202 and [x["state"] for x in r.json()["records"]] == ["PENDING", "PENDING"]
    again = submit(client, body)  # a client resends the very same payload
    assert again.status_code == 200 and len(client.get("/api/v1/sync/records", headers=auth()).json()) == 2
    changed = {**body, "records": [body["records"][0], {**body["records"][1], "amount": 1}]}
    assert submit(client, changed).status_code == 409


def test_process_maps_every_guard_outcome(client, settings, sf):
    submit(client, batch([rec(i) for i in range(1, 6)]))
    guard = FakeGuard([GuardResult("RECORDED", "EV-1"), GuardResult("REPLAY", "EV-2"),
                       GuardResult("CONFLICT", "EV-3", "REF-9"), GuardResult("REJECTED", detail="duplicate assistance"),
                       GuardResult("PERMANENT_ERROR", detail="guard returned 400")])
    with sf() as s:
        counts = process_due(s, settings, guard, later())
    assert counts == {"SYNCED": 2, "CONFLICT": 1, "REJECTED": 1, "FAILED": 1}
    assert list(states(client).values()) == ["SYNCED", "SYNCED", "CONFLICT", "REJECTED", "FAILED"]
    conflict = client.get("/api/v1/sync/records", params={"state": "CONFLICT"}, headers=auth()).json()
    assert conflict[0]["referralId"] == "REF-9"
    assert client.get("/api/v1/sync/summary", headers=auth()).json() == {
        "PENDING": 0, "SYNCED": 2, "CONFLICT": 1, "REJECTED": 1, "FAILED": 1, "total": 5}


def test_events_are_offline_oldest_first_normalised_with_stable_keys(client, settings, sf):
    submit(client, batch([rec(1, pid=" uga-1 ", captured=ago(5)), rec(2, captured=ago(50))]))
    g1, g2 = FakeGuard(), FakeGuard()
    with sf() as s:
        process_due(s, settings, g1, later())
    assert [e["record_id"] for e in g1.events] == ["DST-NHU-2", "DST-NHU-1"]  # oldest capture first
    e = g1.events[1]
    assert e["offline"] is True and e["progres_id"] == "UGA-1" and e["partner_id"] == "NHU" and e["officer_id"] == "off-1"
    assert len(e["idempotency_key"]) == 64 and g1.events[0]["idempotency_key"] != e["idempotency_key"]
    # same record from another partner gets a different key; keys never depend on retries
    other = make_settings(database_url="sqlite:///:memory:")
    sf2 = make_session_factory(other)
    c2 = TestClient(create_app(other, sf2))
    submit(c2, batch([rec(1, pid="UGA-1", captured=ago(5)), rec(2, captured=ago(50))]), partner="WNRP")
    with sf2() as s:
        process_due(s, other, g2, later())
    assert g2.events[1]["idempotency_key"] != e["idempotency_key"]


def test_transient_errors_back_off_then_fail(client, settings, sf):
    submit(client, batch([rec(1)]))
    guard = FakeGuard([TransientError("guard returned 503")] * 20)
    now = later()
    with sf() as s:
        assert process_due(s, settings, guard, now) == {"retry": 1}
        assert process_due(s, settings, guard, now) == {}  # not due yet: backoff holds it
    assert states(client) == {"rec-1": "PENDING"}
    for _ in range(settings.max_attempts - 1):
        now += timedelta(hours=2)
        with sf() as s:
            counts = process_due(s, settings, guard, now)
    assert counts == {"FAILED": 1}
    rec1 = client.get("/api/v1/sync/records", headers=auth()).json()[0]
    assert rec1["state"] == "FAILED" and rec1["attempts"] == settings.max_attempts and "503" in rec1["error"]


def test_transient_then_success_keeps_same_idempotency_key(client, settings, sf):
    submit(client, batch([rec(1)]))
    guard = FakeGuard([TransientError("timeout")])
    with sf() as s:
        process_due(s, settings, guard, later())
        process_due(s, settings, guard, later(120))
    assert len(guard.events) == 2 and guard.events[0]["idempotency_key"] == guard.events[1]["idempotency_key"]
    assert states(client) == {"rec-1": "SYNCED"}


def test_semantic_validation_marks_records_rejected_not_the_batch(client, settings, sf):
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    r = submit(client, batch([rec(1), rec(2, captured=future), rec(3, captured="2026-11-13T08:00:00")]))
    assert r.status_code == 202
    assert [x["state"] for x in r.json()["records"]] == ["PENDING", "REJECTED", "REJECTED"]
    assert "future" in r.json()["records"][1]["error"] and "timezone" in r.json()["records"][2]["error"]
    guard = FakeGuard()
    with sf() as s:
        process_due(s, settings, guard, later())
    assert len(guard.events) == 1  # rejected ones are never forwarded


def test_structural_validation_and_limits(client):
    assert submit(client, {"clientBatchId": "b", "deviceId": "d", "records": []}).status_code == 400
    assert submit(client, batch([{**rec(1), "name": "x"}])).status_code == 400
    assert submit(client, batch([rec(1)]) | {"extra": 1}).status_code == 400
    small = make_settings(max_batch_records=2)
    c = TestClient(create_app(small, make_session_factory(small)))
    assert submit(c, batch([rec(1), rec(2), rec(3)])).status_code == 400


def test_duplicate_client_ids_inside_and_across_batches(client):
    r1 = rec(1)
    r = submit(client, batch([r1, r1], bid="b1"))
    assert r.status_code == 202 and len(client.get("/api/v1/sync/records", headers=auth()).json()) == 1
    assert [x["error"] for x in r.json()["records"]] == [None, None]
    # the identical record resent in a later batch: not stored twice, no error
    r2 = submit(client, batch([r1, rec(2)], bid="b2"))
    assert r2.status_code == 202 and len(client.get("/api/v1/sync/records", headers=auth()).json()) == 2
    assert r2.json()["records"][0]["error"] is None
    # same id, different content: stored version kept and flagged
    r3 = submit(client, batch([{**r1, "amount": 999}], bid="b3"))
    assert "different content" in r3.json()["records"][0]["error"]
    r4 = submit(client, batch([r1, {**r1, "amount": 999}], bid="b4"))
    assert "different content" in r4.json()["records"][1]["error"]


def test_worker_run_once(client, settings, sf):
    from rpcms_offline_sync.worker import run_once

    submit(client, batch([rec(1)]))
    assert run_once(settings, sf, FakeGuard()) == {"SYNCED": 1}
    assert run_once(settings, sf, FakeGuard()) == {}


def test_partner_scoping_and_auth(client):
    submit(client, batch([rec(1)]))
    assert client.get("/api/v1/sync/batches/dev-9/batch-1", headers=auth()).status_code == 200
    assert client.get("/api/v1/sync/batches/dev-9/batch-1", headers=auth(partner="WNRP")).status_code == 404
    assert states(client, partner="WNRP") == {}
    assert client.get("/api/v1/sync/summary", headers=auth(partner="WNRP")).json()["total"] == 0
    assert client.post("/api/v1/sync/batches", json=batch([rec(1)])).status_code == 401
    assert submit(client, batch([rec(1)]), partner=None).status_code == 403
    assert client.get("/api/v1/sync/records", params={"state": "BOGUS"}, headers=auth()).status_code == 400


def test_views_never_expose_the_full_progres_id(client):
    submit(client, batch([rec(1, pid="UGA-00423318")]))
    text = client.get("/api/v1/sync/records", headers=auth()).text + client.get(
        "/api/v1/sync/batches/dev-9/batch-1", headers=auth()).text
    assert "UGA-00423318" not in text and "3318" in text


def test_real_duplicate_guard_end_to_end(tmp_path):
    pytest.importorskip("rpcms_dedup")
    from rpcms_dedup.api import create_app as guard_app
    from rpcms_dedup.config import Settings as GS
    from rpcms_dedup.db import init_schema, make_engine

    from rpcms_offline_sync.guard import HttpGuardClient

    gs = GS(database_url=f"sqlite:///{tmp_path}/g.db", hmac_key="k" * 40, api_key="guard-api-key-0123456")
    eng = make_engine(gs.database_url)
    init_schema(eng)
    http = TestClient(guard_app(gs, eng), headers={"X-API-Key": gs.api_key})
    guard = HttpGuardClient(http)

    s = make_settings()
    sf = make_session_factory(s)
    api = TestClient(create_app(s, sf))
    submit(api, batch([rec(1, captured=ago(30))]), partner="NHU")
    submit(api, batch([rec(1, captured=ago(20))], device="dev-2"), partner="WNRP")  # same person, same type+cycle
    with sf() as sess:
        counts = process_due(sess, s, guard, later())
    assert counts == {"SYNCED": 1, "CONFLICT": 1}
    nhu = client_states(api, "NHU"), client_states(api, "WNRP")
    assert nhu == ({"rec-1": "SYNCED"}, {"rec-1": "CONFLICT"})
    ref = api.get("/api/v1/sync/records", headers=auth(partner="WNRP")).json()[0]["referralId"]
    assert ref and http.get("/v1/referrals").status_code == 200
    with sf() as sess:  # nothing left to do, and a rerun changes nothing
        assert process_due(sess, s, guard, later(5)) == {}


def client_states(api, partner):
    return states(api, partner=partner)
