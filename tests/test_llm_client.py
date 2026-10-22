import asyncio

import pytest

from backend.llm.client import LLMConfig, TranslationLLMClient
from helpers import FakeLLMClient


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- LLMConfig
def test_config_defaults():
    c = LLMConfig()
    assert c.concurrency == 12
    assert c.batch_chars == 5000 and c.batch_items == 24
    assert c.target_lang == "Tiếng Việt" and c.target_lang_code == "vi"
    assert c.bilingual is False
    assert c.enable_editor_agent is False
    assert c.fallback_to_google_free is True
    assert c.api_keys == []


def test_config_key_parsing_string_and_list():
    c = LLMConfig(api_keys="k1, k2;k3\nk4\r\n  ,, ", gemini_api_keys=["a,b", " c ", 5])
    assert c.api_keys == ["k1", "k2", "k3", "k4"]
    assert c.gemini_api_keys == ["a", "b", "c"]
    assert LLMConfig(api_keys=None).api_keys == []


def test_config_provider_normalized():
    assert LLMConfig(provider="  OpenAI ").provider == "openai"
    assert LLMConfig(provider="").provider == "google_free"


def test_config_lang_code_resolved():
    assert LLMConfig(target_lang="English").target_lang_code == "en"
    assert LLMConfig(target_lang="Japanese").target_lang_code == "ja"


@pytest.mark.parametrize("field,val", [("concurrency", 0), ("concurrency", 99), ("temperature", 3), ("batch_items", 0)])
def test_config_bounds(field, val):
    with pytest.raises(Exception):
        LLMConfig(**{field: val})


def test_effective_provider_no_keys_falls_back_to_free(cache):
    c = TranslationLLMClient(LLMConfig(provider="openai", api_keys=[]), cache=cache)
    assert c._effective_provider() == "google_free"
    assert c._effective_model() == "gtx-free"


# --------------------------------------------------- _parse_batch_response
P = TranslationLLMClient._parse_batch_response


def test_parse_ok():
    assert P("[[#0]]\nA\n\n[[#1]]\nB", 2) == ["A", "B"]


def test_parse_tolerates_spacing_and_multiline():
    assert P("[[ #0 ]] one\nline2\n[[#1]]two", 2) == ["one\nline2", "two"]


def test_parse_wrong_count_or_empty():
    assert P("[[#0]] a", 2) is None
    assert P("[[#0]] a\n[[#1]] b\n[[#2]] c", 2) is None
    assert P("", 1) is None
    assert P("[[#0]] a", 0) is None


def test_parse_empty_segment_rejected():
    assert P("[[#0]]\n[[#1]] b", 2) is None


def test_parse_wrong_indices_rejected():
    assert P("[[#0]] a\n[[#5]] b", 2) is None


def test_parse_out_of_order_markers_realigned():
    assert P("[[#1]] b\n[[#0]] a", 2) == ["a", "b"]


# ------------------------------------------------------------ translate_batch
def test_batch_happy_path_and_cache(make_client):
    cl = make_client()
    out = run(cl.translate_batch(["Hello world", "Second para"]))
    assert out == ["VI(Hello world)", "VI(Second para)"]
    assert len(cl.calls) == 1
    # second time: everything is cached -> no new LLM call
    out2 = run(cl.translate_batch(["Hello world", "Second para"]))
    assert out2 == out and len(cl.calls) == 1


def test_batch_blank_items_passthrough_and_order(make_client):
    cl = make_client()
    out = run(cl.translate_batch(["A text", "   ", "B text"]))
    assert out == ["VI(A text)", "   ", "VI(B text)"]


def test_batch_empty(make_client):
    assert run(make_client().translate_batch([])) == []


def test_batch_single_uncached_uses_translate_text(make_client):
    cl = make_client()
    out = run(cl.translate_batch(["Only one"]))
    assert out == ["VI(Only one)"]
    assert "[[#0]]" not in cl.calls[0][1]


def test_batch_marker_loss_splits_in_half(make_client):
    def handler(sys_p, user_p, model):
        # Fails (drops a marker) when asked for >2 items at once.
        if "[[#2]]" in user_p:
            return "[[#0]] x\n[[#1]] y"
        return FakeLLMClient.default_handler(sys_p, user_p, model)

    cl = make_client(handler=handler)
    texts = ["t1 aa", "t2 bb", "t3 cc", "t4 dd"]
    out = run(cl.translate_batch(texts))
    assert out == [f"VI({t})" for t in texts]
    assert len(cl.calls) == 3  # 1 failed full batch + 2 halves


