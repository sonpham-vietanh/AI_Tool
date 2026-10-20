"""Shared fixtures. Fully offline: no network, no real LLM, no real storage/settings."""
from __future__ import annotations

import os
import pathlib
import sqlite3
import sys
import tempfile
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Never let a developer's real key leak into tests.
os.environ.pop("BTH_API_KEY", None)
os.environ.pop("BTH_BASE_URL", None)
os.environ.pop("BTH_ALLOW_REMOTE", None)

# ---------------------------------------------------------------------------
# Import the backend WITHOUT touching real storage:
#  * backend.llm.cache creates `default_cache` (sqlite) at import -> redirect to a temp file
#  * backend.main reads storage/settings.json at import -> pretend it is unreadable
# ---------------------------------------------------------------------------
MAIN_IMPORT_ERROR = None
_IMPORT_TMP = tempfile.mkdtemp(prefix="bth_import_")
_real_connect = sqlite3.connect
_real_read_text = pathlib.Path.read_text


def _safe_connect(database, *a, **kw):
    if isinstance(database, (str, os.PathLike)) and str(database).endswith(("translation_cache.db", "jobs.db")):
        database = os.path.join(_IMPORT_TMP, os.path.basename(str(database)))
    return _real_connect(database, *a, **kw)


def _safe_read_text(self, *a, **kw):
    if self.name == "settings.json":
        raise OSError("blocked by tests")
    return _real_read_text(self, *a, **kw)


sqlite3.connect = _safe_connect
pathlib.Path.read_text = _safe_read_text
try:
    import backend.llm  # noqa: F401

    try:
        import backend.search  # noqa: F401
        import backend.main  # noqa: F401
    except Exception as _exc:  # another dev's in-flight change may break import; surface per-test
        MAIN_IMPORT_ERROR = _exc
finally:
    sqlite3.connect = _real_connect
    pathlib.Path.read_text = _real_read_text

from backend.llm.cache import TranslationCache  # noqa: E402
from backend.llm.client import LLMConfig  # noqa: E402
from helpers import FakeLLMClient  # noqa: E402


@pytest.fixture
def cache(tmp_path) -> TranslationCache:
    c = TranslationCache(str(tmp_path / "cache.db"))
    yield c
    c.close()


@pytest.fixture
def make_client(cache):
    def _make(handler=None, **cfg):
        cfg.setdefault("provider", "openai")
        cfg.setdefault("api_keys", ["sk-test-0000"])
        cfg.setdefault("model", "test-model")
        cfg.setdefault("fallback_to_google_free", False)
        return FakeLLMClient(config=LLMConfig(**cfg), cache=cache, handler=handler)

    return _make
