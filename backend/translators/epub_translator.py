import asyncio
import copy
import logging
import os
import re
import shutil
import tempfile
import zipfile
from typing import Any, Callable, Dict, List, Optional, Tuple
from bs4 import BeautifulSoup, NavigableString, Tag

logger = logging.getLogger(__name__)

# Các thẻ văn bản đích trong HTML/XHTML của EPUB
TARGET_TAGS = (
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "blockquote",
    "figcaption",
    "td",
    "th",
    "dt",
    "dd",
)

# Các thẻ block con nếu nằm bên trong một thẻ thì thẻ cha không được coi là thẻ lá
BLOCK_TAGS = {
    "p",
    "div",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "ul",
    "ol",
    "li",
    "table",
    "tr",
    "td",
    "th",
    "blockquote",
    "section",
    "article",
    "aside",
    "header",
    "footer",
    "nav",
    "figure",
    "figcaption",
    "dl",
    "dt",
    "dd",
}

# Các thẻ inline cần giữ nguyên hoặc bỏ qua khi kiểm tra text
SKIP_PARENT_TAGS = {"script", "style", "code", "pre", "svg", "math", "head", "title"}


def _is_translatable_text(text: str) -> bool:
    """
    Kiểm tra xem chuỗi văn bản có đáng dịch hay không.
    Bỏ qua chuỗi rỗng, chỉ chứa số, ký tự đặc biệt, URL hoặc công thức ngắn.
    """
    if not text:
        return False
    cleaned = text.strip()
    if len(cleaned) < 2:
        return False
    # Bỏ qua nếu chỉ chứa số, dấu câu, ký hiệu toán học
    if re.match(r"^[\d\s\W_]+$", cleaned, re.UNICODE):
        # Kiểm tra xem có chữ cái nào không (hỗ trợ cả Latin, CJK, Cyrillic...)
        if not any(ch.isalpha() for ch in cleaned):
            return False
    # Bỏ qua URL thuần túy
    if re.match(r"^https?://\S+$", cleaned, re.IGNORECASE):
        return False
    return True


def _is_leaf_block_tag(tag: Tag) -> bool:
    """
    Kiểm tra xem một Tag có phải là thẻ văn bản lá (không chứa thẻ block con khác) hay không.
    """
    if tag.name not in TARGET_TAGS:
        return False
    # Không dịch nếu nằm trong script, style, pre, code, svg, math...
    for parent in tag.parents:
        if parent and parent.name in SKIP_PARENT_TAGS:
            return False
        if parent and parent.get("translate") == "no":
            return False
    if tag.get("translate") == "no" or "notranslate" in (tag.get("class") or []):
        return False

    # Kiểm tra xem bên trong có chứa thẻ block con nào có chữ không
    for child in tag.find_all(BLOCK_TAGS):
        if child is not tag and _is_translatable_text(child.get_text(strip=True)):
            return False

    return _is_translatable_text(tag.get_text(strip=True))


def _extract_clean_text(tag: Tag) -> str:
    """
    Trích xuất text từ thẻ HTML, chuẩn hóa khoảng trắng thừa giữa các dòng
    nhưng giữ nguyên nội dung ý nghĩa.
    """
    raw = tag.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", raw).strip()


