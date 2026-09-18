"""翻译编排层：分批打包、并发调用、缓存、术语表、失败降级。

设计要点
--------
* **省 token**：把连续多个段落打包进一次请求，用 JSON 数组一一对应。
* **抗幻觉**：模型返回条数不符时把该批**二分递归重试**，最坏退化到单条翻译，
  保证「块 ↔ 译文」的对应关系绝不错位。
* **可续跑**：每条译文按 (原文, 目标语言, 模型, 术语表) 哈希写入 SQLite 缓存，
  中断后重跑不会重复烧钱。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .config import CJK_LANGS, TARGET_LANGUAGES, Settings, cache_dir
from .engines import (
    AuthError,
    OpenAICompatibleEngine,
    ResponseParseError,
    TranslationError,
    Usage,
    coerce_translation_list,
    extract_json_payload,
)
from .extractor import TextBlock, is_translatable, normalise_text

# --------------------------------------------------------------------------- #
# 术语表
# --------------------------------------------------------------------------- #


class Glossary:
    """原文 → 译文的强制对照表。"""

    def __init__(self, entries: dict[str, str] | None = None) -> None:
        self.entries: dict[str, str] = dict(entries or {})

    def __bool__(self) -> bool:
        return bool(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: str | Path | None) -> "Glossary":
        """支持 .json / .csv / .tsv / .txt（每行 ``原文=译文`` 或制表符分隔）。"""
        if not path:
            return cls()
        file = Path(path)
        if not file.exists():
            raise FileNotFoundError(f"术语表文件不存在：{file}")
        raw = file.read_text(encoding="utf-8-sig")
        entries: dict[str, str] = {}

        if file.suffix.lower() == ".json":
            data = json.loads(raw)
            if isinstance(data, dict):
                entries = {str(k).strip(): str(v).strip() for k, v in data.items()}
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        src = item.get("source") or item.get("src") or item.get("原文")
                        dst = item.get("target") or item.get("dst") or item.get("译文")
                        if src and dst:
                            entries[str(src).strip()] = str(dst).strip()
        else:
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                for sep in ("\t", "=>", "=", ",", "，"):
                    if sep in line:
                        src, _, dst = line.partition(sep)
                        src, dst = src.strip().strip('"'), dst.strip().strip('"')
                        if src and dst:
                            entries[src] = dst
                        break

        return cls({k: v for k, v in entries.items() if k and v})

    # ------------------------------------------------------------------ #
    def prompt_section(self, limit: int = 80) -> str:
        if not self.entries:
            return ""
        items = list(self.entries.items())[:limit]
        lines = "\n".join(f"- {src} → {dst}" for src, dst in items)
        more = "" if len(self.entries) <= limit else f"\n（另有 {len(self.entries) - limit} 条，同样必须遵守）"
        return (
            "\n\n【术语对照表 —— 遇到左侧词必须使用右侧译法，不得改译】\n" + lines + more
        )

    def apply(self, text: str) -> str:
        """本地兜底：确保术语表里的词一定出现在译文中。"""
        if not self.entries or not text:
            return text
        for src, dst in self.entries.items():
            if src in text and dst not in text:
                text = text.replace(src, dst)
        return text

    def signature(self) -> str:
        if not self.entries:
            return ""
        blob = json.dumps(self.entries, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# 缓存
# --------------------------------------------------------------------------- #


class TranslationCache:
    """基于 SQLite 的译文缓存（线程安全）。"""

    def __init__(self, path: Path | str | None = None, enabled: bool = True) -> None:
        self.enabled = enabled
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        if not enabled:
            return
        target = Path(path) if path else cache_dir() / "translations.sqlite3"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(target), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS translations ("
                " key TEXT PRIMARY KEY, source TEXT, target TEXT, model TEXT,"
                " created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
            )
            self._conn.commit()
        except sqlite3.Error:
            self._conn = None
            self.enabled = False

    # ------------------------------------------------------------------ #
    def get(self, key: str) -> str | None:
        if not self.enabled or self._conn is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT target FROM translations WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def put(self, key: str, source: str, target: str, model: str) -> None:
        if not self.enabled or self._conn is None or not target:
            return
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO translations (key, source, target, model) "
                    "VALUES (?, ?, ?, ?)",
                    (key, source, target, model),
                )
                self._conn.commit()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------------ #
    def purge_untranslated(self) -> int:
        """删掉「译文 == 原文」的缓存条目，返回删除条数。

        模型偶尔会整段不译，这类结果一旦缓存下来，之后每次重跑都会命中它、
        那一段就永远是原文了。把它们清掉，下次会重新尝试翻译。
        """
        if not self.enabled or self._conn is None:
            return 0
        with self._lock:
            try:
                cursor = self._conn.execute(
                    "DELETE FROM translations WHERE source = target AND LENGTH(source) > 8"
                )
                self._conn.commit()
                return int(cursor.rowcount or 0)
            except sqlite3.Error:
                return 0

    def clear(self) -> int:
        """清空整个缓存，返回删除条数。"""
        if not self.enabled or self._conn is None:
            return 0
        with self._lock:
            try:
                cursor = self._conn.execute("DELETE FROM translations")
                self._conn.commit()
                return int(cursor.rowcount or 0)
            except sqlite3.Error:
                return 0

    def count(self) -> int:
        if not self.enabled or self._conn is None:
            return 0
        with self._lock:
            try:
                return int(self._conn.execute("SELECT COUNT(*) FROM translations").fetchone()[0])
            except sqlite3.Error:
                return 0

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT_TEMPLATE = """你是一名资深的出版级文档翻译引擎。用户会给你一个 JSON 字符串数组，\
你要把数组里的每一段文本翻译成{target_lang}，然后**只输出一个 JSON 字符串数组**。

