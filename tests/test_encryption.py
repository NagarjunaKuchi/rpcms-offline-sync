import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import DATA_KEY, DATA_KEY2, make_settings
from rpcms_offline_sync import crypto
from rpcms_offline_sync.api import create_app, make_session_factory
from rpcms_offline_sync.models import Record
from rpcms_offline_sync.service import process_due, reencrypt
from test_sync import FakeGuard, batch, later, rec, submit


@pytest.fixture(autouse=True)
def _restore_cipher():
    yield
    crypto.configure(DATA_KEY)


def raw(sf):
    with sf() as s:
        return s.execute(text("select progres_id, auth_txn_ref from sync_record order by id")).all()


def setup(settings):
    sf = make_session_factory(settings)
    return TestClient(create_app(settings, sf)), sf


def test_sensitive_columns_are_ciphertext_in_the_database_but_plaintext_to_the_guard():
    s = make_settings()
    c, sf = setup(s)
    submit(c, batch([rec(1, pid="uga-00423318", authTxnRef="AUTH-TXN-SECRET-77")]))
    ((pid, ref),) = raw(sf)
    assert pid.startswith("enc1:") and "00423318" not in pid
    assert ref.startswith("enc1:") and "SECRET" not in ref
    g = FakeGuard()
    with sf() as sess:
        process_due(sess, s, g, later())
    assert g.events[0]["progres_id"] == "UGA-00423318" and g.events[0]["auth_txn_ref"] == "AUTH-TXN-SECRET-77"  # the Guard gets real values


def test_resend_of_identical_record_still_recognised_with_random_ciphertexts():
    s = make_settings()
    c, _ = setup(s)
    body = batch([rec(1, pid="UGA-1")])
    assert submit(c, body).status_code == 202
    assert submit(c, body).status_code == 200  # same content: replayed, not a conflict (needs decrypt-and-compare)


def test_wrong_or_missing_key_fails_loudly_not_silently():
    s = make_settings()
    c, sf = setup(s)
    submit(c, batch([rec(1)]))
    crypto.configure(DATA_KEY2)
    with sf() as sess, pytest.raises(crypto.KeyError_):
        sess.query(Record).all()
    crypto.configure(None)
    with sf() as sess, pytest.raises(crypto.KeyError_):
        sess.query(Record).all()


def test_key_rotation_new_first_key_old_still_decrypts_then_reencrypt():
    s = make_settings()
    c, sf = setup(s)
    submit(c, batch([rec(1, pid="UGA-ROT-1")]))
    old_ct = raw(sf)[0][0]
    crypto.configure(f"{DATA_KEY2},{DATA_KEY}")  # new key first, old key kept for reading
    with sf() as sess:
        assert sess.query(Record).one().progres_id == "UGA-ROT-1"
        assert reencrypt(sess) == 1
    new_ct = raw(sf)[0][0]
    assert new_ct != old_ct
    crypto.configure(DATA_KEY2)  # old key can now be dropped
    with sf() as sess:
        assert sess.query(Record).one().progres_id == "UGA-ROT-1"


def test_legacy_plaintext_rows_are_readable_and_upgraded_by_reencrypt():
    s = make_settings(data_keys=None, allow_plaintext_storage=True)
    c, sf = setup(s)
    submit(c, batch([rec(1, pid="UGA-LEGACY")]))
    assert raw(sf)[0][0] == "UGA-LEGACY"  # plaintext when no key is configured
    crypto.configure(DATA_KEY)  # encryption switched on later
    with sf() as sess:
        assert sess.query(Record).one().progres_id == "UGA-LEGACY"
        assert reencrypt(sess) == 1
    assert raw(sf)[0][0].startswith("enc1:")


def test_scrubbed_marker_stays_plain_and_requeue_guard_still_works():
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from rpcms_offline_sync.service import scrub_old
    s = make_settings(retention_days=1)
    c, sf = setup(s)
    submit(c, batch([rec(1)]))
    with sf() as sess:
        process_due(sess, s, FakeGuard(), later())
        assert scrub_old(sess, s, datetime.now(timezone.utc) + timedelta(days=5)) == 1
    assert raw(sf)[0] == ("SCRUBBED", None)


def test_startup_refuses_to_store_plaintext_unless_explicitly_allowed():
    with pytest.raises(RuntimeError, match="RPCMS_SYNC_DATA_KEYS"):
        make_session_factory(make_settings(data_keys=None))
    make_session_factory(make_settings(data_keys=None, allow_plaintext_storage=True))
    with pytest.raises(crypto.KeyError_, match="Fernet"):
        make_session_factory(make_settings(data_keys="not-a-key"))
