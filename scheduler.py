"""Schedule generator.

Input:  the courses in the student's plan + their excluded time blocks.
Output: every conflict-free schedule (up to a limit), each one a list of CRNs.

A course needs one section of each section_type it offers (e.g. one LEC and
one LAB). Online-box sections (no meeting days / Online Asynchronous) never
conflict with anything.
"""
from collections import defaultdict

from timeutils import overlaps, parse_days, to_minutes


def parse_excluded_times(excluded):
    """[{"day": "M", "start": "09:00", "end": "10:30"}, ...] -> [(day, start, end)].

    'day' may also be a string of several days ("MWF") or a list.
    Raises ValueError on bad input so the API can return a 400.
    """
    blocks = []
    for item in excluded or []:
        if not isinstance(item, dict):
            raise ValueError("each excluded time must be an object")
        raw_days = item.get("days", item.get("day"))
        if isinstance(raw_days, list):
            raw_days = " ".join(raw_days)
        days = parse_days(raw_days or "")
        start, end = to_minutes(item.get("start")), to_minutes(item.get("end"))
        if not days or start is None or end is None or start >= end:
            raise ValueError(f"invalid excluded time: {item}")
        blocks.extend((day, start, end) for day in days)
    return blocks


def _conflicts(blocks, taken):
    for day, start, end in blocks:
        for t_day, t_start, t_end in taken:
            if day == t_day and overlaps(start, end, t_start, t_end):
                return True
    return False


def build_groups(courses, include_full=False, modes=None):
    """One group per (course, section_type); each group is a list of usable
    sections. Returns (groups, problems) where problems lists courses that
    can't be scheduled at all and why."""
    groups, problems = [], []
    for course in courses:
        by_type = defaultdict(list)
        for section in course.sections:
            by_type[section.section_type or "LEC"].append(section)
        if not by_type:
            problems.append({"course_code": course.course_code, "reason": "no sections offered"})
            continue
        for section_type, sections in sorted(by_type.items()):
            usable = [
                s for s in sections
                if (include_full or not s.is_full)
                and (not modes or s.instruction_mode in modes)
            ]
            if not usable:
                problems.append({
                    "course_code": course.course_code,
                    "section_type": section_type,
                    "reason": "all sections are full or filtered out",
                })
            groups.append(usable)
    return groups, problems


def generate_schedules(courses, excluded_blocks, include_full=False, modes=None, limit=50):
    groups, problems = build_groups(courses, include_full, modes)
    if any(not g for g in groups):
        return [], problems

    # Pre-compute each section's time blocks and drop any that hit an
    # excluded time; try the most constrained groups first.
    candidates = []
    for group in groups:
        options = [(s, s.time_blocks()) for s in group]
        options = [(s, b) for s, b in options if not _conflicts(b, excluded_blocks)]
        if not options:
            code = group[0].course_code
            problems.append({"course_code": code, "reason": "every section overlaps your excluded times"})
            return [], problems
        candidates.append(options)
    candidates.sort(key=len)

    results = []

    def backtrack(index, chosen, taken):
        if len(results) >= limit:
            return
        if index == len(candidates):
            results.append(list(chosen))
            return
        for section, blocks in candidates[index]:
            if _conflicts(blocks, taken):
                continue
            chosen.append(section)
            backtrack(index + 1, chosen, taken + blocks)
            chosen.pop()

    backtrack(0, [], [])
    if not results:
        problems.append({"reason": "no combination of sections fits without a time conflict"})
    return results, problems
