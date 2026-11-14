# rpcms-offline-sync

Offline sync for RPCMS field officers. Devices capture assistance records without connectivity, then push them
here in batches. The service stores them durably, then a worker forwards each to the Duplicate Guard
([rpcms-dedup](https://github.com/NagarjunaKuchi/rpcms-dedup)) as an **offline** event, so duplicates caught
late become referrals (state `CONFLICT`) instead of being lost or silently paid twice.

## API (officer JWT with `sub` and `partner_id`; everything is scoped to the caller's partner)
| Endpoint | Purpose |
|---|---|
| `POST /api/v1/sync/batches` | Submit a batch (<= 500 records). 202 new, 200 identical resend, 409 if `clientBatchId` is reused with different content |
| `GET /api/v1/sync/batches/{deviceId}/{clientBatchId}` | Per-record state of a batch |
| `GET /api/v1/sync/summary` | Counts per state (what a "pending sync" badge needs) |
| `GET /api/v1/sync/records?state=` | List records, e.g. `CONFLICT` for follow-up |

Record states: `PENDING` -> `SYNCED` (recorded or replay) / `CONFLICT` (Guard opened a referral) / `REJECTED` (invalid, or a rejected duplicate) / `FAILED` (retries exhausted or permanent Guard error).

How it behaves:
- **Retry-safe by identity.** A record is identified by `(partner, device, clientRecordId)`. Re-sending a batch, or the same record in a later batch, never stores or forwards it twice. The Guard idempotency key is derived from that identity, so a retry always carries the same key.
- **One bad row does not block the batch.** Future `capturedAt` (beyond a 5 min skew) or a missing timezone marks that record `REJECTED` with a reason; the rest are accepted.
- **Oldest capture first.** Due records are forwarded in `capturedAt` order. Transient failures (network, 429, 5xx, bad Guard API key) back off exponentially (30 s doubling, capped at 1 h) up to 8 attempts, then `FAILED`.
- Responses show only the last 4 characters of the ProGres ID.

## Run
```
pip install -e ".[test]"
export RPCMS_SYNC_JWT_KEY=...  RPCMS_SYNC_GUARD_API_KEY=...  RPCMS_SYNC_IDEMPOTENCY_SALT=...   # each >=16 chars
export RPCMS_SYNC_DATA_KEYS=...    # Fernet key(s) for encrypting stored ProGres IDs (see Encryption at rest)
export RPCMS_SYNC_GUARD_URL=http://guard:8000  RPCMS_SYNC_DATABASE_URL=sqlite:///./sync.db
uvicorn --factory rpcms_offline_sync.api:create_app      # intake API
rpcms-sync-worker                                         # forwarder (run one or more)
pytest
```
Other settings: `_MAX_BATCH_RECORDS`, `_MAX_CLOCK_SKEW_SECONDS`, `_MAX_ATTEMPTS`, `_BACKOFF_BASE_SECONDS`, `_BACKOFF_MAX_SECONDS`, `_WORKER_BATCH_SIZE`, `_WORKER_POLL_SECONDS`, `_ADMIN_ROLE`, `_RETENTION_DAYS`.

## Database migrations
Schema changes are managed with Alembic (`migrations/`, initial revision `0001`). For anything beyond local development:
```
pip install -e ".[postgres]"
export RPCMS_SYNC_DATABASE_URL=postgresql+psycopg://user:pass@host/db  RPCMS_SYNC_CREATE_TABLES=false
alembic upgrade head
```
`RPCMS_SYNC_CREATE_TABLES` defaults to `true` (tables auto-created at startup, for development and tests); set it to `false` where Alembic owns the schema. A test checks that the migrations produce exactly the model schema. To run the suite against a migrated Postgres: `RPCMS_TEST_DATABASE_URL=... pytest` (it truncates the two tables between tests, so use a throwaway database).

## Encryption at rest
The ProGres ID and the auth reference are stored encrypted (Fernet, via the `cryptography` package) as `enc1:<token>`; the Guard still receives the real values and API responses still show only the last 4 characters.
- `RPCMS_SYNC_DATA_KEYS`: comma-separated Fernet keys. The first encrypts, all can decrypt. Generate one with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
- **Fail closed:** with no key the service refuses to start, unless `RPCMS_SYNC_ALLOW_PLAINTEXT_STORAGE=true` (development only).
- **Rotation:** put the new key first and keep the old one, restart, run `rpcms-sync-reencrypt`, then drop the old key. The same command upgrades plaintext rows after you first switch encryption on.
- A missing or wrong key raises an error on read; it never returns garbage or silently skips a row.
- Migration `0002` widens the two columns to hold ciphertext. Downgrading it on PostgreSQL fails while long encrypted values exist (data is not lost); scrub or empty the table first.
- **What this does and does not cover:** it protects the database and its backups against someone without the key. It does not protect against whoever holds the key or runs the service, the key must be kept outside the database, the `SCRUBBED` marker is stored in clear on purpose, and other columns (device, officer, partner, record IDs, timestamps, amounts) are not encrypted. Key storage (secrets manager/KMS) is not provided.

## Requeue and retention
- `POST /api/v1/sync/records/{deviceId}/{clientRecordId}/requeue` moves a `FAILED` record back to `PENDING` with a fresh retry budget. It needs the officer JWT to carry the role in `RPCMS_SYNC_ADMIN_ROLE` (default `sync-admin`) in a `roles` claim, and only reaches the caller's own partner's records (others get 404). Non-`FAILED` records return 409. Scrubbed records cannot be requeued.
- `RPCMS_SYNC_RETENTION_DAYS` is **off by default** (unset). When set, the worker blanks the ProGres ID (to `SCRUBBED`) and auth reference of `SYNCED`/`REJECTED` records older than that many days. The row stays so a late resend is still recognised. `PENDING`, `FAILED` and `CONFLICT` records are never scrubbed. The retention period itself is a programme policy decision that has not been made.

## End-to-end smoke test (development)
With the Duplicate Guard, this API and `rpcms-sync-worker` running on the same environment (all `RPCMS_*` variables set):

```bash
python scripts/e2e_smoke.py        # --sync / --guard to change the URLs
```
It signs a throwaway officer token with `RPCMS_SYNC_JWT_KEY` (claims `sub`, `exp`, `partner_id`), submits one test record, replays the batch, waits for the worker, and checks the record reached `SYNCED`. It fails (state `PENDING`) if the worker is not running. It writes test records into whichever databases you point it at, so do not aim it at real data.

## Known limits (read before relying on this)
- **No Redis.** The earlier architecture spec mentioned a Redis-backed queue; this uses a database table as the outbox (SQLite by default). That is durable and simple, but not what the spec said. A Redis or Celery front can be added without changing the API.
- **The Guard decides the winner by arrival order, not capture time.** The worker sends oldest capture first, but a record captured earlier on another device that syncs later will still become the `CONFLICT` side. Whether capture time should win is a policy decision for the programme.
- **AMP / NIMES are not involved.** Nothing here talks to them; their interfaces are unconfirmed.
- Requeue is per record only; there is no bulk requeue, and the `roles` claim name/format is an assumption about the token issuer.
- Several workers are safe (the Guard's idempotency makes a double send a replay) but there is no claim lock, so work may be duplicated.
- **Retention is off by default** (`RPCMS_SYNC_RETENTION_DAYS`), so encrypted ProGres IDs are kept indefinitely until a retention period is chosen. Encryption is described above; key management is yours to provide.
- Auth is the officer JWT only: no device attestation, and `deviceId` is trusted as sent.
- Migrations exist for the current schema only (one initial revision). Verified on PostgreSQL 16 locally (upgrade, downgrade, full test suite) but not on other versions or under load; no concurrent-worker test on Postgres.
- Verified end-to-end only against `rpcms-dedup` in-process, not over a network.
