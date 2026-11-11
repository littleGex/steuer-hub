import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import settings, storage
from app.db import session_scope
from app.models import Document, Person, WorkDay
from app.people import ensure_people
from worker import run as worker_run

from .helpers import INVOICE_LINES, make_pdf


@pytest.fixture()
def client(env):
    from app.main import app

    with TestClient(app) as c:
        yield c


def upload(client, name="rechnung.pdf", data=None, category="arbeitsmittel", person_id="", year=2026):
    data = data if data is not None else make_pdf(INVOICE_LINES)
    return client.post("/upload", data={"year": str(year), "category": category, "person_id": person_id},
                       files=[("files", (name, data, "application/pdf"))], follow_redirects=False)


def docs():
    with session_scope() as s:
        return list(s.scalars(select(Document).order_by(Document.id)))


def person_id(year=2026):
    with session_scope() as s:
        return ensure_people(s, year)[0].id


def test_dashboard_renders_with_default_person(client):
    r = client.get("/")
    assert r.status_code == 200 and "Person 1" in r.text
    for path in ("/sections", "/documents", "/workdays", "/people", "/review", "/export"):
        assert client.get(path).status_code == 200, path


def test_full_flow_upload_read_review_signoff_export(client, env):
    r = upload(client)
    assert r.status_code == 303
    [d] = docs()
    assert d.status == "pending" and d.stored_path.startswith("2026/arbeitsmittel/")
    assert (env / "nas" / d.stored_path).is_file()

    worker_run.run_once()
    [d] = docs()
    assert (d.status, d.amount_cents, d.vendor) == ("processed", 11900, "Stadtwerke Bremen AG")

    assert "119,00 €" in client.get("/sections").text
    csv_draft = client.get("/export/summary.csv")
    assert "DRAFT" in csv_draft.headers["content-disposition"]
    assert "119,00" in csv_draft.text and csv_draft.text.startswith("﻿")

    # sign-off needs: no open items + confirmation
    assert client.post("/review/approve-all", data={"year": "2026"}, follow_redirects=False).status_code == 303
    assert docs()[0].status == "approved"
    client.post("/review/signoff", data={"year": "2026"}, follow_redirects=False)
    assert "DRAFT" in client.get("/export/summary.csv").headers["content-disposition"]   # no confirm -> not signed
    client.post("/review/signoff", data={"year": "2026", "confirm": "1"}, follow_redirects=False)
    assert "SIGNED-OFF" in client.get("/export/summary.csv").headers["content-disposition"]

    # a later change invalidates the sign-off
    client.post(f"/documents/{d.id}", data={"category": "arbeitsmittel", "amount": "200,00", "doc_date": "2026-03-03",
                                           "vendor": "Stadtwerke", "action": "save"}, follow_redirects=False)
    assert "DRAFT" in client.get("/export/summary.csv").headers["content-disposition"]
    assert "Figures have changed" in client.get("/review").text


def test_duplicate_upload_is_skipped(client):
    upload(client)
    upload(client, name="same-content-other-name.pdf")
    assert len(docs()) == 1


def test_rejects_bad_extension_and_category(client):
    upload(client, name="evil.exe", data=b"MZ")
    upload(client, category="../../etc")
    assert docs() == []


def test_path_traversal_filename_stays_inside_nas(client, env):
    upload(client, name="../../evil.pdf")
    [d] = docs()
    assert storage.abs_path(d).is_relative_to((env / "nas").resolve())
    assert not (env / "evil.pdf").exists() and ".." not in d.stored_path


def test_cross_origin_post_is_blocked(client):
    r = client.post("/workdays/set", data={"person_id": str(person_id()), "kind": "home"},
                    headers={"Origin": "http://evil.example"}, follow_redirects=False)
    assert r.status_code == 403


def test_basic_auth(env, monkeypatch):
    monkeypatch.setenv("STEUER_USER", "alan")
    monkeypatch.setenv("STEUER_PASSWORD", "s3cret")
    settings.reload_settings()
    from app.main import app

    with TestClient(app) as c:
        assert c.get("/").status_code == 401
        assert c.get("/", auth=("alan", "wrong")).status_code == 401
        assert c.get("/", auth=("alan", "s3cret")).status_code == 200
        assert c.get("/healthz").status_code == 200   # health check stays open for Docker


