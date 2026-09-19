"""PDF 版面提取：拿到可翻译的文字块，并标出图片 / 表格等需要跳过的区域。

这里的产出（:class:`DocumentLayout`）是后续「翻译」和「重建排版」的唯一输入，
因此所有坐标都统一采用 PDF 的**未旋转页面坐标系**（PyMuPDF ``get_text`` 的
坐标系），单位是 point(1/72 inch)。
"""

from __future__ import annotations

import io
import re
import statistics
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ._compat import fitz  # PyMuPDF

# --------------------------------------------------------------------------- #
# PyMuPDF 自带的消息转发
# --------------------------------------------------------------------------- #


class _PyMuPDFMessageRouter(io.TextIOBase):
    """PyMuPDF 会直接把提示/警告 ``print`` 出去，这里把它们接进我们的日志。

    像「Consider using the pymupdf_layout package…」这种纯广告式提示会被过滤掉，
    真正的错误信息则加前缀转发，方便排查。
    """

    _NOISE = ("Consider using the pymupdf_layout package",)

    def __init__(self) -> None:
        super().__init__()
        self.handler: Callable[[str], None] = lambda _m: None
        self._buffer = ""

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"

    def writable(self) -> bool:
        return True

    def write(self, text: object) -> int:  # type: ignore[override]
        if not isinstance(text, str):
            return 0
        self._buffer += text
        while "\n" in self._buffer:
            line, _, self._buffer = self._buffer.partition("\n")
            line = line.strip()
            if line and not any(noise in line for noise in self._NOISE):
                try:
                    self.handler(f"[PyMuPDF] {line}")
                except Exception:
                    pass
        return len(text)

    def flush(self) -> None:
        return None


_ROUTER: _PyMuPDFMessageRouter | None = None


def route_pymupdf_messages(handler: Callable[[str], None]) -> None:
    """把 PyMuPDF 的全局消息出口接到 ``handler``（幂等，多实例共用）。"""
    global _ROUTER
    try:
        import pymupdf
    except ImportError:  # 老版本只有 fitz
        return
    if _ROUTER is None:
        router = _PyMuPDFMessageRouter()
        try:
            pymupdf.set_messages(stream=router)
        except Exception:
            return
        _ROUTER = router
    _ROUTER.handler = handler

# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

#: 字体 flags 位（PyMuPDF）
FLAG_SUPERSCRIPT = 1
FLAG_ITALIC = 2
FLAG_SERIF = 4
FLAG_MONO = 8
FLAG_BOLD = 16


#: 图题 / 表题：以 Fig. / Figure / Table / 图 / 表 + 编号 + 分隔符开头。
#: 必须有分隔符（`.` `:` `：` `、`）才算，否则正文里的引用句
#: 「Figure 7 further compares…」会被误当成图题。
_CAPTION_RE = re.compile(
    r"^\s*(?:fig(?:ure)?|tab(?:le)?|chart|algorithm|listing|图|表|图表)"
    r"\s*\.?\s*\d+\s*[a-z]?\s*[.:：、]",
    re.I,
)


def is_caption(text: str) -> bool:
    """判断一个文字块是不是图题/表题。"""
    return bool(_CAPTION_RE.match(text or ""))


def looks_like_prose(text: str) -> bool:
    """判断一个文字块是不是**成段的正文**。

    表格单元格和图内标签都是短片段；一段带好几个句号的完整文字，几乎不可能
    是它们。``find_tables`` 与 ``cluster_drawings`` 的外接矩形经常把紧贴表格、
    图形的正文段落圈进去，靠这条把它们放出来正常翻译。
    """
    stripped = (text or "").strip()
    if len(stripped) < 150:
        return False
    if len(_WORD_RE.findall(stripped)) < 15:
        return False
    return stripped.count(".") >= 2


# --------------------------------------------------------------------------- #
# 公式识别
# --------------------------------------------------------------------------- #

#: 数学排版常用字体（CambriaMath、LibertineMathMI、CMSY…）
_MATH_FONT_RE = re.compile(r"(math|symbol|cmmi|cmsy|cmsl|stix|euclid|mtmi)", re.I)

#: 数学运算符与括号
_MATH_SYMBOLS = set(
    "=+−-×÷±∓∑∏∫∮√∛≤≥≠≈≡≃∼∝∈∉∋⊂⊆⊃⊇∪∩∧∨¬∀∃∄∇∂∞→←↔⇒⇔∴∵⊥∥∠°′″^_{}[]()|/\\"
)

#: 「强」数学符号：出现它们基本可以断定这里在讲数学，而不是普通正文里的标点。
#: 特意排除了 = ( ) [ ] | / ^ _ 这些日常也会用到的字符。
_STRONG_MATH = set(
    "∈∉∋⊂⊆⊃⊇∪∩∧∨¬∀∃∄∇∂∞→←↔⇒⇔∴∵⊥∥∠∑∏∫∮√∛≤≥≠≈≡≃∼∝±∓×÷°′″"
)

#: 至少 3 个字母的连续英文串，用来区分「自然语言」和「变量名」
_WORD_RE = re.compile(r"[A-Za-z]{3,}")


