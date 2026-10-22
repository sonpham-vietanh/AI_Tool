from typing import Dict, List, Optional


LANGUAGE_CODE_MAP: Dict[str, str] = {
    "tiếng việt": "vi",
    "vietnamese": "vi",
    "vi": "vi",
    "english": "en",
    "tiếng anh": "en",
    "en": "en",
    "chinese": "zh-CN",
    "tiếng trung": "zh-CN",
    "zh": "zh-CN",
    "japanese": "ja",
    "tiếng nhật": "ja",
    "ja": "ja",
    "korean": "ko",
    "tiếng hàn": "ko",
    "ko": "ko",
    "french": "fr",
    "tiếng pháp": "fr",
    "fr": "fr",
    "german": "de",
    "tiếng đức": "de",
    "de": "de",
    "spanish": "es",
    "tiếng tây ban nha": "es",
    "es": "es",
    "russian": "ru",
    "tiếng nga": "ru",
    "ru": "ru",
}


def resolve_lang_code(target_lang: str, fallback: str = "vi") -> str:
    if not target_lang:
        return fallback
    key = target_lang.strip().lower()
    if key in LANGUAGE_CODE_MAP:
        return LANGUAGE_CODE_MAP[key]
    for name, code in LANGUAGE_CODE_MAP.items():
        if name in key:
            return code
    return fallback


def format_glossary_block(glossary: Optional[List[Dict[str, str]]]) -> str:
    if not glossary:
        return ""
    lines = [
        "### BẢNG THUẬT NGỮ & TÊN RIÊNG BẮT BUỘC (GLOSSARY):",
        "Hãy tuân thủ tuyệt đối 100% cách dịch các thuật ngữ/tên riêng dưới đây khi chúng xuất hiện trong văn bản:",
    ]
    for item in glossary:
        src = (item.get("src") or "").strip()
        dst = (item.get("dst") or "").strip()
        note = (item.get("note") or "").strip()
        if not src or not dst:
            continue
        if note:
            lines.append(f"- `{src}` → `{dst}` ({note})")
        else:
            lines.append(f"- `{src}` → `{dst}`")
    return "\n".join(lines) if len(lines) > 2 else ""


GENRE_STYLE_PROMPTS: Dict[str, str] = {
    "auto": "",
    "business": (
        "Văn phong Sách Kinh doanh, Quản trị & Tài chính (chuẩn Harvard Business Review / Alpha Books): "
        "Câu văn gãy gọn, súc tích, hiện đại, dùng chuẩn thuật ngữ kinh tế - tài chính - đầu tư, tạo sức thuyết phục cao."
    ),
    "tech": (
        "Văn phong Sách Công nghệ, Kỹ thuật Phần mềm & AI (chuẩn O'Reilly): "
        "Chính xác, mạch lạc, giữ nguyên các thuật ngữ lập trình/kiến trúc quốc tế (hoặc mở ngoặc giải thích ngắn ở lần đầu), tuyệt đối không dịch sai code hoặc tên hàm."
    ),
    "selfhelp": (
        "Văn phong Sách Tâm lý học, Triết học & Phát triển bản thân (Self-Help): "
        "Gần gũi, truyền cảm hứng, nhịp điệu câu cuốn hút, giàu cảm xúc, xưng hô 'bạn - tôi/chúng ta' tự nhiên."
    ),
    "literature": (
        "Văn phong Tiểu thuyết Văn học & Tự truyện: "
        "Giàu hình ảnh, tinh tế, giàu nhạc tính, chú trọng sắc thái biểu cảm của từ ngữ và đối thoại nhân vật sống động."
    ),
    "xianxia": (
        "Văn phong Tiểu thuyết Tiên hiệp, Kiếm hiệp & Huyền huyễn: "
        "Sử dụng từ Hán Việt chuẩn mực cho cảnh giới, chiêu thức, pháp bảo; xưng hô cổ trang chuẩn xác (ta - ngươi, tại hạ - các hạ, huynh - đệ, tiền bối - vãn bối)."
    ),
    "lightnovel": (
        "Văn phong Light Novel / Manga / Hiện đại trẻ trung: "
        "Câu thoại tự nhiên, linh hoạt, bắt đúng nhịp cảm xúc nhân vật, mượt mà và dễ đọc."
    ),
    "academic": (
        "Văn phong Tài liệu Khoa học, Giáo dục, Y khoa & Học thuật chuyên sâu: "
        "Khách quan, chuẩn xác tuyệt đối về thuật ngữ chuyên môn, số liệu, trích dẫn và cấu trúc lập luận khoa học."
    ),
}


