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
    base_instructions = f"""Bạn là dịch giả sách chuyên nghiệp kiêm biên tập viên xuất bản hàng đầu, dịch sang {target_lang}. Bản dịch của bạn phải đọc mượt mà, súc tích, chuẩn mực như sách được viết gốc bằng {target_lang} bởi một chuyên gia bản ngữ, TUYỆT ĐỐI KHÔNG mang mùi văn dịch máy.

### QUY TRÌNH (làm thầm trong đầu, chỉ xuất bản dịch cuối cùng):
1. Hiểu đúng ý, giọng điệu, vai trò chuyên môn và mục đích của đoạn văn trong mạch sách (xem BOOK BRIEF và GLOSSARY nếu có).
2. Diễn đạt lại trọn vẹn ý nghĩa bằng cấu trúc câu và thuật ngữ chuyên ngành tự nhiên của {target_lang}, thoát khỏi vỏ ngữ pháp tiếng gốc.
3. Tự kiểm duyệt khắt khe trước khi trả lời: phát hiện và sửa ngay mọi cụm từ dịch sát chữ (calque), danh từ hóa gượng gạo, lặp từ, lệch xưng hô hoặc sai thuật ngữ.

### NGUYÊN TẮC CỐT LÕI:
1. **Tự nhiên, chuẩn xuất bản & đúng thuật ngữ chuyên ngành**:
   - Với Tiếng Việt: tránh tối đa câu bị động "bị/được ... bởi", trạng từ "một cách ...", và thói quen dịch calque danh từ hóa cứng nhắc (VD: KHÔNG dịch "practices" thành "các thực hành" trơ trọi mà phải dịch là "các biện pháp thực hành / thông lệ thực hành / phương pháp thực hành"; KHÔNG dịch "practitioner" thành "người thực hành" ngây ngô mà dịch đúng vai trò chuyên môn như "giáo viên thực hành / chuyên viên thực hành"; KHÔNG dịch "coach" thành "người huấn luyện" khô cứng trong ngữ cảnh đào tạo/sư phạm mà dùng "chuyên gia huấn luyện / giảng viên huấn luyện / huấn luyện viên"; KHÔNG dịch quy trình "Look-Think-Act" thành khẩu ngữ "Nhìn-Nghĩ-Làm" mà dùng "Quan sát – Suy ngẫm – Hành động"; "debriefing session" dịch là "buổi họp tổng kết & phản hồi"; "focused observation" dịch là "quan sát trọng tâm"; "caseload" dịch là "khối lượng phụ trách / danh sách phụ trách").
   - Ưu tiên câu chủ động, nhịp câu gãy gọn, rõ nghĩa; ngắt câu dài nhiều mệnh đề phụ thành các vế mạch lạc.
2. **Trung thực tuyệt đối**: không bỏ sót, không tóm tắt, không thêm ý, không bình luận hay giải thích của AI. Giữ nguyên số liệu, công thức, code, URL, chuỗi gạch chân điền khuyết (`_____`), trích dẫn dạng `[1]`, `(Smith, 2020)`.
3. **Nhất quán tuyệt đối**: cùng một nhan đề sách, tiêu đề mục lục, thuật ngữ, tên riêng, cách xưng hô phải được dịch giống hệt nhau xuyên suốt cuốn sách (từ Bìa, Mục lục, Tiêu đề đầu trang cho tới Thân bài và Phụ lục). GLOSSARY và BOOK BRIEF là bắt buộc.
4. **Tên riêng & thuật ngữ**: tuân thủ GLOSSARY và BOOK BRIEF. KHÔNG tự ý chèn thêm bản gốc tiếng nước ngoài trong ngoặc đơn nếu bản gốc không có ngoặc.
5. **Thẻ định dạng**: giữ nguyên chính xác các dấu ⟦n⟧ ⟦/n⟧ ⟦n/⟧ (nếu có), đặt chúng vào đúng từ/cụm tương ứng trong bản dịch; không bỏ, không thêm, không đổi số.
6. **Nhãn bảng, biểu mẫu & mã viết tắt ngắn**: những đoạn ngắn kiểu mã/ký hiệu/viết tắt kèm tên (VD: FO-Karen, M-Tyrel, TPOT, Q&A, A1) giữ nguyên hoặc chỉ dịch phần từ chung ngắn gọn (như "Yes No" → "Có Không", "Page ___ of ___" → "Trang ___ / ___"). Với tiêu đề cột bảng, ô biểu mẫu hoặc sơ đồ, dịch cô đọng, súc tích để vừa khít khung chứa gốc."""

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


