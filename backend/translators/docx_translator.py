import asyncio
import logging
import os
import re
import shutil
from typing import Any, Callable, Dict, List, Optional, Set
import docx
from docx.document import Document as DocxDocument
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

logger = logging.getLogger(__name__)


def _is_translatable_text(text: str) -> bool:
    """
    Kiểm tra xem đoạn văn bản có đáng dịch hay không (ít nhất 2 ký tự chữ cái).
    Bỏ qua chuỗi rỗng, chỉ số, ký hiệu hoặc URL thuần túy.
    """
    if not text:
        return False
    cleaned = text.strip()
    if len(cleaned) < 2:
        return False
    alpha_count = sum(1 for ch in cleaned if ch.isalpha())
    if alpha_count < 2:
        return False
    if re.match(r"^https?://\S+$", cleaned, re.IGNORECASE):
        return False
    return True


def _collect_table_paragraphs(table: Table, seen_cells: Set[int], result: List[Paragraph]) -> None:
    """
    Thu thập đệ quy các paragraph trong bảng, tránh lặp lại các ô bị gộp (merged cells).
    """
    for row in table.rows:
        for cell in row.cells:
            # Dùng id của phần tử XML _tc bên dưới để tránh trùng lặp khi cell bị merge
            tc_id = id(cell._tc)
            if tc_id in seen_cells:
                continue
            seen_cells.add(tc_id)

            for p in cell.paragraphs:
                result.append(p)
            for nested_table in cell.tables:
                _collect_table_paragraphs(nested_table, seen_cells, result)


def _collect_all_paragraphs(doc: DocxDocument) -> List[Paragraph]:
    """
    Thu thập toàn bộ các paragraph trong thân tài liệu, các bảng biểu,
    header và footer theo đúng thứ tự xuất hiện.
    """
    paragraphs: List[Paragraph] = []
    seen_cells: Set[int] = set()
    seen_paragraphs: Set[int] = set()

    def add_unique_p(p: Paragraph) -> None:
        p_id = id(p._p)
        if p_id not in seen_paragraphs:
            seen_paragraphs.add(p_id)
            paragraphs.append(p)

    # 1. Các đoạn văn chính trong thân tài liệu
    for p in doc.paragraphs:
        add_unique_p(p)

    # 2. Các đoạn văn nằm trong các bảng biểu (Tables)
    table_paras: List[Paragraph] = []
    for table in doc.tables:
        _collect_table_paragraphs(table, seen_cells, table_paras)
    for p in table_paras:
        add_unique_p(p)

    # 3. Các đoạn văn trong Header / Footer của các section (nếu có chữ đáng kể)
    for section in doc.sections:
        for container in (section.header, section.footer):
            if container and not container.is_linked_to_previous:
                for p in container.paragraphs:
                    add_unique_p(p)
                hf_table_paras: List[Paragraph] = []
                for t in container.tables:
                    _collect_table_paragraphs(t, seen_cells, hf_table_paras)
                for p in hf_table_paras:
                    add_unique_p(p)

    return paragraphs


