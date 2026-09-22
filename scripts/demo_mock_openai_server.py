#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""demo_mock_openai_server.py — 演示用 mock OpenAI 兼容端点 (DEMO ONLY)

============================================================
演示数据 / DEMO ONLY
本端点是纯本地 mock（Python 标准库实现），不加载任何真实模型，
不反映任何真实硬件或模型的性能。所有 TTFT / 吞吐 / token 数都是
按参数合成出来的假数据。仅用于功能演示、GUI 验收截图与回归测试。
============================================================

用途
----
为 JISUMAN LLM Benchmark GUI (/opt/llm-benchmark) 提供一个可离线运行的
OpenAI 兼容端点，让 GUI 的端到端流程（连接、校验、流式/非流式基准、
并发扫测、失败统计）可以在没有真实 LLM serving 的情况下被演示与验收。

实现的接口
----------
  GET  /v1/models             模型列表（3 个 demo 模型，其中 1 个固定失败）
  POST /v1/chat/completions   支持 stream=true/false，带 usage
  GET  /health                健康检查（便于脚本 wait-for-ready）
  GET  /                      端点信息与 DEMO 声明

用法示例
--------
  # 最小启动（默认 127.0.0.1:8123）
  python3 scripts/demo_mock_openai_server.py

  # 后台常驻 + 日志落盘
  setsid nohup python3 scripts/demo_mock_openai_server.py --port 8123 \
      > /tmp/demo_mock.out 2>&1 &

  # 验证模型列表
  curl -s http://127.0.0.1:8123/v1/models | python3 -m json.tool

  # 验证流式（SSE）
  curl -N -s http://127.0.0.1:8123/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"demo-qwen3-32b-fp8","messages":[{"role":"user","content":"hi"}],
         "max_tokens":8,"stream":true}'

  # 验证失败注入（100% 返回 500）
  python3 scripts/demo_mock_openai_server.py --port 8124 --fail-rate 1.0

GUI 里怎么填
------------
  API 地址: http://127.0.0.1:8123/v1/chat/completions
  模型 ID:  demo-qwen3-32b-fp8 / demo-deepseek-r1-70b-fp8
  （demo-broken-model 会 100% 失败，用于演示失败率与错误分类）

参数
----
  --host / --port     监听地址（默认 127.0.0.1:8123）
  --fail-rate         随机失败概率，默认 0.0
  --fail-status       注入失败时返回的 HTTP 状态码，默认 500
  --ttft-ms           首 token 前的等待时间，默认 120ms
  --tok-ms            之后每个 token 的间隔，默认 18ms
  --jitter            每个 token 间隔的抖动比例，默认 0.25（±25%）
  --tokens-per-chunk  每个 SSE chunk 携带的 token 数，默认 1
  --timeout-hold-ms   model 含 "timeout" 时的挂起时长，默认 600000ms
  --quiet             关闭每请求一行 stderr 日志（横幅仍打印）

注意
----
  请求体统一按 JSON 解析；Content-Type 不做强制校验，因为 GUI 的高并发
  路径（aiohttp + bytes body）发送的是 application/octet-stream，真实
  vLLM/OpenAI 端点同样不强制该头部。参数缺失/非法才会返回 400。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── 演示模型清单 ────────────────────────────────────────────────────────────
DEMO_MODELS = [
    "demo-qwen3-32b-fp8",
    "demo-deepseek-r1-70b-fp8",
    "demo-broken-model",  # 固定失败：model id 含 "broken"
]
OWNER = "jisumen-demo"
DEFAULT_MAX_TOKENS = 128
MAX_MAX_TOKENS = 65536
DEFAULT_TIMEOUT_HOLD_MS = 600_000

BANNER = """\
============================================================
演示数据 / DEMO ONLY — 本端点是本地 mock，不反映任何真实
硬件或模型的性能。仅用于功能演示、UI 验收与回归测试。
============================================================"""

