"""Dịch PDF tại chỗ (backend/translators/pdf_inplace.py + pdf_translator.py): dùng PDF tự tạo, không mạng."""
from __future__ import annotations

import asyncio
import os

import fitz
import pytest

from backend.translators import pdf_inplace as pi
from backend.translators.pdf_translator import translate_pdf_file

NAVY = (0.0, 0.2, 0.35)


def _font_or_skip(serif=False):
    if not pi.resolve_font_path(serif, False, False):
        pytest.skip("không có phông Unicode (Arial/Liberation/DejaVu) trên máy này")


def make_pdf(path, build):
    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    build(page)
    doc.save(path)
    doc.close()
    return str(path)


def run(coro):
    return asyncio.run(coro)


def page_text(page) -> str:
    """Lớp chữ của trang, chuẩn hóa NBSP và khoảng trắng để so sánh."""
    return " ".join(page.get_text().replace("\xa0", " ").split())


# ----------------------------------------------------------------------------- helpers thuần
def test_protect_and_restore_urls_roundtrip_and_missing_marker():
    text = "See https://example.org/a.pdf and mail me@site.com or visit ChallengingBehavior.org now."
    protected, mapping = pi.protect_urls(text)
    assert "https://" not in protected and len(mapping) == 3
    assert pi.restore_urls(protected, mapping) == text
    # model làm rơi một marker -> URL vẫn không mất
    key = next(iter(mapping))
    lost = protected.replace(key, "")
    restored = pi.restore_urls(lost, mapping)
    assert all(url in restored for url in mapping.values())
    assert "⟦" not in restored and "⟧" not in restored


def test_is_only_urls_and_should_translate():
    assert pi.is_only_urls("https://a.org/x  www.b.com")
    assert not pi.is_only_urls("Visit https://a.org today")
    assert not pi.should_translate("12") and not pi.should_translate("—") and pi.should_translate("Hello")


def test_clean_text_dehyphenates_and_joins_lines():
    assert pi.clean_text("trans-\nlation of the\ntext") == "translation of the text"


def test_symbol_span_detection():
    assert pi._is_symbol_span({"text": "•", "font": "Arial"})
    assert pi._is_symbol_span({"text": "", "font": "Wingdings-Regular"})
    assert pi._is_symbol_span({"text": "�", "font": "CenturyGothic-Bold"})
    assert not pi._is_symbol_span({"text": "Hello", "font": "Arial"})
    assert not pi._is_symbol_span({"text": ").", "font": "Arial"})  # dấu câu thường vẫn là chữ


# ----------------------------------------------------------------------------- trích xuất
def test_extract_merges_lines_but_keeps_paragraphs_and_cells_separate(tmp_path):
    def build(p):
        for i, line in enumerate(["The first line of a long", "paragraph continues here."]):
            p.insert_text((40, 60 + i * 13), line, fontname="helv", fontsize=11)
        p.insert_text((40, 130), "A different paragraph far below.", fontname="helv", fontsize=11)
        # hai ô bảng cùng hàng, cách xa nhau
        p.insert_text((40, 200), "Monday", fontname="hebo", fontsize=10)
        p.insert_text((250, 200), "Tuesday", fontname="hebo", fontsize=10)

    path = make_pdf(tmp_path / "a.pdf", build)
    units = pi.extract_units(fitz.open(path))
    texts = [u.text for u in units]
    assert "The first line of a long paragraph continues here." in texts  # gộp dòng
    assert "A different paragraph far below." in texts                    # đoạn riêng
    assert "Monday" in texts and "Tuesday" in texts                       # ô bảng riêng


def test_symbol_bullet_not_part_of_text_and_starts_new_item(tmp_path):
    def build(p):
        for i, item in enumerate(["First item", "Second item"]):
            y = 80 + i * 13  # sát nhau: chỉ dấu đầu dòng mới tách mục
            p.insert_text((40, y), "\xb7", fontname="symb", fontsize=11)
            p.insert_text((60, y), item, fontname="helv", fontsize=11)

    path = make_pdf(tmp_path / "b.pdf", build)
    units = pi.extract_units(fitz.open(path))
    assert sorted(u.text for u in units) == ["First item", "Second item"]
    assert all(u.starts_bullet for u in units)


def test_hidden_text_is_ignored(tmp_path):
    def build(p):
        p.insert_text((40, 60), "Visible text here", fontname="helv", fontsize=12)
        p.insert_text((40, 120), "Invisible layer text", fontname="helv", fontsize=12, render_mode=3)

    path = make_pdf(tmp_path / "c.pdf", build)
    texts = [u.text for u in pi.extract_units(fitz.open(path))]
    assert texts == ["Visible text here"]


