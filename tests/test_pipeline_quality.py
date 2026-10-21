"""End-to-end tests of the REAL `_execute_translation_pipeline` with a fake LLM transport.

Covers the untranslated-segment quality accounting (retry / never cached / warnings), the
effective provider reporting, the /api/health `llm` block, job persistence and resume.
Fully offline: storage, settings, cache and jobs.db are all redirected to tmp.
"""
import io
import json
import re
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

import conftest
from helpers import FakeLLMClient

if conftest.MAIN_IMPORT_ERROR is not None:  # pragma: no cover
    pytest.skip(f"backend.main failed to import: {conftest.MAIN_IMPORT_ERROR!r}", allow_module_level=True)

import backend.main as m  # noqa: E402
from backend import jobs_store  # noqa: E402
from backend.llm.cache import TranslationCache  # noqa: E402
from backend.llm.client import LLMConfig  # noqa: E402

KEY = "sk-test-ABCD1234"
TERMINAL = ("completed", "failed", "cancelled", "interrupted")
N_PARAS = 20


def para(i: int) -> str:
    return f"Paragraph number {i} describes the quiet river and the old stone bridge near the town."


def vi_of(src: str) -> str:
    n = re.search(r"Paragraph number (\d+)", src)
    return f"Đoạn văn số {n.group(1) if n else '?'} miêu tả dòng sông yên tĩnh và cây cầu đá cổ gần thị trấn."


def make_handler(echo_indices=(), always=False, seen=None):
    """Fake LLM. Batches carry [[#i]] markers; retries are single un-marked calls.

    Paragraphs whose number is in `echo_indices` are echoed back in English the first time
    (or every time when `always`)."""
    seen = seen if seen is not None else set()

    def answer(block: str) -> str:
        mt = re.search(r"Paragraph number (\d+)", block)
        idx = int(mt.group(1)) if mt else -1
        if idx in echo_indices:
            if always or idx not in seen:
                seen.add(idx)
                return block.strip()
        return vi_of(block)

    def handler(system_prompt, user_prompt, model_override=None):
        parts = re.split(r"(\[\[#\d+\]\])", user_prompt)
        if len(parts) > 1:
            return "\n\n".join(f"{parts[i]}\n{answer(parts[i + 1])}" for i in range(1, len(parts), 2))
        return answer(user_prompt)

    return handler


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
    cache = TranslationCache(str(storage / "cache.db"))
    monkeypatch.setattr(m, "default_cache", cache)
    monkeypatch.setattr(m, "GLOBAL_SETTINGS", LLMConfig(provider="openai", api_keys=[KEY]))
    jobs_store.configure(str(storage / "jobs.db"))
    for name in ("_JOB_LAST_SAVE", "_JOB_LAST_STATUS"):
        if hasattr(m, name):
            getattr(m, name).clear()
    monkeypatch.setattr(m, "JOBS", {})
    for d in (m.JOB_CANCEL_EVENTS, m.JOB_GLOSSARY_EVENTS, m.JOB_TASKS):
        d.clear()

    instances = []

    def install(handler, google_transport=None):
        class _Client(FakeLLMClient):
            def __init__(self, config=None, cache=None, **kw):
                super().__init__(config=config, cache=cache, handler=handler, **kw)
                instances.append(self)

        if google_transport is not None:
            async def _gt(self, text, target_lang_code=None):
                return google_transport(text)

            _Client._translate_google_free_raw = _gt
        monkeypatch.setattr(m, "TranslationLLMClient", _Client)
        return instances

    class Env:
        pass

    e = Env()
    e.storage = storage
    e.cache = cache
    e.install = install
    e.instances = instances
    yield e
    cache.close()
    jobs_store.configure(str(tmp_path / "jobs_after.db"))


@pytest.fixture
def client(env):
    with TestClient(m.app, base_url="http://localhost:8088") as c:
        yield c


def txt_bytes() -> bytes:
    return "\n\n".join(para(i) for i in range(N_PARAS)).encode("utf-8")


def make_epub(n: int = 6) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        z.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">x1</dc:identifier>'
            "<dc:title>Tiny</dc:title><dc:language>en</dc:language></metadata>"
            '<manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/></manifest>'
            '<spine><itemref idref="c1"/></spine></package>',
        )
        z.writestr(
            "OEBPS/nav.xhtml",
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
            '<head><title>Nav</title></head><body><nav epub:type="toc"><ol><li><a href="c1.xhtml">One</a></li></ol></nav></body></html>',
        )
        body = "".join(f"<p>{para(i)}</p>" for i in range(n))
        z.writestr(
            "OEBPS/c1.xhtml",
            '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
            f"<head><title>One</title></head><body>{body}</body></html>",
        )
    return buf.getvalue()