# 可读的中英混合词表：让流式输出像正常文本，而不是一长串单字符
TOKEN_WORDS = [
    "The", "inference", "engine", "processes", "tokens", "in", "parallel",
    "batches", "during", "the", "decode", "phase", "while", "GPU", "memory",
    "bandwidth", "and", "quantization", "stay", "stable", "under", "load",
    "throughput", "latency", "request", "queue", "scheduler", "cache",
    "推理", "引擎", "并行", "批处理", "吞吐量", "延迟", "显存", "带宽",
    "量化", "请求", "队列", "注意力", "缓存", "调度", "基准", "测试",
    "模型", "服务", "端到端", "首token", "解码", "预填充", "采样",
]


# ── 参数容器（由 main 填充，供 handler 读取） ───────────────────────────────
class Config:
    host: str = "127.0.0.1"
    port: int = 8123
    fail_rate: float = 0.0
    fail_status: int = 500
    ttft_ms: float = 120.0
    tok_ms: float = 18.0
    jitter: float = 0.25
    tokens_per_chunk: int = 1
    timeout_hold_ms: int = DEFAULT_TIMEOUT_HOLD_MS
    quiet: bool = False
    started_at: float = 0.0


CFG = Config()
LOG_LOCK = threading.Lock()


def log_line(msg: str) -> None:
    """每请求一行日志到 stderr（--quiet 可关闭）。"""
    if CFG.quiet:
        return
    with LOG_LOCK:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()


def log_always(msg: str) -> None:
    with LOG_LOCK:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()


def jittered(ms: float) -> float:
    """按 --jitter 抖动一个毫秒值，返回秒。"""
    if ms <= 0:
        return 0.0
    lo = max(0.0, 1.0 - CFG.jitter)
    hi = 1.0 + CFG.jitter
    return (ms * random.uniform(lo, hi)) / 1000.0


def estimate_prompt_tokens(messages) -> int:
    """粗略估算 prompt_tokens：CJK 字符 ≈ 1 token，其他字符 ≈ 1/4 token。

    这是刻意的“粗略估算”（DEMO ONLY），只为让 usage 字段自洽，
    不代表任何真实分词器的结果。
    """
    total = 3  # 每个对话的基础开销（近似）
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", "")
        if isinstance(content, list):  # 多模态风格 content
            content = " ".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            )
        text = str(content) if content is not None else ""
        text += str(msg.get("role", ""))
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        other = len(text) - cjk
        total += cjk + math.ceil(other / 4) + 4
    return max(total, 1)


def make_fragments(model: str, n_tokens: int) -> list:
    """生成 n_tokens 个可读 token 片段。"""
    rng = random.Random(f"{model}:{n_tokens}:{os.getpid()}")
    frags = []
    for _ in range(n_tokens):
        word = rng.choice(TOKEN_WORDS)
        # 英文词前面加空格，CJK 直接拼接
        frags.append(word if word[0] > "\u2fff" else " " + word)
    return frags


def inject_failing_model(model: str):
    """model id 里的关键字决定失败方式：broken=必失败，timeout=挂起。"""
    low = model.lower()
    if "timeout" in low:
        return "timeout"
    if "broken" in low:
        return "forced"
    return None


