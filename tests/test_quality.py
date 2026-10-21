import asyncio
import re

import pytest

from backend.llm.cache import TranslationCache
from backend.llm.client import LLMConfig
from backend.llm.quality import looks_untranslated
from tests.helpers import FakeLLMClient

EN = "The old lighthouse keeper walked slowly along the rocky shore every morning."
VI = "Người gác hải đăng già bước chậm rãi dọc bờ đá mỗi buổi sáng."


@pytest.mark.parametrize("src,dst", [
    (EN, EN),
    (EN, EN.upper()),
    (EN, "  " + EN.replace(",", "") + "!! "),
    ("It was the best of times, it was the worst of times.", "It was the best of times, it was the worst of times"),
])
def test_flags_untranslated(src, dst):
    assert looks_untranslated(src, dst, "vi")


@pytest.mark.parametrize("src", [
    "https://example.com/some/long/path/to/page",
    "contact: john.smith@example.com",
    "2024-05-01 12:30:45 1234567890 4567",
    "Chapter 1",
    "Harry Potter",
    "Hogwarts School Witchcraft Wizardry",
    "def foo(x):\n    return x + 1  # compute the value here",
    "⟦0⟧ ⟦1⟧ ⟦2⟧ ⟦3⟧ ⟦4⟧",
    VI,
    "OK",
])
def test_not_flagged_when_unchanged(src):
    assert not looks_untranslated(src, src, "vi")


def test_translated_not_flagged():
    assert not looks_untranslated(EN, VI, "vi")
    assert not looks_untranslated(EN, "Nguoi gac hai dang gia buoc cham rai doc bo da moi buoi sang", "vi")


def test_still_english_different_case_flagged_not_vi_text():
    assert looks_untranslated(EN, EN.replace("old", "ancient"), "vi")


def test_other_scripts():
    assert looks_untranslated(EN, EN, "ja")
    assert looks_untranslated(EN, EN.lower(), "ru")
    assert not looks_untranslated(EN, "年老いた灯台守は毎朝ゆっくりと岩だらけの海岸を歩いた。", "ja")
    assert not looks_untranslated("古い灯台守は毎朝ゆっくりと岩の海岸を歩いた。", "古い灯台守は毎朝ゆっくりと岩の海岸を歩いた。", "ja")
    assert not looks_untranslated(EN, EN, "en")


def _client(handler, tmp_path, provider="openai"):
    cfg = LLMConfig(provider=provider, api_keys=["sk-x"], model="m", fallback_to_google_free=False)
    return FakeLLMClient(config=cfg, cache=TranslationCache(str(tmp_path / "c.db")), handler=handler)


SRCS = [EN, "She opened the heavy wooden door and stepped quietly into the dark hall.", "Chapter 1"]


def test_batch_retry_fixes_and_caches(tmp_path):
    def handler(sys_p, user_p, mo=None):
        if "[[#" in user_p:  # batch: echoes segment 1 untranslated
            return "[[#0]]\n" + VI + "\n[[#1]]\n" + SRCS[1] + "\n[[#2]]\nChương 1"
        return "Cô mở cánh cửa gỗ nặng nề và lặng lẽ bước vào đại sảnh tối."
    c = _client(handler, tmp_path)
    out = asyncio.run(c.translate_batch(SRCS))
    assert out[1].startswith("Cô mở")
    assert c.quality_stats == {"total": 3, "untranslated_found": 1, "retried_fixed": 1, "still_untranslated": 0}
    assert len(c.calls) == 2
    assert "MUST be in the target language" in c.calls[1][0]
    assert c.cache.get(SRCS[1], c.config.target_lang, "openai", "m").startswith("Cô mở")


def test_failed_retry_not_cached_and_poisoned_cache_repaired(tmp_path):
    def bad(sys_p, user_p, mo=None):
        return "[[#0]]\n" + VI + "\n[[#1]]\n" + SRCS[1] + "\n[[#2]]\nChương 1" if "[[#" in user_p else SRCS[1]
    c = _client(bad, tmp_path)
    asyncio.run(c.translate_batch(SRCS))
    assert c.quality_stats["still_untranslated"] == 1
    assert c.cache.get(SRCS[1], c.config.target_lang, "openai", "m") is None
    # poisoned cache entry gets re-translated and overwritten
    c.cache.set(SRCS[0], SRCS[0], c.config.target_lang, "openai", "m")
    c2 = _client(lambda s, u, m=None: VI, tmp_path)
    assert asyncio.run(c2.translate_text(SRCS[0])) == VI
    assert c2.quality_stats["total"] == 1
    assert c2.cache.get(SRCS[0], c2.config.target_lang, "openai", "m") == VI


def test_retry_cap_and_no_recursion(tmp_path):
    texts = [f"Sentence number {i} is here and it is long enough to count as text." for i in range(40)]
    c = _client(lambda s, u, m=None: re.sub(r"\[\[#(\d+)\]\]", r"[[#\1]]", u) if "[[#" in u else u, tmp_path)
    asyncio.run(c.translate_batch(texts))
    st = c.quality_stats
    assert st["untranslated_found"] == 40 and st["still_untranslated"] == 40
    assert len(c.calls) - 1 <= 10  # 25% cap


def test_google_free_counts_without_retry(tmp_path):
    c = _client(lambda s, u, m=None: u, tmp_path, provider="google_free")

    async def fake_raw(text, target_lang_code=None):
        return text
    c._translate_google_free_raw = fake_raw
    asyncio.run(c.translate_text(EN))
    assert c.quality_stats == {"total": 1, "untranslated_found": 1, "retried_fixed": 0, "still_untranslated": 1}
    assert c.calls == []
