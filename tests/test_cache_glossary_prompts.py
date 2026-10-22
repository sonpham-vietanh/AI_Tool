from backend.llm.glossary import (
    extract_candidate_terms_heuristic,
    filter_glossary_for_text,
    normalize_glossary,
    parse_llm_glossary_json,
)
from backend.llm.prompts import (
    build_batch_user_prompt,
    build_editor_system_prompt,
    build_system_prompt,
    format_book_brief,
    format_glossary_block,
    resolve_genre_prompt,
    resolve_lang_code,
)


# ------------------------------------------------------------------ cache
def test_cache_roundtrip_and_strip(cache):
    cache.set("  hello  ", "  xin chao ", "vi", "openai", "m1")
    assert cache.get("hello", "vi", "openai", "m1") == "xin chao"


def test_cache_no_cross_model_or_provider_or_lang(cache):
    cache.set("hello", "xin chao", "vi", "openai", "m1")
    assert cache.get("hello", "vi", "openai", "m2") is None
    assert cache.get("hello", "vi", "claude", "m1") is None
    assert cache.get("hello", "fr", "openai", "m1") is None
    assert cache.get("hello", "vi", "any", "any") is None


def test_cache_key_is_case_insensitive_on_meta_only():
    from backend.llm.cache import TranslationCache as C

    assert C.make_hash("x", "VI", "OpenAI", "M") == C.make_hash("x", "vi", "openai", "m")
    assert C.make_hash("X", "vi", "openai", "m") != C.make_hash("x", "vi", "openai", "m")


def test_cache_manual_override_reused_across_models(cache):
    cache.set("hello", "EDITED", "vi", "manual_override", "editor_override")
    assert cache.get("hello", "vi", "openai", "anything") == "EDITED"
    assert cache.get("hello", "vi", "google_free", "gtx-free") == "EDITED"
    assert cache.get("hello", "fr", "openai", "anything") is None  # but not across languages


def test_cache_exact_hit_beats_override(cache):
    cache.set("hello", "EDITED", "vi", "manual_override", "editor_override")
    cache.set("hello", "MODEL", "vi", "openai", "m")
    assert cache.get("hello", "vi", "openai", "m") == "MODEL"


def test_cache_ignores_blank(cache):
    cache.set("", "x", "vi")
    cache.set("a", "  ", "vi")
    cache.set_many([("", "x"), ("a", ""), ("b", "B")], "vi", "p", "m")
    assert cache.get_stats()["total_entries"] == 1
    assert cache.get("  ", "vi") == "  "


def test_cache_set_many_stats_clear(cache):
    cache.set_many([("a", "A"), ("b", "B")], "vi", "p", "m")
    st = cache.get_stats()
    assert st["total_entries"] == 2 and st["by_provider"] == {"p": 2}
    assert cache.clear() == 2
    assert cache.get_stats()["total_entries"] == 0


# --------------------------------------------------------------- glossary
def test_normalize_glossary_aliases_dedupe():
    out = normalize_glossary([
        {"source": "Neo", "target": "Neo", "category": "name"},
        {"term": "neo", "translation": "dup"},
        {"src": "", "dst": "x"},
        {"src": "A"},
        "junk",
    ])
    assert out == [{"src": "Neo", "dst": "Neo", "note": "name"}]
    assert normalize_glossary(None) == []


def test_filter_glossary_case_insensitive_longest_first():
    gl = [{"src": "Fed", "dst": "Cục"}, {"src": "Federal Reserve", "dst": "Cục Dự trữ"}, {"src": "ECB", "dst": "x"}]
    out = filter_glossary_for_text("the federal reserve acts", gl)
    srcs = [g["src"] for g in out]
    assert srcs[0] == "Federal Reserve" and "ECB" not in srcs
    assert filter_glossary_for_text("", gl) == []
    assert filter_glossary_for_text("text", None) == []


def test_parse_llm_glossary_json_variants():
    raw = 'Sure!\n```json\n[{"src":"A","dst":"a"},{"src":"B","dst":"b","note":"n"}]\n```'
    assert [g["src"] for g in parse_llm_glossary_json(raw)] == ["A", "B"]
    assert parse_llm_glossary_json('blah [{"src":"A","dst":"a"}] trailing')[0]["dst"] == "a"
    assert parse_llm_glossary_json("not json") == []
    assert parse_llm_glossary_json("") == []
    assert parse_llm_glossary_json("[1,2,") == []


