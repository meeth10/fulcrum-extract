"""SQLite schema for the structured financial line-item store.

Every stored number carries provenance so the agent can distinguish human
uploads from machine-retrieved exchange filings.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity          TEXT NOT NULL,
    doc_type        TEXT NOT NULL,
    fiscal_year     TEXT,
    filepath        TEXT NOT NULL,
    source_type     TEXT NOT NULL DEFAULT 'MANUAL_UPLOAD',
    ingested_at     TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS line_items (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id           INTEGER NOT NULL REFERENCES documents(id),
    entity                TEXT NOT NULL,
    period                TEXT NOT NULL,
    statement             TEXT NOT NULL,
    metric                TEXT NOT NULL,
    metric_raw            TEXT,
    value                 REAL,
    unit                  TEXT,
    consolidated          INTEGER,
    source_page           INTEGER,
    source_table          TEXT,
    extraction_method     TEXT,
    extraction_confidence REAL,
    source_type           TEXT NOT NULL DEFAULT 'MANUAL_UPLOAD',
    created_at            TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_line_items_lookup
    ON line_items (entity, metric, period, statement);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    _ensure_column(conn, "documents", "source_type", "TEXT NOT NULL DEFAULT 'MANUAL_UPLOAD'")
    _ensure_column(conn, "line_items", "source_type", "TEXT NOT NULL DEFAULT 'MANUAL_UPLOAD'")
    _ensure_column(conn, "line_items", "source_heading", "TEXT")
    conn.commit()
    return conn
