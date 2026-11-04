"""Load course/section data into the database.

`upsert_courses` takes the JSON shape below, so the Courseleaf scraper only
has to produce this format. Running the file loads a JSON file:

    python loader.py data/sample_courses.json

[
  {
    "course_name": "Data Structures", "subject": "CSE", "course_number": "220",
    "description": "...", "credit": 3, "course_session": "Regular Full",
    "course_career": "Undergraduate", "hasPreReq": true,
    "sections": [
      {"crn": "7587", "meeting_days": "M W F", "start_time": "14:20",
       "end_time": "15:45", "section_location": "117 Duthie",
       "section_type": "LEC", "instructor_name": "Dr. White",
       "instruction_mode": "In Person", "status": "Not Full"}
    ]
  }
]
"""
import json
import sys

from sqlalchemy import or_

from models import Course, Section, db

COURSE_FIELDS = ("course_name", "subject", "course_number", "description",
                 "credit", "course_session", "course_career")
SECTION_FIELDS = ("meeting_days", "start_time", "end_time", "section_location",
                  "section_type", "instructor_name", "instruction_mode", "status")


def upsert_courses(records):
    """Insert or update courses and their sections. Sections no longer listed
    for a course are removed. Returns (courses, sections) counts.

    Existing rows are loaded in two queries up front instead of one lookup
    per course and per section, which matters for a whole term (~2,600
    courses, ~6,400 sections) on MariaDB.
    """
    codes = [(rec.get("course_code") or f"{rec['subject']}{rec['course_number']}").replace(" ", "").upper()
             for rec in records]
    courses = {c.course_code: c for c in Course.query.filter(Course.course_code.in_(codes))}
    crns = [str(sec["crn"]).strip() for rec in records for sec in rec.get("sections", [])]
    sections = {s.crn: s for s in Section.query.filter(
        or_(Section.course_code.in_(codes), Section.crn.in_(crns)))}

    keep = set()
    n_sections = 0
    for rec, code in zip(records, codes):
        subject = rec["subject"].strip().upper()
        number = str(rec["course_number"]).strip()

        course = courses.get(code)
        if course is None:
            course = courses[code] = Course(course_code=code)
            db.session.add(course)
        for field in COURSE_FIELDS:
            if field in rec:
                setattr(course, field, rec[field])
        course.subject, course.course_number = subject, number
        course.has_prereq = bool(rec.get("hasPreReq", rec.get("has_prereq", False)))

        for sec in rec.get("sections", []):
            crn = str(sec["crn"]).strip()
            keep.add(crn)
            section = sections.get(crn)
            if section is None:
                section = sections[crn] = Section(crn=crn)
                db.session.add(section)
            section.course = course
            for field in SECTION_FIELDS:
                if field in sec:
                    setattr(section, field, sec[field])
            # the spec uses both names for this field; accept either
            if "instructor_mode" in sec and "instruction_mode" not in sec:
                section.instruction_mode = sec["instructor_mode"]
            n_sections += 1

    # after every record, so a CRN that moved to another course isn't lost
    for crn, section in sections.items():
        if crn not in keep and section.course_code in courses:
            db.session.delete(section)

    db.session.commit()
    return len(records), n_sections


if __name__ == "__main__":
    from app import create_app

    path = sys.argv[1] if len(sys.argv) > 1 else "data/sample_courses.json"
    with open(path) as f:
        data = json.load(f)
    with create_app().app_context():
        c, s = upsert_courses(data)
    print(f"Loaded {c} courses and {s} sections from {path}")
