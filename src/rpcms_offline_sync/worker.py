"""Run: rpcms-sync-worker   (loops forever, forwarding due records to the Duplicate Guard)."""

from __future__ import annotations

import logging
import time

from .api import make_session_factory
from .config import Settings
from .guard import HttpGuardClient
from .service import process_due

log = logging.getLogger("rpcms.sync.worker")


def run_once(settings: Settings, session_factory, guard) -> dict:
    with session_factory() as s:
        return dict(process_due(s, settings, guard))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    sf, guard = make_session_factory(settings), HttpGuardClient.from_settings(settings)
    while True:
        try:
            counts = run_once(settings, sf, guard)
            if counts:
                log.info("processed: %s", counts)
                continue  # more may be due; do not sleep
        except Exception:  # keep the worker alive; records stay PENDING
            log.exception("worker cycle failed")
        time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