def test_workdays_set_and_range(client):
    pid = person_id()
    client.post("/workdays/set", data={"person_id": str(pid), "day": "2026-03-02", "kind": "home"}, follow_redirects=False)
    client.post("/workdays/set", data={"person_id": str(pid), "day": "2026-03-02", "kind": "office"}, follow_redirects=False)
    client.post("/workdays/range", data={"person_id": str(pid), "start": "2026-03-09", "end": "2026-03-15",
                                         "kind": "home", "weekdays_only": "1"}, follow_redirects=False)
    with session_scope() as s:
        rows = {w.day.isoformat(): w.kind for w in s.scalars(select(WorkDay))}
    assert rows["2026-03-02"] == "office"                 # overwritten, not duplicated
    assert sum(k == "home" for k in rows.values()) == 5   # Mon-Fri only
    client.post("/workdays/set", data={"person_id": str(pid), "day": "2026-03-02", "kind": "clear"}, follow_redirects=False)
    with session_scope() as s:
        assert s.get(WorkDay, (pid, __import__("datetime").date(2026, 3, 2))) is None
    assert client.get("/workdays?year=2026&m=3").status_code == 200
    # wrong-year person is refused
    client.post("/workdays/set", data={"person_id": str(pid), "day": "2027-01-04", "kind": "home"}, follow_redirects=False)
    with session_scope() as s:
        assert not [w for w in s.scalars(select(WorkDay)) if w.day.year == 2027]


def test_people_crud_and_steuerklasse_warning(client):
    pid = person_id()
    client.post("/people/add", data={"year": "2026", "name": "Partner", "steuerklasse": "3", "commute_km": "12"},
                follow_redirects=False)
    assert "Steuerklassen 1/3" in client.get("/people").text
    client.post(f"/people/{pid}", data={"name": "Alan", "steuerklasse": "5", "commute_km": "20"}, follow_redirects=False)
    assert "do not form" not in client.get("/people").text
    # invalid input is rejected
    client.post(f"/people/{pid}", data={"name": "Alan", "steuerklasse": "9"}, follow_redirects=False)
    with session_scope() as s:
        assert s.get(Person, pid).steuerklasse == 5
    # a person with documents cannot be deleted
    upload(client, person_id=str(pid))
    client.post(f"/people/{pid}/delete", follow_redirects=False)
    with session_scope() as s:
        assert s.get(Person, pid) is not None


def test_csv_formula_injection_is_neutralised(client):
    upload(client)
    worker_run.run_once()
    d = docs()[0]
    client.post(f"/documents/{d.id}", data={"category": "arbeitsmittel", "amount": "10,00", "doc_date": "2026-01-02",
                                           "vendor": '=HYPERLINK("http://evil")', "action": "save"}, follow_redirects=False)
    text = client.get("/export/documents.csv").text
    assert "'=HYPERLINK" in text and ";=HYPERLINK" not in text


def test_recategorise_moves_file_and_delete_goes_to_trash(client, env):
    upload(client)
    worker_run.run_once()
    d = docs()[0]
    client.post(f"/documents/{d.id}", data={"category": "schulgeld", "amount": "1000,00", "doc_date": "2026-03-03",
                                           "vendor": "School", "action": "approve"}, follow_redirects=False)
    d = docs()[0]
    assert d.category == "schulgeld" and d.stored_path.startswith("2026/schulgeld/") and d.status == "approved"
    assert (env / "nas" / d.stored_path).is_file()
    assert "300,00 €" in client.get("/sections").text          # 30 % of the school fees
    client.post(f"/documents/{d.id}/delete", follow_redirects=False)
    assert docs() == []
    assert list((env / "nas" / "2026" / "_trash").glob("*.pdf"))   # original kept


def test_inbox_files_are_registered_and_must_be_sorted(client, env):
    inbox = env / "nas" / "2026" / "_inbox"
    inbox.mkdir(parents=True)
    (inbox / "scan.pdf").write_bytes(make_pdf(INVOICE_LINES))
    (inbox / ".hidden.pdf").write_bytes(b"x")
    worker_run.run_once()
    worker_run.run_once()                                       # second pass must not re-register
    [d] = docs()
    assert d.category == "unsorted" and d.status == "processed"
    client.post(f"/documents/{d.id}/approve", follow_redirects=False)
    assert docs()[0].status == "processed"                      # refused while unsorted
    client.post(f"/documents/{d.id}", data={"category": "versicherung", "amount": "119,00", "doc_date": "2026-03-03",
                                           "action": "approve"}, follow_redirects=False)
    d = docs()[0]
    assert d.status == "approved" and d.stored_path.startswith("2026/versicherung/")
    assert not (inbox / "scan.pdf").exists()


