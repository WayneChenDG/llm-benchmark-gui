"""metrics.py — JISUMAN LLM Benchmark Standard v1 metric definitions and aggregation.

Extracted from llm_benchmark.py (TASK-LLM-BENCHMARK-MODULE-SPLIT-TOKEN-HC-001-v1).
No behavior changes — identical logic to the original.
"""
from __future__ import annotations

import statistics

from .stream_parser import _build_parser_profile

# ── Schema version (shared with history_db) ──────────────────────────────────
DB_SCHEMA_VERSION = 2

# ── Standard metric aliases ───────────────────────────────────────────────────
STANDARD_METRIC_ALIASES = {
    "ttft": "ttft",
    "tpot": "tpot",
    "itl": "itl_avg",
    "e2el": "e2e_latency",
    "output_token_throughput": "system_output_tps",
    "request_throughput": "request_throughput_rps",
}


# ── Utility: percentile & filter ─────────────────────────────────────────────

def clean_numbers(values):
    """Return a list with only non-None numeric values."""
    return [v for v in values if isinstance(v, (int, float)) and v is not None]


def percentile(data, p):
    """Compute the p-th percentile (0-100) of a list of numbers.
    Uses linear interpolation. Returns 0 for empty data."""
    data = sorted(clean_numbers(data))
    if not data:
        return 0
    k = (len(data) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(data) - 1)
    return data[f] + (k - f) * (data[c] - data[f]) if c > f else data[f]


# ── JISUMAN LLM Benchmark Standard v1 metric definitions ─────────────────────

def get_metric_standard_definitions() -> dict:
    """Return JISUMAN LLM Benchmark Standard v1 metric definitions.

    The metric set aligns with common LLM serving benchmark terminology:
    - vLLM bench serve: ttft, tpot, itl, e2el
    - NVIDIA GenAI-Perf / NIM Benchmark style: output token throughput,
      request throughput, time to first token, inter token latency
    """
    return {
        "standard_name": "JISUMAN LLM Benchmark Standard v1",
        "references": [
            "vLLM bench serve: ttft / tpot / itl / e2el",
            "NVIDIA GenAI-Perf / NIM-style metrics: output token throughput / request throughput / time to first token / inter token latency",
        ],
        "metrics": {
            "ttft": {
                "display_name": "TTFT",
                "full_name": "Time To First Token",
                "definition": "request_start 到首个非空输出 chunk/token 的时间",
                "formula": "first_output_time - request_start",
                "unit": "seconds",
                "source": "streaming response timestamps",
                "note": "当前工具无 tokenizer，按首个非空流式 chunk 估算 first token",
            },
            "e2el": {
                "display_name": "E2E Latency",
                "full_name": "End-to-End Latency",
                "definition": "request_start 到完整响应结束的时间",
                "formula": "response_end - request_start",
                "unit": "seconds",
                "source": "client-side timer",
            },
            "tpot": {
                "display_name": "TPOT",
                "full_name": "Time Per Output Token",
                "definition": "首 token 之后每个输出 token 的平均耗时",
                "formula": "(e2e_latency - ttft) / max(output_tokens - 1, 1)",
                "unit": "seconds/token",
                "source": "usage.completion_tokens + streaming timestamps",
            },
            "itl": {
                "display_name": "ITL",
                "full_name": "Inter Token Latency",
                "definition": "相邻流式输出 chunk/token 的时间间隔",
                "formula": "timestamp[i] - timestamp[i-1]",
                "unit": "seconds",
                "source": "streaming chunk timestamps",
                "note": "当前工具无 tokenizer，ITL 为 chunk-level approximation，不得声称严格 token-level",
            },
            "system_output_tps": {
                "display_name": "System Output TPS",
                "full_name": "System Output Token Throughput",
                "definition": "整轮 benchmark 的输出 token 吞吐",
                "formula": "sum(completion_tokens) / benchmark_duration",
                "unit": "tokens/s",
                "source": "usage.completion_tokens",
            },
            "system_total_tps": {
                "display_name": "System Total TPS",
                "full_name": "System Total Token Throughput",
                "definition": "整轮 benchmark 的输入+输出 token 总吞吐",
                "formula": "sum(prompt_tokens + completion_tokens) / benchmark_duration",
                "unit": "tokens/s",
                "source": "usage.total_tokens or prompt_tokens + completion_tokens",
            },
            "request_throughput_rps": {
                "display_name": "Request Throughput",
                "full_name": "Requests Per Second",
                "definition": "成功请求数除以整轮 benchmark 持续时间",
                "formula": "successful_requests / benchmark_duration",
                "unit": "req/s",
                "source": "benchmark summary",
            },
            "per_request_output_tps": {
                "display_name": "Per-request Output TPS",
                "full_name": "Per-request Output Token Speed",
                "definition": "单请求维度的输出速度",
                "formula": "completion_tokens / e2e_latency",
                "unit": "tokens/s",
                "source": "per-request usage + e2e latency",
                "note": "这是用户侧单请求体验速度，不等于系统总吞吐",
            },
        },
    }