def looks_like_formula(text: str, font_name: str = "") -> bool:
    """判断一个文字块是不是**数学内容**（原样保留，不翻译）。

    三种情况都算：
      * ``φ0: u4 u0 u5 u1 u3 u2`` —— 整块公式，凑不出正常英文单词
      * ``Δ = 2`` / ``n= 9 e= 11`` —— 短小的变量表达式
      * ``(1) For each vertex u ∈ VQ , L(F(u)) = LQ(u) …`` —— 数学符号密集的
        定义/定理。这类文字虽然夹着英文，但翻译后中文与符号混排、上下标丢失，
        格式反而更乱，保持原样更稳妥。

    区分的关键是**强数学符号**（``∈ ≤ ∑ →`` 等）：普通正文里的 ``= ( )``
    不算数，否则会把一整段正常文字误判成公式。
    """
    stripped = (text or "").strip()
    if not stripped or len(stripped) > 400:
        return False

    words = _WORD_RE.findall(stripped)
    letters = sum(1 for ch in stripped if ch.isalpha())
    digits = sum(1 for ch in stripped if ch.isdigit())
    symbols = sum(1 for ch in stripped if ch in _MATH_SYMBOLS)
    strong = sum(1 for ch in stripped if ch in _STRONG_MATH)

    # ① 强数学符号反复出现 → 数学内容
    if strong >= 2:
        return True
    # ② 通篇没有正常英文单词，只有零散变量和符号 → 公式行
    if not words and (letters + digits) >= 2 and symbols >= 1:
        return True
    # ③ 数学字体 + 短文本，且凑不出多少英文单词
    if _MATH_FONT_RE.search(font_name or "") and len(words) < 3 and len(stripped) <= 160:
        return True
    return False


@dataclass
class TextBlock:
    """一个可翻译的文字块（通常是自然段）。"""

    block_id: str
    page_index: int
    bbox: tuple[float, float, float, float]  # x0, y0, x1, y1（未旋转坐标系）
    text: str
    font_size: float = 11.0
    font_name: str = ""
    color: int = 0
    flags: int = 0
    align: int = 0                 # 0=左 1=居中 2=右 3=两端
    line_count: int = 1
    is_header_footer: bool = False
    translation: str = ""          # 由翻译阶段回填

    # ---------------- 便捷属性 ---------------- #
    @property
    def rect(self) -> fitz.Rect:
        return fitz.Rect(*self.bbox)

    @property
    def width(self) -> float:
        return max(0.0, self.bbox[2] - self.bbox[0])

    @property
    def height(self) -> float:
        return max(0.0, self.bbox[3] - self.bbox[1])

    @property
    def is_bold(self) -> bool:
        return bool(self.flags & FLAG_BOLD)

    @property
    def is_italic(self) -> bool:
        return bool(self.flags & FLAG_ITALIC)

    @property
    def is_serif(self) -> bool:
        return bool(self.flags & FLAG_SERIF)

    @property
    def is_mono(self) -> bool:
        return bool(self.flags & FLAG_MONO)

    @property
    def is_superscript(self) -> bool:
        return bool(self.flags & FLAG_SUPERSCRIPT)

    # ---------------- 颜色 ---------------- #
    def rgb(self) -> tuple[float, float, float]:
        """把 sRGB 整数还原成 0~1 的浮点三元组。"""
        value = int(self.color) & 0xFFFFFF
        return (
            ((value >> 16) & 0xFF) / 255.0,
            ((value >> 8) & 0xFF) / 255.0,
            (value & 0xFF) / 255.0,
        )

    def luminance(self) -> float:
        r, g, b = self.rgb()
        return 0.299 * r + 0.587 * g + 0.114 * b

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "page": self.page_index + 1,
            "bbox": [round(v, 2) for v in self.bbox],
            "text": self.text,
            "font_size": round(self.font_size, 2),
            "font_name": self.font_name,
            "color": self.color,
            "flags": self.flags,
            "align": self.align,
            "lines": self.line_count,
        }


@dataclass
class SkipRegion:
    """需要跳过的区域（图片 / 表格 / 矢量图）。"""

    bbox: tuple[float, float, float, float]
    kind: str  # "image" | "table" | "drawing"
    page_index: int


@dataclass
class PageLayout:
    index: int
    width: float
    height: float
    rotation: int
    blocks: list[TextBlock] = field(default_factory=list)
    skip_regions: list[SkipRegion] = field(default_factory=list)
    has_text_layer: bool = False


@dataclass
class DocumentLayout:
    path: str
    page_count: int
    pages: list[PageLayout] = field(default_factory=list)
    title: str = ""
    is_encrypted: bool = False

    @property
    def blocks(self) -> list[TextBlock]:
        return [b for page in self.pages for b in page.blocks]

    def stats(self) -> dict[str, Any]:
        pages_with_text = sum(1 for p in self.pages if p.has_text_layer)
        blocks = self.blocks
        chars = sum(len(b.text) for b in blocks)
        sizes = [b.font_size for b in blocks] or [0.0]
        return {
            "pages": self.page_count,
            "pages_with_text": pages_with_text,
            "text_blocks": len(blocks),
            "characters": chars,
            "median_font_size": round(statistics.median(sizes), 2),
            "skip_regions": sum(len(p.skip_regions) for p in self.pages),
        }