def start_job(client, data: bytes, name="book.txt", cfg=None):
    cfg = {"auto_glossary": False, **(cfg or {})}
    r = client.post(
        "/api/jobs/upload",
        files={"file": (name, data, "application/octet-stream")},
        data={"config": json.dumps(cfg)},
    )
    assert r.status_code == 200, r.text
    return r.json()["job_id"]


def wait_job(client, job_id, timeout=10.0):
    end = time.time() + timeout
    job = None
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()["job"]
        if job["status"] in TERMINAL:
            return job
        time.sleep(0.05)
    pytest.fail(f"job did not finish in {timeout}s, last status={job and job['status']}")


def read_output(env, job):
    fn = job["output_files"]["primary"]
    return (env.storage / "outputs" / fn).read_text(encoding="utf-8")


# ----------------------------------------------------------------- (a) healthy
def test_healthy_run_has_clean_quality(env, client):
    env.install(make_handler())
    job = wait_job(client, start_job(client, txt_bytes()))
    assert job["status"] == "completed", job.get("error")
    assert job["effective_provider"] == "openai"
    q = job["quality"]
    assert q["total"] == N_PARAS
    assert q["still_untranslated"] == 0 and q["untranslated_found"] == 0
    assert q["percent_untranslated"] == 0
    assert job["warnings"] == []
    assert not job["message"].startswith("⚠")
    out = read_output(env, job)
    assert all(f"Đoạn văn số {i} " in out for i in range(N_PARAS))


def test_healthy_epub_run(env, client):
    env.install(make_handler())
    job = wait_job(client, start_job(client, make_epub(6), name="tiny.epub"))
    assert job["status"] == "completed", job.get("error")
    assert job["quality"]["still_untranslated"] == 0
    assert job["warnings"] == []
    out = env.storage / "outputs" / job["output_files"]["primary"]
    with zipfile.ZipFile(out) as z:
        text = "".join(z.read(n).decode("utf-8", "ignore") for n in z.namelist() if n.endswith("c1.xhtml"))
    assert "Đoạn văn số 0 " in text and "Đoạn văn số 5 " in text


# ------------------------------------------------- (b) echo first try, ok on retry
def test_echo_then_fixed_on_retry(env, client):
    # 4/20 paragraphs echoed on the first attempt. (The retry budget is max(4, 25% of segments seen),
    # so a 40% echo rate would legitimately leave some unfixed; 20% stays inside the budget.)
    echo = {0, 5, 10, 15}
    env.install(make_handler(echo_indices=echo))
    job = wait_job(client, start_job(client, txt_bytes()))
    assert job["status"] == "completed", job.get("error")
    q = job["quality"]
    assert q["untranslated_found"] == len(echo)
    assert q["retried_fixed"] == len(echo) > 0
    assert q["still_untranslated"] == 0
    assert job["warnings"] == []
    out = read_output(env, job)
    assert all(f"Đoạn văn số {i} " in out for i in range(N_PARAS))
    assert "Paragraph number" not in out
    # the retried translations were cached properly
    for i in echo:
        cached = env.cache.get(para(i), job["target_lang"], job["effective_provider"], job["effective_model"])
        assert cached == vi_of(para(i))


# ------------------------------------------ (c) always echoes -> warning, not cached
def test_always_echo_warns_and_is_not_cached(env, client):
    echo = {1, 6, 11, 16}  # 20% >= 15%
    env.install(make_handler(echo_indices=echo, always=True))
    job = wait_job(client, start_job(client, txt_bytes()))
    assert job["status"] == "completed", job.get("error")
    q = job["quality"]
    assert q["still_untranslated"] == len(echo)
    assert q["percent_untranslated"] >= 15
    assert job["message"].startswith("⚠")
    assert any(f"{q['still_untranslated']}/{q['total']}" in w for w in job["warnings"])
    for i in range(N_PARAS):
        cached = env.cache.get(para(i), job["target_lang"], job["effective_provider"], job["effective_model"])
        if i in echo:
            assert cached is None or cached.strip() != para(i).strip(), f"untranslated text cached for {i}"
        else:
            assert cached == vi_of(para(i))


