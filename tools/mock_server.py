"""本地 Mock 翻译服务：完整模拟 OpenAI 兼容接口，用于**离线**验证排版效果。

有了它，不填 API Key、不联网也能跑通整条流水线，检查字号缩放、双语分栏是否正确。

    # 单机启动（默认 http://127.0.0.1:8123）
    python tools/mock_server.py --port 8123

    # 然后另开一个窗口
    python run_cli.py samples/sample_en.pdf --engine custom ^
        --base-url http://127.0.0.1:8123/v1 --model mock --api-key mock

    # 压测降级逻辑：每 5 次请求故意返回错误条数
    python tools/mock_server.py --flaky 5
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# 伪翻译素材：模拟「英文原文 → 中文译文」后的长度变化
_ZH_FILLER = (
    "检索增强生成已经成为让大语言模型对接外部知识的通用做法。系统不再单纯依赖模型内部的"
    "参数化记忆，而是先从文档库中检索出相关段落，再以此为条件生成答案。这样做可以显著降低"
    "在开放域问答等知识密集型任务上的幻觉现象。然而检索质量只解决了一半问题，生成器还必须"
    "学会忽略干扰项、把结论归因到检索到的证据上，并在证据不足时主动拒答。最近的研究表明，"
    "简单堆叠更多段落并不会单调提升准确率，当上下文窗口被无关文本淹没时甚至会变差。本文"
    "提出一个轻量级的重排序阶段，作用在稠密检索器返回的前 k 个候选上。该重排序模型使用"
    "十二万条问答对以对比学习目标训练，不需要任何额外标注数据。"
)


def fake_translate(text: str, index: int = 0) -> str:
    """把一段文本变成「看起来像中文译文」的内容，长度约为原文的 0.5~0.7 倍。"""
    if not re.search(r"[A-Za-z]", text):
        return text  # 纯符号/数字原样返回，与真实模型行为一致
    letters = len(re.findall(r"[A-Za-z]", text))
    if letters / max(1, len(text)) < 0.35:
        return text
    target_len = max(8, int(len(text) * 0.55))
    start = (index * 17) % max(1, len(_ZH_FILLER) - target_len - 1)
    body = _ZH_FILLER[start : start + target_len]
    return f"【译】{body}"


class _Handler(BaseHTTPRequestHandler):
    server_version = "MockOpenAI/1.0"
    protocol_version = "HTTP/1.1"

    # 由 start_mock_server 注入
    flaky: int = 0
    skip_once: bool = False          # 模拟模型漏翻：每段第一次请求时原样返回
    skipped_once: set = set()        # 已经「漏翻」过的文本
    counter = {"n": 0}
    lock = threading.Lock()
    verbose: bool = False

    # ------------------------------------------------------------------ #
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if self.verbose:
            print("[mock] " + fmt % args)

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------ #
    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            self._send(200, {"object": "list", "data": [{"id": "mock", "object": "model"}]})
        else:
            self._send(200, {"status": "ok", "service": "mock-openai"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            request = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, {"error": {"message": "invalid json body"}})
            return

        if not self.headers.get("Authorization"):
            self._send(401, {"error": {"message": "missing api key"}})
            return

        with _Handler.lock:
            _Handler.counter["n"] += 1
            call_no = _Handler.counter["n"]

        messages = request.get("messages") or []
        user_text = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                user_text = message.get("content") or ""
                break

        array_text = _extract_array(user_text)
        items: list[str] = []
        if array_text is not None:
            try:
                items = json.loads(array_text)
            except json.JSONDecodeError:
                items = []
        if not isinstance(items, list):
            items = []
        items = [str(x) for x in items]

        translations = [fake_translate(t, i) for i, t in enumerate(items)]

        # 模拟「模型整段不译」：每段文字第一次被请求时原样返回，第二次才正常翻译。
        # 用来验证客户端有没有把这种结果错误地写进缓存、以及会不会自动重试。
        if self.skip_once:
            with _Handler.lock:
                for index, item in enumerate(items):
                    if item and item not in _Handler.skipped_once:
                        _Handler.skipped_once.add(item)
                        translations[index] = item

        # 模拟模型偶发不听话：故意少返回一条，用来验证二分重试降级
        if self.flaky and call_no % self.flaky == 0 and len(translations) > 1:
            translations = translations[:-1]

        content = json.dumps(translations, ensure_ascii=False)
        prompt_chars = sum(len(str(m.get("content") or "")) for m in messages)
        self._send(200, {
            "id": f"chatcmpl-mock-{call_no}",
            "object": "chat.completion",
            "created": 0,
            "model": request.get("model", "mock"),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": max(1, prompt_chars // 3),
                "completion_tokens": max(1, len(content) // 2),
                "total_tokens": max(2, prompt_chars // 3 + len(content) // 2),
            },
        })


def _extract_array(text: str) -> str | None:
    """从提示词里把最后一个 JSON 数组抠出来。"""
    start = text.rfind("\n[")
    if start != -1:
        candidate = text[start + 1 :].strip()
        if candidate.startswith("["):
            end = candidate.rfind("]")
            if end != -1:
                return candidate[: end + 1]
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        return text[start : end + 1]
    return None


def start_mock_server(
    port: int = 0, *, flaky: int = 0, skip_once: bool = False, verbose: bool = False
) -> tuple[ThreadingHTTPServer, str]:
    """在后台线程启动 Mock 服务，返回 (server, base_url)。port=0 表示随机可用端口。

    ``skip_once=True`` 时每段文字第一次被请求会原样返回（模拟模型漏翻）。
    """
    _Handler.flaky = flaky
    _Handler.skip_once = skip_once
    _Handler.skipped_once = set()
    _Handler.verbose = verbose
    _Handler.counter = {"n": 0}
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, real_port = httpd.server_address[0], httpd.server_address[1]
    return httpd, f"http://{host}:{real_port}/v1"


def main() -> int:
    parser = argparse.ArgumentParser(description="Mock OpenAI 兼容翻译服务（离线测试用）")
    parser.add_argument("--port", type=int, default=8123, help="监听端口，默认 8123")
    parser.add_argument("--flaky", type=int, default=0, help="每 N 次请求故意返回错误条数，用于测试降级")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每个请求")
    args = parser.parse_args()

    httpd, base_url = start_mock_server(args.port, flaky=args.flaky, verbose=args.verbose)
    print("Mock 翻译服务已启动")
    print(f"  Base URL : {base_url}")
    print(f"  模型名   : 随便填（例如 mock）")
    print(f"  API Key  : 随便填（不能为空）")
    print("  按 Ctrl+C 退出\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
