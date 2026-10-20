import json
import re
import unicodedata
from collections import Counter
from functools import lru_cache
from typing import Dict, List, Optional, Tuple


COMMON_STOPWORDS = {
    "The", "This", "That", "These", "Those", "There", "Their", "They", "Then",
    "Than", "When", "Where", "While", "Which", "What", "Who", "Whom", "Whose",
    "Why", "How", "Here", "Have", "Has", "Had", "Having", "With", "Without",
    "Within", "From", "Into", "Onto", "Upon", "Over", "Under", "Above", "Below",
    "Between", "Among", "Through", "During", "Before", "After", "Since", "Until",
    "Because", "Although", "Though", "Unless", "Whether", "However", "Therefore",
    "Moreover", "Furthermore", "Nevertheless", "Nonetheless", "Meanwhile",
    "Instead", "Otherwise", "Perhaps", "Maybe", "Always", "Never", "Often",
    "Sometimes", "Usually", "Already", "Still", "Even", "Also", "Just", "Only",
    "Very", "Much", "Many", "More", "Most", "Some", "Such", "Same", "Other",
    "Another", "Each", "Every", "Either", "Neither", "Both", "All", "Any",
    "First", "Second", "Third", "Last", "Next", "Previous", "Chapter", "Section",
    "Part", "Book", "Page", "Figure", "Table", "Note", "Introduction", "Conclusion",
    "Summary", "Appendix", "Index", "Preface", "Foreword", "Copyright", "All Rights",
    "For", "And", "Nor", "But", "Or", "Yet", "So", "In", "On", "At", "To", "By",
    "Of", "As", "Is", "Are", "Was", "Were", "Be", "Been", "Being", "Can", "Could",
    "Will", "Would", "Shall", "Should", "May", "Might", "Must", "Do", "Does", "Did",
    "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
    "His", "Her", "Hers", "Him", "She", "He", "It", "Its", "We", "Us", "Our", "You",
    "Your", "My", "Me", "Mine", "Yes", "No", "Not", "Now", "Today", "Yesterday",
}


# ---------------------------------------------------------------------------
# Word-boundary aware term matching (Unicode, case-insensitive, possessive/plural)
# ---------------------------------------------------------------------------

# Scripts written without spaces: word boundaries are meaningless there -> plain substring match.
_NO_BOUNDARY_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯฀-๿]")
# Possessive ('s, s') and simple plural (s, es) suffixes tolerated after a source term.
_TERM_SUFFIX = r"(?:['’]s|['’]|es|s)?"


def _term_body(src: str) -> Tuple[str, bool]:
    """Returns (regex body without flags, case_sensitive)."""
    parts = src.split()
    esc = r"\s+".join(re.escape(p) for p in parts)
    if _NO_BOUNDARY_RE.search(src):
        return esc, False
    # Short all-caps acronyms (AI, US, GDP) must not match ordinary words ("ai", "us").
    case_sensitive = len(src) <= 5 and src.isalpha() and src.isupper()
    tail = _TERM_SUFFIX if src[-1:].isalnum() else ""
    return r"(?<!\w)" + esc + tail + r"(?!\w)", case_sensitive


@lru_cache(maxsize=4096)
def compile_term_pattern(src: str) -> "re.Pattern[str]":
    body, case_sensitive = _term_body(src.strip())
    return re.compile(body, 0 if case_sensitive else re.IGNORECASE)


def term_in_text(src: str, text: str) -> bool:
    """True if glossary term `src` occurs in `text` as a whole word/phrase."""
    src = (src or "").strip()
    if not src or not text:
        return False
    return compile_term_pattern(src).search(text) is not None


