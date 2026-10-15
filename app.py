"""
Termly API - Course Search Endpoint
CSE 350 - SCRUM-52

GET /api/courses lets the frontend search courses with optional filters:
    q          - text search on course name or course code
    subject    - subject code (e.g. CSE, MATH)
    session    - first half, second half, regular full, special
    mode       - in person, online, hybrid
    career     - undergraduate, graduate
    hasPreReq  - true or false

All filters are case-insensitive and can be combined.
"""

from flask import Flask, jsonify, request

app = Flask(__name__)

# Allowed values for each filter (stored lowercase so comparisons are case-insensitive)
SESSIONS = {"first half", "second half", "regular full", "special"}
MODES = {"in person", "online", "hybrid"}
CAREERS = {"undergraduate", "graduate"}
ALLOWED_FILTERS = {"q", "subject", "session", "mode", "career", "hasPreReq"}

# Sample data for now. This will be replaced with a MariaDB query later.
COURSES = [
    {
        "course_code": "CSE 350",
        "course_name": "Software Engineering",
        "subject": "CSE",
        "course_session": "Regular Full",
        "course_career": "Undergraduate",
        "hasPreReq": True,
        "sections": [
            {"section": "01", "instruction_mode": "In Person"},
            {"section": "50", "instruction_mode": "Online"},
        ],
    },
    {
        "course_code": "CSE 130",
        "course_name": "Introduction to Programming in C",
        "subject": "CSE",
        "course_session": "First Half",
        "course_career": "Undergraduate",
        "hasPreReq": False,
        "sections": [
            {"section": "01", "instruction_mode": "Hybrid"},
        ],
    },
    {
        "course_code": "CSE 545",
        "course_name": "Artificial Intelligence",
        "subject": "CSE",
        "course_session": "Regular Full",
        "course_career": "Graduate",
        "hasPreReq": True,
        "sections": [
            {"section": "01", "instruction_mode": "In Person"},
        ],
    },
    {
        "course_code": "MATH 205",
        "course_name": "Calculus I",
        "subject": "MATH",
        "course_session": "Second Half",
        "course_career": "Undergraduate",
        "hasPreReq": False,
        "sections": [
            {"section": "01", "instruction_mode": "Online"},
        ],
    },
]


def normalized(value):
    """Trim spaces and lowercase a value so comparisons ignore case."""
    return str(value).strip().casefold()


def error(message):
    """Return a JSON error response with a 400 status code."""
    return jsonify({"error": message}), 400


@app.get("/")
def home():
    return jsonify({"message": "Termly API is running", "search_endpoint": "/api/courses"})


@app.get("/api/courses")
def get_courses():
    # Reject any filter we don't support
    unknown = set(request.args) - ALLOWED_FILTERS
    if unknown:
        return error(f"Unknown filter(s): {', '.join(sorted(unknown))}")

    # Read and clean up the filters (empty string means "not used")
    q = normalized(request.args.get("q", ""))
    subject = normalized(request.args.get("subject", ""))
    session = normalized(request.args.get("session", ""))
    mode = normalized(request.args.get("mode", ""))
    career = normalized(request.args.get("career", ""))
    has_prereq = normalized(request.args.get("hasPreReq", ""))

    # Validate filter values
    if session and session not in SESSIONS:
        return error("Invalid session filter")
    if mode and mode not in MODES:
        return error("Invalid mode filter")
    if career and career not in CAREERS:
        return error("Invalid career filter")
    if has_prereq and has_prereq not in {"true", "false"}:
        return error("hasPreReq must be true or false")

    results = []
    for course in COURSES:
        name = normalized(course.get("course_name", ""))
        code = normalized(course.get("course_code", ""))

        if q and q not in name and q not in code:
            continue
        if subject and subject != normalized(course.get("subject", "")):
            continue
        if session and session != normalized(course.get("course_session", "")):
            continue
        if career and career != normalized(course.get("course_career", "")):
            continue
        if has_prereq and bool(course.get("hasPreReq")) != (has_prereq == "true"):
            continue

        # Only keep sections that match the mode filter
        sections = course.get("sections", [])
        if mode:
            sections = [s for s in sections if normalized(s.get("instruction_mode", "")) == mode]
            if not sections:
                continue

        results.append({**course, "sections": sections})

    return jsonify(results)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
