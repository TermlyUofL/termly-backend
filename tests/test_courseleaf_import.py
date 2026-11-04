import json
from app import create_app
from config import TestConfig
from courseleaf_import import import_file, normalize_courseleaf


def test_normalize_flat_rows():
    rows = [
        {"code": "CSE 220", "title": "Data Structures", "credits": 3, "crn": "12345", "days": "MWF", "startTime": "13:00", "endTime": "13:50"},
        {"code": "CSE 220", "title": "Data Structures", "credits": 3, "crn": "12346", "days": "TR", "startTime": "14:00", "endTime": "15:15"},
    ]
    result = normalize_courseleaf(rows)
    assert len(result) == 1
    assert result[0]["course_code"] == "CSE220"
    assert len(result[0]["sections"]) == 2


def test_import_and_search(tmp_path):
    export = tmp_path / "courseleaf.json"
    export.write_text(json.dumps([{"course_code": "CSE220", "course_name": "Data Structures", "subject": "CSE", "course_number": "220", "credit": 3, "sections": [{"crn": "12345", "meeting_days": "MWF", "start_time": "13:00", "end_time": "13:50", "status": "Not Full", "instruction_mode": "In Person"}]}]))
    app = create_app(TestConfig)
    with app.app_context():
        assert import_file(export) == (1, 1)
        response = app.test_client().get("/api/courses?q=CSE220")
        assert response.status_code == 200
        assert response.get_json()["courses"][0]["sections"][0]["crn"] == "12345"
