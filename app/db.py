"""SQLite 持久层：审计记录的建表、写入与查询。"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone TEXT NOT NULL,
    created_at TEXT NOT NULL,
    serials TEXT NOT NULL,
    items TEXT NOT NULL,
    summary TEXT NOT NULL
);
"""


def default_db_path() -> str:
    return os.environ.get("DNS_AUDIT_DB", "audits.db")


def _connect(db_path: str) -> sqlite3.Connection:
    parent = Path(db_path).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path: str) -> None:
    """启动时初始化数据库（建表，幂等）。"""
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)


def insert_audit(
    db_path: str,
    zone: str,
    serials: list[int],
    items: list[dict],
    summary: dict,
) -> dict:
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO audits (zone, created_at, serials, items, summary)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                zone,
                created_at,
                json.dumps(serials),
                json.dumps(items, ensure_ascii=False),
                json.dumps(summary, ensure_ascii=False),
            ),
        )
        audit_id = cursor.lastrowid
    return {
        "id": audit_id,
        "zone": zone,
        "created_at": created_at,
        "serials": serials,
        "items": items,
        "summary": summary,
    }


def get_audit(db_path: str, audit_id: int) -> dict | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM audits WHERE id = ?", (audit_id,)
        ).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def list_audits(db_path: str, limit: int = 50, offset: int = 0) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, zone, created_at, serials, summary FROM audits"
            " ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    result = []
    for row in rows:
        summary = json.loads(row["summary"])
        result.append(
            {
                "id": row["id"],
                "zone": row["zone"],
                "created_at": row["created_at"],
                "total": summary["total"],
                "risk_count": len(summary["risk_indexes"]),
            }
        )
    return result


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "zone": row["zone"],
        "created_at": row["created_at"],
        "serials": json.loads(row["serials"]),
        "items": json.loads(row["items"]),
        "summary": json.loads(row["summary"]),
    }
