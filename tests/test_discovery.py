"""Tra cứu thông tin sách (backend/search/discovery.py): offline, nguồn HTTP được thay bằng dữ liệu giả."""
from __future__ import annotations

import asyncio

from backend.search import discovery as d

OL_DOC_PUBLIC = {
    "key": "/works/OL66554W",
    "title": "Pride and Prejudice",
    "author_name": ["Jane Austen"],
    "first_publish_year": 1813,
    "isbn": ["0141439513", "9780141439518"],
    "language": ["eng", "vie"],
    "edition_count": 4042,
    "cover_i": 123,
    "ia": ["prideprejudice0000jane"],
    "ebook_access": "public",
}
OL_DOC_BORROW = {
    "key": "/works/OL1W",
    "title": "Atomic Habits",
    "author_name": ["James Clear"],
    "first_publish_year": 2016,
    "isbn": ["9780735211292"],
    "language": ["eng"],
    "edition_count": 42,
    "ia": ["atomichabits0000clea"],
    "ebook_access": "borrowable",
}


def fake_get_json_factory(ol_docs, vn_editions=None, gb_items=None, gut=None, fail=()):
    async def fake(client, url, params, warnings=None):
        host = url.split("/")[2]
        if host in fail:
            if warnings is not None:
                warnings.append(f"{host}: HTTP 429")
            return None
        if url.endswith("/search.json"):
            return {"docs": ol_docs}
        if url.endswith("/query.json"):
            return vn_editions or []
        if "googleapis.com" in url:
            return {"items": gb_items or []}
        if "gutendex" in url:
            return {"results": gut or []}
        return None

    return fake


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------- pure helpers
def test_title_similarity_accent_and_case_insensitive():
    assert d.title_similarity("Đắc nhân tâm", "dac nhan tam") == 1.0
    assert d.title_similarity("Atomic Habits", "Atomic Habits Workbook") == 1.0  # min-based
    assert d.title_similarity("Atomic Habits", "Sapiens") == 0.0


def test_isbn13_picked_over_isbn10():
    assert d._isbn13(["0141439513", "9780141439518"]) == "9780141439518"
    assert d._isbn13(["0141439513"]) is None
    assert d._isbn13([]) is None


def test_retail_links_are_search_urls_only_and_vi_adds_local_stores():
    links = d.retail_links("Atomic Habits", "James Clear", "9780735211292", "vi")
    labels = [x["label"] for x in links]
    assert any("Fahasa" in l for l in labels) and any("Tiki" in l for l in labels)
    assert all(x["type"] == "search_link" and x["legal"] for x in links)
    assert all(x["url"].startswith("https://") for x in links)
    assert not any("Fahasa" in x["label"] for x in d.retail_links("X", "Y", None, "en"))


def test_availability_from_openlibrary_public_vs_borrowable():
    pub = d._availability_from_openlibrary(OL_DOC_PUBLIC)
    assert pub and pub[0]["kind"] == "public_domain"
    bor = d._availability_from_openlibrary(OL_DOC_BORROW)
    assert bor and bor[0]["kind"] == "borrow"
    assert d._availability_from_openlibrary({"ebook_access": "no_ebook", "ia": []}) == []


def test_availability_from_google_for_sale_and_free():
    sale = {"saleability": "FOR_SALE", "buy_link": "https://play.google.com/x", "is_ebook": True,
            "price": {"amount": 4.99, "currencyCode": "USD"}}
    a = d._availability_from_google(sale, "VN")
    assert a[0]["kind"] == "buy_ebook" and "4.99" in a[0]["price"]
    assert d._availability_from_google({"saleability": "NOT_FOR_SALE"}, "VN") == []


# ----------------------------------------------------------------- discover_book
def test_discover_finds_real_vietnamese_edition_and_public_domain(monkeypatch):
    vn = [{"title": "Kiêu hãnh và định kiến", "publishers": ["NXB Hội nhà văn"], "publish_date": "2010", "key": "/books/OL9M"}]
    gut = [{"id": 1342, "title": "Pride and Prejudice", "authors": [{"name": "Austen, Jane"}], "languages": ["en"]}]
    monkeypatch.setattr(d, "_get_json", fake_get_json_factory([OL_DOC_PUBLIC], vn_editions=vn, gut=gut))
    out = run(d.discover_book("Pride and Prejudice"))
    w = out["works"][0]
    assert w["has_translation_in_target"] and w["translations"][0]["title"] == "Kiêu hãnh và định kiến"
    assert w["freely_available"] is True
    kinds = {a["kind"] for a in w["availability"]}
    assert "public_domain" in kinds
    assert any("gutenberg.org/ebooks/1342" in a["url"] for a in w["availability"])
    assert w["isbn13"] == "9780141439518" and w["cover_url"].endswith("123-M.jpg")