def _update_paragraph_text(paragraph: Paragraph, translated_text: str, bilingual: bool = False) -> None:
    """
    Cập nhật văn bản của Paragraph trong DOCX:
    - Bảo tồn style của Paragraph và định dạng font của run có chữ đầu tiên.
    - Tránh làm mất các thành phần phi văn bản (như hình ảnh inline <w:drawing>) nếu có trong run.
    - Nếu bilingual=True: Giữ đoạn gốc và xuống dòng (\n) kèm bản dịch ngay bên dưới.
    """
    orig_text = paragraph.text.strip()
    final_text = f"{orig_text}\n{translated_text}" if bilingual else translated_text

    runs = paragraph.runs
    if not runs:
        paragraph.add_run(final_text)
        return

    # Kiểm tra xem có run nào chứa hình ảnh / drawing inline không
    has_drawing = False
    for r in runs:
        if r._r.xpath(".//*[local-name()='drawing' or local-name()='pict']"):
            has_drawing = True
            break

    if has_drawing:
        # Nếu đoạn văn có chứa ảnh inline, chỉ xóa text trong các run không chứa ảnh
        # và đặt văn bản mới vào run chữ đầu tiên để không làm mất ảnh.
        text_placed = False
        for r in runs:
            if r._r.xpath(".//*[local-name()='drawing' or local-name()='pict']"):
                continue
            if not text_placed:
                r.text = final_text
                text_placed = True
            else:
                r.text = ""
        if not text_placed:
            paragraph.add_run("\n" + final_text if bilingual else " " + final_text)
        return

    if bilingual:
        # Chế độ song ngữ: giữ nguyên các run gốc và thêm xuống dòng + run bản dịch kế thừa font
        ref_run = next((r for r in runs if r.text.strip()), runs[0])
        new_run = paragraph.add_run(f"\n{translated_text}")
        try:
            if ref_run.font.name:
                new_run.font.name = ref_run.font.name
            if ref_run.font.size:
                new_run.font.size = ref_run.font.size
            if ref_run.font.color and ref_run.font.color.rgb:
                new_run.font.color.rgb = ref_run.font.color.rgb
            new_run.bold = ref_run.bold
            new_run.italic = ref_run.italic
        except Exception:
            pass
    else:
        # Chế độ chỉ bản dịch: chọn run đầu tiên có văn bản làm run chuẩn để giữ font/bold/italic/color
        target_run_idx = 0
        for idx, r in enumerate(runs):
            if r.text.strip():
                target_run_idx = idx
                break

        for idx, r in enumerate(runs):
            if idx == target_run_idx:
                r.text = final_text
            else:
                r.text = ""


def _build_smart_batches(
    items: List[Dict[str, Any]],
    max_items_per_batch: int = 10,
    max_chars_per_batch: int = 1800,
) -> List[List[Dict[str, Any]]]:
    """
    Gom các đoạn văn thành các batch thông minh (~8-12 đoạn hoặc ~1800 ký tự).
    """
    batches: List[List[Dict[str, Any]]] = []
    current_batch: List[Dict[str, Any]] = []
    current_chars = 0

    for item in items:
        text_len = len(item["src_text"])
        if current_batch and (
            len(current_batch) >= max_items_per_batch
            or current_chars + text_len > max_chars_per_batch
        ):
            batches.append(current_batch)
            current_batch = []
            current_chars = 0

        current_batch.append(item)
        current_chars += text_len

    if current_batch:
        batches.append(current_batch)

    return batches


async def _invoke_progress_cb(
    progress_cb: Optional[Callable],
    done_items: int,
    total_items: int,
    latest_src: str,
    latest_dst: str,
) -> None:
    if not progress_cb:
        return
    try:
        res = progress_cb(done_items, total_items, latest_src, latest_dst)
        if asyncio.iscoroutine(res):
            await res
    except Exception as e:
        logger.warning(f"Lỗi khi gọi progress_cb trong docx_translator: {e}")


async def _translate_batch_with_client(
    llm_client: Any,
    texts: List[str],
    context: str = "",
    glossary: Optional[List[Dict[str, str]]] = None,
) -> List[str]:
    """
    Gọi llm_client.translate_batch an toàn, hỗ trợ cả coroutine và hàm đồng bộ.
    """
    if not texts:
        return []

    if hasattr(llm_client, "translate_batch"):
        try:
            res = llm_client.translate_batch(texts, context=context, glossary=glossary)
        except TypeError:
            try:
                res = llm_client.translate_batch(texts, glossary=glossary)
            except TypeError:
                res = llm_client.translate_batch(texts)

        if asyncio.iscoroutine(res):
            res = await res

        if isinstance(res, list) and len(res) == len(texts):
            return [
                str(x).strip() if x is not None else orig
                for x, orig in zip(res, texts)
            ]

    # Fallback dịch từng câu nếu batch không tương thích
    results: List[str] = []
    for t in texts:
        if hasattr(llm_client, "translate_text"):
            try:
                r = llm_client.translate_text(t, context=context, glossary=glossary)
            except TypeError:
                r = llm_client.translate_text(t)
            if asyncio.iscoroutine(r):
                r = await r
            results.append(str(r).strip() if r else t)
        else:
            results.append(t)
    return results


