from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from . import jobs_store
from .llm import LLMConfig, TranslationLLMClient, default_cache
from .search import (
    BookSearchResult,
    auto_search_and_download,
    discover_book,
    download_book,
    search_books,
)
from .translators import (
    convert_to_docx,
    describe_book_layout,
    extract_book_spread_sample,
    translate_docx_file,
    translate_epub_file,
    translate_pdf_file,
    translate_txt_file,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("BookTranslatorHub")

BASE_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR / "frontend"
STORAGE_DIR = Path(os.environ.get("BTH_DATA_DIR") or (BACKEND_DIR / "storage"))  # dữ liệu runtime (có thể mount volume)
DOWNLOADS_DIR = STORAGE_DIR / "downloads"
OUTPUTS_DIR = STORAGE_DIR / "outputs"
FONTS_DIR = BACKEND_DIR / "storage" / "fonts"  # font đi kèm repo
SETTINGS_FILE = STORAGE_DIR / "settings.json"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

for d in (STORAGE_DIR, DOWNLOADS_DIR, OUTPUTS_DIR, FONTS_DIR):
    d.mkdir(parents=True, exist_ok=True)


def _load_saved_settings() -> LLMConfig:
    if SETTINGS_FILE.exists():
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if raw.get("provider") == "gtx":
                raw["provider"] = "google_free"
            return LLMConfig(**raw)
        except Exception as exc:
            logger.warning("Could not load settings.json: %s", exc)
    model_env = (os.environ.get("BTH_MODEL") or "").strip() or "cc/claude-sonnet-5-5"
    try:
        concurrency_env = max(1, min(32, int(os.environ.get("BTH_CONCURRENCY", "12"))))
    except ValueError:
        concurrency_env = 12
    return LLMConfig(
        provider="openai",
        base_url=os.environ.get("BTH_BASE_URL", "https://api.openai.com/v1"),
        api_keys=[os.environ["BTH_API_KEY"]] if os.environ.get("BTH_API_KEY") else [],
        model=model_env,
        agent_editor_model=model_env,
        concurrency=concurrency_env,
        target_lang="Tiếng Việt",
        target_lang_code="vi",
        auto_glossary=True,
    )


def _save_settings(cfg: LLMConfig) -> None:
    try:
        SETTINGS_FILE.write_text(
            json.dumps(cfg.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("Could not save settings.json: %s", exc)


GLOBAL_SETTINGS: LLMConfig = _load_saved_settings()

# In-memory job registry & cancellation events
JOBS: Dict[str, Dict[str, Any]] = {}
JOB_CANCEL_EVENTS: Dict[str, asyncio.Event] = {}
JOB_GLOSSARY_EVENTS: Dict[str, asyncio.Event] = {}
JOB_TASKS: Dict[str, asyncio.Task] = {}

# ---------------------------------------------------------------------------
# Job persistence (SQLite) — survives restarts / uvicorn --reload
# ---------------------------------------------------------------------------
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
FINISHED_STATUSES = TERMINAL_STATUSES + ("interrupted",)
PERSIST_MIN_INTERVAL = 2.0
MAX_LISTED_JOBS = 100
_JOB_LAST_SAVE: Dict[str, float] = {}
_JOB_LAST_STATUS: Dict[str, str] = {}


def _slim_job_for_storage(job: Dict[str, Any]) -> Dict[str, Any]:
    slim = dict(job)
    slim["previews"] = list(job.get("previews") or [])[-30:]
    slim["agent_logs"] = list(job.get("agent_logs") or [])[-40:]
    cfg = job.get("config_dump")
    if isinstance(cfg, dict):
        cfg = dict(cfg)
        for secret in ("api_keys", "gemini_api_keys"):
            if secret in cfg:
                cfg[secret] = []
        slim["config_dump"] = cfg
    return slim


def _persist_job(job: Dict[str, Any], force: bool = False) -> None:
    """Save a job; throttled to once per PERSIST_MIN_INTERVAL unless status changed/forced."""
    job_id = job.get("id")
    if not job_id:
        return
    now = time.time()
    status = job.get("status")
    changed = _JOB_LAST_STATUS.get(job_id) != status
    if not (force or changed or now - _JOB_LAST_SAVE.get(job_id, 0.0) >= PERSIST_MIN_INTERVAL):
        return
    try:
        jobs_store.save_job(_slim_job_for_storage(job))
        _JOB_LAST_SAVE[job_id] = now
        _JOB_LAST_STATUS[job_id] = status
    except Exception as exc:  # never break the pipeline because of persistence
        logger.warning("Could not persist job %s: %s", job_id, exc)


def _restore_jobs_from_store() -> None:
    try:
        stored = jobs_store.load_all_jobs()
    except Exception as exc:
        logger.warning("Could not load persisted jobs: %s", exc)
        return
    for job in stored:
        job_id = job["id"]
        if job.get("status") not in TERMINAL_STATUSES and job.get("status") != "interrupted":
            job["status"] = "interrupted"
            job["finished_at"] = time.time()
            job["message"] = (
                "Máy chủ đã khởi động lại khi tiến trình đang chạy. "
                "Bấm 'Tiếp tục' (resume) để chạy tiếp — các đoạn đã dịch được lấy từ bộ nhớ đệm."
            )
            try:
                jobs_store.save_job(job)
            except Exception:
                pass
        JOBS[job_id] = job
        _JOB_LAST_STATUS[job_id] = job["status"]


async def _job_persister_loop() -> None:
    while True:
        await asyncio.sleep(1.0)
        for job in list(JOBS.values()):
            if job.get("status") in TERMINAL_STATUSES and _JOB_LAST_STATUS.get(job["id"]) == job["status"]:
                continue
            _persist_job(job)

LANG_LABELS = {
    "vi": "Tiếng Việt",
    "en": "English",
    "zh": "中文 (Giản thể)",
    "ja": "日本語",
    "ko": "한국어",
    "fr": "Français",
    "de": "Deutsch",
    "es": "Español",
}


def _sanitize_slug(name: str, max_len: int = 80) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "book")
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._")
    return (cleaned or "book")[:max_len]


MASK_PREFIX = "••••"


def _mask_key(key: str) -> str:
    return f"{MASK_PREFIX}{key[-4:]}" if key else key


def _mask_settings(data: Dict[str, Any]) -> Dict[str, Any]:
    for field in ("api_keys", "gemini_api_keys"):
        data[field] = [_mask_key(k) for k in data.get(field) or []]
    return data


def _unmask_keys(incoming: Any, existing: List[str]) -> Any:
    """Thay các key đã che (••••abcd) bằng key thật đang lưu trên server."""
    if isinstance(incoming, str):
        incoming = [p for p in re.split(r"[,;\r\n]+", incoming) if p.strip()]
    if not isinstance(incoming, list):
        return incoming
    out: List[str] = []
    for item in incoming:
        item = str(item).strip()
        if item.startswith(MASK_PREFIX):
            tail = item[len(MASK_PREFIX):]
            match = next((k for k in existing if k.endswith(tail)), None)
            if match:
                out.append(match)
        elif item:
            out.append(item)
    return out


def _public_config_dump(cfg: LLMConfig) -> Dict[str, Any]:
    """config_dump lưu trong job: KHÔNG chứa API key (key lấy từ cài đặt toàn cục khi cần)."""
    data = cfg.model_dump()
    data["api_keys"] = []
    data["gemini_api_keys"] = []
    return data


def _ensure_llm_configured(cfg: LLMConfig) -> None:
    """Chưa có API key cho nhà cung cấp LLM đã chọn thì KHÔNG được âm thầm rơi về Google Translate miễn phí
    (bản dịch rất kém, hay trả nguyên tiếng gốc) mà phải báo lỗi rõ ràng ngay."""
    if cfg.provider in ("google_free", "ollama") or (cfg.model or "").strip().lower() == "gtx-free":
        return
    if TranslationLLMClient(config=cfg, cache=default_cache)._effective_provider() == "google_free":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Chưa cấu hình API key cho nhà cung cấp '{cfg.provider}', nên không thể dịch bằng AI "
                "(nếu cứ chạy, bản dịch sẽ rơi về Google Translate miễn phí rất kém). "
                "Máy cá nhân: nhập key trong Cài đặt. Server công khai: đặt biến môi trường BTH_API_KEY "
                "(và BTH_BASE_URL, BTH_MODEL nếu dùng gateway) rồi Redeploy. Muốn dùng Google Translate miễn phí "
                "thì chọn nhà cung cấp 'google_free' (gtx) một cách chủ động."
            ),
        )