def resolve_genre_prompt(genre_preset: str = "auto", custom_prompt: str = "") -> str:
    parts: List[str] = []
    g_text = GENRE_STYLE_PROMPTS.get((genre_preset or "auto").strip().lower(), "")
    if g_text:
        parts.append(g_text)
    if custom_prompt and custom_prompt.strip():
        parts.append(custom_prompt.strip())
    return "\n".join(parts)


def build_system_prompt(
    target_lang: str = "Tiếng Việt",
    custom_prompt: str = "",
    glossary: Optional[List[Dict[str, str]]] = None,
    context: str = "",
    is_batch: bool = False,
    book_brief: str = "",
    term_candidates: Optional[List[str]] = None,
) -> str:
    """
    Constructs an industrial-grade book translation System Prompt (Agent 3) tailored for natural,
    publication-quality output with dynamic Glossary injection, inline HTML formatting tag preservation,
    and strict PDF layout box length discipline.
    """
    base_instructions = f"""Bạn là dịch giả sách chuyên nghiệp kiêm biên tập viên xuất bản hàng đầu, dịch sang {target_lang}. Bản dịch của bạn phải đọc mượt mà, súc tích, chuẩn mực như sách được viết gốc bằng {target_lang} bởi một chuyên gia bản ngữ, TUYỆT ĐỐI KHÔNG mang mùi văn dịch máy.

### QUY TRÌNH (làm thầm trong đầu, chỉ xuất bản dịch cuối cùng):
1. Hiểu đúng ý, giọng điệu, vai trò chuyên môn và mục đích của đoạn văn trong mạch sách (xem BOOK BRIEF và GLOSSARY nếu có).
2. Diễn đạt lại trọn vẹn ý nghĩa bằng cấu trúc câu và thuật ngữ chuyên ngành tự nhiên của {target_lang}, thoát khỏi vỏ ngữ pháp tiếng gốc.
3. Tự kiểm duyệt khắt khe trước khi trả lời: phát hiện và sửa ngay mọi cụm từ dịch sát chữ (calque), câu bị động gượng gạo, danh từ hóa nặng nề, lặp từ, lệch xưng hô hoặc sai thuật ngữ.

### NGUYÊN TẮC CỐT LÕI:
1. **Cú pháp & Nhịp văn Tiếng Việt tự nhiên, chuẩn xuất bản (Chống dịch sát chữ / Anti-Calque)**:
   - TUYỆT ĐỐI tránh các cấu trúc bị động dịch máy thô cứng kiểu `"được thiết kế để bởi..."`, `"bị/được thực hiện bởi..."`, hay dịch ghép chữ ngô nghê kiểu `"những người thực hành lớp học"` (phải dịch là `"giáo viên thực hành tại lớp"` hoặc `"giáo viên đứng lớp"`).
   - Tránh lạm dụng hư từ rườm rà ở đầu câu hoặc giữa câu như `"Việc thực hiện..."`, `"Một cách..."`, `"Sự..."`, `"Những..."`. Ưu tiên câu chủ động, chủ ngữ - vị ngữ rõ ràng, nhịp câu gãy gọn, mạch lạc.
   - Dịch đúng thuật ngữ chuyên ngành chuẩn mực:
     + `"Classroom Practitioner"` / `"Practitioner"` → `"Giáo viên thực hành"` (KHÔNG dịch `"người thực hành lớp học"`).
     + `"Practices"` → `"Các biện pháp thực hành"` (KHÔNG dịch `"các thực hành"` trơ trọi).
     + `"Practice-Based Coaching (PBC)"` → `"Huấn luyện dựa trên thực hành (PBC)"`.
     + `"Teaching Pyramid Observation Tool (TPOT)"` → `"Công cụ Quan sát Mô hình Kim tự tháp (TPOT)"`.
     + `"Coach"` / `"Classroom Coach"` → `"Chuyên gia huấn luyện"` / `"Chuyên gia huấn luyện lớp học"`.
     + `"Focused Observation"` → `"Quan sát trọng tâm"`.
     + `"Debriefing"` / `"Debriefing Session"` → `"Phản hồi - trao đổi"` / `"Buổi họp tổng kết & phản hồi"`.
     + `"Action Plan"` → `"Kế hoạch hành động"`.
     + `"Look-Think-Act"` → `"Quan sát – Suy ngẫm – Hành động"` (KHÔNG dịch `"Nhìn-Nghĩ-Làm"`).
     + `"Caseload"` → `"Khối lượng phụ trách"` / `"Danh sách phụ trách"`.
2. **Bảo toàn thẻ định dạng Inline HTML (`<b>...</b>`, `<i>...</i>`, `<sup>...</sup>`) & thẻ đánh dấu**:
   - Khi đoạn văn gốc có chứa các thẻ định dạng inline như `<b>...</b>`, `<i>...</i>`, `<sup>...</sup>`, bạn BẮT BUỘC phải giữ nguyên và bọc đúng từ/cụm từ Tiếng Việt tương ứng trong cùng cặp thẻ đó (`<b>...</b>`, `<i>...</i>`, `<sup>...</sup>`).
   - KHÔNG tự ý thêm thẻ HTML mới nếu bản gốc không có, KHÔNG làm rớt thẻ đóng, và KHÔNG đổi sang ký hiệu Markdown (`**`, `*`).
   - Giữ nguyên chính xác các dấu `⟦n⟧`, `⟦/n⟧`, `⟦n/⟧` (nếu có) ở đúng vị trí tương ứng.
3. **Kỷ luật độ dài nghiêm ngặt cho khung bố cục PDF (`[Gợi ý bố cục]`)**:
   - Khi `[Gợi ý bố cục]` (hoặc ghi chú vai trò đoạn) báo hiệu đoạn văn là tiêu đề đầu/cuối trang (`header`, `footer`), nhãn sơ đồ (`diagram label`), tiêu đề cột, hoặc **ô bảng/biểu mẫu hẹp (`ô bảng/biểu mẫu hẹp`)**: BẮT BUỘC dùng cách diễn đạt Tiếng Việt cô đọng, súc tích nhất có thể.
   - Loại bỏ hoàn toàn các từ đệm không mang nghĩa cốt lõi (`Việc thực hiện...`, `Một cách...`, `Những...`, `Các hoạt động của...`) để văn bản dịch nằm gọn đẹp trong khung PDF ở nguyên cỡ chữ gốc mà không bị tràn ô hay co nhỏ chữ.
   - Những mã/ký hiệu/viết tắt ngắn (VD: `FO-Karen`, `M-Tyrel`, `TPOT`, `TPITOS`, `PBC`, `Q&A`, `Yes No` → `Có Không`, `Page ___ of ___` → `Trang ___ / ___`) phải dịch thật gọn và giữ nguyên mã hiệu.
4. **Trung thực & Nhất quán tuyệt đối 100%**:
   - Không bỏ sót, không tóm tắt, không tự thêm lời bình hay giải thích của AI. Giữ nguyên số liệu, công thức, code, URL, chuỗi gạch chân điền khuyết (`_____`), ô chọn (`☐`, `☑`), trích dẫn `(Smith, 2020)`.
   - Tuân thủ 100% **GLOSSARY** và **BOOK BRIEF** xuyên suốt từ Trang bìa, Mục lục, Tiêu đề chương đến Bảng biểu và Phụ lục."""

    blocks = [base_instructions]

    if custom_prompt and custom_prompt.strip():
        blocks.append(
            f"### YÊU CẦU ĐẶC BIỆT TỪ ĐỘC GIẢ / BIÊN TẬP VIÊN:\n{custom_prompt.strip()}"
        )

    if book_brief and book_brief.strip():
        blocks.append(
            f"### BOOK BRIEF (đặc điểm & quy ước dịch của CUỐN SÁCH NÀY, bắt buộc tuân thủ):\n{book_brief.strip()}"
        )

    glossary_str = format_glossary_block(glossary)
    if glossary_str:
        blocks.append(glossary_str)

    if context and context.strip():
        trimmed_ctx = context.strip()[-1200:]
        blocks.append(
            f'### NGỮ CẢNH ĐOẠN TRƯỚC (ROLLING CONTEXT - CHỈ ĐỌC ĐỂ GIỮ MẠCH VĂN & ĐẠI TỪ, KHÔNG DỊCH LẠI ĐOẠN NÀY):\n"""\n{trimmed_ctx}\n"""'
        )

    if is_batch:
        blocks.append(
            """### ĐỊNH DẠNG ĐẦU RA BẮT BUỘC CHO BATCH TRANSLATION:
- Văn bản đầu vào được chia thành nhiều đoạn đánh dấu bằng thẻ `[[#0]]`, `[[#1]]`, `[[#2]]`...
- Bạn BẮT BUỘC phải giữ nguyên chính xác từng thẻ đánh dấu `[[#0]]`, `[[#1]]`, `[[#2]]`... ở đầu mỗi đoạn dịch tương ứng.
- KHÔNG gộp hai đoạn làm một, KHÔNG tách một đoạn thành hai thẻ, KHÔNG bỏ sót bất kỳ thẻ `[[#i]]` nào.
- Bảo toàn mọi thẻ inline `<b>...</b>`, `<i>...</i>`, `<sup>...</sup>` bên trong từng đoạn (nếu bản gốc có).
- Chỉ trả về nội dung đã dịch kèm thẻ `[[#i]]`, không thêm bất kỳ lời mở đầu hay kết thúc nào."""
        )
        cands = [c.strip() for c in (term_candidates or []) if c and c.strip()]
        if cands:
            blocks.append(
                "### GHI NHỚ THUẬT NGỮ MỚI (để giữ nhất quán các batch sau):\n"
                "Các tên riêng/thuật ngữ sau chưa có trong GLOSSARY: "
                + "; ".join(cands[:24])
                + ".\nSau khi dịch xong TẤT CẢ các đoạn, nếu có mục nào trong danh sách trên thật sự là tên riêng/thuật ngữ cần dịch nhất quán, "
                "thêm ở CUỐI CÙNG một dòng `[[TERMS]]` rồi mỗi dòng một mục dạng `gốc => cách bạn đã dịch` (đúng như bạn đã dùng trong bản dịch). "
                "Chỉ liệt kê mục mới trong danh sách trên; bỏ qua nếu không có."
            )
    else:
        blocks.append(
            "### ĐỊNH DẠNG ĐẦU RA:\nChỉ trả về duy nhất bản dịch hoàn chỉnh của đoạn văn đầu vào (giữ nguyên các thẻ `<b>...</b>`, `<i>...</i>`, `<sup>...</sup>` nếu có), không thêm lời dẫn, không giải thích."
        )

    return "\n\n".join(blocks)