def validate_metric_consistency(summary: dict) -> list[str]:
    """Return warnings when benchmark metrics violate expected relationships.

    Tolerance: relative 3%, absolute 0.05
    """
    warnings = []
    eps_rel = 0.03
    eps_abs = 0.05

    def _close(a, b):
        if a == 0 and b == 0:
            return True
        return abs(a - b) <= max(abs(a), abs(b)) * eps_rel + eps_abs

    dur = max(summary.get("duration_sec", 0.001), 0.001)
    ok_count = summary.get("success", 0)
    out_tok = summary.get("total_output_tokens", 0)
    in_tok = summary.get("total_input_tokens", 0)
    tot_tok = summary.get("total_tokens", 0)

    # 1-3: token sums consistency
    if not _close(tot_tok, in_tok + out_tok):
        warnings.append(f"total_tokens ({tot_tok}) != input ({in_tok}) + output ({out_tok})")

    # 4-6: throughput consistency
    if not _close(summary.get("system_output_tps", 0), out_tok / dur):
        warnings.append("system_output_tps != total_output_tokens / duration_sec")
    if not _close(summary.get("system_total_tps", 0), tot_tok / dur):
        warnings.append("system_total_tps != total_tokens / duration_sec")
    if not _close(summary.get("request_throughput_rps", 0), ok_count / dur):
        warnings.append("request_throughput_rps != success / duration_sec")

    # 7-8: standard aliases consistency
    if not _close(summary.get("output_token_throughput", 0), summary.get("system_output_tps", 0)):
        warnings.append("output_token_throughput != system_output_tps")
    if not _close(summary.get("request_throughput", 0), summary.get("request_throughput_rps", 0)):
        warnings.append("request_throughput != request_throughput_rps")

    # 9: e2el alias consistency
    for p in ["avg", "p50", "p95", "p99"]:
        e2e_key = f"e2e_latency_{p}"
        e2el_key = f"e2el_{p}"
        if e2el_key in summary:
            if not _close(summary.get(e2el_key, 0), summary.get(e2e_key, 0)):
                warnings.append(f"{e2el_key} != {e2e_key}")

    # 10: TTFT should not all equal E2E latency when streaming with success
    if summary.get("stream_mode") and ok_count > 0:
        ttft_vals_ok = [r.get("ttft") for r in summary.get("detail", []) if r.get("ok") and r.get("ttft") is not None]
        e2e_vals_ok = [r.get("e2e_latency") for r in summary.get("detail", []) if r.get("ok")]
        if ttft_vals_ok and e2e_vals_ok and len(ttft_vals_ok) == len(e2e_vals_ok):
            all_equal = all(abs(t - e) < 0.001 for t, e in zip(ttft_vals_ok, e2e_vals_ok))
            if all_equal:
                warnings.append("流式模式下所有 TTFT ≈ E2E Latency — 疑似未正确采集 first token 时间")

    # 11: non-streaming must not fake TTFT/TPOT/ITL
    if not summary.get("stream_mode"):
        if summary.get("ttft_avg", 0) > 0 and summary.get("ttft_avg") != 0:
            warnings.append("非流式模式不应有非零 TTFT 值")

    # 12: per_request_output_tps_avg is per-request, must not equal system throughput
    #     Only warn when concurrency > 1 — at concurrency=1 they are expected to be close.
    pr_tps = summary.get("per_request_output_tps_avg", 0)
    sys_tps = summary.get("system_output_tps", 0)
    concurrency = summary.get("concurrency", 1)
    if concurrency > 1 and ok_count > 1 and pr_tps > 0 and sys_tps > 0:
        if _close(pr_tps, sys_tps):
            warnings.append(f"per_request_output_tps_avg ({pr_tps:.2f}) ≈ system_output_tps ({sys_tps:.2f}) — 单请求均速不应等于系统吞吐（除非并发=1）")

    return warnings