# --------------------------------------------------------------------------- #
# 文本规范化
# --------------------------------------------------------------------------- #

_WS_RE = re.compile(r"[ \t\u00a0\u2000-\u200b\u3000]+")
_HYPHEN_END_RE = re.compile(r"(\w)[-\u2010\u2011]$")
_URL_ONLY_RE = re.compile(r"^(?:https?://|www\.)\S+$", re.I)
_NUMBERISH_RE = re.compile(r"^[\s\d\W_]+$")
#: 至少包含一个「可翻译字符」（字母 / 汉字 / 假名 / 谚文…）
_TRANSLATABLE_RE = re.compile(
    r"[A-Za-z\u00c0-\u024f\u0400-\u04ff\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]"
)

#: Unicode「数学字母数字符号」区段（𝑛 𝒖 𝜑 𝟏 …），PDF 用它来表示数学斜体
_MATH_ALPHANUMERIC = range(0x1D400, 0x1D800)


def fold_math_alphanumeric(text: str) -> str:
    """把数学字母折回普通字母数字：``𝑛``→``n``、``𝒖``→``u``、``𝟏``→``1``。

    这些字符对 PDF 排版没意义（只是数学斜体的一种编码），但会带来两个麻烦：
    中文字体不含它们的字形，写进译文会缺字、宽度也量不准；送进提示词还会让
    模型以为整段都是公式而拒绝翻译。折成普通字符后两边都正常。
    """
    if not any(ord(ch) in _MATH_ALPHANUMERIC for ch in text):
        return text
    return "".join(
        unicodedata.normalize("NFKC", ch) if ord(ch) in _MATH_ALPHANUMERIC else ch
        for ch in text
    )


def normalise_text(text: str) -> str:
    """压缩空白、统一全角标点前后的空格，去掉软连字符与数学字母编码。"""
    if not text:
        return ""
    text = text.replace("\u00ad", "").replace("\ufeff", "")
    text = fold_math_alphanumeric(text)
    text = _WS_RE.sub(" ", text)
    return text.strip()


def is_translatable(text: str) -> bool:
    """判断一段文本是否值得送去翻译（过滤页码、纯符号、纯 URL 等）。"""
    stripped = text.strip()
    if len(stripped) < 2:
        return False
    if _URL_ONLY_RE.match(stripped):
        return False
    if _NUMBERISH_RE.match(stripped):
        return False
    return bool(_TRANSLATABLE_RE.search(stripped))


def join_lines(lines: list[str]) -> str:
    """把同一段落的多行拼回一句话，处理英文断词与中英混排。"""
    out = ""
    for raw in lines:
        piece = raw.strip()
        if not piece:
            continue
        if not out:
            out = piece
            continue
        if _HYPHEN_END_RE.search(out) and piece[:1].islower():
            # 英文行末断词：去掉连字符直接接上
            out = out[:-1] + piece
        elif _is_cjk(out[-1]) or _is_cjk(piece[0]):
            out += piece
        else:
            out += " " + piece
    return normalise_text(out)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3000 <= code <= 0x303F
        or 0x3040 <= code <= 0x30FF
        or 0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xAC00 <= code <= 0xD7AF
        or 0xF900 <= code <= 0xFAFF
        or 0xFF00 <= code <= 0xFFEF
    )


# --------------------------------------------------------------------------- #
# 版面分析辅助
# --------------------------------------------------------------------------- #


