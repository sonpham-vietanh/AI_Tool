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
    book_brief: str = "",
    term_candidates: Optional[List[str]] = None,
) -> str:
    """
    Constructs an industrial-grade book translation System Prompt tailored for natural,
    publication-quality output with dynamic Glossary injection and Rolling Context.
    """
    base_instructions = f"""Bạn là dịch giả sách chuyên nghiệp kiêm biên tập viên xuất bản, dịch sang {target_lang}. Bản dịch của bạn phải đọc như sách được viết gốc bằng {target_lang} bởi một tác giả giỏi, KHÔNG phải văn dịch.

### QUY TRÌNH (làm thầm trong đầu, chỉ xuất bản dịch cuối cùng):
1. Hiểu đúng ý, giọng điệu và mục đích của đoạn văn trong mạch sách (xem BOOK BRIEF nếu có).
2. Viết lại trọn ý bằng cấu trúc câu tự nhiên của {target_lang}, KHÔNG bám cấu trúc ngữ pháp tiếng gốc.
3. Tự rà soát trước khi trả lời: có câu nào nghe như văn dịch, thừa chữ, sai sắc thái, sai thuật ngữ, lệch xưng hô, hay bỏ sót ý không? Sửa ngay rồi mới xuất.

### NGUYÊN TẮC:
1. **Tự nhiên, chuẩn xuất bản**: dịch thoát ý, nhịp câu có lên xuống, đúng giọng của thể loại. Không dịch word-by-word.
   - Với Tiếng Việt: hạn chế câu bị động "bị/được ... bởi", trạng từ "một cách ...", lặp "của/những/các/việc/sự" không cần thiết; ưu tiên câu chủ động, ngắt câu dài thành câu ngắn khi cần; thành ngữ/ẩn dụ chuyển bằng cách nói tương đương của người Việt thay vì dịch nghĩa đen.
2. **Trung thực tuyệt đối**: không bỏ sót, không tóm tắt, không thêm ý, không bình luận hay giải thích của AI. Giữ nguyên số liệu, công thức, code, URL, trích dẫn dạng `[1]`, `(Smith, 2020)`.
3. **Nhất quán**: cùng một thuật ngữ, tên riêng, cách xưng hô phải được dịch giống nhau xuyên suốt cuốn sách. GLOSSARY và BOOK BRIEF là bắt buộc.
4. **Tên riêng & thuật ngữ**: theo GLOSSARY; nếu chưa có thì theo quy ước trong BOOK BRIEF. KHÔNG tự chèn bản gốc tiếng nước ngoài trong ngoặc đơn (các đoạn được dịch độc lập nên không biết đâu là "lần đầu"); chỉ giữ ngoặc khi bản gốc đã có ngoặc.
5. **Thẻ định dạng**: giữ nguyên chính xác các dấu ⟦n⟧ ⟦/n⟧ ⟦n/⟧ (nếu có), đặt chúng vào đúng từ/cụm tương ứng trong bản dịch; không bỏ, không thêm, không đổi số.
6. **Nhãn & mã viết tắt ngắn**: những đoạn ngắn kiểu mã/ký hiệu/viết tắt/tên riêng (VD: FO-Karen, M-Tyrel, TPOT, Q&A, A1) giữ nguyên, KHÔNG mở rộng hay giải thích. Với đoạn là nhãn ngắn của bảng/biểu mẫu, dịch ngắn gọn tương đương độ dài bản gốc."""

    blocks = [base_instructions]

    if custom_prompt and custom_prompt.strip():
        blocks.append(
            f"### YÊU CẦU ĐẶC BIỆT TỪ ĐỘC GIẢ / BIÊN TẬP VIÊN:\n{custom_prompt.strip()}"
        )

    if book_brief and book_brief.strip():
        blocks.append(f"### BOOK BRIEF (đặc điểm & quy ước dịch của CUỐN SÁCH NÀY, bắt buộc tuân thủ):\n{book_brief.strip()}")

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
    book_brief: str = "",
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
4. Giữ nguyên chính xác các dấu ⟦n⟧ ⟦/n⟧ ⟦n/⟧ (nếu có), đặt chúng vào đúng từ/cụm tương ứng trong bản dịch; không bỏ, không thêm, không đổi số.
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


def build_book_profile_prompt(
    sample_text: str, target_lang: str = "Tiếng Việt", max_terms: int = 40
) -> str:
    """Agent 2: phân tích cuốn sách một lần để tạo BOOK BRIEF + glossary, thay cho việc người dùng tự viết prompt."""
    return f"""Bạn là tổng biên tập một nhà xuất bản, chuẩn bị "hồ sơ dịch thuật" để một nhóm dịch giả dịch cuốn sách này sang {target_lang} thật nhất quán và hay.
Dưới đây là các đoạn trích lấy từ đầu, giữa và cuối sách. Hãy phân tích và trả về DUY NHẤT một JSON object (không lời dẫn, không markdown) có cấu trúc:

{{
  "genre": "thể loại cụ thể (VD: sách kinh doanh, tiểu thuyết trinh thám, kỹ thuật phần mềm, tự truyện...)",
  "audience": "độc giả mục tiêu",
  "tone": "giọng văn của tác giả (VD: hài hước, học thuật, truyền cảm hứng, kể chuyện ngôi thứ nhất...)",
  "narration": "ngôi kể / cách tác giả xưng hô với độc giả",
  "address_rules": "quy ước xưng hô khi dịch sang {target_lang} (VD: tác giả xưng 'tôi', gọi độc giả là 'bạn'; nhân vật A gọi B là 'anh'...). Nêu cụ thể theo quan hệ nhân vật nếu là tiểu thuyết",
  "style_rules": ["5-8 quy tắc văn phong cụ thể để bản dịch hay và đúng chất sách này"],
  "do_not_translate": ["tên/thuật ngữ nên giữ nguyên tiếng gốc"],
  "glossary": [{{"src": "thuật ngữ/tên riêng gốc", "dst": "cách dịch chuẩn", "note": "loại, ngắn gọn"}}]
}}

Yêu cầu glossary: tối đa {max_terms} mục; chỉ gồm tên nhân vật, địa danh, tổ chức, khái niệm/thuật ngữ đặc thù xuất hiện nhiều hoặc quan trọng; KHÔNG đưa từ vựng thông thường.

### ĐOẠN TRÍCH:
\"\"\"
{sample_text[:18000]}
\"\"\""""


def format_book_brief(profile: Dict[str, object]) -> str:
    """Biến profile JSON thành đoạn BOOK BRIEF ngắn gọn để chèn vào mọi prompt dịch."""
    lines: List[str] = []
    for key, label in (
        ("genre", "Thể loại"),
        ("audience", "Độc giả"),
        ("tone", "Giọng văn"),
        ("narration", "Ngôi kể"),
        ("address_rules", "Quy ước xưng hô"),
    ):
        val = str(profile.get(key) or "").strip()
        if val:
            lines.append(f"- {label}: {val}")
    rules = profile.get("style_rules")
    if isinstance(rules, list):
        for r in rules[:8]:
            if str(r).strip():
                lines.append(f"- Văn phong: {str(r).strip()}")
    keep = profile.get("do_not_translate")
    if isinstance(keep, list) and keep:
        lines.append("- Giữ nguyên gốc: " + ", ".join(str(k) for k in keep[:20]))
    return chr(10).join(lines)
