import logging
import os
import re
import shutil
import zipfile
from typing import List, Set
from bs4 import BeautifulSoup, Tag
import docx
from docx.shared import Pt, RGBColor

logger = logging.getLogger(__name__)


def _clean_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _extract_sample_from_epub(input_path: str, max_chars: int) -> str:
    collected: List[str] = []
    total_len = 0

    with zipfile.ZipFile(input_path, "r") as zf:
        # Ưu tiên lấy thứ tự file HTML từ spine thông qua helper của epub_translator nếu có
        try:
            from .epub_translator import _get_ordered_html_files_from_epub
            html_files = _get_ordered_html_files_from_epub(zf)
        except Exception:
            html_files = [
                name
                for name in zf.namelist()
                if name.lower().endswith((".xhtml", ".html", ".htm"))
                and not os.path.basename(name).startswith(".")
            ]

        for fname in html_files:
            try:
                raw_bytes = zf.read(fname)
                html_str = raw_bytes.decode("utf-8", errors="ignore")
                soup = BeautifulSoup(html_str, "html.parser")
                for skip in soup.find_all(["script", "style", "head", "nav"]):
                    skip.decompose()

                body = soup.find("body") or soup
                for p in body.find_all(["h1", "h2", "h3", "h4", "p", "li", "blockquote"]):
                    txt = _clean_line(p.get_text(separator=" ", strip=True))
                    if len(txt) >= 10:
                        collected.append(txt)
                        total_len += len(txt) + 1
                        if total_len >= max_chars:
                            break
                if total_len >= max_chars:
                    break
            except Exception as e:
                logger.debug(f"Bỏ qua lỗi khi đọc {fname} trong EPUB: {e}")

    joined = "\n".join(collected)
    return joined[:max_chars]


def _extract_sample_from_docx(input_path: str, max_chars: int) -> str:
    collected: List[str] = []
    total_len = 0
    doc = docx.Document(input_path)

    for p in doc.paragraphs:
        txt = _clean_line(p.text)
        if len(txt) >= 5:
            collected.append(txt)
            total_len += len(txt) + 1
            if total_len >= max_chars:
                return "\n".join(collected)[:max_chars]

    # Nếu trong thân văn bản chưa đủ, quét thêm trong các bảng
    seen_cells: Set[int] = set()
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                tc_id = id(cell._tc)
                if tc_id in seen_cells:
                    continue
                seen_cells.add(tc_id)
                for p in cell.paragraphs:
                    txt = _clean_line(p.text)
                    if len(txt) >= 5:
                        collected.append(txt)
                        total_len += len(txt) + 1
                        if total_len >= max_chars:
                            return "\n".join(collected)[:max_chars]

    return "\n".join(collected)[:max_chars]


def _extract_sample_from_pdf(input_path: str, max_chars: int) -> str:
    import fitz  # PyMuPDF

    collected: List[str] = []
    total_len = 0
    with fitz.open(input_path) as pdf_doc:
        for page in pdf_doc:
            page_text = page.get_text("text").strip()
            if page_text:
                collected.append(page_text)
                total_len += len(page_text) + 1
                if total_len >= max_chars:
                    break
    return "\n".join(collected)[:max_chars]


