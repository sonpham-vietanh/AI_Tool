"""Tra cứu THÔNG TIN sách: nhận diện đúng cuốn, các lần tái bản, bản dịch tiếng Việt và cách kiếm HỢP PHÁP
(mua bản điện tử/in, mượn thư viện, bản mở/public domain).

Không tìm hay trỏ tới bản leak/đăng lậu. Nguồn: Open Library, Google Books (theo quốc gia), Project Gutenberg.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
from typing import Any, Dict, List, Optional
from urllib.parse import quote, quote_plus

import httpx

logger = logging.getLogger(__name__)

OL_SEARCH = "https://openlibrary.org/search.json"
GB_VOLUMES = "https://www.googleapis.com/books/v1/volumes"
GUTENDEX = "https://gutendex.com/books"
HEADERS = {"User-Agent": "BookTranslatorHub/1.0 (book discovery; contact: local)"}

OL_FIELDS = (
    "key,title,author_name,first_publish_year,isbn,language,edition_count,cover_i,ia,ebook_access,has_fulltext"
)

# ISO 639-2 (Open Library) và ISO 639-1 (Google Books) cho ngôn ngữ đích
LANG_OL = {"vi": "vie", "en": "eng", "zh": "chi", "ja": "jpn", "ko": "kor", "fr": "fre", "de": "ger", "es": "spa", "ru": "rus"}


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFD", (text or "").lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn").replace("đ", "d")
    return re.sub(r"[^a-z0-9 ]+", " ", text)


def _tokens(text: str) -> set[str]:
    return {t for t in _fold(text).split() if len(t) > 2}


def title_similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def title_overlap(a: str, b: str) -> float:
    """Độ trùng HAI CHIỀU (chia cho tập từ lớn hơn): 'Pride and Prejudice' KHÔNG khớp 'Pride and Prejudice and Zombies'."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def authors_overlap(a: List[str], b: List[str]) -> bool:
    """Hai danh sách tác giả có chung họ/tên nào không (bắt buộc khi cả hai bên đều có tác giả)."""
    ta = set().union(*[_tokens(x) for x in a]) if a else set()
    tb = set().union(*[_tokens(x) for x in b]) if b else set()
    return bool(ta & tb)


def same_book(title_a: str, authors_a: List[str], title_b: str, authors_b: List[str], threshold: float) -> bool:
    if title_overlap(title_a, title_b) < threshold:
        return False
    if authors_a and authors_b:
        return authors_overlap(authors_a, authors_b)
    return False if (authors_a or authors_b) and threshold < 1.0 else True


def _isbn13(isbns: List[str]) -> Optional[str]:
    for i in isbns or []:
        d = re.sub(r"[^0-9Xx]", "", str(i))
        if len(d) == 13 and d.startswith(("978", "979")):
            return d
    return None


def retail_links(title: str, author: str, isbn: Optional[str], lang: str = "vi") -> List[Dict[str, Any]]:
    """Link tìm kiếm tới các cửa hàng/thư viện (chỉ là URL tìm kiếm, không cào dữ liệu)."""
    term = isbn or f"{title} {author}".strip()
    q = quote_plus(term)
    links = [
        {"kind": "buy_ebook", "label": "Kindle (Amazon)", "url": f"https://www.amazon.com/s?k={q}&i=digital-text"},
        {"kind": "buy_ebook", "label": "Google Play Books", "url": f"https://play.google.com/store/search?q={q}&c=books"},
        {"kind": "buy_ebook", "label": "Kobo", "url": f"https://www.kobo.com/us/en/search?query={q}"},
        {"kind": "buy_print", "label": "Amazon (sách in, gửi quốc tế)", "url": f"https://www.amazon.com/s?k={q}&i=stripbooks"},
        {"kind": "library", "label": "WorldCat (thư viện nào có sách)", "url": f"https://search.worldcat.org/search?q={q}"},
    ]
    if lang == "vi":
        links += [
            {"kind": "buy_print", "label": "Fahasa", "url": f"https://www.fahasa.com/catalogsearch/result/?q={q}"},
            {"kind": "buy_print", "label": "Tiki", "url": f"https://tiki.vn/search?q={q}"},
        ]
    for item in links:
        item["legal"] = True
        item["type"] = "search_link"
    return links