必须严格遵守：
1. 输出数组的元素个数、顺序必须与输入**完全一致**，一一对应，不得合并、拆分或遗漏。
2. 只翻译自然语言文字。以下内容必须原样保留，不要翻译、不要改写：
   - 数字、日期、货币、计量单位、数学公式与符号
   - 代码、变量名、命令行、文件路径、URL、邮箱
   - 参考文献引用标记（如 [1]、(Smith et al., 2020)）
   - 图表编号与标签（如 Figure 3、Table 2、Fig. 4a）
3. 保持原文的语气、人称与专业度；学术文本要严谨，营销文案要地道，不要逐字硬译。
4. 段落里**夹杂公式、变量或符号**时，请照常翻译其中的自然语言，并把符号原样保留在译文的
   相应位置。**不要因为一段里含公式就整段不译**，这是最常见的错误。
5. 不要在译文里添加任何解释、注释、译者按或 Markdown 代码块标记。
6. 如果某段文本**通篇**都是纯符号、纯数字、纯代码等确实无需翻译的内容，才原样返回该段。
7. 不要输出除 JSON 数组以外的任何字符。{glossary}{extra}"""


def build_system_prompt(settings: Settings, glossary: Glossary) -> str:
    target = TARGET_LANGUAGES.get(settings.target_lang, settings.target_lang)
    extra = ""
    if settings.extra_prompt.strip():
        extra = "\n\n【本次任务附加要求】\n" + settings.extra_prompt.strip()
    return SYSTEM_PROMPT_TEMPLATE.format(
        target_lang=target,
        glossary=glossary.prompt_section(),
        extra=extra,
    )


# --------------------------------------------------------------------------- #
# 任务与结果
# --------------------------------------------------------------------------- #


@dataclass
class BatchResult:
    translations: list[str]
    usage: Usage = field(default_factory=Usage)
    from_cache: int = 0
    failed: int = 0


class Translator:
    """把一组 :class:`TextBlock` 的 ``text`` 翻译后写回 ``translation``。"""

    def __init__(
        self,
        settings: Settings,
        engine: OpenAICompatibleEngine,
        *,
        glossary: Glossary | None = None,
        cache: TranslationCache | None = None,
        log: Callable[[str], None] | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.glossary = glossary or Glossary()
        if cache is not None:
            self.cache = cache
        else:
            cache_path = (settings.cache_file or "").strip() or None
            self.cache = TranslationCache(path=cache_path, enabled=settings.use_cache)
        self.log = log or (lambda _m: None)
        self.progress = progress or (lambda _d, _t: None)
        self.system_prompt = build_system_prompt(settings, self.glossary)
        self._progress_lock = threading.Lock()
        self._done = 0
        self._total = 0
        # 认证/配置类错误一旦出现，后续请求必然同样失败，记下来快速失败
        self._fatal: Exception | None = None

    # ------------------------------------------------------------------ #
    def _cache_key(self, text: str) -> str:
        blob = "\x1f".join(
            [
                text,
                self.settings.target_lang,
                self.settings.model,
                self.glossary.signature(),
                hashlib.sha256(self.settings.extra_prompt.encode("utf-8")).hexdigest()[:8],
            ]
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ #
    def translate_blocks(self, blocks: Sequence[TextBlock]) -> Usage:
        """就地翻译并回填 ``block.translation``。"""
        pending: list[TextBlock] = []
        skipped_lang = 0

        for block in blocks:
            text = normalise_text(block.text)
            if not text:
                block.translation = ""
                continue
            if not is_translatable(text):
                block.translation = text
                continue
            if self.settings.skip_translated and self._already_target_language(text):
                block.translation = text
                skipped_lang += 1
                continue
            cached = self.cache.get(self._cache_key(text))
            if cached is not None:
                block.translation = self.glossary.apply(cached)
                continue
            pending.append(block)

        if skipped_lang:
            self.log(f"· {skipped_lang} 个段落已是目标语言，自动跳过")

        if not pending:
            self.log("· 全部段落命中缓存或无需翻译")
            return Usage()

        batches = self._make_batches(pending)
        total_items = len(pending)
        self._total = total_items
        self._done = 0
        self.log(
            f"· 待翻译 {total_items} 个段落，打包成 {len(batches)} 个请求，"
            f"并发 {self.settings.concurrency}"
        )

        usage = Usage()
        with ThreadPoolExecutor(max_workers=self.settings.concurrency) as pool:
            futures = {pool.submit(self._run_batch, batch): batch for batch in batches}
            for future in as_completed(futures):
                batch = futures[future]
                try:
                    translations = future.result()
                except TranslationError as exc:
                    self.log(f"✗ 一批翻译失败（{len(batch)} 段）：{exc}")
                    for block in batch:
                        block.translation = block.text  # 失败时保留原文，绝不丢内容
                    with self._progress_lock:
                        self._done += len(batch)
                        self.progress(self._done, self._total)
                    continue

                for block, translated in zip(batch, translations):
                    source = normalise_text(block.text)
                    value = normalise_text(translated) or source
                    value = self.glossary.apply(value)
                    block.translation = value
                    if _should_cache(source, value):
                        self.cache.put(
                            self._cache_key(source), source, value, self.settings.model
                        )

                with self._progress_lock:
                    self._done += len(batch)
                    self.progress(self._done, self._total)

        # 模型偶尔会整段不译（一次请求塞太多段时尤其明显）。
        # 这些段落单独再问一次，别让它们就这么留在原文上。
        self._retry_untranslated(pending)

        usage.add(self.engine.usage)
        if self._fatal is not None:
            self.log(
                "⚠ 因为认证失败，有段落保留了原文。换一个可用的 API Key 后重跑即可，"
                "已经翻译好的部分会命中缓存、不会重复计费。"
            )
        return usage

    # ------------------------------------------------------------------ #
    def _retry_untranslated(self, pending: Sequence[TextBlock]) -> int:
        """把「译文 == 原文」且本应翻译的段落单独重试一遍。"""
        if self._fatal is not None:
            return 0
        missed = [
            block
            for block in pending
            if is_translatable(normalise_text(block.text))
            and (block.translation or "").strip() == normalise_text(block.text)
        ]
        if not missed:
            return 0

        self.log(f"· 有 {len(missed)} 段模型没有翻译（原样返回），正在逐段重试…")
        recovered = 0
        for block in missed:
            source = normalise_text(block.text)
            try:
                result = self._request_batch([source])
            except TranslationError:
                continue
            value = normalise_text(result[0]) if result else ""
            if not value or value == source:
                continue
            value = self.glossary.apply(value)
            block.translation = value
            self.cache.put(self._cache_key(source), source, value, self.settings.model)
            recovered += 1
        self.log(f"  重试补翻了 {recovered}/{len(missed)} 段")
        return recovered

    # ------------------------------------------------------------------ #
    def _already_target_language(self, text: str) -> bool:
        """粗判文本是否已经是目标语言，避免中译中。"""
        sample = text[:400]
        letters = [ch for ch in sample if ch.isalpha() or _is_cjk(ch)]
        if len(letters) < 4:
            return True
        cjk = sum(1 for ch in letters if _is_cjk(ch))
        ratio = cjk / len(letters)
        target = self.settings.target_lang
        if target in ("zh", "zh-TW"):
            return ratio > 0.75
        if target in ("ja", "ko"):
            return ratio > 0.9
        # 目标为拉丁语系：几乎没有 CJK 且含有大量 ASCII 字母
        ascii_letters = sum(1 for ch in letters if ch.isascii() and ch.isalpha())
        return ratio < 0.02 and ascii_letters / len(letters) > 0.85 and target == "en"

    # ------------------------------------------------------------------ #
    def _make_batches(self, blocks: Sequence[TextBlock]) -> list[list[TextBlock]]:
        """按顺序切分批次，控制单批段落数与字符数。"""
        max_items = self.settings.max_items_per_request
        max_chars = self.settings.max_chars_per_request
        batches: list[list[TextBlock]] = []
        current: list[TextBlock] = []
        chars = 0

        for block in blocks:
            length = len(block.text)
            if current and (len(current) >= max_items or chars + length > max_chars):
                batches.append(current)
                current, chars = [], 0
            current.append(block)
            chars += length
        if current:
            batches.append(current)
        return batches

    # ------------------------------------------------------------------ #
    def _run_batch(self, batch: Sequence[TextBlock]) -> list[str]:
        """翻译一批；失败时二分递归，最坏降级到逐条。"""
        texts = [normalise_text(b.text) for b in batch]
        if self._fatal is not None:
            return list(texts)  # 已经确定是配置类错误，别再徒劳地打请求
        if len(texts) == 1:
            return [self._translate_one(texts[0])]

        try:
            result = self._request_batch(texts)
            if len(result) == len(texts):
                return result
        except AuthError:
            # Key 无效 / 无权限：拆分重试没有意义，整批保留原文
            return list(texts)
        except TranslationError as exc:
            self.log(f"· 批次（{len(texts)} 段）整体失败，改为拆分重试：{exc}")

        if len(texts) == 2:
            return [self._translate_one(t) for t in texts]

        mid = len(batch) // 2
        left = self._run_batch(batch[:mid])
        right = self._run_batch(batch[mid:])
        return list(left) + list(right)

    # ------------------------------------------------------------------ #
    def _translate_one(self, text: str) -> str:
        if self._fatal is not None:
            return text
        try:
            result = self._request_batch([text])
            return result[0] if result else text
        except AuthError:
            return text
        except TranslationError as exc:
            self.log(f"✗ 单段翻译失败，保留原文：{exc}")
            return text

    # ------------------------------------------------------------------ #
    def _request_batch(self, texts: Sequence[str]) -> list[str]:
        """真正发一次 HTTP 请求，返回与输入等长的译文列表。"""
        payload = json.dumps(list(texts), ensure_ascii=False)
        messages = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": (
                    f"请翻译下面这个 JSON 数组里的 {len(texts)} 段文本，"
                    f"输出同样是 {len(texts)} 个元素的 JSON 数组：\n{payload}"
                ),
            },
        ]
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                reply = self.engine.chat(messages, max_tokens=_estimate_max_tokens(texts))
            except AuthError as exc:
                if self._fatal is None:
                    self._fatal = exc
                    self.log(
                        "✗ 认证失败（API Key 无效、过期或无权限），已停止后续请求。"
                        "本次未翻译的段落会保留原文。"
                    )
                raise
            try:
                parsed = extract_json_payload(reply.text)
                return coerce_translation_list(parsed, len(texts))
            except ResponseParseError as exc:
                last_error = exc
                if attempt == 0:
                    self.log(f"· 模型输出无法解析，重试一次：{exc}")
        raise ResponseParseError(str(last_error))


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3040 <= code <= 0x30FF
        or 0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xAC00 <= code <= 0xD7AF
    )


def _should_cache(source: str, value: str) -> bool:
    """判断一条译文值不值得写进缓存。

    **译文和原文一模一样时不要缓存**（除非源文本本来就无需翻译，比如纯符号、
    纯数字、参考文献编号）。模型偶尔会整段不译，如果把这种结果缓存下来，
    以后每次重跑都会命中它，那一段就永远是原文了 —— 这正是
    「明明翻译过了，某些段落却还是英文」的元凶。
    """
    if value != source:
        return True
    return not is_translatable(source)


def _estimate_max_tokens(texts: Sequence[str]) -> int:
    """给足输出空间：中文译文通常比英文原文更短，但仍留出余量。"""
    chars = sum(len(t) for t in texts)
    return max(256, min(8000, int(chars * 2.2) + 200))


__all__ = [
    "Glossary",
    "TranslationCache",
    "Translator",
    "BatchResult",
    "build_system_prompt",
]