def _extract_sample_from_txt(input_path: str, max_chars: int) -> str:
    for enc in ("utf-8-sig", "utf-8", "utf-16", "cp1252", "latin-1"):
        try:
            with open(input_path, "r", encoding=enc) as f:
                return f.read(max_chars)
        except UnicodeDecodeError:
            continue
    with open(input_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read(max_chars)


def extract_book_sample_text(input_path: str, max_chars: int = 12000) -> str:
    """
    Trích xuất nhanh tối đa `max_chars` ký tự đầu tiên có nội dung thực
    từ file .epub, .docx, .pdf, .txt hoặc .md để phục vụ tính năng Auto-Glossary Extraction.
    """
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Không tìm thấy file đầu vào: {input_path}")

    ext = os.path.splitext(input_path)[1].lower()
    try:
        if ext == ".epub":
            return _extract_sample_from_epub(input_path, max_chars)
        elif ext == ".docx":
            return _extract_sample_from_docx(input_path, max_chars)
        elif ext == ".pdf":
            return _extract_sample_from_pdf(input_path, max_chars)
        elif ext in (".txt", ".md", ".markdown"):
            return _extract_sample_from_txt(input_path, max_chars)
        else:
            return _extract_sample_from_txt(input_path, max_chars)
    except Exception as e:
        logger.error(f"Lỗi khi trích xuất văn bản mẫu từ {input_path}: {e}")
        return ""


def extract_book_spread_sample(input_path: str, total_chars: int = 15000) -> str:
    """
    Lấy mẫu rải đều từ đầu (bỏ qua trang bìa/mục lục), giữa và cuối sách
    để Agent 2 hiểu đúng thể loại, nhân vật và thuật ngữ cả cuốn thay vì chỉ vài trang đầu.
    """
    full = extract_book_sample_text(input_path, 600_000)
    if len(full) <= total_chars:
        return full
    part = total_chars // 3
    n = len(full)
    starts = [int(n * 0.03), int(n * 0.40), int(n * 0.75)]
    starts = [min(st, n - part) for st in starts]
    return (chr(10) * 2 + "[...]" + chr(10) * 2).join(full[st : st + part] for st in starts)


def _describe_pdf_layout(input_path: str) -> str:
    import fitz  # PyMuPDF

    doc = fitz.open(input_path)
    try:
        n = len(doc)
        if n == 0:
            return ""
        idxs = sorted({int(i * (n - 1) / max(1, min(12, n) - 1)) for i in range(min(12, n))})
        lines = short = vertical_pages = rect_pages = img_pages = bullet_pages = 0
        text_pages = 0
        total_chars = 0
        for i in idxs:
            page = doc[i]
            data = page.get_text("dict")
            page_lines = 0
            page_vertical = False
            page_bullets = False
            page_chars = 0
            for block in data.get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    txt = "".join(sp.get("text", "") for sp in line.get("spans", [])).strip()
                    if not txt:
                        continue
                    page_lines += 1
                    page_chars += len(txt)
                    if len(txt) < 35:
                        short += 1
                    if abs(line.get("dir", (1, 0))[1]) > 0.9:
                        page_vertical = True
                    if any(ch in txt[:3] for ch in "•▪☐☑") or any(
                        "wingding" in sp.get("font", "").lower() or "symbol" in sp.get("font", "").lower()
                        for sp in line.get("spans", [])
                    ):
                        page_bullets = True
            lines += page_lines
            total_chars += page_chars
            text_pages += 1 if page_chars > 30 else 0
            vertical_pages += 1 if page_vertical else 0
            bullet_pages += 1 if page_bullets else 0
            img_pages += 1 if page.get_images() else 0
            try:
                rects = [d for d in page.get_drawings() if d.get("rect") is not None and d["rect"].width > 20 and d["rect"].height > 8]
                rect_pages += 1 if len(rects) >= 6 else 0
            except Exception:
                pass
        k = len(idxs)
        avg_lines = lines / k
        out = [
            f"PDF {n} trang (xem {k} trang mẫu), khổ {int(doc[0].rect.width)}x{int(doc[0].rect.height)}pt; bố cục cố định (máy sẽ thay chữ tại đúng vị trí gốc).",
            f"Trung bình {avg_lines:.0f} dòng chữ/trang, {100 * short / max(1, lines):.0f}% là dòng ngắn (<35 ký tự); {total_chars // max(1, k)} ký tự/trang.",
            f"{100 * rect_pages // k}% trang có bảng/khung/ô tô màu; {100 * img_pages // k}% trang có ảnh; "
            f"{100 * bullet_pages // k}% trang có dấu đầu dòng/ô tích; {100 * vertical_pages // k}% trang có chữ xoay dọc.",
        ]
        if text_pages < k // 2:
            out.append("CẢNH BÁO: nhiều trang gần như không có lớp chữ (có thể là bản scan/ảnh).")
        return " ".join(out)
    finally:
        doc.close()


def _describe_docx_layout(input_path: str) -> str:
    d = docx.Document(input_path)
    tables = len(d.tables)
    paras = [p for p in d.paragraphs if p.text.strip()]
    heads = sum(1 for p in paras if (p.style.name or "").lower().startswith("heading"))
    imgs = len(d.inline_shapes)
    short = sum(1 for p in paras if len(p.text.strip()) < 35)
    return (
        f"DOCX: {len(paras)} đoạn chữ ({100 * short // max(1, len(paras))}% là đoạn ngắn), {heads} đề mục, "
        f"{tables} bảng, {imgs} ảnh."
    )


def _describe_epub_layout(input_path: str) -> str:
    import zipfile

    with zipfile.ZipFile(input_path) as z:
        names = z.namelist()
        html = [n for n in names if n.lower().endswith((".xhtml", ".html", ".htm"))]
        imgs = [n for n in names if n.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".svg"))]
        tables = pres = 0
        for n in html[:25]:
            raw = z.read(n).decode("utf-8", errors="ignore").lower()
            tables += raw.count("<table")
            pres += raw.count("<pre") + raw.count("<code")
    return f"EPUB: {len(html)} tệp nội dung, {len(imgs)} ảnh, {tables} bảng, {pres} khối mã/định dạng sẵn (xem 25 tệp đầu)."


