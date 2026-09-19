"""命令行入口：``python -m pdf_translator.cli`` 或根目录的 ``translate-pdf.bat``。

示例
----
    # 最常用：用 DeepSeek 把英文 PDF 翻成中文，同时输出两个版本
    python run_cli.py paper.pdf --api-key sk-xxx

    # 只生成双语对照版，只翻前 10 页，8 个并发
    python run_cli.py book.pdf -m bilingual --pages 1-10 -c 8

    # 使用本地 Ollama，不联网
    python run_cli.py doc.pdf --engine ollama --model qwen2.5:7b

    # 批量翻译一个目录下的所有 PDF
    python run_cli.py .\\papers\\*.pdf --to zh -o .\\out
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from pathlib import Path

from . import APP_TITLE, __version__
from .config import ENGINE_PRESETS, MODE_BILINGUAL, MODE_REPLACED, TARGET_LANGUAGES, Settings
from .pipeline import CancelledError, Pipeline, StageProgress


# --------------------------------------------------------------------------- #
# 彩色输出（Windows 终端友好，管道重定向时自动降级）
# --------------------------------------------------------------------------- #


def _supports_colour() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            return False
    return True


_COLOUR = _supports_colour()


def paint(text: str, colour: str) -> str:
    if not _COLOUR:
        return text
    codes = {"grey": "90", "red": "31", "green": "32", "yellow": "33", "blue": "36", "bold": "1"}
    return f"\033[{codes.get(colour, '0')}m{text}\033[0m"


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    presets = ", ".join(ENGINE_PRESETS)
    langs = ", ".join(TARGET_LANGUAGES)
    parser = argparse.ArgumentParser(
        prog="pdf-translate",
        description=f"{APP_TITLE} v{__version__} —— 保留原排版的 PDF 翻译工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  pdf-translate paper.pdf --api-key sk-xxx\n"
            "  pdf-translate book.pdf -m bilingual --pages 1-20 --engine deepseek\n"
            "  pdf-translate doc.pdf --engine ollama --model qwen2.5:7b --to zh\n"
        ),
    )
    parser.add_argument("inputs", nargs="*", help="一个或多个 PDF 文件（支持 *.pdf 通配符）")

    group = parser.add_argument_group("输出")
    group.add_argument("-o", "--output-dir", default="", help="输出目录（默认与源文件同目录）")
    group.add_argument(
        "-m", "--mode", default="both", choices=["both", "replaced", "bilingual"],
        help="输出模式：both=两个版本都出（默认）",
    )
    group.add_argument("--suffix", default="_译文", help="译文替换版文件名后缀")
    group.add_argument("--suffix-bilingual", default="_双语对照", help="双语对照版文件名后缀")

    group = parser.add_argument_group("翻译引擎")
    group.add_argument("--engine", default="deepseek", choices=list(ENGINE_PRESETS), help=f"预设：{presets}")
    group.add_argument("--base-url", default="", help="OpenAI 兼容接口的 Base URL（覆盖预设）")
    group.add_argument("--api-key", default="", help="API Key（也可用环境变量）")
    group.add_argument("--model", default="", help="模型名（覆盖预设）")
    group.add_argument("--temperature", type=float, default=None, help="采样温度，默认 0.2")
    group.add_argument("-c", "--concurrency", type=int, default=None, help="并发请求数，默认 4")
    group.add_argument("--timeout", type=int, default=None, help="单次请求超时秒数，默认 180")
    group.add_argument("--retries", type=int, default=None, help="失败重试次数，默认 4")
    group.add_argument("--test", action="store_true", help="只测试 API 连通性，不翻译")
    group.add_argument("--list-engines", action="store_true", help="列出所有内置引擎预设")

    group = parser.add_argument_group("翻译内容")
    group.add_argument("--to", dest="target_lang", default="zh", help=f"目标语言：{langs}")
    group.add_argument("--pages", default="", help='页码范围，如 "1-5,8,10-"；默认全部')
    group.add_argument("--glossary", default="", help="术语表文件（.json / .csv / .txt）")
    group.add_argument("--extra-prompt", default="", help="附加给模型的翻译要求")
    group.add_argument("--translate-tables", action="store_true", help="连表格里的文字一起翻译")
    group.add_argument(
        "--translate-figures", action="store_true",
        help="连图表内的标注（坐标轴、图例）和独立公式一起翻译；默认原样保留",
    )
    group.add_argument("--no-headers", action="store_true", help="不翻译页眉页脚")
    group.add_argument("--no-skip-same-lang", action="store_true", help="即使已是目标语言也强制送翻")
    group.add_argument(
        "--no-notes", action="store_true",
        help="不生成阅读笔记（默认会输出一份速读笔记 .md：问题定义/应用场合/贡献）",
    )
    group.add_argument("--batch-chars", type=int, default=None, help="单次请求的最大字符数，默认 2400")

    group = parser.add_argument_group("版面")
    group.add_argument(
        "--replace-mode", default="redact", choices=["redact", "cover"],
        help="译文替换版清除原文的方式：redact=真正删除（默认）/ cover=白底遮盖",
    )
    group.add_argument("--min-font-scale", type=float, default=None, help="译文最小字号比例，默认 0.62")
    group.add_argument("--line-spacing", type=float, default=None, help="行距倍数，默认 1.0")
    group.add_argument(
        "--bilingual-split", default="vertical", choices=["vertical", "horizontal"],
        help="双语版版式：vertical=左右分栏（默认）/ horizontal=上下分栏",
    )
    group.add_argument("--gap", type=float, default=None, help="双语版两栏间距（pt），默认 18")
    group.add_argument("--no-expand", action="store_true", help="不允许译文向下扩展")

    group = parser.add_argument_group("其它")
    group.add_argument("--no-cache", action="store_true", help="禁用译文缓存")
    group.add_argument("--cache-file", default="", help="指定译文缓存数据库路径")
    group.add_argument(
        "--purge-cache", action="store_true",
        help="清掉缓存里「译文==原文」的条目（模型漏翻留下的），然后退出",
    )
    group.add_argument("--clear-cache", action="store_true", help="清空整个译文缓存，然后退出")
    group.add_argument("--dry-run", action="store_true", help="只解析 PDF 并统计，不调用 API")
    group.add_argument("--json", dest="as_json", action="store_true", help="以 JSON 输出结果摘要")
    group.add_argument("-q", "--quiet", action="store_true", help="只输出警告与结果")
    group.add_argument("--version", action="version", version=f"{APP_TITLE} {__version__}")
    return parser


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def expand_inputs(patterns: list[str]) -> list[Path]:
    files: list[Path] = []
    for pattern in patterns:
        if any(ch in pattern for ch in "*?["):
            files.extend(Path(p) for p in sorted(glob.glob(pattern, recursive=True)))
        else:
            path = Path(pattern)
            if path.is_dir():
                files.extend(sorted(path.glob("*.pdf")))
            else:
                files.append(path)
    seen: set[str] = set()
    unique: list[Path] = []
    for file in files:
        key = str(file.resolve()).lower()
        if key not in seen:
            seen.add(key)
            unique.append(file)
    return unique


def settings_from_args(args: argparse.Namespace) -> Settings:
    settings = Settings()
    settings.apply_engine_preset(args.engine, keep_key=False)
    if args.base_url:
        settings.base_url = args.base_url
    if args.model:
        settings.model = args.model
    if args.api_key:
        settings.api_key = args.api_key

    settings.output_dir = args.output_dir
    settings.output_suffix = args.suffix
    settings.output_suffix_bilingual = args.suffix_bilingual
    settings.target_lang = args.target_lang
    settings.page_range = args.pages
    settings.glossary_path = args.glossary
    settings.extra_prompt = args.extra_prompt
    settings.translate_tables = args.translate_tables
    settings.translate_figures = args.translate_figures
    settings.translate_headers = not args.no_headers
    settings.skip_translated = not args.no_skip_same_lang
    settings.generate_notes = not args.no_notes
    settings.replace_mode = args.replace_mode
    settings.bilingual_split = args.bilingual_split
    settings.allow_expand_down = not args.no_expand
    settings.use_cache = not args.no_cache
    settings.cache_file = args.cache_file

    for attr, value in (
        ("concurrency", args.concurrency), ("timeout", args.timeout),
        ("max_retries", args.retries), ("temperature", args.temperature),
        ("min_font_scale", args.min_font_scale), ("line_spacing", args.line_spacing),
        ("bilingual_gap", args.gap), ("max_chars_per_request", args.batch_chars),
    ):
        if value is not None:
            setattr(settings, attr, value)

    settings.normalise()
    return settings


def run_one(
    pdf: Path,
    args: argparse.Namespace,
    settings: Settings,
    *,
    quiet: bool,
) -> dict:
    modes = {
        "both": [MODE_REPLACED, MODE_BILINGUAL],
        "replaced": [MODE_REPLACED],
        "bilingual": [MODE_BILINGUAL],
    }[args.mode]

    last_line = {"text": ""}

    def log(message: str) -> None:
        if quiet:
            return
        sys.stdout.write("\r" + " " * len(last_line["text"]) + "\r")
        print(message, flush=True)
        last_line["text"] = ""

    def progress(state: StageProgress) -> None:
        # 输出被重定向（非终端）时不画进度条，避免日志里塞满 \r
        if quiet or not sys.stdout.isatty():
            return
        if state.total:
            bar_len = 28
            ratio = min(1.0, state.current / max(1, state.total))
            bar = "█" * int(bar_len * ratio) + "░" * (bar_len - int(bar_len * ratio))
            line = f"   [{bar}] {state.current}/{state.total}  {state.stage}"
        else:
            line = f"   {state.stage}…"
        pad = max(0, len(last_line["text"]) - len(line))
        sys.stdout.write("\r" + line + " " * pad)
        sys.stdout.flush()
        last_line["text"] = line

    task_settings = settings.clone(input_pdf=str(pdf))
    setattr(task_settings, "_modes", modes)

    pipeline = Pipeline(task_settings, log=log, progress=progress)
    try:
        result = pipeline.run(pdf)
    finally:
        if last_line["text"]:
            sys.stdout.write("\r" + " " * len(last_line["text"]) + "\r")
            sys.stdout.flush()
    return {
        "source": result.source_pdf,
        "outputs": result.outputs,
        "blocks": result.blocks,
        "translated_blocks": result.translated_blocks,
        "characters": result.characters,
        "tokens": result.usage.total_tokens,
        "elapsed": round(result.elapsed, 2),
        "warnings": result.warnings,
        "stats": result.stats,
    }


def _force_utf8() -> None:
    """Windows 默认 GBK 控制台会把中文日志变成乱码，这里统一切到 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_engines:
        print(f"{APP_TITLE} v{__version__} —— 内置引擎预设\n")
        for key, preset in ENGINE_PRESETS.items():
            print(f"  {key:<12} {preset['label']}")
            print(f"  {'':<12} Base URL : {preset['base_url'] or '(自定义)'}")
            print(f"  {'':<12} 默认模型 : {preset['model'] or '(自定义)'}")
            print(f"  {'':<12} 环境变量 : {preset['env_key'] or '(不需要)'}")
            print(f"  {'':<12} 说明     : {preset['note']}\n")
        print("目标语言：" + "、".join(f"{k}({v})" for k, v in TARGET_LANGUAGES.items()))
        return 0

    try:
        settings = settings_from_args(args)
    except Exception as exc:
        print(paint(f"参数错误：{exc}", "red"), file=sys.stderr)
        return 2

    if args.purge_cache or args.clear_cache:
        from .translator import TranslationCache

        cache = TranslationCache(path=settings.cache_file or None, enabled=True)
        try:
            if args.clear_cache:
                removed = cache.clear()
                print(f"已清空译文缓存，删除 {removed} 条记录")
            else:
                removed = cache.purge_untranslated()
                print(f"已清除 {removed} 条「译文==原文」的无效缓存（模型漏翻留下的）")
            print(f"缓存现有 {cache.count()} 条记录")
        finally:
            cache.close()
        return 0

    if args.test:
        try:
            print(paint(f"正在测试 {settings.base_url} …", "blue"))
            message = Pipeline(settings).test_connection()
            print(paint(message, "green"))
            return 0
        except Exception as exc:
            print(paint(f"连接失败：{exc}", "red"), file=sys.stderr)
            return 1

    if not args.inputs:
        parser.print_help()
        return 2

    files = expand_inputs(args.inputs)
    if not files:
        print(paint("没有找到任何 PDF 文件。", "red"), file=sys.stderr)
        return 2
    missing = [f for f in files if not f.exists()]
    if missing:
        for path in missing:
            print(paint(f"找不到文件：{path}", "red"), file=sys.stderr)
        return 2

    if args.dry_run:
        from .extractor import PdfExtractor

        report = []
        for pdf in files:
            layout = PdfExtractor(log=lambda m: None).extract(pdf)
            stats = layout.stats()
            report.append({"file": str(pdf), **stats})
            print(
                f"{pdf.name}: {stats['pages']} 页 / 文字块 {stats['text_blocks']} / "
                f"{stats['characters']} 字符 / 跳过区域 {stats['skip_regions']}"
            )
        if args.as_json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if not args.quiet:
        print(paint(f"{APP_TITLE} v{__version__}", "bold"))
        print(f"共 {len(files)} 个文件，引擎 {settings.engine}（{settings.model}），"
              f"目标语言 {TARGET_LANGUAGES.get(settings.target_lang, settings.target_lang)}")
        print("─" * 64)

    reports: list[dict] = []
    failures = 0
    started = time.time()

    for index, pdf in enumerate(files, start=1):
        if not args.quiet:
            print(paint(f"\n[{index}/{len(files)}] {pdf.name}", "blue"))
        try:
            reports.append(run_one(pdf, args, settings, quiet=args.quiet))
        except CancelledError:
            print(paint("\n已中止。", "yellow"), file=sys.stderr)
            return 130
        except KeyboardInterrupt:
            print(paint("\n收到 Ctrl+C，已停止。", "yellow"), file=sys.stderr)
            return 130
        except Exception as exc:
            failures += 1
            print(paint(f"✗ {pdf.name} 处理失败：{exc}", "red"), file=sys.stderr)
            reports.append({"source": str(pdf), "error": str(exc)})

    total_time = time.time() - started
    if args.as_json:
        print(json.dumps(
            {"version": __version__, "elapsed": round(total_time, 2), "results": reports},
            ensure_ascii=False, indent=2,
        ))
    elif not args.quiet:
        print("\n" + "─" * 64)
        ok = sum(1 for r in reports if not r.get("error"))
        print(paint(f"完成：成功 {ok} / 失败 {failures}，总耗时 {total_time:.1f} 秒", "green" if not failures else "yellow"))
        for report in reports:
            if report.get("error"):
                continue
            for path in report.get("outputs", []):
                print(f"  → {path}")
            for warning in report.get("warnings", []):
                print(paint(f"  ⚠ {warning}", "yellow"))

    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
