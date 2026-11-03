"""
Task 4: On-Disk SQLite Database Layer for Rate Limiting
Enforces WAL mode, busy timeout, and index optimization for token sliding window events.
"""

import sqlite3
import os
import logging

logger = logging.getLogger("llm.gateway.db")


def init_sqlite_db(db_path: str) -> sqlite3.Connection:
    """
    Initializes on-disk SQLite database with Write-Ahead Logging (WAL) mode
    and schema for tracking token usage events.
    """
    db_dir = os.path.dirname(os.path.abspath(db_path))
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(
        db_path,
        timeout=10.0,
        isolation_level=None,  # autocommit mode; we manage explicit transactions
        check_same_thread=False  # Allow thread-pool execution protected by locks
    )

    # Enable WAL mode and performance optimizations
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")

    # Create token events table and index
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS token_usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                token_count INTEGER NOT NULL
            );
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tenant_timestamp 
            ON token_usage_events (tenant_id, timestamp);
        """)

    logger.info("Initialized on-disk SQLite database at %s (WAL mode active)", db_path)
    return conn
