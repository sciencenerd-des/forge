"""DurableQueue plain-table fallback: works on the stock stack (no pgmq)."""
from forge_runtime.durable_queue import DurableQueue


def test_fallback_backend_on_sqlite(sqlite_db):
    assert DurableQueue(sqlite_db).backend() == "fallback"


def test_fallback_send_read_archive(sqlite_db):
    q = DurableQueue(sqlite_db)
    msg_id = q.send({"job": "consolidate", "key": "p:consolidate:d"})
    assert msg_id is not None

    msg = q.read(vt=60)
    assert msg is not None and msg.payload["job"] == "consolidate"
    # Hidden while the visibility timeout holds.
    assert q.read(vt=60) is None

    assert q.archive(msg.msg_id)
    assert q.read(vt=60) is None  # archived messages never reappear


def test_fallback_redelivery_after_visibility_timeout(sqlite_db):
    q = DurableQueue(sqlite_db)
    q.send({"job": "backfill"})
    first = q.read(vt=0)  # becomes visible again immediately
    assert first is not None
    again = q.read(vt=60)
    assert again is not None and again.msg_id == first.msg_id


def test_queue_failures_degrade_to_none(sqlite_db, monkeypatch):
    """A broken session must yield 'no message', never an exception."""
    q = DurableQueue(sqlite_db, queue="forge_hygiene")

    def dead(*a, **kw):
        raise RuntimeError("connection lost")

    monkeypatch.setattr(sqlite_db, "execute", dead)
    assert q.send({"job": "x"}) is None
    assert q.read() is None
    assert q.archive(1) is False