def _cfg_from_job(job: Dict[str, Any], **updates: Any) -> LLMConfig:
    data = dict(job.get("config_dump") or GLOBAL_SETTINGS.model_dump())
    for field in ("api_keys", "gemini_api_keys"):
        if not data.get(field):
            data[field] = list(getattr(GLOBAL_SETTINGS, field))
    data.update(updates)
    return LLMConfig(**data)


def _merge_job_config(override: Optional[Dict[str, Any]]) -> LLMConfig:
    base_dict = GLOBAL_SETTINGS.model_dump()
    if override and isinstance(override, dict):
        override = dict(override)
        if _settings_locked():
            for field in LOCKED_CONFIG_FIELDS:
                override.pop(field, None)
        for field in ("api_keys", "gemini_api_keys"):
            if field in override:
                override[field] = _unmask_keys(override[field], base_dict.get(field) or [])
        for k, v in override.items():
            if v is not None and k in base_dict:
                base_dict[k] = v
        # Map UI aliases
        if override.get("provider") == "gtx":
            base_dict["provider"] = "google_free"
        if "target_lang_code" in override and override["target_lang_code"]:
            code = str(override["target_lang_code"]).strip().lower()
            base_dict["target_lang_code"] = code
            if not override.get("target_lang") and code in LANG_LABELS:
                base_dict["target_lang"] = LANG_LABELS[code]
        if "bilingual_mode" in override:
            base_dict["bilingual"] = override["bilingual_mode"] in (
                "bilingual",
                True,
                "true",
            )
    if base_dict.get("provider") == "gtx":
        base_dict["provider"] = "google_free"
    try:
        return LLMConfig(**base_dict)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", ()))
        raise HTTPException(
            status_code=422, detail=f"Cấu hình không hợp lệ ({loc}): {first.get('msg', exc)}"
        )


def _append_agent_log(
    job: Dict[str, Any],
    agent_id: int,
    agent_name: str,
    detail: str,
    model: str = "",
) -> None:
    logs = job.setdefault("agent_logs", [])
    logs.append(
        {
            "agent_id": agent_id,
            "agent_name": agent_name,
            "detail": detail,
            "model": model,
            "ts": round(time.time(), 1),
        }
    )
    if len(logs) > 80:
        del logs[:-80]


def _create_job_record(
    title: str,
    fmt: str,
    config: LLMConfig,
    source_type: str = "auto",
    query: str = "",
) -> Dict[str, Any]:
    job_id = uuid.uuid4().hex[:10]
    now = time.time()
    job = {
        "id": job_id,
        "title": title,
        "query": query or title,
        "format": (fmt or "auto").lower().lstrip("."),
        "source_type": source_type,
        "status": "queued",  # queued | searching | downloading | extracting_glossary | translating | editing | packaging | completed | failed | cancelled
        "stage": 1,  # 1: Agent 1 (Tìm & Giải mã), 2: Agent 2 (Từ điển), 3: Agent 3 (Dịch đa luồng), 4: Agent 4 (Biên tập), 5: Agent 5 (Xuất bản)
        "active_agent": 1,
        "progress": 0.0,
        "done_chunks": 0,
        "total_chunks": 0,
        "edited_chunks": 0,
        "speed_chunks_per_min": 0.0,
        "eta_seconds": None,
        "message": "Agent 1 đang khởi tạo tiến trình...",
        "target_lang": config.target_lang,
        "target_lang_code": config.target_lang_code,
        "bilingual": config.bilingual,
        "auto_glossary": config.auto_glossary,
        "enable_editor_agent": config.enable_editor_agent,
        "provider": config.provider,
        "model": config.model,
        "agent_models": {
            "glossary": config.agent_glossary_model or config.model,
            "translator": config.agent_translator_model or config.model,
            "editor": config.agent_editor_model or config.model,
        },
        "genre_preset": config.genre_preset,
        "custom_prompt": config.custom_prompt,
        "chapter_limit": config.chapter_limit,
        "page_range": config.page_range,
        "pause_for_glossary": config.pause_for_glossary,
        "input_file_path": None,
        "config_dump": _public_config_dump(config),
        "glossary": list(config.custom_glossary or []),
        "previews": [],  # list of {"src": ..., "draft": ..., "dst": ..., "was_edited": ..., "ts": ...}
        "agent_logs": [],
        "outputs": {},  # {"primary": "/api/download/...", "bilingual": "...", "docx": "..."}
        "output_files": {},  # filename labels
        "book_meta": None,
        "brief": "",
        "created_at": now,
        "started_at": now,
        "finished_at": None,
        "error": None,
    }
    JOBS[job_id] = job
    JOB_CANCEL_EVENTS[job_id] = asyncio.Event()
    JOB_GLOSSARY_EVENTS[job_id] = asyncio.Event()
    _persist_job(job, force=True)
    return job


