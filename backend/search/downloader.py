from __future__ import annotations

import asyncio
import inspect
import ipaddress
import logging
import os
import re
import socket
import time
import uuid
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .models import BookSearchResult, score_candidate
from .providers import DEFAULT_HEADERS, search_books

logger = logging.getLogger(__name__)

# Magic bytes cho các định dạng phổ biến
ZIP_MAGIC_BYTES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
PDF_MAGIC_BYTES = b"%PDF"

INTERMEDIATE_HOST_PATTERNS = (
    "library.lol",
    "libgen.lc/ads",
    "libgen.li/ads",
    "libgen.is/ads",
    "libgen.rs/ads",
    "libgen.st/ads",
    "annas-archive.org/md5/",
    "annas-archive.se/md5/",
    "annas-archive.li/md5/",
    "annas-archive.gs/md5/",
)


def _check_public_url_sync(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"URL không hợp lệ: {url}")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except socket.gaierror as exc:
        raise ValueError(f"Không phân giải được host {parsed.hostname}: {exc}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f"Chặn URL trỏ tới địa chỉ nội bộ/không công khai: {parsed.hostname}")


async def _block_private_hosts(request: httpx.Request) -> None:
    """Event hook chạy cho cả mỗi lần redirect -> chống SSRF."""
    await asyncio.to_thread(_check_public_url_sync, str(request.url))


def _is_intermediate_url(url: str) -> bool:
    u_lower = url.lower()
    if any(p in u_lower for p in INTERMEDIATE_HOST_PATTERNS):
        return True
    if "ads.php" in u_lower or "/md5/" in u_lower:
        return True
    return False


def _normalize_special_url(url: str) -> str:
    """
    Chuyển đổi nhanh các URL đặc biệt (GitHub blob, Google Drive view) sang link tải trực tiếp.
    """
    u = url.strip()
    m_gh = re.match(r"https?://github\.com/([^/]+)/([^/]+)/blob/(.+)", u, re.I)
    if m_gh:
        return f"https://raw.githubusercontent.com/{m_gh.group(1)}/{m_gh.group(2)}/{m_gh.group(3)}"

    if "drive.google.com/file/d/" in u:
        m_gd = re.search(r"/file/d/([a-zA-Z0-9_-]{15,})", u)
        if m_gd:
            fid = m_gd.group(1)
            return f"https://drive.google.com/uc?export=download&id={fid}"

    return u


def _cleanup_stale_temp_files(directory: Path, max_age_seconds: float = 3600.0) -> int:
    """Xoá các file tạm `.tmp_*.bin` bị bỏ lại (job crash/huỷ) cũ hơn max_age_seconds."""
    removed = 0
    now = time.time()
    try:
        for f in Path(directory).glob(".tmp_*.bin"):
            try:
                if now - f.stat().st_mtime > max_age_seconds:
                    f.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed


def _sanitize_filename(name: str, max_len: int = 120) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    if not cleaned:
        cleaned = "downloaded_book"
    return cleaned[:max_len]


async def _notify_progress(progress_cb: Optional[Callable], payload: dict) -> None:
    if not progress_cb:
        return
    try:
        if inspect.iscoroutinefunction(progress_cb):
            await progress_cb(payload)
        else:
            res = progress_cb(payload)
            if inspect.isawaitable(res):
                await res
    except Exception as exc:
        logger.debug("Progress callback error: %s", exc)


def _extract_direct_links_from_html(html_text: str, base_url: str) -> list[str]:
    """
    Bóc tách các link tải trực tiếp từ trang trung gian:
    - library.lol/main/... hoặc library.lol/fiction/... (nút GET, Cloudflare, IPFS.io, Pinata)
    - libgen.lc/ads.php hoặc libgen.li/ads.php (get.php?md5=...&key=...)
    - annas-archive.../md5/...
    - Google Drive large file confirmation form
    - Trang web bất kỳ chứa link .pdf / .epub / .docx
    """
    soup = BeautifulSoup(html_text, "html.parser")
    direct_links: list[str] = []
    secondary_intermediates: list[str] = []

    # 0. Kiểm tra form xác nhận tải file lớn của Google Drive (drive.usercontent.google.com)
    gd_form = soup.find("form", id="download-form") or soup.find("form", action=re.compile(r"drive\.usercontent\.google\.com|uc\?export=download", re.I))
    if gd_form and gd_form.get("action"):
        action_url = gd_form.get("action")
        params: list[str] = []
        for inp in gd_form.find_all("input"):
            name = inp.get("name")
            val = inp.get("value", "")
            if name:
                params.append(f"{quote(str(name))}={quote(str(val))}")
        sep = "&" if "?" in action_url else "?"
        full_gd_url = f"{action_url}{sep}{'&'.join(params)}" if params else action_url
        direct_links.append(full_gd_url)

    # 1. Tìm trong thẻ #download (đặc trưng của library.lol)
    download_div = soup.find("div", id="download")
    if download_div:
        for a in download_div.find_all("a", href=True):
            full_href = urljoin(base_url, a["href"].strip())
            if full_href not in direct_links:
                direct_links.append(full_href)

    # 2. Duyệt tất cả các thẻ <a>, <iframe>, <embed> để tìm file trực tiếp
    for tag in soup.find_all(["a", "iframe", "embed"]):
        raw_href = (tag.get("href") or tag.get("src") or "").strip()
        if not raw_href or raw_href.startswith(("javascript:", "#", "mailto:")):
            continue
        full_href = _normalize_special_url(urljoin(base_url, raw_href))
        href_lower = full_href.lower()
        text_upper = (tag.get_text() or "").strip().upper()

        if (
            "get.php?" in href_lower
            or "cloudflare-ipfs" in href_lower
            or "ipfs.io/ipfs" in href_lower
            or "dweb.link/ipfs" in href_lower
            or "download.library.lol" in href_lower
            or "drive.usercontent.google.com/download" in href_lower
            or any(href_lower.endswith(ext) for ext in (".epub", ".pdf", ".docx", ".txt", ".mobi", ".azw3"))
            or re.search(r"\.(epub|pdf|docx)\?", href_lower)
            or text_upper in ("GET", "CLOUDFLARE", "IPFS.IO", "PINATA", "DOWNLOAD", "TẢI XUỐNG")
        ):
            if not _is_intermediate_url(full_href) and full_href not in direct_links:
                direct_links.append(full_href)
        elif any(h in href_lower for h in ("library.lol/main/", "library.lol/fiction/", "libgen.li/ads.php", "libgen.lc/ads.php")):
            if full_href != base_url and full_href not in secondary_intermediates:
                secondary_intermediates.append(full_href)

    return direct_links + secondary_intermediates


async def resolve_direct_download_url(
    result: BookSearchResult,
    client: httpx.AsyncClient,
) -> list[str]:
    """
    Chuẩn hóa và giải mã mọi URL (GitHub, Google Drive, Archive.org, LibGen, Anna's Archive)
    thành danh sách các direct download URL sẵn sàng stream.
    """
    candidate_urls: list[str] = []
    for raw_u in [result.download_url] + list(result.mirror_urls):
        if not raw_u:
            continue
        u = _normalize_special_url(raw_u)
        if u not in candidate_urls:
            candidate_urls.append(u)
        # Nếu là Google Drive uc?export=download&id=..., thêm luôn biến thể usercontent confirm=t
        m_gd = re.search(r"[?&]id=([a-zA-Z0-9_-]{15,})", u)
        if "drive.google.com" in u and m_gd:
            fid = m_gd.group(1)
            alt_gd = f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t"
            if alt_gd not in candidate_urls:
                candidate_urls.append(alt_gd)

    resolved_urls: list[str] = []
    visited_pages: set[str] = set()

    for url in candidate_urls:
        # Nếu là trang /details/{id} của Archive.org, giải mã qua metadata API
        m_ia = re.search(r"archive\.org/details/([a-zA-Z0-9_.\-]+)", url)
        if m_ia:
            ia_id = m_ia.group(1)
            try:
                r_meta = await client.get(f"https://archive.org/metadata/{ia_id}/files", timeout=6.5)
                if r_meta.status_code == 200:
                    for f in r_meta.json().get("result") or []:
                        fname = f.get("name") or ""
                        if fname.lower().endswith((".epub", ".pdf", ".docx", ".txt")) and "_lcp." not in fname.lower():
                            dl_u = f"https://archive.org/download/{ia_id}/{quote(fname)}"
                            if dl_u not in resolved_urls:
                                resolved_urls.append(dl_u)
            except Exception:
                pass
            continue

        if not _is_intermediate_url(url):
            if url not in resolved_urls:
                resolved_urls.append(url)
            continue

        if url in visited_pages:
            continue
        visited_pages.add(url)

        try:
            resp = await client.get(url, timeout=9.0, follow_redirects=True)
            if resp.status_code != 200:
                continue

            content_type = (resp.headers.get("content-type") or "").lower()
            if any(
                t in content_type
                for t in ("application/epub", "application/pdf", "application/octet-stream", "application/zip")
            ):
                final_url = str(resp.url)
                if final_url not in resolved_urls:
                    resolved_urls.append(final_url)
                continue

            extracted = _extract_direct_links_from_html(resp.text, str(resp.url))
            for link in extracted:
                if _is_intermediate_url(link) and link not in visited_pages:
                    visited_pages.add(link)
                    try:
                        sub_resp = await client.get(link, timeout=7.5, follow_redirects=True)
                        if sub_resp.status_code == 200:
                            sub_links = _extract_direct_links_from_html(sub_resp.text, str(sub_resp.url))
                            for sl in sub_links:
                                if not _is_intermediate_url(sl) and sl not in resolved_urls:
                                    resolved_urls.append(sl)
                    except Exception:
                        pass
                elif link not in resolved_urls:
                    resolved_urls.append(link)
        except Exception as exc:
            logger.debug("Failed to resolve intermediate URL %s: %s", url, exc)
            continue

    for u in candidate_urls:
        if u not in resolved_urls and not _is_intermediate_url(u):
            resolved_urls.append(u)

    return resolved_urls


def _detect_actual_format(first_bytes: bytes, content_type: str, expected_format: str) -> Optional[str]:
    """
    Nhận diện định dạng thật từ Magic Bytes đầu file:
    - Nếu là trang HTML lỗi -> trả về None.
    - Nếu bắt đầu bằng %PDF -> trả về 'pdf'.
    - Nếu bắt đầu bằng PK\\x03\\x04 (ZIP):
      + Nếu chứa 'epub' hoặc expected_format == 'epub' -> 'epub'
      + Nếu chứa 'word/' hoặc expected_format == 'docx' -> 'docx'
    - Giúp tự động sửa đuôi file kể cả khi link Google Drive / Web không ghi rõ đuôi!
    """
    if not first_bytes:
        return None

    ct_lower = (content_type or "").lower()
    fmt_lower = (expected_format or "pdf").lower().strip(".")
    head_strip = first_bytes.lstrip()[:512].lower()

    if (
        "text/html" in ct_lower
        or head_strip.startswith(b"<!doctype html")
        or head_strip.startswith(b"<html")
        or b"<head>" in head_strip
        or b"<body" in head_strip
    ):
        return None

    if PDF_MAGIC_BYTES in first_bytes[:1024]:
        return "pdf"

    if first_bytes.startswith(ZIP_MAGIC_BYTES):
        if b"epub" in first_bytes[:256].lower():
            return "epub"
        if b"word/" in first_bytes[:1024].lower() or b"[content_types].xml" in first_bytes[:1024].lower():
            return "docx" if fmt_lower == "docx" or b"word/" in first_bytes[:1024].lower() else "epub"
        return fmt_lower if fmt_lower in ("epub", "docx") else "epub"

    if fmt_lower in ("txt", "md"):
        return fmt_lower

    if fmt_lower in ("mobi", "azw3"):
        return fmt_lower

    # Nếu không phải PDF/ZIP nhưng expected_format là pdf/epub, kiểm tra xem có phải text thuần hợp lệ không
    return None


async def download_book(
    result: BookSearchResult,
    dest_dir: str,
    progress_cb: Optional[Callable] = None,
) -> str:
    """
    Thử lần lượt các direct URLs với streaming download.
    Nếu gặp trang HTML trung gian (VD: trang xác nhận virus scan của Google Drive hoặc trang giới thiệu PDF),
    tự động bóc tách link thực bên trong và tải lại ngay lập tức!
    """
    dest_path_obj = Path(dest_dir).resolve()
    dest_path_obj.mkdir(parents=True, exist_ok=True)

    initial_fmt = (result.format or "epub").lower().strip(".")
    safe_title = _sanitize_filename(
        f"{result.title} - {result.author}"
        if result.author and result.author not in ("Unknown", "")
        else result.title
    )

    _cleanup_stale_temp_files(dest_path_obj)

    timeout_cfg = httpx.Timeout(35.0, connect=10.0, read=28.0)
    last_error: Optional[Exception] = None

    async with httpx.AsyncClient(
        headers=DEFAULT_HEADERS, timeout=timeout_cfg, follow_redirects=True,
        event_hooks={"request": [_block_private_hosts]},
    ) as client:
        await _notify_progress(
            progress_cb,
            {"stage": "resolving", "title": result.title, "source": result.source},
        )
        direct_urls = await resolve_direct_download_url(result, client)
        if not direct_urls:
            raise RuntimeError(f"Không tìm thấy direct download URL nào cho sách '{result.title}'.")

        idx = 0
        rescue_attempted = False
        while idx < len(direct_urls) and idx < 16:
            url = direct_urls[idx]
            idx += 1
            temp_file = dest_path_obj / f".tmp_{safe_title[:60]}_{uuid.uuid4().hex[:12]}_{idx}.bin"
            detected_fmt = initial_fmt

            try:
                await _notify_progress(
                    progress_cb,
                    {
                        "stage": "connecting",
                        "url": url,
                        "attempt": idx,
                        "total_attempts": len(direct_urls),
                    },
                )
                async with client.stream("GET", url, follow_redirects=True) as resp:
                    if resp.status_code != 200:
                        raise RuntimeError(f"HTTP {resp.status_code} từ {url}")

                    content_type = resp.headers.get("content-type", "")
                    total_bytes = int(resp.headers.get("content-length") or 0)
                    downloaded = 0
                    first_chunk_checked = False
                    header_buffer = b""

                    with open(temp_file, "wb") as f_out:
                        async for chunk in resp.aiter_bytes(chunk_size=32768):
                            if not chunk:
                                continue
                            if not first_chunk_checked:
                                header_buffer += chunk
                                if len(header_buffer) < 1024 and (total_bytes == 0 or total_bytes > 1024):
                                    continue

                                actual_fmt = _detect_actual_format(header_buffer, content_type, initial_fmt)
                                if not actual_fmt:
                                    # Nếu nhận về trang HTML (VD: Google Drive confirm hoặc trang chứa link PDF),
                                    # đọc thêm tối đa 64KB để tìm link tải trực tiếp bên trong!
                                    html_bytes = header_buffer
                                    async for extra_chunk in resp.aiter_bytes(chunk_size=16384):
                                        html_bytes += extra_chunk
                                        if len(html_bytes) > 65536:
                                            break
                                    html_str = html_bytes.decode("utf-8", errors="ignore")
                                    sub_links = _extract_direct_links_from_html(html_str, str(resp.url))
                                    for sl in sub_links:
                                        if sl not in direct_urls:
                                            direct_urls.insert(idx, sl)
                                    raise ValueError(
                                        f"URL {url} trả về trang HTML thay vì file sách (đã bóc tách thêm {len(sub_links)} link con)."
                                    )

                                detected_fmt = actual_fmt
                                first_chunk_checked = True
                                f_out.write(header_buffer)
                                downloaded += len(header_buffer)
                                header_buffer = b""
                            else:
                                f_out.write(chunk)
                                downloaded += len(chunk)

                            if progress_cb:
                                await _notify_progress(
                                    progress_cb,
                                    {
                                        "stage": "downloading",
                                        "downloaded_bytes": downloaded,
                                        "total_bytes": total_bytes,
                                        "percent": round((downloaded / total_bytes) * 100, 1) if total_bytes else None,
                                    },
                                )

                        if not first_chunk_checked and header_buffer:
                            actual_fmt = _detect_actual_format(header_buffer, content_type, initial_fmt)
                            if not actual_fmt:
                                raise ValueError(
                                    f"File tải về quá nhỏ hoặc không đúng định dạng ({len(header_buffer)} bytes)."
                                )
                            detected_fmt = actual_fmt
                            f_out.write(header_buffer)
                            downloaded += len(header_buffer)

                    if downloaded < 128:
                        raise ValueError(f"File tải về từ {url} rỗng hoặc quá nhỏ ({downloaded} bytes).")

                result.format = detected_fmt
                target_file = dest_path_obj / f"{safe_title}.{detected_fmt}"
                if target_file.exists():
                    try:
                        target_file.unlink()
                    except OSError:
                        # File đang được job khác dùng -> không ghi đè, dùng tên duy nhất
                        target_file = dest_path_obj / f"{safe_title}_{uuid.uuid4().hex[:8]}.{detected_fmt}"
                temp_file.replace(target_file)

                await _notify_progress(
                    progress_cb,
                    {
                        "stage": "completed",
                        "file_path": str(target_file.resolve()),
                        "downloaded_bytes": downloaded,
                    },
                )
                return str(target_file.resolve())

            except asyncio.CancelledError:
                try:
                    temp_file.unlink()
                except OSError:
                    pass
                raise
            except Exception as exc:
                last_error = exc
                logger.debug("Download attempt %d failed for %s: %s", idx, url, exc)
                if temp_file.exists():
                    try:
                        temp_file.unlink()
                    except OSError:
                        pass

                # Auto-Rescue Fallback: Nếu đã thử hết toàn bộ direct_urls ban đầu mà đều lỗi (401/403/404),
                # tự động quét nhanh các kho công khai khác của cùng cuốn sách để cứu job!
                if idx >= len(direct_urls) and not rescue_attempted:
                    rescue_attempted = True
                    rescue_q = (
                        f"{result.title} {result.author}".strip()
                        if result.author and result.author not in ("Unknown", "")
                        else result.title
                    )
                    try:
                        alt_results = await search_books(rescue_q, limit=12)
                        # Ưu tiên cùng định dạng trước, sau đó đến các định dạng sách khác
                        alt_sorted = sorted(
                            alt_results,
                            key=lambda r: (0 if r.format.lower() == initial_fmt else 1, -r.priority_score),
                        )
                        added_rescue = 0
                        for alt in alt_sorted:
                            for cand_u in [alt.download_url] + list(alt.mirror_urls):
                                if cand_u and cand_u not in direct_urls:
                                    direct_urls.append(cand_u)
                                    added_rescue += 1
                                    if added_rescue >= 6:
                                        break
                            if added_rescue >= 6:
                                break
                    except Exception as rescue_exc:
                        logger.debug("Auto-rescue search failed for %s: %s", rescue_q, rescue_exc)
                continue

    raise RuntimeError(
        f"Không thể tải sách '{result.title}' sau khi thử {idx} link. Lỗi cuối: {last_error}"
    )


MIN_BOOK_CHARS = 20_000
MAX_CANDIDATES = 8
AUTO_TOTAL_TIMEOUT_S = 420.0
PER_CANDIDATE_TIMEOUT_S = 150.0
MIN_ACCEPTABLE_SCORE = 30


def _read_file_metadata_text(path: str) -> str:
    """Đọc nhanh metadata (title/creator) của EPUB/DOCX/PDF; lỗi -> chuỗi rỗng."""
    import zipfile

    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".epub":
            with zipfile.ZipFile(path) as zf:
                for name in zf.namelist():
                    if name.lower().endswith(".opf"):
                        raw = zf.read(name).decode("utf-8", errors="ignore")
                        parts = re.findall(r"<dc:(?:title|creator)[^>]*>(.*?)</dc:(?:title|creator)>", raw, re.S | re.I)
                        return " ".join(re.sub(r"<[^>]+>", " ", p) for p in parts)
        elif ext == ".docx":
            with zipfile.ZipFile(path) as zf:
                raw = zf.read("docProps/core.xml").decode("utf-8", errors="ignore")
                parts = re.findall(r"<dc:(?:title|creator)[^>]*>(.*?)</dc:(?:title|creator)>", raw, re.S | re.I)
                return " ".join(parts)
        elif ext == ".pdf":
            import fitz  # PyMuPDF

            with fitz.open(path) as doc:
                md = doc.metadata or {}
                return f"{md.get('title') or ''} {md.get('author') or ''}"
    except Exception:
        pass
    return ""


def validate_downloaded_book(
    path: str,
    query: str = "",
    title: str = "",
    author: str = "",
    min_chars: int = MIN_BOOK_CHARS,
    target_lang: str = "vi",
) -> tuple[bool, str]:
    """
    Kiểm tra rẻ xem file tải về có đúng là cuốn sách cần dịch không (đồng bộ, chạy trong thread).
    - mở được và có đủ văn bản (>= min_chars ký tự)
    - không phải rác OCR (tỷ lệ chữ cái thấp / nhiều ký tự lỗi)
    - không phải bản đã ở ngôn ngữ đích (vi)
    - token tiêu đề/tác giả (hoặc từ khóa người dùng) xuất hiện trong metadata hoặc các trang đầu
    Trả về (ok, lý do).
    """
    from .models import _fold, _tokens, _VI_DIACRITICS_RE

    if not os.path.isfile(path):
        return False, "file không tồn tại"
    try:
        from ..translators.converter import extract_book_sample_text  # lazy: tránh import vòng
    except Exception as exc:  # pragma: no cover
        logger.debug("Không import được extractor, bỏ qua validate: %s", exc)
        return True, "skipped (no extractor)"

    try:
        text = extract_book_sample_text(path, 80_000) or ""
    except Exception as exc:
        return False, f"không đọc được file: {exc}"

    stripped = text.strip()
    if len(stripped) < min_chars:
        return False, f"quá ít văn bản ({len(stripped)} < {min_chars} ký tự; có thể là bản scan/sample)"

    sample = stripped[:40_000]
    letters = sum(1 for c in sample if c.isalpha())
    if letters / max(1, len(sample)) < 0.5 or sample.count("\ufffd") > len(sample) * 0.01:
        return False, "văn bản giống rác OCR/mã hoá lỗi"

    if (target_lang or "vi").lower() == "vi" and letters:
        vi_chars = len(_VI_DIACRITICS_RE.findall(sample))
        if vi_chars / letters > 0.06:
            return False, "nội dung đã là tiếng Việt (không cần dịch)"

    meta = _read_file_metadata_text(path)
    haystack = set(_fold(f"{meta} {stripped[:30_000]}").split())

    def coverage(s: str) -> Optional[float]:
        toks = _tokens(s)
        if not toks:
            return None
        return sum(1 for t in toks if t in haystack) / len(toks)

    covs = [c for c in (coverage(f"{title} {author}" if author not in ("", "Unknown") else title), coverage(query)) if c is not None]
    if covs and max(covs) < 0.5:
        # Nếu query tiếng Việt và title không có -> không đủ dữ kiện để loại, chỉ loại khi có title
        if not (_VI_DIACRITICS_RE.search(query or "") and not title):
            return False, f"không thấy tên sách/tác giả trong metadata hoặc trang đầu (khớp {max(covs):.0%})"
    return True, "ok"


def rank_candidates(
    query: str,
    results: list[BookSearchResult],
    preferred_formats: Optional[list[str]] = None,
    target_lang: str = "vi",
) -> list[tuple[int, BookSearchResult, list[str]]]:
    """Chấm lại + sắp xếp ứng viên (điểm cao trước). Hàm thuần, dễ test."""
    from .providers import _normalize_query_variants

    pref = [f.lower().strip(".") for f in (preferred_formats or ["epub", "docx", "pdf"])]
    try:
        queries = [query] + [v for v in _normalize_query_variants(query) if v != query]
    except Exception:
        queries = [query]
    ranked: list[tuple[int, BookSearchResult, list[str]]] = []
    seen: set[str] = set()
    for r in results:
        key = (r.download_url or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        score, reasons = score_candidate(queries, r, target_lang=target_lang)
        fmt = (r.format or "").lower()
        if fmt in pref:
            bonus = max(0, 6 - 3 * pref.index(fmt))
            if bonus:
                score += bonus
                reasons.append(f"preferred_format:+{bonus}")
        else:
            score -= 12
            reasons.append("not_preferred_format:-12")
        ranked.append((score, r, reasons))
    ranked.sort(key=lambda t: (t[0], t[1].year or ""), reverse=True)
    return ranked


async def auto_search_and_download(
    query: str,
    dest_dir: str,
    preferred_formats: list[str] = ["epub", "docx", "pdf"],
    progress_cb: Optional[Callable] = None,
    target_lang: str = "vi",
) -> tuple[BookSearchResult, str]:
    """
    Tìm theo tên sách, chấm điểm (bản gốc, định dạng text sạch, khớp tên...), tải lần lượt các ứng viên tốt nhất
    và VALIDATE file (đủ văn bản, đúng sách). Ứng viên hỏng bị xoá và chuyển sang ứng viên kế tiếp,
    có giới hạn số lần thử và tổng thời gian.
    """
    await _notify_progress(progress_cb, {"stage": "searching", "query": query})
    all_results = await search_books(query, limit=35)
    if not all_results:
        raise ValueError(f"Không tìm thấy kết quả nào cho từ khóa: {query!r}")

    ranked = rank_candidates(query, all_results, preferred_formats, target_lang)
    good = [t for t in ranked if t[0] >= MIN_ACCEPTABLE_SCORE]
    chosen = good if good else ranked[:3]
    top_candidates = chosen[:MAX_CANDIDATES]
    errors: list[str] = []
    started = time.monotonic()

    for idx, (score, candidate, reasons) in enumerate(top_candidates):
        if time.monotonic() - started > AUTO_TOTAL_TIMEOUT_S:
            errors.append("Hết ngân sách thời gian tổng, dừng thử thêm ứng viên.")
            break
        file_path: Optional[str] = None
        try:
            await _notify_progress(
                progress_cb,
                {
                    "stage": "trying_candidate",
                    "candidate_index": idx + 1,
                    "total_candidates": len(top_candidates),
                    "title": candidate.title,
                    "format": candidate.format,
                    "source": candidate.source,
                    "score": score,
                },
            )
            remaining = AUTO_TOTAL_TIMEOUT_S - (time.monotonic() - started)
            file_path = await asyncio.wait_for(
                download_book(candidate, dest_dir=dest_dir, progress_cb=progress_cb),
                timeout=max(20.0, min(PER_CANDIDATE_TIMEOUT_S, remaining)),
            )
            await _notify_progress(progress_cb, {"stage": "validating", "title": candidate.title})
            ok, why = await asyncio.wait_for(
                asyncio.to_thread(
                    validate_downloaded_book,
                    file_path,
                    query,
                    candidate.title,
                    candidate.author,
                    MIN_BOOK_CHARS,
                    target_lang,
                ),
                timeout=60.0,
            )
            if not ok:
                raise ValueError(f"file không hợp lệ: {why}")
            return candidate, file_path
        except asyncio.TimeoutError:
            err_msg = f"[{candidate.source} - {candidate.format.upper()}] {candidate.title}: quá thời gian"
            logger.warning("Candidate %d timeout: %s", idx + 1, err_msg)
            errors.append(err_msg)
        except Exception as exc:
            err_msg = f"[{candidate.source} - {candidate.format.upper()}] {candidate.title}: {exc}"
            logger.warning("Candidate %d failed: %s", idx + 1, err_msg)
            errors.append(err_msg)
        if file_path:
            try:
                os.remove(file_path)
            except OSError:
                pass

    raise RuntimeError(
        f"Đã thử tải {len(errors)} ứng viên tốt nhất cho {query!r} nhưng đều thất bại:\n"
        + "\n".join(errors)
    )