def describe_book_layout(input_path: str) -> str:
    """Mô tả ngắn cấu trúc/bố cục của file (do máy đo) để Agent 2 suy ra loại tài liệu và chính sách dịch phù hợp."""
    ext = os.path.splitext(input_path)[1].lower()
    try:
        if ext == ".pdf":
            return _describe_pdf_layout(input_path)
        if ext == ".docx":
            return _describe_docx_layout(input_path)
        if ext == ".epub":
            return _describe_epub_layout(input_path)
    except Exception as exc:
        logger.debug("describe_book_layout failed: %s", exc)
    return ""


def _setup_clean_docx_styles(doc: docx.document.Document) -> None:
    """
    Thiết lập font chữ chuẩn Tiếng Việt (Times New Roman / Arial / Segoe UI)
    và khoảng cách đoạn văn sạch đẹp để báo cáo / nộp sếp.
    """
    try:
        normal_style = doc.styles["Normal"]
        normal_style.font.name = "Times New Roman"
        normal_style.font.size = Pt(12)
        normal_style.paragraph_format.space_after = Pt(6)
        normal_style.paragraph_format.line_spacing = 1.25
    except Exception:
        pass


def _convert_epub_to_docx(input_path: str, output_docx_path: str) -> str:
    doc = docx.Document()
    _setup_clean_docx_styles(doc)

    with zipfile.ZipFile(input_path, "r") as zf:
        try:
            from .epub_translator import _get_ordered_html_files_from_epub, _is_leaf_block_tag, TARGET_TAGS
            html_files = _get_ordered_html_files_from_epub(zf)
        except Exception:
            html_files = [
                name
                for name in zf.namelist()
                if name.lower().endswith((".xhtml", ".html", ".htm"))
                and not os.path.basename(name).startswith(".")
            ]
            TARGET_TAGS = ("p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote")
            _is_leaf_block_tag = lambda t: True

        first_chapter = True
        for fname in html_files:
            raw_bytes = zf.read(fname)
            html_str = raw_bytes.decode("utf-8", errors="ignore")
            soup = BeautifulSoup(html_str, "html.parser")
            for skip in soup.find_all(["script", "style", "head", "nav"]):
                skip.decompose()

            body = soup.find("body") or soup
            chapter_elements: List[Tag] = []
            for tag in body.find_all(TARGET_TAGS):
                if _is_leaf_block_tag(tag):
                    txt = tag.get_text(separator=" ", strip=True)
                    if txt:
                        chapter_elements.append(tag)

            if not chapter_elements:
                continue

            if not first_chapter:
                doc.add_page_break()
            first_chapter = False

            for tag in chapter_elements:
                txt = re.sub(r"\s+", " ", tag.get_text(separator=" ", strip=True)).strip()
                if not txt:
                    continue

                tag_name = tag.name.lower()
                classes = tag.get("class") or []

                if tag_name == "h1":
                    doc.add_heading(txt, level=1)
                elif tag_name == "h2":
                    doc.add_heading(txt, level=2)
                elif tag_name in ("h3", "h4", "h5", "h6"):
                    doc.add_heading(txt, level=3)
                elif tag_name == "li":
                    doc.add_paragraph(txt, style="List Bullet")
                elif tag_name == "blockquote":
                    p = doc.add_paragraph()
                    run = p.add_run(txt)
                    run.italic = True
                else:
                    p = doc.add_paragraph()
                    run = p.add_run(txt)
                    if "bth-bilingual-src" in classes:
                        run.italic = True
                        run.font.size = Pt(10.5)
                        run.font.color.rgb = RGBColor(100, 116, 139)

    doc.save(output_docx_path)
    return output_docx_path