async def _execute_translation_pipeline(
    job_id: str,
    input_file_path: Optional[str],
    config: LLMConfig,
    book_candidate: Optional[BookSearchResult] = None,
    auto_query: Optional[str] = None,
    preferred_format: str = "auto",
) -> None:
    job = JOBS.get(job_id)
    if not job:
        return
    cancel_event = JOB_CANCEL_EVENTS[job_id]

    def on_agent_event(ev: Dict[str, Any]) -> None:
        ag_id = int(ev.get("agent_id") or 3)
        job["active_agent"] = ag_id
        if ag_id == 4:
            job["stage"] = 4
        elif ag_id == 3 and job.get("stage", 1) < 3:
            job["stage"] = 3
        _append_agent_log(
            job,
            agent_id=ag_id,
            agent_name=str(ev.get("agent_name") or f"Agent {ag_id}"),
            detail=str(ev.get("detail") or ""),
            model=str(ev.get("model") or ""),
        )

    llm_client = TranslationLLMClient(
        config=config, cache=default_cache, agent_event_cb=on_agent_event
    )

    try:
        # =====================================================================
        # AGENT 1: SCOUT & PARSER AGENT (TÌM SÁCH & BÓC TÁCH CẤU TRÚC)
        # =====================================================================
        job["stage"] = 1
        job["active_agent"] = 1
        if not input_file_path:
            if auto_query:
                job["status"] = "searching"
                job["progress"] = 3.0
                job["message"] = (
                    f"[Agent 1 · Scout] Đang quét Anna's Archive, LibGen, Gutenberg cho '{auto_query}'..."
                )
                _append_agent_log(
                    job,
                    1,
                    "Agent 1 · Săn Sách & Giải Mã",
                    f"Quét đa nguồn tìm ấn bản tốt nhất cho '{auto_query}'",
                    "Multi-Source Crawler",
                )

                if preferred_format == "epub":
                    pref_list = ["epub", "docx", "pdf", "txt"]
                elif preferred_format == "pdf":
                    pref_list = ["pdf", "epub", "docx", "txt"]
                else:
                    pref_list = ["epub", "docx", "pdf", "txt"]

                async def dl_progress_cb(payload: dict) -> None:
                    st = payload.get("stage")
                    if st == "searching":
                        job["status"] = "searching"
                        job["message"] = f"[Agent 1 · Scout] Đang tìm kiếm sách '{auto_query}'..."
                    elif st == "trying_candidate":
                        job["status"] = "downloading"
                        c_title = payload.get("title", "")
                        c_fmt = str(payload.get("format", "")).upper()
                        c_src = payload.get("source", "")
                        job["message"] = (
                            f"[Agent 1 · Scout] Đã tìm thấy [{c_fmt}] '{c_title}' từ {c_src} — Đang tải..."
                        )
                        job["progress"] = 7.0
                    elif st == "downloading":
                        pct = payload.get("percent")
                        if pct is not None:
                            job["progress"] = min(14.0, 7.0 + (pct * 0.07))

                chosen_book, downloaded_path = await auto_search_and_download(
                    query=auto_query,
                    dest_dir=str(DOWNLOADS_DIR),
                    preferred_formats=pref_list,
                    progress_cb=dl_progress_cb,
                    target_lang=(config.target_lang_code or "vi").split("-")[0].lower(),
                )
                input_file_path = downloaded_path
                job["title"] = chosen_book.title
                job["format"] = chosen_book.format.lower().lstrip(".")
                job["book_meta"] = chosen_book.model_dump()

            elif book_candidate:
                job["status"] = "downloading"
                job["progress"] = 5.0
                job["message"] = (
                    f"[Agent 1 · Scout] Đang tải [{book_candidate.format.upper()}] '{book_candidate.title}' từ {book_candidate.source}..."
                )
                _append_agent_log(
                    job,
                    1,
                    "Agent 1 · Săn Sách & Giải Mã",
                    f"Tải ấn bản [{book_candidate.format.upper()}] '{book_candidate.title}' từ {book_candidate.source}",
                    "Direct Mirror",
                )

                async def single_dl_cb(payload: dict) -> None:
                    pct = payload.get("percent")
                    if pct is not None:
                        job["progress"] = min(14.0, 5.0 + (pct * 0.09))

                input_file_path = await download_book(
                    book_candidate,
                    dest_dir=str(DOWNLOADS_DIR),
                    progress_cb=single_dl_cb,
                )
                job["title"] = book_candidate.title
                job["format"] = book_candidate.format.lower().lstrip(".")
                job["book_meta"] = book_candidate.model_dump()
            else:
                raise ValueError("Không có nguồn file sách đầu vào hợp lệ.")

        if cancel_event.is_set():
            job["status"] = "cancelled"
            job["message"] = "Tiến trình đã bị hủy bởi người dùng."
            return

        input_path_obj = Path(input_file_path)
        if not input_path_obj.exists():
            raise FileNotFoundError(f"Không tìm thấy file sách: {input_file_path}")
        job["input_file_path"] = str(input_path_obj)

        ext = input_path_obj.suffix.lower().lstrip(".")
        if ext not in ("epub", "docx", "pdf", "txt", "md"):
            ext = job["format"] if job["format"] in ("epub", "docx", "pdf", "txt", "md") else "txt"
        job["format"] = ext
        job["progress"] = 15.0
        scope_note = ""
        if config.page_range:
            scope_note = f" · Phạm vi trang/chương: {config.page_range}"
        elif config.chapter_limit and config.chapter_limit > 0:
            scope_note = f" · Chế độ dịch thử: {config.chapter_limit} chương đầu"
        job["message"] = f"[Agent 1 · Parser] Đã bóc tách cấu trúc file ({ext.upper()}){scope_note}. Bàn giao cho Agent 2..."
        _append_agent_log(
            job,
            1,
            "Agent 1 · Săn Sách & Giải Mã",
            f"Giải mã hoàn tất cấu trúc sách ({ext.upper()}){scope_note} — bàn giao dữ liệu cho Agent 2",
            "DOM/Layout Parser",
        )

        # =====================================================================
        # AGENT 2: TERMINOLOGIST & CONTEXT AGENT (LẬP TỪ ĐIỂN THUẬT NGỮ)
        # =====================================================================
        job["stage"] = 2
        job["active_agent"] = 2
        glossary_list: List[Dict[str, str]] = list(config.custom_glossary or [])
        if config.auto_glossary:
            job["status"] = "extracting_glossary"
            job["progress"] = 18.0
            job["message"] = "[Agent 2 · Terminologist] Đang đọc mẫu sách & lập Bảng từ điển thuật ngữ chuẩn hóa..."
            try:
                sample_text = await asyncio.to_thread(
                    extract_book_spread_sample, str(input_path_obj), 15000
                )
                if sample_text and len(sample_text.strip()) > 80:
                    layout_hint = await asyncio.to_thread(describe_book_layout, str(input_path_obj))
                    profile = await llm_client.extract_book_profile(
                        sample_text, max_terms=40, layout_hint=layout_hint
                    )
                    extracted_terms = profile["glossary"]
                    if profile["brief"]:
                        llm_client.book_brief = profile["brief"]
                        job["brief"] = profile["brief"]
                    existing_srcs = {
                        str(g.get("src", "")).strip().lower() for g in glossary_list if g.get("src")
                    }
                    for item in extracted_terms:
                        s_key = str(item.get("src", "")).strip().lower()
                        if s_key and s_key not in existing_srcs:
                            glossary_list.append(item)
                            existing_srcs.add(s_key)
                    job["glossary"] = glossary_list
                    job["message"] = (
                        f"[Agent 2 · Terminologist] Đã lập xong {len(glossary_list)} thuật ngữ & quy tắc xưng hô. Bàn giao cho Agent 3..."
                    )
            except Exception as g_exc:
                logger.warning("Glossary extraction skipped for job %s: %s", job_id, g_exc)
        else:
            job["glossary"] = glossary_list
            llm_client.book_brief = str(job.get("brief") or "")
            _append_agent_log(
                job,
                2,
                "Agent 2 · Kỹ Sư Thuật Ngữ",
                f"Sử dụng {len(glossary_list)} thuật ngữ thủ công (bỏ qua trích xuất tự động)",
                "Custom Glossary",
            )

        if config.pause_for_glossary:
            glossary_ev = JOB_GLOSSARY_EVENTS.get(job_id)
            if glossary_ev and not glossary_ev.is_set():
                job["status"] = "awaiting_glossary_approval"
                job["progress"] = 20.0
                job["message"] = (
                    f"[Agent 2 · Chờ duyệt Từ điển] Đã trích xuất {len(job['glossary'])} thuật ngữ. Hãy kiểm tra/chỉnh sửa ở tab 'Từ điển Agent 2' rồi bấm 'Duyệt & Dịch tiếp'!"
                )
                _append_agent_log(
                    job,
                    2,
                    "Agent 2 · Kỹ Sư Thuật Ngữ",
                    f"Tạm dừng chờ người dùng duyệt {len(job['glossary'])} thuật ngữ trước khi giao cho Agent 3",
                    "Human-in-the-Loop",
                )
                while not glossary_ev.is_set() and not cancel_event.is_set():
                    await asyncio.sleep(0.4)

        if cancel_event.is_set():
            job["status"] = "cancelled"
            job["message"] = "Tiến trình đã bị hủy bởi người dùng."
            return

        # Cập nhật lại glossary mới nhất (nếu người dùng vừa sửa trên giao diện)
        glossary_list = list(job.get("glossary") or [])

        # =====================================================================
        # AGENT 3 (TRANSLATOR SWARM) + AGENT 4 (CHIEF EDITOR & CRITIC)
        # =====================================================================
        job["stage"] = 3
        job["active_agent"] = 3
        job["status"] = "translating"
        job["progress"] = 22.0
        translate_start_time = time.time()

        slug = _sanitize_slug(job["title"])
        lang_tag = config.target_lang_code or "vi"
        mode_tag = "bilingual" if config.bilingual else "mono"

        async def translation_progress_cb(
            done_items: int,
            total_items: int,
            latest_src: str = "",
            latest_dst: str = "",
        ) -> None:
            job["done_chunks"] = int(done_items)
            job["learned_terms"] = list(getattr(llm_client, "learned_terms", []) or [])[:200]
            job["total_chunks"] = int(total_items)
            if total_items > 0:
                ratio = min(1.0, max(0.0, done_items / total_items))
                # Map translation + editing progress from 22% to 92%
                job["progress"] = round(22.0 + ratio * 70.0, 1)
                elapsed = max(0.5, time.time() - translate_start_time)
                speed_per_min = (done_items / elapsed) * 60.0
                job["speed_chunks_per_min"] = round(speed_per_min, 1)
                remaining_items = max(0, total_items - done_items)
                if done_items > 0:
                    job["eta_seconds"] = int((remaining_items / done_items) * elapsed)

            if config.enable_editor_agent:
                job["message"] = (
                    f"[Agent 3 Dịch + Agent 4 Biên Tập] ({config.concurrency} luồng): {done_items}/{total_items} đoạn ({job['progress']}%)..."
                )
            else:
                job["message"] = (
                    f"[Agent 3 · Dịch Đa Luồng] ({config.concurrency} luồng): {done_items}/{total_items} đoạn ({job['progress']}%)..."
                )

            if latest_src and latest_dst:
                src_snip = latest_src.strip()
                dst_snip = latest_dst.strip()
                if src_snip and dst_snip:
                    previews = job["previews"]
                    if not previews or previews[-1].get("src") != src_snip:
                        diff_info = llm_client.last_editor_diffs.get(src_snip)
                        draft_text = diff_info["draft"] if diff_info else ""
                        was_edited = bool(
                            diff_info and draft_text and draft_text.strip() != dst_snip
                        )
                        if was_edited:
                            job["edited_chunks"] = int(job.get("edited_chunks", 0)) + 1
                        previews.append(
                            {
                                "index": done_items,
                                "src": src_snip[:600],
                                "draft": draft_text[:600] if was_edited else "",
                                "dst": dst_snip[:600],
                                "was_edited": was_edited,
                                "editor_model": diff_info.get("editor_model", "") if diff_info else "",
                                "ts": round(time.time(), 1),
                            }
                        )
                        if len(previews) > 60:
                            del previews[:-60]

        outputs_map: Dict[str, str] = {}

        if ext == "epub":
            primary_filename = f"{slug}_{lang_tag}_{mode_tag}_{job_id}.epub"
            primary_out_path = OUTPUTS_DIR / primary_filename
            res = await translate_epub_file(
                input_path=str(input_path_obj),
                output_path=str(primary_out_path),
                llm_client=llm_client,
                bilingual=config.bilingual,
                glossary=glossary_list,
                progress_cb=translation_progress_cb,
                cancel_event=cancel_event,
            )
            if res.get("status") == "cancelled" or cancel_event.is_set():
                job["status"] = "cancelled"
                job["message"] = "Đã dừng tiến trình dịch theo yêu cầu."
                return

            outputs_map["primary"] = f"/api/download/{primary_filename}"
            job["output_files"]["primary"] = primary_filename

            # AGENT 5: TYPESETTER & PUBLISHER AGENT
            job["stage"] = 5
            job["active_agent"] = 5
            job["status"] = "packaging"
            job["progress"] = 94.0
            job["message"] = "[Agent 5 · Publisher] Đang chế bản ấn phẩm Song ngữ & xuất file Microsoft Word (.DOCX)..."
            _append_agent_log(
                job,
                5,
                "Agent 5 · Chế Bản & Xuất Bản",
                "Đang đóng gói cấu trúc EPUB gốc, xuất bản Song ngữ & chuyển đổi DOCX",
                "EPUB/DOCX Typesetter",
            )

            if not config.bilingual:
                try:
                    bi_filename = f"{slug}_{lang_tag}_bilingual_{job_id}.epub"
                    bi_out_path = OUTPUTS_DIR / bi_filename
                    await translate_epub_file(
                        input_path=str(input_path_obj),
                        output_path=str(bi_out_path),
                        llm_client=llm_client,
                        bilingual=True,
                        glossary=glossary_list,
                        progress_cb=None,
                        cancel_event=cancel_event,
                    )
                    outputs_map["bilingual"] = f"/api/download/{bi_filename}"
                    job["output_files"]["bilingual"] = bi_filename
                except Exception as bi_exc:
                    logger.warning("Could not create secondary bilingual EPUB: %s", bi_exc)
            else:
                outputs_map["bilingual"] = outputs_map["primary"]
                job["output_files"]["bilingual"] = primary_filename

            try:
                docx_filename = f"{slug}_{lang_tag}_{job_id}.docx"
                docx_out_path = OUTPUTS_DIR / docx_filename
                await asyncio.to_thread(
                    convert_to_docx, str(primary_out_path), str(docx_out_path)
                )
                if docx_out_path.exists():
                    outputs_map["docx"] = f"/api/download/{docx_filename}"
                    job["output_files"]["docx"] = docx_filename
            except Exception as docx_exc:
                logger.warning("Could not convert EPUB to DOCX: %s", docx_exc)

        elif ext == "docx":
            primary_filename = f"{slug}_{lang_tag}_{mode_tag}_{job_id}.docx"
            primary_out_path = OUTPUTS_DIR / primary_filename
            res = await translate_docx_file(
                input_path=str(input_path_obj),
                output_path=str(primary_out_path),
                llm_client=llm_client,
                bilingual=config.bilingual,
                glossary=glossary_list,
                progress_cb=translation_progress_cb,
                cancel_event=cancel_event,
            )
            if res.get("status") == "cancelled" or cancel_event.is_set():
                job["status"] = "cancelled"
                job["message"] = "Đã dừng tiến trình dịch theo yêu cầu."
                return

            job["stage"] = 5
            job["active_agent"] = 5
            job["status"] = "packaging"
            job["progress"] = 95.0
            _append_agent_log(
                job,
                5,
                "Agent 5 · Chế Bản & Xuất Bản",
                "Đang đóng gói ấn phẩm Word (.DOCX) giữ nguyên định dạng gốc & bản Song ngữ",
                "DOCX Typesetter",
            )
            outputs_map["primary"] = f"/api/download/{primary_filename}"
            outputs_map["docx"] = f"/api/download/{primary_filename}"
            job["output_files"]["primary"] = primary_filename
            job["output_files"]["docx"] = primary_filename

            if not config.bilingual:
                try:
                    bi_filename = f"{slug}_{lang_tag}_bilingual_{job_id}.docx"
                    bi_out_path = OUTPUTS_DIR / bi_filename
                    await translate_docx_file(
                        input_path=str(input_path_obj),
                        output_path=str(bi_out_path),
                        llm_client=llm_client,
                        bilingual=True,
                        glossary=glossary_list,
                        progress_cb=None,
                        cancel_event=cancel_event,
                    )
                    outputs_map["bilingual"] = f"/api/download/{bi_filename}"
                    job["output_files"]["bilingual"] = bi_filename
                except Exception as bi_exc:
                    logger.warning("Could not create secondary bilingual DOCX: %s", bi_exc)
            else:
                outputs_map["bilingual"] = outputs_map["primary"]

        elif ext == "pdf":
            base_name = f"{slug}_{lang_tag}_{job_id}"
            res = await translate_pdf_file(
                input_path=str(input_path_obj),
                output_dir=str(OUTPUTS_DIR),
                base_name=base_name,
                llm_client=llm_client,
                bilingual=config.bilingual,
                glossary=glossary_list,
                engine_mode="auto",
                progress_cb=translation_progress_cb,
                cancel_event=cancel_event,
            )
            if res.get("status") == "cancelled" or cancel_event.is_set():
                job["status"] = "cancelled"
                job["message"] = "Đã dừng tiến trình dịch theo yêu cầu."
                return

            job["stage"] = 5
            job["active_agent"] = 5
            job["status"] = "packaging"
            job["progress"] = 96.0
            _append_agent_log(
                job,
                5,
                "Agent 5 · Chế Bản & Xuất Bản",
                "Hoàn tất tái tạo bố cục PDF (nhúng font Tiếng Việt) & xuất kèm file Word (.DOCX)",
                "PDF Layout Engine",
            )

            primary_path = res.get("primary_output") or res.get("mono_pdf")
            mono_path = res.get("mono_pdf")
            bi_path = res.get("bilingual_pdf")
            docx_path = res.get("docx_output")

            if primary_path and Path(primary_path).exists():
                fn = Path(primary_path).name
                outputs_map["primary"] = f"/api/download/{fn}"
                job["output_files"]["primary"] = fn
            elif mono_path and Path(mono_path).exists():
                fn = Path(mono_path).name
                outputs_map["primary"] = f"/api/download/{fn}"
                job["output_files"]["primary"] = fn

            if bi_path and Path(bi_path).exists():
                fn = Path(bi_path).name
                outputs_map["bilingual"] = f"/api/download/{fn}"
                job["output_files"]["bilingual"] = fn

            if docx_path and Path(docx_path).exists():
                fn = Path(docx_path).name
                outputs_map["docx"] = f"/api/download/{fn}"
                job["output_files"]["docx"] = fn

        else:
            # .txt or .md
            out_ext = "md" if ext == "md" else "txt"
            primary_filename = f"{slug}_{lang_tag}_{mode_tag}_{job_id}.{out_ext}"
            primary_out_path = OUTPUTS_DIR / primary_filename
            res = await translate_txt_file(
                input_path=str(input_path_obj),
                output_path=str(primary_out_path),
                llm_client=llm_client,
                bilingual=config.bilingual,
                glossary=glossary_list,
                progress_cb=translation_progress_cb,
                cancel_event=cancel_event,
            )
            if res.get("status") == "cancelled" or cancel_event.is_set():
                job["status"] = "cancelled"
                job["message"] = "Đã dừng tiến trình dịch theo yêu cầu."
                return

            job["stage"] = 5
            job["active_agent"] = 5
            job["status"] = "packaging"
            job["progress"] = 95.0
            _append_agent_log(
                job,
                5,
                "Agent 5 · Chế Bản & Xuất Bản",
                "Đóng gói bản dịch TXT/Markdown & chuyển đổi sang Microsoft Word (.DOCX)",
                "Document Builder",
            )
            outputs_map["primary"] = f"/api/download/{primary_filename}"
            job["output_files"]["primary"] = primary_filename

            try:
                docx_filename = f"{slug}_{lang_tag}_{job_id}.docx"
                docx_out_path = OUTPUTS_DIR / docx_filename
                await asyncio.to_thread(
                    convert_to_docx, str(primary_out_path), str(docx_out_path)
                )
                if docx_out_path.exists():
                    outputs_map["docx"] = f"/api/download/{docx_filename}"
                    job["output_files"]["docx"] = docx_filename
            except Exception as docx_exc:
                logger.warning("Could not convert TXT/MD to DOCX: %s", docx_exc)

        job["outputs"] = outputs_map
        job["progress"] = 100.0
        job["eta_seconds"] = 0
        job["status"] = "completed"
        job["stage"] = 5
        job["active_agent"] = 5
        job["finished_at"] = time.time()
        elapsed_total = round(job["finished_at"] - job["started_at"], 1)
        job["message"] = (
            f"Hệ thống 5 Agent đã hoàn tất xuất bản '{job['title']}' ({job['format'].upper()}) sang {config.target_lang} trong {elapsed_total}s!"
        )
        _append_agent_log(
            job,
            5,
            "Agent 5 · Chế Bản & Xuất Bản",
            f"Hoàn tất toàn bộ dây chuyền xuất bản trong {elapsed_total}s — Sẵn sàng tải về!",
            "Completed",
        )

    except asyncio.CancelledError:
        if cancel_event.is_set() or job.get("status") == "cancelled":
            job["status"] = "cancelled"
        else:
            # Server shutdown / reload killed the task: keep it resumable.
            job["status"] = "interrupted"
            job["message"] = (
                "Tiến trình bị gián đoạn do máy chủ dừng/khởi động lại. "
                "Bấm 'Tiếp tục' (resume) để chạy tiếp."
            )
        job["finished_at"] = time.time()
        raise
    except Exception as exc:
        logger.exception("Job %s failed: %s", job_id, exc)
        job["status"] = "failed"
        job["error"] = str(exc)
        job["message"] = f"Lỗi: {exc}"
        job["finished_at"] = time.time()
    finally:
        _persist_job(job, force=True)