def _union(rects: Iterable[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    xs0, ys0, xs1, ys1 = [], [], [], []
    for x0, y0, x1, y1 in rects:
        xs0.append(x0)
        ys0.append(y0)
        xs1.append(x1)
        ys1.append(y1)
    return (min(xs0), min(ys0), max(xs1), max(ys1))


def _overlap_ratio(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> float:
    """a 被 b 覆盖的面积比例（相对于 a 自身面积）。"""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area = max(1e-6, (ax1 - ax0) * (ay1 - ay0))
    return inter / area


@dataclass
class _LineInfo:
    """一行的解析结果（用于把文本块按样式变化重新切分）。"""

    text: str
    bbox: tuple[float, float, float, float]
    span_boxes: list[tuple[float, float, float, float]]
    size: float
    color: int
    flags: int
    font_name: str
    horizontal: bool
    bold_ratio: float = 0.0


#: 相邻行主字号相差超过这个比例，就认为它们属于不同的排版元素（标题 vs 正文）
_FONT_SIZE_SPLIT_RATIO = 1.15
#: 一行里粗体字符占比超过/低于这两个阈值，才认定该行「整体是/不是粗体」
_BOLD_LINE_HIGH = 0.75
_BOLD_LINE_LOW = 0.25
#: 只允许在「前一组不超过这么多行」的地方切分。
#: 标题、标签总是短的；正文段落中间偶尔出现的整行粗体（例如强调某个算法名）
#: 前面已经堆了十几行，就不该再切 —— 否则一段话会被腰斩成两半。
_MAX_HEADING_LINES = 3
#: 前一组每一行的宽度都必须小于「本块最宽行的这个比例」，才认为它是标题。
#: 有些正文段落会用加粗字体排印，光看粗体/字号会把它误当成标题。
_HEADING_WIDTH_RATIO = 0.85


def _font_size_changed(previous: float, current: float) -> bool:
    if previous <= 0 or current <= 0:
        return False
    return max(previous, current) / min(previous, current) > _FONT_SIZE_SPLIT_RATIO


def _bold_state(ratio: float) -> int:
    """整行粗体状态：1=粗体，0=非粗体，-1=行内混排（不作为切分依据）。"""
    if ratio >= _BOLD_LINE_HIGH:
        return 1
    if ratio <= _BOLD_LINE_LOW:
        return 0
    return -1


def _line_style_changed(previous: "_LineInfo", current: "_LineInfo") -> bool:
    """判断相邻两行是否属于不同的排版元素。

    字号常常几乎一样（例如小节标题 9.96pt、正文 10.06pt），这时靠**粗体状态**
    区分：标题整行加粗、正文整行不加粗。行内偶尔出现的加粗词（ratio 落在中间）
    会返回 -1，不参与判断，避免把普通段落切碎。
    """
    if _font_size_changed(previous.size, current.size):
        return True
    before, after = _bold_state(previous.bold_ratio), _bold_state(current.bold_ratio)
    return before >= 0 and after >= 0 and before != after


def _guess_align(line_boxes: list[tuple[float, float, float, float]], tol: float = 2.5) -> int:
    """根据各行边界推断对齐方式：0=左 1=居中 2=右。

    **左对齐优先**：正文段落（含两端对齐）每行左边都齐，而两端对齐时每行等宽、
    中心也一致，若先判居中就会把整段正文判错。所以先看左边是否齐。
    """
    if len(line_boxes) < 2:
        return 0
    lefts = [b[0] for b in line_boxes]
    rights = [b[2] for b in line_boxes]
    centers = [(b[0] + b[2]) / 2 for b in line_boxes]

    # 段落首行缩进很常见，判断「左边齐」时从第二行看起
    tail = lefts[1:] if len(lefts) > 2 else lefts
    left_aligned = max(tail) - min(tail) <= tol
    right_aligned = max(rights) - min(rights) <= tol
    center_aligned = max(centers) - min(centers) <= tol

    if left_aligned:
        return 0
    if center_aligned:
        return 1
    if right_aligned:
        return 2
    return 0


def _block_bbox(block: dict[str, Any]) -> tuple[float, float, float, float]:
    raw = block.get("bbox") or (0.0, 0.0, 0.0, 0.0)
    return (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))


#: 两个文本块的 bbox 重叠超过「较小块面积」的这个比例，就认为是同一段内容被拆开了
_BLOCK_MERGE_OVERLAP = 0.30

#: 与「图形区域」重叠超过这个比例的文字块，视为图表内的标注，不翻译。
#: 实测分布是两极的：真正的图内标签（坐标轴、图例、节点名）几乎都是 100% 重叠，
#: 而正文、例子、图题大多在 60% 以下 —— 图形聚类出的外接矩形难免把紧贴图形
#: 周围的文字圈进来。取 70% 能把两者分开。
_DRAWING_OVERLAP = 0.70
#: 与「表格区域」重叠超过这个比例的文字块才跳过。find_tables 同样会把紧贴表格的
#: 正文段落圈进来，只有真正落在单元格内部的文字才该保持原样，所以用同一把尺子。
_TABLE_OVERLAP = 0.70
#: 图形区域至少要占页面这么大才算真正的图表（用来滤掉零星装饰线）
_MIN_DRAWING_AREA_RATIO = 0.003
#: 图形区域向外扩一点，让紧贴图形的坐标轴标题、图例、子图标题也能被覆盖
_DRAWING_PADDING = 4.0


def _group_overlapping_blocks(blocks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """把 bbox 明显互相重叠的文本块聚成一组（并查集）。

    为什么需要：PyMuPDF 会把同一段文字**按字体拆成多个块** —— 正文一个块、
    行内数学公式（CambriaMath 之类）另一个块 —— 但每个块的 bbox 都覆盖整段。
    如果不合并，它们会被当成互不相干的段落各自翻译、各自清除原文、各自写入，
    中文写上去之后，没被删掉的公式符号还留在原地，就叠字了。
    """
    if len(blocks) < 2:
        return [blocks]

    boxes = [fitz.Rect(_block_bbox(b)) for b in blocks]
    parent = list(range(len(blocks)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(blocks)):
        for j in range(i + 1, len(blocks)):
            if find(i) == find(j):
                continue
            inter = boxes[i] & boxes[j]
            if inter.is_empty or inter.is_infinite:
                continue
            area = inter.width * inter.height
            if area <= 0:
                continue
            smaller = min(boxes[i].width * boxes[i].height, boxes[j].width * boxes[j].height)
            if smaller > 0 and area / smaller >= _BLOCK_MERGE_OVERLAP:
                parent[find(j)] = find(i)

    grouped: dict[int, list[dict[str, Any]]] = {}
    for index, block in enumerate(blocks):
        grouped.setdefault(find(index), []).append(block)
    return list(grouped.values())


def _merge_block_group(group: list[dict[str, Any]]) -> dict[str, Any]:
    """把一组块的行按阅读顺序拼成一个块。"""
    if len(group) == 1:
        return group[0]

    lines: list[dict[str, Any]] = []
    for block in group:
        lines.extend(block.get("lines") or [])

    boxes = [_block_bbox(b) for b in group]
    bbox = _union(boxes)
    lines.sort(key=lambda ln: (
        round(float((ln.get("bbox") or (0, 0, 0, 0))[1]), 1),
        float((ln.get("bbox") or (0, 0, 0, 0))[0]),
    ))
    return {"type": 0, "bbox": bbox, "lines": lines}


# --------------------------------------------------------------------------- #
# 主提取器
# --------------------------------------------------------------------------- #

_HEADER_FOOTER_BAND = 0.085  # 页面上下各 8.5% 视为页眉/页脚带


class PdfExtractor:
    """把 PDF 拆解成「文字块 + 跳过区域」。"""

    def __init__(
        self,
        *,
        detect_tables: bool = True,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.detect_tables = detect_tables
        self.log = log or (lambda _msg: None)
        self.skipped_rotated = 0
        self.skipped_overlap = 0
        self.skipped_formula = 0
        route_pymupdf_messages(self.log)

    # ------------------------------------------------------------------ #
    def extract(
        self,
        pdf_path: str | Path,
        *,
        page_indices: list[int] | None = None,
        translate_headers: bool = True,
        skip_images: bool = True,
        skip_tables: bool = True,
        skip_figures: bool = True,
    ) -> DocumentLayout:
        path = Path(pdf_path)
        if not path.exists():
            raise FileNotFoundError(f"找不到文件：{path}")

        doc = fitz.open(str(path))
        try:
            return self._extract_doc(
                doc, str(path), page_indices, translate_headers,
                skip_images, skip_tables, skip_figures,
            )
        finally:
            doc.close()

    # ------------------------------------------------------------------ #
    def _extract_doc(
        self,
        doc: "fitz.Document",
        path: str,
        page_indices: list[int] | None,
        translate_headers: bool,
        skip_images: bool,
        skip_tables: bool,
        skip_figures: bool = True,
    ) -> DocumentLayout:
        layout = DocumentLayout(path=path, page_count=doc.page_count)
        self.skipped_rotated = 0
        self.skipped_overlap = 0
        self.skipped_formula = 0

        if doc.needs_pass:
            layout.is_encrypted = True
            if not doc.authenticate(""):
                raise PermissionError("该 PDF 有密码保护，请先解除密码后再翻译。")

        meta = doc.metadata or {}
        layout.title = (meta.get("title") or "").strip()

        targets = page_indices if page_indices is not None else list(range(doc.page_count))
        targets = [i for i in targets if 0 <= i < doc.page_count]

        # 先逐页提取原始信息，之后再统一做页眉页脚判定
        for index in targets:
            page = doc[index]
            layout.pages.append(
                self._extract_page(page, index, skip_images, skip_tables, skip_figures)
            )

        # 页眉页脚总是标记出来（标记本身无害），是否跳过由上层决定
        self._mark_header_footer(layout)

        if self.skipped_rotated:
            self.log(
                f"⚠ 有 {self.skipped_rotated} 处竖排/旋转文字未参与翻译"
                f"（写入方向难以还原，保留原文更安全）"
            )
        if self.skipped_overlap:
            self.log(f"· 有 {self.skipped_overlap} 处文字属于图片/表格/图表区域，已保持原样")
        if self.skipped_formula:
            self.log(f"· 有 {self.skipped_formula} 处独立公式，已保持原样")

        return layout

    # ------------------------------------------------------------------ #
    def _extract_page(
        self,
        page: "fitz.Page",
        index: int,
        skip_images: bool,
        skip_tables: bool,
        skip_figures: bool = True,
    ) -> PageLayout:
        crop = page.cropbox
        layout = PageLayout(
            index=index,
            width=float(crop.width),
            height=float(crop.height),
            rotation=int(page.rotation or 0),
        )

        raw = page.get_text("dict", flags=fitz.TEXTFLAGS_TEXT | fitz.TEXT_PRESERVE_IMAGES)
        image_rects: list[tuple[float, float, float, float]] = []
        table_rects: list[tuple[float, float, float, float]] = []
        drawing_rects: list[tuple[float, float, float, float]] = []

        # ---- 图片区域 ---- #
        for block in raw.get("blocks", []):
            if block.get("type") == 1:  # 图片块
                bbox = tuple(float(v) for v in block.get("bbox", (0, 0, 0, 0)))
                image_rects.append(bbox)
                if skip_images:
                    layout.skip_regions.append(SkipRegion(bbox, "image", index))

        # 有些图片不在 text dict 里（例如 Form XObject 中的），补充一次
        if skip_images:
            try:
                for info in page.get_image_info():
                    bbox = tuple(float(v) for v in info.get("bbox", (0, 0, 0, 0)))
                    if bbox[2] - bbox[0] > 1 and bbox[3] - bbox[1] > 1:
                        image_rects.append(bbox)
                        layout.skip_regions.append(SkipRegion(bbox, "image", index))
            except Exception:  # pragma: no cover - 老版本 PyMuPDF
                pass

        # ---- 表格区域 ---- #
        if skip_tables and self.detect_tables and hasattr(page, "find_tables"):
            try:
                # lines_strict 只认规整的表格线，能避开把柱状图/折线图误判成表格
                try:
                    finder = page.find_tables(strategy="lines_strict")
                    tables = list(getattr(finder, "tables", []))
                    if not tables:
                        finder = page.find_tables()
                        tables = list(getattr(finder, "tables", []))
                except TypeError:  # 老版本 PyMuPDF 没有 strategy 参数
                    finder = page.find_tables()
                    tables = list(getattr(finder, "tables", []))
                for table in tables:
                    bbox = tuple(float(v) for v in table.bbox)
                    # 至少要是个 2×2 的网格才算表格。find_tables 有时会把表格标题
                    # 或紧贴表格的正文段落也识别成一个「表格」。
                    rows = int(getattr(table, "row_count", 0) or 0)
                    cols = int(getattr(table, "col_count", 0) or 0)
                    if rows and cols and (rows < 2 or cols < 2):
                        continue
                    table_rects.append(bbox)
                    layout.skip_regions.append(SkipRegion(bbox, "table", index))
            except Exception as exc:  # 表格识别失败不应影响主流程
                self.log(f"· 第 {index + 1} 页表格识别跳过：{exc}")

        # ---- 图表区域（矢量图形聚类）---- #
        # 柱状图、折线图、结构示意图大多是用线条画的，不是嵌入图片，靠图片对象
        # 完全识别不到，里面的坐标轴标题、图例、标注就会被当成正文翻译。
        # cluster_drawings() 把绘图对象聚成若干矩形，正好用来框出这些区域。
        if skip_figures and hasattr(page, "cluster_drawings"):
            try:
                page_area = max(1.0, float(page.rect.width * page.rect.height))
                for cluster in page.cluster_drawings():
                    rect = fitz.Rect(cluster)
                    rect.x0 -= _DRAWING_PADDING
                    rect.y0 -= _DRAWING_PADDING
                    rect.x1 += _DRAWING_PADDING
                    rect.y1 += _DRAWING_PADDING
                    if rect.width <= 0 or rect.height <= 0:
                        continue
                    if rect.width * rect.height < page_area * _MIN_DRAWING_AREA_RATIO:
                        continue  # 零星装饰线，忽略
                    bbox = (rect.x0, rect.y0, rect.x1, rect.y1)
                    # 只有「用户要求翻译表格」（skip_tables=False）时，才把表格占用的
                    # 区域让出来；默认情况下表格和图表都是要跳过的，无需互斥 ——
                    # 而且 find_tables 常把柱状图误判成表格，一排除就会漏掉大片图形区域。
                    if not skip_tables and any(
                        _overlap_ratio(bbox, t) > 0.5 for t in table_rects
                    ):
                        continue
                    drawing_rects.append(bbox)
                    layout.skip_regions.append(SkipRegion(bbox, "drawing", index))
            except Exception as exc:
                self.log(f"· 第 {index + 1} 页图表区域识别跳过：{exc}")

        # ---- 文字块 ---- #
        # 先按 bbox 重叠关系合并：PyMuPDF 常把「正文 + 行内公式」拆成互相重叠的多个块
        raw_text_blocks = [b for b in raw.get("blocks", []) if b.get("type") == 0]
        for order, group in enumerate(_group_overlapping_blocks(raw_text_blocks)):
            merged = _merge_block_group(group)
            for text_block in self._build_text_blocks(merged, index, order):
                bbox = text_block.bbox
                # 图题和成段正文都要放行：表格/图表区域都是靠线条猜出来的，
                # 它们的外接矩形难免把紧贴着的标题、正文段落一并圈进去。
                protected = is_caption(text_block.text) or looks_like_prose(text_block.text)

                # 图片区域来自 PDF 的图片对象，位置可靠 —— 压在图片里的说明文字一律不翻。
                if skip_images and any(
                    _overlap_ratio(bbox, r) > 0.35 for r in image_rects
                ):
                    self.skipped_overlap += 1
                    # 记成障碍物：它虽然不翻译，但位置仍被文字占着，
                    # 旁边块的译文向下扩展时必须绕开，否则会压上去叠字。
                    layout.skip_regions.append(SkipRegion(bbox, "text", index))
                    continue
                # 表格里的单元格文字：保持原样
                if (
                    skip_tables
                    and not protected
                    and any(_overlap_ratio(bbox, r) > _TABLE_OVERLAP for r in table_rects)
                ):
                    self.skipped_overlap += 1
                    layout.skip_regions.append(SkipRegion(bbox, "text", index))
                    continue
                # 图表内的坐标轴标题、图例、标注：属于图表的一部分，保持原样
                if (
                    skip_figures
                    and not protected
                    and any(_overlap_ratio(bbox, r) > _DRAWING_OVERLAP for r in drawing_rects)
                ):
                    self.skipped_overlap += 1
                    layout.skip_regions.append(SkipRegion(bbox, "text", index))
                    continue
                # 数学内容（整块公式、短变量表达式、符号密集的定义）：保持原样
                if skip_figures and looks_like_formula(
                    text_block.text, text_block.font_name
                ):
                    self.skipped_formula += 1
                    layout.skip_regions.append(SkipRegion(bbox, "text", index))
                    continue
                layout.blocks.append(text_block)

        self._refine_alignment(layout.blocks)
        layout.has_text_layer = bool(layout.blocks)
        return layout

    # ------------------------------------------------------------------ #
    @staticmethod
    def _refine_alignment(blocks: list[TextBlock]) -> None:
        """单行文本无法从行边界推断对齐，改用它在「版心」里的相对位置判断。

        版心 = 本页所有文字块的外接范围。判定「居中」必须同时满足：
        **左右两侧都有明显留白**（各占版心 3% 以上）且**留白大致相等**。

        只比较中心位置是不够的：满版心的正文行（例如一行 CCS Concepts）中心本来
        就贴着版心中心，会被误判成居中 —— 而它左边是贴边的，这正是左对齐的特征。
        """
        if len(blocks) < 2:
            return
        left = min(b.bbox[0] for b in blocks)
        right = max(b.bbox[2] for b in blocks)
        span = right - left
        if span < 40:
            return
        center = (left + right) / 2
        min_gap = span * 0.03      # 居中的话两侧至少要有这么多留白
        gap_tol = span * 0.05      # 两侧留白之差的上限

        for block in blocks:
            if block.line_count > 1:
                continue  # 多行的已经由行边界判断过
            block_center = (block.bbox[0] + block.bbox[2]) / 2
            left_gap = block.bbox[0] - left
            right_gap = right - block.bbox[2]

            centred = (
                left_gap > min_gap
                and right_gap > min_gap
                and abs(left_gap - right_gap) <= gap_tol
                and abs(block_center - center) <= gap_tol
            )
            if centred:
                block.align = 1
            elif right_gap <= span * 0.03 and left_gap > span * 0.15:
                block.align = 2  # 右对齐（页脚常见）
            else:
                block.align = 0

    # ------------------------------------------------------------------ #
    def _build_text_blocks(
        self, block: dict[str, Any], page_index: int, order: int
    ) -> list[TextBlock]:
        """把一个 PyMuPDF 文本块拆成若干可翻译块。

        PyMuPDF 经常把「小节标题 + 紧跟的正文段落」合进同一个块
        （例如 ``1 Introduction`` 和它下面那段话）。如果整块一起翻译，标题就会
        被并进正文、字号也统一掉。这里按**行主字号**的变化把它重新切开。
        """
        lines = block.get("lines") or []
        if not lines:
            return []

        parsed = [info for info in (self._parse_line(line) for line in lines) if info]
        if not parsed:
            return []

        if not all(info.horizontal for info in parsed):
            # 竖排 / 旋转文字：写入方向难以可靠还原，宁可保留原文
            self.skipped_rotated += 1
            return []

        # 用「本块最宽的一行」当参照：标题行的宽度远小于它，正文行则接近它
        widest = max(info.bbox[2] - info.bbox[0] for info in parsed)
        heading_limit = widest * _HEADING_WIDTH_RATIO

        groups: list[list[_LineInfo]] = [[parsed[0]]]
        for info in parsed[1:]:
            current = groups[-1]
            heading_like = (
                len(current) <= _MAX_HEADING_LINES
                and all((i.bbox[2] - i.bbox[0]) <= heading_limit for i in current)
            )
            if heading_like and _line_style_changed(current[-1], info):
                groups.append([info])
            else:
                current.append(info)

        blocks: list[TextBlock] = []
        for part, group in enumerate(groups):
            built = self._make_text_block(group, page_index, order, part, len(groups))
            if built is not None:
                blocks.append(built)
        return blocks

    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_line(line: dict[str, Any]) -> "_LineInfo | None":
        spans = line.get("spans") or []
        parts: list[str] = []
        span_boxes: list[tuple[float, float, float, float]] = []
        size_weight: list[tuple[float, int]] = []
        color_weight: list[tuple[int, int]] = []
        flag_weight: list[tuple[int, int]] = []
        names: list[str] = []
        bold_chars = 0
        total_chars = 0
        previous_x1: float | None = None

        for span in spans:
            content = span.get("text") or ""
            if not content:
                continue
            box = tuple(float(v) for v in span.get("bbox", (0, 0, 0, 0)))

            # PyMuPDF 在 span 边界常常丢空格（"We let 𝑛" + "and" → "We let 𝑛and"），
            # 用两个 span 之间的空隙把空格补回来，否则会污染译文
            if (
                previous_x1 is not None
                and parts
                and box[0] - previous_x1 > 0.8
                and not parts[-1][-1].isspace()
                and not content[0].isspace()
                and not _is_cjk(parts[-1][-1])
                and not _is_cjk(content[0])
            ):
                parts.append(" ")

            parts.append(content)
            previous_x1 = box[2]

            count = len(content.strip()) or 1
            size_weight.append((float(span.get("size") or 0.0), count))
            color_weight.append((int(span.get("color") or 0), count))
            flags = int(span.get("flags") or 0)
            flag_weight.append((flags, count))
            names.append(str(span.get("font") or ""))
            span_boxes.append(box)
            if content.strip():
                total_chars += count
                if flags & FLAG_BOLD:
                    bold_chars += count

        text = "".join(parts)
        if not text.strip() or not span_boxes:
            return None

        direction = line.get("dir") or (1.0, 0.0)
        horizontal = abs(float(direction[0]) - 1.0) <= 0.02 and abs(float(direction[1])) <= 0.02

        return _LineInfo(
            text=text,
            bbox=tuple(float(v) for v in line.get("bbox", (0, 0, 0, 0))),
            span_boxes=span_boxes,
            size=float(_weighted_mode(size_weight) or 0.0),
            color=int(_weighted_mode(color_weight) or 0),
            flags=int(_weighted_mode(flag_weight) or 0),
            font_name=_most_common(names) or "",
            horizontal=horizontal,
            bold_ratio=(bold_chars / total_chars) if total_chars else 0.0,
        )

    # ------------------------------------------------------------------ #
    def _make_text_block(
        self,
        group: list["_LineInfo"],
        page_index: int,
        order: int,
        part: int,
        total_parts: int,
    ) -> TextBlock | None:
        text = join_lines([info.text for info in group])
        if not text:
            return None

        weights = [(info.size, len(info.text.strip()) or 1) for info in group]
        font_size = float(_weighted_mode(weights) or 0.0) or 11.0
        color = int(_weighted_mode([(i.color, len(i.text.strip()) or 1) for i in group]) or 0)
        flags = int(_weighted_mode([(i.flags, len(i.text.strip()) or 1) for i in group]) or 0)
        font_name = _most_common([info.font_name for info in group]) or ""

        if not (flags & FLAG_SERIF) and re.search(
            r"serif|times|georgia|song|ming|garamond", font_name, re.I
        ):
            flags |= FLAG_SERIF
        if not (flags & FLAG_BOLD) and re.search(
            r"bold|black|heavy|semibold|demi", font_name, re.I
        ):
            flags |= FLAG_BOLD

        bbox = _union([box for info in group for box in info.span_boxes])
        align = _guess_align([info.bbox for info in group])
        suffix = "" if total_parts == 1 else f"s{part}"

        return TextBlock(
            block_id=f"p{page_index + 1}b{order}{suffix}",
            page_index=page_index,
            bbox=bbox,
            text=text,
            font_size=font_size,
            font_name=font_name,
            color=color,
            flags=flags,
            align=align,
            line_count=len(group),
        )

    # ------------------------------------------------------------------ #
    def _mark_header_footer(self, layout: DocumentLayout) -> None:
        """把上下边距带里反复出现的文字标记为页眉/页脚。"""
        seen: dict[str, int] = {}
        for page in layout.pages:
            band_top = page.height * _HEADER_FOOTER_BAND
            band_bottom = page.height * (1 - _HEADER_FOOTER_BAND)
            for block in page.blocks:
                if block.bbox[3] <= band_top or block.bbox[1] >= band_bottom:
                    key = re.sub(r"\d+", "#", block.text.strip().lower())
                    seen[key] = seen.get(key, 0) + 1
        # 出现在 3 页以上、且是纯页码 / 固定页眉的，直接标记
        for page in layout.pages:
            band_top = page.height * _HEADER_FOOTER_BAND
            band_bottom = page.height * (1 - _HEADER_FOOTER_BAND)
            for block in page.blocks:
                if block.bbox[3] <= band_top or block.bbox[1] >= band_bottom:
                    key = re.sub(r"\d+", "#", block.text.strip().lower())
                    if seen.get(key, 0) >= 3 and len(block.text) < 120:
                        block.is_header_footer = True


def _weighted_mode(pairs: list[tuple[Any, int]]) -> Any:
    if not pairs:
        return None
    totals: dict[Any, int] = {}
    for value, weight in pairs:
        key = round(value, 2) if isinstance(value, float) else value
        totals[key] = totals.get(key, 0) + weight
    return max(totals.items(), key=lambda kv: kv[1])[0]


def _most_common(items: list[str]) -> str:
    if not items:
        return ""
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


# --------------------------------------------------------------------------- #
# 页码范围解析
# --------------------------------------------------------------------------- #


def parse_page_range(spec: str, page_count: int) -> list[int] | None:
    """解析 "1-5,8,10-" 这类页码范围，返回 0 基索引列表；空串表示全部。"""
    spec = (spec or "").strip()
    if not spec:
        return None
    result: list[int] = []
    for part in re.split(r"[,，;；\s]+", spec):
        if not part:
            continue
        match = re.fullmatch(r"(\d+)?\s*[-~—]\s*(\d+)?", part)
        if match:
            start = int(match.group(1)) if match.group(1) else 1
            end = int(match.group(2)) if match.group(2) else page_count
        elif part.isdigit():
            start = end = int(part)
        else:
            raise ValueError(f"无法识别的页码范围片段：{part}")
        start = max(1, start)
        end = min(page_count, end)
        result.extend(range(start - 1, end))
    # 去重并保持顺序
    seen: set[int] = set()
    ordered: list[int] = []
    for index in result:
        if index not in seen:
            seen.add(index)
            ordered.append(index)
    return ordered


__all__ = [
    "TextBlock",
    "SkipRegion",
    "PageLayout",
    "DocumentLayout",
    "PdfExtractor",
    "parse_page_range",
    "is_translatable",
    "normalise_text",
]
