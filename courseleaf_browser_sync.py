"""Load every course from CourseLeaf PATH into Termly using a real browser.

UofL's class search API (captured from the live site, Nov 2026):

    POST https://courseleafpath.louisville.edu/api/?page=fose&route=search&stat=A%2CF%2CC%2CW
    Content-Type: application/json, X-Requested-With: XMLHttpRequest
    body = encodeURIComponent('{"other":{"srcdb":"4272"},"criteria":[{"field":"stat","value":"A,F,C,W"}]}')

srcdb 4272 = Spring 2027. Searching with only the status filter A,F,C,W
(open, full, closed, waitlist) returns EVERY section in the term in one
request, so no subject-by-subject loop is needed.

The site sits behind Cloudflare. Plain HTTP (courseleaf_live.py) currently
gets through and is simpler; use this script if it starts getting blocked.
This opens the site in Chromium (which passes the check like a normal
visit) and sends the requests from inside the page. If UofL ever changes
the format, it falls back to clicking Search once, recording the page's
own request and replaying that.

One-time setup:
    python -m pip install -r requirements.txt
    python -m playwright install chromium

Load all courses for a term:
    python courseleaf_browser_sync.py --term "Spring 2027"

Options:
    --subject CSE --subject MATH   only these subjects
    --no-details                   skip details (much faster, but no meeting times or credits)
    --show                         show the browser window (use this if Cloudflare asks you to click)
    --dry-run                      fetch and save raw JSON, don't touch the database

Raw data is always saved to courseleaf_raw_<term>.json; reload it any time with
    python courseleaf_live.py --from-raw courseleaf_raw_<term>.json
"""
import argparse
import json
import re
import sys
import time
import urllib.parse

from courseleaf_live import BASE_URL, INSTRUCTION_MODES, STAT_ALL, is_cancelled, results_to_records

