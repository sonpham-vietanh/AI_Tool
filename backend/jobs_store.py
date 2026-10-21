"""Thread-safe SQLite persistence for translation jobs (survives server restarts)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_BASE = Path(__file__).resolve().parent
DEFAULT_DB_PATH = str(Path(os.environ.get("BTH_DATA_DIR") or (_BASE / "storage")) / "jobs.db")

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_conn_path: Optional[str] = None


def _get_conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    global _conn, _conn_path
    path = db_path or _conn_path or DEFAULT_DB_PATH
    if _conn is None or path != _conn_path:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn_path = path
        cur = _conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.execute(
            "CREATE TABLE IF NOT EXISTS jobs ("
            "id TEXT PRIMARY KEY, data TEXT NOT NULL, created_at REAL, updated_at REAL)"
        )
        _conn.commit()
    return _conn


def configure(db_path: Optional[str]) -> None:
    """Point the store at another DB file (used by tests)."""
    with _lock:
        _get_conn(db_path or DEFAULT_DB_PATH)


def save_job(job: Dict[str, Any]) -> None:
    job_id = job.get("id")
    if not job_id:
        return
    data = json.dumps(job, ensure_ascii=False, default=str)
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO jobs (id, data, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (job_id, data, float(job.get("created_at") or time.time()), time.time()),
        )
        conn.commit()


def load_all_jobs() -> List[Dict[str, Any]]:
    with _lock:
        rows = _get_conn().execute(
            "SELECT data FROM jobs ORDER BY created_at DESC"
        ).fetchall()
    out: List[Dict[str, Any]] = []
    for (raw,) in rows:
        try:
            item = json.loads(raw)
            if isinstance(item, dict) and item.get("id"):
                out.append(item)
        except Exception:
            continue
    return out


def delete_job(job_id: str) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        conn.commit()
