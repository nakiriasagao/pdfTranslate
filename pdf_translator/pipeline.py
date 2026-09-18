"""端到端流程编排：解析 → 翻译 → 重建 → 报告。

GUI 和 CLI 都只调用这里的 :class:`Pipeline`，保证两条入口行为完全一致。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .builder import PdfBuilder
from .config import (
    ENGINE_PRESETS,
    MODE_BILINGUAL,
    MODE_REPLACED,
    TARGET_LANGUAGES,
    Settings,
)
from .engines import OpenAICompatibleEngine, Usage
from .extractor import DocumentLayout, PdfExtractor, parse_page_range
from .translator import Glossary, TranslationCache, Translator

# --------------------------------------------------------------------------- #
# 回调与结果
# --------------------------------------------------------------------------- #

LogFn = Callable[[str], None]


class CancelledError(Exception):
    """用户主动中止。"""


@dataclass
class StageProgress:
    stage: str = ""
    current: int = 0
    total: int = 0
    detail: str = ""


@dataclass
class PipelineResult:
    source_pdf: str = ""
    outputs: list[str] = field(default_factory=list)
    blocks: int = 0
    translated_blocks: int = 0
    characters: int = 0
    usage: Usage = field(default_factory=Usage)
    elapsed: float = 0.0
    stats: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"源文件：{Path(self.source_pdf).name}",
            f"文字块：{self.blocks} 个（送翻 {self.translated_blocks} 个，共 {self.characters} 字符）",
            f"耗时：{self.elapsed:.1f} 秒",
            f"Token：{self.usage.summary()}",
        ]
        for path in self.outputs:
            lines.append(f"输出：{path}")
        if self.warnings:
            lines.append("提示：")
            lines.extend(f"  - {w}" for w in self.warnings)
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


class Pipeline:
    """一次完整的 PDF 翻译任务。"""

    def __init__(
        self,
        settings: Settings,
        *,
        log: LogFn | None = None,
        progress: Callable[[StageProgress], None] | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.log = log or (lambda _m: None)
        self.progress = progress or (lambda _p: None)
        self.cancel = cancel or (lambda: False)

    # ------------------------------------------------------------------ #
    def _check_cancel(self) -> None:
        if self.cancel():
            raise CancelledError("任务已被用户中止")

    def _emit(self, stage: str, current: int = 0, total: int = 0, detail: str = "") -> None:
        self.progress(StageProgress(stage=stage, current=current, total=total, detail=detail))

    # ------------------------------------------------------------------ #
    def make_engine(self) -> OpenAICompatibleEngine:
        api_key = self.settings.resolved_api_key()
        preset = ENGINE_PRESETS.get(self.settings.engine, {})
        if not api_key and self.settings.engine != "ollama":
            key_url = preset.get("key_url") or ""
            hint = f"（可到 {key_url} 申请）" if key_url else ""
            raise ValueError(
                f"尚未填写 {preset.get('label', self.settings.engine)} 的 API Key{hint}。\n"
                f"也可以设置环境变量 {preset.get('env_key', 'API_KEY')}。"
            )
        return OpenAICompatibleEngine(
            base_url=self.settings.base_url,
            api_key=api_key,
            model=self.settings.model,
            timeout=self.settings.timeout,
            temperature=self.settings.temperature,
            max_retries=self.settings.max_retries,
            log=self.log,
        )

    # ------------------------------------------------------------------ #
    def test_connection(self) -> str:
        engine = self.make_engine()
        return engine.test_connection()

    # ------------------------------------------------------------------ #
    def run(self, input_pdf: str | Path | None = None) -> PipelineResult:
        started = time.time()
        source = Path(input_pdf or self.settings.input_pdf)
        if not source.exists():
            raise FileNotFoundError(f"找不到 PDF：{source}")

        result = PipelineResult(source_pdf=str(source))
        self.log(f"▶ 开始处理：{source.name}")

        # ---------- 1. 解析版面 ---------- #
        self._emit("解析 PDF", 0, 1)
        self.log("① 解析 PDF 版面，定位文字块与图片/表格区域…")
        extractor = PdfExtractor(log=self.log)
        layout = self._extract(extractor, source, result)
        stats = layout.stats()
        self.log(
            f"   共 {stats['pages']} 页，提取文字块 {stats['text_blocks']} 个"
            f"（{stats['characters']} 字符），"
            f"识别跳过区域 {stats['skip_regions']} 处（图片/表格不翻译）"
        )
        result.stats = dict(stats)
        result.blocks = stats["text_blocks"]
        result.characters = stats["characters"]
        self._check_cancel()

        if stats["pages_with_text"] == 0:
            result.warnings.append(
                "这份 PDF 没有可提取的文字层（可能是扫描件），需要先做 OCR 才能翻译。"
            )
            self.log("⚠ 未发现文字层，可能是扫描版 PDF。已直接复制原文件。")
            result.outputs.append(str(self._fallback_copy(source)))
            result.elapsed = time.time() - started
            return result

        # 统计实际送翻的块
        blocks_to_translate = [
            b
            for b in layout.blocks
            if (b.text or "").strip()
            and not (b.is_header_footer and not self.settings.translate_headers)
        ]
        if not self.settings.translate_headers:
            for page in layout.pages:
                for block in page.blocks:
                    if block.is_header_footer and not block.translation:
                        block.translation = block.text
        result.translated_blocks = len(blocks_to_translate)

        # ---------- 2. 翻译 ---------- #
        self._emit("翻译中", 0, max(1, len(blocks_to_translate)))
        glossary = Glossary.load(self.settings.glossary_path or None)
        if glossary:
            self.log(f"② 载入术语表 {len(glossary)} 条，将在提示词中强制生效")
        else:
            self.log("② 调用大模型翻译…")
        target_label = TARGET_LANGUAGES.get(self.settings.target_lang, self.settings.target_lang)
        self.log(f"   引擎 {self.settings.model} → 目标语言：{target_label}")

        engine = self.make_engine()
        cache = TranslationCache(
            path=(self.settings.cache_file or "").strip() or None,
            enabled=self.settings.use_cache,
        )
        if self.settings.use_cache:
            self.log(f"   缓存已启用（现有 {cache.count()} 条记录）")

        try:
            translator = Translator(
                self.settings,
                engine,
                glossary=glossary,
                cache=cache,
                log=self.log,
                progress=self._translate_progress,
            )
            usage = translator.translate_blocks(blocks_to_translate)
            result.usage = engine.usage
            self._emit("翻译中", len(blocks_to_translate), len(blocks_to_translate))
            self.log(f"   翻译完成，{usage.summary() if usage.requests else engine.usage.summary()}")
        finally:
            cache.close()
        self._check_cancel()

        # ---------- 3. 生成 PDF ---------- #
        builder = PdfBuilder(self.settings, log=self.log)
        out_dir = Path(self.settings.output_dir) if self.settings.output_dir else source.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = source.stem

        modes = self._requested_modes()
        for mode in modes:
            self._check_cancel()
            if mode == MODE_REPLACED:
                self.log("③ 生成「译文替换版」PDF（保留原排版）…")
                out_path = out_dir / f"{stem}{self.settings.output_suffix}.pdf"
                pdf = builder.build_replaced_pdf(
                    layout, out_path, progress=lambda d, t: self._emit("重建排版", d, t, out_path.name)
                )
            else:
                self.log("③ 生成「双语对照版」PDF（原文/译文对照）…")
                out_path = out_dir / f"{stem}{self.settings.output_suffix_bilingual}.pdf"
                pdf = builder.build_bilingual_pdf(
                    layout, out_path, progress=lambda d, t: self._emit("生成对照版", d, t, out_path.name)
                )
            result.outputs.append(str(pdf))
            self.log(f"   ✓ 已写出 {pdf}")

        result.elapsed = time.time() - started
        if builder.stats["overflow"]:
            result.warnings.append(
                f"有 {builder.stats['overflow']} 个段落的译文较长，已缩到最小字号仍略超出原区域，"
                f"可调低最小字号比例或改用双语对照版。"
            )
        self._emit("完成", 1, 1)
        self.log(f"✅ 全部完成，耗时 {result.elapsed:.1f} 秒")
        return result

    # ------------------------------------------------------------------ #
    def _translate_progress(self, done: int, total: int) -> None:
        self._check_cancel()
        self._emit("翻译中", done, total)

    def _requested_modes(self) -> list[str]:
        raw = getattr(self.settings, "_modes", None)
        if raw:
            return list(raw)
        return [MODE_REPLACED, MODE_BILINGUAL]

    # ------------------------------------------------------------------ #
    def _extract(
        self, extractor: PdfExtractor, source: Path, result: PipelineResult
    ) -> DocumentLayout:
        page_indices = None
        if self.settings.page_range.strip():
            from ._compat import fitz

            with fitz.open(str(source)) as doc:
                page_indices = parse_page_range(self.settings.page_range, doc.page_count)
            if not page_indices:
                raise ValueError(f"页码范围「{self.settings.page_range}」没有匹配到任何页面")
            self.log(f"   仅处理第 {self.settings.page_range} 页，共 {len(page_indices)} 页")

        # 表格识别只在需要时开启（略慢）
        extractor.detect_tables = True
        return extractor.extract(
            source,
            page_indices=page_indices,
            translate_headers=self.settings.translate_headers,
            skip_images=True,
            skip_tables=not self.settings.translate_tables,
            skip_figures=not self.settings.translate_figures,
        )

    # ------------------------------------------------------------------ #
    def _fallback_copy(self, source: Path) -> Path:
        import shutil

        out_dir = Path(self.settings.output_dir) if self.settings.output_dir else source.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"{source.stem}{self.settings.output_suffix}.pdf"
        shutil.copy2(source, target)
        return target


# --------------------------------------------------------------------------- #
# 便捷函数
# --------------------------------------------------------------------------- #


def quick_translate(
    input_pdf: str,
    *,
    api_key: str = "",
    engine: str = "deepseek",
    target_lang: str = "zh",
    output_dir: str = "",
    modes: Sequence[str] = (MODE_REPLACED, MODE_BILINGUAL),
    log: LogFn | None = None,
    progress: Callable[[StageProgress], None] | None = None,
    **overrides: Any,
) -> PipelineResult:
    """最简调用方式（给脚本用）。"""
    settings = Settings()
    settings.apply_engine_preset(engine, keep_key=False)
    settings.input_pdf = input_pdf
    settings.api_key = api_key
    settings.target_lang = target_lang
    settings.output_dir = output_dir
    for key, value in overrides.items():
        if hasattr(settings, key):
            setattr(settings, key, value)
    settings.normalise()
    setattr(settings, "_modes", list(modes))
    return Pipeline(settings, log=log, progress=progress).run()


__all__ = ["Pipeline", "PipelineResult", "StageProgress", "CancelledError", "quick_translate"]
