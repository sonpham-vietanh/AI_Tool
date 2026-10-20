from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
from typing import Optional
from urllib.parse import quote, quote_plus, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .models import BookSearchResult

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
}

SUPPORTED_FORMATS = {"epub", "docx", "pdf", "txt", "md", "mobi", "azw3"}

LIBGEN_DOMAINS = [
    "https://libgen.is",
    "https://libgen.rs",
    "https://libgen.st",
    "https://libgen.li",
]

ANNAS_ARCHIVE_DOMAINS = [
    "https://annas-archive.org",
    "https://annas-archive.se",
    "https://annas-archive.li",
]

# Từ điển ánh xạ nhanh các tựa sách phổ biến sang tên gốc + tác giả để tìm chuẩn ấn bản
VI_TO_EN_BOOK_TITLES: dict[str, str] = {
    "thói quen nguyên tử": "Atomic Habits James Clear",
    "atomic habits": "Atomic Habits James Clear",
    "tâm lý học về tiền": "The Psychology of Money Morgan Housel",
    "the psychology of money": "The Psychology of Money Morgan Housel",
    "psychology of money": "The Psychology of Money Morgan Housel",
    "tư duy nhanh và chậm": "Thinking Fast and Slow Daniel Kahneman",
    "thinking fast and slow": "Thinking Fast and Slow Daniel Kahneman",
    "thinking, fast and slow": "Thinking Fast and Slow Daniel Kahneman",
    "làm việc sâu": "Deep Work Cal Newport",
    "deep work": "Deep Work Cal Newport",
    "cha giàu cha nghèo": "Rich Dad Poor Dad Robert Kiyosaki",
    "nhà giả kim": "The Alchemist Paulo Coelho",
    "đắc nhân tâm": "How to Win Friends and Influence People Dale Carnegie",
    "lược sử loài người": "Sapiens A Brief History of Humankind Yuval Noah Harari",
    "lược sử tương lai": "Homo Deus Yuval Noah Harari",
    "21 bài học cho thế kỷ 21": "21 Lessons for the 21st Century Yuval Noah Harari",
    "từ tốt đến vĩ đại": "Good to Great Jim Collins",
    "khởi nghiệp tinh gọn": "The Lean Startup Eric Ries",
    "không gia đình": "Sans Famille Hector Malot",
    "hoàng tử bé": "The Little Prince Antoine de Saint-Exupery",
    "chiến tranh tiền tệ": "Currency Wars Song Hongbing",
    "người giàu có nhất thành babylon": "The Richest Man in Babylon",
    "bí mật tư duy triệu phú": "Secrets of the Millionaire Mind",
    "tuần làm việc 4 giờ": "The 4-Hour Workweek Tim Ferriss",
    "nghệ thuật tinh tế của việc đếch quan tâm": "The Subtle Art of Not Giving a F*ck Mark Manson",
    "đi tìm lẽ sống": "Man's Search for Meaning Viktor Frankl",
}

JUNK_DOMAINS = (
    "yahoo.com",
    "bing.com",
    "google.com/search",
    "uservoice.com",
    "facebook.com",
    "twitter.com",
    "instagram.com",
    "youtube.com",
    "tiktok.com",
    "amazon.com",
    "goodreads.com",
    "Barnesandnoble.com",
    "wikipedia.org",
    "reddit.com",
    "quora.com",
    "pinterest.com",
    "linkedin.com",
    "ebay.com",
    "walmart.com",
    "target.com",
    "audible.com",
    "apple.com",
    "spotify.com",
    "netflix.com",
)


