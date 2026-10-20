import os
import shutil
from pathlib import Path

DEFAULT_FONT_DIR = r"E:\Workspace\BookTranslatorHub\backend\storage\fonts"
WINDOWS_FONTS_DIR = r"C:\Windows\Fonts"

# Danh sách ưu tiên các font hỗ trợ 100% Tiếng Việt Unicode trên Windows
SANS_FONT_CANDIDATES = [
    "arial.ttf",
    "segoeui.ttf",
    "tahoma.ttf",
    "calibri.ttf",
    "verdana.ttf",
]

SERIF_FONT_CANDIDATES = [
    "times.ttf",
    "georgia.ttf",
    "palabi.ttf",
    "arial.ttf",
]


def _ensure_font_copied(candidates: list[str], target_path: Path) -> str | None:
    """Kiểm tra và copy font đầu tiên khả dụng từ C:\\Windows\\Fonts sang target_path."""
    if target_path.exists() and target_path.stat().st_size > 10_000:
        return str(target_path.resolve())

    target_path.parent.mkdir(parents=True, exist_ok=True)
    win_fonts = Path(WINDOWS_FONTS_DIR)

    for font_name in candidates:
        src = win_fonts / font_name
        if not src.exists():
            # Thử tìm không phân biệt hoa thường
            matches = list(win_fonts.glob(font_name))
            if matches:
                src = matches[0]
        if src.exists():
            try:
                shutil.copy2(src, target_path)
                return str(target_path.resolve())
            except Exception:
                # Nếu không copy được thì trả về trực tiếp đường dẫn gốc
                return str(src.resolve())

    return None


def get_vietnamese_font_path(font_dir: str = None, serif: bool = False) -> str:
    """
    Đảm bảo trả về đường dẫn tuyệt đối tới file font .ttf hỗ trợ đầy đủ 100% Tiếng Việt Unicode.
    Tự động copy từ C:\\Windows\\Fonts sang storage/fonts/vi_regular.ttf và storage/fonts/vi_serif.ttf.
    """
    base_dir = Path(font_dir) if font_dir else Path(DEFAULT_FONT_DIR)
    base_dir.mkdir(parents=True, exist_ok=True)

    regular_target = base_dir / "vi_regular.ttf"
    serif_target = base_dir / "vi_serif.ttf"

    regular_path = _ensure_font_copied(SANS_FONT_CANDIDATES, regular_target)
    serif_path = _ensure_font_copied(SERIF_FONT_CANDIDATES, serif_target)

    chosen = serif_path if serif and serif_path else regular_path
    if chosen and os.path.exists(chosen):
        return str(Path(chosen).resolve())

    # Fallback trực tiếp sang C:\Windows\Fonts nếu cần
    for candidate in SANS_FONT_CANDIDATES + SERIF_FONT_CANDIDATES:
        fallback = Path(WINDOWS_FONTS_DIR) / candidate
        if fallback.exists():
            return str(fallback.resolve())

    return str(regular_target.resolve())