SOURCE_TIMEOUT_S = 8.0


async def _get_json(
    client: httpx.AsyncClient, url: str, params: Dict[str, Any], warnings: Optional[List[str]] = None
) -> Any:
    host = url.split("/")[2]
    try:
        r = await asyncio.wait_for(client.get(url, params=params), timeout=SOURCE_TIMEOUT_S)
        if r.status_code != 200:
            logger.info("discovery: %s -> HTTP %s", url, r.status_code)
            if warnings is not None:
                warnings.append(f"{host}: HTTP {r.status_code}")
            return None
        return r.json()
    except Exception as exc:  # một nguồn lỗi/chậm không làm hỏng cả tra cứu
        logger.info("discovery: %s failed: %s", url, exc)
        if warnings is not None:
            warnings.append(f"{host}: {type(exc).__name__ or 'lỗi'}")
        return None


async def _openlibrary_works(
    client: httpx.AsyncClient, query: str, warnings: List[str], limit: int = 6
) -> List[Dict[str, Any]]:
    data = await _get_json(client, OL_SEARCH, {"q": query, "limit": limit, "fields": OL_FIELDS}, warnings)
    return list((data or {}).get("docs") or [])


async def _openlibrary_translations(
    client: httpx.AsyncClient, work_key: str, lang: str, warnings: List[str]
) -> List[Dict[str, Any]]:
    """Các ấn bản THẬT bằng ngôn ngữ đích của một tác phẩm (lọc theo ngôn ngữ của từng ấn bản)."""
    code = LANG_OL.get(lang)
    if not code or not work_key:
        return []
    params = {
        "type": "/type/edition",
        "works": work_key,
        "languages": f"/languages/{code}",
        "limit": 8,
        "title": "",
        "publishers": "",
        "publish_date": "",
        "isbn_13": "",
        "isbn_10": "",
    }
    data = await _get_json(client, "https://openlibrary.org/query.json", params, warnings)
    out = []
    for e in data if isinstance(data, list) else []:
        isbn = (e.get("isbn_13") or e.get("isbn_10") or [None])[0]
        out.append(
            {
                "title": e.get("title", ""),
                "authors": [],
                "year": (e.get("publish_date") or "")[-4:] or None,
                "publisher": (e.get("publishers") or [""])[0],
                "isbn": isbn,
                "url": f"https://openlibrary.org{e.get('key', '')}",
            }
        )
    return out


async def _google_books(
    client: httpx.AsyncClient, query: str, country: str, lang: str, warnings: List[str], isbn: Optional[str] = None
) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"q": f"isbn:{isbn}" if isbn else query, "maxResults": 8, "printType": "books", "country": country}
    key = os.environ.get("GOOGLE_BOOKS_API_KEY")
    if key:
        params["key"] = key
    data = await _get_json(client, GB_VOLUMES, params, warnings)
    out = []
    for item in (data or {}).get("items") or []:
        v = item.get("volumeInfo") or {}
        sale = item.get("saleInfo") or {}
        acc = item.get("accessInfo") or {}
        ids = [i.get("identifier", "") for i in v.get("industryIdentifiers") or []]
        out.append(
            {
                "title": v.get("title", ""),
                "subtitle": v.get("subtitle", ""),
                "authors": v.get("authors") or [],
                "year": (v.get("publishedDate") or "")[:4],
                "language": v.get("language", ""),
                "publisher": v.get("publisher", ""),
                "isbn13": _isbn13(ids),
                "saleability": sale.get("saleability", ""),  # FOR_SALE | FREE | NOT_FOR_SALE ...
                "is_ebook": bool(sale.get("isEbook")),
                "price": (sale.get("listPrice") or {}),
                "buy_link": sale.get("buyLink", ""),
                "viewability": acc.get("viewability", ""),
                "epub_available": bool((acc.get("epub") or {}).get("isAvailable")),
                "web_reader": acc.get("webReaderLink", ""),
                "info_link": v.get("infoLink", ""),
            }
        )
    return out


