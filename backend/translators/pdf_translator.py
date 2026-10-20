import asyncio
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

import fitz  # PyMuPDF
from docx import Document
from docx.shared import Pt, RGBColor

from .pdf_font_helper import get_vietnamese_font_path

logger = logging.getLogger(__name__)


def _clean_pdf_block_text(raw_text: str) -> str:
    """
    Làm sạch văn bản trích xuất từ PDF block:
    - Nối các từ bị gạch nối ngắt dòng (ví dụ: 'trans-\\nlation' -> 'translation')
    - Biến các dấu ngắt dòng giữa chừng trong cùng 1 đoạn thành khoảng trắng
    - Chuẩn hóa khoảng trắng thừa
    """
    if not raw_text:
        return ""
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    # Nối từ bị ngắt dòng bằng dấu gạch ngang
    text = re.sub(r"([a-zA-Z])-\n([a-zA-Z])", r"\1\2", text)
    # Thay thế xuống dòng đơn bằng khoảng trắng
    text = re.sub(r"\s*\n+\s*", " ", text)
    # Gom nhiều khoảng trắng liên tiếp
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _should_translate_block(text: str, block_type: int) -> bool:
    """
    Kiểm tra xem block có phải là văn bản cần dịch hay không:
    - Bỏ qua block hình ảnh (block_type != 0)
    - Bỏ qua số trang đơn lẻ, ký hiệu toán học/ký tự ngắn không chứa chữ cái
    """
    if block_type != 0:
        return False
    cleaned = text.strip()
    if len(cleaned) < 2:
        return False
    # Bỏ qua nếu chỉ toàn chữ số hoặc ký tự đặc biệt (số trang, mã số...)
    if re.fullmatch(r"[\d\W_]+", cleaned):
        return False
    # Phải có ít nhất 2 chữ cái
    letters = re.findall(r"[A-Za-zÀ-ỹ\u00C0-\u024F\u1E00-\u1EFF]", cleaned)
    if len(letters) < 2:
        return False
    return True


def _estimate_initial_fontsize(rect: fitz.Rect, original_text: str) -> float:
    """
    Ước lượng cỡ chữ khởi đầu dựa trên chiều cao khung rect và độ dài văn bản gốc.
    """
    height = max(rect.height, 10.0)
    width = max(rect.width, 20.0)
    char_count = max(len(original_text), 1)

    # Ước lượng số dòng xấp xỉ
    approx_lines = max(1.0, round(height / 14.0))
    est_from_height = (height / approx_lines) * 0.72

    # Ước lượng từ diện tích khung chia cho số ký tự
    area_per_char = (width * height) / char_count
    est_from_area = (area_per_char ** 0.5) * 0.88

    est = min(est_from_height, est_from_area)
    return float(max(8.5, min(est, 16.0)))


def _insert_vietnamese_textbox_adaptive(
    page: fitz.Page,
    rect: fitz.Rect,
    translated_text: str,
    font_path: str,
    initial_size: float = 11.0,
    min_size: float = 6.0,
) -> None:
    """
    Chèn văn bản Tiếng Việt vào khung rect với cơ chế Adaptive Font-Size Scaling:
    - Thử cỡ chữ từ initial_size giảm dần từng 0.5pt về min_size (6pt) cho đến khi
      insert_textbox trả về >= 0 (vừa khít khung).
    - Nếu ở 6pt vẫn hơi tràn, mở rộng nhẹ chiều cao rect.y1 (trong giới hạn trang)
      để không bị mất bất kỳ chữ nào.
    """
    font_name = "vi_font"
    color = (0.08, 0.08, 0.12)
    page_rect = page.rect

    sz = max(initial_size, min_size)
    while sz >= min_size:
        # Sử dụng Shape hoặc thử trực tiếp trên page; khi insert_textbox < 0 thì PyMuPDF
        # KHÔNG vẽ chữ lên trang, nên ta có thể lặp thử an toàn.
        rc = page.insert_textbox(
            rect,
            translated_text,
            fontname=font_name,
            fontfile=font_path,
            fontsize=sz,
            color=color,
            align=fitz.TEXT_ALIGN_LEFT,
        )
        if rc >= 0:
            return
        sz = round(sz - 0.5, 2)

    # Nếu ở min_size (6pt) vẫn tràn, mở rộng dần chiều cao rect.y1 (và nhẹ sang phải nếu cần)
    expanded_rect = fitz.Rect(rect)
    max_y1 = page_rect.y1 - 4.0
    max_x1 = page_rect.x1 - 4.0

    for extra_h in (12, 24, 40, 65, 100, 160):
        expanded_rect.y1 = min(max_y1, rect.y1 + extra_h)
        expanded_rect.x1 = min(max_x1, rect.x1 + 8.0)
        rc = page.insert_textbox(
            expanded_rect,
            translated_text,
            fontname=font_name,
            fontfile=font_path,
            fontsize=min_size,
            color=color,
            align=fitz.TEXT_ALIGN_LEFT,
        )
        if rc >= 0:
            return

    # Fallback cuối cùng: dùng cỡ chữ 5pt trên khung đã mở rộng tối đa
    page.insert_textbox(
        expanded_rect,
        translated_text,
        fontname=font_name,
        fontfile=font_path,
        fontsize=5.0,
        color=color,
        align=fitz.TEXT_ALIGN_LEFT,
    )