def test_low_percentage_untranslated_keeps_plain_message(env, client):
    # 1/20 = 5% < 15%: warning listed, but the final message is not prefixed.
    env.install(make_handler(echo_indices={3}, always=True))
    job = wait_job(client, start_job(client, txt_bytes()))
    assert job["status"] == "completed"
    assert job["quality"]["still_untranslated"] == 1
    assert job["quality"]["percent_untranslated"] < 15
    assert any(w.startswith("⚠") for w in job["warnings"])
    assert not job["message"].startswith("⚠")


# --------------------------------------------------------------- (d) google_free
def test_explicit_google_free_warns(env, client):
    env.install(make_handler(), google_transport=lambda text: make_handler()("", text))
    job = wait_job(client, start_job(client, txt_bytes(), cfg={"provider": "google_free"}))
    assert job["status"] == "completed", job.get("error")
    assert job["effective_provider"] == "google_free"
    assert any("Google Translate" in w for w in job["warnings"])


def test_upload_without_key_is_rejected_but_google_free_allowed(env, client, monkeypatch):
    monkeypatch.setattr(m, "GLOBAL_SETTINGS", LLMConfig(provider="openai", api_keys=[]))
    env.install(make_handler(), google_transport=lambda text: make_handler()("", text))
    r = client.post(
        "/api/jobs/upload",
        files={"file": ("book.txt", txt_bytes(), "text/plain")},
        data={"config": json.dumps({"auto_glossary": False})},
    )
    assert r.status_code == 400 and "API key" in r.json()["detail"]
    ok = client.post(
        "/api/jobs/upload",
        files={"file": ("book.txt", txt_bytes(), "text/plain")},
        data={"config": json.dumps({"auto_glossary": False, "provider": "google_free"})},
    )
    assert ok.status_code == 200
    wait_job(client, ok.json()["job_id"])


# ------------------------------------------------------------------ (e) health
def test_health_llm_ready_flag(env, client, monkeypatch):
    monkeypatch.setattr(m, "GLOBAL_SETTINGS", LLMConfig(provider="openai", api_keys=[]))
    llm = client.get("/api/health").json()["llm"]
    assert llm["ready"] is False
    assert llm["configured_provider"] == "openai"
    assert llm["provider"] == "google_free"
    assert llm["message"]

    monkeypatch.setattr(m, "GLOBAL_SETTINGS", LLMConfig(provider="openai", api_keys=[KEY]))
    llm = client.get("/api/health").json()["llm"]
    assert llm["ready"] is True and llm["provider"] == "openai"
    assert llm["model"] and KEY not in json.dumps(llm)


# ------------------------------------------------------------- (f) persistence
def test_quality_and_warnings_survive_persistence(env, client):
    env.install(make_handler(echo_indices={1, 6, 11, 16}, always=True))
    job = wait_job(client, start_job(client, txt_bytes()))
    stored = {j["id"]: j for j in jobs_store.load_all_jobs()}
    assert job["id"] in stored
    s = stored[job["id"]]
    assert s["status"] == "completed"
    assert s["quality"] == job["quality"] and s["quality"]["still_untranslated"] == 4
    assert s["warnings"] == job["warnings"] and s["warnings"]
    assert s["effective_provider"] == "openai"


# ---------------------------------------------------------------------- (g) resume
def test_resume_resets_quality_and_warnings(env, client):
    env.install(make_handler())
    path = env.storage / "downloads" / "resume_me.txt"
    path.write_bytes(txt_bytes())
    cfg = LLMConfig(provider="openai", api_keys=[KEY], auto_glossary=False)
    job = m._create_job_record("resume_me", "txt", cfg, source_type="upload")
    job["input_file_path"] = str(path)
    job["status"] = "failed"
    job["error"] = "boom"
    job["quality"] = {
        "total": 50, "untranslated_found": 30, "retried_fixed": 0, "still_untranslated": 30,
        "percent_untranslated": 60.0,
    }
    job["warnings"] = ["⚠ stale warning from the previous run"]
    r = client.post(f"/api/jobs/{job['id']}/resume")
    assert r.status_code == 200, r.text
    done = wait_job(client, job["id"])
    assert done["status"] == "completed", done.get("error")
    assert done["quality"]["total"] == N_PARAS
    assert done["quality"]["still_untranslated"] == 0
    assert done["warnings"] == []
    assert done["effective_provider"] == "openai"
