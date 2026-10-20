import asyncio
import logging
import os
import re
import shutil
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Biểu thức chính quy nhận diện các khối không dịch (fenced code blocks ```...```, ~~~...~~~ hoặc math blocks $$...$$)
PROTECTED_BLOCK_RE = re.compile(
    r"(```[\s\S]*?```|~~~[\s\S]*?~~~|\$\$[\s\S]*?\$\$)",
    re.MULTILINE,
)


def _is_translatable_segment(text: str) -> bool:
    """
    Kiểm tra xem đoạn văn bản TXT/Markdown có nội dung chữ cái cần dịch hay không.
    Bỏ qua đường kẻ ngang Markdown (---, ***), khối rỗng, chỉ số hoặc URL.
    """
    if not text:
        return False
    cleaned = text.strip()
    if len(cleaned) < 2:
        return False
    # Bỏ qua horizontal rules trong Markdown (---, ***, ___)
    if re.match(r"^[-*_]{3,}\s*$", cleaned):
        return False
    # Bỏ qua nếu chỉ toàn số và ký hiệu
    alpha_count = sum(1 for ch in cleaned if ch.isalpha())
    if alpha_count < 2:
        return False
    # Bỏ qua URL đơn lẻ
    if re.match(r"^https?://\S+$", cleaned, re.IGNORECASE):
        return False
    return True


def _parse_text_into_blocks(content: str) -> List[Dict[str, Any]]:
    """
    Chia nội dung file .txt / .md thành danh sách các block theo thứ tự:
    - Bảo toàn nguyên vẹn các khối code ```...``` và khối toán học $$...$$.
    - Chia phần văn bản còn lại theo đoạn văn (\n\n) và giữ nguyên khoảng trắng phân cách.
    """
    blocks: List[Dict[str, Any]] = []

    # Chuẩn hóa xuống dòng về \n để xử lý thống nhất
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")

    # Tách theo các khối code / công thức toán học lớn trước
    parts = PROTECTED_BLOCK_RE.split(normalized)

    for idx, part in enumerate(parts):
        if not part:
            continue

        # Ở vị trí lẻ của re.split có capturing group là các khối PROTECTED_BLOCK_RE
        if idx % 2 == 1:
            blocks.append(
                {
                    "type": "protected",
                    "raw": part,
                    "src_text": part,
                    "dst_text": None,
                }
            )
            continue

        # Với phần văn bản thông thường, tách theo ranh giới đoạn văn (\n\s*\n+)
        sub_parts = re.split(r"(\n\s*\n+)", part)
        for sub_idx, sub in enumerate(sub_parts):
            if not sub:
                continue
            if sub_idx % 2 == 1:
                # Khoảng trắng / dòng trống ngăn cách giữa các đoạn
                blocks.append(
                    {
                        "type": "separator",
                        "raw": sub,
                        "src_text": sub,
                        "dst_text": None,
                    }
                )
            else:
                # Đoạn văn bản
                leading_ws_match = re.match(r"^(\s*)", sub)
                trailing_ws_match = re.search(r"(\s*)$", sub)
                leading_ws = leading_ws_match.group(1) if leading_ws_match else ""
                trailing_ws = trailing_ws_match.group(1) if trailing_ws_match else ""
                core_text = sub.strip()

                if _is_translatable_segment(core_text):
                    blocks.append(
                        {
                            "type": "translatable",
                            "raw": sub,
                            "leading_ws": leading_ws,
                            "trailing_ws": trailing_ws,
                            "src_text": core_text,
                            "dst_text": None,
                        }
                    )
                else:
                    blocks.append(
                        {
                            "type": "passthrough",
                            "raw": sub,
                            "src_text": sub,
                            "dst_text": None,
                        }
                    )

    return blocks


def _build_smart_batches(
    items: List[Dict[str, Any]],
    max_items_per_batch: int = 10,
    max_chars_per_batch: int = 1800,
) -> List[List[Dict[str, Any]]]:
    """
    Gom các đoạn văn cần dịch thành các batch thông minh (~8-12 đoạn hoặc ~1800 ký tự).
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
        logger.warning(f"Lỗi khi gọi progress_cb trong txt_translator: {e}")


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

    # Fallback dịch từng đoạn
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


def _read_text_file_safely(path: str) -> str:
    """
    Đọc nội dung file .txt / .md với nhiều bảng mã phổ biến (UTF-8, UTF-8-SIG, UTF-16, CP1252).
    """
    for enc in ("utf-8-sig", "utf-8", "utf-16", "cp1252", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


async def translate_txt_file(
    input_path: str,
    output_path: str,
    llm_client: Any,
    bilingual: bool = False,
    glossary: Optional[List[Dict[str, str]]] = None,
    progress_cb: Optional[Callable] = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> Dict[str, Any]:
    """
    Dịch file văn bản thuần (.txt) hoặc Markdown (.md):
    - Bảo toàn hoàn toàn các khối code ```...``` và công thức toán học $$...$$.
    - Chia theo đoạn văn (\n\n), gom batch thông minh và dịch qua llm_client.translate_batch.
    - Hỗ trợ chế độ song ngữ (Bilingual) hoặc chỉ bản dịch.
    """
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Không tìm thấy file TXT/MD đầu vào: {input_path}")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    raw_content = _read_text_file_safely(input_path)
    blocks = _parse_text_into_blocks(raw_content)

    translatable_items = [b for b in blocks if b["type"] == "translatable"]
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

    # Ráp lại toàn bộ nội dung file .txt / .md
    output_chunks: List[str] = []
    for b in blocks:
        if b["type"] == "translatable":
            src_text = b["src_text"]
            dst_text = b.get("dst_text") or src_text
            leading_ws = b.get("leading_ws", "")
            trailing_ws = b.get("trailing_ws", "")

            if bilingual:
                combined = f"{leading_ws}{src_text}\n\n{dst_text}{trailing_ws}"
            else:
                combined = f"{leading_ws}{dst_text}{trailing_ws}"
            output_chunks.append(combined)
        else:
            output_chunks.append(b["raw"])

    final_output = "".join(output_chunks)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(final_output)

    return {
        "status": "completed",
        "total_items": total_items,
        "translated_items": done_items,
        "output_path": output_path,
        "bilingual": bilingual,
    }