def _build_side_by_side_bilingual_pdf(
    orig_pdf_path: str, mono_pdf_path: str, bilingual_pdf_path: str
) -> str:
    """
    Tạo file PDF Song Ngữ Đối Chiếu (Side-by-Side):
    Ghép trang gốc bên trái và trang đã dịch Tiếng Việt bên phải bằng show_pdf_page.
    """
    src_doc = fitz.open(orig_pdf_path)
    trans_doc = fitz.open(mono_pdf_path)
    out_doc = fitz.open()

    try:
        page_count = min(len(src_doc), len(trans_doc))
        for i in range(page_count):
            src_page = src_doc[i]
            trans_page = trans_doc[i]

            w = src_page.rect.width
            h = max(src_page.rect.height, trans_page.rect.height)
            gap = 12.0  # Khoảng cách phân tách nhẹ giữa 2 trang

            new_page = out_doc.new_page(width=w * 2 + gap, height=h)

            left_rect = fitz.Rect(0, 0, w, h)
            right_rect = fitz.Rect(w + gap, 0, w * 2 + gap, h)

            new_page.show_pdf_page(left_rect, src_doc, i)
            new_page.show_pdf_page(right_rect, trans_doc, i)

            # Vẽ đường kẻ phân cách mảnh ở giữa 2 trang đối chiếu
            mid_x = w + gap / 2.0
            new_page.draw_line(
                fitz.Point(mid_x, 0),
                fitz.Point(mid_x, h),
                color=(0.82, 0.82, 0.86),
                width=0.75,
            )

        out_doc.save(bilingual_pdf_path, garbage=3, deflate=True)
    finally:
        out_doc.close()
        trans_doc.close()
        src_doc.close()

    return bilingual_pdf_path


def _export_translated_docx(
    pages_blocks: list[list[dict]],
    docx_output_path: str,
    base_name: str,
    bilingual: bool = False,
) -> str:
    """
    Xuất toàn bộ nội dung đã dịch theo từng trang ra file Microsoft Word (.docx)
    để người dùng có thể mở trên Word chỉnh sửa ngay.
    """
    doc = Document()

    # Thiết lập font mặc định hỗ trợ Tiếng Việt
    style = doc.styles["Normal"]
    style.font.name = "Arial"
    style.font.size = Pt(11)

    title_heading = doc.add_heading(f"Bản dịch: {base_name}", level=1)
    title_heading.style.font.name = "Arial"

    for page_idx, blocks in enumerate(pages_blocks, start=1):
        if not blocks:
            continue
        h2 = doc.add_heading(f"Trang {page_idx}", level=2)
        h2.style.font.name = "Arial"

        for item in blocks:
            src_text = item.get("src", "").strip()
            dst_text = item.get("dst", "").strip()
            if not dst_text:
                continue

            if bilingual and src_text:
                p_src = doc.add_paragraph()
                run_src = p_src.add_run(src_text)
                run_src.italic = True
                run_src.font.size = Pt(9.5)
                run_src.font.color.rgb = RGBColor(110, 115, 125)

            p_dst = doc.add_paragraph()
            run_dst = p_dst.add_run(dst_text)
            run_dst.font.name = "Arial"
            run_dst.font.size = Pt(11)

        if page_idx < len(pages_blocks):
            doc.add_page_break()

    Path(docx_output_path).parent.mkdir(parents=True, exist_ok=True)
    doc.save(docx_output_path)
    return docx_output_path


