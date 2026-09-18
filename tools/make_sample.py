"""生成用于测试的样例 PDF（含标题、正文、图片、表格、页眉页脚）。

    python tools/make_sample.py                 # 生成 samples/sample_en.pdf
    python tools/make_sample.py out.pdf 4       # 指定路径与页数
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore[no-redef]


BODY_EN = (
    "Retrieval-augmented generation (RAG) has become a standard technique for grounding "
    "large language models in external knowledge. Instead of relying solely on parametric "
    "memory, the system first retrieves relevant passages from a document store and then "
    "conditions the generator on them. This substantially reduces hallucination on "
    "knowledge-intensive tasks such as open-domain question answering."
)

BODY_EN_2 = (
    "However, retrieval quality is only half of the story. The generator must also learn to "
    "ignore distractors, attribute its claims to the retrieved evidence, and abstain when the "
    "evidence is insufficient. Recent work shows that simply concatenating more passages does "
    "not monotonically improve accuracy, and can even degrade it when the context window is "
    "flooded with irrelevant text."
)

BODY_EN_3 = (
    "In this paper we introduce a lightweight re-ranking stage that operates on the top-k "
    "candidates returned by a dense retriever. The re-ranker is trained with a contrastive "
    "objective on 120,000 question-passage pairs and requires no additional annotation."
)

HEADING_1 = "1. Introduction"
HEADING_2 = "2. Method"
HEADING_3 = "3. Experiments"

TABLE_ROWS = [
    ["Model", "Params", "EM", "F1"],
    ["Baseline (BM25)", "-", "31.2", "38.4"],
    ["Dense only", "110M", "38.7", "45.1"],
    ["Ours (+re-rank)", "110M", "44.9", "51.6"],
]


def _png_bytes(width: int, height: int) -> bytes:
    """用标准库手搓一张 RGB 渐变 PNG（避免依赖 Pillow）。"""
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # 每行的 filter 类型
        for x in range(width):
            raw.append(int(30 + 190 * x / width))       # R
            raw.append(int(60 + 130 * y / height))      # G
            raw.append(int(215 - 150 * x / width))      # B

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


def _make_gradient_pixmap(width: int = 240, height: int = 140) -> "fitz.Pixmap":
    """造一张彩色渐变位图，当作论文插图。"""
    return fitz.Pixmap(_png_bytes(width, height))


def _draw_image(page: fitz.Page, rect: fitz.Rect) -> None:
    """插入一张真正的位图（会被识别为 image 区域，从而跳过翻译）。"""
    page.insert_image(rect, pixmap=_make_gradient_pixmap())
    page.draw_rect(rect, color=(0.1, 0.1, 0.1), width=0.8)
    page.insert_text(
        fitz.Point(rect.x0 + 6, rect.y1 - 8),
        "Figure 1: Retrieval pipeline (image - must NOT be translated)",
        fontsize=7.5,
        fontname="helv",
        color=(1, 1, 1),
    )


def _draw_table(page: fitz.Page, origin: fitz.Point) -> None:
    row_h, col_w = 18.0, 95.0
    x0, y0 = origin.x, origin.y
    for r, row in enumerate(TABLE_ROWS):
        for c, cell in enumerate(row):
            cell_rect = fitz.Rect(
                x0 + c * col_w, y0 + r * row_h, x0 + (c + 1) * col_w, y0 + (r + 1) * row_h
            )
            page.draw_rect(cell_rect, color=(0.4, 0.4, 0.4), width=0.6)
            page.insert_text(
                fitz.Point(cell_rect.x0 + 5, cell_rect.y1 - 5.5),
                cell,
                fontsize=9,
                fontname="hebo" if r == 0 else "helv",
                color=(0, 0, 0),
            )
    page.insert_text(
        fitz.Point(x0, y0 + len(TABLE_ROWS) * row_h + 14),
        "Table 1: Main results (table – must NOT be translated)",
        fontsize=8,
        fontname="tiro",
        color=(0.25, 0.25, 0.25),
    )


def build_sample(path: Path, pages: int = 3) -> Path:
    doc = fitz.open()
    body_blocks = [BODY_EN, BODY_EN_2, BODY_EN_3]

    for index in range(pages):
        page = doc.new_page(width=595, height=842)  # A4
        # 页眉
        page.insert_text(fitz.Point(56, 44), "Journal of Applied Machine Learning, Vol. 12",
                         fontsize=8, fontname="tiro", color=(0.4, 0.4, 0.4))
        page.draw_line(fitz.Point(56, 50), fitz.Point(539, 50), color=(0.7, 0.7, 0.7), width=0.5)

        y = 84.0
        if index == 0:
            written = page.insert_textbox(
                fitz.Rect(56, y, 539, y + 34),
                "Retrieval-Augmented Generation with Lightweight Re-ranking",
                fontsize=15, fontname="hebo", align=fitz.TEXT_ALIGN_CENTER, color=(0, 0, 0),
            )
            if written < 0:  # insert_textbox 放不下时不会写入任何内容
                raise RuntimeError("样例标题放不下，请调小字号")
            y += 42
            written = page.insert_textbox(
                fitz.Rect(56, y, 539, y + 16),
                "A. Researcher, B. Coauthor - Institute of Information Systems",
                fontsize=9.5, fontname="tiro", align=fitz.TEXT_ALIGN_CENTER, color=(0.3, 0.3, 0.3),
            )
            if written < 0:
                raise RuntimeError("样例作者行放不下，请调小字号")
            y += 30

        page.insert_text(fitz.Point(56, y), HEADING_1 if index == 0 else HEADING_2,
                         fontsize=13.5, fontname="hebo", color=(0.05, 0.05, 0.35))
        y += 20

        page.insert_textbox(
            fitz.Rect(56, y, 539, y + 110), body_blocks[index % len(body_blocks)],
            fontsize=10.5, fontname="tiro", align=fitz.TEXT_ALIGN_JUSTIFY, color=(0, 0, 0),
        )
        y += 108

        if index == 0:
            _draw_image(page, fitz.Rect(120, y, 475, y + 130))
            y += 156
            page.insert_text(fitz.Point(56, y), HEADING_3, fontsize=13.5,
                             fontname="hebo", color=(0.05, 0.05, 0.35))
            y += 22
            page.insert_textbox(
                fitz.Rect(56, y, 539, y + 70), BODY_EN_3,
                fontsize=10.5, fontname="tiro", color=(0, 0, 0),
            )
            y += 86
            _draw_table(page, fitz.Point(140, y))
        else:
            page.insert_textbox(
                fitz.Rect(56, y, 539, y + 90), BODY_EN_3,
                fontsize=10.5, fontname="tiro", align=fitz.TEXT_ALIGN_JUSTIFY, color=(0, 0, 0),
            )
            y += 96
            page.insert_textbox(
                fitz.Rect(56, y, 539, y + 90), BODY_EN,
                fontsize=10.5, fontname="tiro", color=(0, 0, 0),
            )

        # 页脚页码
        page.insert_text(fitz.Point(300, 812), f"- {index + 1} -",
                         fontsize=9, fontname="tiro", color=(0.4, 0.4, 0.4))

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path), garbage=3, deflate=True)
    doc.close()
    return path


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent / "samples" / "sample_en.pdf"
    pages = int(argv[2]) if len(argv) > 2 else 3
    built = build_sample(target, pages)
    print(f"已生成样例 PDF：{built}  （{pages} 页）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
