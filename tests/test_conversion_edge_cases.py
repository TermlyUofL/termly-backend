import json
import os

import pytest

from app import create_app
from config import TestConfig
from courseleaf_live import results_to_records
from loader import upsert_courses


def slot(day, start, end):
    return {"meet_day": day, "start_time": start, "end_time": end}


ROWS = [
    # cross-listed: same CRN under two codes
    {"code": "CSE 545", "title": "Artificial Intelligence", "crn": "5000", "schd": "LEC", "stat": "A",
     "sess": "Regular Full Term", "meetingTimes": json.dumps([slot("1", "1100", "1215"), slot("3", "1100", "1215")])},
    {"code": "ECE 545", "title": "Artificial Intelligence", "crn": "5000", "schd": "LEC", "stat": "A",
     "meetingTimes": "[]"},
    # TBA slot comes through as 0/0 -> no meeting times, goes to the Online box
    {"code": "CSE 699", "title": "Independent Study " + "x" * 300, "crn": "5001", "schd": "IND", "stat": "A",
     "sess": "1st Half of Term", "instr": "Y" * 150, "meetingTimes": json.dumps([slot("0", "0", "0")])},
    # second section of CSE 545, different mode, details fetched for 5000 only
    {"code": "CSE 545", "title": "Artificial Intelligence", "crn": "5002", "schd": "LEC", "enrl_stat": "C",
     "stat": "A", "meetingTimes": json.dumps([slot("0", "1800", "1915")])},
]
DETAILS = {"5000": {"_crn": "5000", "hours": "3", "instmode": "Blended",
                    "registration_restrictions": "Pre-req: CSE 310"},
           "5002": {"_crn": "5000", "hours": "3", "instmode": "Blended",
                    "registration_restrictions": "Pre-req: CSE 310"}}


def test_edge_cases():
    recs = {r["course_code"]: r for r in results_to_records(ROWS, DETAILS)}
    assert "ECE545" not in recs                                    # CRN kept under first code only
    ai = recs["CSE545"]
    assert ai["course_session"] == "Regular Full" and ai["hasPreReq"] and ai["credit"] == 3
    s5000, s5002 = ai["sections"]
    assert (s5000["meeting_days"], s5000["start_time"]) == ("T R", "11:00")
    assert s5000["instruction_mode"] == "Blended"
    assert s5002["instruction_mode"] == "In Person"                # 5000's mode not copied to 5002
    assert s5002["status"] == "Full"                               # enrl_stat C = closed
    ind = recs["CSE699"]
    assert ind["course_session"] == "First Half"
    assert len(ind["course_name"]) == 255 and len(ind["sections"][0]["instructor_name"]) == 100
    assert ind["sections"][0]["meeting_days"] == "" and ind["sections"][0]["start_time"] == ""


def _app():
    url = os.getenv("TERMLY_TEST_MARIADB_URL")
    if not url:
        return create_app(TestConfig)

    class MariaConfig(TestConfig):
        SQLALCHEMY_DATABASE_URI = url
    return create_app(MariaConfig)


def test_load_and_schedule_in_database():
    """Runs on SQLite, or on MariaDB when TERMLY_TEST_MARIADB_URL is set."""
    app = _app()
    with app.app_context():
        from models import db
        db.session.execute(db.text("DELETE FROM sections"))
        db.session.execute(db.text("DELETE FROM courses"))
        db.session.commit()
        assert upsert_courses(results_to_records(ROWS, DETAILS)) == (2, 3)
        api = app.test_client()
        online = api.get("/api/courses/CSE699").get_json()["sections"][0]
        assert online["placement"] == "online"
        resp = api.post("/api/schedules/generate", json={"course_codes": ["CSE545", "CSE699"]})
        data = resp.get_json()
        assert resp.status_code == 200
        assert [s["crns"] for s in data["schedules"]] == [["5000", "5001"]]   # 5002 is full
        assert api.get("/api/filters").get_json()["sessions"] == ["First Half", "Regular Full"]