# Explicit alias for Agent 3 translator system prompt
build_translate_system_prompt = build_system_prompt


def build_batch_user_prompt(texts: List[str], hints: Optional[List[str]] = None) -> str:
    """
    Formats a list of text segments into indexed markers [[#0]], [[#1]], ...
    `hints` (tùy chọn): [Gợi ý bố cục] vai trò/vị trí của từng đoạn trong trang (header, footer,
    diagram label, ô bảng/biểu mẫu hẹp...) để Agent 3 điều chỉnh độ dài và văn phong thích hợp.
    """
    parts = []
    for idx, text in enumerate(texts):
        clean_text = text.strip()
        parts.append(f"[[#{idx}]]\n{clean_text}")
    body = "\n\n".join(parts)
    notes = [f"#{i} [Gợi ý bố cục]: {h}" for i, h in enumerate(hints or []) if h]
    if not notes:
        return body
    return (
        "[VAI TRÒ/VỊ TRÍ CÁC ĐOẠN TRONG TRANG - Gợi ý bố cục chỉ để bạn hiểu ngữ cảnh & co gọn câu chữ vừa khung; KHÔNG dịch và KHÔNG chép lại phần này]\n"
        + "\n".join(notes)
        + "\n\n[CÁC ĐOẠN CẦN DỊCH]\n"
        + body
    )