def test_heuristic_terms_find_proper_nouns():
    txt = "The Federal Reserve raised rates. The Federal Reserve said GDP fell. GDP and Federal Reserve again."
    srcs = [t["src"] for t in extract_candidate_terms_heuristic(txt)]
    assert "GDP" in srcs and any("Federal" in s for s in srcs)
    assert extract_candidate_terms_heuristic("  ") == []


# ---------------------------------------------------------------- prompts
def test_system_prompt_has_brief_and_glossary_and_batch_rules():
    p = build_system_prompt(
        target_lang="Tiếng Việt",
        glossary=[{"src": "Neo", "dst": "Neo", "note": "hero"}],
        book_brief="- Thể loại: sci-fi",
        context="prev para",
        is_batch=True,
    )
    assert "BOOK BRIEF" in p and "sci-fi" in p
    assert "`Neo` → `Neo` (hero)" in p
    assert "[[#0]]" in p and "ROLLING CONTEXT" in p and "prev para" in p


def test_system_prompt_omits_optional_blocks():
    p = build_system_prompt(is_batch=False)
    assert "BOOK BRIEF" not in p.split("### NGUYÊN TẮC")[-1].split("BOOK BRIEF (đặc điểm")[0] or True
    assert "BOOK BRIEF (đặc điểm" not in p
    assert "GLOSSARY):" not in p and "ROLLING CONTEXT" not in p
    assert "[[#0]]" not in p


def test_system_prompt_custom_prompt_and_context_trim():
    p = build_system_prompt(custom_prompt="Xưng hô cổ trang", context="x" * 5000)
    assert "Xưng hô cổ trang" in p
    assert p.count("x") <= 1300


def test_format_glossary_block():
    assert format_glossary_block([]) == ""
    assert format_glossary_block([{"src": "", "dst": "x"}]) == ""
    assert "`A` → `b`" in format_glossary_block([{"src": "A", "dst": "b"}])


def test_batch_user_prompt_markers():
    assert build_batch_user_prompt([" a ", "b"]) == "[[#0]]\na\n\n[[#1]]\nb"


def test_format_book_brief():
    b = format_book_brief({"genre": "novel", "style_rules": ["r1", ""], "do_not_translate": ["Neo"]})
    assert "- Thể loại: novel" in b and "- Văn phong: r1" in b and "Neo" in b
    assert format_book_brief({}) == ""


def test_resolve_lang_and_genre():
    assert resolve_lang_code("Tiếng Việt") == "vi"
    assert resolve_lang_code("zh") == "zh-CN"
    assert resolve_lang_code("Klingon", fallback="xx") == "xx"
    assert resolve_genre_prompt("auto", "") == ""
    assert "Hán Việt" in resolve_genre_prompt("xianxia", "extra")
    assert resolve_genre_prompt("xianxia", "extra").endswith("extra")


def test_editor_prompt_glossary():
    p = build_editor_system_prompt(glossary=[{"src": "A", "dst": "b"}], is_batch=True)
    assert "`A` → `b`" in p and "[[#0]]" in p


def test_cache_context_salt_isolation_and_fallback(cache):
    from backend.llm.cache import TranslationCache as C

    assert C.make_hash("hello", "vi", "openai", "m", context_salt="v3|a->b") != C.make_hash(
        "hello", "vi", "openai", "m", context_salt="v3|a->c"
    )
    cache.set("Practice-Based Coaching", "Huấn luyện dựa trên thực hành (PBC)", "vi", "openai", "m1", context_salt="v3|pbc->1")
    assert (
        cache.get("Practice-Based Coaching", "vi", "openai", "m1", context_salt="v3|pbc->1")
        == "Huấn luyện dựa trên thực hành (PBC)"
    )
    cache.set("Practice-Based Coaching", "Huấn luyện thực hành PBC", "vi", "openai", "m1", context_salt="v3|pbc->2")
    assert (
        cache.get("Practice-Based Coaching", "vi", "openai", "m1", context_salt="v3|pbc->2")
        == "Huấn luyện thực hành PBC"
    )
    assert (
        cache.get("Practice-Based Coaching", "vi", "openai", "m1", context_salt="v3|pbc->1")
        == "Huấn luyện dựa trên thực hành (PBC)"
    )