def _convert_txt_or_md_to_docx(input_path: str, output_docx_path: str) -> str:
    doc = docx.Document()
    _setup_clean_docx_styles(doc)

    raw_text = _extract_sample_from_txt(input_path, max_chars=50_000_000)
    normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n")

    in_code_block = False
    code_lines: List[str] = []
    para_buffer: List[str] = []

    def flush_paragraph() -> None:
        if not para_buffer:
            return
        joined = "\n".join(para_buffer).strip()
        para_buffer.clear()
        if not joined:
            return
        doc.add_paragraph(joined)

    for line in normalized.split("\n"):
        stripped = line.strip()

        # Xử lý khối code ```...```
        if stripped.startswith("```") or stripped.startswith("~~~"):
            flush_paragraph()
            if not in_code_block:
                in_code_block = True
                code_lines = []
            else:
                in_code_block = False
                if code_lines:
                    p = doc.add_paragraph()
                    run = p.add_run("\n".join(code_lines))
                    run.font.name = "Consolas"
                    run.font.size = Pt(10)
                code_lines = []
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        if not stripped:
            flush_paragraph()
            continue

        # Xử lý tiêu đề Markdown (#, ##, ###)
        heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading_match:
            flush_paragraph()
            level = min(len(heading_match.group(1)), 3)
            title_text = heading_match.group(2).strip()
            doc.add_heading(title_text, level=level)
            continue

        # Xử lý danh sách gạch đầu dòng (- , * , + )
        bullet_match = re.match(r"^[-*+]\s+(.*)$", stripped)
        if bullet_match:
            flush_paragraph()
            doc.add_paragraph(bullet_match.group(1).strip(), style="List Bullet")
            continue

        # Xử lý trích dẫn (> ...)
        quote_match = re.match(r"^>\s*(.*)$", stripped)
        if quote_match:
            flush_paragraph()
            p = doc.add_paragraph()
            run = p.add_run(quote_match.group(1).strip())
            run.italic = True
            continue

        para_buffer.append(line)

    flush_paragraph()
    if code_lines:
        p = doc.add_paragraph()
        run = p.add_run("\n".join(code_lines))
        run.font.name = "Consolas"
        run.font.size = Pt(10)

    doc.save(output_docx_path)
    return output_docx_path


def _convert_pdf_to_docx(input_path: str, output_docx_path: str) -> str:
    import fitz

    doc = docx.Document()
    _setup_clean_docx_styles(doc)

    with fitz.open(input_path) as pdf_doc:
        for idx, page in enumerate(pdf_doc):
            if idx > 0:
                doc.add_page_break()
            blocks = page.get_text("blocks")
            # Sắp xếp theo tọa độ y0, x0
            blocks.sort(key=lambda b: (round(b[1], 1), round(b[0], 1)))
            for b in blocks:
                if len(b) >= 5 and b[6] == 0:  # block văn bản
                    txt = _clean_line(b[4])
                    if txt:
                        doc.add_paragraph(txt)

    doc.save(output_docx_path)
    return output_docx_path


def convert_to_docx(input_path: str, output_docx_path: str) -> str:
    """
    Chuyển đổi bất kỳ file .epub, .txt, .md, .pdf (hoặc copy nếu đã là .docx)
    đã dịch xong sang file Microsoft Word (.docx) có định dạng tiêu đề (Heading 1, Heading 2)
    và đoạn văn sạch đẹp để người dùng tải bản Word về.
    """
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Không tìm thấy file cần chuyển đổi sang DOCX: {input_path}")

    os.makedirs(os.path.dirname(os.path.abspath(output_docx_path)), exist_ok=True)
    ext = os.path.splitext(input_path)[1].lower()

    if ext == ".docx":
        if os.path.abspath(input_path) != os.path.abspath(output_docx_path):
            shutil.copy2(input_path, output_docx_path)
        return output_docx_path
    elif ext == ".epub":
        return _convert_epub_to_docx(input_path, output_docx_path)
    elif ext in (".txt", ".md", ".markdown"):
        return _convert_txt_or_md_to_docx(input_path, output_docx_path)
    elif ext == ".pdf":
        return _convert_pdf_to_docx(input_path, output_docx_path)
    else:
        return _convert_txt_or_md_to_docx(input_path, output_docx_path)