def build_glossary_extraction_prompt(
    sample_text: str, target_lang: str = "Tiếng Việt", max_terms: int = 55
) -> str:
    """
    Agent 2: Domain Chief Terminologist prompt (JSON Array format).
    """
    target_min = min(35, max_terms)
    return f"""Bạn là Kỹ sư Trưởng Thuật ngữ Chuyên ngành (Domain Chief Terminologist) và Tổng Biên tập dịch sách xuất bản.
Hãy phân tích kỹ đoạn trích đa phân đoạn của cuốn sách dưới đây (bao gồm Trang bìa, Mục lục, Key Terms/Glossary, Phần giới thiệu và các chương/biểu mẫu) và trích xuất từ {target_min} đến {max_terms} thuật ngữ cốt lõi nhất để chuẩn hóa sang {target_lang}.

### YÊU CẦU TRÍCH XUẤT & CHUẨN HÓA THUẬT NGỮ (35–55 MỤC CỐT LÕI):
1. **Phạm vi bao phủ bắt buộc**:
   - Nhan đề chính của sách/tài liệu, tên tổ chức/trung tâm nghiên cứu.
   - Các thuật ngữ khung phương pháp luận cốt lõi (Core domain terms & frameworks).
   - Chức danh / vai trò chuyên môn (Role titles).
   - Tên các công cụ đánh giá, thang đo (Assessment tools & scales).
   - Tiêu đề các chương/đề mục lặp lại (Recurring section headers) và tên các biểu mẫu, bảng kiểm, nhãn cột bảng (Form & table labels).
   - KHÔNG chọn từ vựng giao tiếp thông thường.
2. **Quy tắc giữ từ viết tắt quốc tế trong ngoặc đơn `(...)` đối với Công cụ / Khung phương pháp nổi tiếng**:
   - Với các khung phương pháp luận, mô hình, công cụ đánh giá hoặc tổ chức quốc tế có tên viết tắt chuẩn (Acronym), HÃY GIỮ từ viết tắt tiếng Anh trong ngoặc đơn ở bản dịch:
     + `Practice-Based Coaching (PBC)` → `Huấn luyện dựa trên thực hành (PBC)`
     + `Teaching Pyramid Observation Tool (TPOT)` → `Công cụ Quan sát Mô hình Kim tự tháp (TPOT)`
     + `Teaching Pyramid Infant-Toddler Observation Scale (TPITOS)` → `Thang Quan sát Mô hình Kim tự tháp cho Trẻ sơ sinh và Trẻ nhỏ (TPITOS)`
     + `National Center for Pyramid Model Innovations (NCPMI)` → `Trung tâm Quốc gia về Đổi mới Mô hình Kim tự tháp (NCPMI)`
     + `Prevent-Teach-Reinforce for Young Children (PTR-YC)` → `Ngăn ngừa - Dạy học - Củng cố cho Trẻ nhỏ (PTR-YC)`
     + `Behavior Support Plan (BSP)` → `Kế hoạch Hỗ trợ Hành vi (BSP)`
3. **Quy tắc dịch tự nhiên, chuẩn chuyên ngành cho Chức danh & Khái niệm chung (Chống dịch sát chữ / Anti-Calque)**:
   - Dịch sang Tiếng Việt chuyên ngành tự nhiên, chuẩn xác, KHÔNG kèm tiếng Anh rườm rà cho các khái niệm/vai trò thông dụng và TUYỆT ĐỐI tránh dịch word-by-word:
     + `Classroom Practitioner` / `Practitioner` → `Giáo viên thực hành` (KHÔNG dịch "Người thực hành lớp học")
     + `Coach` / `Classroom Coach` → `Chuyên gia huấn luyện` / `Chuyên gia huấn luyện lớp học`
     + `Focused Observation` → `Quan sát trọng tâm`
     + `Debriefing` → `Phản hồi - trao đổi` (hoặc `Buổi họp tổng kết & phản hồi` cho `Debriefing Session`)
     + `Action Plan` / `Action Planning Form` → `Kế hoạch hành động` / `Biểu mẫu Lập kế hoạch hành động`
     + `Look-Think-Act` → `Quan sát – Suy ngẫm – Hành động` (KHÔNG dịch "Nhìn-Nghĩ-Làm")
     + `Pyramid Model practices` / `Practices` → `Các biện pháp thực hành Mô hình Kim tự tháp` / `Các biện pháp thực hành`
     + `Caseload` → `Khối lượng phụ trách`
     + `Implementation Fidelity` → `Độ trung thực khi triển khai`
4. Trả về KẾT QUẢ DUY NHẤT ở định dạng JSON Array thuần túy (không bọc lời dẫn):
[
  {{"src": "thuật ngữ gốc", "dst": "bản dịch {target_lang} chuẩn xuất bản", "note": "Giải thích ngắn / loại từ (Khung phương pháp, Công cụ đánh giá, Chức danh, Biểu mẫu, Đề mục...)"}}
]

### ĐOẠN TRÍCH ĐA PHÂN ĐOẠN CỦA SÁCH:
\"\"\"
{sample_text[:25000]}
\"\"\""""


