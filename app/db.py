"""SQLite 持久层：审计记录与序列号预留的建表、写入与查询。"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from .suggest import suggest_next_serial

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone TEXT NOT NULL,
    created_at TEXT NOT NULL,
    serials TEXT NOT NULL,
    items TEXT NOT NULL,
    summary TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS serial_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone TEXT NOT NULL,
    request_id TEXT NOT NULL,
    current_serial INTEGER NOT NULL,
    serial INTEGER NOT NULL,
    date TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 幂等键与已分配 serial 的唯一约束（并发兜底；正常路径由 BEGIN IMMEDIATE 串行化保证）
CREATE UNIQUE INDEX IF NOT EXISTS uq_serial_reservations_zone_request
    ON serial_reservations (zone, request_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_serial_reservations_zone_serial
    ON serial_reservations (zone, serial);
"""


class ReservationConflict(Exception):
    """current 与服务端已预留的最新值不一致（对应 HTTP 409）。"""

    def __init__(self, latest_serial: int) -> None:
        super().__init__(f"current 与服务端已预留的最新值 {latest_serial} 不一致")
        self.latest_serial = latest_serial


def default_db_path() -> str:
    return os.environ.get("DNS_AUDIT_DB", "audits.db")


def _connect(db_path: str, *, autocommit: bool = False) -> sqlite3.Connection:
    parent = Path(db_path).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        db_path,
        # 写锁繁忙时等待（配合 BEGIN IMMEDIATE 串行化并发写入）
        timeout=10,
        isolation_level=None if autocommit else "",
    )
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


def reserve_serial(
    db_path: str,
    zone: str,
    request_id: str,
    current: int,
    today: date,
) -> tuple[dict, bool]:
    """在单个 SQLite 事务中为区域原子分配并持久化下一 serial。

    返回 (record, replayed)：
    - 同一 (zone, request_id) 已存在：返回首次记录且 replayed=True，不再推进；
    - 服务端已预留的最新值与 current 不一致：抛 ReservationConflict；
    - 当日编号耗尽 / 半区间歧义：抛 suggest.SuggestionError（API 层转为 409）。

    通过 BEGIN IMMEDIATE 将并发写事务串行化，并以 (zone, request_id)、
    (zone, serial) 唯一约束兜底，确保并发请求不会分配到重复 serial。
    """
    conn = _connect(db_path, autocommit=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM serial_reservations"
                " WHERE zone = ? AND request_id = ?",
                (zone, request_id),
            ).fetchone()
            if row is not None:
                conn.execute("COMMIT")
                return _reservation_row_to_dict(row), True

            latest = conn.execute(
                "SELECT serial FROM serial_reservations"
                " WHERE zone = ? ORDER BY id DESC LIMIT 1",
                (zone,),
            ).fetchone()
            if latest is not None and latest["serial"] != current:
                raise ReservationConflict(latest["serial"])

            # 首次预留以客户端声明的 current 为基准，其后以服务端最新预留值为准
            basis = latest["serial"] if latest is not None else current
            serial = suggest_next_serial(basis, today)

            created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            date_str = today.strftime("%Y%m%d")
            cursor = conn.execute(
                "INSERT INTO serial_reservations"
                " (zone, request_id, current_serial, serial, date, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (zone, request_id, current, serial, date_str, created_at),
            )
            conn.execute("COMMIT")
            return (
                {
                    "id": cursor.lastrowid,
                    "zone": zone,
                    "request_id": request_id,
                    "current": current,
                    "serial": serial,
                    "date": date_str,
                    "created_at": created_at,
                },
                False,
            )
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def list_serial_reservations(
    db_path: str, zone: str, limit: int = 50, offset: int = 0
) -> list[dict]:
    """按区域查询预留记录，按预留先后（id）升序。"""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM serial_reservations WHERE zone = ?"
            " ORDER BY id ASC LIMIT ? OFFSET ?",
            (zone, limit, offset),
        ).fetchall()
    return [_reservation_row_to_dict(row) for row in rows]


def _reservation_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "zone": row["zone"],
        "request_id": row["request_id"],
        "current": row["current_serial"],
        "serial": row["serial"],
        "date": row["date"],
        "created_at": row["created_at"],
    }
