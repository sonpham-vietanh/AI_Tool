"""Dịch PDF "tại chỗ" (in-place): thay chữ gốc bằng bản dịch ngay tại đúng tọa độ, giữ nguyên nền màu, hình ảnh,
đường kẻ, dấu đầu dòng, màu chữ, độ đậm/nghiêng và cỡ chữ gốc (tự co chữ khi bản dịch dài hơn).

Quy trình: extract_units -> (dịch ở pdf_translator) -> render_units.
"""
from __future__ import annotations

import logging
import os
import re
import statistics
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------- phông chữ
_WIN = "C:/Windows/Fonts"
_FONT_CANDIDATES: Dict[Tuple[str, bool, bool], List[str]] = {}


def _variants(dirs: List[str], names: Dict[Tuple[bool, bool], str]) -> Dict[Tuple[bool, bool], List[str]]:
    return {k: [os.path.join(d, n) for d in dirs] for k, n in names.items()}


_SANS_DIRS = [_WIN, "/usr/share/fonts/truetype/liberation2", "/usr/share/fonts/truetype/liberation", "/Library/Fonts"]
_SERIF_DIRS = _SANS_DIRS
_FAMILIES = {
    "sans": [
        {(False, False): "arial.ttf", (True, False): "arialbd.ttf", (False, True): "ariali.ttf", (True, True): "arialbi.ttf"},
        {(False, False): "LiberationSans-Regular.ttf", (True, False): "LiberationSans-Bold.ttf",
         (False, True): "LiberationSans-Italic.ttf", (True, True): "LiberationSans-BoldItalic.ttf"},
        {(False, False): "DejaVuSans.ttf", (True, False): "DejaVuSans-Bold.ttf",
         (False, True): "DejaVuSans-Oblique.ttf", (True, True): "DejaVuSans-BoldOblique.ttf"},
    ],
    "serif": [
        {(False, False): "times.ttf", (True, False): "timesbd.ttf", (False, True): "timesi.ttf", (True, True): "timesbi.ttf"},
        {(False, False): "LiberationSerif-Regular.ttf", (True, False): "LiberationSerif-Bold.ttf",
         (False, True): "LiberationSerif-Italic.ttf", (True, True): "LiberationSerif-BoldItalic.ttf"},
        {(False, False): "DejaVuSerif.ttf", (True, False): "DejaVuSerif-Bold.ttf",
         (False, True): "DejaVuSerif-Italic.ttf", (True, True): "DejaVuSerif-BoldItalic.ttf"},
    ],
}
_EXTRA_DIRS = ["/usr/share/fonts/truetype/dejavu"]
_font_cache: Dict[str, fitz.Font] = {}


def _search_dirs() -> List[str]:
    env = os.environ.get("BTH_FONT_DIR")
    return ([env] if env else []) + _SANS_DIRS + _EXTRA_DIRS


def resolve_font_path(serif: bool, bold: bool, italic: bool, fallback: Optional[str] = None) -> Optional[str]:
    """Tìm file phông Unicode (hỗ trợ tiếng Việt) theo họ/đậm/nghiêng; lần lượt Arial/Times, Liberation, DejaVu."""
    fam = "serif" if serif else "sans"
    for names in _FAMILIES[fam]:
        for want in ((bold, italic), (bold, False), (False, italic), (False, False)):
            fname = names[want]
            for d in _search_dirs():
                p = os.path.join(d, fname)
                if os.path.isfile(p):
                    return p
    return fallback if fallback and os.path.isfile(fallback) else None


def _get_font(path: str) -> fitz.Font:
    if path not in _font_cache:
        _font_cache[path] = fitz.Font(fontfile=path)
    return _font_cache[path]


# ----------------------------------------------------------------------------- mô hình dữ liệu
@dataclass
class Unit:
    page: int
    rect: fitz.Rect                      # hợp bbox các span chữ của đoạn (tọa độ trang)
    line_rects: List[fitz.Rect]          # bbox từng dòng (để xóa chữ gốc)
    text: str                            # văn bản gốc đã làm sạch
    size: float
    color: Tuple[float, float, float]
    bold: bool
    italic: bool
    serif: bool
    align: str = "left"                  # left | center | right
    pitch: float = 0.0                   # khoảng cách dòng (pt), 0 nếu 1 dòng
    vertical: bool = False               # chữ xoay dọc (đọc từ dưới lên)
    container: Optional[fitz.Rect] = None  # ô/khung vẽ bao quanh (bảng, hộp màu)
    dst: str = ""
    skip: bool = False
    starts_bullet: bool = False
    ended_with_tab: bool = False
    protected: Dict[str, str] = field(default_factory=dict)
    has_inline_styles: bool = False
    markup_text: str = ""
    rich_text: str = ""
    max_lines: int = 1
    _dom_chars: int = 0
    _first_style: Optional[Tuple[float, Tuple[float, float, float], bool, bool, bool]] = None
    _last_style: Optional[Tuple[float, Tuple[float, float, float], bool, bool, bool]] = None
    _styled_lines: List[List[Tuple[str, float, bool, bool]]] = field(default_factory=list)


_SYMBOLIC_FONT_RE = re.compile(r"wingding|symbol|dingbat|webding|zapf", re.I)
_BULLET_CHARS = set("•·▪◦■□☐☑○●◆◇▶►‣⁃–—-*✓✔")
_LIST_MARKER_RE = re.compile(r"^\s*(?:\d{1,3}[.)]|[A-Za-z][.)]|[IVX]{1,4}\.)\s*$")
_SERIF_NAME_RE = re.compile(r"garamond|times|georgia|palatino|minion|cambria|book|serif|roman|bodoni|schoolbook|caslon|baskerville", re.I)
_SANS_NAME_RE = re.compile(r"gothic|arial|helvet|calibri|verdana|sans|tahoma|segoe|futura|myriad|frutiger|univers|din", re.I)


def _is_symbol_span(span: dict) -> bool:
    t = (span.get("text") or "").strip()
    if not t:
        return False
    if any(c.isalnum() for c in t):
        return False
    if _SYMBOLIC_FONT_RE.search(span.get("font", "")):
        return True
    if any(ord(c) >= 0xE000 or c == "\ufffd" for c in t):
        return True
    return all(c in _BULLET_CHARS for c in t)


def _is_list_marker_span(span: dict) -> bool:
    t = (span.get("text") or "").strip()
    return bool(t and _LIST_MARKER_RE.match(t))


def _is_serif(font_name: str, flags: int) -> bool:
    if _SANS_NAME_RE.search(font_name):
        return False
    if _SERIF_NAME_RE.search(font_name):
        return True
    return bool(flags & 4) and not font_name.lower().startswith(("arial", "calibri"))


def _rgb(c: int) -> Tuple[float, float, float]:
    return (((c >> 16) & 255) / 255.0, ((c >> 8) & 255) / 255.0, (c & 255) / 255.0)


_SUP_DIGITS = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
_PRESERVE_NFKC_CHARS = set("™®©⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ")


def _nfkc_preserve_superscripts(s: str) -> str:
    """Chuẩn hóa NFKC nhưng giữ nguyên các ký tự superscript Unicode (¹²³...) và ™."""
    if not s:
        return ""
    return "".join(c if c in _PRESERVE_NFKC_CHARS else unicodedata.normalize("NFKC", c) for c in s)


def clean_text(raw: str) -> str:
    text = _nfkc_preserve_superscripts(raw or "").replace("\u00ad", "")
    text = re.sub(r"([A-Za-z])-\s*\n\s*([a-z])", r"\1\2", text)
    text = re.sub(r"\s*\n+\s*", " ", text)
    return re.sub(r"[ \t\u00a0]{2,}", " ", text).strip()


