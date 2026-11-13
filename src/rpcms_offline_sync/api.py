from __future__ import annotations

import jwt
from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from . import service
from .config import Settings
from .models import Base, Batch, Record
from .schemas import BatchIn
from .service import SyncError

PROBLEM = "application/problem+json"
STATES = ("PENDING", "SYNCED", "CONFLICT", "REJECTED", "FAILED")


def _problem(status: int, title: str, detail: str = "") -> JSONResponse:
    return JSONResponse({"type": "about:blank", "title": title, "status": status, "detail": detail},
                        status_code=status, media_type=PROBLEM)


def make_session_factory(settings: Settings):
    kwargs = {}
    if settings.database_url.startswith("sqlite") and ":memory:" in settings.database_url:
        kwargs = {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
    engine = create_engine(settings.database_url, **kwargs)
    Base.metadata.create_all(engine)  # no migrations yet: see README
    return sessionmaker(engine, expire_on_commit=False)


def record_view(r: Record) -> dict:
    return {"clientRecordId": r.client_record_id, "deviceId": r.device_id, "progresIdSuffix": r.progres_id[-4:],
            "assistanceType": r.assistance_type, "cycle": r.cycle, "recordId": r.record_id, "state": r.state,
            "attempts": r.attempts, "error": r.last_error, "referralId": r.referral_id,
            "capturedAt": r.captured_at.isoformat()}


def create_app(settings: Settings | None = None, session_factory=None) -> FastAPI:
    settings = settings or Settings()
    Session_ = session_factory or make_session_factory(settings)
    app = FastAPI(title="RPCMS offline sync", version="0.1.0")

    def db():
        with Session_() as s:
            yield s

    def claims(authorization: str | None = Header(default=None)) -> dict:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise SyncError(401, "Missing bearer token")
        try:
            c = jwt.decode(authorization[7:].strip(), settings.jwt_key, algorithms=[settings.jwt_algorithm],
                           audience=settings.jwt_audience,
                           options={"require": ["sub", "exp"], "verify_aud": settings.jwt_audience is not None})
        except jwt.PyJWTError:
            raise SyncError(401, "Invalid token")
        if not c.get("partner_id"):
            raise SyncError(403, "Token has no partner_id")
        return c

    def caller(c: dict = Depends(claims)) -> tuple[str, str]:
        return c["sub"], c["partner_id"]

    def admin(c: dict = Depends(claims)) -> tuple[str, str]:
        roles = c.get("roles") or []
        if settings.admin_role not in (roles if isinstance(roles, list) else [roles]):
            raise SyncError(403, "Admin role required")
        return c["sub"], c["partner_id"]

    @app.exception_handler(SyncError)
    async def _e(_: Request, e: SyncError):
        return _problem(e.status, e.title, e.detail)

    @app.exception_handler(RequestValidationError)
    async def _v(_: Request, e: RequestValidationError):
        return _problem(400, "Invalid request", "; ".join(f"{'.'.join(map(str, x['loc'][1:]))}: {x['msg']}" for x in e.errors()))

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.post("/api/v1/sync/batches")
    def submit(body: BatchIn, response: Response, who=Depends(caller), s: Session = Depends(db)):
        officer, partner = who
        batch, items, created = service.ingest(s, settings, partner=partner, officer=officer, batch=body)
        response.status_code = 202 if created else 200
        return {"clientBatchId": batch.client_batch_id, "deviceId": batch.device_id,
                "receivedAt": batch.received_at.isoformat(), "records": items}

    @app.get("/api/v1/sync/batches/{device_id}/{client_batch_id}")
    def get_batch(device_id: str, client_batch_id: str, who=Depends(caller), s: Session = Depends(db)):
        b = s.scalar(select(Batch).where(Batch.partner_id == who[1], Batch.device_id == device_id,
                                         Batch.client_batch_id == client_batch_id))
        if b is None:
            raise SyncError(404, "Batch not found")
        return {"clientBatchId": b.client_batch_id, "deviceId": b.device_id, "records": [record_view(r) for r in b.records]}

    @app.post("/api/v1/sync/records/{device_id}/{client_record_id}/requeue")
    def requeue(device_id: str, client_record_id: str, who=Depends(admin), s: Session = Depends(db)):
        return record_view(service.requeue(s, who[1], device_id, client_record_id))

    @app.get("/api/v1/sync/summary")
    def summary(who=Depends(caller), s: Session = Depends(db)):
        return service.summary(s, who[1])

    @app.get("/api/v1/sync/records")
    def records(state: str | None = None, limit: int = 100, who=Depends(caller), s: Session = Depends(db)):
        if state is not None and state not in STATES:
            raise SyncError(400, "Invalid request", f"state must be one of {', '.join(STATES)}")
        q = select(Record).where(Record.partner_id == who[1]).order_by(Record.captured_at, Record.id).limit(min(max(limit, 1), 500))
        if state:
            q = q.where(Record.state == state)
        return [record_view(r) for r in s.scalars(q)]

    return app
