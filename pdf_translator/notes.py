"""阅读笔记：按固定的三段式结构，为论文生成一份速读笔记。

结构由使用场景决定 —— 快速判断「这篇论文跟我有没有关系」，所以只要三节：

  ① 问题定义：输入、输出、约束
  ② 应用场合：真实场景、受众、意义
  ③ 贡献：相比已有工作新在哪里

全文控制在 3—5 行。喂给模型的不是全文，而是**标题 + 摘要/引言 + 结论**——
写这三节够用了，而且省 token。
"""

from __future__ import annotations

import re
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import TARGET_LANGUAGES, Settings
from .engines import OpenAICompatibleEngine, TranslationError
from .extractor import DocumentLayout

#: 结论类章节的标题样式
_CONCLUSION_RE = re.compile(
    r"^\s*(?:\d+\.?\s*)?(?:conclusion|conclusions|concluding remarks|"
    r"summary and|结论|总结|结语)",
    re.I,
)

#: 参考文献章节 —— 到了这里就不再往结论里收了
_REFERENCES_RE = re.compile(
    r"^\s*(?:\d+\.?\s*)?(?:references|bibliography|参考文献)\s*$", re.I
)

#: 摘要 / 引言这类「开篇」内容的页数上限
_HEAD_PAGES = 3
#: 默认喂给模型的字符上限
DEFAULT_MAX_CHARS = 9000

NOTE_HEADINGS = ("① 问题定义", "② 应用场合", "③ 贡献")

_SYSTEM_PROMPT = """你是一名资深研究者，正在为同行写一篇论文的速读笔记。\
请严格按下面三条来写，**全文合计 3—5 行**：

① 问题定义：这篇论文要解决什么问题？把**输入、输出、约束**用一两句话写清楚。
② 应用场合：这个问题出现在什么真实场景？谁会遇到、谁会用它？解决这个问题有什么意义？
③ 贡献：相比已有工作它新在哪里？（新问题 / 新方法 / 新实验发现，至少指明一条）

写作要求：
- 只输出「① 问题定义」「② 应用场合」「③ 贡献」这三节，不要前言、不要总结
- 每节 1—2 句，全文控制在 3—5 行；这是速读卡片，不是摘要复述
- 用{lang}撰写；专业术语首次出现时在括号里保留英文原词
- 要提炼不要照抄；避免「本文提出了一种…」这类没有信息量的句式
- 原文没有写清楚的地方，直接写「原文未明确说明」，**不要编造**
"""


@dataclass
class ReadingNotes:
    """一份阅读笔记。"""

    title: str
    source: str
    content: str
    model: str = ""
    target_lang: str = "zh"
    created: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))
    detailed: str = ""          # 章节精读（按小标题逐节总结）
    section_count: int = 0

    def to_markdown(self) -> str:
        lang = TARGET_LANGUAGES.get(self.target_lang, self.target_lang)
        lines = [
            f"# 阅读笔记：{self.title}",
            "",
            f"- **原文**：`{self.source}`",
            f"- **生成时间**：{self.created}",
            f"- **翻译引擎**：{self.model}",
            f"- **笔记语言**：{lang}",
        ]
        if self.section_count:
            lines.append(f"- **章节精读**：{self.section_count} 节")
        lines += [
            "",
            "---",
            "",
            "## 速读",
            "",
            self.content.strip(),
            "",
        ]
        if self.detailed.strip():
            lines += [
                "---",
                "",
                "## 章节精读",
                "",
                self.detailed.strip(),
                "",
            ]
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 挑材料
# --------------------------------------------------------------------------- #


def guess_title(layout: DocumentLayout) -> str:
    """猜标题：第一页字号最大、且足够长的那个块。"""
    if layout.title:
        return layout.title
    best, best_size = "", 0.0
    for page in layout.pages[:2]:
        for block in page.blocks:
            text = (block.text or "").strip()
            if len(text) < 8 or len(text) > 300:
                continue
            if block.font_size > best_size:
                best, best_size = text, block.font_size
    if best:
        return best.replace("\n", " ").strip()
    return Path(layout.path).stem


