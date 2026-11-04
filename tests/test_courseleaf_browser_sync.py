"""End-to-end test of courseleaf_browser_sync against a local fake CourseLeaf site."""
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("playwright")

from app import create_app
from config import TestConfig
from courseleaf_browser_sync import sync
from courseleaf_live import results_to_records
from loader import upsert_courses

SECTIONS = {
    "CSE": [
        {"code": "CSE 220", "title": "Data Structures", "crn": "7587", "schd": "LEC", "stat": "A",
         "meetingTimes": json.dumps([{"meet_day": d, "start_time": "1300", "end_time": "1350"} for d in "024"])},
        {"code": "CSE 310", "title": "Discrete Structures", "crn": "8001", "schd": "LEC", "stat": "A",
         "instmode": "Online--Asynchronous", "meetingTimes": "[]"},
    ],
    "MATH": [
        {"code": "MATH 205", "title": "Calculus I", "crn": "9100", "schd": "LEC", "stat": "F",
         "meetingTimes": json.dumps([{"meet_day": d, "start_time": "900", "end_time": "950"} for d in "13"])},
    ],
}

PAGE = """<!doctype html><html><body>
<select id="crit-srcdb"><option value="4272">Spring 2027</option><option value="4268">Fall 2026</option></select>
<select id="crit-subject"><option value="">Any</option>
<option value="CSE">Computer Science and Engineering (CSE)</option><option value="MATH">Mathematics (MATH)</option></select>
<button id="search-button">SEARCH</button><div id="results"></div>
<script>
const api = (route, payload, extra='') => fetch('/api/?page=fose&route=' + route + extra,
  {method: 'POST', headers: {'Content-Type': 'application/json'},
   body: encodeURIComponent(JSON.stringify(payload))}).then(r => r.json());
document.getElementById('search-button').onclick = async () => {
  const srcdb = document.getElementById('crit-srcdb').value, subj = document.getElementById('crit-subject').value;
  const data = await api('search', {other: {srcdb}, criteria: [{field: 'subject', value: subj}]}, '&subject=' + subj);
  document.getElementById('results').innerHTML = data.results.map(r =>
    `<a class="result__link" href="#" data-group="code:${r.code}" data-key="crn:${r.crn}">${r.code}</a>`).join('');
  document.querySelectorAll('.result__link').forEach(a => a.onclick = e => { e.preventDefault();
    api('details', {group: a.dataset.group, key: a.dataset.key, srcdb, matched: a.dataset.key}); });
};
</script></body></html>"""


PAGE_ALT = """<!doctype html><html><body>
<select id="termSel"><option value="">Choose</option><option value="4272">Spring 2027</option></select>
<select id="subjSel"><option value="">Any</option>
<option value="CSE">Computer Science &amp; Engineering (CSE)</option><option value="MATH">Mathematics (MATH)</option></select>
<button class="go">Search</button><div id="results"></div>
<script>
document.querySelector('.go').onclick = async () => {
  const term = document.getElementById('termSel').value, s = document.getElementById('subjSel').value;
  const r = await fetch('/api/?page=fose&route=search&subj=' + s, {method: 'POST',
     headers: {'Content-Type': 'application/json'},
     body: JSON.stringify({other: {srcdb: term}, criteria: [{field: 'subj', value: s}]})});
  const data = await r.json();
  document.getElementById('results').innerHTML = data.results.map(x =>
    `<div class="result"><a href="#" data-c="${x.code}" data-n="${x.crn}">${x.code}</a></div>`).join('');
  document.querySelectorAll('.result a').forEach(a => a.onclick = e => { e.preventDefault();
    fetch('/api/?page=fose&route=details', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({group: 'code:' + a.dataset.c, key: 'crn:' + a.dataset.n, srcdb: term})}); });
};
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    page = PAGE
    seen = []

    def log_message(self, *args):
        pass

    def _send(self, body, ctype):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(self.page, "text/html")

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"])).decode()
        if self.page is PAGE:
            payload = json.loads(urllib.parse.unquote(raw))   # this site only accepts the quoted format
            field = "subject"
        else:
            try:
                payload = json.loads(raw)                     # this one only accepts raw JSON
            except json.JSONDecodeError:
                self._send(json.dumps({"fatal": "Invalid request"}), "application/json")
                return
            field = "subj"
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        route = query["route"][0]
        Handler.seen.append((route, payload))
        if route == "search":
            assert payload["other"]["srcdb"] == "4272"
            crit = {c["field"]: c["value"] for c in payload["criteria"]}
            subject = crit.get(field, "")                    # unknown field -> "any subject"
            if self.page is not PAGE:
                assert query["subj"][0] == subject
            if not subject:
                if crit.get("stat") == "A,F,C,W":             # UofL format: whole term
                    allrows = [r for rows in SECTIONS.values() for r in rows]
                    self._send(json.dumps({"count": len(allrows), "results": allrows}), "application/json")
                else:                                         # server ignores bad criteria
                    self._send(json.dumps({"results": SECTIONS["CSE"]}), "application/json")
                return
            self._send(json.dumps({"results": SECTIONS.get(subject, [])}), "application/json")
        else:
            code = payload["group"].split(":", 1)[1]
            self._send(json.dumps({"title": code, "hours": "3", "description": f"About {code}",
                                   "registration_restrictions": "Prerequisite: x" if code == "CSE 220" else ""}),
                       "application/json")


@pytest.fixture(params=["standard", "different-format"])
def fake_site(request):
    Handler.seen = []
    Handler.page = PAGE if request.param == "standard" else PAGE_ALT
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_browser_sync_all_subjects(fake_site):
    srcdb, results, details = sync(fake_site, "Spring 2027", delay=0, log=lambda *_: None)
    assert srcdb == "4272"
    assert {r["crn"] for r in results} == {"7587", "8001", "9100"}
    assert details["MATH 205"]["description"] == "About MATH 205"

    records = {r["course_code"]: r for r in results_to_records(results, details)}
    assert set(records) == {"CSE220", "CSE310", "MATH205"}
    assert records["CSE220"]["hasPreReq"] and records["MATH205"]["credit"] == 3
    # every details request asked for a real course, one per course
    asked = sorted(p["group"] for r, p in Handler.seen if r == "details")
    assert asked[-3:] == ["code:CSE 220", "code:CSE 310", "code:MATH 205"]

    app = create_app(TestConfig)
    with app.app_context():
        assert upsert_courses(list(records.values())) == (3, 3)
        body = app.test_client().get("/api/courses?subject=MATH").get_json()
        assert body["courses"][0]["sections"][0]["status"] == "Full"


def test_only_some_subjects(fake_site):
    _, results, _ = sync(fake_site, "Spring 2027", subjects=["math"], details=False, delay=0, log=lambda *_: None)
    assert {r["crn"] for r in results} == {"9100"}


def test_unknown_term_is_clear(fake_site):
    with pytest.raises(RuntimeError, match="Spring 2027"):
        sync(fake_site, "Fall 2030", delay=0, log=lambda *_: None)


def test_standard_site_uses_one_request(fake_site):
    sync(fake_site, "4272", details=False, delay=0, log=lambda *_: None)
    searches = [p for r, p in Handler.seen if r == "search"
                and not any(c["field"] == "instmode" for c in p["criteria"])]
    if Handler.page is PAGE:
        assert len(searches) == 1          # whole term at once (plus the instmode lookups)
    else:
        assert len(searches) > 2           # fell back to recording the page, per subject