async def translate_docx_file(
    input_path: str,
    output_path: str,
    llm_client: Any,
    bilingual: bool = False,
    glossary: Optional[List[Dict[str, str]]] = None,
    progress_cb: Optional[Callable] = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> Dict[str, Any]:
    """
    Dịch file Microsoft Word (.docx) sang Tiếng Việt giữ nguyên định dạng tài liệu,
    bảng biểu, style, font chữ và hình ảnh.
    """
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Không tìm thấy file DOCX đầu vào: {input_path}")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    doc = docx.Document(input_path)
    all_paragraphs = _collect_all_paragraphs(doc)

    translatable_items: List[Dict[str, Any]] = []
    for para in all_paragraphs:
        raw_text = para.text.strip()
        if _is_translatable_text(raw_text):
            translatable_items.append(
                {
                    "paragraph": para,
                    "src_text": raw_text,
                    "dst_text": None,
                }
            )

    cfg = getattr(llm_client, "config", None)
    chapter_limit = int(getattr(cfg, "chapter_limit", 0) or 0) if cfg else 0
    if chapter_limit > 0 and translatable_items:
        translatable_items = translatable_items[: chapter_limit * 35]

    total_items = len(translatable_items)
    if total_items == 0:
        shutil.copy2(input_path, output_path)
        await _invoke_progress_cb(progress_cb, 0, 0, "", "")
        return {
            "status": "completed",
            "total_items": 0,
            "translated_items": 0,
            "output_path": output_path,
            "bilingual": bilingual,
        }

    batches = _build_smart_batches(
        translatable_items, max_items_per_batch=10, max_chars_per_batch=1800
    )

    batch_contexts: List[str] = []
    for idx, batch in enumerate(batches):
        if idx == 0:
            batch_contexts.append("")
        else:
            prev_batch = batches[idx - 1]
            ctx_texts = [item["src_text"] for item in prev_batch[-2:]]
            batch_contexts.append("\n".join(ctx_texts))

    concurrency = 4
    if hasattr(llm_client, "config") and hasattr(llm_client.config, "concurrency"):
        concurrency = max(1, int(llm_client.config.concurrency))
    elif hasattr(llm_client, "concurrency"):
        concurrency = max(1, int(llm_client.concurrency))

    semaphore = asyncio.Semaphore(concurrency)
    done_items = 0
    progress_lock = asyncio.Lock()
    cancelled = False

    async def process_batch(batch_idx: int, batch: List[Dict[str, Any]]) -> None:
        nonlocal done_items, cancelled
        if cancel_event and cancel_event.is_set():
            cancelled = True
            return

        async with semaphore:
            if cancel_event and cancel_event.is_set():
                cancelled = True
                return

            texts_to_translate = [item["src_text"] for item in batch]
            context_str = batch_contexts[batch_idx]

            translated_texts = await _translate_batch_with_client(
                llm_client,
                texts_to_translate,
                context=context_str,
                glossary=glossary,
            )

            if cancel_event and cancel_event.is_set():
                cancelled = True
                return

            latest_src = ""
            latest_dst = ""
            for item, dst_text in zip(batch, translated_texts):
                final_dst = dst_text.strip() if dst_text else item["src_text"]
                item["dst_text"] = final_dst
                latest_src = item["src_text"]
                latest_dst = final_dst

            async with progress_lock:
                done_items += len(batch)
                await _invoke_progress_cb(
                    progress_cb, done_items, total_items, latest_src, latest_dst
                )

    tasks = [
        asyncio.create_task(process_batch(idx, batch))
        for idx, batch in enumerate(batches)
    ]
    await asyncio.gather(*tasks)

    if cancelled or (cancel_event and cancel_event.is_set()):
        return {
            "status": "cancelled",
            "total_items": total_items,
            "translated_items": done_items,
            "output_path": output_path,
            "bilingual": bilingual,
        }

    # Cập nhật nội dung đã dịch vào các paragraph trong tài liệu Word
    for item in translatable_items:
        dst_text = item.get("dst_text")
        if not dst_text:
            continue
        _update_paragraph_text(item["paragraph"], dst_text, bilingual=bilingual)

    doc.save(output_path)

    return {
        "status": "completed",
        "total_items": total_items,
        "translated_items": done_items,
        "output_path": output_path,
        "bilingual": bilingual,
    }