class DemoHandler(BaseHTTPRequestHandler):
    server_version = "JisumenDemoMock/1.0"
    protocol_version = "HTTP/1.1"  # 保持长连接，与真实 serving 端点一致
    close_connection = True

    # ── 基础工具 ────────────────────────────────────────────────────────────
    def log_message(self, format, *args):  # noqa: A002 — 关掉默认 stderr 噪音
        pass

    def _read_body(self) -> bytes:
        """读取请求体，兼容 Content-Length 与 chunked。"""
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            chunks = []
            while True:
                size_line = self.rfile.readline(65536).strip()
                if not size_line:
                    break
                try:
                    size = int(size_line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline(65536)  # 尾部 CRLF
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)  # CRLF
            return b"".join(chunks)
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return b""
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _send_json(self, status: int, obj: dict) -> None:
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Demo-Mock", "true")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_error_obj(self, status: int, message: str, err_type: str,
                        code=None) -> None:
        self._send_json(status, {
            "error": {
                "message": message,
                "type": err_type,
                "code": status if code is None else code,
            }
        })

    # ── chunked SSE 写出 ────────────────────────────────────────────────────
    def _start_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Demo-Mock", "true")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _sse_chunk(self, text: str) -> None:
        data = text.encode("utf-8")
        self.wfile.write(b"%X\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    def _sse_end(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    # ── HTTP 路由 ───────────────────────────────────────────────────────────
    def do_GET(self):
        t0 = time.monotonic()
        path = self.path.split("?", 1)[0]
        if path in ("/v1/models", "/models"):
            now = int(time.time())
            self._send_json(200, {
                "object": "list",
                "data": [
                    {"id": mid, "object": "model", "created": now,
                     "owned_by": OWNER}
                    for mid in DEMO_MODELS
                ],
            })
            self._log_req("GET", path, 200, t0)
            return
        if path in ("/health", "/v1/health"):
            self._send_json(200, {"status": "ok", "demo": True,
                                  "uptime_sec": round(time.time() - CFG.started_at, 1)})
            self._log_req("GET", path, 200, t0)
            return
        if path == "/":
            self._send_json(200, {
                "service": "jisumen-demo-mock-openai",
                "demo_only": True,
                "notice": "DEMO ONLY — 本地 mock 端点，数据为合成值，"
                          "不反映任何真实硬件或模型性能。",
                "endpoints": ["/v1/models", "/v1/chat/completions",
                              "/health"],
                "models": DEMO_MODELS,
            })
            self._log_req("GET", path, 200, t0)
            return
        self._send_error_obj(404, f"unknown path: {path}", "not_found")
        self._log_req("GET", path, 404, t0)

    def do_POST(self):
        t0 = time.monotonic()
        path = self.path.split("?", 1)[0]
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._read_body()
            self._send_error_obj(404, f"unknown path: {path}", "not_found")
            self._log_req("POST", path, 404, t0)
            return
        self._handle_chat(t0, path)

    # ── chat/completions 主体 ───────────────────────────────────────────────
    def _handle_chat(self, t0: float, path: str) -> None:
        raw = self._read_body()
        if not raw:
            self._send_error_obj(400, "empty request body: expected JSON "
                                      "chat/completions payload",
                                 "invalid_request_error")
            self._log_req("POST", path, 400, t0, extra="empty body")
            return
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_error_obj(
                400, f"invalid JSON body: {exc}", "invalid_request_error")
            self._log_req("POST", path, 400, t0, extra="bad json")
            return
        if not isinstance(body, dict):
            self._send_error_obj(400, "request body must be a JSON object",
                                 "invalid_request_error")
            self._log_req("POST", path, 400, t0, extra="not an object")
            return

        model = body.get("model")
        if not isinstance(model, str) or not model.strip():
            self._send_error_obj(400, "missing or invalid 'model' "
                                      "(must be a non-empty string)",
                                 "invalid_request_error")
            self._log_req("POST", path, 400, t0, extra="bad model")
            return
        model = model.strip()

        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            self._send_error_obj(400, "missing or invalid 'messages' "
                                      "(must be a non-empty array)",
                                 "invalid_request_error")
            self._log_req("POST", path, 400, t0, extra="bad messages")
            return
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                self._send_error_obj(400, f"messages[{i}] must be an object",
                                     "invalid_request_error")
                self._log_req("POST", path, 400, t0, extra="bad message item")
                return
            role = msg.get("role")
            if not isinstance(role, str) or not role.strip():
                self._send_error_obj(
                    400, f"messages[{i}].role is required and must be a "
                         f"non-empty string", "invalid_request_error")
                self._log_req("POST", path, 400, t0, extra="bad role")
                return

        stream = body.get("stream", False)
        if not isinstance(stream, bool):
            self._send_error_obj(400, "'stream' must be a boolean",
                                 "invalid_request_error")
            self._log_req("POST", path, 400, t0, extra="bad stream")
            return

        max_tokens = body.get("max_tokens", DEFAULT_MAX_TOKENS)
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            self._send_error_obj(400, "'max_tokens' must be an integer",
                                 "invalid_request_error")
            self._log_req("POST", path, 400, t0, extra="bad max_tokens")
            return
        if max_tokens < 1 or max_tokens > MAX_MAX_TOKENS:
            self._send_error_obj(
                400, f"'max_tokens' out of range 1..{MAX_MAX_TOKENS} "
                     f"(got {max_tokens})", "invalid_request_error")
            self._log_req("POST", path, 400, t0, extra="max_tokens range")
            return

        temperature = body.get("temperature")
        if temperature is not None and (isinstance(temperature, bool)
                                       or not isinstance(temperature, (int, float))):
            self._send_error_obj(400, "'temperature' must be a number",
                                 "invalid_request_error")
            self._log_req("POST", path, 400, t0, extra="bad temperature")
            return

        # —— 失败注入：先看 model 关键字，再看随机失败 ——
        mode = inject_failing_model(model)
        if mode == "timeout":
            # 故意长时间不返回，用于验证客户端超时路径
            log_always(f"[demo-mock] hold: model={model} hang up to "
                       f"{CFG.timeout_hold_ms}ms (timeout-path demo)")
            end = time.monotonic() + CFG.timeout_hold_ms / 1000.0
            while time.monotonic() < end:
                time.sleep(min(0.5, max(0.0, end - time.monotonic())))
            self._send_error_obj(504, "demo timeout model: held for "
                                      f"{CFG.timeout_hold_ms}ms without "
                                      f"producing tokens", "timeout_error")
            self._log_req("POST", path, 504, t0, extra="timeout-model")
            return
        forced = (mode == "forced")
        if forced or (CFG.fail_rate > 0 and random.random() < CFG.fail_rate):
            status = CFG.fail_status
            self._send_json(status, {
                "error": {
                    "message": "injected demo failure",
                    "type": "server_error",
                    "code": status,
                }
            })
            self._log_req("POST", path, status, t0, model=model,
                          extra="injected" if forced else "injected(random)")
            return

        # —— 正常（合成）响应 ——
        n_tokens = max_tokens
        frags = make_fragments(model, n_tokens)
        prompt_tokens = estimate_prompt_tokens(messages)
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": n_tokens,
            "total_tokens": prompt_tokens + n_tokens,
        }
        now = int(time.time())
        cmpl_id = f"chatcmpl-demo-{int(time.time() * 1000) % 10**9}-" \
                  f"{random.randint(0, 9999)}"

        try:
            if stream:
                self._stream_response(cmpl_id, model, now, frags, usage)
            else:
                self._non_stream_response(cmpl_id, model, now, frags, usage)
        except (BrokenPipeError, ConnectionResetError):
            self._log_req("POST", path, 499, t0, model=model,
                          extra="client disconnected")
            self.close_connection = True
            return

        self._log_req("POST", path, 200, t0, model=model,
                      extra=f"stream={stream} out={n_tokens} "
                            f"in={prompt_tokens}")

    def _stream_response(self, cmpl_id, model, created, frags, usage) -> None:
        self._start_sse()
        # 首 token（首字节）前等待 ttft
        time.sleep(jittered(CFG.ttft_ms) if CFG.ttft_ms > 0 else 0.0)
        per_chunk = max(1, CFG.tokens_per_chunk)
        sent = 0
        first = True
        while sent < len(frags):
            if not first:
                time.sleep(jittered(CFG.tok_ms))
            first = False
            piece = "".join(frags[sent:sent + per_chunk])
            sent += per_chunk
            chunk = {
                "id": cmpl_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {"content": piece},
                    "finish_reason": None,
                }],
            }
            self._sse_chunk("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n")
        # 最终 chunk：delta 为空、finish_reason=stop，并带 usage（OpenAI 兼容风格）
        final = {
            "id": cmpl_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }],
            "usage": usage,
        }
        self._sse_chunk("data: " + json.dumps(final, ensure_ascii=False) + "\n\n")
        self._sse_chunk("data: [DONE]\n\n")
        self._sse_end()

    def _non_stream_response(self, cmpl_id, model, created, frags, usage) -> None:
        # 非流式也按同样的时间模型“花掉”合成延迟，让 E2E 延迟看起来合理
        delay = jittered(CFG.ttft_ms) + jittered(CFG.tok_ms) * max(0, len(frags) - 1)
        if delay > 0:
            time.sleep(delay)
        text = "".join(frags).strip()
        self._send_json(200, {
            "id": cmpl_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": usage,
            "system_fingerprint": "demo-mock-fp",
        })

    # ── 日志 ────────────────────────────────────────────────────────────────
    def _log_req(self, method: str, path: str, status: int, t0: float,
                 model=None, extra=None) -> None:
        ms = (time.monotonic() - t0) * 1000.0
        parts = [f"[demo-mock] {method} {path} {status} {ms:.1f}ms"]
        if model:
            parts.append(f"model={model}")
        if extra:
            parts.append(str(extra))
        log_line(" ".join(parts))


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="DEMO ONLY mock OpenAI-compatible endpoint "
                    "(synthetic data; not a real model).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8123)
    p.add_argument("--fail-rate", type=float, default=0.0,
                   help="probability (0.0-1.0) of returning a failure")
    p.add_argument("--fail-status", type=int, default=500,
                   help="HTTP status used for injected failures")
    p.add_argument("--ttft-ms", type=float, default=120.0,
                   help="synthetic wait before the first token")
    p.add_argument("--tok-ms", type=float, default=18.0,
                   help="synthetic interval between tokens")
    p.add_argument("--jitter", type=float, default=0.25,
                   help="jitter ratio applied to ttft/token intervals")
    p.add_argument("--tokens-per-chunk", type=int, default=1,
                   help="tokens carried by each SSE chunk")
    p.add_argument("--timeout-hold-ms", type=int, default=DEFAULT_TIMEOUT_HOLD_MS,
                   help="hang duration for model ids containing 'timeout'")
    p.add_argument("--quiet", action="store_true",
                   help="suppress the per-request stderr log line")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if not (0.0 <= args.fail_rate <= 1.0):
        print("error: --fail-rate must be within 0.0..1.0", file=sys.stderr)
        return 2
    if args.fail_status < 400 or args.fail_status > 599:
        print("error: --fail-status must be within 400..599", file=sys.stderr)
        return 2
    if args.ttft_ms < 0 or args.tok_ms < 0:
        print("error: --ttft-ms/--tok-ms must be >= 0", file=sys.stderr)
        return 2
    if args.jitter < 0:
        print("error: --jitter must be >= 0", file=sys.stderr)
        return 2
    if args.tokens_per_chunk < 1:
        print("error: --tokens-per-chunk must be >= 1", file=sys.stderr)
        return 2

    CFG.host = args.host
    CFG.port = args.port
    CFG.fail_rate = args.fail_rate
    CFG.fail_status = args.fail_status
    CFG.ttft_ms = args.ttft_ms
    CFG.tok_ms = args.tok_ms
    CFG.jitter = args.jitter
    CFG.tokens_per_chunk = args.tokens_per_chunk
    CFG.timeout_hold_ms = args.timeout_hold_ms
    CFG.quiet = args.quiet
    CFG.started_at = time.time()

    try:
        httpd = DemoServer((args.host, args.port), DemoHandler)
    except OSError as exc:
        print(f"error: cannot bind {args.host}:{args.port} — {exc}",
              file=sys.stderr)
        return 1

    actual_host, actual_port = httpd.server_address[0], httpd.server_address[1]

    print(BANNER, flush=True)
    print("JISUMAN LLM Benchmark — mock OpenAI-compatible endpoint (DEMO ONLY)",
          flush=True)
    print(f"  listening      : http://{actual_host}:{actual_port}", flush=True)
    print(f"  base URL (GUI) : http://{actual_host}:{actual_port}/v1/chat/completions",
          flush=True)
    print(f"  models         : {', '.join(DEMO_MODELS)}", flush=True)
    print(f"  injection      : fail_rate={CFG.fail_rate} "
          f"fail_status={CFG.fail_status} "
          f"(model id 含 'broken' → 必失败, 含 'timeout' → 挂起 "
          f"{CFG.timeout_hold_ms}ms)", flush=True)
    print(f"  synthetic      : ttft_ms={CFG.ttft_ms} tok_ms={CFG.tok_ms} "
          f"jitter={CFG.jitter} tokens_per_chunk={CFG.tokens_per_chunk}",
          flush=True)
    print(f"  per-request log: {'OFF (--quiet)' if CFG.quiet else 'stderr'}",
          flush=True)
    print("=" * 60, flush=True)

    stop = {"flag": False}

    def _on_signal(signum, _frame):
        stop["flag"] = True
        log_always(f"[demo-mock] signal {signum} received, shutting down...")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass

    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            httpd.server_close()
        except OSError:
            pass
        log_always("[demo-mock] server stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
