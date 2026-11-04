import os


class Config:
    # MariaDB in production, e.g.
    #   DATABASE_URL=mysql+pymysql://termly:password@localhost:3306/termly
    # Falls back to a local SQLite file so the API can run without MariaDB.
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///termly_dev.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # Origin of the Svelte dev server (comma-separated for several)
    CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")

    # Caps for the schedule generator
    DEFAULT_SCHEDULE_LIMIT = 50
    MAX_SCHEDULE_LIMIT = 200


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
