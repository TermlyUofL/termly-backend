# Bringing CourseLeaf PATH data into Termly

UofL's class search, https://courseleafpath.louisville.edu/, is a CourseLeaf "FOSE" search page. It loads its results from a JSON API on the same host. Termly copies that public schedule data (no login) into MariaDB. The REST API and the Svelte frontend then read it from there.

## Load all courses (use this)

```bash
cd backend
python -m pip install -r requirements.txt

# MariaDB (schema.sql creates the tables; the app also creates them on start)
export DATABASE_URL=mysql+pymysql://termly:PASSWORD@localhost:3306/termly

python courseleaf_live.py --term "Spring 2027" --all-subjects --details   # every subject + details
python app.py                                                             # http://localhost:5000/api/courses
```

Plain HTTP currently gets past Cloudflare. If it starts getting blocked, use the browser version instead: `python -m playwright install chromium` once, then `python courseleaf_browser_sync.py --term "Spring 2027"`.

### Where each field comes from

The live search results have **no meeting times** (`meetingTimes` is empty on every row) and no instruction mode, so a sync is three steps:

1. One search returns every section in the term (~6,400 for Spring 2027).
2. Four more searches, filtered by `instmode` = `P` / `BL` / `OA` / `OS`, tag each section with its mode. If the server ignores the filter (a filtered result as big as the whole term, or a CRN under two modes), modes are left empty rather than guessed.
3. One details request per course (`{"group":"code:CSE 310","key":"","srcdb":"4272"}`). Its `all_sections` grid has every section's "Meets" text (`TR 11a-12:15p`, `Does Not Meet`), plus the credits, description, session and prerequisites.

Without details (`--no-details`), every section ends up in the Online box, so the schedule generator can't find conflicts. Details for a whole term are ~2,600 requests, run 2 at a time (`--workers`) over kept-alive connections, which takes roughly 10–15 minutes.

### UofL's request format (captured from the live site)

```
POST https://courseleafpath.louisville.edu/api/?page=fose&route=search&stat=A%2CF%2CC%2CW
Content-Type: application/json        X-Requested-With: XMLHttpRequest
body: URL-encoded {"other":{"srcdb":"4272"},"criteria":[{"field":"stat","value":"A,F,C,W"}]}
```

`srcdb` 4272 is Spring 2027. The status filter `A,F,C,W` (open, full, closed, waitlist) with no other criteria returns **every section in the term in one request**. The tests check that Termly sends this exact request, byte for byte.

`courseleaf_browser_sync.py` opens the site in Chromium first, as a normal visit, and sends the request from inside the page. If UofL ever changes the format, it falls back automatically: it clicks Search once, records the page's own request, checks that replaying it gives the same results, and repeats it for each subject.

Browser options: `--subject CSE --subject MATH` (only those subjects), `--no-details` (faster, but no meeting times, credits, descriptions or prerequisites), `--show` (watch the browser), `--dry-run` (don't write to the database), `--delay` (seconds between request starts, 0.3 by default), `--workers` (details requests at once, 2 by default).

The raw data is saved to `courseleaf_raw_<term>.json`. To reload it without fetching again, run `python courseleaf_live.py --from-raw courseleaf_raw_Spring_2027.json`.

**To refresh**, run the same command again. A course's sections are replaced with the current list from the site, and dropped or cancelled sections are removed.

## How site data maps to the Termly spec

| Termly field | From CourseLeaf |
|---|---|
| course_code / subject / course_number | `code` "CSE 220" → `CSE220`, `CSE`, `220` |
| course_name, course_career | `title` "Discrete Structures [UGRD]" → `Discrete Structures`; `acad_career` UGRD/GRAD/LAW/ME/DE → Undergraduate/Graduate/Law/Medicine/Dentistry |
| description, credit | details `description`, `hours_html` (first number in "1-3" ranges) |
| course_session | "1st Half of Term" → First Half, "2nd Half of Term" → Second Half, "Regular Full Term" → Regular Full, "Special Session" → Special; summer sessions keep the site's name |
| hasPreReq | details `registration_restrictions` or description mentions a prerequisite |
| meeting_days / start_time / end_time | details `all_sections` "Meets" `TR 11a-12:15p` → `"T R"`, `"11:00"`, `"12:15"`; "Does Not Meet"/TBA stay empty, so the section goes in the Online box |
| instruction_mode | `instmode` searches P / BL / OA / OS → In Person / Blended / Online Asynchronous / Online Synchronous |
| status | closed or waitlisted → `Full`, otherwise `Not Full`; cancelled sections are skipped |

All values are trimmed to the column sizes in `schema.sql`, so MariaDB strict mode accepts them.

## Tests

```bash
python -m pytest -q                                   # SQLite + a fake CourseLeaf site in Chromium
TERMLY_TEST_MARIADB_URL=mysql+pymysql://termly:PASSWORD@127.0.0.1:3306/termly \
  python -m pytest -q tests/test_conversion_edge_cases.py     # same checks against MariaDB
```

The browser tests run the sync against two fake CourseLeaf sites that use different request formats. That shows it adapts to whichever format a site uses.

## If the first real run fails

Rerun with `--show --subject CSE --dry-run` and read the error. It will say which step failed: the term, the Subject dropdown, the Search button, the replay check, or details. If the Search button or the result links have different names on UofL's page, add their selector to `SEARCH_BUTTON` or `RESULT_LINK` at the top of `courseleaf_browser_sync.py`.

## Other loaders

- If `courseleaf_live.py` gets blocked by Cloudflare (the error says so), you can also pass `--cookie "..." --user-agent "..."` copied from your browser. Never commit those values: the cookie identifies your browser session.
- `courseleaf_import.py`: load an authorized JSON export file.

## Limitations

- There is no term column. Use one database per term, which you choose with `DATABASE_URL`.
- A section with two different meeting patterns (for example MW 10:00 and F 13:00) keeps only the first one, because the schema stores one time per section. Adding a `meetings` table would fix this.
- A cross-listed section that shares one CRN across two course codes is stored under the first code only, because CRN is the primary key.
- Termly only plans schedules. Students still register in CourseLeaf PATH.