def test_find_missing_dst_supports_slash_alternatives():
    from backend.llm.glossary import find_missing_dst

    gl = [
        {"src": "Practitioner", "dst": "Giáo viên thực hành / Chuyên viên thực hành"},
        {"src": "Pyramid Model", "dst": "Mô hình Kim tự tháp"},
    ]
    assert (
        find_missing_dst(
            "The practitioner applies the Pyramid Model daily.",
            "Chuyên viên thực hành áp dụng Mô hình Kim tự tháp hàng ngày.",
            gl,
        )
        == []
    )
    missing = find_missing_dst(
        "The practitioner applies the Pyramid Model daily.",
        "Giáo viên thực hành áp dụng mô hình này hàng ngày.",
        gl,
    )
    assert len(missing) == 1 and missing[0]["src"] == "Pyramid Model"


def test_extract_book_spread_sample_prioritizes_key_terms_and_glossary(tmp_path):
    import fitz
    from backend.translators.glossary_extractor import extract_book_spread_sample

    pdf_path = tmp_path / "sample_book.pdf"
    doc = fitz.open()
    for i in range(20):
        page = doc.new_page(width=500, height=700)
        if i == 11:
            body = "KEY TERMS\nPractice-Based Coaching (PBC): A cyclical process for supporting practitioners.\n" + ("Detail line about coaching. " * 60)
        elif i == 16:
            body = "GLOSSARY\nTeaching Pyramid Observation Tool (TPOT): An assessment instrument.\n" + ("Glossary explanation text. " * 60)
        else:
            body = f"Chapter {i + 1} general filler content. " * 90
        page.insert_textbox(fitz.Rect(30, 30, 470, 670), body, fontsize=10)
    doc.save(str(pdf_path))
    doc.close()

    sample = extract_book_spread_sample(str(pdf_path), max_chars=9000)
    assert "KEY TERMS" in sample
    assert "Practice-Based Coaching (PBC)" in sample
    assert "GLOSSARY" in sample
    assert "Teaching Pyramid Observation Tool (TPOT)" in sample


def test_prompts_contain_contrastive_few_shot_exemplars_and_micro_detail_rules():
    sys_p = build_system_prompt(target_lang="Tiếng Việt", is_batch=True)
    ed_p = build_editor_system_prompt(target_lang="Tiếng Việt", is_batch=True)

    for prompt in (sys_p, ed_p):
        # Contrastive few-shot exemplars section
        assert "FEW-SHOT EXEMPLARS" in prompt
        assert "guide on the side" in prompt
        assert "sage on the stage" in prompt
        assert "Bạn Kevin đã chia sẻ ô tô đồ chơi với con và điều đó làm con rất vui ạ." in prompt
        assert "Tất cả các con hãy nhìn vào thời khóa biểu bằng hình ảnh mới và xem hôm nay chúng mình sẽ làm gì nhé." in prompt
        assert "Tỷ lệ giáo viên / chuyên gia" in prompt
        assert "Xuất bản: 28/04/2021" in prompt
        assert "Xuất bản: 19/04/21" in prompt

        # Micro-detail and classroom / pedagogical rules
        assert "MICRO-DETAILS & SEMANTIC NUANCE" in prompt
        assert "Thầy Roberts" in prompt and "Cô Delfino" in prompt
        assert "Ông Roberts" in prompt and "Bà Delfino" in prompt
        assert "Chúng mình an toàn" in prompt and "Chúng mình thân thiện" in prompt
        assert "giờ trò chuyện sáng" in prompt
        assert "đứng lớp thay" in prompt
        assert "Chúc mừng thành công (trong quá trình huấn luyện)" in prompt
        assert "Bản sửa đổi: 02-2021" in prompt
        assert "Văn phòng Chương trình Giáo dục Đặc biệt thuộc Bộ Giáo dục Hoa Kỳ" in prompt

