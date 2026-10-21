import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Optional


class TranslationCache:
    """
    Thread-safe SQLite chunk cache for storing and retrieving translated segments.
    Saves API tokens and significantly accelerates re-translations or repeated phrases.
    """

    def __init__(self, db_path: Optional[str] = None):
        if not db_path:
            base_dir = Path(__file__).resolve().parent.parent
            storage_dir = Path(os.environ.get("BTH_DATA_DIR") or (base_dir / "storage"))
            storage_dir.mkdir(parents=True, exist_ok=True)
            db_path = str(storage_dir / "translation_cache.db")
        else:
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS cache (
                    hash_key TEXT PRIMARY KEY,
                    src_text TEXT,
                    dst_text TEXT,
                    provider TEXT,
                    model TEXT,
                    target_lang TEXT,
                    created_at REAL
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_cache_provider_lang ON cache(provider, target_lang);"
            )
            self._conn.commit()

    @staticmethod
    def make_hash(
        src_text: str, target_lang: str, provider: str, model: str
    ) -> str:
        normalized_src = src_text.strip()
        raw = f"{provider.strip().lower()}|{model.strip().lower()}|{target_lang.strip().lower()}|{normalized_src}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(
        self,
        src_text: str,
        target_lang: str,
        provider: str = "any",
        model: str = "any",
    ) -> Optional[str]:
        if not src_text or not src_text.strip():
            return src_text

        hash_key = self.make_hash(src_text, target_lang, provider, model)
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT dst_text FROM cache WHERE hash_key = ?", (hash_key,)
            )
            row = cursor.fetchone()
            if row:
                return row[0]

            # Chỉ cho phép dùng lại bản sửa tay của người dùng (không dùng bản dịch của model/provider khác)
            override_key = self.make_hash(src_text, target_lang, "manual_override", "editor_override")
            cursor.execute("SELECT dst_text FROM cache WHERE hash_key = ?", (override_key,))
            row_override = cursor.fetchone()
            return row_override[0] if row_override else None

    def set(
        self,
        src_text: str,
        dst_text: str,
        target_lang: str,
        provider: str = "google_free",
        model: str = "default",
    ) -> None:
        if not src_text or not src_text.strip() or not dst_text or not dst_text.strip():
            return

        hash_key = self.make_hash(src_text, target_lang, provider, model)
        now = time.time()
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO cache
                (hash_key, src_text, dst_text, provider, model, target_lang, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    hash_key,
                    src_text.strip(),
                    dst_text.strip(),
                    provider.strip(),
                    model.strip(),
                    target_lang.strip(),
                    now,
                ),
            )
            self._conn.commit()

    def set_many(
        self,
        items: list[tuple[str, str]],
        target_lang: str,
        provider: str = "google_free",
        model: str = "default",
    ) -> None:
        if not items:
            return
        now = time.time()
        rows = []
        for src_text, dst_text in items:
            if not src_text or not src_text.strip() or not dst_text or not dst_text.strip():
                continue
            h_key = self.make_hash(src_text, target_lang, provider, model)
            rows.append(
                (
                    h_key,
                    src_text.strip(),
                    dst_text.strip(),
                    provider.strip(),
                    model.strip(),
                    target_lang.strip(),
                    now,
                )
            )
        if not rows:
            return
        with self._lock:
            cursor = self._conn.cursor()
            cursor.executemany(
                """
                INSERT OR REPLACE INTO cache
                (hash_key, src_text, dst_text, provider, model, target_lang, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self._conn.commit()

    def clear(self) -> int:
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM cache")
            count = cursor.fetchone()[0]
            cursor.execute("DELETE FROM cache")
            self._conn.commit()
            return count

    def get_stats(self) -> Dict[str, object]:
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM cache")
            total_entries = cursor.fetchone()[0] or 0

            cursor.execute(
                "SELECT provider, COUNT(*) FROM cache GROUP BY provider"
            )
            by_provider = {row[0]: row[1] for row in cursor.fetchall()}

            cursor.execute(
                "SELECT SUM(LENGTH(src_text)), SUM(LENGTH(dst_text)) FROM cache"
            )
            lengths = cursor.fetchone()
            src_chars = lengths[0] or 0
            dst_chars = lengths[1] or 0

            db_size_bytes = (
                os.path.getsize(self.db_path)
                if os.path.exists(self.db_path)
                else 0
            )

            return {
                "total_entries": total_entries,
                "by_provider": by_provider,
                "total_source_chars": src_chars,
                "total_translated_chars": dst_chars,
                "db_size_kb": round(db_size_bytes / 1024, 2),
                "db_path": self.db_path,
            }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# Singleton instance for easy reuse across modules
default_cache = TranslationCache()
