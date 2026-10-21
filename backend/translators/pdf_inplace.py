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
    protected: Dict[str, str] = field(default_factory=dict)
    _dom_chars: int = 0


_SYMBOLIC_FONT_RE = re.compile(r"wingding|symbol|dingbat|webding|zapf", re.I)
_BULLET_CHARS = set("•·▪◦■□☐☑○●◆◇▶►‣⁃–—-*✓✔")
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


def _is_serif(font_name: str, flags: int) -> bool:
    if _SANS_NAME_RE.search(font_name):
        return False
    if _SERIF_NAME_RE.search(font_name):
        return True
    return bool(flags & 4) and not font_name.lower().startswith(("arial", "calibri"))


def _rgb(c: int) -> Tuple[float, float, float]:
    return (((c >> 16) & 255) / 255.0, ((c >> 8) & 255) / 255.0, (c & 255) / 255.0)


def clean_text(raw: str) -> str:
    text = unicodedata.normalize("NFKC", raw or "").replace("\u00ad", "")
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
    return texts, starts_bullet


def _style_of(spans: List[dict]) -> Tuple[float, Tuple[float, float, float], bool, bool, bool]:
    dom = max(spans, key=lambda s: len((s.get("text") or "").strip()))
    name = dom.get("font", "")
    flags = int(dom.get("flags", 0))
    bold = bool(flags & 16) or bool(re.search(r"bold|black|heavy|semibold", name, re.I))
    italic = bool(flags & 2) or bool(re.search(r"italic|oblique", name, re.I))
    return float(dom.get("size", 10.0)), _rgb(int(dom.get("color", 0))), bold, italic, _is_serif(name, flags)


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


def _find_container(rect: fitz.Rect, drawn: List[fitz.Rect]) -> Optional[fitz.Rect]:
    probe = fitz.Rect(rect.x0 - 0.5, rect.y0 - 0.5, rect.x1 + 0.5, rect.y1 + 0.5)
    best: Optional[fitz.Rect] = None
    for r in drawn:
        if r.contains(probe) and (best is None or r.width * r.height < best.width * best.height):
            best = r
    return best


def _is_visible(pix: "fitz.Pixmap", rect: fitz.Rect, scale: float, color: Tuple[float, float, float],
                vertical: bool = False) -> bool:
    """Chữ có thật sự hiện ra không? Cần có điểm ảnh gần màu chữ ở đủ rộng dọc theo chiều chữ
    (loại chữ ẩn/bị che; tránh nhầm với logo hay đường kẻ chỉ chạm một góc vùng chữ)."""
    x0 = max(0, int(rect.x0 * scale))
    y0 = max(0, int(rect.y0 * scale))
    x1 = min(pix.width, int(rect.x1 * scale) + 1)
    y1 = min(pix.height, int(rect.y1 * scale) + 1)
    if x1 - x0 < 1 or y1 - y0 < 1:
        return False
    n, samples, w = pix.n, pix.samples, pix.width
    tr, tg, tb = (int(c * 255) for c in color)
    hit_cols, hit_rows = set(), set()
    for y in range(y0, y1):
        row = y * w * n
        for x in range(x0, x1):
            i = row + x * n
            dr, dg, db = samples[i] - tr, samples[i + 1] - tg, samples[i + 2] - tb
            if dr * dr + dg * dg + db * db < 6400:  # khoảng cách màu < 80
                hit_cols.add(x)
                hit_rows.add(y)
    span = (y1 - y0) if vertical else (x1 - x0)
    covered = len(hit_rows) if vertical else len(hit_cols)
    return covered >= max(3, 0.25 * span)