def test_discover_copyrighted_book_not_free_and_gets_search_links(monkeypatch):
    monkeypatch.setattr(d, "_get_json", fake_get_json_factory([OL_DOC_BORROW]))
    w = run(d.discover_book("Atomic Habits"))["works"][0]
    assert w["freely_available"] is False
    assert any(a["kind"] == "borrow" for a in w["availability"])
    assert any(a["type"] == "search_link" for a in w["availability"])
    assert w["has_translation_in_target"] is False


def test_discover_source_failure_reports_warning_not_crash(monkeypatch):
    monkeypatch.setattr(d, "_get_json", fake_get_json_factory([OL_DOC_BORROW], fail=("www.googleapis.com", "gutendex.com")))
    out = run(d.discover_book("Atomic Habits"))
    assert out["works"] and out["sources_ok"]["open_library"] is True
    assert out["sources_ok"]["google_books"] is False
    assert any("googleapis" in w for w in out["warnings"])


def test_discover_falls_back_to_google_books_when_openlibrary_empty(monkeypatch):
    gb = [{"volumeInfo": {"title": "Sách Hiếm", "authors": ["A B"], "publishedDate": "2020", "language": "en",
                          "industryIdentifiers": [{"identifier": "9781234567897"}]},
           "saleInfo": {"saleability": "FOR_SALE", "isEbook": True, "buyLink": "https://play.google.com/b",
                        "listPrice": {"amount": 3, "currencyCode": "USD"}},
           "accessInfo": {}}]
    monkeypatch.setattr(d, "_get_json", fake_get_json_factory([], gb_items=gb))
    out = run(d.discover_book("Sách Hiếm"))
    w = out["works"][0]
    assert w["title"] == "Sách Hiếm" and w["isbn13"] == "9781234567897"
    assert any(a["kind"] == "buy_ebook" for a in w["availability"])


def test_discover_empty_query_returns_empty():
    out = run(d.discover_book("   "))
    assert out["works"] == [] and out["warnings"] == []


def test_discover_merges_duplicate_openlibrary_records(monkeypatch):
    dup = dict(OL_DOC_PUBLIC, key="/works/OL2W", edition_count=3, isbn=[])
    other = dict(OL_DOC_BORROW)
    monkeypatch.setattr(d, "_get_json", fake_get_json_factory([dup, OL_DOC_PUBLIC, other]))
    works = run(d.discover_book("Pride and Prejudice"))["works"]
    assert [w["title"] for w in works] == ["Pride and Prejudice", "Atomic Habits"]
    assert works[0]["edition_count"] == 4042  # giữ bản ghi nhiều ấn bản hơn
    assert works[0]["open_library_url"].endswith("/works/OL66554W")


def test_derivative_work_is_not_marked_free_via_gutenberg(monkeypatch):
    # Bản chuyển thể còn bản quyền KHÔNG được khớp với bản public domain chỉ vì tên chứa tên kia.
    zombies = {"key": "/works/OL3W", "title": "Pride and Prejudice and Zombies", "author_name": ["Seth Grahame-Smith", "Jane Austen"],
               "first_publish_year": 2009, "isbn": [], "language": ["eng"], "edition_count": 19, "ebook_access": "no_ebook"}
    gut = [{"id": 1342, "title": "Pride and Prejudice", "authors": [{"name": "Austen, Jane"}], "languages": ["en"]}]
    monkeypatch.setattr(d, "_get_json", fake_get_json_factory([zombies], gut=gut))
    w = run(d.discover_book("Pride and Prejudice and Zombies"))["works"][0]
    assert w["freely_available"] is False
    assert not any("gutenberg" in a["url"] for a in w["availability"])


def test_same_book_requires_matching_author_and_bidirectional_title():
    assert d.same_book("Pride and Prejudice", ["Jane Austen"], "Pride and Prejudice", ["Austen, Jane"], 0.8)
    assert not d.same_book("Pride and Prejudice", ["Jane Austen"], "Pride and Prejudice and Zombies", ["Jane Austen"], 0.8)
    assert not d.same_book("Emma", ["Jane Austen"], "Emma", ["Someone Else"], 0.8)
