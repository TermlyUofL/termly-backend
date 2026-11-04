"""Termly REST API (Flask).

Endpoints
  GET  /api/health
  GET  /api/filters                  options for every filter dropdown
  GET  /api/courses                  search + filters, paginated
  GET  /api/courses/<course_code>    one course with all its sections
  GET  /api/sections?crns=1,2,3      section details for a list of CRNs
  GET  /api/sections/<crn>           one section
  POST /api/schedules/generate       build conflict-free schedules
"""
import re

from flask import Flask, jsonify, request
from flask_cors import CORS
from sqlalchemy import or_
from werkzeug.exceptions import HTTPException

from config import Config
from models import Course, Section, db
from scheduler import generate_schedules, parse_excluded_times


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)
    db.init_app(app)
    CORS(app, origins=app.config["CORS_ORIGINS"])

    with app.app_context():
        db.create_all()

    register_routes(app)
    return app


# ---------------------------------------------------------------- helpers

def _split(value):
    """'a,b , c' -> ['a','b','c']; supports repeated query params too."""
    items = []
    for part in value if isinstance(value, list) else [value]:
        items.extend(p.strip() for p in (part or "").split(",") if p.strip())
    return items


def _query_list(name):
    return _split(request.args.getlist(name))


def _subject_code(value):
    """'Computer Science and Engineering (CSE)' or 'CSE' -> 'CSE'."""
    match = re.search(r"\(([A-Za-z]+)\)\s*$", value)
    return (match.group(1) if match else value).strip().upper()


def _parse_bool(value):
    if value is None or value == "":
        return None
    value = str(value).strip().lower()
    if value in ("yes", "true", "1", "y"):
        return True
    if value in ("no", "false", "0", "n"):
        return False
    raise ValueError(f"expected yes/no, got '{value}'")


def _int_arg(name, default, minimum, maximum):
    try:
        value = int(request.args.get(name, default))
    except ValueError:
        raise ValueError(f"'{name}' must be a number")
    return max(minimum, min(value, maximum))


def _search_condition(q):
    """Search by course name or course code. Supports the class-search
    wildcard: '3*' -> 300-level, 'CSE3*' -> CSE 300-level."""
    q = q.strip()
    compact = q.replace(" ", "").upper()
    if "*" in q:
        pattern = compact.replace("*", "%")
        return or_(Course.course_code.like(pattern), Course.course_number.like(pattern))
    return or_(
        Course.course_name.ilike(f"%{q}%"),
        Course.course_code.ilike(f"%{compact}%"),
    )


def _section_with_course(section):
    data = section.to_dict()
    data["course_name"] = section.course.course_name
    data["credit"] = section.course.credit
    return data


# ---------------------------------------------------------------- routes

