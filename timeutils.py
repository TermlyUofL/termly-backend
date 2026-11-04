"""Helpers for parsing the meeting_days / start_time / end_time strings."""
import re

# Canonical day letters match the class search page: M T W R F S U
_DAY_ALIASES = {
    "M": "M", "MO": "M",
    "T": "T", "TU": "T",
    "W": "W", "WE": "W",
    "R": "R", "TR": "R", "TH": "R",
    "F": "F", "FR": "F",
    "S": "S", "SA": "S",
    "U": "U", "SU": "U",
}
# Case-sensitive on purpose: "Tr" (spec style) is Thursday, but "TR"
# (class-search style) is Tuesday + Thursday.
_DAY_TOKEN = re.compile(r"Th|Tr|Tu|Mo|We|Fr|Sa|Su|M|T|W|R|F|S|U")
DAY_ORDER = "MTWRFSU"


def parse_days(days):
    """'M W F', 'MWF', 'T Tr', 'TR', 'M T W Tr F' -> ['M','W','F'] etc."""
    if not days:
        return []
    if days.islower():
        days = days.upper()
    found = []
    for token in _DAY_TOKEN.findall(days.replace(",", " ")):
        day = _DAY_ALIASES[token.upper()]
        if day not in found:
            found.append(day)
    return sorted(found, key=DAY_ORDER.index)


_TIME = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap])?\.?\s*m?\.?\s*$", re.IGNORECASE)


def to_minutes(value):
    """'14:20', '2:20 pm', '3:45 p', '9:00a' -> minutes after midnight.

    A time with no am/pm and an hour of 1-7 is treated as PM (no classes
    start between 1 and 7 am). Storing times as 24h 'HH:MM' avoids guessing.
    Returns None if the value can't be parsed.
    """
    if not value:
        return None
    match = _TIME.match(str(value))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower()
    if hour > 23 or minute > 59:
        return None
    if meridiem == "p" and hour < 12:
        hour += 12
    elif meridiem == "a" and hour == 12:
        hour = 0
    elif not meridiem and 1 <= hour <= 7:
        hour += 12
    return hour * 60 + minute


def overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end