def test_original_file_is_served(client):
    upload(client)
    d = docs()[0]
    r = client.get(f"/documents/{d.id}/file")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")


def test_missing_nas_marker_blocks_uploads(client, env):
    (env / "nas" / storage.MARKER).unlink()
    assert client.get("/healthz").status_code == 503
    assert "NAS problem" in client.get("/").text
    upload(client)
    assert docs() == []
    worker_run.run_once()   # worker pauses instead of marking everything as failed


def test_nightly_backup(client, env):
    upload(client)
    first = worker_run.backup_db()
    assert first and first.is_file() and first.parent.name == "_backups"
    assert worker_run.backup_db() is None                        # only once per day


def _march(pid):
    from datetime import date
    with session_scope() as s:
        return {w.day: w.kind for w in s.scalars(select(WorkDay).where(
            WorkDay.person_id == pid, WorkDay.day >= date(2026, 3, 1), WorkDay.day <= date(2026, 3, 31)))}


def test_workdays_month_cards(client):
    from datetime import date
    pid = person_id()
    json = {"Accept": "application/json"}
    # March 2026 has 22 weekdays. One day is already marked away and must stay untouched.
    client.post("/workdays/set", data={"person_id": str(pid), "day": "2026-03-02", "kind": "away"}, follow_redirects=False)

    r = client.post("/workdays/month", data={"person_id": pid, "year": 2026, "month": 3, "office": "12", "home": "8"},
                    headers=json)
    body = r.json()
    assert r.status_code == 200 and body["ok"]
    assert body["month"] == {**body["month"], "office": 12, "home": 8, "away": 1, "weekdays": 22, "free": 1}
    assert body["totals"] == {"office": 12, "home": 8, "away": 1}
    rows = _march(pid)
    assert rows[date(2026, 3, 2)] == "away"
    assert all(d.weekday() < 5 for d in rows)                       # weekends never filled
    office = sorted(d for d, k in rows.items() if k == "office")
    assert office[0] == date(2026, 3, 3)                            # earliest free weekdays first

    # Lowering one count and raising the other in one request frees days first, then reuses them.
    r = client.post("/workdays/month", data={"person_id": pid, "year": 2026, "month": 3, "office": "4", "home": "17"},
                    headers=json)
    assert r.json()["ok"] and r.json()["month"]["office"] == 4 and r.json()["month"]["home"] == 17
    rows = _march(pid)
    kept = sorted(d for d, k in rows.items() if k == "office")
    assert kept == office[:4]                                       # lowering clears the latest days

    # More than the free weekdays is refused and nothing changes.
    before = _march(pid)
    r = client.post("/workdays/month", data={"person_id": pid, "year": 2026, "month": 3, "home": "20"}, headers=json)
    assert r.status_code == 400 and not r.json()["ok"] and "free weekday" in r.json()["error"]
    assert _march(pid) == before

    # Bad input and a person from another year are refused.
    assert client.post("/workdays/month", data={"person_id": pid, "year": 2026, "month": 3, "home": "-1"},
                       headers=json).status_code == 400
    assert client.post("/workdays/month", data={"person_id": pid, "year": 2027, "month": 3, "home": "1"},
                       headers=json).status_code == 400

    # Without JavaScript the form posts and redirects back.
    r = client.post("/workdays/month", data={"person_id": pid, "year": 2026, "month": 4, "home": "3",
                                             "next": f"/workdays?year=2026&person={pid}"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/workdays?year=2026&person={pid}"

    # The year view shows the cards and the totals.
    page = client.get(f"/workdays?year=2026&person={pid}").text
    assert page.count("data-month") == 12 and 'data-total="home">20<' in page


def test_workdays_month_cross_origin_blocked(client):
    r = client.post("/workdays/month", data={"person_id": person_id(), "year": 2026, "month": 3, "home": "5"},
                    headers={"Origin": "http://evil.example", "Accept": "application/json"}, follow_redirects=False)
    assert r.status_code == 403
