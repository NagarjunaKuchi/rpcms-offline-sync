from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Code = Annotated[str, Field(min_length=1, max_length=64)]
Ident = Annotated[str, Field(min_length=1, max_length=128)]


class RecordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clientRecordId: Ident  # unique per device; the retry-safe identity of the record
    progresId: Annotated[str, Field(min_length=3, max_length=64)]
    assistanceType: Code
    cycle: Code
    recordId: Ident  # the partner's own record reference (e.g. distribution record number)
    capturedAt: datetime
    amount: Annotated[float, Field(ge=0)] | None = None
    currency: Annotated[str, Field(min_length=3, max_length=3)] | None = None
    authTxnRef: Annotated[str, Field(max_length=128)] | None = None


class BatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clientBatchId: Ident
    deviceId: Ident
    records: Annotated[list[RecordIn], Field(min_length=1)]
