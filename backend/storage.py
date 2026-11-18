import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_DB_LOCK = threading.Lock()
_DEFAULT_DB_PATH = Path.home() / ".video_indirici" / "state.db"


def _get_db_path() -> Path:
    env_path = os.environ.get("VIDEO_INDIRICI_DB", "").strip()
    path = Path(env_path) if env_path else _DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_get_db_path()), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db() -> None:
    with _DB_LOCK:
        with _connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS waf_bans (ip TEXT PRIMARY KEY, ban_until REAL NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS waf_events ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, ip TEXT, reason TEXT, path TEXT, created_at REAL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS download_jobs ("
                "job_id TEXT PRIMARY KEY, data_json TEXT NOT NULL, updated_at REAL NOT NULL)"
            )
            conn.commit()


init_db()


def save_ban(ip: str, ban_until: float) -> None:
    with _DB_LOCK:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO waf_bans (ip, ban_until) VALUES (?, ?)", (ip, ban_until)
            )
            conn.commit()


def get_ban_until(ip: str) -> float:
    now = time.time()
    with _DB_LOCK:
        with _connect() as conn:
            row = conn.execute("SELECT ban_until FROM waf_bans WHERE ip = ?", (ip,)).fetchone()
            if not row:
                return 0.0
            if row[0] <= now:
                conn.execute("DELETE FROM waf_bans WHERE ip = ?", (ip,))
                conn.commit()
                return 0.0
            return float(row[0])


def count_active_bans() -> int:
    now = time.time()
    with _DB_LOCK:
        with _connect() as conn:
            conn.execute("DELETE FROM waf_bans WHERE ban_until <= ?", (now,))
            row = conn.execute("SELECT COUNT(*) FROM waf_bans").fetchone()
            conn.commit()
            return int(row[0]) if row else 0


def record_waf_event(time_str: str, masked_ip: str, reason: str, path: str) -> None:
    now = time.time()
    with _DB_LOCK:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO waf_events (time, ip, reason, path, created_at) VALUES (?, ?, ?, ?, ?)",
                (time_str, masked_ip, reason, path[:80], now),
            )
            conn.execute(
                "DELETE FROM waf_events WHERE id NOT IN (SELECT id FROM waf_events ORDER BY id DESC LIMIT 200)"
            )
            conn.commit()


def get_waf_summary(limit: int = 15) -> Dict[str, Any]:
    with _DB_LOCK:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT time, ip, reason, path FROM waf_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            max_id_row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM waf_events").fetchone()
    events = [{"time": r[0], "ip": r[1], "reason": r[2], "path": r[3]} for r in reversed(rows)]
    return {
        "total": int(max_id_row[0]) if max_id_row else len(events),
        "events": events,
    }


def save_job(job_id: str, job_data: Dict[str, Any]) -> None:
    now = time.time()
    payload = json.dumps(job_data, ensure_ascii=False)
    with _DB_LOCK:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO download_jobs (job_id, data_json, updated_at) VALUES (?, ?, ?)",
                (job_id, payload, now),
            )
            conn.execute(
                "DELETE FROM download_jobs WHERE updated_at < ?", (now - 86400,)
            )
            conn.commit()


def load_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _DB_LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT data_json FROM download_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None
