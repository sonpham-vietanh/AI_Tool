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

    def ensure_score(self, query: str = "") -> "BookSearchResult":
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