def _replace_tag_text_preserving_style(tag: Tag, translated_text: str, soup: BeautifulSoup) -> None:
    """
    Thay thế văn bản của thẻ bằng bản dịch nhưng cố gắng giữ nguyên định dạng cơ bản
    hoặc thẻ inline bao ngoài nếu toàn bộ thẻ được bọc trong 1 thẻ inline duy nhất (vd: <p><strong>Text</strong></p>).
    """
    # Kiểm tra nếu có ảnh hoặc thẻ đặc biệt bên trong (như <img/>, <svg/>, <a id="..."/> rỗng dùng làm anchor)
    preserved_elements = []
    for child in list(tag.children):
        if isinstance(child, Tag):
            if child.name in ("img", "svg", "image", "br") or (
                child.name == "a" and not child.get_text(strip=True) and (child.get("id") or child.get("name"))
            ):
                preserved_elements.append(copy.copy(child))

    # Nếu thẻ chỉ chứa đúng 1 thẻ inline duy nhất bọc toàn bộ chữ (ví dụ <b>, <i>, <strong>, <em>, <span>)
    element_children = [c for c in tag.children if isinstance(c, Tag) and c.name not in ("a", "img", "svg", "br")]
    non_empty_strings = [
        c for c in tag.children if isinstance(c, NavigableString) and c.strip()
    ]

    if len(element_children) == 1 and not non_empty_strings and element_children[0].name in (
        "strong",
        "b",
        "em",
        "i",
        "span",
        "u",
        "a",
    ):
        inner_tag = copy.copy(element_children[0])
        inner_tag.clear()
        inner_tag.string = translated_text
        tag.clear()
        for el in preserved_elements:
            tag.append(el)
        tag.append(inner_tag)
    else:
        tag.clear()
        for el in preserved_elements:
            tag.append(el)
        tag.append(NavigableString(translated_text))


def _apply_bilingual_to_tag(tag: Tag, translated_text: str, soup: BeautifulSoup) -> None:
    """
    Chế độ song ngữ (Bilingual):
    Giữ nguyên thẻ gốc (thêm style làm dịu nhẹ để dễ phân biệt)
    và chèn ngay bên dưới một thẻ cùng loại chứa bản dịch Tiếng Việt nổi bật.
    """
    # Nếu là td/th trong bảng, không thể chèn thẻ td/th mới làm lệch cột bảng!
    if tag.name in ("td", "th", "li", "dt", "dd", "figcaption"):
        orig_wrapper = soup.new_tag("div")
        orig_wrapper["class"] = ["bth-bilingual-src"]
        orig_wrapper["style"] = "opacity: 0.78; font-size: 0.92em; margin-bottom: 4px;"
        for child in list(tag.contents):
            orig_wrapper.append(child.extract())

        trans_wrapper = soup.new_tag("div")
        trans_wrapper["class"] = ["bth-bilingual-dst"]
        trans_wrapper["style"] = "font-weight: inherit; margin-top: 2px;"
        trans_wrapper.string = translated_text

        tag.clear()
        tag.append(orig_wrapper)
        tag.append(trans_wrapper)
        return

    # Với các thẻ block thông thường (p, h1..h6, blockquote)
    trans_tag = copy.deepcopy(tag)
    # Xóa id trên thẻ dịch để tránh trùng ID trong XHTML (gây lỗi EPUB check / anchor)
    if trans_tag.has_attr("id"):
        del trans_tag["id"]
    for sub in trans_tag.find_all(attrs={"id": True}):
        del sub["id"]

    # Style nhẹ cho đoạn gốc
    existing_style = tag.get("style", "").strip()
    if existing_style and not existing_style.endswith(";"):
        existing_style += "; "
    tag["style"] = f"{existing_style}opacity: 0.78; font-size: 0.92em; margin-bottom: 4px;"
    existing_classes = list(tag.get("class") or [])
    if "bth-bilingual-src" not in existing_classes:
        existing_classes.append("bth-bilingual-src")
    tag["class"] = existing_classes

    # Cập nhật nội dung thẻ dịch
    _replace_tag_text_preserving_style(trans_tag, translated_text, soup)
    trans_classes = list(trans_tag.get("class") or [])
    if "bth-bilingual-dst" not in trans_classes:
        trans_classes.append("bth-bilingual-dst")
    trans_tag["class"] = trans_classes

    # Chèn ngay sau thẻ gốc
    tag.insert_after(trans_tag)