def should_translate(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 2:
        return False
    if re.fullmatch(r"[\d\W_]+", t):
        return False
    if len(re.findall(r"[^\W\d_]", t)) < 2:
        return False
    return True


# URL / email / tên miền không được dịch
_URL_RE = re.compile(
    r"(?:https?://[^\s)\]]+|www\.[^\s)\]]+|[\w.+-]+@[\w-]+\.[\w.-]+|\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:org|com|net|edu|gov|io|vn|info)\b(?:/[^\s)\]]*)?)"
)


def protect_urls(text: str) -> Tuple[str, Dict[str, str]]:
    mapping: Dict[str, str] = {}

    def repl(m: re.Match) -> str:
        key = f"\u27e6{len(mapping) + 1}/\u27e7"
        mapping[key] = m.group(0)
        return key

    return _URL_RE.sub(repl, text), mapping


def restore_urls(text: str, mapping: Dict[str, str]) -> str:
    if not mapping:
        return text
    out = re.sub(r"\u27e6\s*(\d+)\s*/\s*\u27e7", lambda m: mapping.get(f"\u27e6{m.group(1)}/\u27e7", ""), text)
    missing = [url for key, url in mapping.items() if url not in out]
    if missing:
        out = (out + " " + " ".join(missing)).strip()
    return re.sub(r"[\u27e6\u27e7]", "", out)


def is_only_urls(text: str) -> bool:
    return not re.sub(r"[\s\W]+", "", _URL_RE.sub("", text))


# ----------------------------------------------------------------------------- trích xuất
def _line_text_spans(line: dict) -> Tuple[List[dict], bool]:
    spans = line.get("spans") or []
    texts = [s for s in spans if (s.get("text") or "").strip() and not _is_symbol_span(s)]
    starts_bullet = bool(spans) and _is_symbol_span(spans[0])
    # Nếu dòng bắt đầu bằng span số thứ tự độc lập (vd: "1.\t", "2.\t" được tô màu/cỡ riêng) và phía sau còn chữ,
    # giữ nguyên con số gốc như một ký hiệu đầu dòng để không làm mất màu/cỡ của số và giúp canh lề dòng sau chuẩn xác.
    if len(texts) >= 2 and _is_list_marker_span(texts[0]):
        s0, s1 = texts[0], texts[1]
        raw_has_tab = "\t" in (s0.get("text") or "") or bool(s0.get("had_tab"))
        style_diff = (
            abs(float(s0.get("size", 10.0)) - float(s1.get("size", 10.0))) > 0.5
            or int(s0.get("color", 0)) != int(s1.get("color", 0))
            or (int(s0.get("flags", 0)) & 16) != (int(s1.get("flags", 0)) & 16)
        )
        gap = float(s1["bbox"][0]) - float(s0["bbox"][2])
        if raw_has_tab or style_diff or gap >= 2.0:
            starts_bullet = True
            texts = texts[1:]
    return texts, starts_bullet


def _span_style(sp: dict) -> Tuple[float, Tuple[float, float, float], bool, bool, bool]:
    name = sp.get("font", "")
    flags = int(sp.get("flags", 0))
    bold = bool(flags & 16) or bool(re.search(r"bold|black|heavy|semibold", name, re.I))
    italic = bool(flags & 2) or bool(re.search(r"italic|oblique", name, re.I))
    return float(sp.get("size", 10.0)), _rgb(int(sp.get("color", 0))), bold, italic, _is_serif(name, flags)


def _style_of(spans: List[dict]) -> Tuple[float, Tuple[float, float, float], bool, bool, bool]:
    dom = max(spans, key=lambda s: len((s.get("text") or "").strip()))
    return _span_style(dom)


def _drawn_rects(page: fitz.Page) -> List[fitz.Rect]:
    rects: List[fitz.Rect] = []
    try:
        for d in page.get_drawings():
            r = d.get("rect")
            if r is None or r.is_empty or r.width < 6 or r.height < 6:
                continue
            if r.width * r.height > page.rect.width * page.rect.height * 0.85:
                continue
            if d.get("fill") is None and d.get("color") is None:
                continue
            rects.append(fitz.Rect(r))
    except Exception:
        pass
    return rects


def _extract_vector_lines(page: fitz.Page) -> Tuple[List[fitz.Rect], List[fitz.Rect]]:
    """Trích xuất các đường kẻ dọc (v_lines) và đường kẻ ngang (h_lines) của bảng/khung để chống gộp và tràn chữ."""
    v_lines: List[fitz.Rect] = []
    h_lines: List[fitz.Rect] = []
    try:
        for d in page.get_drawings():
            for item in d.get("items", []):
                kind = item[0]
                if kind == "l":
                    p1, p2 = item[1], item[2]
                    r = fitz.Rect(min(p1.x, p2.x), min(p1.y, p2.y), max(p1.x, p2.x), max(p1.y, p2.y))
                elif kind == "re":
                    r = fitz.Rect(item[1])
                else:
                    continue
                if r.width < 4.0 and r.height > 10.0:
                    v_lines.append(r)
                elif r.height < 4.0 and r.width > 12.0:
                    h_lines.append(r)
    except Exception:
        pass
    return v_lines, h_lines


def _has_v_line_between(x_left: float, x_right: float, y0: float, y1: float, v_lines: List[fitz.Rect]) -> bool:
    if x_right <= x_left:
        return False
    ym = (y0 + y1) / 2.0
    for vl in v_lines:
        vx = (vl.x0 + vl.x1) / 2.0
        if x_left - 1.0 <= vx <= x_right + 1.0 and vl.y0 - 2.0 <= ym <= vl.y1 + 2.0:
            return True
    return False


def _has_h_line_between(y_top: float, y_bottom: float, x0: float, x1: float, h_lines: List[fitz.Rect],
                        require_full_span: bool = False) -> bool:
    if y_bottom < y_top:
        return False
    xm = (x0 + x1) / 2.0
    for hl in h_lines:
        hy = (hl.y0 + hl.y1) / 2.0
        if y_top - 1.5 <= hy <= y_bottom + 1.5:
            if require_full_span:
                if hl.x0 <= x0 + 6.0 and hl.x1 >= x1 - 6.0:
                    return True
            elif hl.x0 - 2.0 <= xm <= hl.x1 + 2.0:
                return True
    return False


def _find_container(rect: fitz.Rect, drawn: List[fitz.Rect]) -> Optional[fitz.Rect]:
    dx = min(2.0, max(0.2, rect.width * 0.08))
    dy = min(2.5, max(0.2, rect.height * 0.18))
    probe = fitz.Rect(rect.x0 + dx, rect.y0 + dy, rect.x1 - dx, rect.y1 - dy)
    if probe.is_empty:
        probe = fitz.Rect(rect)
    best: Optional[fitz.Rect] = None
    for r in drawn:
        if r.contains(probe) and (best is None or r.width * r.height < best.width * best.height):
            best = r
    return best


def _is_visible(pix: "fitz.Pixmap", rect: fitz.Rect, scale: float, color: Tuple[float, float, float],
                vertical: bool = False) -> bool:
    """Chữ có thật sự hiện ra không? Cần có cả điểm ảnh chữ lẫn nền tương phản dọc theo cả chiều đọc và chiều cao chữ
    (loại chữ trắng trên nền trắng, chữ bị hộp che khuất, hoặc đường viền bảng cắt ngang vùng chữ ẩn)."""
    x0 = max(0, int(rect.x0 * scale))
    y0 = max(0, int(rect.y0 * scale))
    x1 = min(pix.width, int(rect.x1 * scale) + 1)
    y1 = min(pix.height, int(rect.y1 * scale) + 1)
    if x1 - x0 < 1 or y1 - y0 < 1:
        return False
    n, samples, w = pix.n, pix.samples, pix.width
    tr, tg, tb = (int(c * 255) for c in color)
    col_fg = [False] * (x1 - x0)
    col_bg = [False] * (x1 - x0)
    row_fg = [False] * (y1 - y0)
    row_bg = [False] * (y1 - y0)
    for y in range(y0, y1):
        ry = y - y0
        row = y * w * n
        for x in range(x0, x1):
            rx = x - x0
            i = row + x * n
            dr, dg, db = samples[i] - tr, samples[i + 1] - tg, samples[i + 2] - tb
            d2 = dr * dr + dg * dg + db * db
            if d2 < 9000:
                col_fg[rx] = True
                row_fg[ry] = True
            elif d2 > 2500:
                col_bg[rx] = True
                row_bg[ry] = True
    cc = sum(1 for fg, bg in zip(col_fg, col_bg) if fg and bg)
    cr = sum(1 for fg, bg in zip(row_fg, row_bg) if fg and bg)
    return cc >= max(3, 0.22 * (x1 - x0)) and cr >= max(3, 0.22 * (y1 - y0))


def _explode_span(span: dict, vertical: bool) -> List[dict]:
    """Từ span của rawdict (có từng ký tự) tạo span có `text`, và tách tại khoảng trống rất rộng (ô bảng, cột, tab)."""
    chars = span.get("chars") or []
    if not chars:
        return [span] if span.get("text") else []
    size = float(span.get("size", 10.0))
    gap_limit = max(10.0, 1.5 * size)
    pieces: List[List[dict]] = [[]]
    piece_had_tab: List[bool] = [False]
    last_end = None
    for ch in chars:
        c_str = ch["c"]
        bb = ch["bbox"]
        if c_str == "\t":
            if any(c["c"].strip() for c in pieces[-1]):
                piece_had_tab[-1] = True
                pieces.append([])
                piece_had_tab.append(False)
                last_end = None
            continue
        if c_str.strip():
            if last_end is not None:
                gap = (last_end - bb[3]) if vertical else (bb[0] - last_end)
                if gap > gap_limit and any(c["c"].strip() for c in pieces[-1]):
                    piece_had_tab[-1] = True
                    pieces.append([])
                    piece_had_tab.append(False)
            last_end = bb[1] if vertical else bb[2]
        pieces[-1].append(ch)
    out: List[dict] = []
    for piece, had_tab in zip(pieces, piece_had_tab):
        text = "".join(c["c"] for c in piece)
        if not text.strip():
            continue
        non_ws = [c for c in piece if c["c"].strip()]
        vis_chars = [c for c in non_ws if c["c"] != "_"] or non_ws
        sp = {k: v for k, v in span.items() if k != "chars"}
        sp["text"] = text
        sp["bbox"] = (
            min(c["bbox"][0] for c in non_ws), min(c["bbox"][1] for c in non_ws),
            max(c["bbox"][2] for c in non_ws), max(c["bbox"][3] for c in non_ws),
        )
        sp["vis_bbox"] = (
            min(c["bbox"][0] for c in vis_chars), min(c["bbox"][1] for c in vis_chars),
            max(c["bbox"][2] for c in vis_chars), max(c["bbox"][3] for c in vis_chars),
        )
        sp["origin"] = non_ws[0]["origin"]
        sp["had_tab"] = had_tab
        out.append(sp)
    return out


def _join_spans_with_gaps(spans: List[dict], vertical: bool) -> Tuple[str, List[Tuple[str, float, bool, bool]]]:
    """Ghép các span trên cùng một dòng, chèn khoảng trắng nếu giữa 2 span có khoảng cách từ rõ rệt mà thiếu dấu cách,
    đồng thời chuyển chữ số/TM mũ (superscript) sang ký tự Unicode chuẩn (¹²³... / ™)."""
    raw_parts: List[str] = []
    styled: List[Tuple[str, float, bool, bool]] = []
    max_sz = max((float(sp.get("size", 10.0)) for sp in spans if (sp.get("text") or "").strip()), default=10.0)
    for i, sp in enumerate(spans):
        t = sp.get("text") or ""
        if not t:
            continue
        sz, _, bold, italic, _ = _span_style(sp)
        flags = int(sp.get("flags", 0))
        is_sup_span = (sz < max_sz * 0.75) or bool(flags & 1)
        t_strip = t.strip()
        if is_sup_span and t_strip:
            if t_strip.upper() == "TM":
                t = t.replace(t_strip, "™")
                sz = max_sz
            elif t_strip.isdigit():
                t = t.translate(_SUP_DIGITS)
                sz = max_sz
        if i > 0 and raw_parts:
            prev_sp = spans[i - 1]
            prev_t = raw_parts[-1]
            prev_sz, _, prev_bold, prev_italic, _ = _span_style(prev_sp)
            is_sup = is_sup_span or (sz < prev_sz * 0.75) or (prev_sz < sz * 0.75)
            gap = (prev_sp["bbox"][1] - sp["bbox"][3]) if vertical else (sp["bbox"][0] - prev_sp["bbox"][2])
            style_changed = (bold != prev_bold) or (italic != prev_italic)
            pipe_boundary = prev_t.endswith("|") or t.startswith("|")
            need_space = (
                not is_sup
                and not prev_t[-1].isspace()
                and not t[0].isspace()
                and (
                    gap >= max(1.8, 0.18 * max(sz, prev_sz))
                    or (
                        gap >= -1.5
                        and (style_changed or pipe_boundary)
                        and prev_t[-1] not in "-–—([{/"
                        and t[0] not in ",.;:!?)]}™⁰¹²³⁴⁵⁶⁷⁸⁹"
                    )
                )
            )
            if need_space:
                t = " " + t
        raw_parts.append(t)
        styled.append((t, sz, bold, italic))
    return clean_text("".join(raw_parts)), styled


def _split_segments(spans: List[dict], vertical: bool, v_lines: Optional[List[fitz.Rect]] = None,
                    h_lines: Optional[List[fitz.Rect]] = None,
                    is_footer_or_header: bool = False) -> List[List[dict]]:
    """Tách một dòng thành các cụm cách nhau xa (ô bảng, cột) để mỗi cụm là một đoạn riêng."""
    if len(spans) < 2:
        return [spans]
    if vertical:
        ordered = sorted(spans, key=lambda sp: -sp["bbox"][3])
    else:
        ordered = sorted(spans, key=lambda sp: sp["bbox"][0])
    groups: List[List[dict]] = [[ordered[0]]]
    for sp in ordered[1:]:
        prev = groups[-1][-1]
        sp_sz = float(sp.get("size", 10.0))
        prev_sz = float(prev.get("size", 10.0))
        size = max(sp_sz, prev_sz)
        is_sup = (sp_sz < prev_sz * 0.75) or (prev_sz < sp_sz * 0.75)
        if vertical:
            gap = prev["bbox"][1] - sp["bbox"][3]
            baseline_diff = abs(sp["bbox"][0] - prev["bbox"][0])
            crossed = bool(gap > 4.0 and h_lines and _has_h_line_between(sp["bbox"][3], prev["bbox"][1], min(prev["bbox"][0], sp["bbox"][0]), max(prev["bbox"][2], sp["bbox"][2]), h_lines))
            split = (gap > max(8.0, 1.0 * size) and not is_sup) or baseline_diff > 3.5 or crossed or bool(prev.get("had_tab"))
        else:
            gap = sp["bbox"][0] - prev["bbox"][2]
            crossed = bool(gap > 4.0 and v_lines and _has_v_line_between(prev["bbox"][2], sp["bbox"][0], min(prev["bbox"][1], sp["bbox"][1]), max(prev["bbox"][3], sp["bbox"][3]), v_lines))
            max_gap = max(8.0, 1.2 * size)
            if is_footer_or_header and (
                "|" in (prev.get("text") or "")
                or "|" in (sp.get("text") or "")
                or is_only_urls(sp.get("text") or "")
            ):
                max_gap = max(max_gap, 25.0)
            split = (gap > max_gap and not is_sup) or crossed or bool(prev.get("had_tab"))
        if split:
            groups.append([sp])
        else:
            groups[-1].append(sp)
    return groups


def _merge_horizontal_units(units: List[Unit], page_h: float,
                            v_lines: Optional[List[fitz.Rect]] = None) -> List[Unit]:
    """Hợp nhất các hộp chữ nằm ngang liền kề trên cùng một dòng cơ sở (đặc biệt là chân trang bị tách đôi
    như 'National Center for Pyramid Model Innovations |' + 'ChallengingBehavior.org' hoặc span cùng dòng bị PDF tách line)."""
    if len(units) < 2:
        return units
    used = [False] * len(units)
    out: List[Unit] = []
    for i, a in enumerate(units):
        if used[i]:
            continue
        if a.vertical or len(a.line_rects) != 1:
            out.append(a)
            continue
        # Tìm các đoạn đơn dòng nằm ngay bên phải a trên cùng baseline
        merged_any = True
        while merged_any:
            merged_any = False
            best_j = -1
            best_gap = 1e9
            in_margin_band = (a.rect.y0 > page_h * 0.90) or (a.rect.y1 < page_h * 0.08)
            for j, b in enumerate(units):
                if j == i or used[j] or b.vertical or len(b.line_rects) != 1 or b.page != a.page:
                    continue
                if abs(a.size - b.size) > 1.5:
                    continue
                if abs(a.rect.y0 - b.rect.y0) > max(3.5, 0.3 * a.size) or abs(a.rect.y1 - b.rect.y1) > max(3.5, 0.3 * a.size):
                    continue
                if (a.container is None) != (b.container is None):
                    continue
                if a.container is not None and b.container is not None and tuple(a.container) != tuple(b.container):
                    continue
                gap = b.rect.x0 - a.rect.x1
                if gap < -2.0:
                    continue
                if v_lines and _has_v_line_between(a.rect.x1, b.rect.x0, min(a.rect.y0, b.rect.y0), max(a.rect.y1, b.rect.y1), v_lines):
                    continue
                footer_pair = in_margin_band and gap < 25.0 and (
                    "|" in a.text or "|" in b.text or is_only_urls(b.text) or is_only_urls(a.text)
                )
                inline_pair = (not a.ended_with_tab and not b.starts_bullet and gap <= max(6.5, 0.7 * a.size))
                if (footer_pair or inline_pair) and gap < best_gap:
                    best_gap = gap
                    best_j = j
            if best_j >= 0:
                b = units[best_j]
                used[best_j] = True
                a.rect |= b.rect
                a.line_rects = [a.line_rects[0] | b.line_rects[0]]
                a.text = clean_text(a.text + " " + b.text)
                a.ended_with_tab = b.ended_with_tab
                a._last_style = b._last_style
                if a._styled_lines and b._styled_lines:
                    a._styled_lines[-1] = a._styled_lines[-1] + [(" ", a.size, False, False)] + b._styled_lines[0]
                if len(b.text) > a._dom_chars and not is_only_urls(b.text):
                    a.size, a.color, a.bold, a.italic, a.serif = b.size, b.color, b.bold, b.italic, b.serif
                    a._dom_chars = len(b.text)
                merged_any = True
        out.append(a)
    return out


def _merge_units(units: List[Unit], v_lines: Optional[List[fitz.Rect]] = None,
                 h_lines: Optional[List[fitz.Rect]] = None) -> List[Unit]:
    """Nối các dòng liền kề (thường mỗi dòng là một block riêng trong PDF) thành đoạn văn đầy đủ."""
    merged: List[Unit] = []
    for u in units:
        prev = merged[-1] if merged else None
        if prev is not None and _can_merge(prev, u, v_lines=v_lines, h_lines=h_lines):
            prev.rect |= u.rect
            prev.line_rects += u.line_rects
            prev.text = clean_text(prev.text + " " + u.text)
            prev.ended_with_tab = u.ended_with_tab
            prev._last_style = u._last_style
            prev._styled_lines.extend(u._styled_lines)
            if len(u.text) > prev._dom_chars:
                prev.size, prev.color, prev.bold, prev.italic, prev.serif = u.size, u.color, u.bold, u.italic, u.serif
                prev._dom_chars = len(u.text)
            continue
        u._dom_chars = len(u.text)
        merged.append(u)
    for u in merged:
        u.max_lines = max(1, len(u.line_rects))
        if len(u.line_rects) > 1:
            if u.vertical:
                xs = sorted(l.x0 for l in u.line_rects)
            else:
                xs = sorted(l.y0 for l in u.line_rects)
            diffs = [b - a for a, b in zip(xs, xs[1:]) if b - a > 0.1]
            u.pitch = statistics.median(diffs) if diffs else 0.0
    return merged


def _finalize_unit_markup(u: Unit) -> None:
    """Xây dựng `markup_text` / `rich_text` có gắn thẻ <b>, <i>, <sup> khi đoạn văn có định dạng hỗn hợp."""
    u.max_lines = max(1, len(u.line_rects))
    non_ws = [(t, sz, b, it) for line in u._styled_lines for (t, sz, b, it) in line if t and t.strip()]
    if not non_ws:
        u.has_inline_styles = False
        u.markup_text = u.text
        u.rich_text = u.text
        return

    def is_sup(t: str, sz: float) -> bool:
        return sz < u.size * 0.75 and len(t.strip()) <= 8

    # Nếu có chữ số hoặc TM mũ so với u.size tổng thể (sau khi gom nhiều dòng), chuẩn hóa sang ¹²³ / ™ trong cả u.text
    text_updated = False
    for line in u._styled_lines:
        for idx_s, (t, sz, b, it) in enumerate(line):
            ts = (t or "").strip()
            if ts and is_sup(t, sz):
                if ts.upper() == "TM":
                    line[idx_s] = (t.replace(ts, "™"), u.size, b, it)
                    text_updated = True
                elif ts.isdigit():
                    line[idx_s] = (t.translate(_SUP_DIGITS), u.size, b, it)
                    text_updated = True
    if text_updated:
        u.text = clean_text(" ".join("".join(t for (t, _, _, _) in line) for line in u._styled_lines))
        non_ws = [(t, sz, b, it) for line in u._styled_lines for (t, sz, b, it) in line if t and t.strip()]

    non_sup = [(t, sz, b, it) for (t, sz, b, it) in non_ws if not is_sup(t, sz)]
    has_sup = len(non_sup) < len(non_ws)
    has_mixed_bold = len({b for (_, _, b, _) in non_sup}) > 1
    has_mixed_italic = len({it for (_, _, _, it) in non_sup}) > 1

    if not (has_sup or has_mixed_bold or has_mixed_italic):
        u.has_inline_styles = False
        u.markup_text = u.text
        u.rich_text = u.text
        return

    # Gom các span liên tiếp có cùng trạng thái (sup, bold, italic) qua các dòng
    runs: List[Tuple[str, Tuple[bool, bool, bool]]] = []
    for li, line in enumerate(u._styled_lines):
        for si, (t, sz, b, it) in enumerate(line):
            if not t:
                continue
            sup_flag = is_sup(t, sz) if t.strip() else False
            b_flag = (not sup_flag) and has_mixed_bold and b
            i_flag = (not sup_flag) and has_mixed_italic and it
            state = (sup_flag, b_flag, i_flag)
            piece = t
            if li < len(u._styled_lines) - 1 and si == len(line) - 1:
                piece = piece + "\n"
            if runs and runs[-1][1] == state:
                runs[-1] = (runs[-1][0] + piece, state)
            elif not t.strip() and runs:
                runs[-1] = (runs[-1][0] + piece, runs[-1][1])
            else:
                runs.append((piece, state))

    parts: List[str] = []
    for raw_piece, (sup_flag, b_flag, i_flag) in runs:
        norm = _nfkc_preserve_superscripts(raw_piece or "").replace("\u00ad", "")
        norm = re.sub(r"([A-Za-z])-\s*\n\s*([a-z])", r"\1\2", norm)
        norm = re.sub(r"\s*\n+\s*", " ", norm)
        norm = re.sub(r"[ \t\u00a0]{2,}", " ", norm)
        if not norm:
            continue
        lead = " " if norm[0].isspace() else ""
        trail = " " if norm[-1].isspace() else ""
        core = norm.strip()
        if not core:
            if lead or trail:
                parts.append(" ")
            continue
        if sup_flag:
            core = f"<sup>{core}</sup>"
        if i_flag:
            core = f"<i>{core}</i>"
        if b_flag:
            core = f"<b>{core}</b>"
        parts.append(f"{lead}{core}{trail}")

    markup = re.sub(r" {2,}", " ", "".join(parts)).strip()
    u.has_inline_styles = bool(markup and markup != u.text)
    u.markup_text = markup if u.has_inline_styles else u.text
    u.rich_text = u.markup_text


def _styles_compatible(a: Unit, b: Unit) -> bool:
    if abs(a.size - b.size) > 0.6 or a.serif != b.serif:
        # Cho phép chênh cỡ chữ nhẹ (<=1.1pt) nếu span cuối dòng trên và span đầu dòng dưới cùng cỡ & họ phông
        if a._last_style and b._first_style:
            if abs(a._last_style[0] - b._first_style[0]) <= 0.6 and a._last_style[4] == b._first_style[4]:
                return True
        return False
    if a.bold == b.bold and a.color == b.color:
        return True
    # Nếu span cuối của dòng trên và span đầu của dòng dưới trùng style (đậm/màu), cho phép nối
    if a._last_style and b._first_style:
        if a._last_style[2] == b._first_style[2] and a._last_style[1] == b._first_style[1]:
            return True
    # Chữ thân bài / danh sách (<= 13.5pt) có từ in đậm/nghiêng hoặc cụm link màu xanh giữa câu:
    if a.size <= 13.5 and not a.vertical:
        a_txt = a.text.strip()
        b_txt = b.text.strip()
        # Không nối nếu dòng trên là tiêu đề in đậm ngắn đứng riêng trước đoạn thường
        if a.bold and not b.bold and not a.starts_bullet and len(a.line_rects) == 1:
            if not (a_txt and a_txt[-1] not in ".?!:" and b_txt and b_txt[0].islower()):
                return False
        # Không nối nếu dòng trên đã kết thúc câu và dòng dưới là đề mục/câu hỏi in đậm mới
        if not a.bold and b.bold and a_txt and a_txt[-1] in ".?!" and b_txt and b_txt[0].isupper():
            return False
        # Nếu dòng trên chưa kết thúc câu hoặc dòng dưới bắt đầu bằng chữ thường -> cùng một câu bị ngắt dòng
        if (a_txt and a_txt[-1] not in ".?!:") or (b_txt and b_txt[0].islower()):
            return True
    return False


def _can_merge(a: Unit, b: Unit, v_lines: Optional[List[fitz.Rect]] = None,
               h_lines: Optional[List[fitz.Rect]] = None) -> bool:
    if b.starts_bullet or a.ended_with_tab or a.vertical != b.vertical or a.page != b.page:
        return False
    if re.match(r"^\s*(?:\d{1,3}[.)]?|[A-Za-z][.)]|[IVX]{1,4}\.)\s+\S", b.text):
        return False  # dòng mới bắt đầu bằng số/chữ đánh số (chú thích 13., a), ...) là mục riêng
    if not _styles_compatible(a, b):
        return False
    if (a.container is None) != (b.container is None):
        return False
    if a.container is not None and b.container is not None and tuple(a.container) != tuple(b.container):
        return False
    last = a.line_rects[-1]
    first = b.line_rects[0]
    if a.vertical:
        gap = first.x0 - last.x1
        y_overlap = min(first.y1, last.y1) - max(first.y0, last.y0)
        if y_overlap <= 2.0:
            return False
        if h_lines and _has_h_line_between(min(first.y1, last.y1), max(first.y0, last.y0), min(last.x0, first.x0), max(last.x1, first.x1), h_lines):
            return False
        aligned = abs(first.y1 - a.line_rects[0].y1) <= 2.5 or abs((first.y0 + first.y1) / 2 - (last.y0 + last.y1) / 2) <= 3.5
    else:
        gap = first.y0 - last.y1
        x_overlap = min(first.x1, last.x1) - max(first.x0, last.x0)
        if x_overlap <= 2.0:
            return False
        if h_lines and _has_h_line_between(last.y1 - 1.0, first.y0 + 1.0, min(last.x0, first.x0), min(last.x1, first.x1), h_lines, require_full_span=True):
            return False
        a0 = a.line_rects[0]
        hanging_ok = a.starts_bullet and 0.0 <= (first.x0 - a0.x0) <= 22.0
        aligned = (
            abs(first.x0 - a0.x0) <= 2.5
            or abs(first.x0 - last.x0) <= 2.5
            or hanging_ok
            or abs((first.x0 + first.x1) / 2 - (last.x0 + last.x1) / 2) <= max(3.0, 0.2 * a.size)
            or abs(first.x1 - last.x1) <= 2.5
        )
    return aligned and -0.35 * a.size <= gap <= a.size * 0.6


def _deduplicate_units(page_units: List[Unit]) -> List[Unit]:
    """Gộp các Unit trùng lặp chồng khít lên nhau trên cùng một trang (vd: footer nhân đôi ở trang biểu mẫu)."""
    out: List[Unit] = []
    for u in page_units:
        norm_u = re.sub(r"\W+", "", u.text.lower())
        merged_dup = False
        for prev in out:
            if prev.vertical != u.vertical:
                continue
            norm_p = re.sub(r"\W+", "", prev.text.lower())
            if not norm_u or not norm_p or (norm_u != norm_p and norm_u not in norm_p and norm_p not in norm_u):
                continue
            ix0, iy0 = max(prev.rect.x0, u.rect.x0), max(prev.rect.y0, u.rect.y0)
            ix1, iy1 = min(prev.rect.x1, u.rect.x1), min(prev.rect.y1, u.rect.y1)
            if ix1 > ix0 and iy1 > iy0:
                inter = (ix1 - ix0) * (iy1 - iy0)
                min_area = max(1.0, min(prev.rect.width * prev.rect.height, u.rect.width * u.rect.height))
                if inter / min_area >= 0.6:
                    prev.line_rects.extend(u.line_rects)
                    if len(u.text) > len(prev.text):
                        prev.text = u.text
                        prev.rect |= u.rect
                    merged_dup = True
                    break
        if not merged_dup:
            out.append(u)
    return out


def extract_units(doc: fitz.Document, pages: Optional[set] = None) -> List[Unit]:
    units: List[Unit] = []
    for pno in range(len(doc)):
        if pages is not None and pno not in pages:
            continue
        page = doc[pno]
        page_h = page.rect.height
        drawn = _drawn_rects(page)
        v_lines, h_lines = _extract_vector_lines(page)
        scale = 2.0
        page_pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        data = page.get_text("rawdict")
        page_units: List[Unit] = []
        dropcaps: List[Tuple[str, fitz.Rect, float]] = []

        for block in data.get("blocks", []):
            if block.get("type") != 0:
                continue
            pending_bullet = False  # dấu đầu dòng nằm ở một "line" riêng ngay trước dòng chữ
            for line in block.get("lines", []):
                dx, dy = line.get("dir", (1.0, 0.0))
                vertical = abs(dy) > 0.9
                raw_spans = line.get("spans") or []
                exploded: List[dict] = []
                for sp in raw_spans:
                    exploded.extend(_explode_span(sp, vertical))
                line = dict(line, spans=exploded)
                all_spans, starts_bullet = _line_text_spans(line)
                if not all_spans:
                    if any(_is_symbol_span(sp) for sp in exploded):
                        pending_bullet = True
                    continue
                if pending_bullet:
                    starts_bullet, pending_bullet = True, False
                line_y0 = min(sp["bbox"][1] for sp in all_spans)
                line_y1 = max(sp["bbox"][3] for sp in all_spans)
                if line_y0 < -0.5 or line_y1 <= 18.0:
                    continue
                is_footer_or_header = (not vertical) and (line_y0 > page_h * 0.90 or line_y1 < page_h * 0.08)
                segments = _split_segments(
                    all_spans, vertical, v_lines=v_lines, h_lines=h_lines,
                    is_footer_or_header=is_footer_or_header,
                )
                for gi, spans in enumerate(segments):
                    text, styled_line = _join_spans_with_gaps(spans, vertical)
                    bbox = fitz.Rect(
                        min(sp["bbox"][0] for sp in spans), min(sp["bbox"][1] for sp in spans),
                        max(sp["bbox"][2] for sp in spans), max(sp["bbox"][3] for sp in spans),
                    )
                    vis_bbox = fitz.Rect(
                        min(sp.get("vis_bbox", sp["bbox"])[0] for sp in spans),
                        min(sp.get("vis_bbox", sp["bbox"])[1] for sp in spans),
                        max(sp.get("vis_bbox", sp["bbox"])[2] for sp in spans),
                        max(sp.get("vis_bbox", sp["bbox"])[3] for sp in spans),
                    )
                    size, color, bold, italic, serif = _style_of(spans)
                    # Phát hiện chữ cái lớn đầu đoạn (Drop-Cap như chữ 'W' 52pt trước 'elcome!')
                    if not vertical and len(text) == 1 and text.isalpha() and text.isupper() and size >= 24.0:
                        if _is_visible(page_pix, vis_bbox, scale, color, vertical):
                            dropcaps.append((text, bbox, size))
                        continue
                    if not should_translate(text):
                        continue
                    if not _is_visible(page_pix, vis_bbox, scale, color, vertical):
                        continue
                    bullet = bool(starts_bullet and gi == 0)
                    has_right_peer = (not vertical) and (len(segments) > 1 and gi < len(segments) - 1)
                    ended_tab = bool(
                        (not vertical)
                        and (has_right_peer or any(sp.get("had_tab") for sp in spans))
                    )
                    u = Unit(
                        page=pno, rect=fitz.Rect(bbox), line_rects=[fitz.Rect(bbox)], text=text,
                        size=size, color=color, bold=bold, italic=italic, serif=serif,
                        vertical=vertical,
                        container=None if vertical else _find_container(bbox, drawn),
                        starts_bullet=bullet,
                        ended_with_tab=ended_tab,
                        _dom_chars=len(text),
                        _first_style=_span_style(spans[0]),
                        _last_style=_span_style(spans[-1]),
                        _styled_lines=[styled_line],
                    )
                    page_units.append(u)

        page_units = _merge_horizontal_units(page_units, page_h, v_lines=v_lines)
        page_units = _merge_units(page_units, v_lines=v_lines, h_lines=h_lines)
        # Ghép Drop-Cap (nếu có) vào đoạn văn ngay bên phải nó
        for dc_char, dc_rect, _ in dropcaps:
            for u in page_units:
                if u.vertical or not u.text or not u.text[0].islower():
                    continue
                l0 = u.line_rects[0]
                if -4.0 <= (l0.x0 - dc_rect.x1) <= 16.0 and dc_rect.y0 - 6.0 <= l0.y0 <= dc_rect.y1:
                    u.text = dc_char + u.text
                    if u._styled_lines and u._styled_lines[0]:
                        first_t, first_sz, first_b, first_i = u._styled_lines[0][0]
                        u._styled_lines[0][0] = (dc_char + first_t, first_sz, first_b, first_i)
                    u.line_rects.insert(0, fitz.Rect(dc_rect))
                    u.rect.x0 = min(u.rect.x0, dc_rect.x0)
                    break
        page_units = _deduplicate_units(page_units)
        for u in page_units:
            _finalize_unit_markup(u)
        _detect_alignment(page_units, page)
        units.extend(page_units)
    return units


def _detect_alignment(page_units: List[Unit], page: fitz.Page) -> None:
    pw = page.rect.width
    for u in [x for x in page_units if not x.vertical]:
        r = u.rect
        if len(u.line_rects) > 1:
            x0s = [l.x0 for l in u.line_rects]
            x1s = [l.x1 for l in u.line_rects]
            cs = [(l.x0 + l.x1) / 2 for l in u.line_rects]
            if max(x0s) - min(x0s) < 3:
                u.align = "left"
            elif max(cs) - min(cs) < max(3.0, 0.2 * u.size):
                u.align = "center"
            elif max(x1s) - min(x1s) < 3:
                u.align = "right"
            continue
        if u.container is not None:
            occupants = sum(1 for o in page_units if o is not u and u.container.contains(o.rect))
            left_gap, right_gap = r.x0 - u.container.x0, u.container.x1 - r.x1
            if occupants == 0:  # khung chỉ chứa đúng đoạn này (ô bảng đơn, thanh tiêu đề)
                if abs(left_gap - right_gap) < 3 and left_gap > 3:
                    u.align = "center"
                    continue
                if left_gap > 3 * max(right_gap, 1.0) and right_gap < 45:
                    u.align = "right"
                    continue
        shares_margin = sum(1 for o in page_units if o is not u and abs(o.rect.x0 - r.x0) <= 1.5) >= 2
        shares_right = sum(1 for o in page_units if o is not u and abs(o.rect.x1 - r.x1) <= 1.5) >= 2
        if shares_right and not shares_margin and r.x0 > 60:
            u.align = "right"
            continue
        if not shares_margin and abs((r.x0 + r.x1) / 2 - pw / 2) < 4 and r.x0 > 30:
            u.align = "center"


# ----------------------------------------------------------------------------- hiển thị
_ALIGN = {"left": 0, "center": 1, "right": 2}


def _untouched_obstacles(page: fitz.Page, todo: List[Unit]) -> List[fitz.Rect]:
    """Lấy bbox của các span không dịch (số trang mục lục, ký hiệu đầu dòng, số thứ tự) để tránh tràn đè lên chúng."""
    redacted = [lr for u in todo for lr in u.line_rects]
    obs: List[fitz.Rect] = []
    try:
        for b in page.get_text("dict").get("blocks", []):
            if b.get("type") != 0:
                continue
            for l in b.get("lines", []):
                for sp in l.get("spans", []):
                    if not (sp.get("text") or "").strip():
                        continue
                    sr = fitz.Rect(sp["bbox"])
                    if sr.is_empty or sr.width < 1 or sr.height < 1:
                        continue
                    if any(sr.intersects(rr) for rr in redacted):
                        continue
                    obs.append(sr)
    except Exception:
        pass
    return obs


def _free_right(u: Unit, others: List[Unit], limit: float,
                v_lines: Optional[List[fitz.Rect]] = None,
                obstacles: Optional[List[fitz.Rect]] = None,
                drawn: Optional[List[fitz.Rect]] = None) -> float:
    """Mép phải tối đa có thể mở rộng sang phải mà không đè chữ khác, khung bên cạnh hoặc đường kẻ bảng."""
    right = limit
    min_line_x1 = min((lr.x1 for lr in u.line_rects if lr.x0 >= u.rect.x0 - 2.0), default=u.rect.x1)
    for o in others:
        if o is u or o.page != u.page or o.vertical:
            continue
        if o.rect.y1 > u.rect.y0 + 1 and o.rect.y0 < u.rect.y1 - 1 and o.rect.x0 >= min_line_x1 - 0.5:
            right = min(right, o.rect.x0 - 3)
    for sr in obstacles or []:
        if sr.y1 > u.rect.y0 + 1 and sr.y0 < u.rect.y1 - 1 and sr.x0 >= min_line_x1 - 0.5:
            right = min(right, sr.x0 - 3)
    for vl in v_lines or []:
        if vl.y1 > u.rect.y0 + 2 and vl.y0 < u.rect.y1 - 2 and vl.x0 >= min_line_x1 - 1.0:
            right = min(right, vl.x0 - 2.5)
    for dr in drawn or []:
        if u.container is not None and tuple(dr) == tuple(u.container):
            continue
        if dr.contains(u.rect):
            continue
        if dr.y1 > u.rect.y0 + 2 and dr.y0 < u.rect.y1 - 2 and dr.x0 >= min_line_x1 - 1.0:
            right = min(right, dr.x0 - 4.0)
    return max(right, min_line_x1)


def _free_left(u: Unit, others: List[Unit], limit: float,
               v_lines: Optional[List[fitz.Rect]] = None,
               obstacles: Optional[List[fitz.Rect]] = None,
               drawn: Optional[List[fitz.Rect]] = None) -> float:
    """Mép trái tối thiểu có thể mở rộng sang trái mà không đè chữ khác, khung bên cạnh hoặc đường kẻ bảng."""
    left = limit
    for o in others:
        if o is u or o.page != u.page or o.vertical:
            continue
        if o.rect.y1 > u.rect.y0 + 1 and o.rect.y0 < u.rect.y1 - 1 and o.rect.x1 <= u.rect.x0 + 0.5:
            left = max(left, o.rect.x1 + 3)
    for sr in obstacles or []:
        if sr.y1 > u.rect.y0 + 1 and sr.y0 < u.rect.y1 - 1 and sr.x1 <= u.rect.x0 + 0.5:
            left = max(left, sr.x1 + 3)
    for vl in v_lines or []:
        if vl.y1 > u.rect.y0 + 2 and vl.y0 < u.rect.y1 - 2 and vl.x1 <= u.rect.x0 + 1.0:
            left = max(left, vl.x1 + 2.5)
    for dr in drawn or []:
        if u.container is not None and tuple(dr) == tuple(u.container):
            continue
        if dr.contains(u.rect):
            continue
        if dr.y1 > u.rect.y0 + 2 and dr.y0 < u.rect.y1 - 2 and dr.x1 <= u.rect.x0 + 1.0:
            left = max(left, dr.x1 + 4.0)
    return min(left, u.rect.x0)


def _free_bottom(u: Unit, others: List[Unit], limit: float,
                 h_lines: Optional[List[fitz.Rect]] = None,
                 obstacles: Optional[List[fitz.Rect]] = None) -> float:
    bottom = limit
    for o in others:
        if o is u or o.page != u.page or o.vertical:
            continue
        if o.rect.x1 > u.rect.x0 + 1 and o.rect.x0 < u.rect.x1 - 1 and o.rect.y0 >= u.rect.y1 - 0.5:
            bottom = min(bottom, o.rect.y0 - 1.5)
    for sr in obstacles or []:
        if sr.x1 > u.rect.x0 + 1 and sr.x0 < u.rect.x1 - 1 and sr.y0 >= u.rect.y1 - 0.5:
            bottom = min(bottom, sr.y0 - 1.5)
    for hl in h_lines or []:
        if hl.x1 > u.rect.x0 + 2 and hl.x0 < u.rect.x1 - 2 and hl.y0 >= u.rect.y1 - 1.0:
            bottom = min(bottom, hl.y0 - 2.0)
    return max(bottom, u.rect.y1)


def _free_up(u: Unit, others: List[Unit], limit: float,
             h_lines: Optional[List[fitz.Rect]] = None,
             obstacles: Optional[List[fitz.Rect]] = None) -> float:
    """Chữ dọc đọc từ dưới lên: y nhỏ nhất có thể mở rộng lên mà không đè chữ khác hoặc vách ngang của bảng."""
    top = limit
    for o in others:
        if o is u or o.page != u.page:
            continue
        if o.rect.x1 > u.rect.x0 + 1 and o.rect.x0 < u.rect.x1 - 1 and o.rect.y1 <= u.rect.y0 + 0.5:
            top = max(top, o.rect.y1 + 3)
    for sr in obstacles or []:
        if sr.x1 > u.rect.x0 + 1 and sr.x0 < u.rect.x1 - 1 and sr.y1 <= u.rect.y0 + 0.5:
            top = max(top, sr.y1 + 3)
    for hl in h_lines or []:
        if hl.x1 > u.rect.x0 + 2 and hl.x0 < u.rect.x1 - 2 and hl.y1 <= u.rect.y0 + 1.5:
            top = max(top, hl.y1 + 2.5)
    return min(top, u.rect.y0)


def _target_box(u: Unit, page_rect: fitz.Rect, others: List[Unit],
                v_lines: Optional[List[fitz.Rect]] = None,
                h_lines: Optional[List[fitz.Rect]] = None,
                obstacles: Optional[List[fitz.Rect]] = None,
                drawn: Optional[List[fitz.Rect]] = None) -> fitz.Rect:
    """Vùng được phép đặt bản dịch (có thể rộng/cao hơn chữ gốc nếu còn chỗ trống)."""
    r = fitz.Rect(u.rect)
    margin = 14.0
    c = u.container
    # Tấm nền trang trí lớn hơn nhiều so với chữ (dải màu, panel ảnh) KHÔNG phải ô chứa: bỏ qua để chữ không tràn vào họa tiết
    if c is not None and c.width > 3 * r.width and c.height > 2 * r.height:
        c = None
    # Khung vươn ra ngoài mép trang (bleed) là họa tiết trang trí, không phải ô chứa
    if c is not None and (c.x1 > page_rect.x1 + 1 or c.x0 < page_rect.x0 - 1 or c.y1 > page_rect.y1 + 1 or c.y0 < page_rect.y0 - 1):
        c = None
    if c is not None:
        c = fitz.Rect(c) & fitz.Rect(page_rect.x0 + margin / 2, page_rect.y0, page_rect.x1 - margin / 2, page_rect.y1)
        pad = max(1.0, min(r.x0 - c.x0, c.x1 - r.x1, 6.0))
        x_left, x_right = c.x0 + pad, c.x1 - pad
        y_bottom_limit = c.y1 - 1.0
    else:
        x_left = max(page_rect.x0 + margin, r.x0 - max(6.0, r.width * 0.25))
        x_right = min(page_rect.x1 - margin, r.x1 + max(6.0, r.width * 0.06))
        if r.y0 > page_rect.height * 0.90 and u.align == "left":
            # Chân trang canh trái cho phép mở rộng sang phải tới trước số trang / vật cản
            x_right = max(x_right, page_rect.x1 - margin * 2.5)
        y_bottom_limit = min(page_rect.y1 - margin, r.y1 + max(10.0, r.height * 0.6))

    free_r = _free_right(u, others, x_right, v_lines=v_lines, obstacles=obstacles, drawn=drawn)
    free_l = _free_left(u, others, x_left, v_lines=v_lines, obstacles=obstacles, drawn=drawn)

    if u.align == "left":
        right = min(x_right, free_r)
        box = fitz.Rect(r.x0, r.y0, right, r.y1)
    elif u.align == "center":
        cx = (r.x0 + r.x1) / 2
        avail_half = min(cx - max(x_left, free_l), min(x_right, free_r) - cx)
        want_half = max(r.width / 2, (x_right - x_left) / 2) if c is not None else r.width / 2 * 1.15
        half = max(r.width / 2, min(want_half, avail_half))
        box = fitz.Rect(cx - half, r.y0, cx + half, r.y1)
    else:
        left = min(r.x0, max(x_left, free_l))
        box = fitz.Rect(left, r.y0, r.x1, r.y1)
    box.y1 = max(box.y1, min(y_bottom_limit, _free_bottom(u, others, y_bottom_limit, h_lines=h_lines, obstacles=obstacles)))
    return box


def _fits(font: fitz.Font, box: fitz.Rect, text: str, size: float, lh: Optional[float], align: int) -> bool:
    tw = fitz.TextWriter(fitz.Rect(-5000, -5000, 5000, 5000))
    try:
        rest = tw.fill_textbox(box, text, font=font, fontsize=size, lineheight=lh, align=align, warn=False)
    except Exception:
        return False
    return not rest


_MARKUP_TAG_RE = re.compile(r"</?(?:b|i|u|em|strong|span|sup|sub|br)\b[^>]*>", re.I)
_INLINE_HTML_RE = re.compile(r"<(?:b|strong|i|em|u|sup|sub|br)\b|\*\*[^*]+\*\*", re.I)
_ALLOWED_HTML_TAG_RE = re.compile(r"</?(?:b|strong|i|em|u|sup|sub|br)\s*/?>", re.I)
_html_archive_cache: Dict[Tuple[bool, Optional[str]], Tuple[Optional[fitz.Archive], str, str]] = {}


def _strip_markup_tags(text: str) -> str:
    """Loại bỏ thẻ HTML/markdown thô (<b>, <i>, **...**) nếu LLM vô tình sinh ra khi dịch văn bản thuần cho PDF."""
    out = _MARKUP_TAG_RE.sub("", text or "")
    out = re.sub(r"\*\*([^*]+)\*\*", r"\1", out)
    return out


def _has_inline_markup(text: str) -> bool:
    return bool(text and _INLINE_HTML_RE.search(text))


def _prepare_html_body(text: str) -> str:
    """Chuẩn hóa nội dung HTML an toàn cho insert_htmlbox (chuyển **bold** -> <b>, escape ký tự '&', '<', '>' ngoài thẻ)."""
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text or "")
    tokens: List[str] = []

    def save_tag(m: re.Match) -> str:
        idx = len(tokens)
        tokens.append(m.group(0))
        return f"\x00TAG{idx}\x00"

    masked = _ALLOWED_HTML_TAG_RE.sub(save_tag, s)
    masked = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)", "&amp;", masked)
    masked = masked.replace("<", "&lt;").replace(">", "&gt;")
    for idx, tag in enumerate(tokens):
        masked = masked.replace(f"\x00TAG{idx}\x00", tag)
    return masked