def build_book_profile_prompt(
    sample_text: str,
    target_lang: str = "Tiếng Việt",
    max_terms: int = 55,
    layout_hint: str = "",
) -> str:
    """
    Agent 2: Domain Chief Terminologist & Book Profiler Prompt.
    Analyzes multi-section book samples (up to 25,000 chars including Key Terms / Glossary / TOC / Intro),
    extracts 35–55 core domain terms, role titles, assessment tools, recurring section headers, and form/table labels,
    and generates a rich, actionable `brief` covering tone, pronoun/addressing rules, and domain conventions.
    """
    target_min = min(35, max_terms)
    layout_block = (
        f"\n### THÔNG TIN BỐ CỤC CỦA FILE (do máy đo, chỉ để tham khảo khi lập quy chuẩn dịch):\n{layout_hint.strip()}\n"
        if layout_hint and layout_hint.strip()
        else ""
    )
    return f"""Bạn là Kỹ sư Trưởng Thuật ngữ Chuyên ngành (Domain Chief Terminologist) kiêm Tổng Biên tập xuất bản kỳ cựu, dẫn dắt nhóm dịch cuốn sách/tài liệu dưới đây sang {target_lang}.
Trước khi dịch, hãy ĐỌC KỸ đoạn trích đa phân đoạn (gồm Trang bìa, Mục lục, trang Key Terms/Glossary/Definitions, Phần giới thiệu và các phần giữa/cuối sách) để lập **HỒ SƠ ĐỊNH HƯỚNG DỊCH THUẬT (BOOK BRIEF)** và **BẢNG THUẬT NGỮ CHUẨN HÓA (GLOSSARY từ {target_min}–{max_terms} mục)**.

### QUY CHUẨN LẬP BẢNG THUẬT NGỮ (35–55 THUẬT NGỮ CỐT LÕI):
1. **Trích xuất đầy đủ 5 nhóm mục tiêu (35–{max_terms} mục)**:
   - (a) Nhan đề chính của sách/tài liệu & tên tổ chức xuất bản.
   - (b) Thuật ngữ chuyên ngành cốt lõi & khung phương pháp luận ( đặc biệt ưu tiên mọi mục trong phần *Key Terms / Glossary / Definitions* của sách).
   - (c) Chức danh, vai trò chuyên môn (Role titles).
   - (d) Tên các công cụ đánh giá, thang đo, bảng kiểm (Assessment tools, scales, checklists).
   - (e) Tiêu đề chương/đề mục lặp lại (Recurring section headers) và tên biểu mẫu, nhãn cột bảng/form (Form & table labels) để đồng bộ 100% từ Mục lục tới Phụ lục.
2. **Giữ từ viết tắt quốc tế trong ngoặc đơn `(...)` cho Công cụ & Khung phương pháp chuẩn**:
   - Với các khung phương pháp, công cụ đánh giá, thang đo hoặc tổ chức quốc tế có tên viết tắt nổi tiếng, giữ từ viết tắt tiếng Anh trong ngoặc đơn:
     + `Practice-Based Coaching (PBC)` → `Huấn luyện dựa trên thực hành (PBC)`
     + `Teaching Pyramid Observation Tool (TPOT)` → `Công cụ Quan sát Mô hình Kim tự tháp (TPOT)`
     + `Teaching Pyramid Infant-Toddler Observation Scale (TPITOS)` → `Thang Quan sát Mô hình Kim tự tháp cho Trẻ sơ sinh và Trẻ nhỏ (TPITOS)`
     + `National Center for Pyramid Model Innovations (NCPMI)` → `Trung tâm Quốc gia về Đổi mới Mô hình Kim tự tháp (NCPMI)`
     + `Prevent-Teach-Reinforce for Young Children (PTR-YC)` → `Ngăn ngừa - Dạy học - Củng cố cho Trẻ nhỏ (PTR-YC)`
     + `Behavior Support Plan (BSP)` → `Kế hoạch Hỗ trợ Hành vi (BSP)`
3. **Dịch tự nhiên, chuẩn Việt hóa cho Vai trò & Khái niệm chung (Chống dịch sát chữ / Anti-Calque)**:
   - Dịch các chức danh, quy trình và khái niệm chung sang {target_lang} tự nhiên, súc tích, chuẩn chuyên môn, tuyệt đối tránh dịch calque từng chữ:
     + `Classroom Practitioner` / `Practitioner` → `Giáo viên thực hành` (KHÔNG dịch "Người thực hành lớp học")
     + `Coach` / `Classroom Coach` → `Chuyên gia huấn luyện` / `Chuyên gia huấn luyện lớp học`
     + `Focused Observation` → `Quan sát trọng tâm`
     + `Debriefing` → `Phản hồi - trao đổi` (`Debriefing Session` → `Buổi họp tổng kết & phản hồi`)
     + `Action Plan` → `Kế hoạch hành động`
     + `Look-Think-Act` → `Quan sát – Suy ngẫm – Hành động` (KHÔNG dịch "Nhìn-Nghĩ-Làm")
     + `Practices` / `Pyramid Model practices` → `Các biện pháp thực hành` / `Các biện pháp thực hành Mô hình Kim tự tháp` (KHÔNG dịch "Các thực hành")
     + `Caseload` → `Khối lượng phụ trách`
4. **Thiết lập `brief` & `translator_notes` giàu tính hành động**:
   - Xác định rõ giọng văn (tone), quy ước đại từ/xưng hô (pronoun/addressing rules), quy ước chuyên ngành (domain conventions), cách tránh câu bị động calque và yêu cầu cô đọng câu chữ cho các ô bảng/biểu mẫu hẹp.

Trả về DUY NHẤT một JSON object hợp lệ (không lời dẫn, không bọc markdown):

{{
  "document_type": "mô tả cụ thể loại tài liệu và lĩnh vực chuyên môn",
  "title_translation": "bản dịch chuẩn xuất bản của nhan đề chính cuốn sách/tài liệu",
  "understanding": "1-3 câu: tài liệu nói về gì, ai viết cho ai, dùng vào mục đích gì",
  "tone": "giọng văn chủ đạo (VD: chuẩn mực sư phạm, chuyên nghiệp, khích lệ, rõ ràng, dễ áp dụng thực tế)",
  "address_rules": "quy ước đại từ và xưng hô thống nhất khi dịch sang {target_lang} (VD: xưng 'bạn' với độc giả/chuyên gia huấn luyện, gọi 'giáo viên thực hành', 'trẻ'...), để rỗng nếu không áp dụng",
  "brief": "bản tóm tắt định hướng dịch thuật toàn diện (gồm giọng văn, quy tắc xưng hô, quy ước thuật ngữ chuyên ngành và kỷ luật độ dài cho bảng biểu/sơ đồ)",
  "translator_notes": "chỉ dẫn biên tập chi tiết và sắc bén cho nhóm dịch: cách chọn từ vựng chuyên môn, các bẫy dịch word-by-word cần tránh tuyệt đối, yêu cầu cô đọng cho các ô bảng/biểu mẫu/sơ đồ",
  "do_not_translate": ["các mã viết tắt, URL, tên miền, ký hiệu giữ nguyên gốc"],
  "glossary": [{{"src": "thuật ngữ/nhan đề/đề mục gốc", "dst": "bản dịch {target_lang} chuẩn xuất bản (giữ viết tắt trong ngoặc với tên công cụ/khung quốc tế như PBC, TPOT)", "note": "loại thuật ngữ"}}]
}}

Yêu cầu glossary: trích xuất từ {target_min} đến {max_terms} mục quan trọng nhất; KHÔNG đưa từ vựng thông thường.
{layout_block}
### ĐOẠN TRÍCH ĐA PHÂN ĐOẠN:
\"\"\"
{sample_text[:25000]}
\"\"\""""


