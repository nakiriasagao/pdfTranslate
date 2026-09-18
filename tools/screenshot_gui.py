"""给图形界面截图，用来确认布局（尤其是小屏幕上按钮是否可见）。

    python tools/screenshot_gui.py                      # 输出 gui_screenshot.png
    python tools/screenshot_gui.py out.png 780x470      # 指定尺寸

窗口会短暂显示在最前面（约 1 秒），截完自动关闭。
"""

from __future__ import annotations

import ctypes
import struct
import sys
import zlib
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------- #
# 用 GDI 抓取屏幕上的一块区域，然后手搓 PNG（不依赖 Pillow）
# --------------------------------------------------------------------------- #
class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def grab_screen(left: int, top: int, width: int, height: int) -> bytes:
    """返回 RGB 原始像素（每像素 3 字节）。"""
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    desktop = user32.GetDC(0)
    memdc = gdi32.CreateCompatibleDC(desktop)
    bitmap = gdi32.CreateCompatibleBitmap(desktop, width, height)
    gdi32.SelectObject(memdc, bitmap)
    # SRCCOPY = 0x00CC0020
    gdi32.BitBlt(memdc, 0, 0, width, height, desktop, left, top, 0x00CC0020)

    header = _BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    header.biWidth = width
    header.biHeight = -height  # 负数 = 自上而下
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = 0

    buffer = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(memdc, bitmap, 0, height, buffer, ctypes.byref(header), 0)

    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(0, desktop)

    raw = buffer.raw
    rgb = bytearray(width * height * 3)
    for index in range(width * height):
        b, g, r = raw[index * 4], raw[index * 4 + 1], raw[index * 4 + 2]
        rgb[index * 3] = r
        rgb[index * 3 + 1] = g
        rgb[index * 3 + 2] = b
    return bytes(rgb)


def write_png(path: Path, width: int, height: int, rgb: bytes) -> None:
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)  # filter type 0
        raw.extend(rgb[y * stride : (y + 1) * stride])

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


# --------------------------------------------------------------------------- #
def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "gui_screenshot.png"
    size = sys.argv[2] if len(sys.argv) > 2 else "auto"
    page = int(sys.argv[3]) if len(sys.argv) > 3 else 0

    from pdf_translator.gui import PdfTranslatorApp

    app = PdfTranslatorApp()
    if size and size.lower() != "auto":  # auto = 用程序自己算出的自然尺寸
        app.geometry(size)
    if page:
        app.notebook.select(page)
    app.attributes("-topmost", True)
    app.update()
    app.lift()
    app.update()
    for _ in range(12):
        app.update()
        app.after(30)

    left, top = app.winfo_rootx(), app.winfo_rooty()
    width, height = app.winfo_width(), app.winfo_height()
    print(f"geometry={app.winfo_geometry()}")
    print(f"客户区 rootxy=({left},{top}) size={width}x{height}")
    print(f"屏幕 {app.winfo_screenwidth()}x{app.winfo_screenheight()}, "
          f"缩放 {app.tk.call('tk', 'scaling')}")
    button = app.start_button
    btn_top = button.winfo_rooty() - top
    print(f"开始翻译按钮：窗口内 top={btn_top} height={button.winfo_height()} "
          f"→ bottom={btn_top + button.winfo_height()}  (窗口高 {height})")

    # Tk 的 rootx/rooty 不含标题栏与边框，多抓一点以免漏掉边缘
    margin = 40
    grab_left = max(0, left - 8)
    grab_top = max(0, top - margin)
    grab_w = min(width + 16, app.winfo_screenwidth() - grab_left)
    grab_h = min(height + margin + 8, app.winfo_screenheight() - grab_top)
    print(f"抓取区域 ({grab_left},{grab_top}) {grab_w}x{grab_h}")

    rgb = grab_screen(grab_left, grab_top, grab_w, grab_h)
    write_png(out, grab_w, grab_h, rgb)
    app.destroy()
    print(f"已保存截图：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
