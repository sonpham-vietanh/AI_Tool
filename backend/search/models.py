from __future__ import annotations

import re
from typing import Optional
from pydantic import BaseModel, Field, field_validator


FORMAT_PRIORITY_MAP: dict[str, int] = {
    "epub": 100,
    "docx": 95,
    "pdf": 88,
    "txt": 72,
    "md": 72,
    "mobi": 48,
    "azw3": 48,
}


def compute_priority_score(
    fmt: str,
    title: str = "",
    author: str = "",
    year: str = "",
    edition: str = "",
    query: str = "",
    has_direct_link: bool = False,
    source: str = "",
) -> int:
    """
    Tính điểm ưu tiên cho ấn bản sách:
    - Ưu tiên mức độ khớp tiêu đề + tác giả + phiên bản với từ khóa tìm kiếm
    - Ưu tiên định dạng: epub = 100, docx = 95, pdf = 88, txt/md = 72
    - Cộng điểm cho link tải trực tiếp (GitHub Raw, Direct PDF/EPUB, Archive.org, Google Drive)
    """
    norm_fmt = (fmt or "").strip().lower().lstrip(".")
    base = FORMAT_PRIORITY_MAP.get(norm_fmt, 40)

    bonus = 0
    if query and title:
        q_clean = re.sub(r"[^\w\s]", " ", query.lower()).strip()
        t_clean = re.sub(r"[^\w\s]", " ", title.lower()).strip()
        full_haystack = re.sub(r"[^\w\s]", " ", f"{title} {author} {edition} {year}".lower()).strip()

        if q_clean and t_clean:
            if q_clean == t_clean or t_clean.startswith(q_clean + " "):
                bonus += 46
            elif q_clean in t_clean:
                bonus += 40
            elif q_clean in full_haystack:
                bonus += 36
            else:
                stop_words = {"the", "and", "for", "with", "from", "book", "pdf", "epub", "download", "edition", "manual"}
                q_words = [w for w in q_clean.split() if len(w) > 1 and w not in stop_words]
                if not q_words:
                    q_words = [w for w in q_clean.split() if len(w) > 1]
                if q_words:
                    matched_title = sum(1 for w in q_words if w in t_clean)
                    matched_full = sum(1 for w in q_words if w in full_haystack)
                    ratio_title = matched_title / len(q_words)
                    ratio_full = matched_full / len(q_words)
                    if ratio_title >= 0.85:
                        bonus += 30
                    elif ratio_full >= 0.85:
                        bonus += 26
                    elif ratio_full >= 0.5:
                        bonus += 12
                    else:
                        bonus -= 35

    if year and year.isdigit():
        yr_int = int(year)
        if yr_int >= 2015:
            bonus += 12
        elif yr_int >= 2005:
            bonus += 6
        elif yr_int < 1999:
            bonus -= 4

    if has_direct_link:
        bonus += 8

    src_lower = (source or "").lower()
    if any(k in src_lower for k in ("github", "direct", "archive", "google drive", "gutenberg")):
        bonus += 5

    return base + bonus


_STOP_WORDS = frozenset(
    "the a an and or of for with from in on to by book books pdf epub docx txt download free ebook "
    "edition full text online read version".split()
)

_JUNK_EDITION_RE = re.compile(
    r"\b(summary|summaries|summarized|study[\s_-]*guide|workbook|work[\s_-]*book|cliff'?s?[\s_-]*notes|"
    r"spark[\s_-]*notes|instaread|bookrags|key[\s_-]*takeaways|analysis of|companion|"
    r"quiz|trivia|lesson[\s_-]*plans?|conversation[\s_-]*starters|book[\s_-]*review|"
    r"cheat[\s_-]*sheet|flash[\s_-]*cards?|reading[\s_-]*guide|teacher'?s?[\s_-]*guide|"
    r"t[oó]m t[aắ]t)\b",
    re.I,
)
_MEDIA_RE = re.compile(r"\b(audio[\s_-]*books?|audio|mp3|m4b|m4a|video|youtube|podcast|cd[\s_-]*rip)\b", re.I)
_SCAN_RE = re.compile(r"\b(scanned|scan|ocr|djvu|low[\s_-]*quality|photocopy|bad[\s_-]*scan)\b", re.I)
_SAMPLE_RE = re.compile(
    r"\b(sample|excerpt|preview|free[\s_-]*chapter|first[\s_-]*chapter|chapter[\s_-]*1 only|teaser|"
    r"look[\s_-]*inside|extract)\b",
    re.I,
)
_VI_DIACRITICS_RE = re.compile(
    r"[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]", re.I
)
_VI_WORDS_RE = re.compile(r"\b(ti[eế]ng vi[eệ]t|vietnamese|b[aả]n d[iị]ch|vi[eệ]t h[oó]a|vietsub)\b", re.I)