def _get_html_archive_and_css(serif: bool, fallback_font: Optional[str] = None) -> Tuple[Optional[fitz.Archive], str, str]:
    key = (serif, fallback_font)
    if key in _html_archive_cache:
        return _html_archive_cache[key]
    p_reg = resolve_font_path(serif, False, False, fallback_font)
    if not p_reg:
        _html_archive_cache[key] = (None, "", "")
        return None, "", ""
    p_bold = resolve_font_path(serif, True, False, fallback_font) or p_reg
    p_italic = resolve_font_path(serif, False, True, fallback_font) or p_reg
    p_bi = resolve_font_path(serif, True, True, fallback_font) or p_bold or p_reg
    dirs = list({os.path.dirname(p) for p in (p_reg, p_bold, p_italic, p_bi) if p})
    try:
        archive = fitz.Archive(dirs) if len(dirs) > 1 else fitz.Archive(dirs[0])
    except Exception:
        archive = None
    fam = "SerifVI" if serif else "SansVI"
    css = (
        f'@font-face {{ font-family: "{fam}"; src: url("{os.path.basename(p_reg)}"); font-weight: normal; font-style: normal; }}\n'
        f'@font-face {{ font-family: "{fam}"; src: url("{os.path.basename(p_bold)}"); font-weight: bold; font-style: normal; }}\n'
        f'@font-face {{ font-family: "{fam}"; src: url("{os.path.basename(p_italic)}"); font-weight: normal; font-style: italic; }}\n'
        f'@font-face {{ font-family: "{fam}"; src: url("{os.path.basename(p_bi)}"); font-weight: bold; font-style: italic; }}\n'
        "body { margin: 0; padding: 0; }\n"
    )
    _html_archive_cache[key] = (archive, css, fam)
    return archive, css, fam


