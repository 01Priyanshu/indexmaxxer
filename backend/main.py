import psycopg2
from fastapi import FastAPI, HTTPException

from backend.db import get_connection

app = FastAPI(title="IndexMaxxer", version="0.1.0")


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
