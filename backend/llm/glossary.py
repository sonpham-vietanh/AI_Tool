import json
import re
from collections import Counter
from typing import Dict, List, Optional


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

    text_lower = text.lower()
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
        if src_lower in text_lower:
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