def _render_htmlbox(page: fitz.Page, u: Unit, box: fitz.Rect, fallback_font: Optional[str] = None) -> Optional[Tuple[float, float]]:
    """Dựng hộp chữ giàu định dạng (<b>, <i>, <sup>) bằng page.insert_htmlbox; trả về (spare_height, scale) hoặc None nếu cần fallback."""
    if not hasattr(page, "insert_htmlbox"):
        return None
    archive, css, fam = _get_html_archive_and_css(u.serif, fallback_font)
    if archive is None or not fam:
        return None
    html_body = _prepare_html_body(u.dst)
    has_b = bool(re.search(r"<(?:b|strong)\b", html_body, re.I))
    has_i = bool(re.search(r"<(?:i|em)\b", html_body, re.I))
    fw = "bold" if (u.bold and not has_b) else "normal"
    fs = "italic" if (u.italic and not has_i) else "normal"
    r_i = max(0, min(255, int(round(u.color[0] * 255))))
    g_i = max(0, min(255, int(round(u.color[1] * 255))))
    b_i = max(0, min(255, int(round(u.color[2] * 255))))
    col_hex = f"#{r_i:02x}{g_i:02x}{b_i:02x}"
    lh = max(1.12, min(1.45, u.pitch / u.size)) if (u.pitch and u.size) else (1.12 if u.size >= 14.0 else 1.18)
    rot = 90 if u.vertical else 0
    html_str = (
        f'<div style="font-family:{fam}; font-size:{u.size}pt; font-weight:{fw}; '
        f'font-style:{fs}; color:{col_hex}; text-align:{u.align}; line-height:{lh:.2f};">'
        f"{html_body}</div>"
    )
    try:
        res = page.insert_htmlbox(box, html_str, css=css, archive=archive, rotate=rot, scale_low=0.35)
        if isinstance(res, tuple) and len(res) == 2:
            spare_h, scale = float(res[0]), float(res[1])
        else:
            spare_h, scale = float(res), 1.0
        if spare_h < 0:
            return None
        return spare_h, scale
    except Exception as exc:
        logger.debug("insert_htmlbox fallback to TextWriter: %s", exc)
        return None


