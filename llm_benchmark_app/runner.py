"""runner.py — LLM HTTP request runner: call_llm, run_benchmark, build_chat_payload, etc.

Extracted from llm_benchmark.py (TASK-LLM-BENCHMARK-MODULE-SPLIT-TOKEN-HC-001-v1).
No behavior changes — identical logic to the original.
"""
from __future__ import annotations

import json
import logging
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from urllib import error, request
from urllib.parse import urlparse, urlunparse

from .stream_parser import _parse_stream_chunk
from .metrics import aggregate_results, percentile

# ── Module-level debug flag — set by llm_benchmark.py after import ────────────
# Usage: import llm_benchmark_app.runner as _runner; _runner.DEBUG_MODE = True
DEBUG_MODE: bool = False


def normalize_api_url(url: str) -> str:
    """Ensure the URL points to a /chat/completions endpoint.
    Uses urlparse to correctly handle any host:port combination.
    Input:  http://host:8000/v1     → http://host:8000/v1/chat/completions
    Input:  http://host:9099/v1/    → http://host:9099/v1/chat/completions
    Input:  https://api.x.com/v1/chat/completions → unchanged
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    return urlunparse((parsed.scheme, parsed.netloc, path,
                        parsed.params, parsed.query, parsed.fragment))


def check_server_reachable(api_url: str, timeout: int = 5) -> tuple[bool, str]:
    """Quick preflight: try a minimal POST to the API endpoint.
    Returns (True, "") if the server responds (even with an error status).
    Returns (False, error_msg) only on connection-level failures.
    """
    if DEBUG_MODE:
        logging.debug("connectivity check → %s", api_url)
    try:
        req = request.Request(api_url, data=b"{}", method="POST")
        req.add_header("Content-Type", "application/json")
        request.urlopen(req, timeout=timeout)
        if DEBUG_MODE:
            logging.debug("connectivity OK")
        return True, ""
    except error.HTTPError as e:
        # 4xx/5xx responses still mean the server is reachable
        if DEBUG_MODE:
            logging.debug("connectivity OK (server responded HTTP %d)", e.code)
        return True, ""
    except Exception as e:
        if DEBUG_MODE:
            logging.warning("connectivity FAIL: %s", e)
        return False, str(e)


def fetch_models(api_url: str, api_key: str, timeout: int = 10) -> tuple[list[str], str]:
    """Fetch available model list from the API's /v1/models endpoint.
    Returns (model_ids, error_msg). model_ids is empty on failure.
    Strips /chat/completions from api_url to get the base URL."""
    # Derive base URL by removing /chat/completions suffix
    parsed = urlparse(api_url)
    path = parsed.path
    if path.endswith("/chat/completions"):
        path = path[:-len("/chat/completions")]
    models_url = urlunparse((parsed.scheme, parsed.netloc, path + "/models",
                             parsed.params, parsed.query, parsed.fragment))
    if DEBUG_MODE:
        logging.debug("fetch models → %s", models_url)
    try:
        req = request.Request(models_url, method="GET")
        req.add_header("Content-Type", "application/json")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        resp = request.urlopen(req, timeout=timeout)
        data = json.loads(resp.read())
        models = [m.get("id", "") for m in data.get("data", [])]
        models = [m for m in models if m]  # filter empty
        if DEBUG_MODE:
            logging.info("fetched %d models", len(models))
        return models, ""
    except Exception as e:
        if DEBUG_MODE:
            logging.warning("fetch models failed: %s", e)
        return [], str(e)


def build_chat_payload(model: str, messages: list[dict], max_tokens: int,
                       temperature: float, stream: bool = True,
                       output_length_mode: str = "normal") -> dict:
    """Build an OpenAI-compatible chat/completions payload.

    Normal mode preserves existing behavior. Fixed mode is vLLM-bench compatible:
    min_tokens=max_tokens and ignore_eos=true.
    """
    mode = output_length_mode if output_length_mode in ("normal", "fixed") else "normal"
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": stream,
    }
    if mode == "fixed":
        payload["min_tokens"] = max_tokens
        payload["ignore_eos"] = True
    if stream:
        # Some OpenAI-compatible servers require stream_options to get usage.
        payload["stream_options"] = {"include_usage": True}
    return payload


def call_llm(api_url: str, api_key: str, model: str, messages: list[dict],
             max_tokens: int, temperature: float, timeout: int = 120,
             stream: bool = True, output_length_mode: str = "normal") -> dict:
    """Send one chat-completion request.

    When stream=True: reads SSE chunks, records first-token time,
    inter-chunk timestamps, and computes TTFT / TPOT / ITL accurately.

    When stream=False: returns e2e_latency only; TTFT/TPOT/ITL are None.
    The caller/UI MUST indicate that non-streaming cannot measure true TTFT.

    Returns a dict with keys:
      ok, e2e_latency, latency, ttft, tpot, itl_avg, itl_values,
      prompt_tokens, completion_tokens, total_tokens,
      per_request_output_tps_e2e, per_request_decode_tps,
      finish_reason, stream, [error, error_type on failure]
    """
    body_dict = build_chat_payload(model, messages, max_tokens, temperature,
                                   stream=stream,
                                   output_length_mode=output_length_mode)

    body = json.dumps(body_dict).encode("utf-8")
    req = request.Request(api_url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")

    if DEBUG_MODE:
        logging.debug("POST %s | model=%s max_tokens=%d temp=%.2f stream=%s msg_len=%d",
                       api_url, model, max_tokens, temperature, stream, len(body))

    request_start = time.perf_counter()

    # ── helper: build success result ──
    def _success_result(e2e_latency, ttft, tpot, itl_avg, itl_values,
                        prompt_tokens, completion_tokens, total_tokens,
                        finish_reason, used_stream,
                        visible_ttft=None, visible_tpot=None,
                        debug_fields=None):
        latency = e2e_latency
        per_req_out_tps = completion_tokens / e2e_latency if e2e_latency > 0 and completion_tokens > 0 else 0.0
        per_req_decode_tps = None
        if ttft is not None and completion_tokens >= 2 and (e2e_latency - ttft) > 0:
            per_req_decode_tps = (completion_tokens - 1) / (e2e_latency - ttft)
        result = {
            "ok": True,
            "e2e_latency": round(e2e_latency, 6),
            "latency": round(latency, 6),
            "ttft": round(ttft, 6) if ttft is not None else None,
            "visible_ttft": round(visible_ttft, 6) if visible_ttft is not None else None,
            "tpot": round(tpot, 6) if tpot is not None else None,
            "visible_tpot": round(visible_tpot, 6) if visible_tpot is not None else None,
            "itl_avg": round(itl_avg, 6) if itl_avg is not None else None,
            "itl_values": [round(v, 6) for v in itl_values],
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "per_request_output_tps_e2e": round(per_req_out_tps, 2),
            "per_request_decode_tps": round(per_req_decode_tps, 2) if per_req_decode_tps is not None else None,
            "finish_reason": finish_reason,
            "stream": used_stream,
        }
        if debug_fields:
            result.update(debug_fields)
        return result

    # ── helper: build failure result ──
    def _fail_result(e2e_latency, err_msg, err_type, used_stream):
        return {
            "ok": False,
            "e2e_latency": round(e2e_latency, 6),
            "latency": round(e2e_latency, 6),
            "ttft": None, "tpot": None, "itl_avg": None, "itl_values": [],
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "per_request_output_tps_e2e": 0.0, "per_request_decode_tps": None,
            "finish_reason": "",
            "stream": used_stream,
            "error": err_msg,
            "error_type": err_type,
        }

    try:
        if not stream:
            # ── non-streaming path ──
            resp = request.urlopen(req, timeout=timeout)
            raw = resp.read()
            e2e_latency = time.perf_counter() - request_start
            data = json.loads(raw)
            choice = data.get("choices", [{}])[0]
            usage = data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)
            finish = choice.get("finish_reason", "unknown")
            if DEBUG_MODE:
                logging.debug("OK (non-stream) e2e=%.3fs tokens=%d finish=%s",
                              e2e_latency, total_tokens, finish)
            return _success_result(e2e_latency, None, None, None, [],
                                   prompt_tokens, completion_tokens, total_tokens,
                                   finish, False)

        # ── streaming path ──
        try:
            resp = request.urlopen(req, timeout=timeout)
        except error.HTTPError as e:
            # Try fallback: remove stream_options and retry
            if "stream_options" in body_dict:
                if DEBUG_MODE:
                    logging.debug("stream_options rejected (%s), retrying without", e)
                body_dict.pop("stream_options", None)
                body2 = json.dumps(body_dict).encode("utf-8")
                req2 = request.Request(api_url, data=body2, method="POST")
                req2.add_header("Content-Type", "application/json")
                if api_key:
                    req2.add_header("Authorization", f"Bearer {api_key}")
                request_start = time.perf_counter()  # RESET timer after fallback
                resp = request.urlopen(req2, timeout=timeout)
            else:
                raise

        # ── streaming timing variables ──────────────────────────────────
        first_data_line_time = None
        first_json_chunk_time = None
        # transport / stream-event level
        first_stream_event_time = None
        stream_event_timestamps = []
        # generated content (any field: content / reasoning / text / tool)
        first_generated_token_time = None
        generated_token_timestamps = []
        # answer content (delta.content only)
        first_answer_token_time = None
        answer_token_timestamps = []
        # reasoning content (delta.reasoning_content / delta.reasoning)
        first_reasoning_token_time = None
        reasoning_token_timestamps = []
        # counters / token totals
        completion_tokens = 0
        prompt_tokens = 0
        total_tokens = 0
        finish_reason = "unknown"
        raw_data_lines = 0
        # diagnostics
        _obs_delta_keys: set = set()
        _unk_delta_keys: set = set()
        _gen_field_counts: dict = {}
        _stream_debug: dict = {"first_delta_samples": [], "first_generated_samples": []}

        # Read SSE line by line for accurate per-event timing
        with resp:
            while True:
                line_bytes = resp.readline()
                if not line_bytes:
                    break
                now = time.perf_counter()
                raw_data_lines += 1
                line = line_bytes.decode("utf-8", errors="replace").strip()

                if not line:
                    continue
                if not line.startswith("data:"):
                    continue

                if first_data_line_time is None:
                    first_data_line_time = now

                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break

                try:
                    obj = json.loads(data_str)
                except json.JSONDecodeError:
                    if DEBUG_MODE:
                        logging.warning("SSE JSON decode error: %s", data_str[:100])
                    continue

                if first_json_chunk_time is None:
                    first_json_chunk_time = now

                # ── centralized parser ──
                _chunk = _parse_stream_chunk(obj)

                # transport: stream event (any choices chunk, including role-only)
                if _chunk.has_stream_event:
                    stream_event_timestamps.append(now)
                    if first_stream_event_time is None:
                        first_stream_event_time = now

                # diagnostics
                _obs_delta_keys.update(_chunk.raw_delta_keys)
                _unk_delta_keys.update(_chunk.unknown_delta_keys)
                if len(_stream_debug["first_delta_samples"]) < 5 and _chunk.raw_delta_keys:
                    _stream_debug["first_delta_samples"].append({
                        "keys": list(_chunk.raw_delta_keys[:10]),
                        "generated": (_chunk.generated_text or "")[:200] if _chunk.generated_text else None,
                    })

                # generated content (any non-empty supported field)
                if _chunk.generated_text:
                    generated_token_timestamps.append(now)
                    if _chunk.generated_field:
                        _gen_field_counts[_chunk.generated_field] = (
                            _gen_field_counts.get(_chunk.generated_field, 0) + 1)
                    if first_generated_token_time is None:
                        first_generated_token_time = now
                        if len(_stream_debug["first_generated_samples"]) < 3:
                            _stream_debug["first_generated_samples"].append({
                                "field": _chunk.generated_field,
                                "text": _chunk.generated_text[:200],
                            })

                # answer content (delta.content only)
                if _chunk.answer_text:
                    answer_token_timestamps.append(now)
                    if first_answer_token_time is None:
                        first_answer_token_time = now

                # reasoning content
                if _chunk.reasoning_text:
                    reasoning_token_timestamps.append(now)
                    if first_reasoning_token_time is None:
                        first_reasoning_token_time = now

                # finish reason
                if _chunk.finish_reason:
                    finish_reason = _chunk.finish_reason

                # usage
                if _chunk.is_usage_chunk:
                    usage_chunk = obj.get("usage") or {}
                    prompt_tokens = usage_chunk.get("prompt_tokens", prompt_tokens)
                    completion_tokens = usage_chunk.get("completion_tokens", completion_tokens)
                    total_tokens = usage_chunk.get("total_tokens", prompt_tokens + completion_tokens)

        request_end = time.perf_counter()
        e2e_latency = request_end - request_start

        content_pieces = len(generated_token_timestamps)
        token_source = "usage" if completion_tokens > 0 else "missing_usage"
        if completion_tokens == 0 and DEBUG_MODE:
            logging.debug("stream: no usage in response, token_source=%s content_pieces=%d",
                          token_source, content_pieces)

        # ── transport / benchmark-aligned timing ──
        first_data_line_s    = round(first_data_line_time    - request_start, 6) if first_data_line_time    else None
        first_json_chunk_s   = round(first_json_chunk_time   - request_start, 6) if first_json_chunk_time   else None
        first_stream_event_s = round(first_stream_event_time - request_start, 6) if first_stream_event_time else None

        # ── generated content timing ──
        first_generated_token_s = round(first_generated_token_time - request_start, 6) if first_generated_token_time else None
        first_answer_token_s    = round(first_answer_token_time    - request_start, 6) if first_answer_token_time    else None
        first_reasoning_token_s = round(first_reasoning_token_time - request_start, 6) if first_reasoning_token_time else None

        # ── gaps (first_json_chunk → first generated/answer) ──
        first_generated_gap_s = None
        if first_json_chunk_s is not None and first_generated_token_s is not None:
            first_generated_gap_s = round(first_generated_token_s - first_json_chunk_s, 6)
        first_answer_gap_s = None
        if first_json_chunk_s is not None and first_answer_token_s is not None:
            first_answer_gap_s = round(first_answer_token_s - first_json_chunk_s, 6)

        # ── backward compat aliases ──
        first_non_empty_s   = first_generated_token_s      # was: first chunk with content/reasoning_content
        first_visible_gap_s = first_generated_gap_s        # old name

        # ── TTFT: benchmark-aligned = first JSON chunk ──
        ttft = first_json_chunk_s if first_json_chunk_s is not None else first_data_line_s
        # visible TTFT backward compat = first generated token
        visible_ttft = first_generated_token_s

        # ── TPOT (benchmark-aligned, formula unchanged) ──
        tpot = None
        if ttft is not None and completion_tokens >= 2 and (e2e_latency - ttft) > 0:
            tpot = (e2e_latency - ttft) / max(completion_tokens - 1, 1)

        # ── Visible TPOT (debug only, uses first_generated_token_s) ──
        visible_tpot = None
        if first_generated_token_s is not None and completion_tokens >= 2 and (e2e_latency - first_generated_token_s) > 0:
            visible_tpot = (e2e_latency - first_generated_token_s) / max(completion_tokens - 1, 1)

        # ── ITL: generated_itl is primary; stream_event_itl is debug fallback ──
        generated_itl_values = []
        generated_itl_avg = None
        if len(generated_token_timestamps) >= 2:
            generated_itl_values = [generated_token_timestamps[i] - generated_token_timestamps[i - 1]
                                    for i in range(1, len(generated_token_timestamps))]
            generated_itl_avg = statistics.mean(generated_itl_values)

        answer_itl_values = []
        answer_itl_avg = None
        if len(answer_token_timestamps) >= 2:
            answer_itl_values = [answer_token_timestamps[i] - answer_token_timestamps[i - 1]
                                 for i in range(1, len(answer_token_timestamps))]
            answer_itl_avg = statistics.mean(answer_itl_values)

        reasoning_itl_values = []
        reasoning_itl_avg = None
        if len(reasoning_token_timestamps) >= 2:
            reasoning_itl_values = [reasoning_token_timestamps[i] - reasoning_token_timestamps[i - 1]
                                    for i in range(1, len(reasoning_token_timestamps))]
            reasoning_itl_avg = statistics.mean(reasoning_itl_values)

        stream_event_itl_values = []
        stream_event_itl_avg = None
        if len(stream_event_timestamps) >= 2:
            stream_event_itl_values = [stream_event_timestamps[i] - stream_event_timestamps[i - 1]
                                       for i in range(1, len(stream_event_timestamps))]
            stream_event_itl_avg = statistics.mean(stream_event_itl_values)

        # Primary ITL: generated ITL preferred; stream_event ITL as calibration fallback
        if generated_itl_values:
            itl_values = generated_itl_values
            itl_avg = generated_itl_avg
        elif stream_event_itl_values:
            itl_values = stream_event_itl_values
            itl_avg = stream_event_itl_avg
        else:
            itl_values = []
            itl_avg = None

        # ── diagnostic warnings ──
        _stream_warnings = []
        if completion_tokens > 0 and not generated_token_timestamps:
            _stream_warnings.append(
                "服务端返回了 completion_tokens，但工具没有捕获到任何生成内容字段。"
                f"请检查流式 chunk 字段格式。observed_delta_keys={sorted(_obs_delta_keys)}"
            )
        if first_answer_token_s is None and first_reasoning_token_s is not None:
            _stream_warnings.append(
                "该模型本次流式输出包含 reasoning 字段，但未捕获到回答正文 content。"
                "若前端隐藏 reasoning，用户首字体验应参考 First Answer Token。"
            )
        if _unk_delta_keys:
            _stream_warnings.append(
                f"检测到未识别的流式 delta 字段：{sorted(_unk_delta_keys)}。"
                "已保存 stream_debug 供兼容性分析。"
            )
        if _gen_field_counts and not _gen_field_counts.get("content") and (
                _gen_field_counts.get("reasoning") or _gen_field_counts.get("reasoning_content")):
            _stream_warnings.append(
                "本次输出主要来自 reasoning 字段，最终回答正文 content 可能未流式输出或被服务端隐藏。"
            )

        if DEBUG_MODE:
            logging.debug(
                "OK (stream) e2e=%.3fs ttft=%.3fs gen=%.3fs answer=%.3fs reason=%.3fs tpot=%.3fs"
                " tokens=%d pieces=%d finish=%s",
                e2e_latency, ttft or -1, first_generated_token_s or -1,
                first_answer_token_s or -1, first_reasoning_token_s or -1,
                tpot or -1, total_tokens, content_pieces, finish_reason,
            )
            for _w in _stream_warnings:
                logging.warning("stream_diag: %s", _w)

        debug_fields = {
            # transport timing
            "first_data_line_s":    first_data_line_s,
            "first_json_chunk_s":   first_json_chunk_s,
            "first_stream_event_s": first_stream_event_s,
            # generated content timing
            "first_generated_token":  first_generated_token_s,
            "first_answer_token":     first_answer_token_s,
            "first_reasoning_token":  first_reasoning_token_s,
            "first_generated_gap":    first_generated_gap_s,
            "first_answer_gap":       first_answer_gap_s,
            # backward compat
            "first_non_empty_s":    first_non_empty_s,
            "first_visible_gap_s":  first_visible_gap_s,
            "content_pieces":       content_pieces,
            "raw_data_lines":       raw_data_lines,
            # ITL debug
            "generated_itl_avg":    generated_itl_avg,
            "answer_itl_avg":       answer_itl_avg,
            "reasoning_itl_avg":    reasoning_itl_avg,
            "stream_event_itl_avg": stream_event_itl_avg,
            # diagnostics
            "observed_delta_keys":    sorted(_obs_delta_keys),
            "unknown_delta_keys":     sorted(_unk_delta_keys),
            "generated_field_counts": dict(_gen_field_counts),
            "stream_debug":           _stream_debug,
            "stream_warnings":        _stream_warnings,
        }
        return _success_result(e2e_latency, ttft, tpot, itl_avg, itl_values,
                               prompt_tokens, completion_tokens, total_tokens,
                               finish_reason, True, visible_ttft=visible_ttft,
                               visible_tpot=visible_tpot, debug_fields=debug_fields)

    except Exception as e:
        e2e_latency = time.perf_counter() - request_start
        err_msg = str(e)
        used_stream = stream
        if isinstance(e, error.HTTPError):
            err_type = f"HTTP {e.code}"
        elif isinstance(e, error.URLError):
            err_type = "网络连接失败"
        elif isinstance(e, json.JSONDecodeError):
            err_type = "响应格式错误"
        else:
            err_type = type(e).__name__
        if DEBUG_MODE:
            logging.warning("FAIL e2e=%.3fs type=%s error=%s", e2e_latency, err_type, err_msg)
        return _fail_result(e2e_latency, err_msg, err_type, used_stream)


def run_benchmark(api_url: str, api_key: str, model: str, messages: list[dict],
                  max_tokens: int, temperature: float,
                  concurrency: int, num_requests: int,
                  progress_cb, done_cb,
                  stream: bool = True,
                  preset_name: str = "",
                  output_length_mode: str = "normal") -> None:
    """Run benchmark in a background thread; call progress_cb(completed, total, fail)
    and done_cb(summary_dict) on the main thread."""
    if DEBUG_MODE:
        logging.info("benchmark start: concurrency=%d total=%d stream=%s preset=%s",
                     concurrency, num_requests, stream, preset_name)
    results = []
    lock = threading.Lock()
    completed = [0]  # boxed for mutation in closure
    failed = [0]     # boxed for mutation in closure

    def worker():
        r = call_llm(api_url, api_key, model, messages, max_tokens, temperature,
                     stream=stream, output_length_mode=output_length_mode)
        with lock:
            results.append(r)
            completed[0] += 1
            if not r["ok"]:
                failed[0] += 1
            progress_cb(completed[0], num_requests, failed[0])
        return r

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(worker) for _ in range(num_requests)]
        for f in as_completed(futures):
            pass  # results collected in worker via closure
    duration = time.perf_counter() - t0

    if DEBUG_MODE:
        ok_count = sum(1 for r in results if r["ok"])
        fail_count = len(results) - ok_count
        logging.info("benchmark done: ok=%d fail=%d duration=%.2fs", ok_count, fail_count, duration)

    config = {
        "api_url": api_url,
        "model": model,
        "prompt": messages[-1]["content"][:100] if messages else "",
        "max_tokens": max_tokens,
        "temperature": temperature,
        "concurrency": concurrency,
        "total": num_requests,
        "stream_mode": stream,
        "benchmark_preset_name": preset_name,
        "benchmark_preset_type": "fixed_concurrency",
        "output_length_mode": output_length_mode,
    }
    summary = aggregate_results(results, duration, config)
    # Also add preset info to output
    summary["benchmark_preset_name"] = preset_name
    summary["benchmark_preset_type"] = "fixed_concurrency"
    done_cb(summary)
