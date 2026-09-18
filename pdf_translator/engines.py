"""AI 翻译引擎层：统一的 OpenAI 兼容 HTTP 客户端。

只需一个 base_url + api_key + model，即可对接 DeepSeek、OpenAI、Kimi、
通义千问、智谱、硅基流动、Ollama 以及任何自建兼容端点。

本模块不依赖 openai SDK，只用 requests 直接发 HTTP，最大限度减少安装负担。
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

import requests

# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #


class TranslationError(Exception):
    """翻译过程中的可预期错误（会被展示给用户）。"""


class AuthError(TranslationError):
    """API Key 无效或没有权限 —— 重试没有意义。"""


class RateLimitError(TranslationError):
    """触发限流，可以退避后重试。"""


class ResponseParseError(TranslationError):
    """模型返回的内容无法解析成预期结构。"""


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class Usage:
    """token 用量统计。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    requests: int = 0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.requests += other.requests

    def summary(self) -> str:
        return (
            f"{self.requests} 次请求 / 输入 {self.prompt_tokens} tokens / "
            f"输出 {self.completion_tokens} tokens / 合计 {self.total_tokens}"
        )


@dataclass
class ChatResult:
    text: str
    usage: Usage = field(default_factory=Usage)


# --------------------------------------------------------------------------- #
# HTTP 客户端
# --------------------------------------------------------------------------- #

_ENDPOINT_SUFFIX = "/chat/completions"


def build_endpoint(base_url: str) -> str:
    """把用户填写的 Base URL 规范化成完整的 chat/completions 地址。"""
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise TranslationError("尚未填写 API Base URL（例如 https://api.deepseek.com/v1）")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if url.endswith(_ENDPOINT_SUFFIX):
        return url
    # 允许用户只填主机名，自动补 /v1
    if not re.search(r"/v\d+(/|$)", url) and not url.endswith("/compatible-mode/v1"):
        if "api.deepseek.com" in url or "api.openai.com" in url:
            url += "/v1"
    return url + _ENDPOINT_SUFFIX