def _explode_span(span: dict, vertical: bool) -> List[dict]:
    """Từ span của rawdict (có từng ký tự) tạo span có `text`, và tách tại khoảng trống rất rộng (ô bảng, cột)."""
    chars = span.get("chars") or []
    if not chars:
        return [span] if span.get("text") else []
    gap_limit = max(10.0, 1.5 * float(span.get("size", 10.0)))
    pieces: List[List[dict]] = [[]]
    last_end = None
    for ch in chars:
        bb = ch["bbox"]
        if ch["c"].strip():
            if last_end is not None:
                gap = (last_end - bb[3]) if vertical else (bb[0] - last_end)
                if gap > gap_limit and pieces[-1]:
                    pieces.append([])
            last_end = bb[1] if vertical else bb[2]
        pieces[-1].append(ch)
    out: List[dict] = []
    for piece in pieces:
        text = "".join(c["c"] for c in piece)
        if not text.strip():
            continue
        sp = {k: v for k, v in span.items() if k != "chars"}
        sp["text"] = text
        sp["bbox"] = (
            min(c["bbox"][0] for c in piece), min(c["bbox"][1] for c in piece),
            max(c["bbox"][2] for c in piece), max(c["bbox"][3] for c in piece),
        )
        sp["origin"] = piece[0]["origin"]
        out.append(sp)
    return out


def _split_segments(spans: List[dict], vertical: bool) -> List[List[dict]]:
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
        gap = (prev["bbox"][1] - sp["bbox"][3]) if vertical else (sp["bbox"][0] - prev["bbox"][2])
        if gap > max(8.0, 1.2 * float(sp.get("size", 10.0))):
            groups.append([sp])
        else:
            groups[-1].append(sp)
    return groups


def _merge_units(units: List[Unit]) -> List[Unit]:
    """Nối các dòng liền kề (thường mỗi dòng là một block riêng trong PDF) thành đoạn văn đầy đủ."""
    merged: List[Unit] = []
    for u in units:
        prev = merged[-1] if merged else None
        if prev is not None and _can_merge(prev, u):
            prev.rect |= u.rect
            prev.line_rects += u.line_rects
            prev.text = clean_text(prev.text + " " + u.text)
            if len(u.text) > prev._dom_chars:
                prev.size, prev.color, prev.bold, prev.italic, prev.serif = u.size, u.color, u.bold, u.italic, u.serif
                prev._dom_chars = len(u.text)
            continue
        u._dom_chars = len(u.text)
        merged.append(u)
    for u in merged:
        if len(u.line_rects) > 1:
            if u.vertical:
                xs = sorted(l.x0 for l in u.line_rects)
            else:
                xs = sorted(l.y0 for l in u.line_rects)
            diffs = [b - a for a, b in zip(xs, xs[1:]) if b - a > 0.1]
            u.pitch = statistics.median(diffs) if diffs else 0.0
    return merged


def _can_merge(a: Unit, b: Unit) -> bool:
    if b.starts_bullet or a.vertical != b.vertical or a.page != b.page:
        return False
    if re.match(r"^\s*(?:\d{1,3}[.)]?|[A-Za-z][.)])\s+\S", b.text):
        return False  # dòng mới bắt đầu bằng số/chữ đánh số (chú thích 13., a), ...) là mục riêng
    if abs(a.size - b.size) > 0.6 or a.bold != b.bold or a.color != b.color or a.serif != b.serif:
        return False
    if (a.container is None) != (b.container is None):
        return False
    if a.container is not None and b.container is not None and tuple(a.container) != tuple(b.container):
        return False
    last = a.line_rects[-1]
    first = b.line_rects[0]
    if a.vertical:
        gap = first.x0 - last.x1
        aligned = abs(first.y1 - a.line_rects[0].y1) <= 2.5 or abs((first.y0 + first.y1) / 2 - (last.y0 + last.y1) / 2) <= 3
    else:
        gap = first.y0 - last.y1
        a0 = a.line_rects[0]
        aligned = (
            abs(first.x0 - a0.x0) <= 2.5
            or abs((first.x0 + first.x1) / 2 - (last.x0 + last.x1) / 2) <= max(3.0, 0.2 * a.size)
            or abs(first.x1 - last.x1) <= 2.5
        )
    return aligned and -0.35 * a.size <= gap <= a.size * 0.6


