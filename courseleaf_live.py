"""Sync Termly with the public CourseLeaf PATH class search.

https://courseleafpath.louisville.edu/ is a CourseLeaf "FOSE" class search.
Its page talks to a JSON API on the same host. Captured from the live site:

    POST /api/?page=fose&route=search&stat=A%2CF%2CC%2CW
    body = encodeURIComponent('{"other":{"srcdb":"4272"},"criteria":[{"field":"stat","value":"A,F,C,W"}]}')

srcdb 4272 = Spring 2027. Criteria are also repeated as URL parameters. A
search with only the status filter returns every section in the term.
    POST /api/?page=fose&route=details  -> one section's full details

Search rows have no meeting times (`meetingTimes` is empty on the live
site) and no instruction mode. Times come from the details response's
`all_sections` grid (one request per course covers every section), and modes
from four extra searches filtered by `instmode`.

The site is behind Cloudflare, but plain HTTP currently gets through. If this
client ever gets an HTML challenge page instead of JSON, use
courseleaf_browser_sync.py, or pass your browser's cookie and user agent with
--cookie / --user-agent.

This module calls those endpoints the same way the public search page does
(no login, public catalog data only), converts the results to the format
`loader.upsert_courses` expects, and loads them.

Usage (run on a machine that can reach the site, e.g. on campus/home wifi):

    python courseleaf_live.py --list-terms
    python courseleaf_live.py --term "Spring 2027" --subject CSE
    python courseleaf_live.py --term "Spring 2027" --subject CSE --subject MATH --details
    python courseleaf_live.py --term "Spring 2027" --all-subjects --details   # one request
    python courseleaf_live.py --term "Spring 2027" --subject CSE --dump raw.json --dry-run

Be polite: requests are spaced by --delay seconds (default 0.3) and at most
--workers (default 2) run at once.
"""
import argparse
import html
import http.client
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE_URL = "https://courseleafpath.louisville.edu"
USER_AGENT = "Termly/1.0 (UofL CSE 350 student course planner)"

# Status filter the site sends: A=open, F=full, C=closed, W=waitlist (all of them)
STAT_ALL = "A,F,C,W"

# FOSE meetingTimes use 0=Mon ... 6=Sun
_FOSE_DAYS = {"0": "M", "1": "T", "2": "W", "3": "R", "4": "F", "5": "S", "6": "U"}

# Status codes seen in CourseLeaf class search results
_FULL_STATUSES = {"F", "C", "W", "CLOSED", "FULL", "WAITLIST"}
_CANCELLED_STATUSES = {"X", "CANCELLED", "CANCELED"}

# instmode filter values of the class search -> Termly mode names
INSTRUCTION_MODES = {"P": "In Person", "BL": "Blended",
                     "OA": "Online Asynchronous", "OS": "Online Synchronous"}

# Course-level details fields say this when sections differ
VARIES = "Varies by section"


# ------------------------------------------------------------------ HTTP

