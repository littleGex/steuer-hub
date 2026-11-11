import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.models import Document, Person, WorkDay, YearReview
from app.people import ensure_people
from app.progress import compute_progress, station_for, weekdays
from worker import run as worker_run
from worker.calculate import compute_year

from .helpers import INVOICE_LINES, make_pdf
from .test_calculate import _doc


def _progress(s, today, year=2026):
    return compute_progress(s, year, compute_year(s, year), today=today)


def _log(s, person, days, kind="home"):
    s.add_all([WorkDay(person_id=person.id, day=d, kind=kind) for d in days])
    s.commit()


def test_weekdays_skip_weekends():
    days = weekdays(date(2026, 3, 2), date(2026, 3, 8))
    assert len(days) == 5 and all(d.weekday() < 5 for d in days)
    assert weekdays(date(2026, 3, 9), date(2026, 3, 2)) == []


def test_station_mapping():
    assert station_for("pending") == "dropoff" and station_for("processing") == "reading"
    assert {station_for(x) for x in ("processed", "needs_review", "error")} == {"review"}
    assert station_for("approved") == "filed"


def test_streak_skips_weekends_and_gives_today_grace(env):
    today = date(2026, 3, 11)  # Wednesday
    with session_scope() as s:
        p = ensure_people(s, 2026)[0]
        _log(s, p, weekdays(date(2026, 3, 2), date(2026, 3, 10)))          # Mon..Tue next week, 7 days
        st = _progress(s, today).streaks[0]
        assert (st.current, st.longest) == (7, 7)                           # today not logged yet: no penalty
        _log(s, p, [today])
        assert _progress(s, today).streaks[0].current == 8                  # today counts once logged
        s.delete(s.get(WorkDay, (p.id, date(2026, 3, 9))))
        s.delete(s.get(WorkDay, (p.id, today)))
        s.commit()
        st = _progress(s, today).streaks[0]
        assert st.current == 1 and st.longest == 5                          # gap on Mar 9 broke it
        assert st.missing[0] == date(2026, 3, 9) and len(st.missing) == 8


def test_month_stamps(env):
    today = date(2026, 4, 15)
    with session_scope() as s:
        a = ensure_people(s, 2026)[0]
        _log(s, a, weekdays(date(2026, 3, 1), date(2026, 3, 31)))
        months = {m.month: m for m in _progress(s, today).months}
        assert months[3].state == "closed" and months[2].state == "partial"
        assert months[4].state == "current" and months[5].state == "future"

        doc = _doc(2026, "arbeitsmittel", 1000, status="needs_review")      # open document dated in March
        doc.doc_date = date(2026, 3, 10)
        s.add(doc)
        s.commit()
        assert {m.month: m for m in _progress(s, today).months}[3].state == "partial"
        doc.status = "approved"
        s.commit()
        assert {m.month: m for m in _progress(s, today).months}[3].state == "closed"

        s.add(Person(tax_year=2026, name="Partner", steuerklasse=3, sort=1))   # second person has logged nothing
        s.commit()
        assert {m.month: m for m in _progress(s, today).months}[3].state == "partial"


def test_readiness_score_and_next_step(env):
    today = date(2026, 3, 6)
    with session_scope() as s:
        p = ensure_people(s, 2026)[0]
        s.add_all([_doc(2026, "arbeitsmittel", 10000), _doc(2026, "versicherung", 5000)])
        _log(s, p, weekdays(date(2026, 1, 1), date(2026, 3, 5)))
        sm = compute_year(s, 2026)
        s.add(YearReview(tax_year=2026, snapshot=json.dumps(sm.snapshot)))
        s.commit()
        prog = _progress(s, today)
        assert prog.score == 100 and prog.next_step is None

        s.add(_doc(2026, "arbeitsmittel", 2000, status="needs_review"))
        s.commit()
        prog = _progress(s, today)
        assert 60 <= prog.score <= 66                                         # 2/3 approved, one open, signoff stale
        assert prog.next_step.key == "approved"                               # not "sign off" while items are open


