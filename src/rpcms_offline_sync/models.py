from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Record states
PENDING, SYNCED, CONFLICT, REJECTED, FAILED = "PENDING", "SYNCED", "CONFLICT", "REJECTED", "FAILED"


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Batch(Base):
    __tablename__ = "sync_batch"
    __table_args__ = (UniqueConstraint("partner_id", "device_id", "client_batch_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    partner_id: Mapped[str] = mapped_column(String(64))
    officer_id: Mapped[str] = mapped_column(String(64))
    device_id: Mapped[str] = mapped_column(String(128))
    client_batch_id: Mapped[str] = mapped_column(String(128))
    fingerprint: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    records: Mapped[list["Record"]] = relationship(back_populates="batch", order_by="Record.id")


class Record(Base):
    __tablename__ = "sync_record"
    __table_args__ = (
        UniqueConstraint("partner_id", "device_id", "client_record_id"),
        Index("ix_sync_record_due", "state", "next_attempt_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("sync_batch.id"))
    partner_id: Mapped[str] = mapped_column(String(64))
    officer_id: Mapped[str] = mapped_column(String(64))
    device_id: Mapped[str] = mapped_column(String(128))
    client_record_id: Mapped[str] = mapped_column(String(128))
    progres_id: Mapped[str] = mapped_column(String(64))
    assistance_type: Mapped[str] = mapped_column(String(64))
    cycle: Mapped[str] = mapped_column(String(64))
    record_id: Mapped[str] = mapped_column(String(128))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    amount: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    auth_txn_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)

    state: Mapped[str] = mapped_column(String(16), default=PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    guard_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    referral_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    batch: Mapped[Batch] = relationship(back_populates="records")
