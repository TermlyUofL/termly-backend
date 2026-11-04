import io
import json
import urllib.parse

from app import create_app
from config import TestConfig
from courseleaf_live import (CourseLeafClient, fetch_term, parse_meeting_times,
                             parse_select_options, resolve_term, results_to_records)
from loader import upsert_courses

HOMEPAGE = """
<select id="crit-srcdb"><option value="4272">Spring 2027</option>
<option value="4268" selected>Fall 2026</option></select>
<select id="crit-subject"><option value="">Any</option>
<option value="CSE">Computer Science and Engineering (CSE)</option>
<option value="MATH">Mathematics (MATH)</option></select>
"""

SEARCH = {"srcdb": "4272", "count": 4, "results": [
    {"key": "1", "code": "CSE 220", "title": "Data Structures", "crn": "7587", "no": "01",
     "schd": "LEC", "stat": "A", "instr": "W. White", "isCancelled": "",
     "meetingTimes": json.dumps([{"meet_day": d, "start_time": "1300", "end_time": "1350"} for d in "024"])},
    {"key": "2", "code": "CSE 220", "title": "Data Structures", "crn": "7590", "no": "02",
     "schd": "LAB", "stat": "F", "instr": "Staff",
     "meetingTimes": json.dumps([{"meet_day": "1", "start_time": "930", "end_time": "1045"},
                                 {"meet_day": "3", "start_time": "930", "end_time": "1045"}])},
    {"key": "3", "code": "CSE 310", "title": "Discrete Structures", "crn": "8001", "no": "50",
     "schd": "LEC", "stat": "A", "instmode": "Online--Asynchronous", "meetingTimes": "[]"},
    {"key": "4", "code": "CSE 310", "title": "Discrete Structures", "crn": "8002",
     "schd": "LEC", "stat": "X", "isCancelled": "1", "meetingTimes": "[]"},
]}

DETAILS = {"title": "Data Structures", "hours": "3", "description": "<p>Lists, trees &amp; graphs.</p>",
           "registration_restrictions": "Prerequisite: CSE 130."}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_opener(calls):
    def opener(req, timeout=None):
        calls.append(req)
        url = req.full_url
        if "route=search" in url:
            body = json.loads(urllib.parse.unquote(req.data.decode()))
            crit = {c["field"]: c["value"] for c in body["criteria"]}
            assert crit["stat"] == "A,F,C,W" and "stat=A%2CF%2CC%2CW" in url
            subject = crit.get("subject")
            rows = [r for r in SEARCH["results"] if not subject or r["code"].startswith(subject + " ")]
            return FakeResponse(json.dumps({"results": rows}).encode())
        if "route=details" in url:
            return FakeResponse(json.dumps(DETAILS).encode())
        return FakeResponse(HOMEPAGE.encode())
    return opener


def test_parse_homepage_options():
    opts = parse_select_options(HOMEPAGE)
    assert opts["srcdb"] == [("4272", "Spring 2027"), ("4268", "Fall 2026")]
    assert [v for v, _ in opts["subject"]] == ["CSE", "MATH"]
    assert resolve_term(opts["srcdb"], "spring 2027") == "4272"
    assert resolve_term(opts["srcdb"], "4268") == "4268"


def test_meeting_times():
    assert parse_meeting_times(SEARCH["results"][0]["meetingTimes"]) == ("M W F", "13:00", "13:50")
    assert parse_meeting_times(SEARCH["results"][1]["meetingTimes"]) == ("T R", "09:30", "10:45")
    assert parse_meeting_times("") == ("", "", "")


def test_convert_results():
    records = {r["course_code"]: r for r in results_to_records(SEARCH["results"], {"7587": DETAILS})}
    cse220 = records["CSE220"]
    assert cse220["credit"] == 3 and cse220["hasPreReq"] is True
    assert cse220["description"] == "Lists, trees & graphs."
    lab = next(s for s in cse220["sections"] if s["crn"] == "7590")
    assert lab["status"] == "Full" and lab["section_type"] == "LAB"
    cse310 = records["CSE310"]
    assert [s["crn"] for s in cse310["sections"]] == ["8001"]   # cancelled one dropped
    assert cse310["sections"][0]["instruction_mode"] == "Online Asynchronous"


def test_end_to_end_sync_and_schedule():
    calls = []
    client = CourseLeafClient("https://example.test", delay=0, opener=fake_opener(calls))
    results, details = fetch_term(client, "4272", ["CSE"], with_details=True, log=lambda *_: None)
    assert len(results) == 4
    # one details call per course, not per section (cancelled CSE310 row skipped)
    assert sum("route=details" in c.full_url for c in calls) == 2

    app = create_app(TestConfig)
    with app.app_context():
        upsert_courses(results_to_records(results, details))
        api = app.test_client()
        course = api.get("/api/courses/CSE220").get_json()
        assert {s["crn"] for s in course["sections"]} == {"7587", "7590"}
        resp = api.post("/api/schedules/generate",
                        json={"course_codes": ["CSE220", "CSE310"], "include_full": True})
        data = resp.get_json()
        assert resp.status_code == 200 and data["count"] == 1
        assert set(data["schedules"][0]["crns"]) == {"7587", "7590", "8001"}
        assert data["schedules"][0]["online"][0]["crn"] == "8001"


# --data-raw from the request the live UofL site sent (Spring 2027)
LIVE_BODY = ("%7B%22other%22%3A%7B%22srcdb%22%3A%224272%22%7D%2C%22criteria%22%3A%5B%7B%22field%22"
             "%3A%22stat%22%2C%22value%22%3A%22A%2CF%2CC%2CW%22%7D%5D%7D")
LIVE_URL = "https://courseleafpath.louisville.edu/api/?page=fose&route=search&stat=A%2CF%2CC%2CW"


def test_request_matches_live_site_exactly():
    calls = []
    client = CourseLeafClient(delay=0, opener=fake_opener(calls))
    client.search_raw("4272")
    req = calls[0]
    assert req.full_url == LIVE_URL
    assert req.data.decode() == LIVE_BODY
    assert req.get_header("X-requested-with") == "XMLHttpRequest"
    assert req.get_header("Content-type") == "application/json"


def test_browser_sync_builds_same_request():
    from courseleaf_browser_sync import known_search
    assert known_search("https://courseleafpath.louisville.edu", "4272") == (LIVE_URL, LIVE_BODY)


def test_whole_term_in_one_request():
    calls = []
    client = CourseLeafClient("https://example.test", delay=0, opener=fake_opener(calls))
    results, _ = fetch_term(client, "4272", None, log=lambda *_: None)
    searches = [c for c in calls if "route=search" in c.full_url and "instmode" not in c.full_url]
    assert len(results) == 4 and len(searches) == 1