class KeepAliveOpener:
    """urlopen replacement that reuses one connection per thread and host.

    A fresh urllib connection pays DNS + TCP + TLS on every request; on some
    networks the DNS lookup alone takes seconds, which dominated a full sync.
    """

    def __init__(self):
        self._local = threading.local()

    def _conn(self, scheme, host, timeout, fresh=False):
        pool = self._local.__dict__.setdefault("pool", {})
        if fresh and (scheme, host) in pool:
            pool.pop((scheme, host)).close()
        if (scheme, host) not in pool:
            cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
            pool[(scheme, host)] = cls(host, timeout=timeout)
        return pool[(scheme, host)]

    def __call__(self, req, timeout=60):
        parts = urllib.parse.urlsplit(req.full_url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = dict(req.header_items())
        for attempt in (0, 1):
            conn = self._conn(parts.scheme, parts.netloc, timeout, fresh=attempt == 1)
            try:
                conn.request(req.get_method(), path, body=req.data, headers=headers)
                resp = conn.getresponse()
                body = resp.read()
                break
            except (http.client.HTTPException, ConnectionError, OSError):
                if attempt:                       # stale kept-alive socket: retry once on a new one
                    raise
        if resp.status >= 400:
            raise urllib.error.HTTPError(req.full_url, resp.status, resp.reason, resp.headers, None)
        return _Body(body)


class _Body:
    def __init__(self, data):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._data


class CourseLeafClient:
    def __init__(self, base_url=BASE_URL, delay=0.3, timeout=60, opener=None,
                 cookie=None, user_agent=None, retries=3):
        self.base_url = base_url.rstrip("/")
        self.cookie = cookie
        self.user_agent = user_agent or USER_AGENT
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self._open = opener or KeepAliveOpener()
        self._last = 0.0
        self._lock = threading.Lock()

    def _wait(self):
        # shared by worker threads: requests start at least `delay` apart
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self.delay:
                time.sleep(self.delay - gap)
            self._last = time.monotonic()

    def _request(self, url, body=None):
        """Retries network errors, timeouts, HTTP 429/5xx and empty bodies."""
        for attempt in range(self.retries + 1):
            try:
                text = self._request_once(url, body)
                if text.strip():
                    return text
                error = RuntimeError(f"{url}: empty response")
            except urllib.error.HTTPError as exc:
                if exc.code != 429 and exc.code < 500:
                    raise
                error = exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                error = exc
            if attempt == self.retries:
                raise error
            time.sleep(self.delay and 2 ** attempt)

    def _request_once(self, url, body=None):
        self._wait()
        data = None
        headers = {"User-Agent": self.user_agent,
                   "Accept": "application/json, text/javascript, */*; q=0.01"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if body is not None:
            # The search page sends the JSON payload URL-encoded as the raw body
            data = urllib.parse.quote(json.dumps(body, separators=(",", ":")), safe="").encode()
            headers.update({"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest",
                            "Origin": self.base_url, "Referer": self.base_url + "/"})
        req = urllib.request.Request(url, data=data, headers=headers)
        with self._open(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def homepage(self):
        return self._request(self.base_url + "/")

    def api(self, route, payload, **params):
        query = urllib.parse.urlencode({"page": "fose", "route": route, **params})
        text = self._request(f"{self.base_url}/api/?{query}", payload)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            hint = " (Cloudflare check: use courseleaf_browser_sync.py)" if "<html" in text[:500].lower() else ""
            raise RuntimeError(f"{route}: expected JSON, got: {text[:200]!r}{hint}") from exc
        if isinstance(data, dict) and data.get("fatal"):
            raise RuntimeError(f"{route}: CourseLeaf error: {data['fatal']}")
        return data

    def search_raw(self, srcdb, subject=None, extra=()):
        """Same request the site sends; subject=None returns the whole term.
        `extra` adds criteria such as {"field": "instmode", "value": "OA"}."""
        criteria = [{"field": "subject", "value": subject}] if subject else []
        criteria.extend(extra)
        criteria.append({"field": "stat", "value": STAT_ALL})
        params = {c["field"]: c["value"] for c in criteria}
        return self.api("search", {"other": {"srcdb": srcdb}, "criteria": criteria}, **params)

    def search(self, srcdb, subject=None):
        data = self.search_raw(srcdb, subject)
        return data.get("results", []) if isinstance(data, dict) else []

    def details(self, srcdb, code):
        """Whole-course details; `all_sections` lists every section's meetings."""
        return self.api("details", {"group": f"code:{code}", "key": "", "srcdb": srcdb})


# ------------------------------------------------------- homepage options

_SELECT = re.compile(r"<select[^>]*\bid=[\"']crit-(?P<id>[\w-]+)[\"'][^>]*>(?P<body>.*?)</select>",
                     re.IGNORECASE | re.DOTALL)
_OPTION = re.compile(r"<option[^>]*\bvalue=[\"'](?P<value>[^\"']*)[\"'][^>]*>(?P<label>.*?)</option>",
                     re.IGNORECASE | re.DOTALL)


def parse_select_options(page_html):
    """{'srcdb': [(value, label), ...], 'subject': [...], ...} from the search page."""
    selects = {}
    for match in _SELECT.finditer(page_html):
        options = []
        for opt in _OPTION.finditer(match.group("body")):
            value = html.unescape(opt.group("value")).strip()
            label = html.unescape(re.sub(r"<[^>]+>", "", opt.group("label"))).strip()
            if value:
                options.append((value, label))
        selects[match.group("id")] = options
    return selects


def resolve_term(terms, wanted):
    """Accept a term code ('4272') or a label ('Spring 2027')."""
    for value, label in terms:
        if wanted == value or wanted.strip().lower() == label.lower():
            return value
    raise ValueError(f"unknown term {wanted!r}; options: {', '.join(l for _, l in terms)}")


# ------------------------------------------------------------- converting

def _hhmm(raw):
    """'1300' / '930' / '13:00' -> '13:00'."""
    raw = str(raw or "").replace(":", "").strip()
    if not raw.isdigit():
        return ""
    raw = raw.zfill(4)
    return f"{raw[:2]}:{raw[2:]}"


def parse_meeting_times(value):
    """FOSE meetingTimes (JSON string or list) -> (days, start, end).

    Days are joined with spaces ("M W F") so timeutils.parse_days reads
    them unambiguously. Only the first time slot is kept because the
    Termly schema stores one start/end per section.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else []
        except json.JSONDecodeError:
            value = []
    days, start, end = [], "", ""
    for slot in value or []:
        day = _FOSE_DAYS.get(str(slot.get("meet_day", "")).strip())
        s, e = _hhmm(slot.get("start_time")), _hhmm(slot.get("end_time"))
        if not day or not s or not e or s >= e:     # TBA slots come as 0/0
            continue
        if not start:
            start, end = s, e
        if (s, e) == (start, end) and day not in days:
            days.append(day)
    return " ".join(days), start, end


_MEETS = re.compile(r"^([MTWRFSU]+)\s+(\d{1,2}(?::\d{2})?[ap]?)-(\d{1,2}(?::\d{2})?[ap])$")


def _clock(text, meridiem):
    """'11' + 'a' -> 660 minutes."""
    hour, _, minute = text.partition(":")
    return int(hour) % 12 * 60 + (720 if meridiem == "p" else 0) + int(minute or 0)


def parse_meets(text):
    """CourseLeaf "Meets" text -> (days, start, end).

    'MWF 9-9:50a' -> ('M W F', '09:00', '09:50'); 'TR 11a-12:15p' ->
    ('T R', '11:00', '12:15'). 'Does Not Meet', TBA and anything unrecognised
    give empty values, so the section goes in the Online box. Like
    parse_meeting_times, only the first time slot is kept; other days with the
    same times are merged into it.
    """
    days, start, end = [], None, None
    for part in re.split(r"\s*[;,]\s*", (text or "").strip()):
        match = _MEETS.match(part)
        if not match:
            continue
        letters, a, b = match.groups()
        b_end = _clock(b[:-1], b[-1])
        if a[-1] in "ap":
            a_start = _clock(a[:-1], a[-1])
        else:   # '11-12:15p': start borrows the end's meridiem unless that is after the end
            a_start = _clock(a, b[-1])
            if a_start > b_end:
                a_start = _clock(a, "a")
        if a_start >= b_end:
            continue
        if start is None:
            start, end = a_start, b_end
        if (a_start, b_end) == (start, end):
            days.extend(d for d in letters if d not in days)
    if start is None:
        return "", "", ""
    return " ".join(days), f"{start // 60:02d}:{start % 60:02d}", f"{end // 60:02d}:{end % 60:02d}"


_GRID_ROW = re.compile(r"<a\b[^>]*\bclass=[\"'][^\"']*\bcourse-section\b[^>]*>(.*?)</a>", re.DOTALL)
_GRID_CELL = re.compile(r"class=[\"']course-section-([\w-]+)[\"'][^>]*>(.*?)</div>", re.DOTALL)
_HEADER = re.compile(r"<span[^>]*header-text[^>]*>.*?</span>", re.DOTALL)


def parse_all_sections(grid_html):
    """Details `all_sections` grid -> {crn: {'meets', 'instructor', 'section', 'schd'}}."""
    out = {}
    for row in _GRID_ROW.finditer(grid_html or ""):
        cells = {name: " ".join(_text(_HEADER.sub("", body)).split())
                 for name, body in _GRID_CELL.findall(row.group(1))}
        if cells.get("crn"):
            out[cells["crn"]] = {"meets": cells.get("meeting-name", ""),
                                 "instructor": cells.get("instructorresult-html", ""),
                                 "section": cells.get("section", ""),
                                 "schd": cells.get("schd", "")}
    return out


# acad_career codes / the [UGRD] title suffix -> spec values
_CAREERS = {"UGRD": "Undergraduate", "GRAD": "Graduate", "LAW": "Law",
            "ME": "Medicine", "DE": "Dentistry"}


def split_title(raw):
    """'Discrete Structures [UGRD]' -> ('Discrete Structures', 'UGRD').
    Topic courses look like 'Special Topics [UGRD]: HON: ...'; the base title is kept."""
    match = re.match(r"^(.*?)\s*\[([A-Z]+)\](?::.*)?$", (raw or "").strip())
    return (match.group(1), match.group(2)) if match else ((raw or "").strip(), None)


def _text(value):
    """Strip tags/entities from CourseLeaf *_html fields."""
    if value is None or value == VARIES:
        return ""
    value = re.sub(r"<br\s*/?>", "\n", str(value), flags=re.IGNORECASE)
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def normalize_mode(raw):
    """'Online--Asynchronous' / 'OA' -> 'Online Asynchronous' etc. (models.ONLINE_ASYNC)."""
    if str(raw or "").strip().upper() in INSTRUCTION_MODES:
        return INSTRUCTION_MODES[str(raw).strip().upper()]
    text = _text(raw).lower()
    if not text:
        return None
    if "async" in text:
        return "Online Asynchronous"
    if "sync" in text:
        return "Online Synchronous"
    if "blend" in text or "hybrid" in text:
        return "Blended"
    if "person" in text or text in ("p", "ip"):
        return "In Person"
    return _text(raw)


def normalize_status(row):
    for key in ("enrl_stat", "stat", "status"):
        value = str(row.get(key) or "").strip().upper()
        if value:
            return "Full" if value in _FULL_STATUSES else "Not Full"
    return "Not Full"


def is_cancelled(row):
    if str(row.get("isCancelled") or "").strip() in ("1", "true", "True"):
        return True
    return str(row.get("stat") or "").strip().upper() in _CANCELLED_STATUSES


# Spec values (schema.sql) for the session names shown on CourseLeaf PATH
_SESSIONS = {
    "1st half of term": "First Half", "first half": "First Half",
    "2nd half of term": "Second Half", "second half": "Second Half",
    "regular full term": "Regular Full", "regular full": "Regular Full", "regular": "Regular Full",
    "special session": "Special", "special": "Special",
}


def normalize_session(raw):
    text = _text(raw)
    return _SESSIONS.get(text.lower(), text) or None


def _fit(value, size):
    """Trim to the column size in schema.sql so MariaDB strict mode accepts it."""
    if value is None:
        return None
    return str(value)[:size]


def _credits(*values):
    for value in values:
        match = re.search(r"\d+(?:\.\d+)?", _text(value))
        if match:
            return int(float(match.group()))
    return 0


def _first(row, *keys):
    for key in keys:
        if row.get(key) not in (None, "", VARIES):
            return row[key]
    return None


def results_to_records(results, details=None, log=None):
    """FOSE search results (+ optional details) -> loader.upsert_courses records.

    `details` maps a course code ("CSE 220") to its details response; older
    raw dumps keyed by CRN work too. Field sizes follow schema.sql. A CRN is
    the sections table's primary key, so a CRN listed under two course codes
    (cross-listing) is kept only under the first one.
    """
    details = details or {}
    courses, owner, grids = {}, {}, {}
    for row in results:
        if is_cancelled(row):
            continue
        code_raw = str(row.get("code") or "").strip()     # "CSE 220"
        crn = str(row.get("crn") or "").strip()
        if not code_raw or not crn:
            continue
        subject, _, number = code_raw.partition(" ")
        if not number:                                      # "CSE220"
            subject = code_raw.rstrip("0123456789")
            number = code_raw[len(subject):]
        subject, number = subject.upper().strip(), number.replace(" ", "").strip()
        code = f"{subject}{number}"
        if owner.setdefault(crn, code) != code:
            if log:
                log(f"  CRN {crn} is cross-listed as {owner[crn]} and {code}; kept under {owner[crn]}")
            continue
        det = details.get(code_raw) or details.get(crn) or {}
        if not isinstance(det, dict):     # courses with no details come back as []
            det = {}
        if code_raw not in grids:
            grids[code_raw] = parse_all_sections(det.get("all_sections"))
        grid = grids[code_raw].get(crn, {})
        # Course-level fields that differ per section say "Varies by section"
        # and are skipped; a single value is the same for every section.
        det_sec = det if str(det.get("_crn") or det.get("crn") or crn) in (crn, VARIES) else {}

        course = courses.get(code)
        if course is None:
            title, career_code = split_title(_text(_first(det, "title") or row.get("title")))
            career = _text(_first(row, "career", "acad_career") or _first(det, "career", "career_html")) or career_code
            prereq_text = " ".join(_text(det.get(k)) for k in
                                   ("registration_restrictions", "prereqs", "prerequisites", "clssnotes",
                                    "description"))
            course = courses[code] = {
                "course_code": _fit(code, 20),
                "subject": _fit(subject, 10),
                "course_number": _fit(number, 10),
                "course_name": _fit(title or code, 255),
                "description": _text(_first(det, "description", "description_html")),
                "credit": _credits(_first(det, "hours", "hours_html"), row.get("hours"), row.get("credits")),
                "course_session": _fit(normalize_session(
                    _first(row, "sess", "session") or _first(det, "session", "session_html")), 50),
                "course_career": _fit(_CAREERS.get(career, career) or None, 50),
                "hasPreReq": any(w in prereq_text.lower() for w in ("prereq", "pre-req")),
                "sections": [],
            }

        days, start, end = parse_meeting_times(row.get("meetingTimes"))
        if not days:
            days, start, end = parse_meets(_first(row, "meets") or grid.get("meets"))
        mode = normalize_mode(_first(row, "instmode", "inst_mode", "insm", "mode")
                              or _first(det_sec, "instmode", "inst_mode", "instmode_html"))
        if mode is None and days:
            mode = "In Person"
        location = _text(_first(row, "bldg_room", "room", "location")
                         or _first(det_sec, "location"))
        instructor = _text(row.get("instr") or grid.get("instructor")
                           or _first(det_sec, "instructordetail_html"))
        if instructor.upper().endswith("TBA"):
            instructor = ""
        course["sections"] = [s for s in course["sections"] if s["crn"] != crn]
        course["sections"].append({
            "crn": _fit(crn, 10),
            "meeting_days": _fit(days, 20),
            "start_time": start,
            "end_time": end,
            "section_location": _fit(location, 100),
            "section_type": _fit(str(row.get("schd") or "LEC").strip().upper() or "LEC", 10),
            "instructor_name": _fit(instructor, 100),
            "instruction_mode": _fit(mode, 50),
            "status": normalize_status(row),
        })
    return list(courses.values())


# ------------------------------------------------------------------ sync

def fetch_modes(client, srcdb, total=0, log=print):
    """{crn: 'OA', ...} from one instmode-filtered search per mode.

    CourseLeaf ignores criteria it doesn't know and returns the whole term, so
    a filtered result as large as the term, or a CRN under two modes, means
    the filter was ignored and nothing is used.
    """
    modes = {}
    for value in INSTRUCTION_MODES:
        try:
            data = client.search_raw(srcdb, extra=[{"field": "instmode", "value": value}])
        except Exception as exc:  # modes are optional
            log(f"  instmode {value} failed: {exc}")
            continue
        crns = {str(r.get("crn")) for r in (data.get("results", []) if isinstance(data, dict) else [])}
        if (total and len(crns) >= total) or crns & modes.keys():
            log("  instmode filter ignored by the server; modes left empty")
            return {}
        modes.update(dict.fromkeys(crns, value))
    return modes


def fetch_details(client, srcdb, codes, workers=2, log=print):
    """{code: details} with one request per course, `workers` at a time."""
    def one(code):
        try:
            return code, client.details(srcdb, code)
        except Exception as exc:  # keep going; details are optional
            log(f"  details failed for {code}: {exc}")
            return code, None

    details, failed = {}, 0
    with ThreadPoolExecutor(max(1, workers)) as pool:
        for i, (code, det) in enumerate(pool.map(one, codes), 1):
            if det is None:
                failed += 1
            else:
                details[code] = det
            if i % 250 == 0:
                log(f"  details {i}/{len(codes)}")
    if failed:
        log(f"  details failed for {failed} of {len(codes)} courses (no times or credits for those)")
    return details


def fetch_term(client, srcdb, subjects=None, with_details=False, log=print, all_subjects=(), workers=2):
    """subjects=None -> whole term in one request (falls back to per subject
    using `all_subjects` if the server returns fewer rows than it counts).

    Returns (results, details). Each result row gets an `instmode` code and
    details are keyed by course code.
    """
    results, count = [], None
    if not subjects:
        data = client.search_raw(srcdb)
        results = data.get("results", []) if isinstance(data, dict) else []
        count = data.get("count") if isinstance(data, dict) else None
        log(f"  whole term: {len(results)} sections")
        if isinstance(count, int) and count > len(results) and all_subjects:
            log(f"  server counted {count}; fetching per subject instead")
            results, subjects = [], list(all_subjects)
    for subject in subjects or []:
        rows = client.search(srcdb, subject)
        log(f"  {subject}: {len(rows)} sections")
        results.extend(rows)

    # whole-term size, to spot a filter the server ignored
    total = len(results) if not subjects else (count or 0)
    modes = fetch_modes(client, srcdb, total, log)
    for row in results:
        if str(row.get("crn")) in modes:
            row.setdefault("instmode", modes[str(row.get("crn"))])
    log(f"  instruction mode known for {sum(1 for r in results if r.get('instmode'))} sections")

    details = {}
    if with_details:
        codes = list(dict.fromkeys(str(r["code"]) for r in results
                                   if r.get("code") and r.get("crn") and not is_cancelled(r)))
        log(f"  fetching details for {len(codes)} courses ...")
        details = fetch_details(client, srcdb, codes, workers, log)
    return results, details


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sync Termly from CourseLeaf PATH")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--list-terms", action="store_true", help="print terms and subjects, then exit")
    parser.add_argument("--term", help="term label ('Spring 2027') or code")
    parser.add_argument("--subject", action="append", default=[], help="subject code, repeatable")
    parser.add_argument("--all-subjects", action="store_true")
    parser.add_argument("--details", action="store_true",
                        help="also fetch meeting times, credits and descriptions (one request per course)")
    parser.add_argument("--delay", type=float, default=0.3, help="seconds between requests")
    parser.add_argument("--workers", type=int, default=2, help="details requests at once")
    parser.add_argument("--dump", help="save raw search+details JSON here")
    parser.add_argument("--dry-run", action="store_true", help="don't write to the database")
    parser.add_argument("--from-raw", help="load a saved raw JSON dump instead of fetching")
    parser.add_argument("--cookie", help="Cookie header from your browser (only if Cloudflare blocks you)")
    parser.add_argument("--user-agent", help="your browser's User-Agent (use together with --cookie)")
    args = parser.parse_args(argv)

    if args.from_raw:
        with open(args.from_raw, encoding="utf-8") as fh:
            raw = json.load(fh)
        return _load(results_to_records(raw.get("results", []), raw.get("details", {}), log=print),
                     args.dry_run)

    client = CourseLeafClient(args.base_url, delay=args.delay, cookie=args.cookie, user_agent=args.user_agent)
    options = parse_select_options(client.homepage())
    terms, subjects = options.get("srcdb", []), options.get("subject", [])

    if args.list_terms:
        print("Terms:")
        for value, label in terms:
            print(f"  {value:10} {label}")
        print("Subjects:", ", ".join(v for v, _ in subjects))
        return 0
    if not args.term:
        parser.error("--term is required (see --list-terms)")
    srcdb = resolve_term(terms, args.term) if terms else args.term

    if not args.all_subjects and not args.subject:
        parser.error("pass --subject CODE (repeatable) or --all-subjects")
    wanted = None if args.all_subjects else [s.upper() for s in args.subject]

    print(f"Fetching {'the whole term' if wanted is None else ', '.join(wanted)} for term {srcdb} ...")
    results, details = fetch_term(client, srcdb, wanted, args.details,
                                  all_subjects=[v for v, _ in subjects], workers=args.workers)
    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as fh:
            json.dump({"srcdb": srcdb, "results": results, "details": details}, fh)
        print(f"Raw data saved to {args.dump}")

    return _load(results_to_records(results, details, log=print), args.dry_run)


def _load(records, dry_run=False):
    print(f"Converted {len(records)} courses, {sum(len(r['sections']) for r in records)} sections")
    if dry_run:
        return 0
    if not records:
        print("Nothing returned; leaving the database unchanged.")
        return 1

    from app import create_app
    from loader import upsert_courses
    with create_app().app_context():
        c, s = upsert_courses(records)
    print(f"Loaded {c} courses and {s} sections into Termly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
