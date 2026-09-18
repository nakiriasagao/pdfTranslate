"""把 PDF 渲染成 PNG，方便肉眼检查排版效果。

    python tools/preview.py 输出.pdf                 # 默认 110 DPI，全部页面
    python tools/preview.py 输出.pdf --dpi 150 --pages 1-2
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore[no-redef]


def main() -> int:
    parser = argparse.ArgumentParser(description="PDF → PNG 预览")
    parser.add_argument("pdf", help="要渲染的 PDF")
    parser.add_argument("-o", "--out-dir", default="", help="输出目录（默认 PNG 与 PDF 同目录）")
    parser.add_argument("--dpi", type=int, default=110, help="渲染分辨率，默认 110")
    parser.add_argument("--pages", default="", help='页码范围，如 "1-2"；默认全部')
    parser.add_argument("--suffix", default="", help="文件名后缀，避免覆盖")
    args = parser.parse_args()

    source = Path(args.pdf)
    if not source.exists():
        print(f"找不到文件：{source}", file=sys.stderr)
        return 1
    out_dir = Path(args.out_dir) if args.out_dir else source.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    indices: list[int] | None = None
    if args.pages.strip():
        from pdf_translator.extractor import parse_page_range

        with fitz.open(str(source)) as probe:
            indices = parse_page_range(args.pages, probe.page_count)

    written: list[Path] = []
    with fitz.open(str(source)) as doc:
        targets = indices if indices is not None else range(doc.page_count)
        for index in targets:
            pix = doc[index].get_pixmap(dpi=args.dpi)
            target = out_dir / f"{source.stem}{args.suffix}_p{index + 1:02d}.png"
            pix.save(str(target))
            written.append(target)

    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