class OpenAICompatibleEngine:
    """一个极简但健壮的 OpenAI 兼容客户端。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: int = 180,
        temperature: float = 0.2,
        max_retries: int = 4,
        extra_headers: dict[str, str] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip()
        self.timeout = timeout
        self.temperature = temperature
        self.max_retries = max_retries
        self.extra_headers = extra_headers or {}
        self.log = log or (lambda _msg: None)
        self.usage = Usage()
        self.endpoint = build_endpoint(base_url)
        self._session = requests.Session()

    # ------------------------------------------------------------------ #
    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "pdf-translator/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)
        return headers

    # ------------------------------------------------------------------ #
    def _post_once(self, payload: dict[str, Any]) -> ChatResult:
        resp = self._session.post(
            self.endpoint,
            headers=self._headers(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=self.timeout,
        )
        if resp.status_code in (401, 403):
            raise AuthError(
                f"API Key 被拒绝（HTTP {resp.status_code}）。请检查 Key 是否正确、"
                f"是否已实名/开通对应模型。服务返回：{resp.text[:300]}"
            )
        if resp.status_code == 429:
            raise RateLimitError(f"触发限流（HTTP 429）：{resp.text[:200]}")
        if resp.status_code >= 500:
            raise RateLimitError(f"服务端错误（HTTP {resp.status_code}）：{resp.text[:200]}")
        if resp.status_code >= 400:
            raise TranslationError(f"请求被拒绝（HTTP {resp.status_code}）：{resp.text[:400]}")

        try:
            data = resp.json()
        except ValueError as exc:  # 非 JSON 响应
            raise TranslationError(f"服务返回了非 JSON 内容：{resp.text[:300]}") from exc

        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise TranslationError(f"接口返回错误：{msg}")

        try:
            choices = data["choices"]
            message = choices[0]["message"]
            content = message.get("content") or ""
            # 部分推理模型会把思考过程放在 reasoning_content，只取正文
            if not content and message.get("reasoning_content"):
                content = message["reasoning_content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise TranslationError(f"响应结构不符合 OpenAI 规范：{str(data)[:300]}") from exc

        usage = Usage()
        raw_usage = data.get("usage") or {}
        usage.prompt_tokens = int(raw_usage.get("prompt_tokens") or 0)
        usage.completion_tokens = int(raw_usage.get("completion_tokens") or 0)
        usage.total_tokens = int(raw_usage.get("total_tokens") or 0)
        if not usage.total_tokens:
            usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
        usage.requests = 1
        return ChatResult(text=content, usage=usage)

    # ------------------------------------------------------------------ #
    def chat(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> ChatResult:
        """发送一次对话请求，自动重试限流与网络抖动。"""
        if not self.model:
            raise TranslationError("尚未填写模型名称（例如 deepseek-chat）")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": self.temperature if temperature is None else temperature,
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                result = self._post_once(payload)
                self.usage.add(result.usage)
                return result
            except AuthError:
                raise
            except (RateLimitError, requests.RequestException, TranslationError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                # 指数退避 + 抖动；限流时退避更久
                base = 6.0 if isinstance(exc, RateLimitError) else 1.6
                delay = base * (2 ** attempt) + random.uniform(0, 1.2)
                delay = min(delay, 60.0)
                self.log(f"⚠ 请求失败（第 {attempt + 1} 次）：{exc}；{delay:.1f}s 后重试")
                time.sleep(delay)

        raise TranslationError(f"重试 {self.max_retries} 次后仍然失败：{last_error}")

    # ------------------------------------------------------------------ #
    def test_connection(self) -> str:
        """连通性自检，返回一行可展示的结果。"""
        result = self.chat(
            [{"role": "user", "content": "请只回复两个字：正常"}],
            temperature=0.0,
            max_tokens=16,
        )
        snippet = result.text.strip().replace("\n", " ")[:40]
        return f"连接成功 ✓  模型 {self.model} 回复：{snippet or '(空)'}"


# --------------------------------------------------------------------------- #
# 从模型输出里稳健地抠出 JSON 数组
# --------------------------------------------------------------------------- #

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def extract_json_payload(raw: str) -> Any:
    """从模型回复中解析出 JSON（容忍 markdown 代码块、前后缀说明）。"""
    text = (raw or "").strip()
    if not text:
        raise ResponseParseError("模型返回了空内容")

    candidates: list[str] = [text]
    fence = _FENCE_RE.search(text)
    if fence:
        candidates.insert(0, fence.group(1).strip())

    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue

    # 最后尝试：容忍结尾多余逗号
    for candidate in candidates:
        fixed = re.sub(r",\s*([\]}])", r"\1", candidate)
        try:
            return json.loads(fixed)
        except (json.JSONDecodeError, TypeError):
            continue

    raise ResponseParseError(f"无法把模型输出解析成 JSON：{text[:200]}")


def coerce_translation_list(payload: Any, expected: int) -> list[str]:
    """把解析结果规整成长度恰为 ``expected`` 的字符串列表。"""
    items: Any = payload
    if isinstance(payload, dict):
        for key in ("translations", "result", "results", "data", "items", "output"):
            if key in payload and isinstance(payload[key], (list, dict)):
                items = payload[key]
                break
        else:
            # {"1": "...", "2": "..."} 形式
            numeric = {k: v for k, v in payload.items() if str(k).strip().isdigit()}
            if numeric:
                items = [numeric[k] for k in sorted(numeric, key=lambda x: int(str(x)))]
            elif len(payload) == 1:
                items = next(iter(payload.values()))

    if isinstance(items, dict):
        numeric = {k: v for k, v in items.items() if str(k).strip().isdigit()}
        if numeric:
            items = [numeric[k] for k in sorted(numeric, key=lambda x: int(str(x)))]

    if isinstance(items, str):
        items = [items]

    if not isinstance(items, list):
        raise ResponseParseError(f"期望得到数组，实际得到 {type(items).__name__}")

    out: list[str] = []
    for element in items:
        if isinstance(element, str):
            out.append(element)
        elif isinstance(element, dict):
            # {"i":1,"t":"..."} / {"index":1,"text":"..."} / {"translation":"..."}
            value = None
            for key in ("t", "text", "translation", "translated", "target", "content", "zh"):
                if key in element and isinstance(element[key], str):
                    value = element[key]
                    break
            out.append(value if value is not None else json.dumps(element, ensure_ascii=False))
        elif element is None:
            out.append("")
        else:
            out.append(str(element))

    if len(out) != expected:
        raise ResponseParseError(f"返回条数 {len(out)} 与请求条数 {expected} 不一致")
    return out


def simple_translate(
    engine: OpenAICompatibleEngine,
    system_prompt: str,
    user_prompt: str,
    *,
    json_mode: bool = False,
) -> str:
    """一次性翻译调用（给术语抽取等小任务用）。"""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    return engine.chat(messages, json_mode=json_mode).text


def iter_chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]