# Mã ngôn ngữ đích -> tập tên/ISO có thể xuất hiện trong trường language của kết quả
_LANG_ALIASES: dict[str, set[str]] = {
    "vi": {"vi", "vie", "vietnamese", "tiếng việt", "tieng viet"},
    "en": {"en", "eng", "english"},
    "zh": {"zh", "zho", "chi", "chinese"},
    "ja": {"ja", "jpn", "japanese"},
    "ko": {"ko", "kor", "korean"},
    "fr": {"fr", "fra", "fre", "french"},
    "de": {"de", "deu", "ger", "german"},
    "es": {"es", "spa", "spanish"},
}
_ENGLISH_LANGS = _LANG_ALIASES["en"]


def _fold(text: str) -> str:
    import unicodedata

    t = unicodedata.normalize("NFD", (text or "").lower().replace("đ", "d"))
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _tokens(text: str) -> list[str]:
    toks = [w for w in _fold(text).split() if (len(w) > 1 or w.isdigit()) and w not in _STOP_WORDS]
    return toks


def parse_size_bytes(size: str) -> Optional[int]:
    """'2.3 MB' / '850 KB' / '1,2MiB' -> bytes; None nếu không đọc được."""
    m = re.search(r"([\d.,]+)\s*(kb|kib|mb|mib|gb|gib|b)\b", (size or "").lower())
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    mult = {"b": 1, "kb": 1024, "kib": 1024, "mb": 1024**2, "mib": 1024**2, "gb": 1024**3, "gib": 1024**3}
    return int(num * mult[m.group(2)])


def _is_target_language(result, target_lang: str) -> bool:
    lang = (getattr(result, "language", "") or "").strip().lower()
    aliases = _LANG_ALIASES.get(target_lang, {target_lang})
    if lang and (lang in aliases or any(lang.startswith(a + "-") for a in aliases)):
        return True
    blob = f"{result.title} {result.edition} {result.download_url}"
    if target_lang == "vi":
        if _VI_DIACRITICS_RE.search(result.title or "") or _VI_WORDS_RE.search(blob):
            return True
    return False