async def _gutenberg(client: httpx.AsyncClient, query: str, warnings: List[str]) -> List[Dict[str, Any]]:
    data = await _get_json(client, GUTENDEX, {"search": query}, warnings)
    out = []
    for b in ((data or {}).get("results") or [])[:5]:
        out.append(
            {
                "title": b.get("title", ""),
                "authors": [a.get("name", "") for a in b.get("authors") or []],
                "url": f"https://www.gutenberg.org/ebooks/{b.get('id')}",
                "languages": b.get("languages") or [],
            }
        )
    return out


def _availability_from_openlibrary(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    access = doc.get("ebook_access") or ""
    ia = (doc.get("ia") or [])[:2]
    for ident in ia:
        if access == "public":
            items.append(
                {"type": "direct", "kind": "public_domain", "legal": True, "label": "Internet Archive (đọc/tải miễn phí, public domain)",
                 "url": f"https://archive.org/details/{ident}"}
            )
        elif access == "borrowable":
            items.append(
                {"type": "direct", "kind": "borrow", "legal": True, "label": "Internet Archive (mượn kỹ thuật số miễn phí)",
                 "url": f"https://archive.org/details/{ident}"}
            )
    if not ia and access in ("public", "borrowable"):
        items.append(
            {"type": "direct", "kind": "borrow" if access == "borrowable" else "public_domain", "legal": True,
             "label": "Open Library", "url": f"https://openlibrary.org{doc.get('key', '')}"}
        )
    return items


def _availability_from_google(vol: Dict[str, Any], country: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    price = vol.get("price") or {}
    price_txt = f"{price.get('amount')} {price.get('currencyCode')}" if price.get("amount") else ""
    if vol.get("saleability") == "FOR_SALE" and vol.get("buy_link"):
        items.append(
            {"type": "direct", "kind": "buy_ebook" if vol.get("is_ebook") else "buy_print", "legal": True,
             "label": f"Google Play Books (bán tại {country})", "price": price_txt, "url": vol["buy_link"]}
        )
    if vol.get("saleability") == "FREE" and vol.get("web_reader"):
        items.append(
            {"type": "direct", "kind": "public_domain", "legal": True, "label": "Google Books (miễn phí)", "url": vol["web_reader"]}
        )
    elif vol.get("viewability") in ("PARTIAL", "ALL_PAGES") and vol.get("web_reader"):
        items.append(
            {"type": "direct", "kind": "preview", "legal": True, "label": "Google Books (đọc thử)", "url": vol["web_reader"]}
        )
    return items


async def discover_book(query: str, target_lang: str = "vi", country: str = "VN") -> Dict[str, Any]:
    """Trả về các cuốn sách khớp truy vấn, mỗi cuốn kèm bản dịch tiếng đích và danh sách cách kiếm hợp pháp."""
    query = (query or "").strip()
    if not query:
        return {"query": query, "works": [], "sources_ok": {}, "warnings": []}

    warnings: List[str] = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=True) as client:
        ol_docs, gb_items, gut_items = await asyncio.gather(
            _openlibrary_works(client, query, warnings),
            _google_books(client, query, country, target_lang, warnings),
            _gutenberg(client, query, warnings),
        )

        works: List[Dict[str, Any]] = []
        # Gộp các bản ghi trùng (Open Library hay có nhiều "work" cho cùng một cuốn): giữ bản nhiều ấn bản nhất
        for doc in ol_docs:  # giữ thứ tự độ liên quan của Open Library
            title = doc.get("title", "")
            authors = doc.get("author_name") or []
            first_author = _fold((authors or [""])[0])
            same = next(
                (
                    w
                    for w in works
                    if title_overlap(title, w["title"]) >= 0.95 and _fold((w["authors"] or [""])[0]) == first_author
                ),
                None,
            )
            if same is not None:
                if (doc.get("edition_count") or 0) > (same["doc"].get("edition_count") or 0):
                    same["doc"] = doc  # cùng một cuốn: lấy bản ghi có nhiều ấn bản hơn, giữ vị trí cũ
                continue
            if len(works) < 4:
                works.append({"doc": doc, "title": title, "authors": authors})

        # Fallback: Open Library không có -> dựng "work" từ Google Books
        if not works and gb_items:
            for vol in gb_items[:3]:
                works.append({"doc": {}, "title": vol["title"], "authors": vol["authors"], "gb_seed": vol})

        translations_all = await asyncio.gather(
            *[_openlibrary_translations(client, (w["doc"] or {}).get("key", ""), target_lang, warnings) for w in works[:3]]
        ) if works else []

        # Với cuốn đầu tiên có ISBN thì hỏi Google Books chính xác theo ISBN (giá/khả dụng theo quốc gia)
        gb_by_isbn: List[Dict[str, Any]] = []
        if works:
            first_isbn = _isbn13(works[0]["doc"].get("isbn") or []) or (works[0].get("gb_seed") or {}).get("isbn13")
            if first_isbn:
                gb_by_isbn = await _google_books(client, query, country, target_lang, warnings, isbn=first_isbn)

    result_works: List[Dict[str, Any]] = []
    for idx, w in enumerate(works):
        doc = w["doc"]
        title, authors = w["title"], w["authors"]
        isbn13 = _isbn13(doc.get("isbn") or []) or (w.get("gb_seed") or {}).get("isbn13")
        availability: List[Dict[str, Any]] = _availability_from_openlibrary(doc)

        # Google Books khớp tên -> thêm giá/khả dụng
        pool = (gb_by_isbn if idx == 0 else []) + gb_items
        seen_links = set()
        for vol in pool:
            if not same_book(title, authors, vol["title"], vol["authors"], 0.7):
                continue
            for a in _availability_from_google(vol, country):
                if a["url"] not in seen_links:
                    seen_links.add(a["url"])
                    availability.append(a)

        # Gutenberg khớp tên -> bản public domain
        for g in gut_items:
            if same_book(title, authors, g["title"], g["authors"], 0.8):
                availability.append(
                    {"type": "direct", "kind": "public_domain", "legal": True, "label": "Project Gutenberg (miễn phí, public domain)", "url": g["url"]}
                )
                break

        translations = translations_all[idx] if idx < len(translations_all) else []
        # Bản dịch từ Google Books (ngôn ngữ đích)
        for vol in gb_items:
            if vol.get("language") == target_lang and authors_overlap(authors, vol["authors"]) and len(translations) < 6:
                translations.append(
                    {"title": vol["title"], "authors": vol["authors"], "year": vol["year"], "publisher": vol["publisher"],
                     "isbn": vol["isbn13"], "url": vol["info_link"]}
                )

        if idx == 0:
            availability += retail_links(title, (authors or [""])[0], isbn13, target_lang)

        cover = f"https://covers.openlibrary.org/b/id/{doc['cover_i']}-M.jpg" if doc.get("cover_i") else ""
        result_works.append(
            {
                "title": title,
                "authors": authors,
                "first_publish_year": doc.get("first_publish_year") or (w.get("gb_seed") or {}).get("year"),
                "edition_count": doc.get("edition_count"),
                "languages": doc.get("language") or [],
                "isbn13": isbn13,
                "cover_url": cover,
                "open_library_url": f"https://openlibrary.org{doc['key']}" if doc.get("key") else "",
                "translations": translations,
                "has_translation_in_target": bool(translations),
                "availability": availability,
                "freely_available": any(a["kind"] in ("public_domain",) for a in availability),
            }
        )

    return {
        "query": query,
        "target_lang": target_lang,
        "country": country,
        "works": result_works,
        "sources_ok": {"open_library": bool(ol_docs), "google_books": bool(gb_items), "gutenberg": bool(gut_items)},
        "warnings": sorted(set(warnings)),
    }