def collect_material(layout: DocumentLayout, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """挑出写笔记够用的片段：开篇（标题/摘要/引言）+ 结论。

    不送全文：笔记的三节内容几乎都集中在摘要、引言和结论里，把整篇论文塞进
    提示词既贵又容易让模型写偏。
    """
    blocks: list[tuple[int, object]] = []
    for page in layout.pages:
        for block in page.blocks:
            blocks.append((page.index, block))
    blocks.sort(key=lambda item: (item[0], item[1].bbox[1]))  # type: ignore[attr-defined]

    head: list[str] = []
    tail: list[str] = []
    in_conclusion = False

    for page_index, block in blocks:
        text = (block.text or "").strip()
        # 章节判定要在长度过滤**之前**做 —— 「6 Conclusion」这种标题很短，
        # 先按长度筛掉的话就永远进不了结论区。
        if _CONCLUSION_RE.match(text):
            in_conclusion = True
        elif in_conclusion and _REFERENCES_RE.match(text):
            break  # 结论之后是参考文献，再往后没有笔记需要的内容了
        if len(text) < 15:
            continue
        if in_conclusion:
            tail.append(text)
        elif page_index < _HEAD_PAGES:
            head.append(text)

    # 开篇和结论各自分配预算。只按总长度截断的话，前几页（标题页往往还夹着
    # 图 1 的分类标签）会把额度吃光，结论一个字都进不来。
    head_budget = int(max_chars * 0.6)
    tail_budget = max(600, max_chars - head_budget)

    head_text = "\n\n".join(head)[:head_budget].strip()
    tail_text = "\n\n".join(tail)[:tail_budget].strip()

    material = head_text
    if tail_text:
        material += "\n\n[...中间章节略...]\n\n" + tail_text
    return material.strip()


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #


def build_prompt(material: str, target_lang: str, filename: str = "") -> str:
    lang = TARGET_LANGUAGES.get(target_lang, target_lang)
    header = f"论文文件：{filename}\n\n" if filename else ""
    return (
        f"{header}下面是这篇论文的开头部分（标题、摘要、引言）与结尾部分（结论）。"
        f"请据此写出速读笔记：\n\n<<<论文内容开始>>>\n{material}\n<<<论文内容结束>>>"
    )


def generate_notes(
    engine: OpenAICompatibleEngine,
    layout: DocumentLayout,
    settings: Settings,
    *,
    log: Callable[[str], None] | None = None,
) -> ReadingNotes:
    """调用模型生成阅读笔记。"""
    log = log or (lambda _m: None)
    material = collect_material(layout)
    title = guess_title(layout)

    if len(material) < 200:
        raise TranslationError("这篇 PDF 里可用的文字太少，无法生成阅读笔记。")

    log(f"   取材 {len(material)} 字符（开篇 + 结论），标题识别为：{title[:60]}")

    system = _SYSTEM_PROMPT.format(
        lang=TARGET_LANGUAGES.get(settings.target_lang, settings.target_lang)
    )
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": build_prompt(material, settings.target_lang, Path(layout.path).name),
        },
    ]
    result = engine.chat(messages, temperature=0.3, max_tokens=1200)
    content = (result.text or "").strip()
    if not content:
        raise TranslationError("模型返回了空的阅读笔记。")

    _warn_missing_sections(content, log)
    return ReadingNotes(
        title=title,
        source=str(layout.path),
        content=content,
        model=settings.model,
        target_lang=settings.target_lang,
    )


def _warn_missing_sections(content: str, log: Callable[[str], None]) -> None:
    missing = [h for h in NOTE_HEADINGS if h not in content]
    if missing:
        log(f"   ⚠ 笔记里缺少小节：{'、'.join(missing)}（模型没有完全按格式输出）")