# ----------------------------------------------------------------------------- dựng trang
def test_render_replaces_text_keeps_background_and_bullet(tmp_path):
    _font_or_skip()

    def build(p):
        p.draw_rect(fitz.Rect(20, 40, 380, 90), color=None, fill=NAVY)
        p.insert_text((30, 70), "Hello world", fontname="helv", fontsize=14, color=(1, 1, 1))
        p.insert_text((40, 150), "\xb7", fontname="symb", fontsize=11)
        p.insert_text((60, 150), "Bullet item text", fontname="helv", fontsize=11)

    path = make_pdf(tmp_path / "d.pdf", build)
    doc = fitz.open(path)
    units = pi.extract_units(doc)
    for u in units:
        u.dst = {"Hello world": "Xin chào thế giới", "Bullet item text": "Nội dung mục đầu dòng"}[u.text]
    stats = pi.render_units(doc, units)
    assert stats["rendered"] == 2
    page = doc[0]
    text = page_text(page)
    assert "Hello world" not in text and "Bullet item text" not in text      # chữ gốc đã bị xóa thật
    assert "Xin chào thế giới" in text and "Nội dung mục đầu dòng" in text    # bản dịch có mặt
    # nền màu vẫn còn (không bị phủ trắng): lấy điểm ảnh ở vùng nền không có chữ
    pix = page.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
    r, g, b = pix.pixel(370, 80)[:3]
    assert (r, g, b) != (255, 255, 255) and b > r
    # dấu đầu dòng (font Symbol) còn nguyên
    fonts = {s["font"] for blk in page.get_text("dict")["blocks"] if blk["type"] == 0
             for ln in blk["lines"] for s in ln["spans"]}
    assert any("symbol" in f.lower() for f in fonts)


def test_render_shrinks_long_translation_to_fit_and_keeps_all_text(tmp_path):
    _font_or_skip()

    def build(p):
        p.insert_textbox(fitz.Rect(40, 60, 200, 100), "Short original label text", fontname="helv", fontsize=10)

    path = make_pdf(tmp_path / "e.pdf", build)
    doc = fitz.open(path)
    units = pi.extract_units(doc)
    long_vi = "Một nhãn dịch dài hơn nhiều so với bản gốc để buộc thuật toán phải co chữ cho vừa khung " * 2
    for u in units:
        u.dst = long_vi
    stats = pi.render_units(doc, units)
    out = page_text(doc[0])
    assert long_vi.strip()[:40] in out and long_vi.strip()[-30:] in out  # không mất chữ
    assert stats["shrunk"] == 1


def test_vertical_text_is_translated_in_place(tmp_path):
    _font_or_skip()

    def build(p):
        p.insert_text((200, 250), "Vertical form label", fontname="helv", fontsize=12, rotate=90)

    path = make_pdf(tmp_path / "f.pdf", build)
    doc = fitz.open(path)
    units = pi.extract_units(doc)
    assert len(units) == 1 and units[0].vertical
    units[0].dst = "Nhãn biểu mẫu dọc"
    pi.render_units(doc, units)
    text = page_text(doc[0])
    assert "Vertical form label" not in text and "Nhãn biểu mẫu dọc" in text
    # chữ mới vẫn chạy dọc (dir ~ (0,-1))
    dirs = [ln["dir"] for blk in doc[0].get_text("dict")["blocks"] if blk["type"] == 0 for ln in blk["lines"]]
    assert any(abs(d[1]) > 0.9 for d in dirs)


def test_target_box_does_not_overlap_neighbor_on_the_right(tmp_path):
    def build(p):
        p.insert_text((40, 100), "Left cell", fontname="helv", fontsize=10)
        p.insert_text((110, 100), "Right neighbour", fontname="helv", fontsize=10)

    path = make_pdf(tmp_path / "g.pdf", build)
    doc = fitz.open(path)
    units = pi.extract_units(doc)
    left = next(u for u in units if u.text == "Left cell")
    right = next(u for u in units if u.text == "Right neighbour")
    box = pi._target_box(left, doc[0].rect, units)
    assert box.x1 <= right.rect.x0 - 2.5