SEARCH_BUTTON = ["#search-button", "button:has-text('Search')", "input[type=submit][value*=earch i]"]
RESULT_LINK = [".result__link", ".result a", "a[data-key]", "[data-key^='crn:']"]
API_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/javascript, */*; q=0.01",
               "X-Requested-With": "XMLHttpRequest"}


# ------------------------------------------------------- request templates

def decode_body(raw):
    """Return (payload, style) for a captured POST body."""
    raw = raw or ""
    try:
        return json.loads(raw), "json"
    except json.JSONDecodeError:
        return json.loads(urllib.parse.unquote(raw)), "quoted"


def encode_body(payload, style):
    text = json.dumps(payload, separators=(",", ":"))
    return urllib.parse.quote(text, safe="") if style == "quoted" else text


def replace_values(obj, old, new, substring=False):
    """Copy of obj with string values equal to (or containing) `old` replaced."""
    if isinstance(obj, dict):
        return {k: replace_values(v, old, new, substring) for k, v in obj.items()}
    if isinstance(obj, list):
        return [replace_values(v, old, new, substring) for v in obj]
    if isinstance(obj, str):
        if obj == old:
            return new
        if substring and old in obj:
            return obj.replace(old, new)
    return obj


def contains_value(obj, target, substring=False):
    return json.dumps(replace_values(obj, target, "\x00MARK\x00", substring)) != json.dumps(obj)


def replace_query_values(url, old, new):
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(k, new if v == old else v) for k, v in query]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


class Template:
    """A request (known or captured from the page) replayed with new values."""

    def __init__(self, url, method, headers, payload, style):
        self.url, self.method, self.headers = url, method, headers
        self.payload, self.style = payload, style

    @classmethod
    def from_request(cls, request):
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() in ("content-type", "accept", "x-requested-with")}
        payload, style = decode_body(request.post_data)
        return cls(request.url, request.method, headers, payload, style)

    def build_search(self, original_subject, subject):
        if not (contains_value(self.payload, original_subject)
                or f"={urllib.parse.quote(original_subject)}" in self.url):
            raise RuntimeError(f"subject {original_subject!r} not found in the captured search request: "
                               f"{self.url} {self.payload}")
        payload = replace_values(self.payload, original_subject, subject)
        return replace_query_values(self.url, original_subject, subject), encode_body(payload, self.style)

    def build_details(self, original, code, crn):
        """original = (code, crn) of the result the page opened."""
        orig_code, orig_crn = original
        payload = replace_values(self.payload, orig_crn, crn, substring=True)
        payload = replace_values(payload, orig_code, code, substring=True)
        url = replace_query_values(replace_query_values(self.url, orig_crn, crn), orig_code, code)
        return url, encode_body(payload, self.style)


# ---------------------------------------------- UofL's known request format

def known_search(base_url, srcdb, subject=None, extra=()):
    """Exactly what courseleafpath.louisville.edu sends (see module docstring)."""
    criteria = []
    if subject:
        criteria.append({"field": "subject", "value": subject})
    criteria.extend(extra)
    criteria.append({"field": "stat", "value": STAT_ALL})
    query = urllib.parse.urlencode({"page": "fose", "route": "search",
                                    **{c["field"]: c["value"] for c in criteria}})
    body = encode_body({"other": {"srcdb": srcdb}, "criteria": criteria}, "quoted")
    return f"{base_url}/api/?{query}", body


def known_details(base_url, srcdb, code):
    """Whole-course details; `all_sections` has every section's meeting times."""
    body = encode_body({"group": f"code:{code}", "key": "", "srcdb": srcdb}, "quoted")
    return f"{base_url}/api/?page=fose&route=details", body


# ------------------------------------------------------------ page helpers

def _first_visible(page, selectors, timeout=15000):
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        for sel in selectors:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                return loc.first
        page.wait_for_timeout(250)
    raise RuntimeError(f"couldn't find any of {selectors} on the page")


def read_options(page, select_id):
    return page.eval_on_selector_all(
        f"#{select_id} option",
        "opts => opts.filter(o => o.value).map(o => [o.value, o.textContent.trim()])")


def find_select_id(page, contains_label):
    """id of the <select> that has an option labelled `contains_label`."""
    return page.evaluate(
        """label => { for (const s of document.querySelectorAll('select'))
               for (const o of s.options)
                 if (o.textContent.trim().toLowerCase() === label.toLowerCase()) return s.id;
             return null; }""", contains_label)


def find_subject_select(page):
    """The <select> whose options look like 'Accounting (ACCT)'."""
    return page.evaluate(
        """() => { let best = null, score = 0;
             for (const s of document.querySelectorAll('select')) {
               const n = [...s.options].filter(o => /\\([A-Z]{2,5}\\)\\s*$/.test(o.textContent.trim())).length;
               if (n > score) { best = s.id; score = n; } }
             return best; }""")


def resolve_subjects(options, wanted):
    """Map user codes ('CSE') to the page's option values (label '... (CSE)')."""
    if not wanted:
        return [v for v, _ in options]
    out = []
    for code in wanted:
        code = code.strip().upper()
        match = next((v for v, label in options
                      if v.upper() == code or label.upper().endswith(f"({code})")), None)
        if match is None:
            raise RuntimeError(f"subject {code!r} not on the page")
        out.append(match)
    return out


def fetch_json(page, url, body, headers=API_HEADERS, method="POST"):
    text = page.evaluate(
        """async ([url, method, headers, body]) => {
               const r = await fetch(url, {method, headers, body, credentials: 'include'});
               return await r.text(); }""",
        [url, method, headers, body])
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError(f"not JSON (Cloudflare check or error page?): {text[:150]!r}")
    if isinstance(data, dict) and data.get("fatal"):
        raise RuntimeError(data["fatal"])
    return data


def fetch_json_many(page, requests, workers=2, gap_ms=300, headers=API_HEADERS):
    """POST [(url, body), ...] from inside the page, `workers` at a time, starts
    at least `gap_ms` apart. Returns parsed JSON per request, or None on failure
    (after up to 3 tries with back-off)."""
    texts = page.evaluate(
        """async ([reqs, workers, gap, headers]) => {
            const out = new Array(reqs.length).fill(null), sleep = ms => new Promise(r => setTimeout(r, ms));
            let next = 0, last = 0;
            const slot = async () => { for (;;) { const wait = last + gap - Date.now();
                if (wait <= 0) { last = Date.now(); return; } await sleep(wait); } };
            const worker = async () => {
              while (next < reqs.length) {
                const i = next++;
                for (let attempt = 0; attempt < 3 && out[i] === null; attempt++) {
                  if (attempt) await sleep(1000 * 2 ** attempt);
                  await slot();
                  try { const r = await fetch(reqs[i][0], {method: 'POST', headers, body: reqs[i][1],
                                                           credentials: 'include'});
                        const t = await r.text();
                        if (r.ok && t.trim()) out[i] = t; else if (r.status < 500 && r.status !== 429) break;
                  } catch (e) {}
                }
              } };
            await Promise.all(Array.from({length: Math.min(workers, reqs.length)}, worker));
            return out; }""",
        [list(requests), workers, gap_ms, headers])
    out = []
    for text in texts:
        try:
            data = json.loads(text) if text else None
        except json.JSONDecodeError:
            data = None
        out.append(None if isinstance(data, dict) and data.get("fatal") else data)
    return out


def fetch_modes(page, base_url, srcdb, total, log):
    """{crn: 'OA', ...}; empty if the server ignores the instmode filter."""
    modes = {}
    for value in INSTRUCTION_MODES:
        try:
            rows = _rows(fetch_json(page, *known_search(base_url, srcdb, extra=[{"field": "instmode",
                                                                                   "value": value}])))
        except Exception as exc:
            log(f"  instmode {value} failed: {exc}")
            continue
        crns = {str(r.get("crn")) for r in rows}
        if (total and len(crns) >= total) or crns & modes.keys():
            log("  instmode filter ignored by the server; modes left empty")
            return {}
        modes.update(dict.fromkeys(crns, value))
    return modes


def replay(page, template, url, body):
    return fetch_json(page, url, body, template.headers, template.method)


def _rows(data):
    return data.get("results", []) if isinstance(data, dict) else []


def _crns(data):
    rows = _rows(data)
    return rows, {str(r.get("crn")) for r in rows}


def _retry(fn, log, label, tries=3, wait_ms=2000, page=None):
    for attempt in range(tries):
        try:
            return fn()
        except Exception as exc:
            if attempt == tries - 1:
                log(f"  {label}: FAILED ({exc})")
                return None
            if page is not None:
                page.wait_for_timeout(wait_ms * (attempt + 1))


# ------------------------------------------------------------ fallback

def capture_templates(page, subject_select, first, want_details, log):
    """Click Search once and record the page's own requests."""
    page.select_option(f"#{subject_select}", value=first)
    with page.expect_response(lambda r: "route=search" in r.url, timeout=30000) as resp:
        _first_visible(page, SEARCH_BUTTON).click()
    search_tpl = Template.from_request(resp.value.request)
    page_rows, page_crns = _crns(resp.value.json())
    page.wait_for_load_state("networkidle")

    url, body = search_tpl.build_search(first, first)
    _, replay_crns = _crns(replay(page, search_tpl, url, body))
    if replay_crns != page_crns:
        raise RuntimeError(f"replay check failed for {first}: page got {len(page_crns)} sections, "
                           f"replay got {len(replay_crns)}")
    log(f"  request format captured and verified ({len(page_crns)} sections for {first})")

    details_tpl = original = None
    if want_details and page_rows:
        try:
            with page.expect_request(lambda r: "route=details" in r.url, timeout=15000) as req:
                _first_visible(page, RESULT_LINK).click()
            details_tpl = Template.from_request(req.value)
            original = next(((str(r["code"]), str(r["crn"])) for r in page_rows
                             if contains_value(details_tpl.payload, str(r["crn"]), substring=True)
                             or str(r["crn"]) in details_tpl.url), None)
            if original is None:
                raise RuntimeError("opened result's CRN not found in the details request")
        except Exception as exc:
            log(f"  (no details: {exc}) -- credits/descriptions will be empty")
            details_tpl = None
    return search_tpl, page_crns, details_tpl, original


# ------------------------------------------------------------------- sync

def sync(base_url, term, subjects=None, details=True, show=False, delay=0.3, log=print, workers=2):
    from playwright.sync_api import sync_playwright

    base_url = base_url.rstrip("/")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not show)
        page = browser.new_page()
        page.goto(base_url + "/", wait_until="networkidle")
        if show:
            # give a human time to pass a Cloudflare check if one appears
            page.wait_for_selector("select", timeout=120000)

        # --- term (label 'Spring 2027' or code '4272')
        term = term.strip()
        if term.isdigit():
            term_select = page.evaluate("t => [...document.querySelectorAll('select')].find("
                                        "s => [...s.options].some(o => o.value === t))?.id || null", term)
        else:
            term_select = find_select_id(page, term)
        if not term_select:
            labels = page.eval_on_selector_all("select option", "o => o.map(x => x.textContent.trim())")
            seen = [l for l in labels if re.search(r"(Spring|Summer|Fall|Winter) \d{4}", l)]
            if not labels:
                raise RuntimeError("the search page didn't load (Cloudflare check?). Rerun with --show.")
            raise RuntimeError(f"term {term!r} not on the page. Terms seen: " + ", ".join(seen))
        srcdb = term if term.isdigit() else next(
            v for v, label in read_options(page, term_select) if label.lower() == term.lower())
        page.select_option(f"#{term_select}", value=srcdb)

        subject_select = find_subject_select(page)
        subject_values = (resolve_subjects(read_options(page, subject_select), subjects)
                          if subjects and subject_select else [s.upper() for s in (subjects or [])])
        log(f"Term {term} (srcdb {srcdb})")

        # --- 1) UofL's known format: whole term in one request
        results, mode = None, "known"
        try:
            if subject_values:
                results = []
                for subject in subject_values:
                    rows = _rows(fetch_json(page, *known_search(base_url, srcdb, subject)))
                    log(f"  {subject}: {len(rows)} sections")
                    results.extend(rows)
                    page.wait_for_timeout(int(delay * 1000))
            else:
                data = fetch_json(page, *known_search(base_url, srcdb))
                results = _rows(data)
                count = data.get("count") if isinstance(data, dict) else None
                log(f"  whole term: {len(results)} sections" + (f" (server count {count})" if count else ""))
                if isinstance(count, int) and count > len(results):
                    log("  server returned fewer sections than it counted; switching to per-subject requests")
                    results = None
            if results is not None and not results:
                raise RuntimeError("no sections returned")
        except Exception as exc:
            log(f"  known request format didn't work ({exc}); falling back to recording the page")
            results = None

        # --- 2) fallback: record the page's own request, loop over subjects
        details_tpl = original = None
        if results is None:
            mode = "captured"
            if not subject_select:
                raise RuntimeError("couldn't find the Subject dropdown on the page")
            wanted = subject_values or resolve_subjects(read_options(page, subject_select), None)
            search_tpl, first_crns, details_tpl, original = capture_templates(
                page, subject_select, wanted[0], details, log)
            results = []
            for i, subject in enumerate(wanted, 1):
                url, body = search_tpl.build_search(wanted[0], subject)
                data = _retry(lambda: replay(page, search_tpl, url, body), log, subject, page=page) or {}
                rows, crns = _crns(data)
                if crns and subject != wanted[0] and crns == first_crns:
                    raise RuntimeError(f"{subject} returned exactly the same sections as {wanted[0]}; "
                                       "the subject isn't being substituted correctly")
                results.extend(rows)
                log(f"  [{i}/{len(wanted)}] {subject}: {len(rows)} sections")
                page.wait_for_timeout(int(delay * 1000))

        # --- instruction modes: search rows don't include them
        if mode == "known":
            modes = fetch_modes(page, base_url, srcdb, 0 if subject_values else len(results), log)
            for row in results:
                if str(row.get("crn")) in modes:
                    row.setdefault("instmode", modes[str(row.get("crn"))])
            log(f"  instruction mode known for {sum(1 for r in results if r.get('instmode'))} sections")

        # --- details: one request per course (meeting times live here)
        details_out = {}
        if details:
            by_code = {}
            for row in results:
                if not is_cancelled(row) and row.get("code") and row.get("crn"):
                    by_code.setdefault(str(row["code"]), []).append(str(row["crn"]))
            log(f"Fetching details for {len(by_code)} courses ...")

            if mode == "known":
                # try the known format on one course, then fetch the rest in parallel
                codes = list(by_code)
                first = fetch_json_many(page, [known_details(base_url, srcdb, codes[0])])[0] if codes else None
                if isinstance(first, dict) and first:
                    details_out[codes[0]] = first
                    failed = 0
                    for start in range(1, len(codes), 200):
                        chunk = codes[start:start + 200]
                        got = fetch_json_many(page, [known_details(base_url, srcdb, c) for c in chunk],
                                              workers, int(delay * 1000))
                        for code, det in zip(chunk, got):
                            if det is None:
                                failed += 1
                            elif isinstance(det, dict):
                                details_out[code] = det
                        log(f"  {min(start + len(chunk), len(codes))}/{len(codes)}")
                    if failed:
                        log(f"  details failed for {failed} courses (no times or credits for those)")
                    by_code = {}          # done

            def request_for(code, crn):
                if details_tpl:
                    return replay, details_tpl, details_tpl.build_details(original, code, crn)
                return None, None, known_details(base_url, srcdb, code)

            failed = 0
            for i, (code, crns) in enumerate(by_code.items(), 1):
                fn, tpl, (url, body) = request_for(code, crns[0])
                try:
                    det = fn(page, tpl, url, body) if fn else fetch_json(page, url, body)
                except Exception as exc:
                    det = {}
                    failed += 1
                    if i == 1 and mode == "known" and not details_tpl:
                        # known details format rejected: try recording the page's own
                        log(f"  details request rejected ({exc}); recording the page's own")
                        try:
                            if subject_select:
                                first = resolve_subjects(read_options(page, subject_select),
                                                         [code.split(" ")[0]])[0]
                                _, _, details_tpl, original = capture_templates(
                                    page, subject_select, first, True, log)
                        except Exception as exc2:
                            log(f"  couldn't record details request ({exc2})")
                        if details_tpl:
                            failed -= 1
                            try:
                                url, body = details_tpl.build_details(original, code, crns[0])
                                det = replay(page, details_tpl, url, body)
                            except Exception:
                                failed += 1
                        else:
                            log("  continuing without details (credits/descriptions empty)")
                            break
                if isinstance(det, dict) and det:
                    details_out[code] = {**det, "_crn": crns[0]}   # which section these details belong to
                if i % 100 == 0:
                    log(f"  {i}/{len(by_code)}")
                page.wait_for_timeout(int(delay * 1000))
            if failed:
                log(f"  details failed for {failed} courses (they load without description/credits)")

        browser.close()
    return srcdb, results, details_out


def main(argv=None):
    p = argparse.ArgumentParser(description="Load all CourseLeaf PATH courses into Termly")
    p.add_argument("--term", required=True, help="e.g. 'Spring 2027' (as shown on the site) or 4272")
    p.add_argument("--subject", action="append", help="limit to these subjects (repeatable)")
    p.add_argument("--no-details", action="store_true")
    p.add_argument("--show", action="store_true", help="show the browser window")
    p.add_argument("--delay", type=float, default=0.3)
    p.add_argument("--workers", type=int, default=2, help="details requests at once")
    p.add_argument("--base-url", default=BASE_URL)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    srcdb, results, details = sync(args.base_url, args.term, args.subject,
                                   details=not args.no_details, show=args.show, delay=args.delay,
                                   workers=args.workers)
    slug = re.sub(r"\W+", "_", args.term).strip("_")
    raw_path = f"courseleaf_raw_{slug}.json"
    with open(raw_path, "w", encoding="utf-8") as fh:
        json.dump({"srcdb": srcdb, "results": results, "details": details}, fh)
    records = results_to_records(results, details, log=print)
    n_sec = sum(len(r["sections"]) for r in records)
    print(f"Got {len(records)} courses / {n_sec} sections (raw saved to {raw_path})")
    if args.dry_run:
        return 0
    if not records:
        print("Nothing returned; database left unchanged.")
        return 1

    from app import create_app
    from loader import upsert_courses
    with create_app().app_context():
        c, s = upsert_courses(records)
    print(f"Loaded {c} courses and {s} sections into Termly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
