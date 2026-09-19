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

    def to_markdown(self) -> str:
        lang = TARGET_LANGUAGES.get(self.target_lang, self.target_lang)
        lines = [
            f"# 阅读笔记：{self.title}",
            "",
            f"- **原文**：`{self.source}`",
            f"- **生成时间**：{self.created}",
            f"- **翻译引擎**：{self.model}",
            f"- **笔记语言**：{lang}",
            "",
            "---",
            "",
            self.content.strip(),
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


__all__ = [
    "ReadingNotes",
    "NOTE_HEADINGS",
    "collect_material",
    "generate_notes",
    "guess_title",
    "notes_path_for",
    "save_notes",
]