def apply_glossary_replacements(text: str, items: Optional[List[Dict[str, str]]]) -> str:
    """
    Replaces whole-word occurrences of each item's `src` by its `dst` (only where src != dst),
    longest source first, in a single pass (so a replaced `dst` is never re-replaced).
    A trailing possessive/plural suffix attached to the source is absorbed by the replacement.
    """
    if not text or not items:
        return text
    todo = []
    seen = set()
    for item in items:
        src = (item.get("src") or "").strip()
        dst = (item.get("dst") or "").strip()
        if not src or not dst or src.lower() == dst.lower() or src.lower() in seen:
            continue
        seen.add(src.lower())
        todo.append((src, dst))
    if not todo:
        return text
    todo.sort(key=lambda p: len(p[0]), reverse=True)
    alts = []
    for i, (src, _dst) in enumerate(todo):
        body, case_sensitive = _term_body(src)
        alts.append(f"(?P<g{i}>{body})" if case_sensitive else f"(?P<g{i}>(?i:{body}))")
    combined = re.compile("|".join(alts))

    def _sub(m: "re.Match[str]") -> str:
        return todo[int(m.lastgroup[1:])][1]

    return combined.sub(_sub, text)


def _norm_cmp(s: str) -> str:
    return unicodedata.normalize("NFC", s).casefold()


def find_missing_dst(
    src_text: str, out_text: str, matched_glossary: Optional[List[Dict[str, str]]]
) -> List[Dict[str, str]]:
    """Glossary entries whose src is in `src_text` but whose dst is absent from `out_text`."""
    if not matched_glossary or not src_text or not out_text:
        return []
    out_norm = _norm_cmp(out_text)
    missing = []
    for item in matched_glossary:
        src = (item.get("src") or "").strip()
        dst = (item.get("dst") or "").strip()
        if not src or not dst:
            continue
        if term_in_text(src, src_text) and _norm_cmp(dst) not in out_norm:
            missing.append(item)
    return missing


# ---------------------------------------------------------------------------
# Term memory helpers (proper-noun candidates + [[TERMS]] section parsing)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\w[\w'’\-]*")
_CONNECTORS = {"of", "the", "de", "del", "la", "le", "van", "von", "der", "den", "da", "di", "du", "al", "bin", "ibn", "and"}
_ROMAN_OR_NOISE = {"II", "III", "IV", "VI", "VII", "VIII", "IX", "XI", "XII", "AM", "PM", "US", "UK", "OK", "TV", "ID"}
_STOP_LOWER = {w.lower() for w in COMMON_STOPWORDS}
_TERMS_MARKER_RE = re.compile(r"\[\[\s*TERMS\s*\]\]", re.IGNORECASE)


def _strip_possessive(tok: str) -> str:
    for suf in ("'s", "’s", "'", "’"):
        if tok.endswith(suf) and len(tok) > len(suf) + 1:
            return tok[: -len(suf)]
    return tok


