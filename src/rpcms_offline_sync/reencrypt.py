"""CLI: rpcms-sync-reencrypt. Re-encrypts stored sensitive fields with the primary key in RPCMS_SYNC_DATA_KEYS."""
from .api import make_session_factory
from .config import Settings
from .service import reencrypt


def main() -> None:
    sf = make_session_factory(Settings())
    with sf() as s:
        print(f"re-encrypted {reencrypt(s)} records")


if __name__ == "__main__":
    main()