def _trim_form_underscores(font: fitz.Font, box: fitz.Rect, text: str, size: float, lh: Optional[float], align: int) -> str:
    """Với dòng biểu mẫu có chuỗi gạch chân điền khuyết (vd: 'Giáo viên: ________'), tự rút bớt dấu '_' để vừa khít 1 dòng ở đúng cỡ chữ gốc."""
    if "____" not in text:
        return text
    cur = text
    while not _fits(font, box, cur, size, lh, align):
        nxt = re.sub(r"_{6,}", lambda m: m.group(0)[:-2], cur)
        if nxt == cur:
            break
        cur = nxt
    return cur


def _write(page: fitz.Page, font: fitz.Font, box: fitz.Rect, text: str, size: float, lh: Optional[float],
           align: int, color: Tuple[float, float, float], morph=None) -> None:
    tw = fitz.TextWriter(page.rect)
    tw.fill_textbox(box, text, font=font, fontsize=size, lineheight=lh, align=align, warn=False)
    tw.write_text(page, color=color, morph=morph)


def _choose_size(font: fitz.Font, box: fitz.Rect, text: str, orig: float, lh: Optional[float], align: int,
                 tight: Optional[fitz.Rect] = None) -> Tuple[float, fitz.Rect]:
    """Ưu tiên giữ nguyên số dòng như bản gốc (co chữ tối đa ~22%); nếu không được mới cho xuống dòng/ co thêm."""
    if tight is not None:
        size = orig
        while size >= orig * 0.78 - 1e-6:
            if _fits(font, tight, text, size, lh, align):
                return size, tight
            size -= 0.25
    min_size = max(4.0, orig * 0.55)
    size = orig
    while size >= min_size - 1e-6:
        if _fits(font, box, text, size, lh, align):
            return size, box
        size -= 0.25
    # vẫn tràn: mở rộng xuống (không để mất chữ)
    tall = fitz.Rect(box.x0, box.y0, box.x1, box.y1 + max(box.height, orig * 2) * 3)
    return min_size, tall