def extract_units(doc: fitz.Document, pages: Optional[set] = None) -> List[Unit]:
    units: List[Unit] = []
    for pno in range(len(doc)):
        if pages is not None and pno not in pages:
            continue
        page = doc[pno]
        drawn = _drawn_rects(page)
        scale = 2.0
        page_pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        data = page.get_text("rawdict")
        page_units: List[Unit] = []
        for block in data.get("blocks", []):
            if block.get("type") != 0:
                continue
            paras: List[List[dict]] = []
            prev_meta = None
            pending_bullet = False  # dấu đầu dòng nằm ở một "line" riêng ngay trước dòng chữ
            for line in block.get("lines", []):
                dx, dy = line.get("dir", (1.0, 0.0))
                vertical = abs(dy) > 0.9
                exploded: List[dict] = []
                for sp in line.get("spans") or []:
                    exploded.extend(_explode_span(sp, vertical))
                line = dict(line, spans=exploded)
                all_spans, starts_bullet = _line_text_spans(line)
                if not all_spans:
                    if any(_is_symbol_span(sp) for sp in exploded):
                        pending_bullet = True
                    continue
                if pending_bullet:
                    starts_bullet, pending_bullet = True, False
                for gi, spans in enumerate(_split_segments(all_spans, vertical)):
                    size, color, bold, italic, serif = _style_of(spans)
                    bbox = fitz.Rect(
                        min(sp["bbox"][0] for sp in spans), min(sp["bbox"][1] for sp in spans),
                        max(sp["bbox"][2] for sp in spans), max(sp["bbox"][3] for sp in spans),
                    )
                    text = "".join(sp["text"] for sp in spans)
                    bullet = bool(starts_bullet and gi == 0)
                    meta = {"size": size, "color": color, "bold": bold, "italic": italic, "serif": serif,
                            "vertical": vertical, "bbox": bbox, "text": text, "origin": spans[0]["origin"],
                            "bullet": bullet}
                    new_para = True
                    if prev_meta is not None and not bullet and gi == 0:
                        same_style = (
                            abs(prev_meta["size"] - size) <= 0.6
                            and prev_meta["bold"] == bold
                            and prev_meta["color"] == color
                            and prev_meta["vertical"] == vertical
                        )
                        idx = 0 if vertical else 1
                        gap = abs(meta["origin"][idx] - prev_meta["origin"][idx])
                        # cùng đường cơ sở (gap ~ 0) = ô khác cùng hàng => đoạn mới
                        new_para = not (same_style and size * 0.3 <= gap <= size * 1.75)
                    if new_para:
                        paras.append([])
                    paras[-1].append(meta)
                    prev_meta = meta

            for lines in paras:
                text = clean_text(" ".join(m["text"] for m in lines))
                if not should_translate(text):
                    continue
                rect = fitz.Rect(lines[0]["bbox"])
                for m in lines[1:]:
                    rect |= m["bbox"]
                dom = max(lines, key=lambda m: len(m["text"]))
                vertical = lines[0]["vertical"]
                if not _is_visible(page_pix, rect, scale, dom["color"], vertical):
                    continue  # chữ ẩn / bị che / ngoài trang
                u = Unit(
                    page=pno, rect=rect, line_rects=[fitz.Rect(m["bbox"]) for m in lines], text=text,
                    size=dom["size"], color=dom["color"], bold=dom["bold"], italic=dom["italic"],
                    serif=dom["serif"], vertical=vertical,
                    container=None if vertical else _find_container(rect, drawn),
                )
                u.starts_bullet = bool(lines[0]["bullet"])
                page_units.append(u)

        page_units = _merge_units(page_units)
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


def _free_right(u: Unit, others: List[Unit], limit: float) -> float:
    """Mép phải tối đa có thể mở rộng sang phải mà không đè chữ khác."""
    right = limit
    for o in others:
        if o is u or o.page != u.page or o.vertical:
            continue
        if o.rect.y1 > u.rect.y0 + 1 and o.rect.y0 < u.rect.y1 - 1 and o.rect.x0 >= u.rect.x1 - 0.5:
            right = min(right, o.rect.x0 - 3)
    return max(right, u.rect.x1)