def build_batch_user_prompt(texts: List[str], hints: Optional[List[str]] = None) -> str:
    """
    Formats a list of text segments into indexed markers [[#0]], [[#1]], ...
    `hints` (tùy chọn): vai trò/vị trí của từng đoạn trong trang (máy suy ra từ bố cục) để AI tự hiểu ngữ cảnh.
    """
    parts = []
    for idx, text in enumerate(texts):
        clean_text = text.strip()
        parts.append(f"[[#{idx}]]\n{clean_text}")
    body = "\n\n".join(parts)
    notes = [f"#{i}: {h}" for i, h in enumerate(hints or []) if h]
    if not notes:
        return body
    return (
        "[VAI TRÒ/VỊ TRÍ CÁC ĐOẠN TRONG TRANG - chỉ để bạn hiểu ngữ cảnh khi dịch; KHÔNG dịch và KHÔNG chép lại phần này]\n"
        + "\n".join(notes)
        + "\n\n[CÁC ĐOẠN CẦN DỊCH]\n"
        + body
    )


def build_glossary_extraction_prompt(
    sample_text: str, target_lang: str = "Tiếng Việt", max_terms: int = 40
) -> str:
    return f"""Bạn là chuyên gia thuật ngữ học và tổng biên tập dịch sách chuyên nghiệp.
Hãy phân tích đoạn trích mẫu của cuốn sách dưới đây và trích xuất tối đa {max_terms} thuật ngữ chuyên ngành, nhan đề tài liệu, tiêu đề chương mục/biểu mẫu cốt lõi, tên nhân vật, tên tổ chức hoặc địa danh quan trọng nhất cần được thống nhất cách dịch sang {target_lang}.

### YÊU CẦU:
1. Bắt buộc bao gồm nhan đề chính của sách/tài liệu, các thuật ngữ khung phương pháp luận và tên biểu mẫu/công cụ lặp lại nhiều lần. Không chọn từ vựng thông thường.
2. Đề xuất bản dịch chuẩn xuất bản, tự nhiên và giàu tính chuyên môn trong {target_lang} (TUYỆT ĐỐI tránh dịch nghĩa đen từng chữ kiểu "người thực hành" cho "practitioner", "các thực hành" cho "practices", hay "nhìn-nghĩ-làm" cho "look-think-act").
3. Trả về KẾT QUẢ DUY NHẤT ở định dạng JSON Array thuần túy (không bọc lời dẫn), mỗi phần tử có cấu trúc:
[
  {{"src": "thuật ngữ gốc", "dst": "bản dịch {target_lang} chuẩn xuất bản (không kèm tiếng Anh trong ngoặc)", "note": "Giải thích ngắn / loại từ"}}
]

### ĐOẠN TRÍCH MẪU CỦA SÁCH:
\"\"\"
{sample_text[:15000]}
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
    base = f"""Bạn là Tổng Biên tập xuất bản kỳ cựu, hiệu đính bản dịch sang {target_lang}. Bạn nhận từng đoạn gồm **SOURCE** (gốc) và **DRAFT** (bản dịch nháp) và trả về bản dịch hoàn thiện đạt chuẩn in sách.

### NGUYÊN TẮC HIỆU ĐÍNH:
1. **Diệt tận gốc "mùi văn dịch" & lỗi dịch sát chữ (calque)**:
   - Chủ động phát hiện và chuốt lại ngay các cụm dịch thô/bám chữ tiếng Anh: ví dụ "các thực hành" → "các biện pháp thực hành", "người thực hành" → "giáo viên/chuyên viên thực hành", "người huấn luyện" → "chuyên gia huấn luyện", "nhìn-nghĩ-làm" → "Quan sát – Suy ngẫm – Hành động", "buổi họp phản hồi theo hướng suy ngẫm và phản hồi" (lặp từ) → "buổi họp tổng kết & phản hồi trên tinh thần suy ngẫm", câu bị động "được/bị ... bởi", hoặc thừa từ "việc/sự/một cách" không cần thiết.
   - Đảm bảo 100% thuật ngữ và nhan đề tuân thủ đúng **GLOSSARY** và **BOOK BRIEF**.
2. **Không sửa vô lối khi câu đã mượt**:
   - Nếu DRAFT đã chuẩn thuật ngữ, đúng nghĩa và đọc tự nhiên, súc tích như người bản ngữ viết: giữ nguyên.