def _build_smart_batches(
    items: List[Dict[str, Any]],
    max_items_per_batch: int = 10,
    max_chars_per_batch: int = 1800,
) -> List[List[Dict[str, Any]]]:
    """
    Gom các đoạn văn bản thành các Batch thông minh (~8-12 đoạn hoặc tổng ~1800 ký tự).
    Ưu tiên gom các đoạn trong cùng một file XHTML để ngữ cảnh mạch lạc nhất.
    """
    batches: List[List[Dict[str, Any]]] = []
    current_batch: List[Dict[str, Any]] = []
    current_chars = 0
    current_file = None

    for item in items:
        text_len = len(item["src_text"])
        file_name = item["file_name"]

        # Ngắt batch nếu sang file chương mới hoặc vượt ngưỡng kích thước
        should_split = False
        if current_batch:
            if file_name != current_file:
                should_split = True
            elif len(current_batch) >= max_items_per_batch:
                should_split = True
            elif current_chars + text_len > max_chars_per_batch:
                should_split = True

        if should_split:
            batches.append(current_batch)
            current_batch = []
            current_chars = 0

        current_batch.append(item)
        current_chars += text_len
        current_file = file_name

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
        logger.warning(f"Lỗi khi gọi progress_cb: {e}")


async def _translate_batch_with_client(
    llm_client: Any,
    texts: List[str],
    context: str = "",
    glossary: Optional[List[Dict[str, str]]] = None,
) -> List[str]:
    """
    Gọi llm_client.translate_batch một cách linh hoạt và an toàn,
    tự động fallback nếu chữ ký hàm của llm_client khác biệt nhỏ.
    """
    if not texts:
        return []

    if hasattr(llm_client, "translate_batch"):
        try:
            res = llm_client.translate_batch(
                texts, context=context, glossary=glossary
            )
        except TypeError:
            try:
                res = llm_client.translate_batch(texts, glossary=glossary)
            except TypeError:
                res = llm_client.translate_batch(texts)

        if asyncio.iscoroutine(res):
            res = await res

        if isinstance(res, list) and len(res) == len(texts):
            return [str(x).strip() if x is not None else orig for x, orig in zip(res, texts)]

    # Fallback dịch từng đoạn nếu translate_batch không khớp số lượng
    results = []
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


def _get_ordered_html_files_from_epub(zf: zipfile.ZipFile) -> List[str]:
    """
    Lấy danh sách các file HTML/XHTML trong EPUB theo đúng thứ tự đọc (spine trong OPF)
    kèm theo các file HTML phụ khác chưa có trong spine.
    """
    all_names = zf.namelist()
    html_extensions = (".xhtml", ".html", ".htm")
    all_html_files = [
        name for name in all_names
        if name.lower().endswith(html_extensions)
        and not os.path.basename(name).startswith(".")
    ]

    ordered_files: List[str] = []
    try:
        # Tìm file .opf từ META-INF/container.xml hoặc quét trực tiếp
        opf_path = None
        if "META-INF/container.xml" in all_names:
            container_xml = zf.read("META-INF/container.xml").decode("utf-8", errors="ignore")
            soup_c = BeautifulSoup(container_xml, "html.parser")
            rootfile = soup_c.find("rootfile")
            if rootfile and rootfile.get("full-path"):
                opf_path = rootfile.get("full-path")

        if not opf_path:
            for name in all_names:
                if name.lower().endswith(".opf"):
                    opf_path = name
                    break

        if opf_path and opf_path in all_names:
            opf_dir = os.path.dirname(opf_path)
            opf_content = zf.read(opf_path).decode("utf-8", errors="ignore")
            soup_opf = BeautifulSoup(opf_content, "html.parser")

            manifest_map: Dict[str, str] = {}
            for item in soup_opf.find_all("item"):
                item_id = item.get("id")
                href = item.get("href")
                if item_id and href:
                    # Giải mã URL encoding cơ bản nếu có
                    from urllib.parse import unquote
                    clean_href = unquote(href.split("#")[0])
                    full_href = f"{opf_dir}/{clean_href}".lstrip("/") if opf_dir else clean_href
                    # Chuẩn hóa đường dẫn
                    full_href = os.path.normpath(full_href).replace("\\", "/")
                    manifest_map[item_id] = full_href

            for itemref in soup_opf.find_all("itemref"):
                idref = itemref.get("idref")
                if idref and idref in manifest_map:
                    target_path = manifest_map[idref]
                    if target_path in all_html_files and target_path not in ordered_files:
                        ordered_files.append(target_path)
    except Exception as e:
        logger.debug(f"Không thể phân tích thứ tự spine OPF, dùng thứ tự mặc định: {e}")

    # Thêm các file HTML còn lại chưa nằm trong spine
    for f in all_html_files:
        if f not in ordered_files:
            ordered_files.append(f)

    return ordered_files