# ----------------------------------------------------------------------------- luồng đầy đủ
def test_translate_pdf_file_end_to_end_with_fake_llm(tmp_path, make_client):
    _font_or_skip()

    def build(p):
        p.draw_rect(fitz.Rect(20, 20, 380, 60), color=None, fill=NAVY)
        p.insert_text((30, 45), "Guide Title", fontname="helv", fontsize=14, color=(1, 1, 1))
        p.insert_text((40, 120), "Read https://example.org/doc.pdf carefully", fontname="helv", fontsize=11)
        p.insert_text((40, 200), "https://only.url.example.com/path", fontname="helv", fontsize=11)

    src = make_pdf(tmp_path / "src.pdf", build)
    client = make_client()
    seen = []

    async def cb(done, total, s="", d=""):
        seen.append((done, total))

    res = run(translate_pdf_file(src, str(tmp_path / "out"), "book", client, progress_cb=cb))
    for key in ("mono_pdf", "bilingual_pdf", "docx_output"):
        assert os.path.exists(res[key]), key
    assert res["total_items"] == 2 and seen[-1] == (2, 2)  # đoạn chỉ gồm URL bị bỏ qua
    out = fitz.open(res["mono_pdf"])
    text = page_text(out[0])
    assert "VI(Guide Title)" in text
    assert "VI(Read https://example.org/doc.pdf carefully)" in text  # URL còn nguyên trong bản dịch
    assert "https://only.url.example.com/path" in text  # đoạn toàn URL giữ nguyên
    # PDF song ngữ: mỗi trang gồm trang gốc | trang dịch (rộng gấp đôi)
    assert fitz.open(res["bilingual_pdf"])[0].rect.width > out[0].rect.width * 1.9


def test_identical_strings_are_translated_once_and_consistently(tmp_path, make_client):
    _font_or_skip()

    def build(p):
        p.insert_text((40, 60), "Running header text", fontname="helv", fontsize=11)
        p.insert_text((40, 160), "Body paragraph one", fontname="helv", fontsize=11)

    doc = fitz.open()
    for _ in range(3):  # cùng đầu trang ở 3 trang
        build(doc.new_page(width=400, height=300))
    src = tmp_path / "multi.pdf"
    doc.save(str(src))
    doc.close()

    client = make_client()
    res = run(translate_pdf_file(str(src), str(tmp_path / "out"), "multi", client))
    assert res["total_items"] == 2  # 6 đoạn nhưng chỉ 2 chuỗi khác nhau
    sent = " ".join(c[1] for c in client.calls)
    assert sent.count("Running header text") == 1  # gửi LLM đúng một lần
    out = fitz.open(res["mono_pdf"])
    assert all("VI(Running header text)" in page_text(pg) for pg in out)  # nhưng hiện ở cả 3 trang


def test_role_hints_reach_the_model_and_markers_stay_parseable(tmp_path, make_client):
    _font_or_skip()

    def build(p):
        p.draw_rect(fitz.Rect(20, 20, 380, 60), color=None, fill=NAVY)
        p.insert_text((30, 45), "Guide Title", fontname="hebo", fontsize=24, color=(1, 1, 1))
        for i in range(3):
            p.insert_text((40, 120 + i * 40), "Body text paragraph number %d here." % i, fontname="helv", fontsize=11)

    src = make_pdf(tmp_path / "hints.pdf", build)
    client = make_client()
    res = run(translate_pdf_file(src, str(tmp_path / "out"), "hints", client))
    sent = " ".join(c[1] for c in client.calls)
    assert "VAI TRÒ/VỊ TRÍ" in sent and "tiêu đề cỡ lớn" in sent and "trang 1" in sent
    out = page_text(fitz.open(res["mono_pdf"])[0])
    assert "VI(Guide Title)" in out and "VAI TRÒ" not in out  # phần ngữ cảnh không lọt vào bản dịch


def test_free_form_brief_is_rendered_and_old_profile_format_still_works():
    from backend.llm.prompts import format_book_brief

    brief = format_book_brief(
        {"document_type": "hợp đồng dịch vụ", "understanding": "Hợp đồng giữa hai công ty.",
         "translator_notes": "Dịch 'shall' là 'phải'. Giữ tên pháp nhân.", "do_not_translate": ["Northwind Traders, Inc."]}
    )
    assert "Loại tài liệu: hợp đồng dịch vụ" in brief and "'shall' là 'phải'" in brief and "Northwind" in brief
    legacy = format_book_brief({"genre": "kinh doanh", "style_rules": ["ngắn gọn"], "policies": {"organization_names": "giữ"}})
    assert "kinh doanh" in legacy and "ngắn gọn" in legacy and "organization_names: giữ" in legacy
