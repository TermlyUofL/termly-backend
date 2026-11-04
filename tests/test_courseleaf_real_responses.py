"""Sync against responses captured from the live UofL site (Spring 2027).

Real search rows have an empty `meetingTimes` and no instruction mode, so
meeting times must come from the details `all_sections` grid and modes from
instmode-filtered searches.
"""
import io
import json
import urllib.error
import urllib.parse
from pathlib import Path

from app import create_app
from config import TestConfig
from courseleaf_live import CourseLeafClient, fetch_term, parse_meets, results_to_records
from loader import upsert_courses
from models import Course, Section, db

FIXTURES = Path(__file__).parent / "fixtures"
DETAILS = {code: json.loads((FIXTURES / f"courseleaf-details-4272-{code.replace(' ', '-')}.json").read_text())
           for code in ("CSE 130", "CSE 310")}
# the search rows CourseLeaf returns for these courses (echoed in details.allInGroup)
ROWS = [row for det in DETAILS.values() for row in det["allInGroup"]]
MODES = {"OA": {"3571", "4056"}, "P": {"2521", "2987", "5831", "5832"}}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener(calls, fail_first=0):
    def open_(req, timeout=None):
        calls.append(req)
        if len(calls) <= fail_first:
            raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)
        body = json.loads(urllib.parse.unquote(req.data.decode()))
        if "route=details" in req.full_url:
            return FakeResponse(json.dumps(DETAILS[body["group"].split(":", 1)[1]]).encode())
        crit = {c["field"]: c["value"] for c in body["criteria"]}
        rows = [r for r in ROWS if "instmode" not in crit or r["crn"] in MODES.get(crit["instmode"], ())]
        return FakeResponse(json.dumps({"srcdb": "4272", "count": len(rows), "results": rows}).encode())
    return open_


def sync(calls, **kw):
    client = CourseLeafClient("https://example.test", delay=0, opener=opener(calls, **kw))
    return fetch_term(client, "4272", None, with_details=True, log=lambda *_: None)


def test_real_rows_have_no_meeting_times():
    assert all(r["meetingTimes"] == "" for r in ROWS)


def test_parse_meets_formats():
    assert parse_meets("MWF 9-9:50a") == ("M W F", "09:00", "09:50")
    assert parse_meets("TR 11a-12:15p") == ("T R", "11:00", "12:15")
    assert parse_meets("TR 11-12:15p") == ("T R", "11:00", "12:15")
    assert parse_meets("MWF 12-1:15p") == ("M W F", "12:00", "13:15")
    assert parse_meets("MW 10-10:50a; F 10-10:50a") == ("M W F", "10:00", "10:50")
    assert parse_meets("Does Not Meet") == ("", "", "")
    assert parse_meets("TBA") == ("", "", "")


def test_sections_get_times_modes_and_course_fields():
    calls = []
    results, details = sync(calls)
    # one whole-term search, four mode searches, one group details call per course
    detail_bodies = [json.loads(urllib.parse.unquote(c.data.decode())) for c in calls
                     if "route=details" in c.full_url]
    assert sorted(b["group"] for b in detail_bodies) == ["code:CSE 130", "code:CSE 310"]
    assert all(b["key"] == "" for b in detail_bodies)
    assert len(calls) == 1 + 4 + 2

    records = {r["course_code"]: r for r in results_to_records(results, details)}
    cse310 = records["CSE310"]
    assert cse310["course_name"] == "Discrete Structures"
    assert cse310["course_career"] == "Undergraduate"
    assert cse310["credit"] == 3 and cse310["course_session"] == "Regular Full"
    sec = {s["crn"]: s for s in cse310["sections"]}
    assert (sec["2521"]["meeting_days"], sec["2521"]["start_time"], sec["2521"]["end_time"]) == \
        ("T R", "11:00", "12:15")
    assert sec["2521"]["instruction_mode"] == "In Person"
    assert sec["3571"]["meeting_days"] == "" and sec["3571"]["instruction_mode"] == "Online Asynchronous"

    labs = [s for s in records["CSE130"]["sections"] if s["section_type"] == "LAB"]
    assert {(s["meeting_days"], s["start_time"]) for s in labs} == {("T", "14:00"), ("R", "14:00")}


def test_schedules_from_real_data():
    results, details = sync([])
    app = create_app(TestConfig)
    with app.app_context():
        upsert_courses(results_to_records(results, details))
        data = app.test_client().post("/api/schedules/generate", json={
            "course_codes": ["CSE130", "CSE310"], "modes": ["In Person"]}).get_json()
        assert data["count"] > 0
        assert all(s["online"] == [] for s in data["schedules"])
        assert {b["crn"] for b in data["schedules"][0]["calendar"]} >= {"2987", "2521"}


def test_retries_server_errors():
    calls = []
    results, _ = sync(calls, fail_first=2)
    assert len(results) == len(ROWS)


def test_ignored_mode_filter_leaves_modes_empty():
    calls = []

    def ignoring(req, timeout=None):
        calls.append(req)
        if "route=details" in req.full_url:
            return FakeResponse(b"[]")
        return FakeResponse(json.dumps({"count": len(ROWS), "results": ROWS}).encode())

    client = CourseLeafClient("https://example.test", delay=0, opener=ignoring)
    results, _ = fetch_term(client, "4272", None, log=lambda *_: None)
    assert not any(r.get("instmode") for r in results)


def test_crn_moving_to_another_course_is_kept():
    app = create_app(TestConfig)
    with app.app_context():
        base = {"subject": "CSE", "course_name": "X", "credit": 3}
        upsert_courses([{**base, "course_number": "100", "sections": [{"crn": "1"}]},
                        {**base, "course_number": "200", "sections": []}])
        # CRN 1 now belongs to CSE200; CSE100 is processed after it
        upsert_courses([{**base, "course_number": "200", "sections": [{"crn": "1"}]},
                        {**base, "course_number": "100", "sections": []}])
        assert db.session.get(Section, "1").course_code == "CSE200"
        assert Course.query.count() == 2