def score_candidate(
    query,
    result,
    target_lang: str = "vi",
) -> tuple[int, list[str]]:
    """
    Hàm thuần: chấm điểm 1 ứng viên cho việc DỊCH sách. Trả về (score, reasons).
    `query` là str hoặc list[str] (các biến thể truy vấn, lấy biến thể khớp nhất).
    Ưu tiên: bản gốc (thường tiếng Anh), định dạng text sạch (EPUB > DOCX > PDF > TXT),
    khớp tên sách/tác giả, bản đầy đủ mới nhất. Phạt: bản đã ở ngôn ngữ đích, tóm tắt/study guide,
    audio/video, scan/OCR, sample/excerpt, file quá nhỏ, sai sách.
    """
    queries = [query] if isinstance(query, str) else [q for q in (query or []) if q]
    fmt = (result.format or "").strip().lower().lstrip(".")
    score = FORMAT_PRIORITY_MAP.get(fmt, 30)
    reasons: list[str] = [f"format:{fmt or '?'}={score}"]

    def add(delta: int, why: str) -> None:
        nonlocal score
        score += delta
        reasons.append(f"{why}:{delta:+d}")

    title = result.title or ""
    author = result.author or ""
    edition = result.edition or ""
    meta_blob = f"{title} {author} {edition}"
    meta_tokens = set(_tokens(meta_blob))
    title_folded = _fold(title)
    url_blob = _fold(result.download_url or "")

    # ---- 1. Khớp query <-> tiêu đề + tác giả ----
    q_has_vi = any(_VI_DIACRITICS_RE.search(q) for q in queries)
    best_ratio, best_exact = 0.0, False
    for q in queries:
        qt = _tokens(q)
        if not qt:
            continue
        ratio = sum(1 for w in qt if w in meta_tokens) / len(qt)
        qf = _fold(q)
        if qf and (qf == title_folded or f" {qf} " in f" {title_folded} "):
            best_exact = True
            ratio = max(ratio, 1.0)
        best_ratio = max(best_ratio, ratio)
    if queries:
        if best_exact:
            add(46, "title_exact")
        elif best_ratio >= 0.85:
            add(34, "title_match_high")
        elif best_ratio >= 0.6:
            add(14, "title_match_medium")
        elif best_ratio >= 0.34:
            add(-14, "title_match_weak")
        else:
            add(-12 if q_has_vi else -75, "title_mismatch")

    query_text = " ".join(queries).lower()

    # ---- 2. Edition rác: tóm tắt / study guide / workbook ----
    junk_hits = {m.group(0).lower() for m in _JUNK_EDITION_RE.finditer(f"{meta_blob} {result.download_url or ''}")}
    junk_hits = {h for h in junk_hits if h not in query_text}
    if junk_hits:
        add(-90, "summary_or_guide(" + ",".join(sorted(junk_hits))[:40] + ")")

    if _MEDIA_RE.search(f"{meta_blob} {result.download_url or ''}") and not _MEDIA_RE.search(query_text):
        add(-90, "audio_or_video")
    if fmt in ("mp3", "m4b", "m4a", "mp4", "avi", "mkv", "wav", "djvu", "cbz", "cbr", "zip", "rar"):
        add(-90, "unsupported_format")

    if _SCAN_RE.search(meta_blob) and not _SCAN_RE.search(query_text):
        add(-22, "scan_ocr_hint")
    if _SAMPLE_RE.search(f"{meta_blob} {url_blob}") and not _SAMPLE_RE.search(query_text):
        add(-55, "sample_or_excerpt")

    # ---- 3. Kích thước ----
    size_b = parse_size_bytes(result.size)
    if size_b is not None:
        if size_b < 50 * 1024:
            add(-45, "tiny_file")
        elif size_b < 150 * 1024 and fmt in ("pdf", "epub", "docx"):
            add(-18, "very_small_file")
        elif size_b > 400 * 1024 * 1024:
            add(-30, "huge_file")

    # ---- 4. Ngôn ngữ: ưu tiên bản gốc, phạt bản đã ở ngôn ngữ đích ----
    target = (target_lang or "vi").lower()
    if _is_target_language(result, target):
        add(-50, f"already_{target}")
    else:
        lang = (getattr(result, "language", "") or "").strip().lower()
        if lang in _ENGLISH_LANGS or any(lang.startswith(a + "-") for a in _ENGLISH_LANGS):
            add(10, "original_english")
        elif lang:
            add(-8, f"other_language:{lang[:12]}")

    # ---- 5. Năm / ấn bản mới nhất ----
    year = (result.year or "").strip()
    if year.isdigit():
        yr = int(year)
        if yr >= 2015:
            add(8, "recent_edition")
        elif yr >= 2005:
            add(4, "recent_edition")
        elif yr < 1999:
            add(-3, "old_edition")
    if re.search(r"\b(\d+)(st|nd|rd|th)\s+ed", f"{edition} {title}", re.I):
        add(2, "numbered_edition")

    # ---- 6. Link / nguồn ----
    dl = (result.download_url or "").lower()
    if any(k in dl for k in (".epub", ".pdf", ".docx", ".txt", "raw.githubusercontent.com", "archive.org/download", "gutenberg.org")):
        add(6, "direct_link")
    src = (result.source or "").lower()
    if any(k in src for k in ("gutenberg", "archive", "github", "direct")):
        add(4, "trusted_source")

    return score, reasons


class BookSearchResult(BaseModel):
    id: str
    title: str
    author: str = "Unknown"
    format: str
    size: str = ""
    language: str = ""
    year: str = ""
    edition: str = ""
    source: str
    download_url: str
    mirror_urls: list[str] = Field(default_factory=list)
    cover_url: Optional[str] = None
    priority_score: int = 0

    @field_validator("format", mode="before")
    @classmethod
    def normalize_format(cls, v: str) -> str:
        if not v:
            return "pdf"
        cleaned = str(v).strip().lower().lstrip(".")
        if "epub" in cleaned:
            return "epub"
        if "docx" in cleaned or "word" in cleaned:
            return "docx"
        if "pdf" in cleaned:
            return "pdf"
        if "txt" in cleaned or "text/plain" in cleaned or cleaned == "text":
            return "txt"
        if "md" in cleaned or "markdown" in cleaned:
            return "md"
        if "mobi" in cleaned:
            return "mobi"
        if "azw3" in cleaned or "azw" in cleaned:
            return "azw3"
        return cleaned

    def ensure_score(self, query: "str | list[str]" = "", target_lang: str = "vi") -> "BookSearchResult":
        """Gán priority_score bằng score_candidate (query có thể là list biến thể)."""
        if query:
            self.priority_score, _ = score_candidate(query, self, target_lang=target_lang)
            return self
        dl_lower = (self.download_url or "").lower()
        has_direct = any(
            ext in dl_lower
            for ext in (
                ".epub",
                ".pdf",
                ".docx",
                ".txt",
                "raw.githubusercontent.com",
                "/raw/",
                "drive.google.com/uc",
                "gutendex",
                "gutenberg.org",
                "archive.org/download",
            )
        )
        self.priority_score = compute_priority_score(
            fmt=self.format,
            title=self.title,
            author=self.author,
            year=self.year,
            edition=self.edition,
            query=query,
            has_direct_link=has_direct,
            source=self.source,
        )
        return self
