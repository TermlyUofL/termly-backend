"""Import an authorized CourseLeaf PATH JSON export into Termly.

This is a file integration, not a claim to access a live CourseLeaf API.
Export rows need course information and section CRNs. Both a list of
courses with nested sections and a flat list of course/section rows work.
"""
import argparse
import json
from collections import OrderedDict
from pathlib import Path

from loader import upsert_courses


def pick(row, *keys, default=None):
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return default


def normalize_courseleaf(data):
    """Convert CourseLeaf-style JSON to the format used by loader.py."""
    if isinstance(data, dict):
        data = pick(data, "courses", "results", "data", "items")
    if not isinstance(data, list):
        raise ValueError("Expected JSON list or an object containing a courses/results/data list")
    grouped = OrderedDict()
    for row in data:
        if not isinstance(row, dict):
            raise ValueError("Course records must be JSON objects")
        raw_code = str(pick(row, "course_code", "courseCode", "code", default="")).replace(" ", "").upper()
        subject = str(pick(row, "subject", "subject_code", "subjectCode", default="")).strip().upper()
        number = str(pick(row, "course_number", "courseNumber", "catalog_number", "catalogNumber", default="")).strip()
        if not subject and raw_code:
            subject = raw_code.rstrip("0123456789")
        if not number and raw_code:
            number = raw_code[len(subject):]
        if not subject or not number:
            raise ValueError(f"Course must include subject and number (or code): {row!r}")
        code = f"{subject}{number}"
        name = pick(row, "course_name", "courseName", "title", "name")
        if not name:
            raise ValueError(f"Missing course title for {code}")
        credit_raw = pick(row, "credit", "credits", "units", default=0)
        try:
            credits = int(float(credit_raw))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid credit value for {code}: {credit_raw}") from exc
        if code not in grouped:
            grouped[code] = {
                "course_code": code, "subject": subject, "course_number": number,
                "course_name": str(name), "description": pick(row, "description", "desc", default=""),
                "credit": credits, "course_session": pick(row, "course_session", "session"),
                "course_career": pick(row, "course_career", "career"),
                "hasPreReq": bool(pick(row, "hasPreReq", "has_prereq", default=False)),
                "sections": [],
            }
        sections = row.get("sections")
        if sections is None:
            sections = [row] if pick(row, "crn", "class_number", "classNumber") else []
        if not isinstance(sections, list):
            raise ValueError(f"Sections for {code} must be a list")
        for section in sections:
            crn = pick(section, "crn", "class_number", "classNumber")
            if not crn:
                raise ValueError(f"Section missing CRN for {code}")
            normalized = {
                "crn": str(crn),
                "meeting_days": pick(section, "meeting_days", "meetingDays", "days", default=""),
                "start_time": pick(section, "start_time", "startTime", default=""),
                "end_time": pick(section, "end_time", "endTime", default=""),
                "section_location": pick(section, "section_location", "location", "room", default=""),
                "section_type": pick(section, "section_type", "component", default="LEC"),
                "instructor_name": pick(section, "instructor_name", "instructor", default=""),
                "instruction_mode": pick(section, "instruction_mode", "instructor_mode", "mode", default="In Person"),
                "status": pick(section, "status", default="Not Full"),
            }
            current = grouped[code]["sections"]
            current[:] = [existing for existing in current if existing["crn"] != normalized["crn"]]
            current.append(normalized)
    return list(grouped.values())


def import_file(path):
    with open(path, encoding="utf-8") as handle:
        records = normalize_courseleaf(json.load(handle))
    if not records:
        raise ValueError("Export is empty; refusing to modify existing course records")
    return upsert_courses(records)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Load an authorized CourseLeaf JSON export")
    parser.add_argument("path", type=Path, help="Path to CourseLeaf JSON export")
    args = parser.parse_args()
    from app import create_app
    with create_app().app_context():
        courses, sections = import_file(args.path)
    print(f"Imported {courses} courses and {sections} sections")
