import logging

import psycopg2
from fastapi import FastAPI, HTTPException

from backend.db import PROJECT_ROOT, get_connection
from backend.recommendations import load_latest_recommendations

app = FastAPI(title="IndexMaxxer", version="0.1.0")
logger = logging.getLogger(__name__)


@app.get("/")
def root():
    return {"project": "IndexMaxxer", "docs": "/docs"}


@app.get("/health")
def health():
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database(), version()")
                database, postgres_version = cur.fetchone()
    except (psycopg2.Error, RuntimeError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Database check failed. Check .env and PostgreSQL.",
        ) from exc

    return {
        "status": "ok",
        "database": database,
        "postgres_version": postgres_version,
    }


@app.get("/api/recommendations")
def recommendations():
    """Return recommendations from the latest completed comparison."""
    try:
        return load_latest_recommendations(PROJECT_ROOT / "results")
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail="No completed index comparison report is available.",
        ) from exc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.exception("Could not load index recommendations")
        raise HTTPException(
            status_code=500,
            detail="The saved experiment report could not be read.",
        ) from exc
