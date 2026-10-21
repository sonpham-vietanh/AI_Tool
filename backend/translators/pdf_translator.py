import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Callable, List, Optional

import fitz  # PyMuPDF
from docx import Document
from docx.shared import Pt, RGBColor

from .pdf_font_helper import get_vietnamese_font_path
from .pdf_inplace import Unit, extract_units, is_only_urls, protect_urls, render_units, restore_urls

logger = logging.getLogger(__name__)


def _build_side_by_side_bilingual_pdf(
    orig_pdf_path: str, mono_pdf_path: str, bilingual_pdf_path: str
) -> str:
    """
    Tạo file PDF Song Ngữ Đối Chiếu (Side-by-Side):
    Ghép trang gốc bên trái và trang đã dịch bên phải bằng show_pdf_page.
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

            mid_x = w + gap / 2.0
            new_page.draw_line(
                fitz.Point(mid_x, 0),
                fitz.Point(mid_x, h),
                color=(0.82, 0.82, 0.86),
                width=0.75,
            )

        out_doc.save(bilingual_pdf_path, garbage=4, deflate=True)
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


def _parse_page_range(page_range_str: str, total: int) -> Optional[set]:
    allowed: set = set()
    for part in re.split(r"[,;]+", page_range_str):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            s_str, e_str = part.split("-", 1)
            if s_str.strip().isdigit() and e_str.strip().isdigit():
                for p_num in range(max(1, int(s_str)), min(total, int(e_str)) + 1):
                    allowed.add(p_num - 1)
        elif part.isdigit() and 1 <= int(part) <= total:
            allowed.add(int(part) - 1)
    return allowed or None


def _role_hint(u: Unit, page_h: float, body_size: float, repeats: int) -> str:
    """Máy suy ra VAI TRÒ của đoạn trong trang (từ cỡ chữ, độ đậm, khung chứa, vị trí) để AI tự hiểu ngữ cảnh."""
    parts: List[str] = [f"trang {u.page + 1}"]
    top, bottom = u.rect.y0 < page_h * 0.08, u.rect.y1 > page_h * 0.92
    if u.vertical:
        parts.append("chữ xoay dọc trên biểu mẫu")
    if body_size and u.size >= body_size * 1.5:
        parts.append("tiêu đề cỡ lớn")
    elif u.bold and len(u.text) < 70:
        parts.append("đề mục hoặc nhãn in đậm")
    if (top or bottom) and repeats >= 3:
        parts.append("đầu/chân trang lặp lại ở nhiều trang (giữ nhất quán)")
    elif bottom and body_size and u.size < body_size * 0.9:
        parts.append("chú thích/ghi chú cuối trang")
    if u.container is not None and len(u.text) < 45:
        parts.append("nhãn hoặc ô nằm trong bảng/khung")
    if u.starts_bullet:
        parts.append("một mục trong danh sách gạch đầu dòng/ô tích")
    if len(u.text) < 22 and len(parts) == 1:
        parts.append("nhãn ngắn")
    return "; ".join(parts)


def _make_batches(units: List[Unit], max_items: int, max_chars: int) -> List[List[Unit]]:
    """Gom các đoạn liền kề (theo thứ tự trang) thành batch; ngắt batch khi quá số đoạn/ký tự."""
    batches: List[List[Unit]] = []
    cur: List[Unit] = []
    chars = 0
    for u in units:
        n = len(u.text)
        if cur and (len(cur) >= max_items or chars + n > max_chars):
            batches.append(cur)
            cur, chars = [], 0
        cur.append(u)
        chars += n
    if cur:
        batches.append(cur)
    return batches


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
    Dịch PDF TẠI CHỖ (in-place): giữ nguyên 100% bố cục, nền màu, hình ảnh, bảng, dấu đầu dòng, màu/độ đậm/cỡ chữ.
    Chữ gốc bị xóa (không tô nền) rồi bản dịch được ghi đúng khung cũ, tự co chữ khi dài hơn.
    Tạo đồng thời: PDF đơn ngữ (`mono_pdf`), PDF song ngữ đối chiếu (`bilingual_pdf`) và Word (`docx_output`).
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    clean_base = re.sub(r'[\\/*?:"<>|]', "_", base_name)
    mono_pdf_path = str((out_dir / f"{clean_base}_vi.pdf").resolve())
    bilingual_pdf_path = str((out_dir / f"{clean_base}_bilingual.pdf").resolve())
    docx_output_path = str((out_dir / f"{clean_base}_vi.docx").resolve())

    fallback_font = get_vietnamese_font_path()

    cfg = getattr(llm_client, "config", None)
    chapter_limit = int(getattr(cfg, "chapter_limit", 0) or 0) if cfg else 0
    page_range_str = str(getattr(cfg, "page_range", "") or "").strip() if cfg else ""
    batch_items = int(getattr(cfg, "batch_items", 24) or 24)
    batch_chars = int(getattr(cfg, "batch_chars", 5000) or 5000)
    concurrency = max(1, int(getattr(cfg, "concurrency", 4) or 4))

    doc = fitz.open(input_path)
    try:
        allowed_pages: Optional[set] = None
        if page_range_str:
            allowed_pages = _parse_page_range(page_range_str, len(doc))
        elif chapter_limit > 0:
            allowed_pages = set(range(min(len(doc), chapter_limit * 5)))  # mỗi "chương" thử ~5 trang

        # 1) Trích xuất các đoạn văn (đã gom dòng, bỏ chữ ẩn, giữ dấu đầu dòng/ký hiệu)
        units: List[Unit] = await asyncio.to_thread(extract_units, doc, allowed_pages)

        work: List[Unit] = []
        for u in units:
            if is_only_urls(u.text):
                u.skip = True
                continue
            protected_text, mapping = protect_urls(u.text)
            u.protected = mapping
            u._send = protected_text  # type: ignore[attr-defined]
            work.append(u)

        # Cùng một chuỗi (đầu/chân trang lặp ở mọi trang, nhãn lặp lại) chỉ dịch MỘT lần -> nhất quán và tiết kiệm token
        groups: dict = {}
        for u in work:
            groups.setdefault(u._send, []).append(u)  # type: ignore[attr-defined]
        reps = [g[0] for g in groups.values()]
        sizes = sorted(u.size for u in work for _ in range(max(1, len(u.text) // 40)))
        body_size = sizes[len(sizes) // 2] if sizes else 0.0
        page_h = doc[0].rect.height if len(doc) else 792.0
        for key, members in groups.items():
            members[0]._hint = _role_hint(members[0], page_h, body_size, len(members))  # type: ignore[attr-defined]
        total_items = len(reps)
        translated_items = 0

        async def notify(done: int, total: int, src: str = "", dst: str = "") -> None:
            if not progress_cb:
                return
            try:
                res = progress_cb(done, total, src, dst)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as cb_err:
                logger.debug("Progress callback error: %s", cb_err)

        await notify(0, max(total_items, 1))

        # 2) Dịch song song theo batch (có ngữ cảnh đoạn liền trước, glossary và BOOK BRIEF qua llm_client)
        batches = _make_batches(reps, batch_items, batch_chars)
        sem = asyncio.Semaphore(concurrency)
        lock = asyncio.Lock()
        cancelled = False

        async def run_batch(idx: int, batch: List[Unit]) -> None:
            nonlocal translated_items, cancelled
            if cancel_event and cancel_event.is_set():
                cancelled = True
                return
            texts = [u._send for u in batch]  # type: ignore[attr-defined]
            context = ""
            if idx > 0:
                context = "\n".join(u.text for u in batches[idx - 1][-2:])
            async with sem:
                if cancel_event and cancel_event.is_set():
                    cancelled = True
                    return
                hints = [getattr(u, "_hint", "") for u in batch]
                try:
                    translations = await llm_client.translate_batch(
                        texts, context=context, glossary=glossary, hints=hints
                    )
                except TypeError:
                    translations = await llm_client.translate_batch(texts)
            if not translations or len(translations) != len(batch):
                translations = [
                    (translations[i] if translations and i < len(translations) else texts[i])
                    for i in range(len(batch))
                ]
            for u, t in zip(batch, translations):
                dst = (t or "").strip() or u._send  # type: ignore[attr-defined]
                final = restore_urls(dst, u.protected)
                for member in groups[u._send]:  # type: ignore[attr-defined]
                    member.dst = final
            async with lock:
                translated_items += len(batch)
                await notify(translated_items, total_items, batch[-1].text, batch[-1].dst)

        await asyncio.gather(*[run_batch(i, b) for i, b in enumerate(batches)])
        if cancelled or (cancel_event and cancel_event.is_set()):
            raise asyncio.CancelledError("Tác vụ dịch PDF đã bị hủy bởi người dùng.")

        # 3) Xóa chữ gốc (không tô nền) và ghi bản dịch vào đúng khung cũ
        stats = await asyncio.to_thread(render_units, doc, work, fallback_font)
        logger.info("PDF in-place: %s", stats)

        doc.save(mono_pdf_path, garbage=4, deflate=True)
    finally:
        doc.close()

    # 4) PDF song ngữ đối chiếu (trang gốc | trang dịch)
    await asyncio.to_thread(_build_side_by_side_bilingual_pdf, input_path, mono_pdf_path, bilingual_pdf_path)

    # 5) Word (.docx) theo từng trang
    pages_metadata: list[list[dict]] = [[] for _ in range(max((u.page for u in work), default=-1) + 1)]
    for u in work:
        pages_metadata[u.page].append({"src": u.text, "dst": u.dst})
    await asyncio.to_thread(_export_translated_docx, pages_metadata, docx_output_path, base_name, bilingual)

    primary_output = bilingual_pdf_path if bilingual else mono_pdf_path

    return {
        "primary_output": primary_output,
        "mono_pdf": mono_pdf_path,
        "bilingual_pdf": bilingual_pdf_path,
        "docx_output": docx_output_path,
        "total_items": total_items,
        "translated_items": translated_items,
    }