def test_batch_api_error_does_not_split_and_raises_without_fallback(make_client):
    # Lỗi API (không phải lệch marker) => không chia đôi, chỉ 1 lượt gọi, rồi raise (fallback tắt).
    def handler(sys_p, user_p, model):
        raise RuntimeError("boom")

    cl = make_client(handler=handler)
    with pytest.raises(RuntimeError, match="boom"):
        run(cl.translate_batch(["one one", "two two"]))
    assert len(cl.calls) == 1


def test_batch_circuit_breaker_stops_after_repeated_api_errors(make_client):
    def handler(sys_p, user_p, model):
        raise RuntimeError("quota exceeded")

    cl = make_client(handler=handler, fallback_to_google_free=True)

    async def fake_google(text, target_lang_code=None):
        return "G:" + text

    cl._translate_google_free_raw = fake_google
    # 2 batch đầu: rơi về Google, batch thứ 3: ngắt mạch bằng lỗi rõ ràng
    run(cl.translate_batch(["a a", "b b"]))
    run(cl.translate_batch(["c c", "d d"]))
    with pytest.raises(RuntimeError, match="Tiếp tục"):
        run(cl.translate_batch(["e e", "f f"]))


def test_batch_success_resets_failure_counter(make_client):
    state = {"fail": True}

    def handler(sys_p, user_p, model):
        if state["fail"]:
            raise RuntimeError("temporary")
        return "[[#0]] X0" + chr(10) + "[[#1]] X1"

    cl = make_client(handler=handler, fallback_to_google_free=True)

    async def fake_google(text, target_lang_code=None):
        return "G:" + text

    cl._translate_google_free_raw = fake_google
    run(cl.translate_batch(["a a", "b b"]))
    assert cl._consecutive_batch_failures == 1
    state["fail"] = False
    run(cl.translate_batch(["c c", "d d"]))
    assert cl._consecutive_batch_failures == 0


def test_batch_cache_isolated_per_model(make_client, cache):
    a = make_client(model="model-a")
    run(a.translate_batch(["Hello there", "General Kenobi"]))
    b = make_client(handler=lambda s, u, m: "[[#0]] B0\n[[#1]] B1", model="model-b")
    out = run(b.translate_batch(["Hello there", "General Kenobi"]))
    assert out == ["B0", "B1"]  # not reusing model-a's cache entries


def test_batch_prompts_contain_glossary_and_brief(make_client):
    cl = make_client()
    cl.book_brief = "- Thể loại: tiểu thuyết"
    gl = [{"src": "Kenobi", "dst": "Ken-O-Bi", "note": "tên"}, {"src": "unused", "dst": "x"}]
    run(cl.translate_batch(["Hello Kenobi", "Bye now"], glossary=gl))
    sys_p = cl.calls[0][0]
    assert "BOOK BRIEF" in sys_p and "tiểu thuyết" in sys_p
    assert "Kenobi" in sys_p and "Ken-O-Bi" in sys_p
    assert "unused" not in sys_p  # filtered out


def test_batch_google_free_path_stubbed(make_client):
    cl = make_client(provider="google_free", api_keys=[])

    async def fake_free(text, target_lang_code=None):
        return text.replace("[[#0]]\n", "[[#0]]\nGF:").replace("[[#1]]\n", "[[#1]]\nGF:")

    cl._translate_google_free_raw = fake_free
    out = run(cl.translate_batch(["alpha", "beta"]))
    assert out == ["GF:alpha", "GF:beta"]
    assert cl.calls == []  # LLM never called


def test_translate_text_falls_back_to_free_when_llm_fails(make_client):
    def boom(*a):
        raise RuntimeError("down")

    cl = make_client(handler=boom, fallback_to_google_free=True)

    async def fake_free(text, target_lang_code=None):
        return "FREE:" + text

    cl._translate_google_free_raw = fake_free
    assert run(cl.translate_text("hello there")) == "FREE:hello there"


def test_translate_text_no_fallback_raises(make_client):
    def boom(*a):
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        run(make_client(handler=boom).translate_text("hello there"))


