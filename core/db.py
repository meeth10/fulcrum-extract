"""Thin data-access layer over the SQLite store."""

import sqlite3
from dataclasses import dataclass
from typing import Optional

from .schema import init_db


@dataclass
class LineItem:
    entity: str
    period: str
    statement: str
    metric: str
    metric_raw: str
    value: float
    unit: str
    consolidated: Optional[bool]
    source_page: Optional[int]
    source_table: Optional[str]
    extraction_method: str
    extraction_confidence: Optional[float]
    source_type: str = "MANUAL_UPLOAD"
    source_heading: Optional[str] = None


def add_document(conn: sqlite3.Connection, entity: str, doc_type: str,
                  fiscal_year: str, filepath: str,
                  source_type: str = "MANUAL_UPLOAD") -> int:
    cur = conn.execute(
        "INSERT INTO documents (entity, doc_type, fiscal_year, filepath, source_type) VALUES (?, ?, ?, ?, ?)",
        (entity, doc_type, fiscal_year, filepath, source_type),
    )
    conn.commit()
    return cur.lastrowid


def add_line_item(conn: sqlite3.Connection, document_id: int, item: LineItem) -> int:
    cur = conn.execute(
        """INSERT INTO line_items
           (document_id, entity, period, statement, metric, metric_raw, value,
            unit, consolidated, source_page, source_table, extraction_method,
            extraction_confidence, source_type, source_heading)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (document_id, item.entity, item.period, item.statement, item.metric,
         item.metric_raw, item.value, item.unit,
         None if item.consolidated is None else int(item.consolidated),
         item.source_page, item.source_table, item.extraction_method,
         item.extraction_confidence, item.source_type, item.source_heading),
    )
    conn.commit()
    return cur.lastrowid


def get_line_item(conn: sqlite3.Connection, entity: str, metric: str, period: str,
                   statement: Optional[str] = None,
                   consolidated: Optional[bool] = None) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    query = "SELECT * FROM line_items WHERE entity = ? AND metric = ? AND period = ?"
    params: list = [entity, metric, period]
    if statement:
        query += " AND statement = ?"
        params.append(statement)
    if consolidated is not None:
        query += " AND consolidated = ?"
        params.append(int(consolidated))
    return conn.execute(query, params).fetchall()


def list_periods(conn: sqlite3.Connection, entity: str) -> list[str]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT DISTINCT period FROM line_items WHERE entity = ? ORDER BY period",
        (entity,),
    ).fetchall()
    return [r["period"] for r in rows]


def list_metrics(conn: sqlite3.Connection, entity: str, statement: Optional[str] = None) -> list[str]:
    conn.row_factory = sqlite3.Row
    query = "SELECT DISTINCT metric FROM line_items WHERE entity = ?"
    params: list = [entity]
    if statement:
        query += " AND statement = ?"
        params.append(statement)
    rows = conn.execute(query, params).fetchall()
    return [r["metric"] for r in rows]
