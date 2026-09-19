"""端到端离线自检：不联网、不需要 API Key，就能验证整条流水线。

    python tests/test_offline.py

覆盖内容
--------
1. 版面提取：文字块、图片区域、表格区域的识别
2. 翻译流程：批量打包、JSON 解析、缓存命中、条数不符时二分降级
3. 版面重建：译文替换版（原文被替换 / 图片仍在 / 页数不变）
4. 双语对照版：页面加宽、左栏保留原文、右栏为译文
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

# Windows 控制台默认是 GBK，输出 ✓/✗ 会直接抛 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        if _stream is not None and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore[no-redef]

from mock_server import start_mock_server  # noqa: E402
from make_sample import build_sample  # noqa: E402

from pdf_translator.config import MODE_BILINGUAL, MODE_REPLACED, Settings  # noqa: E402
from pdf_translator.extractor import PdfExtractor  # noqa: E402
from pdf_translator.pipeline import Pipeline  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    if condition:
        PASSED.append(label)
        print(f"  ✓ {label}")
    else:
        FAILED.append(f"{label} {detail}".strip())
        print(f"  ✗ {label}  {detail}")


def page_text(page: "fitz.Page") -> str:
    return page.get_text("text")


# --------------------------------------------------------------------------- #
def test_extraction(sample: Path) -> None:
    print("\n[1] 版面提取")
    layout = PdfExtractor().extract(sample)
    stats = layout.stats()
    check(stats["pages"] == 3, "页数 = 3", f"实际 {stats['pages']}")
    check(stats["text_blocks"] > 8, "提取到足够多的文字块", f"实际 {stats['text_blocks']}")
    check(stats["skip_regions"] >= 2, "识别出图片/表格跳过区域", f"实际 {stats['skip_regions']}")

    kinds = {r.kind for p in layout.pages for r in p.skip_regions}
    check("image" in kinds, "识别到图片区域")
    check("table" in kinds, "识别到表格区域")

    all_text = " ".join(b.text for b in layout.blocks)
    check("Figure 1" not in all_text, "图片内的说明文字未被当作正文提取")
    check("Baseline (BM25)" not in all_text, "表格内的文字未被当作正文提取")
    check("Retrieval-augmented generation" in all_text, "正文段落被正确提取")
    check("Journal of Applied" in all_text, "页眉被提取（供后续按开关处理）")

    headers = [b for b in layout.blocks if b.is_header_footer]
    check(bool(headers), "页眉/页脚被标记", f"标记 {len(headers)} 个")


# --------------------------------------------------------------------------- #
def test_translation_and_build(sample: Path, workdir: Path, base_url: str) -> None:
    print("\n[2] 翻译 + 重建排版")
    settings = Settings()
    settings.input_pdf = str(sample)
    settings.output_dir = str(workdir)
    settings.engine = "custom"
    settings.base_url = base_url
    settings.api_key = "mock-key"
    settings.model = "mock"
    settings.target_lang = "zh"
    settings.concurrency = 2
    settings.replace_mode = "redact"
    settings.bilingual_split = "vertical"
    settings.cache_file = str(workdir / "trans_main.sqlite3")

    logs: list[str] = []
    setattr(settings, "_modes", [MODE_REPLACED, MODE_BILINGUAL])

    result = Pipeline(settings, log=logs.append).run(sample)
    check(len(result.outputs) == 2, "生成了 2 个 PDF（替换版 + 双语版）", f"实际 {len(result.outputs)}")
    check(result.translated_blocks > 0, "有段落被送去翻译", f"实际 {result.translated_blocks}")
    check(result.usage.requests > 0, "统计到了 API 调用次数")

    replaced = Path(result.outputs[0])
    bilingual = Path(result.outputs[1])
    check(replaced.exists() and replaced.stat().st_size > 3000, "译文替换版文件已写出")
    check(bilingual.exists() and bilingual.stat().st_size > 3000, "双语对照版文件已写出")

    # ---------- 替换版 ---------- #
    print("\n[3] 译文替换版检查")
    with fitz.open(replaced) as doc:
        check(doc.page_count == 3, "页数保持不变", f"实际 {doc.page_count}")
        first = page_text(doc[0])
        check("【译】" in first, "页面出现译文")
        check("Retrieval-augmented generation (RAG) has become" not in first, "原文正文已被替换")
        check("Figure 1" in doc[0].get_text(), "图片区域内容未受影响")
        check("Baseline (BM25)" in doc[0].get_text(), "表格内容未被翻译/删除")
        images = doc[0].get_images(full=True)
        drawings = doc[0].get_drawings()
        check(len(drawings) > 0, "矢量图形（插图/表格线）仍然存在", f"{len(drawings)} 个")
        all_pages = page_text(doc[0]) + page_text(doc[1]) + page_text(doc[2])
        check(all_pages.count("【译】") >= 6, "多页都完成了翻译", f"共 {all_pages.count('【译】')} 处")

        # 译文不应越出页面边界
        overflow = False
        for page in doc:
            rect = page.rect
            for block in page.get_text("blocks"):
                x0, y0, x1, y1 = block[:4]
                if x1 > rect.width + 2 or y1 > rect.height + 2 or x0 < -2 or y0 < -2:
                    overflow = True
        check(not overflow, "没有文字溢出页面边界")

    # ---------- 双语版 ---------- #
    print("\n[4] 双语对照版检查")
    with fitz.open(bilingual) as doc, fitz.open(sample) as src:
        check(doc.page_count == src.page_count, "双语版页数与原文一致")
        expected_w = src[0].rect.width * 2 + settings.bilingual_gap
        check(abs(doc[0].rect.width - expected_w) < 1.5,
              "双语版页面已加宽为两栏", f"{doc[0].rect.width:.1f} vs {expected_w:.1f}")
        check(abs(doc[0].rect.height - src[0].rect.height) < 1.0, "双语版高度与原文一致")

        page = doc[0]
        full = page_text(page)
        check("Retrieval-augmented generation" in full, "左栏保留了英文原文")
        check("【译】" in full, "右栏写入了中文译文")
        left_clip = page.get_text("text", clip=fitz.Rect(0, 0, src[0].rect.width, page.rect.height))
        right_clip = page.get_text(
            "text",
            clip=fitz.Rect(src[0].rect.width + settings.bilingual_gap, 0, page.rect.width, page.rect.height),
        )
        check("【译】" not in left_clip, "左栏没有被写入译文（纯净原文）")
        check("【译】" in right_clip, "译文确实落在右栏区域")
        check("Retrieval-augmented generation (RAG) has become" not in right_clip,
              "右栏的英文原文已被清除（redaction 对 Form XObject 生效）",
              "→ 若此项失败，说明该 PyMuPDF 版本无法对 XObject 做 redact，"
              "请把双语版改为 cover 模式")
        check("Baseline (BM25)" in full, "双语版同样保留了表格内容")


# --------------------------------------------------------------------------- #
def test_cache_and_fallback(sample: Path, workdir: Path, base_url: str) -> None:
    print("\n[5] 缓存与降级")
    settings = Settings()
    settings.input_pdf = str(sample)
    settings.output_dir = str(workdir / "cache_test")
    settings.engine = "custom"
    settings.base_url = base_url
    settings.api_key = "mock-key"
    settings.model = "mock"
    settings.target_lang = "zh"
    settings.concurrency = 1
    settings.use_cache = True
    settings.cache_file = str(workdir / "cache_test" / "trans.sqlite3")
    setattr(settings, "_modes", [MODE_REPLACED])

    started = time.time()
    first = Pipeline(settings, log=lambda _m: None).run(sample)
    first_time = time.time() - started
    check(first.usage.requests > 0, "首次运行确实调用了 API", f"{first.usage.requests} 次")

    started = time.time()
    second = Pipeline(settings, log=lambda _m: None).run(sample)
    second_time = time.time() - started
    check(second.usage.requests == 0, "第二次运行全部命中缓存，零 API 调用",
          f"实际 {second.usage.requests} 次")
    check(second_time <= first_time + 0.05, "第二次不慢于第一次",
          f"{second_time:.2f}s vs {first_time:.2f}s")

    # ---------- 条数不符时的二分降级 ----------
    httpd, flaky_url = start_mock_server(flaky=1)  # 每次请求都故意少返回一条
    try:
        flaky_settings = settings.clone(
            output_dir=str(workdir / "flaky"),
            use_cache=False,
            concurrency=2,
            cache_file=str(workdir / "flaky" / "trans.sqlite3"),
        )
        flaky_settings.base_url = flaky_url
        setattr(flaky_settings, "_modes", [MODE_REPLACED])
        flaky_result = Pipeline(flaky_settings, log=lambda _m: None).run(sample)
        with fitz.open(flaky_result.outputs[0]) as doc:
            count = page_text(doc[0]).count("【译】")
        check(count >= 3, "模型返回条数不符时，二分重试仍拿到完整译文", f"命中 {count} 处")
    finally:
        httpd.shutdown()
        httpd.server_close()


# --------------------------------------------------------------------------- #
def test_alignment_and_splitting() -> None:
    """回归：对齐误判（正文被居中）与「标题+正文」被合并成一个块。

    真实案例来自 ACM 论文 3786623.pdf：
      * ``CCS Concepts: …`` 这类满版心的正文行，中心本来就贴着版心中心，
        曾因为只比较中心位置而被判成「居中」。
      * PyMuPDF 会把 ``1 Introduction`` 和紧跟的正文放进同一个文本块，
        整块翻译后标题被并进正文、字号也被统一。
    """
    print("\n[6] 对齐推断与块切分（回归）")
    from pdf_translator.extractor import (
        TextBlock, PdfExtractor, _guess_align, _line_style_changed, _LineInfo,
    )

    # ---- 多行块：两端对齐的正文不能被判成居中 ----
    justified = [(56.0, 0.0, 539.0, 10.0), (56.0, 12.0, 539.0, 22.0), (56.0, 24.0, 320.0, 34.0)]
    check(_guess_align(justified) == 0, "两端对齐的多行正文判为左对齐")
    centred_lines = [(150.0, 0.0, 445.0, 10.0), (180.0, 12.0, 415.0, 22.0)]
    check(_guess_align(centred_lines) == 1, "每行都居中的多行标题判为居中")

    # ---- 单行块：满版心的正文行不能被判成居中 ----
    blocks = [
        TextBlock("wide1", 0, (56.0, 60.0, 539.0, 78.0), "a full width body line", line_count=1),
        TextBlock("wide2", 0, (56.0, 90.0, 538.5, 108.0), "another full width line", line_count=1),
        TextBlock("label", 0, (56.0, 120.0, 538.0, 138.0), "CCS Concepts: a label", line_count=1),
        TextBlock("title", 0, (180.0, 150.0, 415.0, 168.0), "A centred title", line_count=1),
        TextBlock("foot", 0, (400.0, 800.0, 539.0, 818.0), "page footer", line_count=1),
    ]
    PdfExtractor._refine_alignment(blocks)
    by_id = {b.block_id: b.align for b in blocks}
    check(by_id["wide1"] == 0, "满版心正文行判为左对齐（不被居中）", f"实际 align={by_id['wide1']}")
    check(by_id["wide2"] == 0, "满版心正文行判为左对齐（第 2 例）", f"实际 align={by_id['wide2']}")
    check(by_id["label"] == 0, "满版心的 CCS 标签行判为左对齐", f"实际 align={by_id['label']}")
    check(by_id["title"] == 1, "两侧留白对称的标题判为居中", f"实际 align={by_id['title']}")
    check(by_id["foot"] == 2, "贴右边距的页脚判为右对齐", f"实际 align={by_id['foot']}")

    # ---- 样式变化判定：行内混排不能当作切分依据 ----
    def line(bold_ratio: float, size: float = 10.0) -> "_LineInfo":
        return _LineInfo(
            text="x", bbox=(0, 0, 100, 10), span_boxes=[(0, 0, 100, 10)],
            size=size, color=0, flags=0, font_name="F", horizontal=True,
            bold_ratio=bold_ratio,
        )

    check(_line_style_changed(line(1.0), line(0.0)) is True, "整行粗体 → 非粗体 判定为样式变化")
    check(_line_style_changed(line(0.0), line(0.5)) is False, "行内混排（ratio=0.5）不作为切分依据")
    check(_line_style_changed(line(0.0, 10.0), line(0.0, 14.0)) is True, "字号差异判定为样式变化")
    check(_line_style_changed(line(0.0, 9.96), line(0.0, 10.06)) is False, "字号几乎相同不切分")

    # ---- 块切分：粗体短标题 + 满宽正文 ----
    extractor = PdfExtractor(log=lambda _m: None)

    def span(text: str, size: float, flags: int, font: str, x0: float, x1: float) -> dict:
        return {"text": text, "size": size, "flags": flags, "font": font,
                "color": 0, "bbox": (x0, 0.0, x1, 0.0)}

    def ln(y0: float, y1: float, *spans: dict, x1: float = 539.0) -> dict:
        """构造一行；x1 是这一行的实际右边界（真实 PDF 里 line bbox 紧贴文本）。"""
        return {"dir": (1.0, 0.0), "bbox": (56.0, y0, x1, y1), "spans": list(spans)}

    heading_block = {
        "type": 0,
        "lines": [
            ln(60, 76, span("1 Introduction", 10.0, 16, "X-Bold", 56.0, 150.0), x1=150.0),
            ln(78, 94, span("Subgraph matching is a basic operator in graph analysis, and it "
                            "has been studied for decades.", 10.0, 4, "X-Reg", 56.0, 539.0)),
        ],
    }
    parts = extractor._build_text_blocks(heading_block, 0, 0)
    check(len(parts) == 2, "「短标题 + 正文」被切成两块", f"实际 {len(parts)} 块")
    if len(parts) == 2:
        check(parts[0].text == "1 Introduction", "第一块是标题本身", repr(parts[0].text))
        check(parts[0].is_bold is True, "标题块保留了粗体")
        check(parts[0].font_size == 10.0, "标题块字号正确")
        check("Subgraph matching" in parts[1].text, "第二块是正文")
        check(parts[1].is_bold is False, "正文块没有被误标成粗体")

    # 反面用例：正文段落中间出现整行粗体，前面已经堆了很多行 → 不许切
    long_body = {"type": 0, "lines": [
        ln(100 + i * 18, 116 + i * 18,
           span(f"Body line {i} of a normal paragraph that spans the full text width.",
                10.0, 4, "X-Reg", 56.0, 539.0))
        for i in range(6)
    ] + [
        ln(208, 224, span("This whole line happens to be typeset in a bold face but it is "
                          "still part of the same paragraph.", 10.0, 16, "X-Bold", 56.0, 539.0)),
        ln(226, 242, span("And the paragraph simply continues here afterwards as usual.",
                          10.0, 4, "X-Reg", 56.0, 539.0)),
    ]}
    body_parts = extractor._build_text_blocks(long_body, 0, 0)
    check(len(body_parts) == 1, "段落中间的整行粗体不触发切分（避免腰斩正文）",
          f"实际 {len(body_parts)} 块")


# --------------------------------------------------------------------------- #
def test_merge_captions_and_underlines(workdir: Path) -> None:
    """回归：真实论文（ACM SIGMOD）里踩到的三个版式坑。

    1. PyMuPDF 把「正文 + 行内数学公式」拆成多个 bbox 互相重叠的块，
       不合并就会各自翻译、各自清除原文，中文写上去和残留公式叠字。
    2. 图题（``Fig. 6. …``）紧贴被误判成表格的柱状图，被当成「与跳过区域重叠」
       整块丢掉，于是同一篇论文里有的图题翻译了、有的没翻译。
    3. 下划线是独立矢量对象，不会跟着译文走，译文一重排就错位。
    """
    print("\n[7] 重叠块合并 / 图题保留 / 下划线清除（回归）")
    from pdf_translator.builder import PdfBuilder
    from pdf_translator.config import Settings
    from pdf_translator.extractor import (
        PdfExtractor, TextBlock, _group_overlapping_blocks, _merge_block_group,
        fold_math_alphanumeric, is_caption, normalise_text,
    )

    # ---- 1. bbox 重叠的块必须合并 ----
    def raw_block(x0, y0, x1, y1, text):
        return {"type": 0, "bbox": (x0, y0, x1, y1), "lines": [{
            "dir": (1.0, 0.0), "bbox": (x0, y0, x1, y1),
            "spans": [{"text": text, "size": 10.0, "flags": 4, "font": "F",
                       "color": 0, "bbox": (x0, y0, x1, y1)}]}]}

    raw = [
        raw_block(50, 100, 500, 200, "We let"),          # 正文片段
        raw_block(50, 100, 500, 200, "n"),               # 行内公式，bbox 完全重叠
        raw_block(50, 300, 500, 400, "Separate paragraph"),  # 不相干的一段
    ]
    groups = _group_overlapping_blocks(raw)
    check(len(groups) == 2, "bbox 重叠的「正文 + 行内公式」被并成一组",
          f"实际 {len(groups)} 组")
    merged = _merge_block_group(groups[0])
    check(len(merged["lines"]) == 2, "合并后保留了双方的行", f"实际 {len(merged['lines'])} 行")

    # ---- 2. span 之间丢失的空格要补回来 ----
    extractor = PdfExtractor(log=lambda _m: None)
    line_info = extractor._parse_line({
        "dir": (1.0, 0.0), "bbox": (50, 100, 200, 114),
        "spans": [
            {"text": "We let ", "size": 10.0, "flags": 4, "font": "F", "color": 0,
             "bbox": (50, 100, 84, 114)},
            {"text": "n", "size": 10.0, "flags": 4, "font": "M", "color": 0,
             "bbox": (87, 100, 92, 114)},
            {"text": "and", "size": 10.0, "flags": 4, "font": "F", "color": 0,
             "bbox": (95, 100, 110, 114)},
        ],
    })
    check(line_info is not None and "n and" in line_info.text,
          "span 边界丢掉空格时按坐标空隙补回", repr(line_info.text if line_info else None))

    # ---- 3. 图题识别：真图题要认，正文引用句不能认 ----
    for text in ("Fig. 1. Example", "Figure 12: Results", "Table 2. Datasets",
                 "TABLE 3. Percentage", "图 9. 基于验证的方法"):
        check(is_caption(text), f"识别为图题：{text!r}")
    for text in ("Figure 7 further compares our implementation",
                 "Fig. 1 shows the pipeline [12]",
                 "Figure 1 exhibits our newly proposed structure"):
        check(not is_caption(text), f"不误判正文引用：{text[:34]!r}…")

    # ---- 4. Unicode 数学字母折回普通字母 ----
    folded = normalise_text("We let 𝑛 and 𝑒 be, e.g., 𝑐5 = |𝑁(𝑣)|")
    check("𝑛" not in folded and "n and e" in folded,
          "数学字母被折成普通字母", repr(folded))
    check(fold_math_alphanumeric("𝟏𝟐𝟑") == "123", "数学粗体数字折成 ASCII 数字")

    # ---- 5. 落在待重写块里的下划线要被盖掉 ----
    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text(fitz.Point(50, 120), "Underlined heading", fontsize=12, fontname="hebo")
    page.draw_line(fitz.Point(50, 124), fitz.Point(180, 124), color=(0, 0, 0), width=0.8)
    page.draw_line(fitz.Point(50, 124), fitz.Point(90, 124), color=(0, 0, 0), width=0.8)
    # 一条远离文字块的线，不该被误盖
    page.draw_line(fitz.Point(50, 260), fitz.Point(180, 260), color=(0, 0, 0), width=0.8)

    block = TextBlock("b1", 0, (48, 106, 190, 128), "带下划线的标题", align=0, line_count=1)
    builder = PdfBuilder(Settings())
    covered = builder._cover_text_rules(page, [block])
    check(covered >= 2, "文字块内的下划线被识别并盖掉", f"实际盖了 {covered} 条")

    # 覆盖是「画一层底色」而不是删除对象，所以要看渲染结果里还有没有深色像素。
    # 裁剪带只取线条所在的那 3pt，避开上方的文字。
    def dark_pixels(clip) -> int:
        pix = page.get_pixmap(clip=clip, dpi=144)
        count = 0
        for y in range(pix.height):
            for x in range(pix.width):
                r, g, b = pix.pixel(x, y)[:3]
                if r + g + b < 420:  # 明显深于纸白
                    count += 1
        return count

    check(dark_pixels(fitz.Rect(52, 123, 178, 126)) == 0,
          "块内下划线处已无深色像素（真的被盖住）",
          f"仍有 {dark_pixels(fitz.Rect(52, 123, 178, 126))} 个深色像素")
    check(dark_pixels(fitz.Rect(52, 259, 178, 262)) > 0,
          "块外的线仍在（没有误删其他内容）")
    doc.close()


# --------------------------------------------------------------------------- #
def test_figure_and_formula_skip(workdir: Path) -> None:
    """回归：图表内的标注和独立公式必须保持原样，不参与翻译。

    真实案例：ACM 论文里的图 9 是用线条画的（不是嵌入图片），里面的
    ``Search orders`` / ``Cost Model`` / ``φ0: u4 u0 u5 u1 u3 u2`` 都被当成
    正文翻译了。靠 ``cluster_drawings()`` 把图形区域框出来才能挡住它们。
    """
    print("\n[8] 图表标注与独立公式保持原样（回归）")
    from pdf_translator.extractor import PdfExtractor, looks_like_formula

    for text, font in (
        ("φ0: u4 u0 u5 u1 u3 u2", "CambriaMath"),
        ("n= 9 e= 11", "CambriaMath"),
        ("Δ = 2", "CambriaMath"),
        ("Final order φ2: u0 u3 u4 u2 u5 u1", "CambriaMath"),
    ):
        check(looks_like_formula(text, font), f"判定为独立公式：{text!r}")
    for text, font in (
        ("Practical cost estimation (ci,li ). Since deriving the precise cost", "LibertineMathMI"),
        ("We let n and e be the numbers of vertices and edges in the data graph", "LinLibertineT"),
        ("Fig. 9. Validation-based methods.", "LinBiolinumT"),
        ("Cost Model", "TimesNewRomanPSMT"),
    ):
        check(not looks_like_formula(text, font), f"不误判正文/图题：{text[:38]!r}…")

    # 造一个带矢量图表的页面
    doc = fitz.open()
    page = doc.new_page(width=500, height=700)
    page.insert_text(fitz.Point(50, 60),
                     "This paragraph is normal body text and must be translated.",
                     fontsize=10, fontname="helv")
    page.insert_text(fitz.Point(50, 660),
                     "Another body line far below the figure area.",
                     fontsize=10, fontname="helv")
    for i in range(12):  # 模拟柱状图的网格线
        page.draw_line(fitz.Point(100 + i * 20, 300), fitz.Point(100 + i * 20, 420), width=0.8)
        page.draw_line(fitz.Point(100, 300 + i * 10), fitz.Point(340, 300 + i * 10), width=0.8)
    page.insert_text(fitz.Point(150, 360), "Cost Model", fontsize=9, fontname="helv")
    page.insert_text(fitz.Point(200, 380), "Data graph", fontsize=9, fontname="helv")
    page.insert_text(fitz.Point(150, 450), "Fig. 3. A vector figure.", fontsize=9, fontname="helv")
    path = workdir / "figure_test.pdf"
    doc.save(str(path))
    doc.close()

    # 关掉表格检测，隔离出「图表区域」这一条判据
    layout = PdfExtractor(detect_tables=False).extract(path)
    texts = [b.text.strip() for b in layout.blocks]

    check(any("normal body text" in t for t in texts), "图表外的正文正常提取")
    check(any("far below" in t for t in texts), "图表下方的正文正常提取")
    check(not any("Cost Model" in t for t in texts), "图表内的坐标轴标签被跳过")
    check(not any("Data graph" in t for t in texts), "图表内的第二个标签被跳过")
    check(any("Fig. 3." in t for t in texts), "图题仍然保留（图题是要翻译的）")

    layout_all = PdfExtractor(detect_tables=False).extract(path, skip_figures=False)
    texts_all = [b.text.strip() for b in layout_all.blocks]
    check(any("Cost Model" in t for t in texts_all),
          "关闭该机制后图表标签会参与翻译（开关有效）")


# --------------------------------------------------------------------------- #
def test_untranslated_retry(sample: Path, workdir: Path) -> None:
    """回归：模型整段不译时，结果不能进缓存，并且要自动重试。

    真实故障：一次请求里塞了很多段，模型偶尔会原样返回其中几段。早期版本把这
    种「译文 == 原文」当成正常结果写进缓存，之后每次重跑都命中它，那一段就
    永远是英文了 —— 表现为「明明翻译过了，某些段落却还是英文」。
    """
    print("\n[9] 模型漏翻时不进缓存且自动重试（回归）")
    from pdf_translator.translator import TranslationCache, _should_cache

    check(_should_cache("Hello world", "你好世界") is True, "正常译文允许写缓存")
    check(_should_cache("Hello world", "Hello world") is False,
          "模型漏翻（译文 == 原文）不允许写缓存")
    check(_should_cache("1234", "1234") is True, "纯数字可以写缓存")
    check(_should_cache("[1]", "[1]") is True, "引用标记可以写缓存")

    httpd, url = start_mock_server(skip_once=True)
    try:
        cache_file = workdir / "retry_test" / "cache.sqlite3"
        settings = Settings()
        settings.input_pdf = str(sample)
        settings.output_dir = str(workdir / "retry_test")
        settings.engine = "custom"
        settings.base_url = url
        settings.api_key = "mock"
        settings.model = "mock"
        settings.target_lang = "zh"
        settings.concurrency = 2
        settings.use_cache = True
        settings.cache_file = str(cache_file)
        setattr(settings, "_modes", [MODE_REPLACED])

        result = Pipeline(settings, log=lambda _m: None).run(sample)
        with fitz.open(result.outputs[0]) as doc:
            text = "".join(doc[i].get_text() for i in range(doc.page_count))

        # 第一次请求被 mock 原样返回，重试后应该补翻
        check("【译】" in text, "重试后拿到了译文")
        body = "Retrieval-augmented generation (RAG) has become a standard technique"
        check(body not in text, "原本被漏翻的正文段落已被补翻", "该段仍是英文")

        cache = TranslationCache(path=cache_file, enabled=True)
        try:
            total = cache.count()
            same = cache._conn.execute(
                "SELECT COUNT(*) FROM translations WHERE source = target"
            ).fetchone()[0]
        finally:
            cache.close()
        check(total > 0, "缓存里确实写入了记录", f"{total} 条")
        check(same == 0, "缓存里没有任何「译文==原文」的条目", f"仍有 {same} 条")
    finally:
        httpd.shutdown()
        httpd.server_close()


# --------------------------------------------------------------------------- #
def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="pdf-translator-test-"))
    print(f"临时目录：{workdir}")

    sample = build_sample(workdir / "sample_en.pdf", pages=3)
    print(f"样例 PDF：{sample}")

    httpd, base_url = start_mock_server()
    print(f"Mock 服务：{base_url}")
    try:
        test_extraction(sample)
        test_translation_and_build(sample, workdir, base_url)
        test_cache_and_fallback(sample, workdir, base_url)
        test_alignment_and_splitting()
        test_merge_captions_and_underlines(workdir)
        test_figure_and_formula_skip(workdir)
        test_untranslated_retry(sample, workdir)
    finally:
        httpd.shutdown()
        httpd.server_close()

    print("\n" + "═" * 60)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("失败清单：")
        for item in FAILED:
            print("  - " + item)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