# Alias requested by pipeline orchestrator / Agent 2 callers
build_glossary_extract_prompt = build_book_profile_prompt


def build_editor_system_prompt(
    target_lang: str = "Tiếng Việt",
    custom_prompt: str = "",
    glossary: Optional[List[Dict[str, str]]] = None,
    is_batch: bool = True,
    book_brief: str = "",
) -> str:
    """
    System prompt for Agent 4: Chief Editor & Literary Critic Agent.
    Reviews draft translations against source text, polishes stiff calques, strictly enforces
    100% of mandatory glossary terms, and preserves all inline HTML tags (<b>, <i>, <sup>).
    """
    base = f"""Bạn là Tổng Biên tập xuất bản kỳ cựu (Agent 4 - Chief Editor), hiệu đính bản dịch sang {target_lang}. Bạn nhận từng đoạn gồm **SOURCE** (gốc) và **DRAFT** (bản dịch nháp) và trả về bản dịch hoàn thiện đạt chuẩn in sách.

### NGUYÊN TẮC HIỆU ĐÍNH CỐT LÕI:
1. **Diệt tận gốc "mùi văn dịch" & lỗi dịch sát chữ (calque)**:
   - Chủ động phát hiện và chuốt lại ngay các cấu trúc bị động dịch máy hoặc cụm từ bám chữ tiếng Anh gượng gạo:
     + Sửa cấu trúc bị động/lủng củng kiểu `"được thiết kế để bởi..."`, `"bị/được thực hiện bởi..."` thành câu chủ động tự nhiên.
     + Sửa các cụm dịch thô: `"những người thực hành lớp học"` / `"người thực hành"` → `"giáo viên thực hành"`; `"các thực hành"` → `"các biện pháp thực hành"`; `"người huấn luyện"` → `"chuyên gia huấn luyện"`; `"nhìn-nghĩ-làm"` → `"Quan sát – Suy ngẫm – Hành động"`; `"quan sát tập trung"` → `"quan sát trọng tâm"`; `"buổi họp phản hồi"` → `"phản hồi - trao đổi"` / `"buổi họp tổng kết & phản hồi"`.
     + Cắt bỏ các hư từ thừa ở đầu câu hoặc trong ô bảng hẹp như `"Việc thực hiện..."`, `"Một cách..."`, `"Sự..."`, `"Những..."` để câu văn súc tích, gãy gọn.
2. **Thực thi nghiêm ngặt 100% BẢNG THUẬT NGỮ BẮT BUỘC (GLOSSARY) & BOOK BRIEF**:
   - Đối chiếu từng câu giữa **SOURCE** và **DRAFT**: nếu **SOURCE** chứa thuật ngữ nằm trong **GLOSSARY** mà **DRAFT** dịch lệch hoặc bỏ sót, BẮT BUỘC sửa lại đúng 100% theo **GLOSSARY** (kể cả chữ viết tắt trong ngoặc đơn như `(PBC)`, `(TPOT)` nếu có trong GLOSSARY).
3. **Bảo toàn tuyệt đối thẻ định dạng Inline HTML (`<b>...</b>`, `<i>...</i>`, `<sup>...</sup>`) & dữ liệu**:
   - BẮT BUỘC giữ nguyên mọi thẻ định dạng inline `<b>...</b>`, `<i>...</i>`, `<sup>...</sup>` có trong **SOURCE** / **DRAFT**, bọc đúng từ/cụm từ Tiếng Việt tương ứng; không làm mất thẻ, không thêm thẻ lạ hay đổi sang Markdown (`**`, `*`).
   - Giữ nguyên 100% ý nghĩa, số liệu, tên riêng, công thức, URL, chuỗi gạch chân điền khuyết (`_____`), ô tích (`☐`, `☑`), và các dấu `⟦n⟧`, `⟦/n⟧`, `⟦n/⟧` (nếu có).
   - Nếu **SOURCE** là nhãn ngắn trong bảng/biểu mẫu hoặc tiêu đề đầu/cuối trang, bản hiệu đính phải cô đọng tương đương để vừa khít khung bố cục PDF.
4. **Không sửa vô lối khi câu đã mượt**:
   - Nếu **DRAFT** đã chuẩn 100% thuật ngữ GLOSSARY, bảo toàn đúng thẻ `<b>/<i>/<sup>`, đúng nghĩa và đọc tự nhiên như chuyên gia bản ngữ viết: giữ nguyên.
"""
    blocks = [base]
    if custom_prompt and custom_prompt.strip():
        blocks.append(f"### CHỈ DẪN VĂN PHONG ĐẶC BIỆT:\n{custom_prompt.strip()}")
    if book_brief and book_brief.strip():
        blocks.append(f"### BOOK BRIEF (bắt buộc tuân thủ):\n{book_brief.strip()}")
    glossary_str = format_glossary_block(glossary)
    if glossary_str:
        blocks.append(glossary_str)

    if is_batch:
        blocks.append(
            """### ĐỊNH DẠNG ĐẦU RA BẮT BUỘC:
- Giữ nguyên chính xác các thẻ đánh dấu `[[#0]]`, `[[#1]]`, `[[#2]]`... ở đầu mỗi đoạn đã biên tập.
- Bảo toàn mọi thẻ inline `<b>...</b>`, `<i>...</i>`, `<sup>...</sup>` bên trong từng đoạn (nếu có).
- Chỉ trả về duy nhất nội dung bản dịch ĐÃ HIỆU ĐÍNH kèm thẻ `[[#i]]` (KHÔNG trả lại phần SOURCE hay lời bình luận)."""
        )
    else:
        blocks.append(
            "### ĐỊNH DẠNG ĐẦU RA:\nChỉ trả về duy nhất bản dịch đã hiệu đính hoàn chỉnh (giữ nguyên các thẻ `<b>...</b>`, `<i>...</i>`, `<sup>...</sup>` nếu có), không giải thích."
        )
    return "\n\n".join(blocks)


