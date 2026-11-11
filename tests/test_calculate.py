from datetime import date

from app import config
from app.db import session_scope
from app.models import Document, Person, WorkDay
from app.people import ensure_people, steuerklasse_warnings
from worker.calculate import compute_year


def _doc(year, cat, cents, person_id=None, status="approved", n=[0]):
    n[0] += 1
    return Document(tax_year=year, category=cat, person_id=person_id, original_name="x.pdf",
                    stored_path=f"{year}/{cat}/x{n[0]}.pdf", sha256=f"{n[0]:064d}", size_bytes=1,
                    status=status, amount_cents=cents)


def test_config_is_consistent(env):
    assert config.validate() == []


def test_single_person_totals(env):
    with session_scope() as s:
        p = ensure_people(s, 2026)[0]
        p.commute_km = 10
        s.add_all([
            _doc(2026, "arbeitsmittel", 10000),
            _doc(2026, "schulgeld", 100000),          # 30 % -> 300,00
            _doc(2026, "handwerker", 1000000),        # 20 % = 2.000 -> capped at 1.200
            _doc(2026, "versicherung", 50000, status="needs_review"),
        ])
        s.add_all([WorkDay(person_id=p.id, day=date(2026, 3, d), kind="home") for d in range(2, 7)])     # 5 home
        s.add_all([WorkDay(person_id=p.id, day=date(2026, 4, d), kind="office") for d in range(1, 5)])   # 4 office
        s.commit()
        sm = compute_year(s, 2026)

    sec = {x.key: x for x in sm.sections}
    assert sec["sonderausgaben_sonstige"].total_cents == 30000
    assert sec["steuerermaessigung"].total_cents == 120000
    assert sec["sonderausgaben_vorsorge"].total_cents == 50000
    assert sec["sonderausgaben_vorsorge"].open_count == 1
    ps = sm.people[0]
    assert ps.home_cents == 3000                       # 5 x 6 EUR
    assert ps.commute_cents == 1520                    # 4 x 10 km x 0.38
    assert ps.werbungskosten_cents == 10000 + 3000 + 1520
    assert not ps.above_pauschbetrag
    assert sm.open_items == 1


def test_home_office_cap(env):
    with session_scope() as s:
        p = ensure_people(s, 2026)[0]
        d = date(2026, 1, 1)
        from datetime import timedelta
        s.add_all([WorkDay(person_id=p.id, day=d + timedelta(days=i), kind="home") for i in range(230)])
        s.commit()
        ps = compute_year(s, 2026).people[0]
    assert ps.home_days == 230 and ps.home_counted_days == 210 and ps.home_cents == 126000


def test_two_people_split_and_unassigned(env):
    with session_scope() as s:
        a = ensure_people(s, 2026)[0]
        a.name, a.steuerklasse = "Alan", 4
        b = Person(tax_year=2026, name="Partner", steuerklasse=3, sort=1)
        s.add(b)
        s.commit()
        s.add_all([_doc(2026, "arbeitsmittel", 20000, a.id), _doc(2026, "arbeitsmittel", 5000, b.id),
                   _doc(2026, "arbeitsmittel", 1000, None)])
        s.commit()
        sm = compute_year(s, 2026)
    by = {p.person.name: p for p in sm.people}
    assert by["Alan"].doc_cents == 20000 and by["Partner"].doc_cents == 5000
    assert sm.needs_person == 1 and sm.open_items == 1
    wk = next(x for x in sm.sections if x.key == "werbungskosten")
    assert wk.total_cents == 26000
    assert any("Steuerklassen 3/4" in w for w in sm.warnings)   # III + IV is not a valid pair


def test_steuerklasse_pairs():
    mk = lambda *k: [Person(name=f"p{i}", steuerklasse=c) for i, c in enumerate(k)]  # noqa: E731
    assert steuerklasse_warnings(mk(3, 5)) == []
    assert steuerklasse_warnings(mk(4, 4)) == []
    assert steuerklasse_warnings(mk(1, 1)) == []
    assert steuerklasse_warnings(mk(3, 4))
    assert steuerklasse_warnings([Person(name="x", steuerklasse=3, faktor=0.9)])


def test_people_copied_to_next_year(env):
    with session_scope() as s:
        a = ensure_people(s, 2026)[0]
        a.name, a.steuerklasse, a.commute_km = "Alan", 4, 12
        s.commit()
        nxt = ensure_people(s, 2027)
    assert nxt[0].name == "Alan" and nxt[0].steuerklasse == 4 and nxt[0].commute_km == 12


def test_investment_documents_are_never_counted(env):
    with session_scope() as s:
        p = ensure_people(s, 2026)[0]
        s.add_all([
            _doc(2026, "kapitalertraege", 123456, person_id=p.id),
            _doc(2026, "kapitalertraege", 999, status="needs_review"),
            _doc(2026, "arbeitsmittel", 10000),
        ])
        s.flush()
        sm = compute_year(s, 2026)
    secs = {sec.key: sec for sec in sm.sections}
    kap = secs["kapitalertraege"]
    assert kap.documents_only and kap.doc_count == 2 and kap.open_count == 1
    assert kap.total_cents == 0 and kap.approved_cents == 0
    assert sm.people[0].doc_cents == 10000                       # Werbungskosten unaffected
    assert sm.snapshot["sections"]["kapitalertraege"] == 2       # a new certificate shows up as a change
