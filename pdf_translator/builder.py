"""PDF 版面重建：把译文按原排版写回，并生成双语对照版。

两种输出
--------
1. **译文替换版**（``build_replaced_pdf``）
   在原始 PDF 上精确清除每个文字块范围内的原文（保留图片、表格与线条），
   再按原位置、原字号、原颜色、原对齐方式写入译文，字号自适应缩放。

2. **双语对照版**（``build_bilingual_pdf``）
   新建加宽/加高的页面：一侧完整保留原页面，另一侧显示同一版面的译文，
   左右（或上下）段落位置严格对齐，便于逐段比对。

本模块自带一个轻量文本排版引擎（分词 → 断行 → 基线定位 → 字号自适应），
不依赖 ``insert_textbox`` 的隐式行为，因此对中英混排的控制更精确。
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from ._compat import fitz  # PyMuPDF
from .config import CJK_LANGS, Settings
from .extractor import DocumentLayout, PageLayout, TextBlock

# --------------------------------------------------------------------------- #
# 字体解析
# --------------------------------------------------------------------------- #

_WINDIR = os.environ.get("WINDIR") or r"C:\Windows"
_LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or ""

FONT_DIRS: list[Path] = [
    Path(_WINDIR) / "Fonts",
    Path(_LOCALAPPDATA) / "Microsoft" / "Windows" / "Fonts" if _LOCALAPPDATA else Path("/nonexistent"),
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
    Path("/System/Library/Fonts"),
    Path.home() / ".fonts",
    Path.home() / ".local" / "share" / "fonts",
]

#: 各语言优先使用的系统字体文件（按顺序命中即用）
CJK_FONT_CANDIDATES: dict[str, dict[str, list[str]]] = {
    "zh": {
        "sans": ["msyh.ttc", "msyh.ttf", "simhei.ttf", "Deng.ttf", "NotoSansCJKsc-Regular.otf",
                 "NotoSansSC-Regular.otf", "SourceHanSansSC-Regular.otf", "wqy-microhei.ttc"],
        "serif": ["simsun.ttc", "simfang.ttf", "NotoSerifCJKsc-Regular.otf",
                  "NotoSerifSC-Regular.otf", "SourceHanSerifSC-Regular.otf", "wqy-zenhei.ttc"],
        "bold": ["msyhbd.ttc", "simhei.ttf", "Dengb.ttf", "NotoSansCJKsc-Bold.otf",
                 "SourceHanSansSC-Bold.otf", "msyh.ttc"],
    },
    "zh-TW": {
        "sans": ["msjh.ttc", "msjh.ttf", "mingliu.ttc", "NotoSansCJKtc-Regular.otf", "PingFang.ttc"],
        "serif": ["mingliu.ttc", "simsun.ttc", "NotoSerifCJKtc-Regular.otf"],
        "bold": ["msjhbd.ttc", "msjh.ttc", "NotoSansCJKtc-Bold.otf"],
    },
    "ja": {
        "sans": ["meiryo.ttc", "YuGothM.ttc", "msgothic.ttc", "NotoSansCJKjp-Regular.otf",
                 "NotoSansJP-Regular.otf", "SourceHanSansJP-Regular.otf"],
        "serif": ["yumin.ttf", "msmincho.ttc", "NotoSerifCJKjp-Regular.otf"],
        "bold": ["meiryob.ttc", "YuGothB.ttc", "msgothic.ttc", "NotoSansCJKjp-Bold.otf"],
    },
    "ko": {
        "sans": ["malgun.ttf", "gulim.ttc", "NotoSansCJKkr-Regular.otf", "NotoSansKR-Regular.otf"],
        "serif": ["batang.ttc", "NotoSerifCJKkr-Regular.otf"],
        "bold": ["malgunbd.ttf", "malgun.ttf", "NotoSansCJKkr-Bold.otf"],
    },
}

#: 内置 Base-14 字体（无需嵌入，适合纯拉丁译文）
BASE14_SANS = "helv"
BASE14_SANS_BOLD = "hebo"
BASE14_SERIF = "tiro"
BASE14_SERIF_BOLD = "tibo"
BASE14_MONO = "cour"
BASE14_MONO_BOLD = "cobo"

_CJK_CHAR_RE = re.compile(
    r"[\u1100-\u11ff\u2e80-\u303f\u3040-\u30ff\u3130-\u318f\u3400-\u4dbf"
    r"\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef]"
)

#: 中文字体普遍缺失、但排版里常见的符号 → 可用的替代字符
_SUBSTITUTES: dict[str, str] = {
    "✓": "√", "✔": "√", "☑": "√", "✅": "√", "🗸": "√",
    "✗": "×", "✘": "×", "❌": "×",
    "☐": "□", "❑": "□",
    "−": "-", "–": "-", "‐": "-",
    "≤": "≤", "≥": "≥", "≠": "≠",
    "′": "'", "″": '"',
}

#: 小于这个码位的字符一律认为字体支持（ASCII / 拉丁 / 希腊 / 西里尔），省去逐字查询
_SAFE_CODEPOINT = 0x2000


def contains_cjk(text: str) -> bool:
    return bool(_CJK_CHAR_RE.search(text or ""))


@dataclass
class FontSpec:
    """一个可用的字体（内置名或字体文件）。"""

    name: str                      # 传给 PyMuPDF 的字体名/内置名
    file: str | None = None        # 字体文件路径（None 表示用内置 Base-14）
    display: str = ""              # 给人看的名字

    def load(self) -> "fitz.Font":
        if self.file:
            return fitz.Font(fontfile=self.file)
        return fitz.Font(self.name)


class FontResolver:
    """按目标语言与原文风格挑选合适的字体，并缓存 ``fitz.Font`` 对象。"""

    def __init__(self, target_lang: str = "zh") -> None:
        self.target_lang = target_lang
        self._file_cache: dict[tuple[str, bool], FontSpec | None] = {}
        self._font_cache: dict[tuple[str, str | None], "fitz.Font"] = {}
        self._glyph_support: dict[str, dict[int, bool]] = {}
        self._available: dict[str, Path] | None = None

    # ------------------------------------------------------------------ #
    def _supports(self, font: "fitz.Font", key: str, ch: str) -> bool:
        """字体是否含该字符的字形（按字体缓存结果）。"""
        code = ord(ch)
        if code < _SAFE_CODEPOINT:
            return True
        table = self._glyph_support.setdefault(key, {})
        known = table.get(code)
        if known is None:
            try:
                known = bool(font.has_glyph(code))
            except Exception:
                known = True
            table[code] = known
        return known

    def sanitise(self, text: str, font: "fitz.Font", key: str) -> str:
        """把目标字体渲染不出来的字符换成等价写法。

        译文里经常残留原文的行内数学符号（``𝑛`` ``𝒖`` ``𝜑`` 等 Unicode 数学字母），
        而中文字体（宋体/黑体）并不包含这些字形：``text_length`` 会给出一个假宽度，
        断行与定位随之算错，写出来还会缺字。这里按字体能力做一次规范化，
        ``𝑛`` → ``n``、``𝟏`` → ``1``，既保住了可读性，也让测量回到正轨。
        """
        if not text:
            return text
        if not any(ord(ch) >= _SAFE_CODEPOINT for ch in text):
            return text

        out: list[str] = []
        for ch in text:
            if self._supports(font, key, ch):
                out.append(ch)
                continue
            replacement = unicodedata.normalize("NFKC", ch)
            if replacement and replacement != ch and all(
                self._supports(font, key, c) for c in replacement
            ):
                out.append(replacement)
            else:
                out.append(_SUBSTITUTES.get(ch, ch))
        return "".join(out)

    # ------------------------------------------------------------------ #
    def _scan_fonts(self) -> dict[str, Path]:
        if self._available is not None:
            return self._available
        found: dict[str, Path] = {}
        for directory in FONT_DIRS:
            try:
                if not directory.is_dir():
                    continue
                for entry in directory.iterdir():
                    if entry.is_file() and entry.suffix.lower() in (".ttf", ".ttc", ".otf", ".otc"):
                        found.setdefault(entry.name.lower(), entry)
            except OSError:
                continue
        self._available = found
        return found

    def find_file(self, *filenames: str) -> Path | None:
        available = self._scan_fonts()
        for name in filenames:
            hit = available.get(name.lower())
            if hit is not None:
                return hit
        return None

    # ------------------------------------------------------------------ #
    def _cjk_spec(self, want_bold: bool, want_serif: bool) -> FontSpec | None:
        key = ("cjk", want_bold)
        if key in self._file_cache:
            return self._file_cache[key]
        table = CJK_FONT_CANDIDATES.get(self.target_lang) or CJK_FONT_CANDIDATES["zh"]
        order: list[str] = []
        if want_bold:
            order += table.get("bold", [])
        order += table.get("serif" if want_serif else "sans", [])
        order += table.get("sans", []) + table.get("serif", [])
        spec: FontSpec | None = None
        path = self.find_file(*order)
        if path is not None:
            try:
                fitz.Font(fontfile=str(path))  # 试加载，个别 TTC 可能不被支持
                digest = hashlib.md5(str(path).encode("utf-8")).hexdigest()[:8]
                spec = FontSpec(name=f"PDFT{digest}", file=str(path), display=path.name)
            except Exception:
                spec = None
        if spec is None:
            # 回退到 PyMuPDF 内置 CJK 字体
            builtin = "china-ts" if self.target_lang == "zh-TW" else {
                "ja": "japan-s", "ko": "korea-s",
            }.get(self.target_lang, "china-ss")
            try:
                fitz.Font(builtin)
                spec = FontSpec(name=builtin, file=None, display=f"内置 {builtin}")
            except Exception:
                spec = None
        self._file_cache[key] = spec
        return spec

    def _base14_spec(self, want_bold: bool, want_serif: bool, want_mono: bool) -> FontSpec:
        if want_mono:
            return FontSpec(BASE14_MONO_BOLD if want_bold else BASE14_MONO, None, "Courier")
        if want_serif:
            return FontSpec(BASE14_SERIF_BOLD if want_bold else BASE14_SERIF, None, "Times")
        return FontSpec(BASE14_SANS_BOLD if want_bold else BASE14_SANS, None, "Helvetica")

    def resolve(self, block: TextBlock, sample_text: str = "") -> FontSpec:
        """为某个文字块挑选字体。"""
        want_cjk_font = self.target_lang in CJK_LANGS or contains_cjk(sample_text or block.text)
        if want_cjk_font:
            spec = self._cjk_spec(block.is_bold, block.is_serif)
            if spec is not None:
                return spec
        return self._base14_spec(block.is_bold, block.is_serif, block.is_mono)

    def font_object(self, spec: FontSpec) -> "fitz.Font":
        key = (spec.name, spec.file)
        cached = self._font_cache.get(key)
        if cached is None:
            cached = spec.load()
            self._font_cache[key] = cached
        return cached


# --------------------------------------------------------------------------- #
# 轻量文本排版引擎
# --------------------------------------------------------------------------- #

_CJK_RANGES = (
    (0x1100, 0x11FF), (0x2E80, 0x303F), (0x3040, 0x30FF), (0x3130, 0x318F),
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xAC00, 0xD7AF), (0xF900, 0xFAFF),
    (0xFE30, 0xFE4F), (0xFF00, 0xFFEF),
)

#: 不能出现在行首的标点（避头尾）
_NO_LINE_START = set("，。、；：？！）】》」』”’%,.;:?!)]}>")
#: 不能出现在行尾的标点
_NO_LINE_END = set("（【《「『“‘([{<")


def _is_cjk_char(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def tokenize(text: str) -> list[str]:
    """把文本切成排版单元：CJK 逐字，拉丁按单词，空白单独成 token。"""
    tokens: list[str] = []
    buffer = ""
    for ch in text:
        if ch in "\r\n":
            if buffer:
                tokens.append(buffer)
                buffer = ""
            tokens.append("\n")
        elif _is_cjk_char(ch):
            if buffer:
                tokens.append(buffer)
                buffer = ""
            tokens.append(ch)
        elif ch.isspace():
            if buffer:
                tokens.append(buffer)
                buffer = ""
            tokens.append(" ")
        else:
            buffer += ch
    if buffer:
        tokens.append(buffer)
    return tokens


def wrap_text(
    text: str, font: "fitz.Font", fontsize: float, width: float
) -> list[str]:
    """按可用宽度断行，返回每行的字符串（不含换行符）。"""
    if width <= 1 or fontsize <= 0:
        return [text]

    def measure(s: str) -> float:
        try:
            return font.text_length(s, fontsize=fontsize)
        except Exception:
            return len(s) * fontsize * 0.5

    lines: list[str] = []
    for paragraph in text.split("\n"):
        tokens = tokenize(paragraph)
        current = ""
        current_width = 0.0

        for token in tokens:
            token_width = measure(token)
            if token == " ":
                if current and current_width + token_width <= width:
                    current += token
                    current_width += token_width
                continue

            # 超长不可断的 token（长 URL / 长英文单词）强制按字符切分
            if token_width > width and len(token) > 1:
                for ch in token:
                    ch_width = measure(ch)
                    if current and current_width + ch_width > width:
                        lines.append(current.rstrip())
                        current, current_width = "", 0.0
                    current += ch
                    current_width += ch_width
                continue

            if current and current_width + token_width > width:
                # 避头尾：标点不该出现在行首，就把它挤在上一行末尾
                if token and token[0] in _NO_LINE_START:
                    current += token[0]
                    lines.append(current.rstrip())
                    current, current_width = token[1:], measure(token[1:])
                    continue
                stripped = current.rstrip()
                if stripped and stripped[-1] in _NO_LINE_END and len(stripped) > 1:
                    lines.append(stripped[:-1])
                    current = stripped[-1] + token
                    current_width = measure(current)
                    continue
                lines.append(stripped)
                current, current_width = token, token_width
                continue

            current += token
            current_width += token_width

        if current.strip():
            lines.append(current.rstrip())
        elif not lines:
            lines.append("")

    return lines or [""]


def measure_block_height(lines: Sequence[str], fontsize: float, line_height: float) -> float:
    return len(lines) * line_height


# --------------------------------------------------------------------------- #
# 写文字
# --------------------------------------------------------------------------- #

ALIGN_LEFT, ALIGN_CENTER, ALIGN_RIGHT, ALIGN_JUSTIFY = 0, 1, 2, 3


def _line_origin(
    line: str, font: "fitz.Font", fontsize: float, rect: "fitz.Rect", align: int
) -> float:
    """返回该行的起始 x 坐标。"""
    try:
        width = font.text_length(line, fontsize=fontsize)
    except Exception:
        width = len(line) * fontsize * 0.5
    if align == ALIGN_CENTER:
        return rect.x0 + max(0.0, (rect.width - width) / 2)
    if align == ALIGN_RIGHT:
        return rect.x1 - width
    return rect.x0


def draw_text_block(
    page: "fitz.Page",
    rect: "fitz.Rect",
    text: str,
    font: "fitz.Font",
    fontsize: float,
    line_height: float,
    color: tuple[float, float, float],
    align: int = ALIGN_LEFT,
) -> None:
    """在给定矩形内写入文本（调用前需保证已经量好字号）。"""
    lines = wrap_text(text, font, fontsize, rect.width)
    ascender = getattr(font, "ascender", 0.9) or 0.9
    descender = getattr(font, "descender", -0.21) or -0.21
    text_height = fontsize * (ascender - descender)
    writer = fitz.TextWriter(page.rect, color=color)
    wrote = False

    baseline = rect.y0 + max(0.0, (line_height - text_height) / 2) + fontsize * ascender
    for line in lines:
        if not line.strip():
            baseline += line_height
            continue
        x = _line_origin(line, font, fontsize, rect, align)
        writer.append(fitz.Point(x, baseline), line, font=font, fontsize=fontsize)
        wrote = True
        baseline += line_height

    if not wrote:
        return
    try:
        writer.write_text(page, color=color, render_mode=0, overlay=True)
    except TypeError:  # 老版本 PyMuPDF 没有 render_mode 参数
        writer.write_text(page, color=color)


# --------------------------------------------------------------------------- #
# 字号自适应
# --------------------------------------------------------------------------- #

#: 字号缩到这个值就不再缩了 —— 再小就没法读，宁可让它略微溢出
_HARD_MIN_FONT_SIZE = 5.0


@dataclass
class PlacementPlan:
    rect: "fitz.Rect"
    fontsize: float
    line_height: float
    lines: list[str]
    overflow: bool = False


def plan_placement(
    block: TextBlock,
    text: str,
    font: "fitz.Font",
    settings: Settings,
    *,
    max_bottom: float,
    page_width: float,
    max_left: float | None = None,
    max_right: float | None = None,
) -> PlacementPlan:
    """在原文位置附近，为译文找一个放得下的字号与矩形。"""
    base_size = max(1.0, block.font_size)
    rect = fitz.Rect(block.bbox)
    if rect.width < 4:
        rect.x1 = rect.x0 + max(20.0, page_width - rect.x0 - 24)
    # 单行文本（标题、页眉、列表项、图表说明）允许横向扩展，避免被挤成多行小字
    if max_left is not None and max_left < rect.x0:
        rect.x0 = max_left
    if max_right is not None and max_right > rect.x1:
        rect.x1 = max_right
    original_line_height = (
        block.height / block.line_count if block.line_count > 1 else base_size * 1.22
    )
    original_line_height = max(original_line_height, base_size * 1.02)

    min_size = max(4.0, base_size * settings.min_font_scale)
    max_size = base_size * settings.max_font_scale
    hard_min = min(_HARD_MIN_FONT_SIZE, min_size)

    # 允许向下扩展的最大高度
    expand_limit = rect.y1
    if settings.allow_expand_down and max_bottom > rect.y1:
        expand_limit = max_bottom
    max_height = max(rect.height, expand_limit - rect.y0)

    def build(size: float) -> PlacementPlan | None:
        line_height = original_line_height * (size / base_size) * settings.line_spacing
        lines = wrap_text(text, font, size, rect.width)
        needed = len(lines) * line_height
        if needed > max_height + 0.6:
            return None
        return PlacementPlan(
            fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y0 + max(rect.height, needed)),
            size, line_height, lines,
        )

    # ① 从原字号（可放大到 max_font_scale）往下找第一个放得下的字号
    size = max_size
    step = max(0.25, (max_size - min_size) / 24.0) if max_size > min_size else 0.25
    while size >= min_size - 1e-6:
        plan = build(size)
        if plan is not None:
            return plan
        size -= step

    # ② 到了 min_font_scale 还放不下：继续往下缩，换取「不压到下一段」。
    #    溢出会把两段文字叠在一起，比字小一点难看得多。
    size = min_size
    while size > hard_min + 1e-6:
        size = max(hard_min, size * 0.92)
        plan = build(size)
        if plan is not None:
            return plan

    # ③ 极端情况（译文比原文长好几倍）：按硬下限排并允许溢出，至少不丢内容
    line_height = original_line_height * (hard_min / base_size) * settings.line_spacing
    lines = wrap_text(text, font, hard_min, rect.width)
    needed = len(lines) * line_height
    return PlacementPlan(
        fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y0 + max(rect.height, needed)),
        hard_min,
        line_height,
        lines,
        overflow=needed > max_height + 0.6,
    )


# --------------------------------------------------------------------------- #
# 生成器
# --------------------------------------------------------------------------- #


class PdfBuilder:
    """把翻译结果写回 PDF。"""

    def __init__(
        self,
        settings: Settings,
        *,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.log = log or (lambda _m: None)
        self.fonts = FontResolver(settings.target_lang)
        self.stats = {"placed": 0, "shrunk": 0, "overflow": 0, "skipped": 0}

    # ------------------------------------------------------------------ #
    def _translation_of(self, block: TextBlock) -> str:
        return (block.translation or "").strip() or block.text

    def _page_margins(self, page_layout: PageLayout) -> tuple[float, float]:
        """本页「版心」的左右边界（由所有文字块的外接范围推断）。"""
        if not page_layout.blocks:
            return 24.0, max(24.0, page_layout.width - 24.0)
        left = min(b.bbox[0] for b in page_layout.blocks)
        right = max(b.bbox[2] for b in page_layout.blocks)
        if right - left < 40:
            return 24.0, max(24.0, page_layout.width - 24.0)
        return left, right

    def _horizontal_limits(
        self,
        page_layout: PageLayout,
        block: TextBlock,
        ordered: Sequence[TextBlock],
        left_margin: float,
        right_margin: float,
    ) -> tuple[float, float]:
        """在**同一水平带**上，这个块能向左右扩展到多远（避开图片/表格/其它文字块）。"""
        limit_left = left_margin
        limit_right = right_margin
        y0, y1 = block.bbox[1], block.bbox[3]

        for region in page_layout.skip_regions:
            rx0, ry0, rx1, ry1 = region.bbox
            if ry1 <= y0 + 1 or ry0 >= y1 - 1:
                continue  # 纵向不重叠，不构成障碍
            if rx0 >= block.bbox[2] - 1:
                limit_right = min(limit_right, rx0 - 2)
            elif rx1 <= block.bbox[0] + 1:
                limit_left = max(limit_left, rx1 + 2)

        for other in ordered:
            if other is block:
                continue
            if other.bbox[3] <= y0 + 1 or other.bbox[1] >= y1 - 1:
                continue
            if other.bbox[0] >= block.bbox[2] - 1:
                limit_right = min(limit_right, other.bbox[0] - 2)
            elif other.bbox[2] <= block.bbox[0] + 1:
                limit_left = max(limit_left, other.bbox[2] + 2)

        return limit_left, max(block.bbox[2], limit_right)

    def _max_bottom_for(
        self, page_layout: PageLayout, block: TextBlock, ordered: Sequence[TextBlock]
    ) -> float:
        """块下方可用于扩展的边界：下方文字块 / 跳过区域 / 页底边距。

        判据用的是「顶部在本块顶部之下」，而不是「顶部在本块底部之下」——
        PyMuPDF 给出的块外框常常互相咬合，若要求前者严格在后者下面，
        很多实际存在的下方内容会被漏判，译文就会直接压上去。
        """
        limit = page_layout.height - 20.0
        top = block.bbox[1]
        x0, x1 = block.bbox[0], block.bbox[2]

        for region in page_layout.skip_regions:
            rx0, ry0, rx1, _ry1 = region.bbox
            if rx1 > x0 + 1 and rx0 < x1 - 1 and ry0 > top + 2:
                limit = min(limit, ry0 - 2)
        for other in ordered:
            if other is block:
                continue
            if other.bbox[1] <= top + 2:  # 在本块上方或几乎同行
                continue
            if other.bbox[2] > x0 + 1 and other.bbox[0] < x1 - 1:  # 横向交叠
                limit = min(limit, other.bbox[1] - 2)

        return max(block.bbox[3], limit)

    # ------------------------------------------------------------------ #
    def _cover_text_rules(
        self,
        page: "fitz.Page",
        blocks: Sequence[TextBlock],
        offset: tuple[float, float] = (0.0, 0.0),
    ) -> int:
        """盖掉落在待重写块里的细横线（下划线、分数线、强调线）。

        这些线是**独立的矢量对象**，不会跟着译文走：原文那一行有下划线，
        译文重排后长度变了、行数也可能变了，线却还留在原处，看起来就是
        「下划线没对齐」。译文本身已经把内容表达完整，直接把线盖掉最干净。

        只处理**完全落在**某个待重写块内部的线，所以表格线、图片边框、
        页眉分隔线都不受影响（表格区域整体不参与重写）。
        """
        dx, dy = offset
        regions: list["fitz.Rect"] = []
        for block in blocks:
            rect = fitz.Rect(block.bbox)
            if dx or dy:
                rect = rect + (dx, dy, dx, dy)
            regions.append(rect)
        if not regions:
            return 0

        try:
            drawings = page.get_drawings()
        except Exception:
            return 0

        probe = self._background_probe(page)
        covered = 0
        for drawing in drawings:
            for item in drawing["items"]:
                rect: "fitz.Rect | None" = None
                if item[0] == "l":  # 线段
                    p1, p2 = item[1], item[2]
                    if abs(p1.y - p2.y) < 1.5 and abs(p2.x - p1.x) > 3.0:
                        mid = (p1.y + p2.y) / 2
                        rect = fitz.Rect(min(p1.x, p2.x), mid - 0.8,
                                         max(p1.x, p2.x), mid + 0.8)
                elif item[0] == "re":  # 细长矩形
                    r = item[1]
                    if r.height < 2.0 and r.width > 3.0:
                        rect = fitz.Rect(r.x0, r.y0 - 0.3, r.x1, r.y1 + 0.3)
                if rect is None or rect.is_empty:
                    continue
                if not any(region.contains(rect) for region in regions):
                    continue
                page.draw_rect(rect, color=None, fill=probe(rect), overlay=True)
                covered += 1
        return covered

    # ------------------------------------------------------------------ #
    @staticmethod
    def _background_probe(page: "fitz.Page"):
        """整页渲染一次，返回「按页面坐标取背景色」的函数。

        这样盖掉下划线时用的是纸张本身的底色（多数是白色，也可能是米色、
        浅灰），不会在非白底页面上留下显眼的白道子。
        """
        try:
            pix = page.get_pixmap(dpi=72)  # 1pt ≈ 1px
        except Exception:
            return lambda _rect: (1.0, 1.0, 1.0)

        width, height = pix.width, pix.height
        origin_x, origin_y = page.rect.x0, page.rect.y0

        def probe(rect: "fitz.Rect") -> tuple[float, float, float]:
            # 取线条正下方一点，那里通常是空白
            x = int(rect.x0 + min(2.0, rect.width / 2) - origin_x)
            y = int(rect.y1 + 1.5 - origin_y)
            if 0 <= x < width and 0 <= y < height:
                try:
                    r, g, b = pix.pixel(x, y)[:3]
                    return (r / 255.0, g / 255.0, b / 255.0)
                except Exception:
                    pass
            return (1.0, 1.0, 1.0)

        return probe

    # ------------------------------------------------------------------ #
    def _erase_blocks(
        self, page: "fitz.Page", blocks: Sequence[TextBlock], offset: tuple[float, float] = (0.0, 0.0)
    ) -> None:
        mode = self.settings.replace_mode
        dx, dy = offset
        if mode == "redact":
            for block in blocks:
                rect = fitz.Rect(block.bbox)
                rect.x0 -= 1.0
                rect.y0 -= 1.0
                rect.x1 += 1.0
                rect.y1 += 1.0
                if dx or dy:
                    rect = rect + (dx, dy, dx, dy)
                page.add_redact_annot(rect)
            try:
                kwargs: dict[str, object] = {"images": fitz.PDF_REDACT_IMAGE_NONE}
                if hasattr(fitz, "PDF_REDACT_LINE_ART_NONE"):
                    kwargs["graphics"] = fitz.PDF_REDACT_LINE_ART_NONE
                if hasattr(fitz, "PDF_REDACT_TEXT_REMOVE"):
                    kwargs["text"] = fitz.PDF_REDACT_TEXT_REMOVE
                page.apply_redactions(**kwargs)
            except TypeError:
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        else:  # cover：用白底矩形遮盖
            for block in blocks:
                rect = fitz.Rect(block.bbox)
                rect.x0 -= 1.0
                rect.y0 -= 1.0
                rect.x1 += 1.0
                rect.y1 += 1.0
                if dx or dy:
                    rect = rect + (dx, dy, dx, dy)
                page.draw_rect(rect, color=None, fill=(1, 1, 1), overlay=True)

    # ------------------------------------------------------------------ #
    def _rewritable(self, page_layout: PageLayout) -> list[TextBlock]:
        """只挑出**真的需要重写**的文字块。

        译文为空、或译文与原文完全相同时（已是目标语言 / 纯数字 / 翻译失败保留了原文），
        保持原始文字对象不动 —— 这样能最大限度保住原文档的字体渲染质量。
        """
        result: list[TextBlock] = []
        for block in page_layout.blocks:
            translated = (block.translation or "").strip()
            if not translated:
                continue
            if translated == (block.text or "").strip():
                continue
            result.append(block)
        return result

    # ------------------------------------------------------------------ #
    def _draw_all(
        self,
        page: "fitz.Page",
        page_layout: PageLayout,
        blocks: Sequence[TextBlock],
        offset: tuple[float, float] = (0.0, 0.0),
    ) -> None:
        dx, dy = offset
        # 计算可扩展边界时要考虑页面上所有块，而不只是待重写的块
        all_ordered = sorted(page_layout.blocks, key=lambda b: (round(b.bbox[1], 1), b.bbox[0]))
        left_margin, right_margin = self._page_margins(page_layout)
        for block in sorted(blocks, key=lambda b: (round(b.bbox[1], 1), b.bbox[0])):
            text = self._translation_of(block)
            if not text.strip():
                self.stats["skipped"] += 1
                continue

            spec = self.fonts.resolve(block, text)
            font = self.fonts.font_object(spec)
            # 目标字体渲染不出来的字符先规范化，否则测量与定位都会算错
            text = self.fonts.sanitise(text, font, spec.name)
            if not text.strip():
                self.stats["skipped"] += 1
                continue
            max_bottom = self._max_bottom_for(page_layout, block, all_ordered)

            # 单行块（标题 / 页眉 / 列表项 / 图表说明）允许横向借用留白
            max_left = max_right = None
            if block.line_count <= 1:
                limit_left, limit_right = self._horizontal_limits(
                    page_layout, block, all_ordered, left_margin, right_margin
                )
                if block.align == ALIGN_CENTER:
                    # 居中块必须**对称**扩展，否则会偏离原来的中心位置
                    center_x = (block.bbox[0] + block.bbox[2]) / 2
                    half = min(center_x - limit_left, limit_right - center_x)
                    if half > block.width / 2 + 2:
                        max_left, max_right = center_x - half, center_x + half
                elif limit_right > block.bbox[2] + 2:
                    max_right = limit_right
                    if limit_left < block.bbox[0] - 2:
                        max_left = limit_left

            plan = plan_placement(
                block, text, font, self.settings,
                max_bottom=max_bottom, page_width=page_layout.width,
                max_left=max_left, max_right=max_right,
            )

            if plan.fontsize < block.font_size * 0.98:
                self.stats["shrunk"] += 1
            if plan.overflow:
                self.stats["overflow"] += 1
                self.log(f"· 第 {block.page_index + 1} 页某段译文较长，已缩至最小字号")

            rect = plan.rect
            if dx or dy:
                rect = rect + (dx, dy, dx, dy)
            color = block.rgb()
            draw_text_block(
                page, rect, text, font, plan.fontsize, plan.line_height,
                color, block.align,
            )
            self.stats["placed"] += 1

    # ------------------------------------------------------------------ #
    def build_replaced_pdf(
        self,
        layout: DocumentLayout,
        output_path: str | Path,
        *,
        progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        """输出「译文替换版」：直接改写原文页面。"""
        progress = progress or (lambda _d, _t: None)
        doc = fitz.open(layout.path)
        try:
            total = len(layout.pages)
            for done, page_layout in enumerate(layout.pages, start=1):
                blocks = self._rewritable(page_layout)
                if blocks:
                    page = doc[page_layout.index]
                    try:
                        self._erase_blocks(page, blocks)
                        self._cover_text_rules(page, blocks)
                        self._draw_all(page, page_layout, blocks)
                    except Exception as exc:
                        self.log(f"✗ 第 {page_layout.index + 1} 页重建失败：{exc}")
                progress(done, total)

            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            self._save(doc, out)
            return out
        finally:
            doc.close()

    # ------------------------------------------------------------------ #
    def build_bilingual_pdf(
        self,
        layout: DocumentLayout,
        output_path: str | Path,
        *,
        progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        """输出「双语对照版」：一侧原文，另一侧对应译文。"""
        progress = progress or (lambda _d, _t: None)
        source = fitz.open(layout.path)
        out = fitz.open()
        try:
            total = len(layout.pages)
            for done, page_layout in enumerate(layout.pages, start=1):
                try:
                    self._append_bilingual_page(out, source, page_layout)
                except Exception as exc:
                    self.log(f"✗ 第 {page_layout.index + 1} 页双语排版失败：{exc}")
                    self._append_plain_pair(out, source, page_layout)
                progress(done, total)

            target = Path(output_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._save(out, target)
            return target
        finally:
            source.close()
            out.close()

    # ------------------------------------------------------------------ #
    def _append_bilingual_page(
        self, out: "fitz.Document", source: "fitz.Document", page_layout: PageLayout
    ) -> None:
        width, height = page_layout.width, page_layout.height
        gap = self.settings.bilingual_gap
        vertical = self.settings.bilingual_split == "vertical"

        if page_layout.rotation % 360 != 0:
            raise ValueError("页面带有旋转，改用原页+译页的退化对照模式")

        if vertical:
            new_page = out.new_page(width=width * 2 + gap, height=height)
            left = fitz.Rect(0, 0, width, height)
            right = fitz.Rect(width + gap, 0, width * 2 + gap, height)
        else:
            new_page = out.new_page(width=width, height=height * 2 + gap)
            left = fitz.Rect(0, 0, width, height)
            right = fitz.Rect(0, height + gap, width, height * 2 + gap)

        new_page.show_pdf_page(left, source, page_layout.index)
        new_page.show_pdf_page(right, source, page_layout.index)
        offset = (right.x0, right.y0)

        blocks = self._rewritable(page_layout)
        if blocks:
            self._erase_blocks(new_page, blocks, offset)
            self._cover_text_rules(new_page, blocks, offset)
            self._draw_all(new_page, page_layout, blocks, offset)

    # ------------------------------------------------------------------ #
    def _append_plain_pair(
        self, out: "fitz.Document", source: "fitz.Document", page_layout: PageLayout
    ) -> None:
        """退化模式：一页原文 + 一页纯译文，保证内容不丢。"""
        width, height = page_layout.width, page_layout.height
        original = out.new_page(width=width, height=height)
        original.show_pdf_page(fitz.Rect(0, 0, width, height), source, page_layout.index)

        translated = out.new_page(width=width, height=height)
        blocks = self._rewritable(page_layout)
        if blocks:
            blank_layout = PageLayout(
                index=page_layout.index, width=width, height=height,
                rotation=0, blocks=page_layout.blocks, skip_regions=[],
            )
            self._draw_all(translated, blank_layout, blocks)

    # ------------------------------------------------------------------ #
    def _save(self, doc: "fitz.Document", path: Path) -> None:
        try:
            doc.subset_fonts()
        except Exception:
            pass
        doc.save(
            str(path),
            garbage=3,
            deflate=True,
            clean=False,
        )


__all__ = [
    "PdfBuilder",
    "FontResolver",
    "FontSpec",
    "wrap_text",
    "tokenize",
    "draw_text_block",
    "plan_placement",
    "contains_cjk",
]
