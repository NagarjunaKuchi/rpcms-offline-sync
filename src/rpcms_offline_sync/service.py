"""Intake and forwarding logic. HTTP lives in api.py, the loop in worker.py."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import Settings
from .guard import GuardClient, TransientError
from .models import CONFLICT, FAILED, PENDING, REJECTED, SYNCED, Batch, Record
from .schemas import BatchIn, RecordIn


class SyncError(Exception):
    def __init__(self, status: int, title: str, detail: str = ""):
        super().__init__(title)
        self.status, self.title, self.detail = status, title, detail


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def batch_fingerprint(b: BatchIn) -> str:
    return hashlib.sha256(json.dumps(b.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _same_content(rec: Record, r: RecordIn) -> bool:
    return (rec.progres_id == r.progresId.strip().upper() and rec.assistance_type == r.assistanceType
            and rec.cycle == r.cycle and rec.record_id == r.recordId
            and _utc(rec.captured_at) == _utc(r.capturedAt)
            and (None if rec.amount is None else float(rec.amount)) == r.amount and rec.currency == r.currency)


def ingest(session: Session, settings: Settings, *, partner: str, officer: str, batch: BatchIn,
           now: datetime | None = None) -> tuple[Batch, list[dict], bool]:
    """Store a batch durably. Returns (batch, per-record items, created).
    Re-sending a batch (same id, same content) changes nothing and returns the current states."""
    now = now or datetime.now(timezone.utc)
    if len(batch.records) > settings.max_batch_records:
        raise SyncError(400, "Batch too large", f"at most {settings.max_batch_records} records per batch")
    fp = batch_fingerprint(batch)
    existing = session.scalar(select(Batch).where(
        Batch.partner_id == partner, Batch.device_id == batch.deviceId, Batch.client_batch_id == batch.clientBatchId))
    if existing is not None:
        if existing.fingerprint != fp:
            raise SyncError(409, "clientBatchId reused with different content")
        return existing, _items(session, partner, batch), False

    row = Batch(partner_id=partner, officer_id=officer, device_id=batch.deviceId,
                client_batch_id=batch.clientBatchId, fingerprint=fp, received_at=now)
    session.add(row)
    seen: dict[str, RecordIn] = {}
    for r in batch.records:
        if r.clientRecordId in seen:
            continue  # duplicate inside the batch: first occurrence wins, reported via _items
        seen[r.clientRecordId] = r
        if session.scalar(select(Record.id).where(Record.partner_id == partner, Record.device_id == batch.deviceId,
                                                  Record.client_record_id == r.clientRecordId)) is not None:
            continue  # already received in an earlier batch
        error = None
        if r.capturedAt.tzinfo is None:
            error = "capturedAt must include a timezone"
        elif _utc(r.capturedAt) > now + timedelta(seconds=settings.max_clock_skew_seconds):
            error = "capturedAt is in the future"
        row.records.append(Record(
            partner_id=partner, officer_id=officer, device_id=batch.deviceId, client_record_id=r.clientRecordId,
            progres_id=r.progresId.strip().upper(), assistance_type=r.assistanceType, cycle=r.cycle,
            record_id=r.recordId, captured_at=_utc(r.capturedAt), amount=r.amount,
            currency=r.currency.upper() if r.currency else None, auth_txn_ref=r.authTxnRef,
            state=REJECTED if error else PENDING, last_error=error, next_attempt_at=now))
    try:
        session.commit()
    except IntegrityError:  # concurrent identical request won the race
        session.rollback()
        existing = session.scalar(select(Batch).where(
            Batch.partner_id == partner, Batch.device_id == batch.deviceId, Batch.client_batch_id == batch.clientBatchId))
        if existing is None or existing.fingerprint != fp:
            raise SyncError(409, "clientBatchId reused with different content")
        return existing, _items(session, partner, batch), False
    return row, _items(session, partner, batch), True


def _items(session: Session, partner: str, batch: BatchIn) -> list[dict]:
    out, first_seen = [], {}
    for r in batch.records:
        rec = session.scalar(select(Record).where(Record.partner_id == partner, Record.device_id == batch.deviceId,
                                                  Record.client_record_id == r.clientRecordId))
        item = {"clientRecordId": r.clientRecordId, "state": rec.state, "error": rec.last_error,
                "referralId": rec.referral_id}
        if r.clientRecordId in first_seen and not _same_content(rec, r):
            item["error"] = "clientRecordId reused with different content in this batch"
        elif r.clientRecordId not in first_seen and not _same_content(rec, r):
            item["error"] = "clientRecordId already known with different content; stored version kept"
        first_seen[r.clientRecordId] = True
        out.append(item)
    return out


def idempotency_key(settings: Settings, rec: Record) -> str:
    raw = f"{settings.idempotency_salt}|{rec.partner_id}|{rec.device_id}|{rec.client_record_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


def process_due(session: Session, settings: Settings, guard: GuardClient, now: datetime | None = None) -> Counter:
    """Forward due PENDING records to the Duplicate Guard, oldest capture first.
    Safe to run from several workers: the Guard's idempotency key makes a double send a replay."""
    now = now or datetime.now(timezone.utc)
    due = session.scalars(select(Record).where(Record.state == PENDING, Record.next_attempt_at <= now)
                          .order_by(Record.captured_at, Record.id).limit(settings.worker_batch_size)).all()
    counts: Counter = Counter()
    for rec in due:
        event = {
            "idempotency_key": idempotency_key(settings, rec), "progres_id": rec.progres_id,
            "assistance_type": rec.assistance_type, "cycle": rec.cycle, "partner_id": rec.partner_id,
            "record_id": rec.record_id, "recorded_at": _utc(rec.captured_at).isoformat(),
            "amount": None if rec.amount is None else float(rec.amount), "currency": rec.currency,
            "auth_txn_ref": rec.auth_txn_ref, "officer_id": rec.officer_id, "offline": True,
        }
        try:
            res = guard.record(event)
        except TransientError as e:
            rec.attempts += 1
            rec.last_error = str(e)[:300]
            if rec.attempts >= settings.max_attempts:
                rec.state = FAILED
            else:
                delay = min(settings.backoff_base_seconds * 2 ** (rec.attempts - 1), settings.backoff_max_seconds)
                rec.next_attempt_at = now + timedelta(seconds=delay)
            counts["retry" if rec.state == PENDING else FAILED] += 1
            session.commit()
            continue
        rec.attempts += 1
        if res.outcome in ("RECORDED", "REPLAY"):
            rec.state, rec.last_error = SYNCED, None
        elif res.outcome == "CONFLICT":
            rec.state, rec.last_error = CONFLICT, None
        elif res.outcome == "REJECTED":
            rec.state, rec.last_error = REJECTED, res.detail
        else:
            rec.state, rec.last_error = FAILED, res.detail
        rec.guard_event_id, rec.referral_id = res.event_id, res.referral_id
        counts[rec.state] += 1
        session.commit()
    return counts


def summary(session: Session, partner: str) -> dict:
    rows = session.execute(select(Record.state).where(Record.partner_id == partner)).scalars().all()
    c = Counter(rows)
    return {s: c.get(s, 0) for s in (PENDING, SYNCED, CONFLICT, REJECTED, FAILED)} | {"total": len(rows)}
