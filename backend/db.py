from contextlib import contextmanager
from pathlib import Path

import psycopg2
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@contextmanager
def get_connection():
    config = dotenv_values(PROJECT_ROOT / ".env")
    dsn = config.get("DATABASE_URL")

    if not dsn:
        raise RuntimeError("DATABASE_URL is missing from .env")

    conn = psycopg2.connect(
        dsn,
        connect_timeout=5,
        application_name="indexmaxxer",
    )
    try:
        with conn:
            yield conn
    finally:
        conn.close()