async def translate_epub_file(
    input_path: str,
    output_path: str,
    llm_client: Any,
    bilingual: bool = False,
    glossary: Optional[List[Dict[str, str]]] = None,
    progress_cb: Optional[Callable] = None,
    cancel_event: Optional[asyncio.Event] = None,
) -> Dict[str, Any]:
    """
    Dịch file EPUB giữ nguyên 100% cấu trúc gốc (ảnh bìa, minh họa, CSS, font, TOC/ncx, OPF).
    Kết hợp tinh hoa của bilingual_book_maker, Ebook-Translator-Calibre-Plugin và LinguaGacha:
    - Mở EPUB trực tiếp dưới dạng ZipFile, chỉ parse và dịch các file .xhtml/.html/.htm.
    - Copy nguyên vẹn tất cả tài nguyên khác sang EPUB đích.
    - Gom batch thông minh và dịch song song bất đồng bộ theo concurrency của llm_client.
    """
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Không tìm thấy file EPUB đầu vào: {input_path}")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    # 1. Đọc file EPUB gốc và parse các file HTML/XHTML
    parsed_docs: Dict[str, BeautifulSoup] = {}
    translatable_items: List[Dict[str, Any]] = []

    with zipfile.ZipFile(input_path, "r") as zin:
        ordered_html_files = _get_ordered_html_files_from_epub(zin)

        for file_name in ordered_html_files:
            raw_bytes = zin.read(file_name)
            try:
                html_text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                html_text = raw_bytes.decode("utf-8", errors="replace")

            soup = BeautifulSoup(html_text, "html.parser")
            parsed_docs[file_name] = soup

            # Tìm tất cả các thẻ văn bản lá có nội dung đáng dịch
            body = soup.find("body") or soup
            for tag in body.find_all(TARGET_TAGS):
                if _is_leaf_block_tag(tag):
                    src_text = _extract_clean_text(tag)
                    if _is_translatable_text(src_text):
                        translatable_items.append(
                            {
                                "file_name": file_name,
                                "tag": tag,
                                "soup": soup,
                                "src_text": src_text,
                                "dst_text": None,
                            }
                        )

    # Áp dụng giới hạn chương / khoảng chương dịch thử (nếu có cấu hình trên llm_client.config)
    cfg = getattr(llm_client, "config", None)
    chapter_limit = int(getattr(cfg, "chapter_limit", 0) or 0) if cfg else 0
    page_range_str = str(getattr(cfg, "page_range", "") or "").strip() if cfg else ""
    if (chapter_limit > 0 or page_range_str) and translatable_items:
        # Nhóm các file có thực chất nội dung (>= 3 đoạn văn) thành danh sách chương
        file_counts: Dict[str, int] = {}
        chapter_files: List[str] = []
        for it in translatable_items:
            fn = it["file_name"]
            file_counts[fn] = file_counts.get(fn, 0) + 1
        for fn in ordered_html_files:
            if file_counts.get(fn, 0) >= 3:
                chapter_files.append(fn)
        if not chapter_files:
            chapter_files = list(dict.fromkeys(it["file_name"] for it in translatable_items))

        allowed_files: set[str] = set()
        if page_range_str:
            for part in re.split(r"[,;]+", page_range_str):
                part = part.strip()
                if not part:
                    continue
                if "-" in part:
                    s_str, e_str = part.split("-", 1)
                    if s_str.strip().isdigit() and e_str.strip().isdigit():
                        s_i, e_i = int(s_str.strip()), int(e_str.strip())
                        for c_idx in range(max(1, s_i), min(len(chapter_files), e_i) + 1):
                            allowed_files.add(chapter_files[c_idx - 1])
                elif part.isdigit():
                    c_idx = int(part)
                    if 1 <= c_idx <= len(chapter_files):
                        allowed_files.add(chapter_files[c_idx - 1])
        elif chapter_limit > 0:
            allowed_files = set(chapter_files[:chapter_limit])

        if allowed_files:
            filtered = [it for it in translatable_items if it["file_name"] in allowed_files]
            if filtered:
                translatable_items = filtered
        elif chapter_limit > 0:
            translatable_items = translatable_items[: chapter_limit * 35]

    total_items = len(translatable_items)
    if total_items == 0:
        # Không có đoạn văn nào cần dịch, copy nguyên bản
        shutil.copy2(input_path, output_path)
        await _invoke_progress_cb(progress_cb, 0, 0, "", "")
        return {
            "status": "completed",
            "total_items": 0,
            "translated_items": 0,
            "output_path": output_path,
            "bilingual": bilingual,
        }

    # 2. Chia thành các Batch thông minh (~8-12 đoạn hoặc ~1800 ký tự)
    batches = _build_smart_batches(
        translatable_items, max_items_per_batch=10, max_chars_per_batch=1800
    )

    # Chuẩn bị context (đoạn văn trước của mỗi batch)
    batch_contexts: List[str] = []
    for idx, batch in enumerate(batches):
        if idx == 0:
            batch_contexts.append("")
        else:
            prev_batch = batches[idx - 1]
            # Lấy tối đa 2 đoạn cuối của batch trước làm ngữ cảnh
            ctx_texts = [item["src_text"] for item in prev_batch[-2:]]
            batch_contexts.append("\n".join(ctx_texts))

    # 3. Chạy dịch bất đồng bộ với độ tương tranh cao
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

    # 4. Gán bản dịch vào DOM của từng file HTML/XHTML
    for item in translatable_items:
        dst_text = item.get("dst_text")
        if not dst_text:
            continue
        tag = item["tag"]
        soup = item["soup"]
        if bilingual:
            _apply_bilingual_to_tag(tag, dst_text, soup)
        else:
            _replace_tag_text_preserving_style(tag, dst_text, soup)

    # 5. Đóng gói lại file EPUB đích an toàn tuyệt đối (giữ chuẩn mimetype đầu tiên không nén)
    temp_fd, temp_epub_path = tempfile.mkstemp(suffix=".epub")
    os.close(temp_fd)

    try:
        with zipfile.ZipFile(input_path, "r") as zin, zipfile.ZipFile(
            temp_epub_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as zout:
            infolist = zin.infolist()

            # Theo chuẩn EPUB, file 'mimetype' phải nằm đầu tiên và không nén (ZIP_STORED)
            for info in infolist:
                if info.filename == "mimetype":
                    zout.writestr(
                        "mimetype",
                        zin.read("mimetype"),
                        compress_type=zipfile.ZIP_STORED,
                    )
                    break

            for info in infolist:
                if info.filename == "mimetype":
                    continue

                if info.filename in parsed_docs:
                    soup_doc = parsed_docs[info.filename]
                    # Xuất lại chuỗi HTML/XHTML UTF-8
                    rendered_html = str(soup_doc)
                    zout.writestr(
                        info,
                        rendered_html.encode("utf-8"),
                        compress_type=zipfile.ZIP_DEFLATED,
                    )
                else:
                    # Copy nguyên vẹn 100% ảnh, CSS, font, toc.ncx, content.opf, META-INF...
                    zout.writestr(
                        info,
                        zin.read(info.filename),
                        compress_type=info.compress_type,
                    )

        shutil.move(temp_epub_path, output_path)
    finally:
        if os.path.exists(temp_epub_path):
            try:
                os.remove(temp_epub_path)
            except OSError:
                pass

    return {
        "status": "completed",
        "total_items": total_items,
        "translated_items": done_items,
        "output_path": output_path,
        "bilingual": bilingual,
    }
