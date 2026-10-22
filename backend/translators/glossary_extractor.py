import logging
import os
import re
from typing import Dict, List, Optional, Tuple

from .converter import (
    _clean_line,
    describe_book_layout,
    extract_book_sample_text,
)
from . import converter as _converter

logger = logging.getLogger(__name__)

# Priority section keywords that define a book's core terminology, structure, and concepts
PRIORITY_SECTION_KEYWORDS: Tuple[str, ...] = (
    "key terms",
    "glossary",
    "definitions",
    "table of contents",
    "introduction",
)

_PRIORITY_HEADING_RE = re.compile(
    r"(?:^|\n)\s*(?:key\s+terms|glossary|definitions|table\s+of\s+contents|contents|introduction)\b",
    re.IGNORECASE,
)

_PRIORITY_ANY_RE = re.compile(
    r"\b(?:key\s+terms|glossary|definitions|table\s+of\s+contents|introduction)\b",
    re.IGNORECASE,
)


def _score_pdf_page_priority(page_idx: int, total_pages: int, page_text: str) -> int:
    """
    Scores a PDF page for terminology/structural importance so Agent 2 always captures
    pages like 'Key Terms', 'Glossary', 'Definitions', 'Table of Contents', and 'Introduction'.
    Returns 0 if not a priority page; higher score = higher priority.
    """
    if not page_text or len(page_text.strip()) < 20:
        return 0

    low = page_text.lower()
    score = 0

    # Check explicit priority keywords
    if "key terms" in low:
        score += 100
    if "glossary" in low:
        score += 95
    if "definitions" in low:
        score += 85
    if "table of contents" in low:
        score += 90
    if "introduction" in low:
        # Boost Introduction when it appears in the first third of the book
        score += 75 if page_idx <= max(5, total_pages // 3) else 35

    if score > 0 and _PRIORITY_HEADING_RE.search(page_text):
        score += 40

    # Cover / title page is always important for document title & organization
    if page_idx == 0:
        score = max(score, 80)

    return score


def _extract_pdf_spread_sample(input_path: str, max_chars: int = 25000) -> str:
    import fitz  # PyMuPDF

    pages_text: List[Tuple[int, str]] = []
    with fitz.open(input_path) as pdf_doc:
        total_pages = len(pdf_doc)
        if total_pages == 0:
            return ""
        for idx, page in enumerate(pdf_doc):
            raw = page.get_text("text").strip()
            if raw:
                pages_text.append((idx, f"[Page {idx + 1}]\n{raw}"))

    if not pages_text:
        return ""

    # If the entire PDF fits within max_chars, return all pages directly
    all_joined = "\n\n".join(txt for _, txt in pages_text)
    if len(all_joined) <= max_chars:
        return all_joined

    page_map: Dict[int, str] = dict(pages_text)
    non_empty_indices = [idx for idx, _ in pages_text]
    n_non_empty = len(non_empty_indices)

    # 1. Compute 7 evenly-spaced positions across the non-empty pages of the book
    num_even = min(7, n_non_empty)
    if num_even <= 1:
        even_indices = [non_empty_indices[0]]
    else:
        even_indices = [
            non_empty_indices[round(i * (n_non_empty - 1) / (num_even - 1))]
            for i in range(num_even)
        ]

    # 2. Detect priority pages ("Key Terms", "Glossary", "Definitions", "Table of Contents", "Introduction")
    scored_priority: List[Tuple[int, int]] = []
    for idx, txt in pages_text:
        sc = _score_pdf_page_priority(idx, total_pages, txt)
        if sc > 0:
            scored_priority.append((sc, idx))
    # Sort highest priority score first, then earlier page index
    scored_priority.sort(key=lambda item: (-item[0], item[1]))
    priority_indices = [idx for _, idx in scored_priority]

    # Combine priority pages (up to 6 best priority pages) + 7 evenly-spaced positions
    selected_order: List[int] = []
    seen_pages = set()

    for idx in priority_indices[:6]:
        if idx not in seen_pages:
            seen_pages.add(idx)
            selected_order.append(idx)

    for idx in even_indices:
        if idx not in seen_pages:
            seen_pages.add(idx)
            selected_order.append(idx)
        else:
            # If this evenly-spaced index was already picked as a priority page,
            # try picking an adjacent unselected page to keep rich coverage
            for neighbor in (idx + 1, idx - 1, idx + 2):
                if neighbor in page_map and neighbor not in seen_pages:
                    seen_pages.add(neighbor)
                    selected_order.append(neighbor)
                    break

    if not selected_order:
        return all_joined[:max_chars]

    # Allocate budget per selected page, guaranteeing priority pages get generous space
    per_page_budget = max(1800, max_chars // len(selected_order))
    snippets_by_page: Dict[int, str] = {}
    used_chars = 0

    for i, page_idx in enumerate(selected_order):
        remaining_slots = len(selected_order) - i
        remaining_budget = max_chars - used_chars
        if remaining_budget <= 200:
            break
        # Give priority pages ("Key Terms", "Glossary", etc.) up to 4000 chars so full term tables aren't cut off
        is_high_priority = page_idx in priority_indices[:4]
        slot_cap = max(per_page_budget, min(4200, remaining_budget // max(1, remaining_slots) + 1200)) if is_high_priority else max(1200, remaining_budget // max(1, remaining_slots))
        page_txt = page_map[page_idx]
        snippet = page_txt[:slot_cap]
        snippets_by_page[page_idx] = snippet
        used_chars += len(snippet) + 9

    # Assemble in natural ascending page order
    ordered_snippets = [snippets_by_page[idx] for idx in sorted(snippets_by_page.keys())]
    result = "\n\n[...]\n\n".join(ordered_snippets)
    return result[:max_chars]


def extract_book_spread_sample(
    input_path: str,
    max_chars: int = 25000,
    total_chars: Optional[int] = None,
) -> str:
    """
    Richer multi-section book sampler for Agent 2 (Book Profiler & Chief Terminologist):
    - Default `max_chars` upgraded to 25,000 characters.
    - For PDFs: automatically detects and prioritizes pages containing 'Key Terms', 'Glossary',
      'Definitions', 'Table of Contents', or 'Introduction' in addition to sampling 7 evenly-spaced
      positions across the book.
    - For EPUB/DOCX/TXT/MD: samples 7 evenly-spaced positions across the book plus any detected
      Key Terms / Glossary / Table of Contents / Introduction sections.
    """
    budget = int(total_chars if total_chars is not None else max_chars)
    if budget <= 0:
        return ""

    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Không tìm thấy file đầu vào: {input_path}")

    ext = os.path.splitext(input_path)[1].lower()
    if ext == ".pdf":
        try:
            pdf_sample = _extract_pdf_spread_sample(input_path, max_chars=budget)
            if pdf_sample:
                return pdf_sample
        except Exception as exc:
            logger.warning("PDF multi-section sampling fallback to text stream (%s)", exc)

    full = extract_book_sample_text(input_path, max(600_000, budget * 4))
    if len(full) <= budget:
        return full

    # Detect priority windows in non-PDF text ("Key Terms", "Glossary", "Definitions", "Table of Contents", "Introduction")
    n = len(full)
    priority_windows: List[Tuple[int, int]] = []
    for m in _PRIORITY_ANY_RE.finditer(full):
        start = max(0, m.start() - 200)
        end = min(n, start + 2800)
        if not any(abs(start - ps) < 1800 for ps, _ in priority_windows):
            priority_windows.append((start, end))
        if len(priority_windows) >= 3:
            break

    # 7 evenly-spaced positions across the book
    num_positions = 7
    priority_Reserved = sum(e - s for s, e in priority_windows)
    even_budget = max(budget // 2, budget - priority_Reserved)
    part = max(1000, even_budget // num_positions)

    fractions = [i / (num_positions - 1) for i in range(num_positions)]
    windows: List[Tuple[int, int]] = list(priority_windows)
    for frac in fractions:
        st = min(int(n * frac), max(0, n - part))
        en = min(n, st + part)
        if not any(not (en <= ws or st >= we) for ws, we in windows):
            windows.append((st, en))

    windows.sort(key=lambda w: w[0])
    pieces = [full[s:e] for s, e in windows if e > s]
    joined = "\n\n[...]\n\n".join(pieces)
    return joined[:budget]


# Keep converter.extract_book_spread_sample and package export synchronized so all imports use the upgraded sampler
import sys as _sys

_converter.extract_book_spread_sample = extract_book_spread_sample
if "backend.translators" in _sys.modules:
    setattr(_sys.modules["backend.translators"], "extract_book_spread_sample", extract_book_spread_sample)

__all__ = [
    "extract_book_spread_sample",
    "extract_book_sample_text",
    "describe_book_layout",
    "PRIORITY_SECTION_KEYWORDS",
]