def save_notes(notes: ReadingNotes, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(notes.to_markdown(), encoding="utf-8")
    return target


def notes_path_for(pdf_path: str | Path, output_dir: str | Path, suffix: str = "_阅读笔记") -> Path:
    source = Path(pdf_path)
    return Path(output_dir) / f"{source.stem}{suffix}.md"


# --------------------------------------------------------------------------- #
# 章节精读：按小标题切分正文，逐节总结
# --------------------------------------------------------------------------- #

DEFAULT_SECTION_CHARS = 3500
DEFAULT_MAX_SECTIONS = 30

#: 章节小标题。不同出版社的编号习惯差别很大，这里都要认：
#:   ``1 Introduction`` / ``3.2 Filter-based``  —— ACM 与多数期刊
#:   ``I. INTRODUCTION``                        —— IEEE 一级标题（罗马数字）
#:   ``A. Applications`` / ``1) Scenarios``     —— IEEE 的子标题
#: 字母编号**必须带点**（``A.``），否则正文里的「A Survey of…」会被误判成标题。
_SECTION_RE = re.compile(
    r"^\s*("
    r"\d+(?:\.\d+){0,3}\s*[.)]?"      # 1 / 1. / 3.2.1 / 1)
    r"|[IVXLCDM]{1,7}\s*\."           # I. / II. / IV.
    r"|[A-Z]\s*\."                    # A. / B.
    r")\s+"
    r"([A-Z\u4e00-\u9fff][^\n]{2,110})$"
)

#: 伪代码、公式行、坐标轴刻度的特征 —— 它们常以数字开头，会被误当成小标题
_CODEISH_RE = re.compile(
    r"[∈←→×÷≤≥∑∏∫√∀∃∇∂(){}\\]|for each|\bdo\b|\bfor\b|\bFunction\b|\breturn\b|\bend\b",
    re.I,
)

_SECTION_PROMPT = """你是一名资深研究者，正在为同行撰写论文的精读笔记。\
下面给出论文中某一小节的完整内容，请**只针对这一节**写摘要。

严格按下面的格式输出（Markdown，不要任何前后缀、不要复述全文）：

**主题**：这一节在讲什么？一句话说清。

**专有名词**：逐一列出本节出现的专业术语，格式为「术语 —— 一句话解释」，用分号隔开；确实没有就写「无」。

**关键内容**：
- 方法/方案/算法的核心思路，或某个公式的含义（用文字讲清楚它算的是什么，不要照抄符号）
- 图表：图 X / 表 Y 展示了什么，它想说明什么问题
- 数据：若有实验数据，说明谁比谁高或低、变化趋势如何、这些数字意味着什么

要求：
- 只写这一节，不要提及其他章节，不要写「本节介绍了…」这类空话
- **有数据的地方必须解释数据的意义**（对比、趋势、量级），不要只把数字抄一遍
- 用{lang}撰写；专业术语首次出现时保留英文原词
- 原文没有提到的内容不要编造；该节确实没有图表或数据时，对应那一条写「无」
"""


@dataclass
class Section:
    """论文中的一个小节。"""

    number: str
    title: str
    page: int
    text: str

    @property
    def heading(self) -> str:
        return f"{self.number} {self.title}".strip()


def _heading_of(
    block: object, body_size: float, page_height: float = 0.0
) -> tuple[str, str] | None:
    """判断一个块是不是章节小标题，是则返回 (编号, 标题文字)。

    字号**不能**当作主要判据：实测 IEEE 期刊里 ``I. INTRODUCTION`` 只有 7.97pt，
    而正文中位字号是 9.96pt —— 标题比正文还小。所以这里以「编号格式」为主，
    字号只用来排除明显更小的东西（页脚、脚注、图表刻度）。
    """
    text = (getattr(block, "text", "") or "").strip()
    if not text or len(text) > 120:
        return None
    match = _SECTION_RE.match(text)
    if not match:
        return None
    if _CODEISH_RE.search(text):
        return None  # 伪代码行、公式行
    number, title = match.group(1).strip(), match.group(2).strip()

    # 页眉页脚：贴着页面上下边缘。它们常以页码开头（「5106 IEEE TRANSACTIONS…」），
    # 光看编号格式会误判成章节标题。
    if page_height > 0:
        top = float(getattr(block, "bbox", (0, 0, 0, 0))[1])
        bottom = float(getattr(block, "bbox", (0, 0, 0, 0))[3])
        if bottom <= page_height * 0.10 or top >= page_height * 0.90:
            return None

    # 明显比正文小的一律不算（脚注、图表刻度）
    size = float(getattr(block, "font_size", 0.0))
    if size and body_size and size < body_size * 0.80:
        return None

    return number, title


def _is_two_column(layout: DocumentLayout) -> bool:
    """判断是不是双栏排版（IEEE 期刊基本都是）。

    双栏文档如果直接按 y 排序，左右栏的内容会交织在一起：右栏顶部的子标题
    会排到左栏底部的正文前面，章节归属就乱了。
    """
    left = right = total = 0
    for page in layout.pages:
        half = page.width / 2
        for block in page.blocks:
            bbox = getattr(block, "bbox", None)
            if not bbox:
                continue
            centre = (bbox[0] + bbox[2]) / 2
            total += 1
            if centre < half * 0.9:
                left += 1
            elif centre > half * 1.1:
                right += 1
    if total < 20:
        return False
    return left / total > 0.3 and right / total > 0.3


def split_sections(
    layout: DocumentLayout,
    *,
    max_chars: int = DEFAULT_SECTION_CHARS,
    max_sections: int = DEFAULT_MAX_SECTIONS,
) -> list[Section]:
    """按小标题把正文切成若干节。"""
    two_column = _is_two_column(layout)
    blocks: list[tuple[int, int, float, object]] = []
    for page in layout.pages:
        half = page.width / 2
        for block in page.blocks:
            bbox = getattr(block, "bbox", (0, 0, 0, 0))
            # 双栏时把「栏」插进排序键，保证先读左栏再读右栏
            column = 0
            if two_column:
                column = 1 if (bbox[0] + bbox[2]) / 2 >= half else 0
            blocks.append((page.index, column, float(page.height), block))
    blocks.sort(key=lambda item: (item[0], item[1], item[3].bbox[1]))  # type: ignore[attr-defined]

    sizes = [float(getattr(b, "font_size", 0.0)) for _p, _c, _h, b in blocks]
    body_size = statistics.median(sizes) if sizes else 10.0

    sections: list[Section] = []
    current: dict | None = None

    def flush() -> None:
        if not current:
            return
        text = "\n\n".join(current["parts"])[:max_chars].strip()
        if text:
            sections.append(
                Section(current["number"], current["title"], current["page"], text)
            )

    for page_index, _column, page_height, block in blocks:
        head = _heading_of(block, body_size, page_height)
        if head:
            flush()
            current = {
                "number": head[0], "title": head[1],
                "page": page_index + 1, "parts": [],
            }
            continue
        if current is None:
            continue  # 第一个小标题之前是标题页/摘要，不属于任何一节
        text = (getattr(block, "text", "") or "").strip()
        if len(text) >= 20:
            current["parts"].append(text)

    flush()
    return sections[:max_sections]


def summarise_section(
    engine: OpenAICompatibleEngine, section: Section, target_lang: str
) -> str:
    """为单个小节生成摘要。"""
    lang = TARGET_LANGUAGES.get(target_lang, target_lang)
    messages = [
        {"role": "system", "content": _SECTION_PROMPT.format(lang=lang)},
        {
            "role": "user",
            "content": (
                f"小节标题：{section.heading}\n\n"
                f"<<<小节内容开始>>>\n{section.text}\n<<<小节内容结束>>>"
            ),
        },
    ]
    result = engine.chat(messages, temperature=0.3, max_tokens=900)
    return (result.text or "").strip()


def summarise_sections(
    engine: OpenAICompatibleEngine,
    sections: list[Section],
    settings: Settings,
    *,
    log: Callable[[str], None] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[str, int]:
    """并发为每一节生成摘要，返回 (Markdown 文本, 成功节数)。"""
    log = log or (lambda _m: None)
    progress = progress or (lambda _d, _t: None)
    if not sections:
        return "", 0

    results: dict[str, str] = {}
    done = 0
    workers = max(1, min(int(settings.concurrency or 3), 6))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(summarise_section, engine, section, settings.target_lang): section
            for section in sections
        }
        for future in as_completed(futures):
            section = futures[future]
            try:
                text = future.result()
                if text:
                    results[section.heading] = text
            except TranslationError as exc:
                log(f"   ⚠ 小节「{section.heading}」总结失败：{exc}")
            done += 1
            progress(done, len(sections))

    lines: list[str] = []
    for section in sections:
        body = results.get(section.heading)
        if not body:
            continue
        lines += [f"### {section.heading}", "", f"*（第 {section.page} 页）*", "", body, ""]
    return "\n".join(lines).strip(), len(results)


__all__ = [
    "ReadingNotes",
    "Section",
    "NOTE_HEADINGS",
    "DEFAULT_MAX_SECTIONS",
    "DEFAULT_SECTION_CHARS",
    "collect_material",
    "generate_notes",
    "guess_title",
    "notes_path_for",
    "save_notes",
    "split_sections",
    "summarise_section",
    "summarise_sections",
]