def test_glossary_post_replacement_for_free_mode():
    f = TranslationLLMClient._apply_glossary_post_replacement
    assert f("I met kenobi today", "x", [{"src": "Kenobi", "dst": "Ken"}]) == "I met Ken today"
    assert f("same", "x", [{"src": "Same", "dst": "same"}]) == "same"
    assert f("", "x", [{"src": "a", "dst": "b"}]) == ""


def test_extract_book_profile_with_fake_llm(make_client):
    import json

    prof = {"genre": "novel", "tone": "dark", "style_rules": ["short sentences"],
            "glossary": [{"src": "Arrakis", "dst": "Arrakis", "note": "planet"}]}
    cl = make_client(handler=lambda *a: "```json\n" + json.dumps(prof) + "\n```")
    res = run(cl.extract_book_profile("some sample text"))
    assert "novel" in res["brief"] and "short sentences" in res["brief"]
    assert res["glossary"][0]["src"] == "Arrakis"


def test_strip_english_gloss_preserves_acronyms_and_strips_plain_english():
    from backend.llm.client import _strip_english_gloss

    # Giữ nguyên các từ viết tắt chữ hoa trong ngoặc như (PBC), (TPOT), (TACSEI), (IEP)
    assert _strip_english_gloss("Practice-Based Coaching", "Huấn luyện dựa trên thực hành (PBC)") == "Huấn luyện dựa trên thực hành (PBC)"
    assert _strip_english_gloss("Teaching Pyramid Observation Tool", "Công cụ quan sát Kim tự tháp Giảng dạy (TPOT)") == "Công cụ quan sát Kim tự tháp Giảng dạy (TPOT)"
    assert _strip_english_gloss("Huấn luyện dựa trên thực hành (PBC)") == "Huấn luyện dựa trên thực hành (PBC)"
    # Loại bỏ phần giải nghĩa tiếng Anh thường ở cuối
    assert _strip_english_gloss("Focused Observation", "Quan sát có trọng tâm (Focused Observation)") == "Quan sát có trọng tâm"
    # Giữ nguyên nếu nằm trong glossary dst
    gl = [{"src": "Action Plan", "dst": "Kế hoạch hành động (Action Plan)"}]
    assert _strip_english_gloss("Action Plan", "Kế hoạch hành động (Action Plan)", glossary=gl) == "Kế hoạch hành động (Action Plan)"


def test_is_llm_refusal_detects_copyright_refusals_and_invalidates_cache(make_client, cache):
    from backend.llm.client import _is_llm_refusal

    src = "Use the Observation Form to record notes during the session."
    refusal_vi = "Không dịch trọn đoạn này vì đây là văn bản có bản quyền. Tóm tắt: Sử dụng biểu mẫu quan sát."
    refusal_en = "I'm sorry, I can't translate this copyrighted passage verbatim."
    normal_vi = "Sử dụng Biểu mẫu Quan sát để ghi chép trong buổi làm việc."

    assert _is_llm_refusal(src, refusal_vi) is True
    assert _is_llm_refusal(src, refusal_en) is True
    assert _is_llm_refusal(src, normal_vi) is False
    # Nếu bản gốc thực sự nói về bản quyền thì câu dịch chứa "văn bản có bản quyền" là hợp lệ
    assert _is_llm_refusal("This copyrighted text may not be reproduced.", "Văn bản có bản quyền này không được sao chép.") is False

    # Kiểm tra khi cache bị nhiễm câu từ chối bản quyền, translate_batch tự coi là cache miss và dịch lại
    cl = make_client(handler=lambda s, u, m: "[[#0]] Bản dịch chuẩn 1\n[[#1]] Bản dịch chuẩn 2")
    salt = cl._context_salt(src, None)
    cache.set(src, refusal_vi, "Tiếng Việt", "openai", "test-model", context_salt=salt)
    out = run(cl.translate_batch([src, "Second normal sentence here."]))
    assert out[0] == "Bản dịch chuẩn 1"
    assert "Không dịch trọn đoạn này" not in out[0]


