"""API tests via FastAPI TestClient. All storage / settings / cache redirected to tmp dirs."""
import io
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

import conftest

if conftest.MAIN_IMPORT_ERROR is not None:  # pragma: no cover
    pytest.skip(f"backend.main failed to import: {conftest.MAIN_IMPORT_ERROR!r}", allow_module_level=True)

import backend.main as m  # noqa: E402
from backend.llm.cache import TranslationCache  # noqa: E402
from backend.llm.client import LLMConfig  # noqa: E402

REAL_KEY = "sk-secret-ABCD1234"
GEM_KEY = "AIzaGeminiKeyWXYZ"


@pytest.fixture
def env(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    for sub in ("downloads", "outputs", "fonts"):
        (storage / sub).mkdir(parents=True)
    monkeypatch.setattr(m, "STORAGE_DIR", storage)
    monkeypatch.setattr(m, "DOWNLOADS_DIR", storage / "downloads")
    monkeypatch.setattr(m, "OUTPUTS_DIR", storage / "outputs")
    monkeypatch.setattr(m, "FONTS_DIR", storage / "fonts")
    monkeypatch.setattr(m, "SETTINGS_FILE", storage / "settings.json")
    c = TranslationCache(str(storage / "cache.db"))
    monkeypatch.setattr(m, "default_cache", c)
    monkeypatch.setattr(
        m, "GLOBAL_SETTINGS",
        LLMConfig(provider="openai", api_keys=[REAL_KEY, "sk-other-WXYZ9999"], gemini_api_keys=[GEM_KEY]),
    )
    # Job persistence (backend/jobs_store.py) must also go to tmp, never the real jobs.db.
    from backend import jobs_store

    jobs_store.configure(str(storage / "jobs.db"))
    for name in ("_JOB_LAST_SAVE", "_JOB_LAST_STATUS"):
        if hasattr(m, name):
            getattr(m, name).clear()
    # Never launch the real pipeline (it would hit network / LLM).
    async def fake_pipeline(*a, **kw):
        return None

    monkeypatch.setattr(m, "_execute_translation_pipeline", fake_pipeline)
    for d in (m.JOBS, m.JOB_CANCEL_EVENTS, m.JOB_GLOSSARY_EVENTS, m.JOB_TASKS):
        monkeypatch.setattr(m, "JOBS", {}) if d is m.JOBS else d.clear()
    yield storage
    c.close()
    jobs_store.configure(str(tmp_path / "jobs_after.db"))  # keep pointing at tmp after the test


@pytest.fixture
def client(env):
    with TestClient(m.app, base_url="http://localhost:8088") as c:
        yield c


def _job(status="translating", **extra):
    j = {"id": "job1", "status": status, "created_at": time.time(), "agent_logs": []}
    j.update(extra)
    m.JOBS["job1"] = j
    return j


# ------------------------------------------------------------- basics
def test_health_uses_tmp_cache(client, env):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["cache_stats"]["db_path"].startswith(str(env))


# ----------------------------------------------------------- settings
def test_get_settings_masks_keys(client):
    s = client.get("/api/settings").json()["settings"]
    assert s["api_keys"] == ["••••1234", "••••9999"]
    assert s["gemini_api_keys"] == ["••••WXYZ"]
    assert REAL_KEY not in json.dumps(s) and GEM_KEY not in json.dumps(s)


def test_save_settings_unmasks_and_persists_real_keys(client, env):
    payload = {"api_keys": ["••••1234", "sk-brand-new-0000"], "concurrency": 5, "gemini_api_keys": ["••••WXYZ"]}
    r = client.post("/api/settings", json=payload)
    assert r.status_code == 200
    assert r.json()["settings"]["api_keys"] == ["••••1234", "••••0000"]
    saved = json.loads((env / "settings.json").read_text(encoding="utf-8"))
    assert saved["api_keys"] == [REAL_KEY, "sk-brand-new-0000"]
    assert saved["gemini_api_keys"] == [GEM_KEY]
    assert saved["concurrency"] == 5
    assert m.GLOBAL_SETTINGS.api_keys[0] == REAL_KEY


def test_save_settings_unknown_mask_dropped_and_no_masks_stored(client, env):
    client.post("/api/settings", json={"api_keys": ["••••nope"]})
    saved = json.loads((env / "settings.json").read_text(encoding="utf-8"))
    assert saved["api_keys"] == []  # stale mask never persisted as a key


def test_save_settings_gtx_alias_and_string_keys(client):
    r = client.post("/api/settings", json={"provider": "gtx"}).json()["settings"]
    assert r["provider"] == "google_free" and r["ui_provider"] == "gtx"
    r = client.post("/api/settings", json={"provider": "openai", "api_keys": "k-aaaa1111, k-bbbb2222"}).json()["settings"]
    assert r["api_keys"] == ["••••1111", "••••2222"]


def test_settings_invalid_value_rejected(env):
    with TestClient(m.app, base_url="http://localhost:8088", raise_server_exceptions=False) as c:
        r = c.post("/api/settings", json={"concurrency": 9999})
    assert 400 <= r.status_code < 500


def test_mask_helpers():
    assert m._mask_key("abcdefgh") == "••••efgh"
    assert m._mask_key("") == ""
    assert m._unmask_keys("••••1234, new", [REAL_KEY]) == [REAL_KEY, "new"]


# ------------------------------------------------- origin / host guard
def test_evil_origin_blocked_on_post(client):
    r = client.post("/api/cache/clear", headers={"Origin": "http://evil.com"})
    assert r.status_code == 403


def test_evil_origin_blocked_on_settings_get(client):
    r = client.get("/api/settings", headers={"Origin": "https://evil.com"})
    assert r.status_code == 403


def test_evil_origin_lookalike_blocked(client):
    for o in ("http://localhost.evil.com", "http://127.0.0.1.evil.com:8088", "null"):
        assert client.post("/api/cache/clear", headers={"Origin": o}).status_code == 403


def test_local_origin_allowed(client):
    for o in ("http://localhost:8088", "http://127.0.0.1:8088", "http://[::1]:8088"):
        assert client.post("/api/cache/clear", headers={"Origin": o}).status_code == 200


def test_no_origin_allowed(client):
    assert client.post("/api/cache/clear").status_code == 200


def test_evil_host_blocked(client):
    assert client.get("/api/health", headers={"Host": "evil.com"}).status_code == 403
    assert client.get("/api/health", headers={"Host": "evil.com:8088"}).status_code == 403


def test_evil_host_allowed_when_remote_enabled(client, monkeypatch):
    monkeypatch.setenv("BTH_ALLOW_REMOTE", "1")
    assert client.get("/api/health", headers={"Host": "myserver.lan"}).status_code == 200


def test_evil_origin_does_not_modify_settings(client, env):
    client.post("/api/settings", json={"concurrency": 3}, headers={"Origin": "http://evil.com"})
    assert m.GLOBAL_SETTINGS.concurrency == 12
    assert not (env / "settings.json").exists()


# --------------------------------------------------------- job guards
def test_get_job_404_and_list(client):
    assert client.get("/api/jobs/nope").status_code == 404
    _job()
    assert client.get("/api/jobs/job1").json()["job"]["id"] == "job1"
    assert len(client.get("/api/jobs").json()["jobs"]) == 1


def test_cancel_unknown_404(client):
    assert client.post("/api/jobs/nope/cancel").status_code == 404


def test_cancel_running_job(client):
    import asyncio

    ev = asyncio.Event()
    m.JOB_CANCEL_EVENTS["job1"] = ev
    _job("translating")
    j = client.post("/api/jobs/job1/cancel").json()["job"]
    assert j["status"] == "cancelled" and j["finished_at"]
    assert ev.is_set()


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_cancel_finished_job_is_noop(client, status):
    _job(status)
    assert client.post("/api/jobs/job1/cancel").json()["job"]["status"] == status


def test_repackage_guards(client, env):
    assert client.post("/api/jobs/nope/repackage").status_code == 404
    _job("translating")
    assert client.post("/api/jobs/job1/repackage").status_code == 409
    _job("completed", input_file_path=None)
    assert client.post("/api/jobs/job1/repackage").status_code == 400
    _job("completed", input_file_path=str(env / "downloads" / "missing.epub"))
    assert client.post("/api/jobs/job1/repackage").status_code == 400


def test_repackage_ok_resets_state(client, env):
    f = env / "downloads" / "b.txt"
    f.write_text("hello world", encoding="utf-8")
    _job("completed", input_file_path=str(f), glossary=[{"src": "a", "dst": "b"}])
    r = client.post("/api/jobs/job1/repackage")
    assert r.status_code == 200 and r.json()["job"]["status"] == "packaging"


def test_glossary_update_cleans_entries(client):
    _job()
    r = client.put("/api/jobs/job1/glossary", json={"glossary": [{"src": " A ", "dst": "b"}, {"src": "", "dst": "x"}]})
    assert r.json()["glossary"] == [{"src": "A", "dst": "b", "note": ""}]
    assert client.put("/api/jobs/zzz/glossary", json={"glossary": []}).status_code == 404


def test_download_is_confined_to_outputs(client, env):
    (env / "outputs" / "ok.txt").write_text("x", encoding="utf-8")
    (env / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    assert client.get("/api/download/ok.txt").status_code == 200
    assert client.get("/api/download/missing.txt").status_code == 404
    r = client.get("/api/download/..%2Fsecret.txt")
    assert r.status_code == 404 and b"TOP SECRET" not in r.content
    r = client.get("/api/download/..%5Csecret.txt")
    assert b"TOP SECRET" not in r.content


# -------------------------------------------------------------- upload
def _upload(client, name, data=b"Hello world paragraph.", config=None):
    files = {"file": (name, io.BytesIO(data), "application/octet-stream")}
    form = {"config": json.dumps(config)} if config else {}
    return client.post("/api/jobs/upload", files=files, data=form)


def test_upload_rejects_bad_extension(client, env):
    for name in ("evil.exe", "a.html", "noext", "x.epub.exe"):
        r = _upload(client, name)
        assert r.status_code == 400, name
    assert list((env / "downloads").iterdir()) == []


@pytest.mark.parametrize("name", ["b.txt", "b.md", "b.EPUB", "b.docx", "b.pdf"])
def test_upload_accepts_supported(client, env, name):
    r = _upload(client, name)
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    assert jid in m.JOBS
    assert len(list((env / "downloads").iterdir())) == 1


def test_upload_filename_sanitized(client, env):
    r = _upload(client, "../../evil name?.txt")
    assert r.status_code == 200
    saved = list((env / "downloads").iterdir())
    assert len(saved) == 1 and saved[0].parent == env / "downloads"
    assert ".." not in saved[0].name and "?" not in saved[0].name


def test_upload_too_large_rejected_and_cleaned(client, env, monkeypatch):
    monkeypatch.setattr(m, "MAX_UPLOAD_BYTES", 10)
    r = _upload(client, "big.txt", data=b"x" * 50)
    assert r.status_code == 413
    assert list((env / "downloads").iterdir()) == []


def test_upload_config_override_applied(client):
    r = _upload(client, "b.txt", config={"concurrency": 3})
    assert r.status_code == 200


def test_upload_missing_file_422(client):
    assert client.post("/api/jobs/upload").status_code == 422


# ------------------------------------------------------ job creation
def test_auto_job_requires_query(client):
    assert client.post("/api/jobs/auto", json={"query": "   "}).status_code == 400
    r = client.post("/api/jobs/auto", json={"query": "Dune"})
    assert r.status_code == 200 and r.json()["job"]["status"] in ("queued", "searching")


def test_from_search_invalid_book_400(client):
    assert client.post("/api/jobs/from-search", json={"book": {"bogus": 1}}).status_code == 400


def test_job_config_does_not_leak_keys_in_response(client):
    r = client.post("/api/jobs/auto", json={"query": "Dune"})
    assert REAL_KEY not in r.text and GEM_KEY not in r.text
    assert REAL_KEY not in client.get("/api/jobs").text


# ---------------------------------------------------------------- cache
def test_cache_clear_endpoint_uses_tmp_cache(client):
    m.default_cache.set("a", "b", "vi", "p", "m")
    r = client.post("/api/cache/clear").json()
    assert r["removed_entries"] == 1
    assert client.get("/api/cache/stats").json()["cache_stats"]["total_entries"] == 0


def test_merge_job_config_overrides(env):
    cfg = m._merge_job_config({"target_lang_code": "EN", "bilingual_mode": "bilingual", "api_keys": ["••••1234"]})
    assert cfg.target_lang == "English" and cfg.bilingual is True
    assert cfg.api_keys == [REAL_KEY]
    assert m._merge_job_config(None).api_keys[0] == REAL_KEY


# ------------------------------------------------- job list / persistence
def test_jobs_list_is_slim_except_active(client):
    _job(previews=[{"src": "a", "dst": "b"}], glossary=[{"src": "x", "dst": "y"}], brief="B", title="T")
    slim = client.get("/api/jobs").json()["jobs"][0]
    assert slim["title"] == "T" and "previews" not in slim and "glossary" not in slim and "brief" not in slim
    full = client.get("/api/jobs", params={"active": "job1"}).json()["jobs"][0]
    assert full["previews"] and full["glossary"] and full["brief"] == "B"


def test_running_jobs_become_interrupted_on_restore(env):
    from backend import jobs_store

    jobs_store.save_job({"id": "run1", "status": "translating", "created_at": 1.0})
    jobs_store.save_job({"id": "done1", "status": "completed", "created_at": 2.0})
    m.JOBS.clear()
    m._restore_jobs_from_store()
    assert m.JOBS["run1"]["status"] == "interrupted"
    assert m.JOBS["done1"]["status"] == "completed"


# ------------------------------------------------- deployment: auth + allowed hosts
def _basic(user, pw):
    import base64

    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_basic_auth_required_when_password_set(env, monkeypatch):
    monkeypatch.setattr(m, "AUTH_PASSWORD", "s3cret")
    monkeypatch.setattr(m, "AUTH_USER", "admin")
    with TestClient(m.app, base_url="http://localhost:8088") as c:
        assert c.get("/api/health").status_code == 401
        assert c.get("/api/health", headers=_basic("admin", "wrong")).status_code == 401
        assert c.get("/api/health", headers=_basic("admin", "s3cret")).status_code == 200
        assert c.get("/").status_code == 401  # cả giao diện cũng bị khóa
        assert c.get("/api/ping").status_code == 200  # healthcheck không cần đăng nhập


def test_allowed_hosts_domain_and_same_origin(env, monkeypatch):
    monkeypatch.setattr(m, "ALLOWED_HOSTS", {"books.example.com"})
    ok = TestClient(m.app, base_url="https://books.example.com")
    assert ok.get("/api/ping").status_code == 200
    assert ok.post("/api/cache/clear", headers={"Origin": "https://books.example.com"}).status_code == 200
    assert ok.post("/api/cache/clear", headers={"Origin": "https://evil.com"}).status_code == 403
    assert TestClient(m.app, base_url="https://other.example.com").get("/api/ping").status_code == 403


def test_public_deploy_without_password_refuses_to_start(tmp_path):
    import subprocess
    import sys

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_vars = {**os.environ, "BTH_DATA_DIR": str(tmp_path), "BTH_ALLOWED_HOSTS": "books.example.com"}
    env_vars.pop("BTH_AUTH_PASSWORD", None)
    bad = subprocess.run(
        [sys.executable, "-c", "import backend.main"], cwd=root, env=env_vars, capture_output=True, text=True
    )
    assert bad.returncode != 0 and "BTH_AUTH_PASSWORD" in bad.stderr
    env_vars["BTH_AUTH_PASSWORD"] = "pw"
    good = subprocess.run(
        [sys.executable, "-c", "import backend.main"], cwd=root, env=env_vars, capture_output=True, text=True
    )
    assert good.returncode == 0, good.stderr[-400:]


# ------------------------------------------------- public + no login => credentials locked
def _public_no_auth(monkeypatch):
    monkeypatch.setattr(m, "ALLOWED_HOSTS", {"books.example.com"})
    monkeypatch.setattr(m, "AUTH_PASSWORD", "")


def test_public_without_login_app_is_open_but_settings_locked(env, monkeypatch):
    _public_no_auth(monkeypatch)
    c = TestClient(m.app, base_url="https://books.example.com")
    assert c.get("/").status_code == 200  # không cần đăng nhập
    r = c.post("/api/settings", json={"base_url": "https://evil.example/v1"})
    assert r.status_code == 403
    assert m.GLOBAL_SETTINGS.base_url != "https://evil.example/v1"


def test_public_without_login_job_override_cannot_redirect_key(env, monkeypatch):
    _public_no_auth(monkeypatch)
    cfg = m._merge_job_config(
        {"base_url": "https://evil.example/v1", "api_keys": ["attacker"], "gemini_base_url": "https://evil.example", "model": "m-x"}
    )
    assert cfg.base_url == m.GLOBAL_SETTINGS.base_url
    assert cfg.api_keys == m.GLOBAL_SETTINGS.api_keys
    assert cfg.gemini_base_url == m.GLOBAL_SETTINGS.gemini_base_url
    assert cfg.model == "m-x"  # trường không nhạy cảm vẫn đổi được


def test_local_mode_settings_not_locked(env):
    with TestClient(m.app, base_url="http://localhost:8088") as c:
        assert c.post("/api/settings", json={"concurrency": 5}).status_code == 200
