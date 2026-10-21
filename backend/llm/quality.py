"""Heuristic detection of segments that came back NOT translated.

Pure functions, no I/O.  The single entry point is `looks_untranslated(src, dst, lang)`.
Bias: precision first (a false positive costs an LLM call, a false negative leaves
source-language text in the book, but we only flag when we are fairly sure).
"""
import re
import unicodedata
from typing import List, Set

MIN_WORDS = 3
MIN_LETTERS = 20

_MARKER_RE = re.compile(r"\[\[\s*#\s*\d+\s*\]\]|⟦\s*\d+\s*⟧|⟦[^⟧]*⟧|\{\{[^}]*\}\}|\{\d*\}|%\(\w+\)s|%[sd]")
_URL_RE = re.compile(
    r"(?:https?://|ftp://|www\.)\S+|\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"
    r"|\b(?:[\w-]+\.)+(?:com|org|net|edu|gov|io|info|vn|uk|de|fr|ru|cn|jp|co|me|app|dev)\b(?:/\S*)?",
    re.I,
)
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.U)
_CODE_START_RE = re.compile(
    r"^\s*(?:def |class |import |from \S+ import |function |const |let |var |#include|<\?php|"
    r"SELECT |INSERT |UPDATE |<[a-z!/][^>]*>|\$ |>>> )",
)

# Vietnamese letters that never occur in plain English/ASCII text.
_VI_CHARS = set(
    "àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ"
)
_VI_CHARS |= {c.upper() for c in _VI_CHARS}

_STOPWORDS = frozenset(
    "the a an of and or but in on at to for with from by as is are was were be been it its this that "
    "these those he she they we you i his her their our your my not no if then than so do does did "
    "has have had will would can could should may might into over under about after before".split()
)

# target code -> (script name used for the "is the output in the target script" test)
_SCRIPT_RANGES = {
    "cjk": [(0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x3040, 0x30FF), (0xAC00, 0xD7AF), (0x1100, 0x11FF)],
    "cyrillic": [(0x0400, 0x04FF)],
    "arabic": [(0x0600, 0x06FF), (0x0750, 0x077F)],
    "hebrew": [(0x0590, 0x05FF)],
    "thai": [(0x0E00, 0x0E7F)],
    "devanagari": [(0x0900, 0x097F)],
    "greek": [(0x0370, 0x03FF)],
}
_LANG_SCRIPT = {
    "ja": "cjk", "zh": "cjk", "zh-cn": "cjk", "zh-tw": "cjk", "zh-hans": "cjk", "zh-hant": "cjk", "ko": "cjk",
    "ru": "cyrillic", "uk": "cyrillic", "bg": "cyrillic", "sr": "cyrillic", "be": "cyrillic",
    "ar": "arabic", "fa": "arabic", "ur": "arabic", "he": "hebrew", "iw": "hebrew",
    "th": "thai", "hi": "devanagari", "mr": "devanagari", "ne": "devanagari", "el": "greek",
}


def _in_script(ch: str, script: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _SCRIPT_RANGES[script])


def _clean(text: str) -> str:
    """Remove markers/placeholders and URLs/emails/domains."""
    text = _MARKER_RE.sub(" ", text or "")
    return _URL_RE.sub(" ", text)


def normalize(text: str) -> str:
    """casefold + drop punctuation/digits-agnostic noise + collapse whitespace."""
    t = unicodedata.normalize("NFKC", _clean(text)).casefold()
    t = re.sub(r"[\W_]+", " ", t, flags=re.U)
    return t.strip()


def _words(text: str) -> List[str]:
    return _WORD_RE.findall(text)


def _letter_count(words: List[str]) -> int:
    return sum(len(w) for w in words)


def _is_code_like(raw: str) -> bool:
    if _CODE_START_RE.search(raw):
        return True
    s = re.sub(r"\s+", "", raw)
    if not s:
        return False
    symbols = sum(1 for c in s if c in "{}[]()<>=;_\\|/*+#$%^&~`@")
    if symbols / len(s) > 0.12 and re.search(r"[{};=<>]", raw):
        return True
    # snake_case / camelCase / dotted identifiers dominate
    idents = re.findall(r"\b\w+(?:_\w+|\.\w+\()|\b[a-z]+[A-Z]\w*\b", raw)
    return len(idents) >= 3


def _is_proper_noun_only(words: List[str]) -> bool:
    """A short run of capitalised words without function words: a name/title, fine to keep."""
    if len(words) > 5:
        return False
    if any(w.casefold() in _STOPWORDS for w in words):
        return False
    return all(w[0].isupper() for w in words)


def _vi_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if c in _VI_CHARS) / len(letters)


def _script_ratio(text: str, script: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if _in_script(c, script)) / len(letters)


def _latin_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if ord(c) < 0x250) / len(letters)


def _overlap(dst_words: List[str], src_words: List[str]) -> float:
    if not dst_words:
        return 0.0
    src_set: Set[str] = {w.casefold() for w in src_words}
    hit = sum(1 for w in dst_words if w.casefold() in src_set)
    return hit / len(dst_words)


def looks_untranslated(src: str, dst: str, target_lang_code: str = "vi") -> bool:
    """True when `src` should have been translated into `target_lang_code` but `dst` still isn't."""
    if not src or not src.strip():
        return False
    if dst is None:
        return False
    lang = (target_lang_code or "vi").strip().lower()
    base = lang.split("-")[0] if lang not in _LANG_SCRIPT else lang

    cleaned_src = _clean(src)
    src_words = _words(cleaned_src)
    if len(src_words) < MIN_WORDS or _letter_count(src_words) < MIN_LETTERS:
        return False
    if _is_code_like(src) or _is_proper_noun_only(src_words):
        return False

    # Source already in the target language -> nothing to translate.
    script = _LANG_SCRIPT.get(lang) or _LANG_SCRIPT.get(base)
    if base == "vi" and _vi_ratio(cleaned_src) > 0.12:
        return False
    if script and _script_ratio(cleaned_src, script) > 0.5:
        return False
    if base == "en":
        return False  # cannot tell "already English" from "failed" cheaply

    if not dst.strip():
        return False  # empty output is a different failure (handled elsewhere)

    # Rule 1: identical after normalisation.
    if normalize(src) == normalize(dst):
        return True

    cleaned_dst = _clean(dst)
    dst_words = _words(cleaned_dst)
    if not dst_words:
        return False

    # Rule 2: output still in the source language/script.
    if base == "vi":
        if _vi_ratio(cleaned_dst) == 0 and _letter_count(dst_words) >= MIN_LETTERS and len(dst_words) >= MIN_WORDS:
            if _latin_ratio(cleaned_dst) > 0.95 and _overlap(dst_words, src_words) >= 0.6:
                return True
    elif script:
        if _script_ratio(cleaned_dst, script) < 0.05 and _latin_ratio(cleaned_dst) > 0.9:
            if _letter_count(dst_words) >= MIN_LETTERS and _overlap(dst_words, src_words) >= 0.5:
                return True
    else:
        # Other Latin-script targets (fr, de, es...): only trust high overlap + shared stopwords of the source.
        if len(dst_words) >= MIN_WORDS and _overlap(dst_words, src_words) >= 0.9:
            return True
    return False