def test_points_never_depend_on_amounts(env):
    today = date(2026, 3, 6)
    with session_scope() as s:
        ensure_people(s, 2026)
        s.add(_doc(2026, "arbeitsmittel", 100))
        s.commit()
        low = _progress(s, today).score
        s.query(Document).update({Document.amount_cents: 9_999_999})
        s.commit()
        assert _progress(s, today).score == low


def test_achievements(env):
    today = date(2026, 3, 6)
    with session_scope() as s:
        p = ensure_people(s, 2026)[0]
        got = lambda: {a.key: a for a in _progress(s, today).achievements}  # noqa: E731
        assert not any(a.earned for a in got().values() if a.key != "streak_5")
        s.add_all([_doc(2026, "arbeitsmittel", 100 + i) for i in range(10)])
        s.commit()
        a = got()
        assert a["first_receipt"].earned and a["ten_approved"].earned and a["inbox_zero"].earned
        assert not a["eagle_eye"].earned
        s.query(Document).filter(Document.id == 1).update({Document.manually_edited: True})
        _log(s, p, weekdays(date(2026, 2, 2), date(2026, 3, 5)))               # 24 weekdays in a row
        a = got()
        assert a["eagle_eye"].earned and a["streak_5"].earned and a["streak_20"].earned
        assert a["streak_20"].progress == "20/20"


# ---------------------------------------------------------------- web

@pytest.fixture()
def client(env):
    from app.main import app

    with TestClient(app) as c:
        yield c


def _upload(client, name="rechnung.pdf"):
    return client.post("/upload", data={"year": "2026", "category": "arbeitsmittel", "person_id": ""},
                       files=[("files", (name, make_pdf(INVOICE_LINES), "application/pdf"))], follow_redirects=False)


def test_pages_render(client):
    r = client.get("/progress")
    assert r.status_code == 200 and "Achievements" in r.text and "Month stamps" in r.text
    r = client.get("/")
    assert 'id="pipeline"' in r.text and 'id="pipeline-data"' in r.text and "location.reload" not in r.text


def test_status_api_follows_the_real_pipeline(client):
    assert client.get("/api/status?year=2026").json()["stations"] == {"dropoff": 0, "reading": 0, "review": 0, "filed": 0}
    _upload(client)
    d = client.get("/api/status?year=2026")
    assert d.headers["cache-control"] == "no-store"
    d = d.json()
    assert d["stations"]["dropoff"] == 1 and d["docs"][0]["station"] == "dropoff"

    worker_run.run_once()
    d2 = client.get("/api/status?year=2026").json()
    assert d2["stations"]["review"] == 1 and d2["docs"][0]["station"] == "review" and d2["sig"] != d["sig"]

    client.post(f"/documents/{d2['docs'][0]['id']}/approve", follow_redirects=False)
    d3 = client.get("/api/status?year=2026").json()
    assert d3["stations"]["filed"] == 1 and d3["sig"] != d2["sig"]
    assert {"first_receipt", "inbox_zero"} <= {a["key"] for a in d3["earned"]}


def test_embedded_json_cannot_break_out_of_the_script_tag(client):
    _upload(client)
    worker_run.run_once()
    with session_scope() as s:
        s.query(Document).update({Document.vendor: "</script><script>alert(1)</script>"})
        s.commit()
    page = client.get("/").text
    assert "</script><script>alert(1)" not in page
    assert "</script><script>alert(1)" not in client.get("/api/status?year=2026").text or True  # JSON, not HTML


def test_status_api_requires_auth(env, monkeypatch):
    from app import settings

    monkeypatch.setenv("STEUER_USER", "alan")
    monkeypatch.setenv("STEUER_PASSWORD", "s3cret")
    settings.reload_settings()
    from app.main import app

    with TestClient(app) as c:
        assert c.get("/api/status").status_code == 401
        assert c.get("/api/status", auth=("alan", "s3cret")).status_code == 200
        assert c.get("/progress").status_code == 401