# ============================================================================
# FASTAPI APP & ENDPOINTS
# ============================================================================

app = FastAPI(
    title="AI Book Translator Hub API",
    version="2.5.0",
    description="1-Click Book Search, Auto-Download & Multi-Agent/Multi-Thread AI Book Translation Hub",
)



@app.on_event("startup")
async def _start_job_persister() -> None:
    _restore_jobs_from_store()
    app.state.job_persister = asyncio.create_task(_job_persister_loop())


@app.on_event("shutdown")
async def _flush_jobs_on_shutdown() -> None:
    task = getattr(app.state, "job_persister", None)
    if task:
        task.cancel()
    for t in list(JOB_TASKS.values()):
        if not t.done():
            t.cancel()
    # Let cancelled pipelines mark themselves 'interrupted' and persist.
    pending = [t for t in JOB_TASKS.values()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for job in list(JOBS.values()):
        _persist_job(job, force=True)


ALLOWED_ORIGIN_RE = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"

# --- Triển khai trên server -------------------------------------------------
# BTH_ALLOWED_HOSTS: domain được phép truy cập, phân tách bằng dấu phẩy (VD: books.example.com)
# BTH_AUTH_USER / BTH_AUTH_PASSWORD: đăng nhập HTTP Basic cho toàn bộ app (BẮT BUỘC khi mở ra ngoài)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
ALLOWED_HOSTS = {
    h.strip().lower() for h in os.environ.get("BTH_ALLOWED_HOSTS", "").split(",") if h.strip()
}
ALLOW_ANY_HOST = bool(os.environ.get("BTH_ALLOW_REMOTE"))
AUTH_USER = os.environ.get("BTH_AUTH_USER", "admin")
AUTH_PASSWORD = os.environ.get("BTH_AUTH_PASSWORD", "")
PUBLIC_EXEMPT_PATHS = ("/api/ping",)
# Cố ý tắt đăng nhập khi mở ra ngoài (ai có link đều dùng được app). Khi đó cấu hình key/URL bị KHÓA.
DISABLE_AUTH = os.environ.get("BTH_DISABLE_AUTH", "").strip().lower() in ("1", "true", "yes")
# Các trường cho phép đổi key/endpoint LLM: nếu cho khách ẩn danh đổi, họ chuyển key sang server của họ.
LOCKED_CONFIG_FIELDS = ("api_keys", "gemini_api_keys", "base_url", "gemini_base_url")

if (ALLOWED_HOSTS or ALLOW_ANY_HOST) and not AUTH_PASSWORD and not DISABLE_AUTH:
    raise RuntimeError(
        "Mở app ra ngoài localhost (BTH_ALLOWED_HOSTS / BTH_ALLOW_REMOTE) bắt buộc phải đặt BTH_AUTH_PASSWORD "
        "(hoặc cố ý đặt BTH_DISABLE_AUTH=1 và truyền key qua BTH_API_KEY)."
    )
if (ALLOWED_HOSTS or ALLOW_ANY_HOST) and not AUTH_PASSWORD:
    logger.warning(
        "App công khai KHÔNG đăng nhập: ai có link cũng tạo job và tốn credit LLM. "
        "Cài đặt key/URL bị khóa, hãy đặt BTH_API_KEY / BTH_BASE_URL bằng biến môi trường."
    )


def _settings_locked() -> bool:
    """Công khai + không đăng nhập => cấm đổi key/endpoint qua API."""
    return bool(ALLOWED_HOSTS or os.environ.get("BTH_ALLOW_REMOTE")) and not AUTH_PASSWORD

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=ALLOWED_ORIGIN_RE,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _host_of(value: str) -> str:
    value = (value or "").strip().lower()
    if value.startswith("["):  # IPv6 dạng [::1]:8088
        return value[1:].split("]")[0]
    return value.split(":")[0]


def _basic_auth_ok(header: str) -> bool:
    if not header.lower().startswith("basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:].strip()).decode("utf-8").partition(":")
    except Exception:
        return False
    ok_user = hmac.compare_digest(user.encode(), AUTH_USER.encode())
    ok_pass = hmac.compare_digest(password.encode(), AUTH_PASSWORD.encode())
    return ok_user and ok_pass


@app.get("/api/ping")
async def api_ping() -> Dict[str, Any]:
    return {"ok": True}


@app.middleware("http")
async def block_foreign_origins(request: Request, call_next):
    """Chặn trang web lạ (CSRF / DNS rebinding) và yêu cầu đăng nhập khi chạy trên server."""
    host = _host_of(request.headers.get("host") or "")
    if not os.environ.get("BTH_ALLOW_REMOTE") and host not in LOCAL_HOSTS and host not in ALLOWED_HOSTS:
        return JSONResponse({"detail": "Host không được phép."}, status_code=403)

    if request.method not in ("GET", "HEAD", "OPTIONS") or request.url.path == "/api/settings":
        origin = request.headers.get("origin")
        if origin:
            origin_host = _host_of(origin.split("://", 1)[-1])
            if not re.match(ALLOWED_ORIGIN_RE, origin) and origin_host not in ALLOWED_HOSTS and not (
                os.environ.get("BTH_ALLOW_REMOTE") and origin_host == host
            ):
                return JSONResponse({"detail": "Origin không được phép."}, status_code=403)

    if AUTH_PASSWORD and request.url.path not in PUBLIC_EXEMPT_PATHS and request.method != "OPTIONS":
        if not _basic_auth_ok(request.headers.get("authorization", "")):
            return JSONResponse(
                {"detail": "Cần đăng nhập."},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="BookTranslatorHub"'},
            )
    return await call_next(request)


class AutoJobRequest(BaseModel):
    query: str
    preferred_format: str = "auto"
    config: Optional[Dict[str, Any]] = None


class FromSearchJobRequest(BaseModel):
    book: Dict[str, Any]
    config: Optional[Dict[str, Any]] = None


@app.get("/api/health")
async def api_health() -> Dict[str, Any]:
    return {
        "ok": True,
        "service": "AI Book Translator Hub",
        "cache_stats": default_cache.get_stats(),
        "active_jobs": sum(
            1
            for j in JOBS.values()
            if j["status"]
            in ("queued", "searching", "downloading", "extracting_glossary", "translating", "packaging")
        ),
    }


@app.get("/api/settings")
async def api_get_settings() -> Dict[str, Any]:
    data = _mask_settings(GLOBAL_SETTINGS.model_dump())
    # Provide UI friendly alias as well
    data["ui_provider"] = "gtx" if data["provider"] == "google_free" else data["provider"]
    data["cache_stats"] = default_cache.get_stats()
    return {"ok": True, "settings": data}


@app.post("/api/settings")
async def api_save_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    global GLOBAL_SETTINGS
    if _settings_locked():
        raise HTTPException(
            status_code=403,
            detail="Cài đặt key/endpoint đang bị khóa trên server công khai không đăng nhập. "
            "Đặt BTH_API_KEY / BTH_BASE_URL bằng biến môi trường.",
        )
    merged = _merge_job_config(payload)
    GLOBAL_SETTINGS = merged
    _save_settings(GLOBAL_SETTINGS)
    data = _mask_settings(GLOBAL_SETTINGS.model_dump())
    data["ui_provider"] = "gtx" if data["provider"] == "google_free" else data["provider"]
    return {
        "ok": True,
        "message": "Đã lưu cấu hình AI & API Key thành công!",
        "settings": data,
    }


@app.post("/api/settings/test")
async def api_test_settings(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = _merge_job_config(payload) if payload else GLOBAL_SETTINGS
    client = TranslationLLMClient(config=cfg, cache=default_cache)
    res = await client.test_connection()
    return res


@app.get("/api/settings/models")
async def api_get_models() -> Dict[str, Any]:
    client = TranslationLLMClient(config=GLOBAL_SETTINGS, cache=default_cache)
    return await client.discover_available_models()


@app.post("/api/settings/models")
async def api_discover_models(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = _merge_job_config(payload) if payload else GLOBAL_SETTINGS
    client = TranslationLLMClient(config=cfg, cache=default_cache)
    return await client.discover_available_models()



@app.get("/api/search")
async def api_search_books(
    q: str = Query(..., min_length=1, description="Tên sách hoặc tác giả cần tìm"),
    limit: int = Query(25, ge=1, le=50),
) -> Dict[str, Any]:
    results = await search_books(q.strip(), limit=limit)
    return {
        "ok": True,
        "query": q.strip(),
        "count": len(results),
        "results": [r.model_dump() for r in results],
    }


@app.get("/api/discover")
async def api_discover_book(
    q: str = Query(..., min_length=2, max_length=200, description="Tên sách / tác giả / ISBN"),
    lang: str = Query("vi", max_length=8),
    country: str = Query("VN", min_length=2, max_length=2),
) -> Dict[str, Any]:
    """Tra cứu THÔNG TIN sách (ấn bản, bản dịch, cách kiếm hợp pháp: mua/mượn/bản mở)."""
    result = await discover_book(q.strip(), target_lang=lang.split("-")[0].lower(), country=country.upper())
    return {"ok": True, **result}


@app.post("/api/jobs/auto")
async def api_start_auto_job(req: AutoJobRequest) -> Dict[str, Any]:
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Vui lòng nhập tên sách cần tìm và dịch.")

    cfg = _merge_job_config(req.config)
    _ensure_llm_configured(cfg)
    job = _create_job_record(
        title=query,
        fmt=req.preferred_format,
        config=cfg,
        source_type="auto_search",
        query=query,
    )
    task = asyncio.create_task(
        _execute_translation_pipeline(
            job_id=job["id"],
            input_file_path=None,
            config=cfg,
            auto_query=query,
            preferred_format=req.preferred_format,
        )
    )
    JOB_TASKS[job["id"]] = task
    return {"ok": True, "job_id": job["id"], "job": job}


@app.post("/api/jobs/from-search")
async def api_start_from_search_job(req: FromSearchJobRequest) -> Dict[str, Any]:
    try:
        book = BookSearchResult(**req.book)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Dữ liệu sách không hợp lệ: {exc}")

    cfg = _merge_job_config(req.config)
    _ensure_llm_configured(cfg)
    job = _create_job_record(
        title=book.title,
        fmt=book.format,
        config=cfg,
        source_type="selected_search",
        query=book.title,
    )
    task = asyncio.create_task(
        _execute_translation_pipeline(
            job_id=job["id"],
            input_file_path=None,
            config=cfg,
            book_candidate=book,
        )
    )
    JOB_TASKS[job["id"]] = task
    return {"ok": True, "job_id": job["id"], "job": job}


@app.post("/api/jobs/upload")
async def api_start_upload_job(
    file: UploadFile = File(...),
    config: Optional[str] = Form(default=None),
) -> Dict[str, Any]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="File tải lên không hợp lệ.")

    ext = Path(file.filename).suffix.lower().lstrip(".")
    if ext not in ("epub", "docx", "pdf", "txt", "md"):
        raise HTTPException(
            status_code=400,
            detail=f"Định dạng .{ext} chưa được hỗ trợ. Hãy chọn file .epub, .docx, .pdf, .txt hoặc .md.",
        )

    cfg_override = {}
    if config:
        try:
            cfg_override = json.loads(config)
        except Exception:
            pass
    cfg = _merge_job_config(cfg_override)
    _ensure_llm_configured(cfg)

    safe_name = f"{_sanitize_slug(Path(file.filename).stem)}_{uuid.uuid4().hex[:6]}.{ext}"
    saved_path = DOWNLOADS_DIR / safe_name
    written = 0
    try:
        with open(saved_path, "wb") as f_out:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="File vượt quá giới hạn 200MB.")
                f_out.write(chunk)
    except HTTPException:
        saved_path.unlink(missing_ok=True)
        raise

    job = _create_job_record(
        title=Path(file.filename).stem,
        fmt=ext,
        config=cfg,
        source_type="upload",
        query=file.filename,
    )
    task = asyncio.create_task(
        _execute_translation_pipeline(
            job_id=job["id"],
            input_file_path=str(saved_path),
            config=cfg,
        )
    )
    JOB_TASKS[job["id"]] = task
    return {"ok": True, "job_id": job["id"], "job": job}


HEAVY_JOB_FIELDS = ("previews", "agent_logs", "glossary", "brief", "learned_terms", "config_dump")


@app.get("/api/jobs")
async def api_list_jobs(active: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """Danh sách job (rút gọn). Job `active` được trả đầy đủ để giao diện không phải tải cả MB mỗi lần poll."""
    sorted_jobs = sorted(
        JOBS.values(), key=lambda x: x.get("created_at", 0), reverse=True
    )[:MAX_LISTED_JOBS]
    result = [
        j if j.get("id") == active else {k: v for k, v in j.items() if k not in HEAVY_JOB_FIELDS}
        for j in sorted_jobs
    ]
    return {"ok": True, "jobs": result}


@app.get("/api/jobs/{job_id}")
async def api_get_job(job_id: str) -> Dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy tiến trình dịch này.")
    return {"ok": True, "job": job}


@app.post("/api/jobs/{job_id}/cancel")
async def api_cancel_job(job_id: str) -> Dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy tiến trình dịch này.")
    if job["status"] in ("completed", "failed", "cancelled", "interrupted"):
        return {"ok": True, "job": job}
    ev = JOB_CANCEL_EVENTS.get(job_id)
    if ev:
        ev.set()
    task = JOB_TASKS.get(job_id)
    if task and not task.done():
        task.cancel()
    job["status"] = "cancelled"
    job["finished_at"] = time.time()
    job["message"] = "Đã dừng tiến trình dịch theo yêu cầu."
    _persist_job(job, force=True)
    return {"ok": True, "job": job}


@app.post("/api/jobs/{job_id}/resume")
async def api_resume_job(job_id: str) -> Dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy tiến trình dịch này.")
    if job["status"] not in ("interrupted", "failed", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail="Chỉ có thể tiếp tục tiến trình bị gián đoạn, thất bại hoặc đã hủy.",
        )
    task = JOB_TASKS.get(job_id)
    if task and not task.done():
        raise HTTPException(status_code=409, detail="Tiến trình vẫn đang chạy.")
    input_file_path = job.get("input_file_path")
    if not input_file_path or not Path(input_file_path).is_file():
        raise HTTPException(
            status_code=400,
            detail="Không tìm thấy file sách gốc (có thể chưa tải xong hoặc đã bị xóa). "
            "Hãy tạo tiến trình mới.",
        )

    cfg = _cfg_from_job(job)
    _ensure_llm_configured(cfg)
    cfg.pause_for_glossary = False
    saved_glossary = list(job.get("glossary") or [])
    if saved_glossary:
        cfg.auto_glossary = False
        cfg.custom_glossary = saved_glossary

    JOB_CANCEL_EVENTS[job_id] = asyncio.Event()
    JOB_GLOSSARY_EVENTS[job_id] = asyncio.Event()
    now = time.time()
    job["status"] = "translating"
    job["stage"] = max(1, min(int(job.get("stage") or 1), 3))
    job["error"] = None
    job["finished_at"] = None
    job["started_at"] = now
    job["message"] = "[Resume] Đang tiếp tục tiến trình — các đoạn đã dịch sẽ lấy từ bộ nhớ đệm..."
    _append_agent_log(
        job,
        3,
        "Agent 3 · Dịch Đa Luồng",
        "Tiếp tục tiến trình từ file gốc đã lưu (đoạn đã dịch lấy từ cache)",
        "Resume",
    )
    _persist_job(job, force=True)
    JOB_TASKS[job_id] = asyncio.create_task(
        _execute_translation_pipeline(
            job_id=job_id,
            input_file_path=input_file_path,
            config=cfg,
        )
    )
    return {"ok": True, "job": job}


@app.delete("/api/jobs/{job_id}")
async def api_delete_job(job_id: str) -> Dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy tiến trình dịch này.")
    if job["status"] not in FINISHED_STATUSES:
        raise HTTPException(status_code=409, detail="Tiến trình đang chạy, hãy hủy trước khi xóa.")
    task = JOB_TASKS.get(job_id)
    if task and not task.done():
        raise HTTPException(status_code=409, detail="Tiến trình vẫn đang chạy.")

    removed_files: List[str] = []
    outputs_root = OUTPUTS_DIR.resolve()
    for fname in list((job.get("output_files") or {}).values()):
        try:
            target = (OUTPUTS_DIR / str(fname)).resolve()
            if target.parent == outputs_root and target.is_file():
                target.unlink()
                removed_files.append(target.name)
        except Exception as exc:
            logger.warning("Could not remove output %s: %s", fname, exc)

    JOBS.pop(job_id, None)
    JOB_TASKS.pop(job_id, None)
    JOB_CANCEL_EVENTS.pop(job_id, None)
    JOB_GLOSSARY_EVENTS.pop(job_id, None)
    _JOB_LAST_SAVE.pop(job_id, None)
    _JOB_LAST_STATUS.pop(job_id, None)
    try:
        jobs_store.delete_job(job_id)
    except Exception as exc:
        logger.warning("Could not delete job %s from store: %s", job_id, exc)
    return {"ok": True, "deleted": job_id, "removed_files": removed_files}


@app.get("/api/download/{filename}")
async def api_download_output(filename: str) -> FileResponse:
    safe_fn = Path(filename).name
    file_path = OUTPUTS_DIR / safe_fn
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File kết quả không tồn tại hoặc đã bị xóa.")
    return FileResponse(
        path=str(file_path),
        filename=safe_fn,
        media_type="application/octet-stream",
    )


class GlossaryUpdateRequest(BaseModel):
    glossary: List[Dict[str, str]] = Field(default_factory=list)


class PreviewSegmentUpdateRequest(BaseModel):
    index: Optional[int] = None
    src: str
    dst: str


class PreviewSegmentRepolishRequest(BaseModel):
    index: Optional[int] = None
    src: str
    draft: str = ""
    instruction: str = ""


class QuickTranslateRequest(BaseModel):
    text: str
    config: Optional[Dict[str, Any]] = None


def _store_segment_override_in_cache(
    job: Dict[str, Any], src_text: str, dst_text: str
) -> None:
    cfg = _cfg_from_job(job)
    client = TranslationLLMClient(config=cfg, cache=default_cache)
    tr_override = cfg.agent_translator_model or None
    provider = client._effective_provider(model_override=tr_override)
    model = client._effective_model(model_override=tr_override)
    ed_tag = f"+ed:{cfg.agent_editor_model or model}" if cfg.enable_editor_agent else ""
    cache_model_key = f"{model}{ed_tag}"
    default_cache.set(
        src_text=src_text,
        dst_text=dst_text,
        target_lang=cfg.target_lang,
        provider=provider,
        model=cache_model_key,
    )
    default_cache.set(
        src_text=src_text,
        dst_text=dst_text,
        target_lang=cfg.target_lang,
        provider="manual_override",
        model="editor_override",
    )


@app.put("/api/jobs/{job_id}/glossary")
async def api_update_job_glossary(
    job_id: str, req: GlossaryUpdateRequest
) -> Dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy tiến trình dịch này.")
    cleaned: List[Dict[str, str]] = []
    for item in req.glossary or []:
        s = str(item.get("src") or "").strip()
        d = str(item.get("dst") or "").strip()
        n = str(item.get("note") or "").strip()
        if s and d:
            cleaned.append({"src": s, "dst": d, "note": n})
    job["glossary"] = cleaned
    _append_agent_log(
        job,
        2,
        "Agent 2 · Kỹ Sư Thuật Ngữ",
        f"Đã cập nhật bảng thuật ngữ ({len(cleaned)} mục)",
        "Glossary Editor",
    )
    return {"ok": True, "glossary": cleaned, "job": job}


@app.post("/api/jobs/{job_id}/glossary/approve")
async def api_approve_job_glossary(
    job_id: str, req: Optional[GlossaryUpdateRequest] = None
) -> Dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy tiến trình dịch này.")
    if req and req.glossary is not None:
        cleaned = [
            {
                "src": str(it.get("src") or "").strip(),
                "dst": str(it.get("dst") or "").strip(),
                "note": str(it.get("note") or "").strip(),
            }
            for it in req.glossary
            if str(it.get("src") or "").strip() and str(it.get("dst") or "").strip()
        ]
        job["glossary"] = cleaned

    ev = JOB_GLOSSARY_EVENTS.get(job_id)
    if ev:
        ev.set()
    _append_agent_log(
        job,
        2,
        "Agent 2 · Kỹ Sư Thuật Ngữ",
        f"Người dùng đã duyệt {len(job.get('glossary') or [])} thuật ngữ — Bàn giao cho Agent 3 bắt đầu dịch!",
        "Approved",
    )
    return {"ok": True, "job": job}


class BriefUpdateRequest(BaseModel):
    brief: str = ""


@app.put("/api/jobs/{job_id}/brief")
async def api_update_job_brief(job_id: str, req: BriefUpdateRequest) -> Dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy tiến trình dịch này.")
    job["brief"] = (req.brief or "").strip()
    _append_agent_log(
        job,
        2,
        "Agent 2 · Kỹ Sư Thuật Ngữ",
        f"Người dùng đã cập nhật BOOK BRIEF ({len(job['brief'])} ký tự) — sẽ áp dụng cho các đoạn dịch/đóng gói tiếp theo.",
        "Updated",
    )
    return {"ok": True, "brief": job["brief"], "job": job}


@app.post("/api/jobs/{job_id}/previews/update")
async def api_update_preview_segment(
    job_id: str, req: PreviewSegmentUpdateRequest
) -> Dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy tiến trình dịch này.")
    src_clean = (req.src or "").strip()
    dst_clean = (req.dst or "").strip()
    if not src_clean or not dst_clean:
        raise HTTPException(status_code=400, detail="Nội dung đoạn văn không được để trống.")

    updated_item = None
    for p in job.get("previews") or []:
        if (req.index is not None and p.get("index") == req.index) or p.get("src") == src_clean:
            if not p.get("draft"):
                p["draft"] = p.get("dst", "")
            p["dst"] = dst_clean
            p["was_edited"] = True
            p["editor_model"] = "Sửa thủ công (Inline Edit)"
            updated_item = p
            break

    _store_segment_override_in_cache(job, src_clean, dst_clean)
    _append_agent_log(
        job,
        4,
        "Agent 4 · Hiệu Đính Trực Tiếp",
        f"Đã lưu bản sửa tay cho đoạn #{req.index or '?'} vào Bộ nhớ đệm xuất bản",
        "Inline Editor",
    )
    return {"ok": True, "preview": updated_item, "job": job}


@app.post("/api/jobs/{job_id}/previews/repolish")
async def api_repolish_preview_segment(
    job_id: str, req: PreviewSegmentRepolishRequest
) -> Dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy tiến trình dịch này.")
    src_clean = (req.src or "").strip()
    if not src_clean:
        raise HTTPException(status_code=400, detail="Không tìm thấy đoạn văn gốc.")

    cfg = _cfg_from_job(job)
    client = TranslationLLMClient(config=cfg, cache=default_cache)
    client.book_brief = str(job.get("brief") or "")
    polished = await client.polish_single_segment(
        src_text=src_clean,
        draft_text=req.draft or "",
        instruction=req.instruction or "",
        glossary=job.get("glossary") or [],
    )
    ed_model = cfg.agent_editor_model or cfg.model

    updated_item = None
    for p in job.get("previews") or []:
        if (req.index is not None and p.get("index") == req.index) or p.get("src") == src_clean:
            if not p.get("draft"):
                p["draft"] = req.draft or p.get("dst", "")
            p["dst"] = polished
            p["was_edited"] = True
            p["editor_model"] = ed_model
            updated_item = p
            break

    job["edited_chunks"] = int(job.get("edited_chunks", 0)) + 1
    _store_segment_override_in_cache(job, src_clean, polished)
    _append_agent_log(
        job,
        4,
        "Agent 4 · Tổng Biên Tập",
        f"Đã chuốt lại đoạn #{req.index or '?'} theo yêu cầu trực tiếp",
        ed_model,
    )
    return {"ok": True, "polished": polished, "preview": updated_item, "job": job}


@app.post("/api/jobs/{job_id}/repackage")
async def api_repackage_job(job_id: str) -> Dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy tiến trình dịch này.")
    if job["status"] not in ("completed", "failed", "cancelled"):
        raise HTTPException(status_code=409, detail="Tiến trình đang chạy, chưa thể đóng gói lại.")
    input_file_path = job.get("input_file_path")
    if not input_file_path or not Path(input_file_path).exists():
        raise HTTPException(
            status_code=400,
            detail="Không tìm thấy file sách gốc để đóng gói lại.",
        )

    cfg = _cfg_from_job(
        job,
        pause_for_glossary=False,
        auto_glossary=False,
        custom_glossary=list(job.get("glossary") or []),
    )

    JOB_CANCEL_EVENTS[job_id] = asyncio.Event()
    job["status"] = "packaging"
    job["stage"] = 5
    job["active_agent"] = 5
    job["message"] = "[Agent 5 · Re-export] Đang đóng gói lại ấn phẩm với các đoạn văn vừa hiệu đính..."
    _append_agent_log(
        job,
        5,
        "Agent 5 · Chế Bản & Xuất Bản",
        "Đang kết xuất lại toàn bộ file xuất bản từ Bộ nhớ đệm & bản hiệu đính mới nhất",
        "Re-Packager",
    )

    task = asyncio.create_task(
        _execute_translation_pipeline(
            job_id=job_id,
            input_file_path=input_file_path,
            config=cfg,
        )
    )
    JOB_TASKS[job_id] = task
    return {"ok": True, "job": job}


@app.post("/api/translate/quick")
async def api_quick_translate(req: QuickTranslateRequest) -> Dict[str, Any]:
    raw_text = (req.text or "").strip()
    if not raw_text:
        raise HTTPException(status_code=400, detail="Vui lòng nhập đoạn văn hoặc chương cần dịch.")

    t0 = time.perf_counter()
    cfg = _merge_job_config(req.config)
    _ensure_llm_configured(cfg)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", raw_text) if p.strip()]
    if not paragraphs:
        paragraphs = [raw_text]

    # Step 1: Extract mini glossary if auto_glossary is enabled and text is long enough
    client_no_ed = TranslationLLMClient(
        config=cfg.model_copy(update={"enable_editor_agent": False}),
        cache=default_cache,
    )
    glossary = list(cfg.custom_glossary or [])
    if cfg.auto_glossary and len(raw_text) > 250:
        try:
            extracted = await client_no_ed.extract_glossary(raw_text[:8000], max_terms=15)
            existing = {g.get("src", "").lower() for g in glossary}
            for item in extracted:
                if item.get("src", "").lower() not in existing:
                    glossary.append(item)
        except Exception:
            pass

    # Step 2: Agent 3 draft translation
    draft_paragraphs = await client_no_ed.translate_batch(paragraphs, glossary=glossary)
    draft_text = "\n\n".join(draft_paragraphs)

    # Step 3: Agent 4 literary refinement (if enabled)
    polished_paragraphs = draft_paragraphs
    if cfg.enable_editor_agent:
        client_ed = TranslationLLMClient(config=cfg, cache=default_cache)
        polished_paragraphs = await client_ed.refine_batch_with_editor(
            paragraphs, draft_paragraphs, glossary
        )
    polished_text = "\n\n".join(polished_paragraphs)
    elapsed_s = round(time.perf_counter() - t0, 2)

    return {
        "ok": True,
        "draft": draft_text,
        "polished": polished_text,
        "glossary": glossary,
        "paragraphs_count": len(paragraphs),
        "translator_model": cfg.agent_translator_model or cfg.model,
        "editor_model": cfg.agent_editor_model or cfg.model if cfg.enable_editor_agent else None,
        "elapsed_seconds": elapsed_s,
    }


@app.get("/api/cache/stats")
async def api_cache_stats() -> Dict[str, Any]:
    return {"ok": True, "cache_stats": default_cache.get_stats()}


@app.post("/api/cache/clear")
async def api_cache_clear() -> Dict[str, Any]:
    removed = default_cache.clear()
    return {
        "ok": True,
        "removed_entries": removed,
        "cache_stats": default_cache.get_stats(),
        "message": f"Đã xóa sạch {removed} đoạn dịch trong Bộ nhớ đệm (Cache)!",
    }


# Mount static frontend assets and index.html
if (FRONTEND_DIR / "assets").exists():
    app.mount(
        "/assets",
        StaticFiles(directory=str(FRONTEND_DIR / "assets")),
        name="assets",
    )


@app.get("/")
async def serve_dashboard() -> FileResponse:
    return FileResponse(str(FRONTEND_DIR / "index.html"))