def _try_pdf2zh_cli(
    input_path: str,
    output_dir: str,
    vi_font_path: str,
) -> tuple[Optional[str], Optional[str]]:
    """
    Hỗ trợ tích hợp công cụ PDFMathTranslate (`pdf2zh`) nếu engine_mode == 'pdf2zh'
    và môi trường đã cài đặt `pdf2zh`. Nếu không khả dụng hoặc lỗi sẽ tự động fallback
    về PyMuPDF Smart Layout Engine.
    """
    pdf2zh_bin = shutil.which("pdf2zh")
    if not pdf2zh_bin:
        return None, None

    try:
        cmd = [
            pdf2zh_bin,
            input_path,
            "-li",
            "en",
            "-lo",
            "vi",
            "-o",
            output_dir,
            "-f",
            vi_font_path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if res.returncode == 0:
            stem = Path(input_path).stem
            mono_candidate = Path(output_dir) / f"{stem}-mono.pdf"
            dual_candidate = Path(output_dir) / f"{stem}-dual.pdf"
            if mono_candidate.exists():
                return (
                    str(mono_candidate.resolve()),
                    str(dual_candidate.resolve()) if dual_candidate.exists() else None,
                )
    except Exception as exc:
        logger.warning("pdf2zh CLI fallback sang PyMuPDF Smart Layout Engine: %s", exc)

    return None, None


async def translate_pdf_file(
    input_path: str,
    output_dir: str,
    base_name: str,
    llm_client,
    bilingual: bool = False,
    glossary: list[dict] = None,
    engine_mode: str = "auto",
    progress_cb: Optional[Callable] = None,
    cancel_event: asyncio.Event = None,
) -> dict:
    """
    Dịch file PDF giữ nguyên 100% bố cục, hình ảnh, biểu đồ, bảng biểu:
    - Kết hợp PyMuPDF Smart Layout Engine (`fitz`) + tích hợp `pdf2zh` (`PDFMathTranslate`)
    - Nhúng font Tiếng Việt Unicode chuẩn 100% (`pdf_font_helper.get_vietnamese_font_path`)
    - Xóa chữ gốc bằng Redaction (`images=fitz.PDF_REDACT_IMAGE_NONE`) bảo toàn mọi hình ảnh/đường kẻ
    - Chèn chữ Tiếng Việt với Adaptive Font-Size Scaling
    - Tạo đồng thời:
        1. File PDF đơn ngữ (`mono_pdf`)
        2. File PDF song ngữ đối chiếu Side-by-Side (`bilingual_pdf`)
        3. File Word `.docx` (`docx_output`) để chỉnh sửa dễ dàng
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    clean_base = re.sub(r'[\\/*?:"<>|]', "_", base_name)
    mono_pdf_path = str((out_dir / f"{clean_base}_vi.pdf").resolve())
    bilingual_pdf_path = str((out_dir / f"{clean_base}_bilingual.pdf").resolve())
    docx_output_path = str((out_dir / f"{clean_base}_vi.docx").resolve())

    vi_font_path = get_vietnamese_font_path()

    # 1. Mở PDF bằng PyMuPDF và trích xuất toàn bộ các block văn bản cần dịch trên từng trang
    doc = fitz.open(input_path)
    pages_metadata: list[list[dict]] = []
    all_work_items: list[dict] = []

    cfg = getattr(llm_client, "config", None)
    chapter_limit = int(getattr(cfg, "chapter_limit", 0) or 0) if cfg else 0
    page_range_str = str(getattr(cfg, "page_range", "") or "").strip() if cfg else ""
    allowed_pages: Optional[set[int]] = None
    if page_range_str:
        allowed_pages = set()
        for part in re.split(r"[,;]+", page_range_str):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                s_str, e_str = part.split("-", 1)
                if s_str.strip().isdigit() and e_str.strip().isdigit():
                    s_i, e_i = int(s_str.strip()), int(e_str.strip())
                    for p_num in range(max(1, s_i), min(len(doc), e_i) + 1):
                        allowed_pages.add(p_num - 1)
            elif part.isdigit():
                p_num = int(part)
                if 1 <= p_num <= len(doc):
                    allowed_pages.add(p_num - 1)
    elif chapter_limit > 0:
        # Mỗi chương dịch thử PDF tương đương ~5 trang đầu
        max_p = min(len(doc), chapter_limit * 5)
        allowed_pages = set(range(max_p))

    try:
        for page_index in range(len(doc)):
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError("Tác vụ dịch PDF đã bị hủy bởi người dùng.")

            if allowed_pages is not None and page_index not in allowed_pages:
                pages_metadata.append([])
                continue

            page = doc[page_index]
            raw_blocks = page.get_text("blocks")
            page_items: list[dict] = []

            for b in raw_blocks:
                # Cấu trúc block: (x0, y0, x1, y1, text, block_no, block_type)
                if len(b) < 7:
                    continue
                x0, y0, x1, y1, raw_text, block_no, block_type = b[:7]
                if not _should_translate_block(raw_text, block_type):
                    continue

                cleaned_text = _clean_pdf_block_text(raw_text)
                if not _should_translate_block(cleaned_text, 0):
                    continue

                rect = fitz.Rect(x0, y0, x1, y1)
                item = {
                    "page_index": page_index,
                    "block_no": block_no,
                    "rect": rect,
                    "src": cleaned_text,
                    "dst": cleaned_text,
                    "est_fontsize": _estimate_initial_fontsize(rect, cleaned_text),
                }
                page_items.append(item)
                all_work_items.append(item)

            pages_metadata.append(page_items)

        total_items = len(all_work_items)
        translated_items = 0

        if progress_cb:
            try:
                res = progress_cb(0, max(total_items, 1), "", "")
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass

        # 2. Gom các block thành batch và dịch bất đồng bộ qua llm_client.translate_batch
        batch_size = getattr(getattr(llm_client, "config", None), "batch_items", 12) or 12
        batch_size = max(1, min(int(batch_size), 30))

        for start_idx in range(0, total_items, batch_size):
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError("Tác vụ dịch PDF đã bị hủy bởi người dùng.")

            batch = all_work_items[start_idx : start_idx + batch_size]
            texts = [it["src"] for it in batch]

            try:
                translations = await llm_client.translate_batch(texts, glossary=glossary)
            except TypeError:
                # Phòng trường hợp chữ ký translate_batch không nhận keyword glossary
                translations = await llm_client.translate_batch(texts)

            if not translations or len(translations) != len(batch):
                translations = [
                    (translations[i] if translations and i < len(translations) else batch[i]["src"])
                    for i in range(len(batch))
                ]

            for item, trans_text in zip(batch, translations):
                cleaned_trans = (trans_text or item["src"]).strip()
                item["dst"] = cleaned_trans

            translated_items += len(batch)
            latest_src = batch[-1]["src"]
            latest_dst = batch[-1]["dst"]

            if progress_cb:
                try:
                    cb_res = progress_cb(translated_items, total_items, latest_src, latest_dst)
                    if asyncio.iscoroutine(cb_res):
                        await cb_res
                except Exception as cb_err:
                    logger.debug("Progress callback error: %s", cb_err)

        # 3. Xóa chữ gốc bằng Redaction (bảo toàn 100% ảnh/biểu đồ/bảng) và ghi bản dịch Tiếng Việt
        for page_index, page_items in enumerate(pages_metadata):
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError("Tác vụ dịch PDF đã bị hủy bởi người dùng.")

            if not page_items:
                continue

            page = doc[page_index]

            # Bước 3.1: Đánh dấu redact toàn bộ các vùng text block cần dịch trên trang
            for item in page_items:
                page.add_redact_annot(item["rect"], fill=(1, 1, 1))

            # Áp dụng redaction nhưng GIỮ NGUYÊN 100% hình ảnh và đồ họa vector
            try:
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
            except TypeError:
                page.apply_redactions()

            # Bước 3.2: Chèn chữ Tiếng Việt Unicode với Adaptive Font-Size Scaling
            for item in page_items:
                _insert_vietnamese_textbox_adaptive(
                    page=page,
                    rect=item["rect"],
                    translated_text=item["dst"],
                    font_path=vi_font_path,
                    initial_size=item["est_fontsize"],
                    min_size=6.0,
                )

        # Lưu file PDF đơn ngữ Tiếng Việt
        doc.save(mono_pdf_path, garbage=3, deflate=True)
    finally:
        doc.close()

    # Nếu người dùng chỉ định rõ engine_mode == "pdf2zh" và có cài đặt pdf2zh CLI, thử nâng cấp thêm
    if engine_mode == "pdf2zh":
        p2z_mono, p2z_dual = await asyncio.to_thread(
            _try_pdf2zh_cli, input_path, str(out_dir), vi_font_path
        )
        if p2z_mono and os.path.exists(p2z_mono):
            shutil.copy2(p2z_mono, mono_pdf_path)
        if p2z_dual and os.path.exists(p2z_dual):
            shutil.copy2(p2z_dual, bilingual_pdf_path)

    # 4. Tạo file PDF Song Ngữ Đối Chiếu (Side-by-Side: Trang gốc bên trái, Trang dịch bên phải)
    if not os.path.exists(bilingual_pdf_path):
        await asyncio.to_thread(
            _build_side_by_side_bilingual_pdf,
            input_path,
            mono_pdf_path,
            bilingual_pdf_path,
        )

    # 5. Tạo file Word (.docx) chứa toàn bộ nội dung đã dịch theo từng trang
    await asyncio.to_thread(
        _export_translated_docx,
        pages_metadata,
        docx_output_path,
        base_name,
        bilingual,
    )

    primary_output = bilingual_pdf_path if bilingual else mono_pdf_path

    return {
        "primary_output": primary_output,
        "mono_pdf": mono_pdf_path,
        "bilingual_pdf": bilingual_pdf_path,
        "docx_output": docx_output_path,
        "total_items": total_items,
        "translated_items": translated_items,
    }