def _make_id(prefix: str, unique_str: str) -> str:
    digest = hashlib.md5(unique_str.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _clean_text(text: Optional[str]) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _extract_year_and_edition(text: str, url: str = "") -> tuple[str, str]:
    combined = f"{text} {unquote(url)}"
    year = ""
    m_year = re.search(r"\b(19[7-9]\d|20[0-2]\d)\b", combined)
    if m_year:
        year = m_year.group(1)

    edition_parts: list[str] = []
    # Phát hiện ấn bản (1st, 2nd, 3rd, Revised, Updated, Expanded, v.v.)
    m_ed = re.search(
        r"\b(\d+(?:st|nd|rd|th)\s+ed(?:ition)?|revised\s+ed(?:ition)?|updated\s+ed(?:ition)?|expanded\s+ed(?:ition)?|international\s+ed(?:ition)?|collector'?s\s+ed(?:ition)?|tái\s+bản[^,.;)|]*)\b",
        combined,
        re.I,
    )
    if m_ed:
        edition_parts.append(m_ed.group(1).strip())

    # Phát hiện ngày phát hành trong tên file (VD: 8.28.19, 10-21-19, v2.1)
    m_ver = re.search(r"\b(\d{1,2}[.-]\d{1,2}[.-]\d{2,4}|v\d+\.\d+)\b", unquote(url), re.I)
    if m_ver:
        ver_str = m_ver.group(1)
        if ver_str not in edition_parts:
            edition_parts.append(f"Bản {ver_str}")

    if re.search(r"\b(tiếng\s*việt|vietnamese|bản\s*dịch|vietsub)\b", combined, re.I):
        edition_parts.append("Bản Tiếng Việt")

    edition = " · ".join(edition_parts)
    return year, edition


def _prettify_filename_title(url_or_name: str, fallback: str = "") -> str:
    raw = unquote(url_or_name.split("?")[0].rstrip("/").split("/")[-1])
    raw = re.sub(r"\.(epub|pdf|docx|txt|md|mobi|azw3)$", "", raw, flags=re.I)
    raw = re.sub(r"[_+]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" -_.")
    if len(raw) < 4 or raw.isdigit():
        return fallback or "Book Document"
    return raw


def _normalize_query_variants(raw_query: str) -> list[str]:
    """
    Tạo các biến thể truy vấn sạch (bỏ dấu ':', tách tiêu đề chính trước ':' + tên tác giả sau 'by')
    để Yahoo Search không bị lỗi '500 INKApi Error' và Archive.org tìm trúng kho công khai.
    Ví dụ:
      'The Art of Learning: An Inner Journey to Optimal Performance by Josh Waitzkin'
      -> ['The Art of Learning Josh Waitzkin', 'The Art of Learning An Inner Journey to Optimal Performance Josh Waitzkin']
    """
    q = (raw_query or "").strip()
    if not q:
        return []

    variants: list[str] = []

    # Tách phần tác giả nếu có ' by Author'
    author_part = ""
    title_part = q
    m_by = re.search(r"\s+\bby\s+([A-Za-zÀ-ỹ0-9.\s,'-]+)$", q, re.I)
    if m_by:
        author_part = m_by.group(1).strip()
        title_part = q[: m_by.start()].strip()

    # Nếu tiêu đề có dấu ':' hoặc ' - ' (phụ đề dài), lấy tiêu đề chính + tác giả làm truy vấn ngắn chuẩn nhất
    if ":" in title_part or " - " in title_part:
        main_title = re.split(r"\s*:\s*|\s+[-–—]\s+", title_part, maxsplit=1)[0].strip()
        if len(main_title) >= 3:
            short_q = f"{main_title} {author_part}".strip()
            short_q = re.sub(r"[:?!\"'()\[\]{}]+", " ", short_q)
            short_q = re.sub(r"\s+", " ", short_q).strip()
            if short_q and short_q not in variants:
                variants.append(short_q)

    # Bản đầy đủ nhưng đã loại bỏ từ nối 'by' và ký tự ':' gây lỗi Yahoo/Lucene
    full_clean = f"{title_part} {author_part}".strip() if author_part else q
    full_clean = re.sub(r"[:?!\"'()\[\]{}]+", " ", full_clean)
    full_clean = re.sub(r"\s+", " ", full_clean).strip()
    if full_clean and full_clean not in variants:
        variants.append(full_clean)

    return variants


async def _expand_bilingual_queries(query: str, client: httpx.AsyncClient) -> list[str]:
    """
    Nếu người dùng nhập tên sách bằng tiếng Việt, tên ngắn, hoặc tên kèm phụ đề dài ('Title: Subtitle by Author'),
    tự động mở rộng thành danh sách từ khóa sạch gồm cả tên ngắn + tác giả và tên tiếng Anh.
    """
    q_clean = query.strip()
    queries = _normalize_query_variants(q_clean)
    if not queries:
        queries = [q_clean]
    q_lower = q_clean.lower()

    for vi_name, en_name in VI_TO_EN_BOOK_TITLES.items():
        if vi_name in q_lower:
            if en_name not in queries:
                queries.insert(0, en_name)
            break

    # Kiểm tra nếu có ký tự tiếng Việt có dấu mà chưa có trong từ điển
    if len(queries) == 1 and re.search(r"[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]", q_lower):
        try:
            resp = await client.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": "vi", "tl": "en", "dt": "t", "q": q_clean},
                timeout=3.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                en_translated = "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()
                for v in _normalize_query_variants(en_translated):
                    if v and v.lower() != q_lower and v not in queries:
                        queries.append(v)
        except Exception:
            pass

    return queries


# ============================================================================
# NGUỒN 1: WEB SEARCH & LEAK HUNTER (YAHOO / DUCKDUCKGO DORKS)
# Săn trực tiếp file PDF, EPUB, DOCX, GitHub Raw, Google Drive, Archive.org, CDN
# ============================================================================
async def _run_yahoo_dork(client: httpx.AsyncClient, dork_query: str) -> list[tuple[str, str, str]]:
    """
    Trả về danh sách (title, url, snippet) từ Yahoo Search.
    Tự động retry nếu Yahoo trả về 500 ở request đầu tiên.
    """
    hits: list[tuple[str, str, str]] = []
    search_endpoints = [
        "https://vn.search.yahoo.com/search",
        "https://search.yahoo.com/search",
    ]
    resp = None
    for ep in search_endpoints:
        for attempt in range(2):
            try:
                r = await client.get(ep, params={"p": dork_query}, timeout=6.5, follow_redirects=True)
                if r.status_code == 200 and len(r.text) > 2000:
                    resp = r
                    break
            except Exception:
                continue
        if resp:
            break

    if not resp:
        return hits

    soup = BeautifulSoup(resp.text, "html.parser")
    seen_urls: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        actual_url = ""
        if "RU=" in href:
            try:
                actual_url = unquote(href.split("RU=")[1].split("/RK=")[0])
            except Exception:
                actual_url = ""
        elif href.startswith(("http://", "https://")) and "yahoo.com" not in href:
            actual_url = href

        if not actual_url.startswith(("http://", "https://")):
            continue
        u_lower = actual_url.lower()
        if any(junk in u_lower for junk in JUNK_DOMAINS):
            continue
        if actual_url in seen_urls:
            continue

        title_text = _clean_text(a.get_text(" ", strip=True))
        # Bỏ qua các link phân trang hoặc menu
        if not title_text or title_text.lower() in ("next", "prev", "tiếp", "trước", "cached"):
            continue
        # Bỏ breadcrumb ở đầu title của Yahoo (VD: "github.com › user › repo Title")
        if "›" in title_text:
            parts = [p.strip() for p in title_text.split("›") if p.strip()]
            if len(parts) >= 2:
                # Phần cuối chứa tên trang thực sự
                last_part = parts[-1]
                # Tách phần path ngắn đầu tiên nếu có
                title_text = last_part

        parent_div = a.find_parent("div", class_=re.compile(r"algo|dd|Sr", re.I)) or a.find_parent("li")
        snippet = _clean_text(parent_div.get_text(" ", strip=True)) if parent_div else ""

        seen_urls.add(actual_url)
        hits.append((title_text, actual_url, snippet))

    return hits


async def _run_ddg_lite_dork(client: httpx.AsyncClient, dork_query: str) -> list[tuple[str, str, str]]:
    """
    Dự phòng DuckDuckGo Lite nếu cần thêm kết quả.
    """
    hits: list[tuple[str, str, str]] = []
    try:
        r = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": dork_query},
            timeout=6.0,
            follow_redirects=True,
        )
        if r.status_code != 200:
            return hits
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("a.result__a"):
            href = a.get("href", "")
            if "uddg=" in href:
                actual_url = unquote(href.split("uddg=")[1].split("&")[0])
            else:
                actual_url = href
            if not actual_url.startswith(("http://", "https://")):
                continue
            if any(junk in actual_url.lower() for junk in JUNK_DOMAINS):
                continue
            title_text = _clean_text(a.get_text(" ", strip=True))
            hits.append((title_text, actual_url, ""))
    except Exception:
        pass
    return hits


def _classify_web_hit_to_result(
    raw_title: str,
    url: str,
    snippet: str,
    query: str,
) -> Optional[BookSearchResult]:
    """
    Phân tích 1 URL tìm được trên mạng (GitHub, Google Drive, Direct PDF/EPUB/DOCX,
    Archive.org, Scribd/Manual, CDN...) thành BookSearchResult chuẩn kèm thông tin phiên bản.
    """
    u_lower = url.lower()
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().replace("www.", "")
    path_lower = (parsed.path or "").lower()

    # 1. Xử lý GitHub Leak (github.com/.../blob/... hoặc raw.githubusercontent.com)
    if "github.com" in host or "githubusercontent.com" in host:
        dl_url = url
        mirrors: list[str] = []
        if "github.com/" in u_lower and "/blob/" in u_lower:
            # Chuyển sang raw.githubusercontent.com
            m_gh = re.match(r"https?://github\.com/([^/]+)/([^/]+)/blob/(.+)", url, re.I)
            if m_gh:
                user, repo, rest = m_gh.group(1), m_gh.group(2), m_gh.group(3)
                dl_url = f"https://raw.githubusercontent.com/{user}/{repo}/{rest}"
                mirrors.append(f"https://github.com/{user}/{repo}/raw/{rest}")

        m_ext = re.search(r"\.(epub|pdf|docx|txt|md|mobi|azw3)(?:$|\?)", dl_url.lower())
        if not m_ext:
            return None
        fmt = m_ext.group(1)
        nice_title = _prettify_filename_title(dl_url, fallback=raw_title)
        year, edition = _extract_year_and_edition(f"{raw_title} {snippet}", dl_url)
        if not edition:
            edition = "Bản Leak GitHub"
        return BookSearchResult(
            id=_make_id("gh", dl_url),
            title=nice_title,
            author=_extract_author_hint(raw_title, snippet, host="GitHub"),
            format=fmt,
            size="",
            language="",
            year=year,
            edition=edition,
            source="GitHub Repo Leak",
            download_url=dl_url,
            mirror_urls=mirrors,
        ).ensure_score(query)

    # 2. Xử lý Google Drive Leak (drive.google.com/file/d/{ID} hoặc drive.google.com/open?id={ID})
    if "drive.google.com" in host or "docs.google.com" in host:
        m_id = re.search(r"/file/d/([a-zA-Z0-9_-]{15,})", url) or re.search(r"[?&]id=([a-zA-Z0-9_-]{15,})", url)
        if not m_id:
            return None
        file_id = m_id.group(1)
        direct_gd = f"https://drive.google.com/uc?export=download&id={file_id}"
        mirror_gd = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"

        combined_meta = f"{raw_title} {snippet}".lower()
        fmt = "pdf"
        if ".epub" in combined_meta or "epub" in combined_meta:
            fmt = "epub"
        elif ".docx" in combined_meta or "docx" in combined_meta:
            fmt = "docx"

        clean_t = re.sub(r"\s*-\s*Google\s*Drive.*$", "", raw_title, flags=re.I).strip()
        clean_t = re.sub(r"\.(pdf|epub|docx)$", "", clean_t, flags=re.I).strip() or raw_title
        year, edition = _extract_year_and_edition(f"{raw_title} {snippet}", url)
        if not edition:
            edition = "Bản chia sẻ Google Drive"

        return BookSearchResult(
            id=f"gdrive_{file_id[:16]}",
            title=clean_t,
            author=_extract_author_hint(raw_title, snippet, host="Google Drive"),
            format=fmt,
            size="",
            language="",
            year=year,
            edition=edition,
            source="Google Drive Leak",
            download_url=direct_gd,
            mirror_urls=[mirror_gd, url],
        ).ensure_score(query)

    # 3. Xử lý Archive.org direct download link (/download/{id}/{file})
    if "archive.org/download/" in u_lower:
        m_ext = re.search(r"\.(epub|pdf|docx|txt|mobi)(?:$|\?)", path_lower)
        if m_ext and "_lcp." not in path_lower:
            fmt = m_ext.group(1)
            nice_title = _prettify_filename_title(url, fallback=raw_title)
            year, edition = _extract_year_and_edition(f"{raw_title} {snippet}", url)
            return BookSearchResult(
                id=_make_id("ia_dl", url),
                title=nice_title,
                author=_extract_author_hint(raw_title, snippet, host="Archive.org"),
                format=fmt,
                size="",
                language="",
                year=year,
                edition=edition or "Bản lưu trữ Archive.org",
                source="Internet Archive Direct",
                download_url=url,
                mirror_urls=[],
            ).ensure_score(query)

    # 4. Xử lý link chứa MD5 của Anna's Archive / Welib / LibGen
    m_md5 = re.search(r"/md5/([a-fA-F0-9]{32})", u_lower) or re.search(r"md5=([a-fA-F0-9]{32})", u_lower)
    if m_md5:
        md5 = m_md5.group(1).lower()
        combined_meta = f"{raw_title} {snippet}".lower()
        fmt = "epub" if "epub" in combined_meta else ("pdf" if "pdf" in combined_meta else "epub")
        mirrors = _build_libgen_mirrors(md5, [], is_fiction=False)
        year, edition = _extract_year_and_edition(f"{raw_title} {snippet}", url)
        return BookSearchResult(
            id=f"md5_{md5[:16]}",
            title=re.sub(r"\s*[-|]\s*(Anna's Archive|Welib|LibGen).*$", "", raw_title, flags=re.I).strip() or raw_title,
            author=_extract_author_hint(raw_title, snippet),
            format=fmt,
            size="",
            language="",
            year=year,
            edition=edition or f"MD5 {md5[:8]}",
            source="Shadow Library Mirror",
            download_url=mirrors[0],
            mirror_urls=mirrors[1:],
        ).ensure_score(query)

    # 5. Xử lý mọi link trực tiếp .epub, .pdf, .docx, .txt, .mobi trên toàn mạng (CDN, trường ĐH, tổ chức, blog...)
    m_direct_ext = re.search(r"\.(epub|pdf|docx|txt|mobi|azw3)(?:$|\?|#)", path_lower)
    if m_direct_ext:
        fmt = m_direct_ext.group(1)
        file_title = _prettify_filename_title(url, fallback=raw_title)
        # Kết hợp raw_title và tên file nếu tên file rõ ràng hơn
        display_title = file_title if len(file_title) >= 8 else (raw_title or file_title)
        display_title = re.sub(r"^\[PDF\]\s*", "", display_title, flags=re.I).strip()
        year, edition = _extract_year_and_edition(f"{raw_title} {file_title} {snippet}", url)
        if not edition:
            edition = f"Direct {fmt.upper()} ({host})"
        return BookSearchResult(
            id=_make_id("web", url),
            title=display_title,
            author=_extract_author_hint(raw_title, snippet, host=host),
            format=fmt,
            size="",
            language="",
            year=year,
            edition=edition,
            source=f"Web Direct ({host})",
            download_url=url,
            mirror_urls=[],
        ).ensure_score(query)

    return None


def _extract_author_hint(title: str, snippet: str, host: str = "") -> str:
    combined = f"{title} | {snippet}"
    m_by = re.search(r"\bby\s+([A-Z][a-zA-Z.\s&-]{2,35}?)(?:\s*[|,\-–(]|$|\s+epub|\s+pdf)", combined)
    if m_by:
        cand = m_by.group(1).strip()
        if len(cand) >= 3 and cand.lower() not in ("the", "pdf", "epub", "free", "download"):
            return cand
    return host or "Unknown"


async def search_web_leaks_and_editions(
    query: str,
    client: httpx.AsyncClient,
    timeout: float = 9.0,
) -> list[BookSearchResult]:
    """
    Săn lùng mọi phiên bản sách (kể cả sách bản quyền, sách mới, tài liệu chuyên ngành PQA-R,
    sách tái bản, bản dịch leak trên GitHub / Google Drive / Archive.org / Direct PDF & EPUB).
    """
    results: list[BookSearchResult] = []
    expanded_queries = await _expand_bilingual_queries(query, client)
    primary_q = expanded_queries[0]

    # Khởi tạo cookie phiên cho Yahoo Search
    try:
        await client.get("https://vn.search.yahoo.com/", timeout=3.5)
    except Exception:
        pass

    dorks: list[str] = [
        f"{primary_q} epub",
        f"{primary_q} pdf",
        f"{primary_q} github.com OR archive.org OR drive.google.com",
    ]
    # Nếu có biến thể truy vấn thứ 2 (tên tiếng Anh mở rộng từ tiếng Việt), thêm dork bổ sung
    if len(expanded_queries) > 1 and expanded_queries[1].lower() not in primary_q.lower():
        second_q = expanded_queries[1]
        dorks.append(f"{second_q} epub")
        dorks.append(f"{second_q} pdf")

    yahoo_sem = asyncio.Semaphore(2)

    async def _bounded_yahoo(d_str: str) -> list[tuple[str, str, str]]:
        async with yahoo_sem:
            res_hits = await _run_yahoo_dork(client, d_str)
            await asyncio.sleep(0.15)
            return res_hits

    tasks = [_bounded_yahoo(d) for d in dorks]
    tasks.append(_run_ddg_lite_dork(client, f"{primary_q} pdf OR epub"))

    all_hit_lists = await asyncio.gather(*tasks, return_exceptions=True)

    raw_hits: list[tuple[str, str, str]] = []
    seen_urls: set[str] = set()
    archive_detail_ids: list[str] = []

    for hit_list in all_hit_lists:
        if not isinstance(hit_list, list):
            continue
        for title, url, snippet in hit_list:
            if url in seen_urls:
                continue
            seen_urls.add(url)

            # Nếu là trang chi tiết Archive.org (/details/{identifier}), gom lại để lấy danh sách file thật
            m_ia_detail = re.search(r"archive\.org/details/([a-zA-Z0-9_.\-]+)", url)
            if m_ia_detail:
                ia_id = m_ia_detail.group(1)
                if ia_id not in ("", "details", "search") and ia_id not in archive_detail_ids:
                    archive_detail_ids.append(ia_id)
                continue

            item = _classify_web_hit_to_result(title, url, snippet, query)
            if item:
                # Re-score với cả từ khóa gốc và từ khóa mở rộng để lấy điểm cao nhất
                best_score = item.priority_score
                for eq in expanded_queries:
                    item.ensure_score(eq)
                    if item.priority_score > best_score:
                        best_score = item.priority_score
                item.priority_score = best_score
                results.append(item)

    # Giải mã song song các trang Archive.org /details/{id} tìm được từ Yahoo/Google
    if archive_detail_ids:
        ia_tasks = [
            _resolve_ia_item_files({"identifier": ia_id}, primary_q, client)
            for ia_id in archive_detail_ids[:6]
        ]
        ia_resolved = await asyncio.gather(*ia_tasks, return_exceptions=True)
        for r_list in ia_resolved:
            if isinstance(r_list, list):
                results.extend(r_list)

    return results


# ============================================================================
# NGUỒN 2: GUTENDEX API (PROJECT GUTENBERG)
# ============================================================================
async def search_gutendex(
    query: str,
    client: httpx.AsyncClient,
    timeout: float = 7.5,
) -> list[BookSearchResult]:
    results: list[BookSearchResult] = []
    clean_q = re.sub(r"\s+-\s+", " ", query).strip()
    url = f"https://gutendex.com/books/?search={quote_plus(clean_q)}"
    try:
        resp = await client.get(
            url,
            headers={"Accept": "application/json"},
            timeout=timeout,
            follow_redirects=True,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.debug("Gutendex search error for %r: %s", query, exc)
        return results

    for item in data.get("results", [])[:10]:
        book_id = item.get("id")
        title = _clean_text(item.get("title") or "Untitled")
        authors_list = item.get("authors") or []
        author = ", ".join(
            _clean_text(a.get("name", "")) for a in authors_list if a.get("name")
        ) or "Unknown"
        languages = item.get("languages") or []
        language = languages[0] if languages else "en"
        formats: dict[str, str] = item.get("formats") or {}
        cover_url = formats.get("image/jpeg")

        epub_urls: list[str] = []
        txt_urls: list[str] = []
        for mime, link in formats.items():
            if not link or link.endswith(".zip"):
                continue
            mime_lower = mime.lower()
            if "application/epub+zip" in mime_lower or ".epub" in link.lower():
                epub_urls.append(link)
            elif "text/plain" in mime_lower or link.lower().endswith(".txt"):
                txt_urls.append(link)

        if book_id and not epub_urls:
            epub_urls.append(f"https://www.gutenberg.org/ebooks/{book_id}.epub3.images")
            epub_urls.append(f"https://www.gutenberg.org/ebooks/{book_id}.epub.noimages")

        if epub_urls:
            primary_epub = epub_urls[0]
            mirrors = epub_urls[1:]
            if book_id:
                for fallback in (
                    f"https://www.gutenberg.org/ebooks/{book_id}.epub3.images",
                    f"https://www.gutenberg.org/ebooks/{book_id}.epub.images",
                    f"https://www.gutenberg.org/ebooks/{book_id}.epub.noimages",
                ):
                    if fallback != primary_epub and fallback not in mirrors:
                        mirrors.append(fallback)
            res = BookSearchResult(
                id=f"gutenberg_epub_{book_id or _make_id('gt', title)}",
                title=title,
                author=author,
                format="epub",
                size="",
                language=language,
                edition="Public Domain Standard",
                source="Project Gutenberg",
                download_url=primary_epub,
                mirror_urls=mirrors,
                cover_url=cover_url,
            ).ensure_score(query)
            results.append(res)

        if txt_urls:
            primary_txt = txt_urls[0]
            mirrors_txt = txt_urls[1:]
            res_txt = BookSearchResult(
                id=f"gutenberg_txt_{book_id or _make_id('gt', title)}",
                title=title,
                author=author,
                format="txt",
                size="",
                language=language,
                edition="Plain Text UTF-8",
                source="Project Gutenberg",
                download_url=primary_txt,
                mirror_urls=mirrors_txt,
                cover_url=cover_url,
            ).ensure_score(query)
            results.append(res_txt)

    return results


# ============================================================================
# NGUỒN 3 & 4: LIBGEN & ANNA'S ARCHIVE
# ============================================================================
def _extract_md5_from_links(links: list[str]) -> Optional[str]:
    for href in links:
        m = re.search(r"([a-fA-F0-9]{32})", href)
        if m:
            return m.group(1).lower()
    return None


def _build_libgen_mirrors(md5: Optional[str], existing_links: list[str], is_fiction: bool = False) -> list[str]:
    mirrors: list[str] = []
    for link in existing_links:
        if link and link not in mirrors:
            mirrors.append(link)
    if md5:
        candidates = (
            [
                f"http://library.lol/fiction/{md5}",
                f"https://library.lol/fiction/{md5}",
                f"http://library.lol/main/{md5}",
                f"https://libgen.li/ads.php?md5={md5}",
                f"http://libgen.lc/ads.php?md5={md5}",
                f"https://annas-archive.org/md5/{md5}",
            ]
            if is_fiction
            else [
                f"http://library.lol/main/{md5}",
                f"https://library.lol/main/{md5}",
                f"https://libgen.li/ads.php?md5={md5}",
                f"http://libgen.lc/ads.php?md5={md5}",
                f"http://library.lol/fiction/{md5}",
                f"https://annas-archive.org/md5/{md5}",
            ]
        )
        for c in candidates:
            if c not in mirrors:
                mirrors.append(c)
    return mirrors


def _parse_libgen_scitech_html(html_text: str, base_domain: str, query: str) -> list[BookSearchResult]:
    results: list[BookSearchResult] = []
    soup = BeautifulSoup(html_text, "html.parser")
    table = soup.find("table", class_="c")
    if not table:
        for t in soup.find_all("table"):
            if len(t.find_all("tr")) > 2 and ("Author" in t.text or "Extension" in t.text):
                table = t
                break
    if not table:
        return results

    for row in table.find_all("tr")[1:]:
        cols = row.find_all("td")
        if len(cols) < 8:
            continue
        row_links = [
            urljoin(base_domain, a.get("href", ""))
            for a in row.find_all("a", href=True)
            if a.get("href")
        ]
        md5 = _extract_md5_from_links(row_links)
        ext_candidate_8 = _clean_text(cols[8].get_text()).lower() if len(cols) > 8 else ""
        year_val = ""
        if ext_candidate_8 in SUPPORTED_FORMATS:
            author = _clean_text(cols[1].get_text()) or "Unknown"
            title_col = cols[2]
            for font_tag in title_col.find_all("font"):
                font_tag.decompose()
            title = _clean_text(title_col.get_text())
            year_val = _clean_text(cols[4].get_text()) if len(cols) > 4 else ""
            language = _clean_text(cols[6].get_text())
            size = _clean_text(cols[7].get_text())
            fmt = ext_candidate_8
        else:
            continue

        if not title:
            continue
        mirror_links = [
            lnk for lnk in row_links
            if any(k in lnk.lower() for k in ("library.lol", "libgen.lc", "libgen.li/ads", "ads.php", "get.php", "md5="))
        ]
        all_mirrors = _build_libgen_mirrors(md5, mirror_links, is_fiction=False)
        if not all_mirrors:
            continue

        _, edition = _extract_year_and_edition(title, "")
        item_id = f"libgen_{md5}" if md5 else _make_id("libgen", f"{title}_{author}_{fmt}")
        results.append(
            BookSearchResult(
                id=item_id,
                title=title,
                author=author,
                format=fmt,
                size=size,
                language=language,
                year=year_val,
                edition=edition or (f"Xuất bản {year_val}" if year_val else "LibGen Sci-Tech"),
                source="LibGen",
                download_url=all_mirrors[0],
                mirror_urls=all_mirrors[1:],
            ).ensure_score(query)
        )
    return results


async def search_libgen(
    query: str,
    client: httpx.AsyncClient,
    timeout: float = 5.5,
) -> list[BookSearchResult]:
    q_enc = quote_plus(query)
    for domain in LIBGEN_DOMAINS[:2]:
        search_url = f"{domain}/search.php?req={q_enc}&res=25&view=simple&column=def"
        try:
            resp = await client.get(search_url, timeout=timeout / 1.5, follow_redirects=True)
            if resp.status_code == 200 and len(resp.text) > 500:
                parsed = _parse_libgen_scitech_html(resp.text, domain, query)
                if parsed:
                    return parsed
        except Exception:
            continue
    return []


def _parse_annas_archive_html(html_text: str, base_domain: str, query: str) -> list[BookSearchResult]:
    results: list[BookSearchResult] = []
    uncommented_html = re.sub(
        r"<!--\s*(.*?/md5/[a-fA-F0-9]{32}.*?)\s*-->",
        r"\1",
        html_text,
        flags=re.DOTALL,
    )
    soup = BeautifulSoup(uncommented_html, "html.parser")
    seen_md5: set[str] = set()
    md5_anchors = soup.find_all("a", href=re.compile(r"^/md5/[a-fA-F0-9]{32}", re.I))

    for a_tag in md5_anchors:
        href = a_tag.get("href", "")
        m_md5 = re.search(r"/md5/([a-fA-F0-9]{32})", href, re.I)
        if not m_md5:
            continue
        md5 = m_md5.group(1).lower()
        if md5 in seen_md5:
            continue

        container = a_tag
        if not _clean_text(a_tag.get_text()):
            parent = a_tag.find_parent("div")
            if parent:
                container = parent

        title_el = (
            container.find("h3")
            or container.find(attrs={"class": re.compile(r"font-bold|text-lg|text-xl")})
        )
        title = _clean_text(title_el.get_text()) if title_el else ""
        author_el = container.find(attrs={"class": re.compile(r"italic|text-sm")})
        author = _clean_text(author_el.get_text()) if author_el else "Unknown"
        full_text = _clean_text(container.get_text(" | "))
        if not title:
            segments = [s.strip() for s in full_text.split("|") if s.strip()]
            if not segments:
                continue
            title = segments[0]

        m_fmt = re.search(r"\b(epub|docx|pdf|txt|md|mobi|azw3)\b", full_text.lower())
        fmt = m_fmt.group(1) if m_fmt else "epub"
        m_size = re.search(r"(\d+(?:[.,]\d+)?\s*(?:MB|KB|GB))", full_text, re.I)
        size = m_size.group(1) if m_size else ""
        m_lang = re.search(r"\[([a-z]{2,3})\]", full_text.lower())
        language = m_lang.group(1) if m_lang else ""
        year, edition = _extract_year_and_edition(full_text, "")

        img_tag = container.find("img")
        cover_url = img_tag.get("src") if img_tag and img_tag.get("src") else None

        seen_md5.add(md5)
        primary_url = f"{base_domain}/md5/{md5}"
        mirrors = _build_libgen_mirrors(md5, [primary_url], is_fiction=False)

        results.append(
            BookSearchResult(
                id=f"annas_{md5}",
                title=title,
                author=author or "Unknown",
                format=fmt,
                size=size,
                language=language,
                year=year,
                edition=edition or "Anna's Archive",
                source="Anna's Archive",
                download_url=primary_url,
                mirror_urls=[m for m in mirrors if m != primary_url],
                cover_url=cover_url,
            ).ensure_score(query)
        )
    return results


async def search_annas_archive(
    query: str,
    client: httpx.AsyncClient,
    timeout: float = 5.5,
) -> list[BookSearchResult]:
    q_enc = quote_plus(query)
    for domain in ANNAS_ARCHIVE_DOMAINS[:2]:
        url = f"{domain}/search?q={q_enc}"
        try:
            resp = await client.get(url, timeout=timeout / 1.5, follow_redirects=True)
            if resp.status_code == 200 and "/md5/" in resp.text:
                parsed = _parse_annas_archive_html(resp.text, domain, query)
                if parsed:
                    return parsed
        except Exception:
            continue
    return []


# ============================================================================
# NGUỒN 5: INTERNET ARCHIVE & OPENLIBRARY MULTI-EDITION RESOLVER
# ============================================================================
async def _resolve_ia_item_files(
    doc: dict,
    query: str,
    client: httpx.AsyncClient,
) -> list[BookSearchResult]:
    identifier = doc.get("identifier")
    if not identifier:
        return []

    meta_url = f"https://archive.org/metadata/{identifier}"
    try:
        resp = await client.get(
            meta_url,
            headers={"Accept": "application/json"},
            timeout=6.5,
            follow_redirects=True,
        )
        resp.raise_for_status()
        meta_json = resp.json()
        files_list = meta_json.get("files") or []
        item_meta = meta_json.get("metadata") or {}
    except Exception:
        return []

    raw_title = item_meta.get("title") or doc.get("title") or identifier
    if isinstance(raw_title, list):
        raw_title = raw_title[0] if raw_title else identifier
    title = _clean_text(str(raw_title))
    if title == identifier:
        title = _prettify_filename_title(identifier, fallback=identifier)

    creator = doc.get("creator") or item_meta.get("creator")
    if isinstance(creator, list):
        author = ", ".join(str(c) for c in creator[:2])
    elif isinstance(creator, str):
        author = creator
    else:
        author = "Unknown"

    lang_raw = doc.get("language") or item_meta.get("language")
    if isinstance(lang_raw, list):
        language = str(lang_raw[0]) if lang_raw else ""
    else:
        language = str(lang_raw or "")

    year_raw = str(doc.get("year") or item_meta.get("date") or item_meta.get("year") or "")
    m_yr = re.search(r"\b(19\d\d|20\d\d)\b", year_raw)
    year = m_yr.group(1) if m_yr else ""

    cover_url = f"https://archive.org/services/img/{identifier}"

    # Lấy các file hợp lệ trong item và ưu tiên những file có tên khớp với từ khóa tìm kiếm
    found_files: list[tuple[int, str, str, str, str]] = []  # (match_score, fmt, dl_link, size_str, fname)
    seen_fmt_names: set[str] = set()
    q_words = [w for w in re.sub(r"[^\w\s]", " ", query.lower()).split() if len(w) > 1]

    for f in files_list:
        # Bỏ qua các file bị khóa mượn (Controlled Digital Lending / private=true -> HTTP 401/403)
        if str(f.get("private", "")).lower() == "true":
            continue
        fname = f.get("name") or ""
        fname_lower = fname.lower()
        if (
            "_lcp." in fname_lower
            or "_encrypted" in fname_lower
            or "encrypted" in str(f.get("format", "")).lower()
            or fname_lower.endswith(("_bw.pdf", "_text.pdf"))
        ):
            continue

        fmt = ""
        if fname_lower.endswith(".epub"):
            fmt = "epub"
        elif fname_lower.endswith(".docx"):
            fmt = "docx"
        elif fname_lower.endswith(".pdf"):
            fmt = "pdf"
        elif fname_lower.endswith("_djvu.txt") or (fname_lower.endswith(".txt") and "meta" not in fname_lower):
            fmt = "txt"

        if not fmt:
            continue

        fmt_key = f"{fmt}:{fname_lower[:32]}"
        if fmt_key in seen_fmt_names:
            continue
        seen_fmt_names.add(fmt_key)

        size_bytes = f.get("size")
        size_str = ""
        if size_bytes and str(size_bytes).isdigit():
            mb = int(size_bytes) / (1024 * 1024)
            size_str = f"{mb:.1f} MB" if mb >= 1 else f"{max(1, int(size_bytes) // 1024)} KB"

        # Tính điểm khớp của riêng tên file với từ khóa (để nếu kho chứa 20 sách thì lấy đúng cuốn người dùng tìm)
        fname_clean = re.sub(r"[^\w\s]", " ", fname_lower)
        f_score = sum(10 for w in q_words if w in fname_clean)
        if fmt == "epub":
            f_score += 5
        elif fmt == "docx":
            f_score += 4
        elif fmt == "pdf":
            f_score += 3

        dl_link = f"https://archive.org/download/{identifier}/{quote(fname)}"
        found_files.append((f_score, fmt, dl_link, size_str, fname))

    found_files.sort(key=lambda x: x[0], reverse=True)
    is_collection = len(found_files) > 4

    out: list[BookSearchResult] = []
    top_files = found_files[:4]
    all_links = [item[2] for item in top_files]

    for _, fmt, primary_link, sz, fname in top_files:
        mirrors = [] if is_collection else [u for u in all_links if u != primary_link]
        nice_fname = _prettify_filename_title(fname, fallback=title)
        display_title = nice_fname if (is_collection and len(nice_fname) > 8) else (title if len(title) > 5 else nice_fname)
        f_year, f_ed = _extract_year_and_edition(f"{display_title} {fname}", primary_link)
        final_year = f_year or year
        edition_label = f_ed or (f"Ấn bản {final_year}" if final_year else f"Archive ({nice_fname[:28]})")
        out.append(
            BookSearchResult(
                id=_make_id(f"ia_{fmt}", primary_link),
                title=display_title,
                author=author,
                format=fmt,
                size=sz,
                language=language,
                year=final_year,
                edition=edition_label,
                source="Internet Archive",
                download_url=primary_link,
                mirror_urls=mirrors,
                cover_url=cover_url,
            ).ensure_score(query)
        )
    return out


async def search_openlibrary(
    query: str,
    client: httpx.AsyncClient,
    timeout: float = 8.5,
) -> list[BookSearchResult]:
    """
    Quét sâu cả Internet Archive Advanced Search (theo tiêu đề + toàn văn)
    và OpenLibrary Search API để lấy trọn bộ các lần tái bản / ấn bản.
    """
    results: list[BookSearchResult] = []
    expanded = await _expand_bilingual_queries(query, client)

    # Làm sạch ký tự đặc biệt tránh lỗi cú pháp Lucene của Archive.org
    def _clean_lucene(q_str: str) -> str:
        return re.sub(r'[-+:!(){}\[\]^"~*?:\\/]', " ", q_str).strip()

    ia_queries: list[str] = []
    for eq in expanded[:2]:
        cl = _clean_lucene(eq)
        if cl:
            ia_queries.append(
                f'title:("{cl}") AND mediatype:(texts) AND -collection:(inlibrary) AND -collection:(printdisabled)'
            )
            ia_queries.append(
                f'({cl}) AND mediatype:(texts) AND -collection:(inlibrary) AND -collection:(printdisabled) '
                f'AND (format:(EPUB) OR format:(Text PDF) OR format:(PDF))'
            )

    async def _fetch_ia_docs(ia_q: str) -> list[dict]:
        url = (
            f"https://archive.org/advancedsearch.php?q={quote_plus(ia_q)}"
            f"&fl[]=identifier,title,creator,format,language,year"
            f"&sort[]=downloads+desc&rows=10&output=json"
        )
        try:
            r = await client.get(url, headers={"Accept": "application/json"}, timeout=6.0, follow_redirects=True)
            if r.status_code == 200:
                return (r.json().get("response") or {}).get("docs") or []
        except Exception:
            pass
        return []

    async def _fetch_openlibrary_ia_docs(q_str: str) -> list[dict]:
        url = (
            f"https://openlibrary.org/search.json?q={quote_plus(q_str)}"
            f"&fields=key,title,author_name,first_publish_year,edition_count,ia,cover_i&limit=8"
        )
        docs_out: list[dict] = []
        try:
            r = await client.get(url, timeout=6.0, follow_redirects=True)
            if r.status_code == 200:
                for d in r.json().get("docs") or []:
                    ia_list = d.get("ia") or []
                    for ia_id in ia_list[:3]:
                        docs_out.append(
                            {
                                "identifier": ia_id,
                                "title": d.get("title"),
                                "creator": d.get("author_name"),
                                "year": d.get("first_publish_year"),
                            }
                        )
        except Exception:
            pass
        return docs_out

    search_tasks = [_fetch_ia_docs(iq) for iq in ia_queries]
    search_tasks.append(_fetch_openlibrary_ia_docs(expanded[0]))
    doc_batches = await asyncio.gather(*search_tasks, return_exceptions=True)

    unique_docs: list[dict] = []
    seen_ids: set[str] = set()
    for batch in doc_batches:
        if not isinstance(batch, list):
            continue
        for d in batch:
            ident = d.get("identifier")
            if ident and ident not in seen_ids:
                seen_ids.add(ident)
                unique_docs.append(d)

    resolve_tasks = [_resolve_ia_item_files(doc, expanded[0], client) for doc in unique_docs[:12]]
    resolved_lists = await asyncio.gather(*resolve_tasks, return_exceptions=True)
    for item_list in resolved_lists:
        if isinstance(item_list, list):
            results.extend(item_list)

    return results


def _deduplicate_results(results: list[BookSearchResult]) -> list[BookSearchResult]:
    """
    Khử trùng lặp thông minh:
    - Chỉ gộp khi trùng chính xác download_url hoặc trùng MD5 + format.
    - GIỮ NGUYÊN các phiên bản khác nhau, năm tái bản khác nhau, nguồn leak khác nhau
      (GitHub, Google Drive, Archive.org, Direct PDF/EPUB) để người dùng tự do lựa chọn!
    """
    deduped: dict[str, BookSearchResult] = {}

    for item in results:
        norm_url = item.download_url.strip().rstrip("/")
        m_md5 = re.search(r"([a-fA-F0-9]{32})", f"{item.id} {norm_url}")
        if m_md5:
            key = f"md5:{m_md5.group(1).lower()}:{item.format}"
        else:
            key = f"url:{norm_url.lower()}"

        if key not in deduped:
            deduped[key] = item
        else:
            existing = deduped[key]
            combined_urls = [existing.download_url] + existing.mirror_urls + [item.download_url] + item.mirror_urls
            unique_urls: list[str] = []
            for u in combined_urls:
                if u and u not in unique_urls:
                    unique_urls.append(u)
            if item.priority_score > existing.priority_score:
                item.download_url = unique_urls[0]
                item.mirror_urls = unique_urls[1:]
                if not item.cover_url and existing.cover_url:
                    item.cover_url = existing.cover_url
                deduped[key] = item
            else:
                existing.download_url = unique_urls[0]
                existing.mirror_urls = unique_urls[1:]
                if not existing.cover_url and item.cover_url:
                    existing.cover_url = item.cover_url

    return list(deduped.values())


async def search_books(query: str, limit: int = 35) -> list[BookSearchResult]:
    """
    Hàm chính của Agent 1 (Scout):
    Quét đồng thời 5 kênh:
    1. Web Search & Leak Hunter (GitHub Raw, Google Drive, Direct PDF/EPUB, CDN, शैक्षणिक/Manuals)
    2. Internet Archive & OpenLibrary Multi-Edition Search
    3. Project Gutenberg (Gutendex)
    4. LibGen (Sci-Tech & Fiction)
    5. Anna's Archive
    """
    query = (query or "").strip()
    if not query:
        return []

    timeout_cfg = httpx.Timeout(10.0, connect=4.5)
    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=timeout_cfg, verify=False, follow_redirects=True) as client:
        try:
            expanded = await asyncio.wait_for(_expand_bilingual_queries(query, client), timeout=5.0)
        except Exception as exc:
            logger.debug("Query expansion failed: %s", exc)
            expanded = []
        if not expanded:
            expanded = [query]
        primary_or_en = expanded[-1]  # Nếu câu gốc là tiếng Việt thì expanded[-1] là tên tiếng Anh

        provider_coroutines = [
            asyncio.wait_for(search_web_leaks_and_editions(query, client, timeout=9.5), timeout=10.5),
            asyncio.wait_for(search_openlibrary(query, client, timeout=9.0), timeout=10.0),
            asyncio.wait_for(search_gutendex(primary_or_en, client, timeout=7.5), timeout=8.5),
            asyncio.wait_for(search_libgen(primary_or_en, client, timeout=5.5), timeout=6.5),
            asyncio.wait_for(search_annas_archive(primary_or_en, client, timeout=5.5), timeout=6.5),
        ]
        raw_lists = await asyncio.gather(*provider_coroutines, return_exceptions=True)

    all_results: list[BookSearchResult] = []
    for res in raw_lists:
        if isinstance(res, list):
            all_results.extend(res)
        elif isinstance(res, Exception):
            logger.debug("Provider exception during search_books: %s", res)

    scorable: list[BookSearchResult] = []
    for r in all_results:
        if not isinstance(r, BookSearchResult) or not r.download_url:
            continue
        try:
            r.ensure_score(list(expanded) + [query])
            scorable.append(r)
        except Exception as exc:  # một kết quả lỗi không được làm hỏng cả lượt tìm
            logger.debug("Scoring failed for %r: %s", getattr(r, "title", "?"), exc)
    all_results = scorable

    unique_results = _deduplicate_results(all_results)
    unique_results.sort(
        key=lambda x: (
            x.priority_score,
            1 if "Leak" in x.source or "Direct" in x.source or "Archive" in x.source else 0,
            len(x.mirror_urls),
        ),
        reverse=True,
    )
    # Giới hạn hợp lý: tối đa 15 kết quả/nguồn để một nguồn ồn ào không chiếm hết danh sách
    per_source: dict[str, int] = {}
    capped: list[BookSearchResult] = []
    for r in unique_results:
        n = per_source.get(r.source, 0)
        if n >= 15:
            continue
        per_source[r.source] = n + 1
        capped.append(r)
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 35
    return capped[:limit]