def render_units(doc: fitz.Document, units: List[Unit], fallback_font: Optional[str] = None) -> Dict[str, int]:
    stats = {"rendered": 0, "shrunk": 0, "overflow": 0, "skipped": 0}
    by_page: Dict[int, List[Unit]] = {}
    for u in units:
        by_page.setdefault(u.page, []).append(u)

    for pno, page_units in by_page.items():
        page = doc[pno]
        todo = [u for u in page_units if u.dst.strip() and not u.skip]
        if not todo:
            continue
        drawn = _drawn_rects(page)
        v_lines, h_lines = _extract_vector_lines(page)
        obstacles = _untouched_obstacles(page, todo)
        # 1) Xóa chữ gốc: KHÔNG tô nền, không đụng ảnh / đồ họa
        for u in todo:
            for lr in u.line_rects:
                inner = fitz.Rect(lr.x0 + 0.4, lr.y0 + 0.8, lr.x1 - 0.4, lr.y1 - 0.8)
                if inner.is_empty:
                    inner = fitz.Rect(lr)
                page.add_redact_annot(inner, fill=False)
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_REMOVE,
        )
        # 2) Ghi bản dịch
        for u in todo:
            path = resolve_font_path(u.serif, u.bold, u.italic, fallback_font)
            if not path:
                stats["skipped"] += 1
                continue
            font = _get_font(path)
            lh = None
            if u.pitch and u.size:
                lh = max(1.12, min(1.5, u.pitch / u.size))
            align = _ALIGN.get(u.align, 0)
            clean_dst = _strip_markup_tags(u.dst)
            if u.vertical:
                top = _free_up(u, units, 14.0, h_lines=h_lines, obstacles=obstacles)
                if _has_inline_markup(u.dst):
                    html_box = fitz.Rect(
                        u.rect.x0 - 1.5, top,
                        u.rect.x1 + max(2.5, u.size * 0.25), u.rect.y1 + 1.5,
                    )
                    html_res = _render_htmlbox(page, u, html_box, fallback_font)
                    if html_res is not None:
                        _, scale = html_res
                        stats["rendered"] += 1
                        if scale < 0.99:
                            stats["shrunk"] += 1
                        continue
                W = max(u.rect.height, min(u.rect.y1 - top, u.rect.height * 1.8))
                H = u.rect.width + max(0.0, u.size * 0.2)
                local = fitz.Rect(u.rect.x0, u.rect.y1, u.rect.x0 + W, u.rect.y1 + H)
                tight_local = fitz.Rect(local.x0, local.y0, local.x1, local.y0 + u.rect.width + 0.5)
                dst_text = _trim_form_underscores(font, tight_local, clean_dst, u.size, lh, align)
                size, box = _choose_size(font, local, dst_text, u.size, lh, align, tight=tight_local)
                _write(page, font, box, dst_text, size, lh, align, u.color,
                       morph=(fitz.Point(u.rect.x0, u.rect.y1), fitz.Matrix(90)))
            else:
                box0 = _target_box(u, page.rect, units, v_lines=v_lines, h_lines=h_lines, obstacles=obstacles, drawn=drawn)
                if _has_inline_markup(u.dst):
                    html_box = fitz.Rect(
                        box0.x0, box0.y0 - 1.5,
                        box0.x1, max(box0.y1, u.rect.y1 + max(2.5, u.size * 0.3)),
                    )
                    html_res = _render_htmlbox(page, u, html_box, fallback_font)
                    if html_res is not None:
                        _, scale = html_res
                        stats["rendered"] += 1
                        if scale < 0.99:
                            stats["shrunk"] += 1
                        continue
                tight = fitz.Rect(box0.x0, box0.y0, box0.x1, min(box0.y1, u.rect.y1 + 0.5))
                dst_text = _trim_form_underscores(font, tight, clean_dst, u.size, lh, align)
                size, box = _choose_size(font, box0, dst_text, u.size, lh, align, tight=tight)
                _write(page, font, box, dst_text, size, lh, align, u.color)
            stats["rendered"] += 1
            if size < u.size - 0.01:
                stats["shrunk"] += 1
            if box.height > (u.rect.height if u.vertical else box0.height) + 0.5 and size <= max(4.0, u.size * 0.55) + 0.01:
                stats["overflow"] += 1
    return stats