3. **Bảo toàn cấu trúc & dữ liệu tuyệt đối**:
   - Giữ nguyên 100% ý nghĩa, số liệu, tên riêng, công thức, URL, chuỗi gạch chân điền khuyết (`_____`), ký hiệu. Nếu SOURCE là nhãn ngắn trong bảng/biểu mẫu hoặc vế danh sách ngắn, bản dịch phải cô đọng tương đương để không tràn khung bố cục.
   - Giữ nguyên chính xác các dấu ⟦n⟧ ⟦/n⟧ ⟦n/⟧ (nếu có), đặt chúng vào đúng từ/cụm tương ứng; không bỏ, không thêm, không đổi số.
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
    sample_text: str, target_lang: str = "Tiếng Việt", max_terms: int = 50, layout_hint: str = ""
) -> str:
    """Agent 2: AI đọc tài liệu, tự hiểu nó là gì, dùng để làm gì, rồi tự viết ghi chú dịch thuật cho ĐÚNG tài liệu này
    (không có danh sách ô cố định phải điền)."""
    layout_block = (
        f"\n### THÔNG TIN BỐ CỤC CỦA FILE (do máy đo, chỉ để tham khảo khi suy luận):\n{layout_hint.strip()}\n"
        if layout_hint and layout_hint.strip()
        else ""
    )
    return f"""Bạn là Tổng Biên tập kiêm dịch giả trưởng giàu kinh nghiệm, sắp dẫn dắt nhóm dịch tài liệu dưới đây sang {target_lang}.
Trước khi dịch, hãy ĐỌC KỸ đoạn trích (gồm trang bìa/mục lục, phần đầu, phần giữa, phần cuối/phụ lục) và xác lập quy chuẩn dịch thuật đạt chất lượng xuất bản cao nhất cho tài liệu này.

### LƯU Ý QUAN TRỌNG KHI LẬP HỒ SƠ & BẢNG THUẬT NGỮ (GLOSSARY):
1. **Chống dịch sát chữ (anti-calque)**: Tuyệt đối không dịch nghĩa đen ngây ngô từng từ đơn. Hãy chọn thuật ngữ chuyên ngành chuẩn mực, tự nhiên trong {target_lang}:
   - Ví dụ trong giáo dục/đào tạo: "Practitioner" dịch là "Giáo viên thực hành / Chuyên viên thực hành" (KHÔNG dịch "Người thực hành"); "Coach" dịch là "Chuyên gia huấn luyện / Giảng viên huấn luyện" (KHÔNG dịch "Người huấn luyện"); "Practices" dịch là "Các biện pháp thực hành / Thông lệ thực hành" (KHÔNG dịch "Các thực hành"); "Focused Observation" dịch là "Quan sát trọng tâm"; "Debriefing Session" dịch là "Buổi họp tổng kết & phản hồi"; "Look-Think-Act" dịch là "Quan sát – Suy ngẫm – Hành động"; "Caseload" dịch là "Khối lượng phụ trách / Danh sách phụ trách".
2. **Đồng bộ Nhan đề sách, Đề mục Mục lục & Biểu mẫu**:
   - BẮT BUỘC đưa **Nhan đề chính của sách/tài liệu** (thường lặp lại ở trang bìa và tiêu đề đầu/cuối mỗi trang), **Tên các Chương/Phần lớn** trong Mục lục, và **Tên các Biểu mẫu/Bảng kiểm (Checklist/Form)** vào `glossary` để toàn bộ các trang dịch khớp nhau 100%.

Trả về DUY NHẤT một JSON object hợp lệ (không lời dẫn, không bọc markdown):

{{
  "document_type": "mô tả cụ thể loại tài liệu và lĩnh vực chuyên môn",
  "title_translation": "bản dịch chuẩn xuất bản của nhan đề chính cuốn sách/tài liệu",
  "understanding": "1-3 câu: tài liệu nói về gì, ai viết cho ai, dùng vào mục đích gì",
  "translator_notes": "chỉ dẫn biên tập chi tiết và sắc bén cho nhóm dịch: cách chọn từ vựng chuyên môn, cách tránh các bẫy dịch word-by-word trong tài liệu này, yêu cầu cô đọng cho các ô bảng/biểu mẫu/sơ đồ",
  "address_rules": "quy ước xưng hô thống nhất khi dịch sang {target_lang} (VD: xưng 'bạn' với độc giả/chuyên gia huấn luyện, gọi 'giáo viên', 'trẻ'...), để rỗng nếu không áp dụng",
  "do_not_translate": ["các mã viết tắt, URL, tên miền, ký hiệu giữ nguyên gốc"],
  "glossary": [{{"src": "thuật ngữ/nhan đề/đề mục gốc", "dst": "bản dịch {target_lang} chuẩn xuất bản, KHÔNG kèm tiếng gốc trong ngoặc", "note": "loại thuật ngữ"}}]
}}

Yêu cầu glossary: tối đa {max_terms} mục quan trọng nhất (ưu tiên nhan đề tài liệu, thuật ngữ cốt lõi, tên biểu mẫu, tên tổ chức, chức danh); KHÔNG đưa từ vựng thông thường.
{layout_block}
### ĐOẠN TRÍCH:
\"\"\"
{sample_text[:18000]}
\"\"\""""


def format_book_brief(profile: Dict[str, object]) -> str:
    """Biến hồ sơ do AI tự viết thành BOOK BRIEF để chèn vào mọi prompt dịch."""
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