def build_editor_batch_user_prompt(
    sources: List[str], drafts: List[str]
) -> str:
    parts = []
    for idx, (src, dft) in enumerate(zip(sources, drafts)):
        parts.append(
            f"[[#{idx}]]\n[SOURCE]: {src.strip()}\n[DRAFT]: {dft.strip()}"
        )
    return "\n\n".join(parts)


def format_book_brief(profile: Dict[str, object]) -> str:
    """Biến hồ sơ do Agent 2 lập thành BOOK BRIEF giàu tính hành động để chèn vào mọi prompt dịch & biên tập."""
    lines: List[str] = []
    for key, label in (
        ("document_type", "Loại tài liệu"),
        ("title_translation", "Nhan đề tài liệu chuẩn"),
        ("understanding", "Hiểu về tài liệu"),
        ("genre", "Thể loại"),
        ("audience", "Độc giả"),
        ("tone", "Giọng văn"),
        ("narration", "Ngôi kể"),
        ("address_rules", "Quy ước xưng hô"),
        ("brief", "Định hướng dịch thuật & quy ước chuyên ngành"),
    ):
        val = str(profile.get(key) or "").strip()
        if val:
            lines.append(f"- {label}: {val}")
    notes = profile.get("translator_notes")
    if isinstance(notes, list):
        notes = " ".join(str(n).strip() for n in notes if str(n).strip())
    notes = str(notes or "").strip()
    if notes:
        lines.append(f"- Ghi chú của biên tập viên cho nhóm dịch: {notes}")
    # tương thích hồ sơ kiểu cũ (policies / style_rules)
    policies = profile.get("policies")
    if isinstance(policies, dict):
        for key, val in policies.items():
            if str(val or "").strip():
                lines.append(f"- {key}: {str(val).strip()}")
    rules = profile.get("style_rules")
    if isinstance(rules, list):
        for r in rules[:8]:
            if str(r).strip():
                lines.append(f"- Văn phong: {str(r).strip()}")
    keep = profile.get("do_not_translate")
    if isinstance(keep, list) and keep:
        lines.append("- Giữ nguyên gốc: " + ", ".join(str(k) for k in keep[:20]))
    return chr(10).join(lines)
