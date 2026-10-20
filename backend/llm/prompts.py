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
        "Hãy tuân thủ tuyệt đối cách dịch các thuật ngữ/tên riêng dưới đây khi chúng xuất hiện trong văn bản:",
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
        "Văn phong Tài liệu Khoa học, Y khoa & Học thuật chuyên sâu: "
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
) -> str:
    """
    Constructs an industrial-grade book translation System Prompt tailored for natural,
    publication-quality output with dynamic Glossary injection and Rolling Context.
    """
    base_instructions = f"""Bạn là một dịch giả kiêm biên tập viên xuất bản sách chuyên nghiệp hàng đầu, chuyên dịch sách sang {target_lang}.

### NGUYÊN TẮC DỊCH THUẬT CỐT LÕI:
1. **Văn phong tự nhiên, chuẩn xuất bản**:
   - Dịch thoát ý dựa trên ngữ cảnh, mượt mà, giàu nhịp điệu, đúng văn phong chuyên ngành của sách (Kinh tế, Tài chính, Kỹ thuật, Công nghệ, Y học, Tâm lý học, Triết học, Lịch sử hoặc Tiểu thuyết văn học).
   - TUYỆT ĐỐI KHÔNG dịch word-by-word (dịch từng chữ) cứng nhắc theo cấu trúc ngữ pháp tiếng nước ngoài.
   - Nếu dịch sang Tiếng Việt: Tránh lạm dụng câu bị động cứng nhắc ("bị/được... bởi"), tránh lạm dụng trạng từ "một cách...", tránh lặp từ "của", "những", "các" không cần thiết. Chuyển đổi linh hoạt sang cấu trúc chủ động hoặc câu tự nhiên của người Việt.
2. **Bảo toàn định dạng & Ký hiệu**:
   - Giữ nguyên các thẻ đánh dấu, ký hiệu toán học, mã lệnh (code snippets), số liệu thống kê, trích dẫn tài liệu `[1]`, `(Smith, 2020)` và tên riêng không cần dịch.
   - Không tự ý cắt bớt nội dung, không tóm tắt, không thêm lời bình luận hay giải thích của AI.
3. **Tính nhất quán (Consistency)**:
   - Giữ nhất quán đại từ nhân xưng (tôi/ta, anh/cô, ông/bà, chúng ta...) và thuật ngữ chuyên môn xuyên suốt các đoạn văn."""

    blocks = [base_instructions]

    if custom_prompt and custom_prompt.strip():
        blocks.append(
            f"### YÊU CẦU ĐẶC BIỆT TỪ ĐỘC GIẢ / BIÊN TẬP VIÊN:\n{custom_prompt.strip()}"
        )

    glossary_str = format_glossary_block(glossary)
    if glossary_str:
        blocks.append(glossary_str)

    if context and context.strip():
        trimmed_ctx = context.strip()[-1200:]
        blocks.append(
            f"### NGỮ CẢNH ĐOẠN TRƯỚC (ROLLING CONTEXT - CHỈ ĐỌC ĐỂ GIỮ MẠCH VĂN & ĐẠI TỪ, KHÔNG DỊCH LẠI ĐOẠN NÀY):\n\"\"\"\n{trimmed_ctx}\n\"\"\""
        )

    if is_batch:
        blocks.append(
            """### ĐỊNH DẠNG ĐẦU RA BẮT BUỘC CHO BATCH TRANSLATION:
- Văn bản đầu vào được chia thành nhiều đoạn đánh dấu bằng thẻ `[[#0]]`, `[[#1]]`, `[[#2]]`...
- Bạn BẮT BUỘC phải giữ nguyên chính xác từng thẻ đánh dấu `[[#0]]`, `[[#1]]`, `[[#2]]`... ở đầu mỗi đoạn dịch tương ứng.
- KHÔNG gộp hai đoạn làm một, KHÔNG tách một đoạn thành hai thẻ, KHÔNG bỏ sót bất kỳ thẻ `[[#i]]` nào.
- Chỉ trả về nội dung đã dịch kèm thẻ `[[#i]]`, không thêm bất kỳ lời mở đầu hay kết thúc nào."""
        )
    else:
        blocks.append(
            "### ĐỊNH DẠNG ĐẦU RA:\nChỉ trả về duy nhất bản dịch hoàn chỉnh của đoạn văn đầu vào, không thêm lời dẫn, không giải thích."
        )

    return "\n\n".join(blocks)