def test_active_glossary_enforcement_and_auto_repair_with_context_salt(make_client, cache):
    gl = [{"src": "Practice-Based Coaching", "dst": "Huấn luyện dựa trên thực hành (PBC)"}]
    src1 = "Welcome to Practice-Based Coaching for teachers."
    src2 = "This guide explains the steps clearly."

    # 1) Cache lưu sẵn một bản dịch cũ vi phạm glossary (dịch sai/thiếu thuật ngữ bắt buộc)
    cl_seed = make_client()
    cache.set(src1, "Chào mừng đến với phương pháp huấn luyện cho giáo viên.", "Tiếng Việt", "openai", "test-model")

    # 2) Khi chạy translate_batch kèm glossary:
    #    - Bản cache cũ bị phát hiện thiếu "Huấn luyện dựa trên thực hành (PBC)" -> bị bác bỏ
    #    - Giả sử lượt dịch đầu của Agent 3 vẫn quên thuật ngữ -> cơ chế _repair_glossary_violations tự động gọi sửa
    calls_log = []

    def handler(sys_p, user_p, model):
        calls_log.append((sys_p, user_p))
        if "GLOSSARY ENFORCEMENT" in sys_p or "[DRAFT]" in user_p:
            return "[[#0]] Chào mừng đến với Huấn luyện dựa trên thực hành (PBC) dành cho giáo viên."
        return (
            "[[#0]] Chào mừng đến với chương trình huấn luyện dành cho giáo viên.\n"
            "[[#1]] Tài liệu hướng dẫn này giải thích rõ các bước."
        )

    cl = make_client(handler=handler)
    out = run(cl.translate_batch([src1, src2], glossary=gl))
    assert "Huấn luyện dựa trên thực hành (PBC)" in out[0]
    assert out[1] == "Tài liệu hướng dẫn này giải thích rõ các bước."
    assert len(calls_log) == 2  # 1 lượt dịch batch + 1 lượt auto-repair cho đúng câu vi phạm glossary


def test_should_invoke_editor_catches_calques_substantive_prose_and_ignores_test_stubs():
    from backend.llm.client import _should_invoke_editor

    # 1) Bắt các lỗi dịch sát chữ (calques) trong tiếng Việt sư phạm / mầm non
    calque_cases = [
        ("Mr. Roberts greeted the children.", "Ông Roberts chào đón các bé."),
        ("Ms. Delfino prepared the activity.", "Bà Delfino chuẩn bị hoạt động."),
        ("Ask a colleague to cover class.", "Nhờ đồng nghiệp trông lớp thay."),
        ("Celebrations around the coaching process", "Ăn mừng trong quá trình huấn luyện"),
        ("During morning meeting", "Trong giờ họp lớp buổi sáng"),
        ("We are safe, we are kind.", "Chúng ta an toàn, chúng ta tử tế."),
    ]
    for src, calque_draft in calque_cases:
        assert _should_invoke_editor(src, calque_draft) is True, f"Failed to catch calque: {calque_draft}"

    # 2) Kích hoạt Agent 4 trên câu văn xuôi dài (>= 85 ký tự), hội thoại, câu trích dẫn hoặc câu hỏi
    long_src = (
        "This guide helps classroom coaches partner with teachers to implement "
        "effective practices across daily routines and learning centers."
    )
    long_draft = (
        "Cẩm nang này giúp các chuyên gia huấn luyện phối hợp với giáo viên triển khai "
        "các biện pháp thực hành hiệu quả trong sinh hoạt hàng ngày."
    )
    assert _should_invoke_editor(long_src, long_draft) is True
    assert _should_invoke_editor('Teacher: "Let us look at the schedule."', 'Giáo viên: "Hãy nhìn vào lịch biểu."') is True
    assert _should_invoke_editor("Be a 'guide on the side' when coaching.", "Hãy là người đồng hành bên cạnh khi huấn luyện.") is True
    assert _should_invoke_editor("What if the teacher cancels?", "Tôi nên làm gì nếu giáo viên hủy lịch?") is True

    # 3) Nhãn ngắn đã dịch chuẩn và không dính calque -> không cần gọi Agent 4
    assert _should_invoke_editor("Action Plan", "Kế hoạch hành động") is False
    assert _should_invoke_editor("Focused Observation", "Quan sát trọng tâm") is False

    # 4) Bỏ qua các bản dịch giả lập của unit test dạng VI(...) ngay cả khi câu gốc dài hoặc có dấu hỏi
    assert _should_invoke_editor(long_src, "VI(Bản dịch giả lập cho câu dài)") is False
    assert _should_invoke_editor("What if the teacher cancels?", "VI(Bản dịch câu hỏi?)") is False

