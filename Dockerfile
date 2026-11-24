FROM docker.io/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Runtime deps only (pytest/playwright are for tests and live CourseLeaf syncs).
COPY requirements.txt .
RUN grep -viE '^(pytest|playwright)' requirements.txt > requirements.runtime.txt \
    && pip install -r requirements.runtime.txt gunicorn

COPY . .

# Bake the bundled course data into a SQLite database inside the image.
# Override DATABASE_URL at runtime (e.g. MariaDB) to use an external database instead.
ENV DATABASE_URL=sqlite:////app/db/termly.db
RUN mkdir -p db \
    && python courseleaf_live.py --from-raw data/spring-2027.json \
    && useradd --system --uid 10001 termly \
    && chmod -R a+rX /app \
    && chown -R termly db

USER termly

ENV GUNICORN_CMD_ARGS="--bind 0.0.0.0:5000 --workers 3 --access-logfile -"
EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]

CMD ["gunicorn", "app:create_app()"]