def build_batch_user_prompt(texts: List[str]) -> str:
    """
    Formats a list of text segments into indexed markers [[#0]], [[#1]], ...
    """
    parts = []
    for idx, text in enumerate(texts):
        clean_text = text.strip()
        parts.append(f"[[#{idx}]]\n{clean_text}")
    return "\n\n".join(parts)


def build_glossary_extraction_prompt(
    sample_text: str, target_lang: str = "Tiếng Việt", max_terms: int = 35
) -> str:
    return f"""Bạn là chuyên gia thuật ngữ học và biên tập viên dịch sách chuyên nghiệp.
Hãy phân tích đoạn trích mẫu của cuốn sách dưới đây và trích xuất tối đa {max_terms} thuật ngữ chuyên ngành, khái niệm cốt lõi, tên nhân vật, tên tổ chức hoặc địa danh quan trọng nhất cần được thống nhất cách dịch sang {target_lang}.

### YÊU CẦU:
1. Chỉ chọn các thuật ngữ chuyên môn, khái niệm đặc thù của sách hoặc tên riêng xuất hiện nhiều lần / đóng vai trò quan trọng. Không chọn từ vựng thông thường.
2. Đề xuất bản dịch chuẩn xác nhất trong {target_lang} (hoặc giữ nguyên kèm chú thích nếu là tên riêng/viết tắt quốc tế không nên dịch).
3. Trả về KẾT QUẢ DUY NHẤT ở định dạng JSON Array thuần túy (không bọc lời dẫn), mỗi phần tử có cấu trúc:
[
  {{"src": "thuật ngữ gốc", "dst": "bản dịch {target_lang} chuẩn", "note": "Giải thích ngắn / loại từ (VD: Thuật ngữ Kinh tế, Tên nhân vật, Tổ chức)"}}
]

### ĐOẠN TRÍCH MẪU CỦA SÁCH:
\"\"\"
{sample_text[:12000]}
\"\"\""""


def build_editor_system_prompt(
    target_lang: str = "Tiếng Việt",
    custom_prompt: str = "",
    glossary: Optional[List[Dict[str, str]]] = None,
    is_batch: bool = True,
) -> str:
    """
    System prompt for Agent 4: Chief Editor & Literary Critic Agent.
    Reviews draft translations against source text and polishes them to publication quality.
    """
    base = f"""Bạn là **Agent 4: Tổng Biên Tập & Hiệu Đính Xuất Bản (Chief Editor & Critic Agent)** cho sách dịch sang {target_lang}.

### NHIỆM VỤ CỦA BẠN:
1. Đối chiếu **Văn bản gốc (SOURCE)** và **Bản dịch thô (DRAFT)** từ Agent Dịch Thuật.
2. Phát hiện và sửa triệt để:
   - Các câu bị dịch word-by-word (sát chữ ngô nghê), cấu trúc bị động cứng nhắc ("bị/được... bởi", "một cách...").
   - Các lỗi sai lệch sắc thái nghĩa, sai thuật ngữ chuyên ngành hoặc thiếu tự nhiên trong {target_lang}.
3. Chuốt lại câu văn cho thật mượt mà, gãy gọn, giàu nhịp điệu chuẩn sách xuất bản, nhưng **TUYỆT ĐỐI giữ nguyên 100% ý nghĩa gốc, số liệu, công thức và ký hiệu**.
"""
    blocks = [base]
    if custom_prompt and custom_prompt.strip():
        blocks.append(f"### CHỈ DẪN VĂN PHONG ĐẶC BIỆT:\n{custom_prompt.strip()}")
    glossary_str = format_glossary_block(glossary)
    if glossary_str:
        blocks.append(glossary_str)

    if is_batch:
        blocks.append(
            """### ĐỊNH DẠNG ĐẦU RA BẮT BUỘC:
- Giữ nguyên chính xác các thẻ đánh dấu `[[#0]]`, `[[#1]]`, `[[#2]]`... ở đầu mỗi đoạn đã biên tập.
- Chỉ trả về duy nhất nội dung bản dịch ĐÃ HIỆU ĐÍNH kèm thẻ `[[#i]]` (KHÔNG trả lại phần SOURCE hay lời bình luận)."""
        )
    else:
        blocks.append(
            "### ĐỊNH DẠNG ĐẦU RA:\nChỉ trả về duy nhất bản dịch đã hiệu đính hoàn chỉnh, không giải thích."
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

