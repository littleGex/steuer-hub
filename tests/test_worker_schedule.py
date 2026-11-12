"""The worker must leave the NAS alone when there is nothing to do, so NAS disks can spin down."""
from datetime import datetime

from app import storage
from app.db import session_scope
from app.models import Document
from worker import run as worker_run

from .helpers import INVOICE_LINES, make_pdf


def _count_nas_checks(monkeypatch):
    calls = []
    real = storage.nas_status

    def counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(storage, "nas_status", counting)
    return calls


def test_idle_tick_does_not_touch_the_nas(env, monkeypatch):
    calls = _count_nas_checks(monkeypatch)
    now = datetime(2026, 3, 10, 14, 0)
    sched = worker_run.Schedule(next_scan=1000.0, last_backup_day=now.date())
    for t in range(0, 900, 5):                        # 15 idle minutes of 5 s ticks
        worker_run.tick(sched, mono=float(t), now=now)
    assert calls == []


def test_inbox_scan_runs_on_its_own_interval(env, monkeypatch):
    calls = _count_nas_checks(monkeypatch)
    now = datetime(2026, 3, 10, 14, 0)
    sched = worker_run.Schedule(last_backup_day=now.date())
    worker_run.tick(sched, mono=0.0, now=now)         # first tick scans
    assert len(calls) == 1 and sched.next_scan == 900.0
    for t in range(5, 900, 5):
        worker_run.tick(sched, mono=float(t), now=now)
    assert len(calls) == 1                            # nothing until the interval is up
    worker_run.tick(sched, mono=900.0, now=now)
    assert len(calls) == 2


def test_pending_upload_is_read_without_waiting_for_the_scan(env, monkeypatch):
    sha = "a" * 64
    path = env / "nas" / "2026" / "arbeitsmittel" / "r.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(make_pdf(INVOICE_LINES))
    with session_scope() as s:
        s.add(Document(tax_year=2026, category="arbeitsmittel", original_name="r.pdf",
                       stored_path="2026/arbeitsmittel/r.pdf", sha256=sha, size_bytes=1))
    now = datetime(2026, 3, 10, 14, 0)
    sched = worker_run.Schedule(next_scan=10_000.0, last_backup_day=now.date())
    worker_run.tick(sched, mono=5.0, now=now)
    with session_scope() as s:
        [d] = s.query(Document).all()
        assert d.status in ("processed", "needs_review") and d.amount_cents == 11900
    assert sched.next_scan == 10_000.0                # the inbox scan was not pulled forward


def test_backup_once_a_day_after_the_hour(env, monkeypatch):
    made = []
    monkeypatch.setattr(worker_run, "backup_db", lambda now=None: made.append(now))
    sched = worker_run.Schedule(next_scan=10_000.0)
    worker_run.tick(sched, mono=1.0, now=datetime(2026, 3, 10, 2, 0))   # before STEUER_BACKUP_HOUR (3)
    assert made == []
    for t in range(2, 50):
        worker_run.tick(sched, mono=float(t), now=datetime(2026, 3, 10, 4, 0))
    assert len(made) == 1
    worker_run.tick(sched, mono=60.0, now=datetime(2026, 3, 11, 4, 0))
    assert len(made) == 2


def test_nas_down_retries_scan_later_without_hammering(env, monkeypatch):
    monkeypatch.setattr(storage, "nas_status", lambda: (False, "down"))
    now = datetime(2026, 3, 10, 14, 0)
    sched = worker_run.Schedule(last_backup_day=now.date())
    worker_run.tick(sched, mono=0.0, now=now)
    assert sched.nas_ok is False and sched.next_scan == 60.0


def test_status_check_is_cached(env, monkeypatch):
    calls = _count_nas_checks(monkeypatch)
    monkeypatch.setattr(storage, "_status_cache", None)
    for _ in range(10):
        assert storage.nas_status_cached()[0] is True
    assert len(calls) == 1


def test_backup_never_opens_sqlite_on_the_nas(env, monkeypatch):
    """SQLite locking does not work over SMB/NFS: a backup written there hung the worker forever on a real NAS.
    The copy must be made on local disk and only the finished file copied to the NAS."""
    import sqlite3

    from app.settings import get_settings
    nas = str(get_settings().nas_root.resolve())
    real_connect = sqlite3.connect

    def guarded(path, *a, **kw):
        assert not str(path).startswith(nas) and not str(__import__("pathlib").Path(path).resolve()).startswith(nas), \
            f"sqlite opened a file on the NAS: {path}"
        return real_connect(path, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", guarded)
    dest = worker_run.backup_db(datetime(2026, 3, 10, 4, 0))
    assert dest is not None and dest.is_file() and dest.parent == get_settings().backup_dir
    monkeypatch.setattr(sqlite3, "connect", real_connect)
    assert real_connect(dest).execute("pragma integrity_check").fetchone()[0] == "ok"
    assert not list(get_settings().backup_dir.glob("*.tmp")) and not list(get_settings().data_dir.glob("*.bak-tmp"))
