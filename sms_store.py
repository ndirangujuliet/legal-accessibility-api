"""
sms_store.py — lightweight SQLite logging for outgoing and incoming SMS.

Why SQLite and not just another in-memory dict: SMS logs are exactly the
kind of thing you want to survive an app restart (e.g. Render redeploying
your service, or a crash) so you don't lose your demo data mid-hackathon.

IMPORTANT CAVEAT for Render's free tier specifically: the filesystem there
is EPHEMERAL — it resets whenever the service redeploys or spins down/up
again after idling. So this file protects you against in-process restarts,
but not against a fresh Render deploy wiping the disk. For anything beyond
a hackathon demo, swap this for a hosted database (e.g. Render's free
Postgres tier) — the query functions below are written narrowly enough
that swapping the storage backend later is a small, contained change.
"""

import sqlite3
import logging
import os
from datetime import datetime
from contextlib import contextmanager

logger = logging.getLogger("haki-legal-aid.sms_store")

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sms_logs.db")
DB_PATH = os.environ.get("SMS_DB_PATH", DEFAULT_DB_PATH)


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Creates the sms_logs table if it doesn't already exist. Call once at startup."""
    db_directory = os.path.dirname(DB_PATH)
    if db_directory:
        os.makedirs(db_directory, exist_ok=True)

    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sms_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                direction TEXT NOT NULL,        -- 'outgoing' or 'incoming'
                phone_hash TEXT NOT NULL,        -- pseudonymised, never the raw number
                message TEXT NOT NULL,
                status TEXT,                     -- e.g. 'sent', 'failed' (outgoing only)
                at_message_id TEXT,               -- Africa's Talking's own message/linkId if available
                created_at TEXT NOT NULL
            )
            """
        )
    logger.info("sms_logs table ready at %s", DB_PATH)


def log_sms(direction: str, phone_hash: str, message: str, status: str = None, at_message_id: str = None):
    """
    Records one SMS event. Never raises — logging failures shouldn't break
    the SMS send/receive flow itself, so callers can call this and move on.
    """
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO sms_logs (direction, phone_hash, message, status, at_message_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (direction, phone_hash, message, status, at_message_id, datetime.utcnow().isoformat()),
            )
    except Exception as exc:  # noqa: BLE001 - logging must never crash the caller
        logger.error("Failed to log SMS (%s): %s", direction, exc)


def get_logs(limit: int = 100, direction: str = None):
    """
    Returns recent SMS logs, newest first. Optionally filter by direction
    ('outgoing' or 'incoming'). Used by the /admin/sms/logs endpoint.
    """
    query = "SELECT id, direction, phone_hash, message, status, at_message_id, created_at FROM sms_logs"
    params = ()
    if direction:
        query += " WHERE direction = ?"
        params = (direction,)
    query += " ORDER BY id DESC LIMIT ?"
    params = params + (limit,)

    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()

    columns = ["id", "direction", "phone_hash", "message", "status", "at_message_id", "created_at"]
    return [dict(zip(columns, row)) for row in rows]