def _free_bottom(u: Unit, others: List[Unit], limit: float) -> float:
    bottom = limit
    for o in others:
        if o is u or o.page != u.page or o.vertical:
            continue
        if o.rect.x1 > u.rect.x0 + 1 and o.rect.x0 < u.rect.x1 - 1 and o.rect.y0 >= u.rect.y1 - 0.5:
            bottom = min(bottom, o.rect.y0 - 1.5)
    return max(bottom, u.rect.y1)


def _free_up(u: Unit, others: List[Unit], limit: float) -> float:
    """Chữ dọc đọc từ dưới lên: y nhỏ nhất có thể mở rộng lên mà không đè chữ khác."""
    top = limit
    for o in others:
        if o is u or o.page != u.page:
            continue
        if o.rect.x1 > u.rect.x0 + 1 and o.rect.x0 < u.rect.x1 - 1 and o.rect.y1 <= u.rect.y0 + 0.5:
            top = max(top, o.rect.y1 + 3)
    return min(top, u.rect.y0)


def _target_box(u: Unit, page_rect: fitz.Rect, others: List[Unit]) -> fitz.Rect:
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
        x_left, x_right = r.x0, min(page_rect.x1 - margin, r.x1 + max(6.0, r.width * 0.06))
        y_bottom_limit = min(page_rect.y1 - margin, r.y1 + max(10.0, r.height * 0.6))

    if u.align == "left":
        right = max(r.x1, min(x_right, _free_right(u, others, x_right)))
        box = fitz.Rect(r.x0, r.y0, right, r.y1)
    elif u.align == "center":
        half = max(r.width / 2, min((x_right - x_left) / 2, (x_right - x_left) / 2))
        cx = (r.x0 + r.x1) / 2
        half = min(half, cx - x_left, x_right - cx) if c is not None else r.width / 2 * 1.15
        box = fitz.Rect(cx - half, r.y0, cx + half, r.y1)
    else:
        left = min(r.x0, max(x_left, r.x0 - r.width * 0.25))
        box = fitz.Rect(left, r.y0, r.x1, r.y1)
    box.y1 = max(box.y1, min(y_bottom_limit, _free_bottom(u, others, y_bottom_limit)))
    return box


def _fits(font: fitz.Font, box: fitz.Rect, text: str, size: float, lh: Optional[float], align: int) -> bool:
    tw = fitz.TextWriter(fitz.Rect(-5000, -5000, 5000, 5000))
    try:
        rest = tw.fill_textbox(box, text, font=font, fontsize=size, lineheight=lh, align=align, warn=False)
    except Exception:
        return False
    return not rest


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
            if u.vertical:
                top = _free_up(u, units, 14.0)
                W = max(u.rect.height, min(u.rect.y1 - top, u.rect.height * 1.8))
                H = u.rect.width + max(0.0, u.size * 0.2)
                local = fitz.Rect(u.rect.x0, u.rect.y1, u.rect.x0 + W, u.rect.y1 + H)
                tight_local = fitz.Rect(local.x0, local.y0, local.x1, local.y0 + u.rect.width + 0.5)
                size, box = _choose_size(font, local, u.dst, u.size, lh, align, tight=tight_local)
                _write(page, font, box, u.dst, size, lh, align, u.color,
                       morph=(fitz.Point(u.rect.x0, u.rect.y1), fitz.Matrix(90)))
            else:
                box0 = _target_box(u, page.rect, units)
                tight = fitz.Rect(box0.x0, box0.y0, box0.x1, min(box0.y1, u.rect.y1 + 0.5))
                size, box = _choose_size(font, box0, u.dst, u.size, lh, align, tight=tight)
                _write(page, font, box, u.dst, size, lh, align, u.color)
            stats["rendered"] += 1
            if size < u.size - 0.01:
                stats["shrunk"] += 1
            if box.height > (u.rect.height if u.vertical else _target_box(u, page.rect, units).height) + 0.5 and size <= max(4.0, u.size * 0.55) + 0.01:
                stats["overflow"] += 1
    return stats