def extract_proper_noun_candidates(
    text: str,
    known_srcs: Optional[List[str]] = None,
    max_candidates: int = 24,
) -> List[str]:
    """
    Cheap heuristic: capitalized proper-noun-like phrases/acronyms in `text` that are not
    already covered by `known_srcs` (glossary / learned terms). Sentence-initial single words
    are ignored (too noisy); multi-word capitalized runs always count.
    """
    if not text:
        return []
    known_lower = {k.strip().lower() for k in (known_srcs or []) if k and k.strip()}
    known_words = set()
    for k in known_lower:
        known_words.update(_TOKEN_RE.findall(k))

    toks = list(_TOKEN_RE.finditer(text))
    counts: Counter = Counter()
    order: Dict[str, int] = {}

    def initial(pos: int) -> bool:
        prefix = text[:pos].rstrip(" \t")
        return (not prefix) or prefix[-1] in '.!?…:;\n"“”‘([—'

    def is_cap(tok: str) -> bool:
        base = _strip_possessive(tok)
        return (
            bool(base)
            and base[0].isalpha()
            and base[0].isupper()
            and base not in COMMON_STOPWORDS
            and not re.fullmatch(r"[IVXLCDM]+", base)  # số La Mã (đầu chương)
        )

    i = 0
    n = len(toks)
    while i < n:
        m = toks[i]
        if not is_cap(m.group()):
            i += 1
            continue
        j = i
        words = [_strip_possessive(m.group())]
        # extend run over capitalized tokens separated by a single space (or connectors)
        while j + 1 < n:
            nxt = toks[j + 1]
            gap = text[toks[j].end():nxt.start()]
            if gap != " ":
                break
            if is_cap(nxt.group()):
                words.append(_strip_possessive(nxt.group()))
                j += 1
                continue
            if (
                nxt.group().lower() in _CONNECTORS
                and j + 2 < n
                and text[nxt.end():toks[j + 2].start()] == " "
                and is_cap(toks[j + 2].group())
            ):
                words.append(nxt.group())
                words.append(_strip_possessive(toks[j + 2].group()))
                j += 2
                continue
            break
        phrase = " ".join(words)
        single = len(words) == 1
        keep = True
        if single:
            w = words[0]
            if len(w) < 3 and not w.isupper():
                keep = False
            elif w.isupper() and (len(w) < 2 or len(w) > 8 or w in _ROMAN_OR_NOISE or not w.isalpha()):
                keep = False
            elif initial(m.start()) and not w.isupper():
                keep = False
            elif w.lower() in _STOP_LOWER:
                keep = False
        if keep:
            low = phrase.lower()
            if low in known_lower or (single and low in known_words):
                keep = False
            elif not single and all(w.lower() in known_words for w in words if w.lower() not in _CONNECTORS):
                keep = False
        if keep:
            counts[phrase] += 1
            order.setdefault(phrase, len(order))
        i = j + 1

    ranked = sorted(counts, key=lambda p: (-counts[p], order[p]))
    return ranked[:max_candidates]


def split_terms_section(raw_output: str) -> Tuple[str, List[Tuple[str, str]]]:
    """
    Splits a model batch output into (body, [(src, dst), ...]) where the optional trailing
    `[[TERMS]]` section lists `src => dst` lines. Body is returned unchanged when no section.
    """
    if not raw_output:
        return raw_output, []
    m = _TERMS_MARKER_RE.search(raw_output)
    if not m:
        return raw_output, []
    body = raw_output[: m.start()].rstrip()
    terms: List[Tuple[str, str]] = []
    for line in raw_output[m.end():].splitlines():
        line = line.strip().strip("`").lstrip("-*• \t").strip()
        if not line:
            continue
        parts = re.split(r"\s*(?:=>|->|→|⇒)\s*", line, maxsplit=1)
        if len(parts) != 2:
            continue
        src = parts[0].strip(" \t\"'`*“”")
        dst = parts[1].strip(" \t\"'`*“”")
        if not src or not dst or len(src) > 80 or len(dst) > 120 or "[[" in src or "[[" in dst:
            continue
        terms.append((src, dst))
    return body, terms


def normalize_glossary(glossary: Optional[List[Dict[str, str]]]) -> List[Dict[str, str]]:
    """
    Cleans and deduplicates glossary items into standard {"src": ..., "dst": ..., "note": ...} dicts.
    """
    if not glossary:
        return []
    cleaned: List[Dict[str, str]] = []
    seen = set()
    for item in glossary:
        if not isinstance(item, dict):
            continue
        src = str(item.get("src") or item.get("source") or item.get("term") or "").strip()
        dst = str(item.get("dst") or item.get("target") or item.get("translation") or "").strip()
        note = str(item.get("note") or item.get("category") or item.get("context") or "").strip()
        if not src or not dst:
            continue
        key = src.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append({"src": src, "dst": dst, "note": note})
    return cleaned


def filter_glossary_for_text(
    text: str, glossary: Optional[List[Dict[str, str]]]
) -> List[Dict[str, str]]:
    """
    LinguaGacha / KeywordGacha style dynamic glossary filter:
    Returns only the glossary entries whose `src` term appears (case-insensitive)
    in the given `text` (or batch of texts), sorted longest-first for priority.
    """
    if not text or not glossary:
        return []

    matched: List[Dict[str, str]] = []
    seen = set()

    for item in glossary:
        if not isinstance(item, dict):
            continue
        src = (item.get("src") or "").strip()
        dst = (item.get("dst") or "").strip()
        if not src or not dst:
            continue
        src_lower = src.lower()
        if src_lower in seen:
            continue
        if term_in_text(src, text):
            seen.add(src_lower)
            matched.append(
                {
                    "src": src,
                    "dst": dst,
                    "note": (item.get("note") or "").strip(),
                }
            )

    # Sort by source term length descending so multi-word phrases appear prominently
    matched.sort(key=lambda x: len(x["src"]), reverse=True)
    return matched