def aggregate_results(results: list[dict], duration: float, config: dict) -> dict:
    """Aggregate benchmark results into a summary dict.

    Args:
        results: list of per-request result dicts from call_llm()
        duration: wall-clock time from first request start to last response end
        config: dict with keys api_url, model, prompt, max_tokens, temperature,
                concurrency, total, stream_mode

    Returns:
        Summary dict with all required metrics (TTFT, TPOT, ITL, E2E, throughput, etc.)
    """
    ok_results = [r for r in results if r["ok"]]
    fail_results = [r for r in results if not r["ok"]]
    num_total = len(results)
    num_ok = len(ok_results)
    num_fail = len(fail_results)

    # ── latencies ──
    e2e_latencies = [r["e2e_latency"] for r in ok_results]
    ttfts = [
        r.get("ttft")
        for r in ok_results
        if isinstance(r.get("ttft"), (int, float)) and r.get("ttft") is not None
    ]
    tpots = [r.get("tpot") for r in ok_results if isinstance(r.get("tpot"), (int, float))]
    itl_values_all = []
    for r in ok_results:
        itl_values_all.extend(r.get("itl_values", []))

    # ── split TTFT: first_stream / first_visible ──
    first_data_lines    = [r.get("first_data_line_s")  for r in ok_results if isinstance(r.get("first_data_line_s"),  (int, float))]
    first_json_chunks   = [r.get("first_json_chunk_s") for r in ok_results if isinstance(r.get("first_json_chunk_s"), (int, float))]
    first_visible_tokens = [r.get("first_non_empty_s") for r in ok_results if isinstance(r.get("first_non_empty_s"), (int, float))]
    visible_ttfts       = [r.get("visible_ttft")       for r in ok_results if isinstance(r.get("visible_ttft"),       (int, float))]
    visible_tpots       = [r.get("visible_tpot")       for r in ok_results if isinstance(r.get("visible_tpot"),       (int, float))]
    first_visible_gaps  = [r.get("first_visible_gap_s") for r in ok_results if isinstance(r.get("first_visible_gap_s"), (int, float))]

    # ── new: generated/answer/reasoning timing aggregates ──
    def _clean(key):
        return [r.get(key) for r in ok_results if isinstance(r.get(key), (int, float))]

    first_generated_tokens   = _clean("first_generated_token")
    first_answer_tokens      = _clean("first_answer_token")
    first_reasoning_tokens   = _clean("first_reasoning_token")
    first_generated_gaps     = _clean("first_generated_gap")
    first_answer_gaps        = _clean("first_answer_gap")
    generated_itl_avgs       = _clean("generated_itl_avg")
    answer_itl_avgs          = _clean("answer_itl_avg")
    reasoning_itl_avgs       = _clean("reasoning_itl_avg")
    stream_event_itl_avgs    = _clean("stream_event_itl_avg")

    # ── tokens ──
    total_input_tokens = sum(r.get("prompt_tokens", 0) for r in ok_results)
    total_output_tokens = sum(r.get("completion_tokens", 0) for r in ok_results)
    total_tokens = total_input_tokens + total_output_tokens

    # ── throughput ──
    dur = max(duration, 0.001)
    request_throughput_rps = num_ok / dur
    system_output_tps = total_output_tokens / dur
    system_total_tps = total_tokens / dur

    # ── per-request output TPS ──
    per_req_tps = [r.get("per_request_output_tps_e2e", 0) for r in ok_results]

    # ── success rate ──
    success_rate = (num_ok / num_total * 100) if num_total > 0 else 0.0

    # ── stream mode flag ──
    stream_mode = any(r.get("stream") for r in ok_results)

    def _p(data, p):
        return round(percentile(data, p), 3)

    summary = {
        "api_url": config["api_url"],
        "model": config["model"],
        "prompt": config["prompt"],
        "max_tokens": config["max_tokens"],
        "output_length_mode": config.get("output_length_mode", "normal"),
        "fixed_output_tokens": config["max_tokens"] if config.get("output_length_mode") == "fixed" else None,
        "min_tokens_sent": config["max_tokens"] if config.get("output_length_mode") == "fixed" else None,
        "ignore_eos": config.get("output_length_mode") == "fixed",
        "temperature": config["temperature"],
        "concurrency": config["concurrency"],
        "total": num_total,
        "success": num_ok,
        "fail": num_fail,
        "success_rate": round(success_rate, 1),
        "duration_sec": round(duration, 2),
        "stream_mode": stream_mode,

        # ── standard metadata ──
        "metric_standard": "JISUMAN LLM Benchmark Standard v1",
        "metric_references": [
            "vLLM bench serve compatible terminology: ttft/tpot/itl/e2el",
            "NVIDIA GenAI-Perf/NIM-style terminology: output token throughput/request throughput/TTFT/ITL",
        ],

        # E2E latency
        "e2e_latency_min": round(min(e2e_latencies), 3) if e2e_latencies else 0,
        "e2e_latency_avg": round(statistics.mean(e2e_latencies), 3) if e2e_latencies else 0,
        "e2e_latency_max": round(max(e2e_latencies), 3) if e2e_latencies else 0,
        "e2e_latency_p50": _p(e2e_latencies, 50),
        "e2e_latency_p95": _p(e2e_latencies, 95),
        "e2e_latency_p99": _p(e2e_latencies, 99),

        # ── standard e2el aliases (vLLM: e2el) ──
        "e2el_min": round(min(e2e_latencies), 3) if e2e_latencies else 0,
        "e2el_avg": round(statistics.mean(e2e_latencies), 3) if e2e_latencies else 0,
        "e2el_max": round(max(e2e_latencies), 3) if e2e_latencies else 0,
        "e2el_p50": _p(e2e_latencies, 50),
        "e2el_p95": _p(e2e_latencies, 95),
        "e2el_p99": _p(e2e_latencies, 99),

        # backward compat: latency_* = e2e_latency_*
        "latency_min": round(min(e2e_latencies), 3) if e2e_latencies else 0,
        "latency_avg": round(statistics.mean(e2e_latencies), 3) if e2e_latencies else 0,
        "latency_max": round(max(e2e_latencies), 3) if e2e_latencies else 0,
        "latency_p50": _p(e2e_latencies, 50),
        "latency_p95": _p(e2e_latencies, 95),
        "latency_p99": _p(e2e_latencies, 99),

        # TTFT
        "ttft_avg": round(statistics.mean(ttfts), 3) if ttfts else 0,
        "ttft_p50": _p(ttfts, 50),
        "ttft_p95": _p(ttfts, 95),
        "ttft_p99": _p(ttfts, 99),

        # TPOT
        "tpot_avg": round(statistics.mean(tpots), 3) if tpots else 0,
        "tpot_p50": _p(tpots, 50),
        "tpot_p95": _p(tpots, 95),
        "tpot_p99": _p(tpots, 99),

        # ITL
        "itl_avg": round(statistics.mean(itl_values_all), 3) if itl_values_all else 0,
        "itl_p50": _p(itl_values_all, 50),
        "itl_p95": _p(itl_values_all, 95),
        "itl_p99": _p(itl_values_all, 99),

        # ── split TTFT: first stream chunk (vLLM-aligned) ──
        "first_data_line_avg":  round(statistics.mean(first_data_lines),  3) if first_data_lines  else 0,
        "first_data_line_p50":  _p(first_data_lines, 50),
        "first_data_line_p95":  _p(first_data_lines, 95),
        "first_data_line_p99":  _p(first_data_lines, 99),

        "first_json_chunk_avg": round(statistics.mean(first_json_chunks), 3) if first_json_chunks else 0,
        "first_json_chunk_p50": _p(first_json_chunks, 50),
        "first_json_chunk_p95": _p(first_json_chunks, 95),
        "first_json_chunk_p99": _p(first_json_chunks, 99),

        # ── split TTFT: first visible token (user-perceived) ──
        "first_visible_token_avg": round(statistics.mean(first_visible_tokens), 3) if first_visible_tokens else None,
        "first_visible_token_p50": _p(first_visible_tokens, 50) if first_visible_tokens else None,
        "first_visible_token_p95": _p(first_visible_tokens, 95) if first_visible_tokens else None,
        "first_visible_token_p99": _p(first_visible_tokens, 99) if first_visible_tokens else None,

        "visible_ttft_avg": round(statistics.mean(visible_ttfts), 3) if visible_ttfts else None,
        "visible_ttft_p50": _p(visible_ttfts, 50) if visible_ttfts else None,
        "visible_ttft_p95": _p(visible_ttfts, 95) if visible_ttfts else None,
        "visible_ttft_p99": _p(visible_ttfts, 99) if visible_ttfts else None,

        # ── first visible gap ──
        "first_visible_gap_avg": round(statistics.mean(first_visible_gaps), 3) if first_visible_gaps else None,
        "first_visible_gap_p50": _p(first_visible_gaps, 50) if first_visible_gaps else None,
        "first_visible_gap_p95": _p(first_visible_gaps, 95) if first_visible_gaps else None,
        "first_visible_gap_p99": _p(first_visible_gaps, 99) if first_visible_gaps else None,

        # ── visible TPOT (debug only) ──
        "visible_tpot_avg": round(statistics.mean(visible_tpots), 3) if visible_tpots else None,
        "visible_tpot_p50": _p(visible_tpots, 50) if visible_tpots else None,
        "visible_tpot_p95": _p(visible_tpots, 95) if visible_tpots else None,
        "visible_tpot_p99": _p(visible_tpots, 99) if visible_tpots else None,

        # ── new: generated / answer / reasoning timing ──
        # None when no samples (never faked as 0)
        "first_generated_token_avg": round(statistics.mean(first_generated_tokens), 3) if first_generated_tokens else None,
        "first_generated_token_p50": _p(first_generated_tokens, 50) if first_generated_tokens else None,
        "first_generated_token_p95": _p(first_generated_tokens, 95) if first_generated_tokens else None,
        "first_generated_token_p99": _p(first_generated_tokens, 99) if first_generated_tokens else None,

        "first_answer_token_avg": round(statistics.mean(first_answer_tokens), 3) if first_answer_tokens else None,
        "first_answer_token_p50": _p(first_answer_tokens, 50) if first_answer_tokens else None,
        "first_answer_token_p95": _p(first_answer_tokens, 95) if first_answer_tokens else None,
        "first_answer_token_p99": _p(first_answer_tokens, 99) if first_answer_tokens else None,

        "first_reasoning_token_avg": round(statistics.mean(first_reasoning_tokens), 3) if first_reasoning_tokens else None,
        "first_reasoning_token_p50": _p(first_reasoning_tokens, 50) if first_reasoning_tokens else None,
        "first_reasoning_token_p95": _p(first_reasoning_tokens, 95) if first_reasoning_tokens else None,
        "first_reasoning_token_p99": _p(first_reasoning_tokens, 99) if first_reasoning_tokens else None,

        "first_generated_gap_avg": round(statistics.mean(first_generated_gaps), 3) if first_generated_gaps else None,
        "first_generated_gap_p50": _p(first_generated_gaps, 50) if first_generated_gaps else None,
        "first_generated_gap_p95": _p(first_generated_gaps, 95) if first_generated_gaps else None,
        "first_generated_gap_p99": _p(first_generated_gaps, 99) if first_generated_gaps else None,

        "first_answer_gap_avg": round(statistics.mean(first_answer_gaps), 3) if first_answer_gaps else None,
        "first_answer_gap_p50": _p(first_answer_gaps, 50) if first_answer_gaps else None,
        "first_answer_gap_p95": _p(first_answer_gaps, 95) if first_answer_gaps else None,
        "first_answer_gap_p99": _p(first_answer_gaps, 99) if first_answer_gaps else None,

        "generated_itl_avg_agg": round(statistics.mean(generated_itl_avgs), 3) if generated_itl_avgs else None,
        "answer_itl_avg_agg":    round(statistics.mean(answer_itl_avgs),    3) if answer_itl_avgs    else None,
        "reasoning_itl_avg_agg": round(statistics.mean(reasoning_itl_avgs), 3) if reasoning_itl_avgs else None,
        "stream_event_itl_avg_agg": round(statistics.mean(stream_event_itl_avgs), 3) if stream_event_itl_avgs else None,

        # tokens
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_tokens": total_tokens,

        # ── per-request token stats (for token calibration validation) ──
        "actual_completion_tokens_avg": round(total_output_tokens / num_ok, 2) if num_ok else 0,
        "actual_completion_tokens_min": min((r.get("completion_tokens", 0) for r in ok_results), default=0),
        "actual_completion_tokens_max": max((r.get("completion_tokens", 0) for r in ok_results), default=0),
        "actual_prompt_tokens_avg": round(total_input_tokens / num_ok, 2) if num_ok else 0,
        "actual_prompt_tokens_min": min((r.get("prompt_tokens", 0) for r in ok_results), default=0),
        "actual_prompt_tokens_max": max((r.get("prompt_tokens", 0) for r in ok_results), default=0),

        # token calibration metadata (populated from config if present)
        "target_input_tokens": config.get("target_input_tokens"),
        "target_output_tokens": config.get("target_output_tokens"),
        "calibration_method": config.get("calibration_method"),
        "prompt_mode": config.get("prompt_mode"),
        "token_tolerance": config.get("token_tolerance", 8),

        # throughput
        "request_throughput_rps": round(request_throughput_rps, 2),
        "system_output_tps": round(system_output_tps, 1),
        "system_total_tps": round(system_total_tps, 1),

        # ── standard throughput aliases (NVIDIA: output_token_throughput / request_throughput) ──
        "output_token_throughput": round(system_output_tps, 1),
        "request_throughput": round(request_throughput_rps, 2),

        # per-request TPS (backward compat: tokens_per_sec)
        "per_request_output_tps_avg": round(statistics.mean(per_req_tps), 2) if per_req_tps else 0,
        "per_request_output_tps_p50": _p(per_req_tps, 50),
        "per_request_output_tps_p95": _p(per_req_tps, 95),
        "tokens_per_sec": round(statistics.mean(per_req_tps), 2) if per_req_tps else 0,

        # detail
        "detail": ok_results[:200],
        "fail_detail": fail_results[:50],
    }

    # ── run metric consistency validation ──
    metric_warnings = validate_metric_consistency(summary)
    if summary.get("output_length_mode") == "fixed":
        avg_completion = total_output_tokens / num_ok if num_ok else 0
        target_output_tokens = config["max_tokens"]
        summary["fixed_output_avg_completion_tokens"] = round(avg_completion, 2)
        summary["fixed_output_validation_passed"] = avg_completion >= target_output_tokens * 0.9
        if not summary["fixed_output_validation_passed"]:
            summary["fixed_output_validation_warning"] = (
                "Fixed output mode is enabled, but actual completion tokens are far below the target. "
                "The server may not support min_tokens / ignore_eos, or context/stop constraints may apply.")
            metric_warnings.append(summary["fixed_output_validation_warning"])
    else:
        summary["fixed_output_avg_completion_tokens"] = None
        summary["fixed_output_validation_passed"] = None
        summary["fixed_output_validation_warning"] = ""

    # ── input token validation (if target_input_tokens provided in config) ──
    tgt_in = config.get("target_input_tokens")
    tol = config.get("token_tolerance", 8)
    if tgt_in and num_ok > 0:
        avg_prompt = total_input_tokens / num_ok
        summary["input_token_validation_passed"] = abs(avg_prompt - tgt_in) <= tol
    else:
        summary["input_token_validation_passed"] = None

    summary["metric_warnings"] = metric_warnings

    # ── parser_profile: stream capability profile from per-request results ──
    summary["parser_profile"] = _build_parser_profile(ok_results, config)

    return summary
