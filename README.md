# Termly Backend

Flask REST API for Termly, built from the Termly Data Spec (Classes and Filters). It serves course and section data, filter options, and conflict-free schedule suggestions. SQLite is used by default for local development; MariaDB is supported for deployment.

## Requirements

- Python
- CourseLeaf PATH access to import live course data (optional)
- MariaDB for deployment (optional)

## Run locally

Create a virtual environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
```

With no `DATABASE_URL`, the app uses SQLite at `instance/termly_dev.db` and creates its tables at startup. To load real data, first check available terms, then sync a subject with section details:

```bash
python courseleaf_live.py --list-terms
python courseleaf_live.py --term "Spring 2027" --subject CSE --details
```

Start the development server:

```bash
flask --app app run --debug
```

The API is available at <http://localhost:5000>; check it with `curl http://localhost:5000/api/health`. `--details` retrieves meeting times required for calendar conflict checking. For a full term, replace `--subject CSE` with `--all-subjects`; this can take 10–15 minutes. See [COURSELEAF_SETUP.md](COURSELEAF_SETUP.md) for import options and troubleshooting.

To try the API without fetching data, load the included sample dataset instead:

```bash
python loader.py data/sample_courses.json
```

Delete `instance/termly_dev.db` to reset the local database.

## Database configuration

For MariaDB, create the schema and set `DATABASE_URL` before importing data or starting the app:

```bash
mysql -u root -p < schema.sql
export DATABASE_URL="mysql+pymysql://termly:password@localhost:3306/termly"
```

Then load data with `courseleaf_live.py` or `loader.py` as shown above, and start the app with `flask --app app run`. `CORS_ORIGINS` controls allowed browser origins; it defaults to `http://localhost:5173` and accepts comma-separated origins.

## API

All endpoints are under `/api`.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `GET` | `/filters` | Filter dropdown options |
| `GET` | `/courses` | Search and filter courses; supports `q`, `subject`, `session`, `mode`, `career`, `has_prereq`, `open_only`, `page`, and `per_page` |
| `GET` | `/courses/<course_code>` | Course and its sections |
| `GET` | `/sections?crns=7587,8101` | Section details for saved CRNs |
| `GET` | `/sections/<crn>` | One section |
| `POST` | `/schedules/generate` | Generate schedules for requested course codes |

Example schedule request:

```json
{
  "course_codes": ["CSE220", "CSE310"],
  "excluded_times": [{"day": "MWF", "start": "08:00", "end": "10:00"}],
  "include_full": false,
  "modes": ["In Person"],
  "limit": 50
}
```

Only `course_codes` is required. Schedule plans, excluded times, favorites, and total credits are managed by the frontend; the backend accepts them in requests as needed.

### Section placement and schedule behavior

- A section is placed in the Online box if it has no meeting days or its `instruction_mode` is `Online Asynchronous`; otherwise it appears on the calendar. Online Synchronous sections have meeting times and appear on the calendar.
- Schedules include one section of each `section_type` a course offers (for example, one lecture and one lab).
- Full sections are skipped unless `include_full` is true.
- Meeting times are stored as 24-hour `HH:MM`. The parser also accepts forms such as `3:45 p` and `2:20 pm`.
- `Tr` / `Th` mean Thursday; `TR` means Tuesday and Thursday.

## Tests

```bash
pytest -q tests
```

The tests use in-memory SQLite and require no database server or network access.

## Project files

| File | Purpose |
|---|---|
| `app.py` | Flask app and API routes |
| `models.py` | Course and section database models |
| `scheduler.py` | Conflict-free schedule generation |
| `timeutils.py` | Meeting day and time parsing |
| `loader.py` | Upsert course and section JSON into the database |
| `courseleaf_live.py` | Sync data from CourseLeaf PATH |
| `schema.sql` | MariaDB schema |
