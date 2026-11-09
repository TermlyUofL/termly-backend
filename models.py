"""Database models for Termly, following the Termly Data Spec.

Course  - one row per course (same for every section)
Section - one row per section (CRN); a course may have many
"""
from flask_sqlalchemy import SQLAlchemy

from timeutils import parse_days, to_minutes

db = SQLAlchemy()

ONLINE_ASYNC = "Online Asynchronous"


class Course(db.Model):
    __tablename__ = "courses"

    course_code = db.Column(db.String(20), primary_key=True)      # CSE220
    course_name = db.Column(db.String(255), nullable=False)       # Data Structures
    subject = db.Column(db.String(10), nullable=False, index=True)  # CSE
    course_number = db.Column(db.String(10), nullable=False)      # 220
    description = db.Column(db.Text, default="")
    credit = db.Column(db.Integer, nullable=False, default=0)     # 3
    course_session = db.Column(db.String(50), index=True)         # Regular Full
    course_career = db.Column(db.String(50), index=True)          # Undergraduate
    has_prereq = db.Column(db.Boolean, nullable=False, default=False)

    sections = db.relationship(
        "Section", back_populates="course", cascade="all, delete-orphan",
        order_by="Section.crn",
    )

    def to_dict(self, sections=None):
        data = {
            "course_name": self.course_name,
            "subject": self.subject,
            "course_number": self.course_number,
            "course_code": self.course_code,
            "description": self.description,
            "credit": self.credit,
            "course_session": self.course_session,
            "course_career": self.course_career,
            "hasPreReq": self.has_prereq,
        }
        if sections is not None:
            data["sections"] = [s.to_dict() for s in sections]
        return data


class Section(db.Model):
    __tablename__ = "sections"

    crn = db.Column(db.String(10), primary_key=True)              # 7587
    course_code = db.Column(
        db.String(20), db.ForeignKey("courses.course_code", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    meeting_days = db.Column(db.String(20), default="")           # "M W F" / "TR"
    start_time = db.Column(db.String(10), default="")             # "14:20"
    end_time = db.Column(db.String(10), default="")               # "15:45"
    section_location = db.Column(db.String(100), default="")      # room or campus: "117 Duthie" / "Belknap Campus"
    section_type = db.Column(db.String(10), default="LEC")        # LEC/REC/SEM/LAB
    instructor_name = db.Column(db.String(100), default="")
    instruction_mode = db.Column(db.String(50), index=True)       # In Person, Online ...
    status = db.Column(db.String(10), default="Not Full")         # Full / Not Full

    course = db.relationship("Course", back_populates="sections")

    # ---- derived values used by the API and the scheduler ----

    @property
    def is_full(self):
        return (self.status or "").strip().lower() == "full"

    @property
    def placement(self):
        """Rule from the spec: a section goes in the Online Classes box if it
        has no meeting days or is Online Asynchronous; otherwise it goes on the
        calendar (Online Synchronous has times, so it is on the calendar)."""
        if not (self.meeting_days or "").strip() or self.instruction_mode == ONLINE_ASYNC:
            return "online"
        return "calendar"

    def time_blocks(self):
        """List of (day, start_minute, end_minute) for calendar sections."""
        if self.placement != "calendar":
            return []
        start, end = to_minutes(self.start_time), to_minutes(self.end_time)
        if start is None or end is None:
            return []
        return [(day, start, end) for day in parse_days(self.meeting_days)]

    def to_dict(self):
        return {
            "crn": self.crn,
            "course_code": self.course_code,
            "meeting_days": self.meeting_days or "",
            "start_time": self.start_time or "",
            "end_time": self.end_time or "",
            "section_location": self.section_location or "",
            "section_type": self.section_type,
            "instructor_name": self.instructor_name or "",
            "instruction_mode": self.instruction_mode,
            "status": self.status,
            "placement": self.placement,
        }