def parse_llm_glossary_json(raw_response: str) -> List[Dict[str, str]]:
    """
    Robustly extracts a JSON list of glossary items from an LLM output string.
    """
    if not raw_response:
        return []

    cleaned = raw_response.strip()
    # Strip markdown code fences if present
    fence_match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1)
    else:
        bracket_match = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if bracket_match:
            cleaned = bracket_match.group(0)

    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            return normalize_glossary(data)
    except Exception:
        pass

    return []


def extract_candidate_terms_heuristic(
    sample_text: str, max_terms: int = 35
) -> List[Dict[str, str]]:
    """
    Fast heuristic extractor (used in `google_free` mode or when LLM extraction fails).
    Identifies recurring capitalized multi-word entities, technical acronyms, and key terms.
    """
    if not sample_text or not sample_text.strip():
        return []

    candidates: Counter = Counter()

    # 1. Multi-word Capitalized Phrases (e.g., "Federal Reserve", "Quantum Mechanics", "Artificial Intelligence")
    multi_cap_pattern = re.compile(
        r"\b([A-Z][a-zA-Z0-9'-]{1,25}(?:\s+(?:of|the|and|for|in|on|to|de|van|von)\s+[A-Z][a-zA-Z0-9'-]{1,25}|\s+[A-Z][a-zA-Z0-9'-]{1,25}){1,3})\b"
    )
    for match in multi_cap_pattern.finditer(sample_text):
        phrase = match.group(1).strip()
        words = phrase.split()
        if any(w in COMMON_STOPWORDS for w in (words[0], words[-1])):
            continue
        if len(phrase) >= 4:
            candidates[phrase] += 2  # Boost multi-word proper nouns / technical phrases

    # 2. Acronyms / Technical Abbreviations (e.g., "GDP", "DNA", "API", "CBT", "NLP")
    acronym_pattern = re.compile(r"\b([A-Z]{2,8})\b")
    for match in acronym_pattern.finditer(sample_text):
        acr = match.group(1)
        if acr not in {"II", "III", "IV", "VI", "VII", "VIII", "IX", "XI", "XII", "AM", "PM", "US", "UK", "OK"} and acr not in COMMON_STOPWORDS:
            candidates[acr] += 1

    # 3. Single Capitalized Proper Nouns / Technical Terms appearing mid-sentence or repeatedly
    single_cap_pattern = re.compile(r"(?<=[a-z,;:]\s)([A-Z][a-z]{3,22})\b")
    for match in single_cap_pattern.finditer(sample_text):
        word = match.group(1)
        if word not in COMMON_STOPWORDS:
            candidates[word] += 1

    # Filter and rank
    results: List[Dict[str, str]] = []
    seen_lower = set()

    for term, score in candidates.most_common(max_terms * 3):
        t_low = term.lower()
        if t_low in seen_lower or term in COMMON_STOPWORDS:
            continue
        # Require at least 2 occurrences (or score >= 2 for multi-word)
        if score < 2 and len(candidates) > 10:
            continue
        seen_lower.add(t_low)

        if term.isupper() and len(term) <= 8:
            note = "Từ viết tắt / Thuật ngữ"
        elif " " in term:
            note = "Cụm thuật ngữ / Tên riêng"
        else:
            note = "Tên riêng / Khái niệm"

        results.append(
            {
                "src": term,
                "dst": term,  # Will be populated by Google Translate or kept if proper noun
                "note": note,
            }
        )
        if len(results) >= max_terms:
            break

    return results