def register_routes(app):

    @app.errorhandler(ValueError)
    def bad_request(err):
        return jsonify(error=str(err)), 400

    @app.errorhandler(HTTPException)
    def http_error(err):
        return jsonify(error=err.description), err.code

    @app.get("/api/health")
    def health():
        return jsonify(status="ok")

    @app.get("/api/filters")
    def filters():
        def distinct(column):
            rows = db.session.query(column).distinct().order_by(column).all()
            return [r[0] for r in rows if r[0]]

        return jsonify(
            subjects=distinct(Course.subject),
            sessions=distinct(Course.course_session),
            modes_of_instruction=distinct(Section.instruction_mode),
            careers=distinct(Course.course_career),
            has_prereq=["yes", "no"],
        )

    @app.get("/api/courses")
    def list_courses():
        query = Course.query

        q = request.args.get("q", "").strip()
        if q:
            query = query.filter(_search_condition(q))

        subjects = [_subject_code(s) for s in _query_list("subject")]
        if subjects:
            query = query.filter(Course.subject.in_(subjects))

        sessions = _query_list("session")
        if sessions:
            query = query.filter(Course.course_session.in_(sessions))

        careers = _query_list("career")
        if careers:
            query = query.filter(Course.course_career.in_(careers))

        has_prereq = _parse_bool(request.args.get("has_prereq"))
        if has_prereq is not None:
            query = query.filter(Course.has_prereq == has_prereq)

        # Mode of instruction is a section filter: keep courses that have at
        # least one matching section, and only return those sections.
        modes = _query_list("mode")
        if modes:
            query = query.filter(Course.sections.any(Section.instruction_mode.in_(modes)))

        open_only = _parse_bool(request.args.get("open_only")) or False
        if open_only:
            query = query.filter(Course.sections.any(Section.status != "Full"))

        page = _int_arg("page", 1, 1, 10_000)
        per_page = _int_arg("per_page", 25, 1, 100)
        total = query.count()
        courses = (
            query.order_by(Course.subject, Course.course_number)
            .offset((page - 1) * per_page).limit(per_page).all()
        )

        results = []
        for course in courses:
            sections = [
                s for s in course.sections
                if (not modes or s.instruction_mode in modes)
                and (not open_only or not s.is_full)
            ]
            results.append(course.to_dict(sections=sections))

        return jsonify(courses=results, total=total, page=page, per_page=per_page)

    @app.get("/api/courses/<course_code>")
    def get_course(course_code):
        code = course_code.replace(" ", "").upper()
        course = db.get_or_404(Course, code, description=f"course {code} not found")
        return jsonify(course.to_dict(sections=course.sections))

    @app.get("/api/sections")
    def get_sections():
        crns = _query_list("crns")
        if not crns:
            raise ValueError("pass one or more CRNs, e.g. ?crns=7587,7590")
        sections = Section.query.filter(Section.crn.in_(crns)).all()
        found = {s.crn for s in sections}
        return jsonify(
            sections=[_section_with_course(s) for s in sections],
            missing=[c for c in crns if c not in found],
        )

    @app.get("/api/sections/<crn>")
    def get_section(crn):
        section = db.get_or_404(Section, crn, description=f"section {crn} not found")
        return jsonify(_section_with_course(section))

    @app.post("/api/schedules/generate")
    def generate():
        """Body:
        {
          "course_codes":   ["CSE220", "CSE310"],          # the schedule plan
          "excluded_times": [{"day": "MWF", "start": "08:00", "end": "10:00"}],
          "include_full":   false,                         # optional
          "modes":          ["In Person"],                 # optional
          "limit":          50                             # optional
        }
        """
        body = request.get_json(silent=True) or {}
        codes = [c.replace(" ", "").upper() for c in _split(body.get("course_codes") or [])]
        if not codes:
            raise ValueError("course_codes is required")
        codes = list(dict.fromkeys(codes))  # drop duplicates, keep order

        courses = Course.query.filter(Course.course_code.in_(codes)).all()
        found = {c.course_code for c in courses}
        missing = [c for c in codes if c not in found]
        if missing:
            return jsonify(error="unknown course codes", missing=missing), 404
        courses.sort(key=lambda c: codes.index(c.course_code))

        excluded = parse_excluded_times(body.get("excluded_times"))
        limit = body.get("limit", app.config["DEFAULT_SCHEDULE_LIMIT"])
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, app.config["MAX_SCHEDULE_LIMIT"])

        schedules, problems = generate_schedules(
            courses, excluded,
            include_full=bool(body.get("include_full", False)),
            modes=_split(body.get("modes") or []) or None,
            limit=limit,
        )

        total_credits = sum(c.credit for c in courses)
        payload = []
        for chosen in schedules:
            chosen.sort(key=lambda s: (codes.index(s.course_code), s.section_type or ""))
            payload.append({
                "crns": [s.crn for s in chosen],
                "total_credits": total_credits,
                "calendar": [_section_with_course(s) for s in chosen if s.placement == "calendar"],
                "online": [_section_with_course(s) for s in chosen if s.placement == "online"],
            })

        return jsonify(
            schedules=payload,
            count=len(payload),
            limit_reached=len(payload) >= limit,
            total_credits=total_credits,
            problems=problems,
        )


if __name__ == "__main__":
    create_app().run(debug=True, port=5000)
