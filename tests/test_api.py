import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import create_app  # noqa: E402
from config import TestConfig  # noqa: E402
from loader import upsert_courses  # noqa: E402
from timeutils import parse_days, to_minutes  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "..", "data", "sample_courses.json")


@pytest.fixture
def client():
    app = create_app(TestConfig)
    with app.app_context():
        with open(DATA) as f:
            upsert_courses(json.load(f))
        yield app.test_client()


# ---- parsing ----

def test_parse_days():
    assert parse_days("M T W Tr F") == ["M", "T", "W", "R", "F"]
    assert parse_days("MWF") == ["M", "W", "F"]
    assert parse_days("TR") == ["T", "R"]
    assert parse_days("") == []


def test_to_minutes():
    assert to_minutes("14:20") == 14 * 60 + 20
    assert to_minutes("3:45 p") == 15 * 60 + 45
    assert to_minutes("2:20") == 14 * 60 + 20      # no am/pm, 1-7 -> PM
    assert to_minutes("9:00 am") == 9 * 60
    assert to_minutes("nonsense") is None


# ---- courses & filters ----

def test_filters(client):
    data = client.get("/api/filters").get_json()
    assert "CSE" in data["subjects"]
    assert "Second Half" in data["sessions"]
    assert "Online Synchronous" in data["modes_of_instruction"]
    assert set(data["careers"]) == {"Graduate", "Undergraduate"}


def test_search_by_name_and_code(client):
    assert [c["course_code"] for c in client.get("/api/courses?q=data struct").get_json()["courses"]] == ["CSE220"]
    assert [c["course_code"] for c in client.get("/api/courses?q=CSE 220").get_json()["courses"]] == ["CSE220"]


def test_wildcard_search(client):
    codes = [c["course_code"] for c in client.get("/api/courses?q=3*").get_json()["courses"]]
    assert codes == ["CSE310", "CSE350", "ENGL303"]


def test_course_filters(client):
    get = lambda qs: [c["course_code"] for c in client.get(f"/api/courses?{qs}").get_json()["courses"]]
    assert get("subject=Computer Science and Engineering (CSE)&career=Graduate") == ["CSE632"]
    assert get("session=Second Half") == ["ENGL303"]
    assert get("has_prereq=no") == ["ENGL303"]


def test_mode_filter_limits_sections(client):
    courses = client.get("/api/courses?mode=Online Asynchronous").get_json()["courses"]
    assert {c["course_code"] for c in courses} == {"CSE220", "ENGL303"}
    for c in courses:
        assert all(s["instruction_mode"] == "Online Asynchronous" for s in c["sections"])


def test_course_detail_and_placement(client):
    course = client.get("/api/courses/cse220").get_json()
    placements = {s["crn"]: s["placement"] for s in course["sections"]}
    assert placements == {"7587": "calendar", "7588": "calendar", "7589": "online"}
    assert course["hasPreReq"] is True
    sync = client.get("/api/sections/8350").get_json()
    assert sync["placement"] == "calendar"   # Online Synchronous stays on the calendar
    assert client.get("/api/courses/XYZ999").status_code == 404


def test_sections_lookup(client):
    data = client.get("/api/sections?crns=7587,0000").get_json()
    assert [s["crn"] for s in data["sections"]] == ["7587"]
    assert data["missing"] == ["0000"]


# ---- schedule generation ----

def _generate(client, **body):
    return client.post("/api/schedules/generate", json=body)


def test_generate_basic(client):
    data = _generate(client, course_codes=["CSE220", "CSE310"]).get_json()
    # CSE220: 7587 or 7589 (7588 is full); CSE310: LEC 8101 + LAB 8102 or 8103.
    # 7587 (MWF 14:20) conflicts with LAB 8102 (F 14:00) -> 3 schedules.
    assert data["count"] == 3
    assert data["total_credits"] == 7
    combos = {tuple(sorted(s["crns"])) for s in data["schedules"]}
    assert ("7587", "8101", "8102") not in combos
    for s in data["schedules"]:
        assert all(x["placement"] == "online" for x in s["online"])


def test_generate_include_full(client):
    data = _generate(client, course_codes=["CSE220"], include_full=True).get_json()
    assert data["count"] == 3


def test_generate_excluded_times(client):
    data = _generate(client, course_codes=["CSE220"],
                     excluded_times=[{"day": "MWF", "start": "14:00", "end": "16:00"}]).get_json()
    assert [s["crns"] for s in data["schedules"]] == [["7589"]]


def test_generate_impossible(client):
    data = _generate(client, course_codes=["CSE350"],
                     excluded_times=[{"day": "T", "start": "14:00", "end": "15:00"}]).get_json()
    assert data["count"] == 0
    assert data["problems"]


def test_generate_errors(client):
    assert _generate(client).status_code == 400
    assert _generate(client, course_codes=["NOPE1"]).status_code == 404
    bad = _generate(client, course_codes=["CSE220"], excluded_times=[{"day": "M", "start": "10:00"}])
    assert bad.status_code == 400
