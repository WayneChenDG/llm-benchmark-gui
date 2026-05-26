"""customer_metrics.py — Customer-facing benchmark objectives and acceptance metrics.

Implements:
  - BenchmarkObjective / BenchmarkMode string constants
  - compute_single_session_metrics()    — single-session decode speed
  - compute_peak_throughput_summary()   — peak throughput sweep analysis
  - select_peak_valid_point()           — validity-filtered peak selection
  - build_workload_label()              — workload string, e.g. "W1_C1_I1024_O2048"
  - generate_customer_acceptance_report_section() — PMO-ready text block

TASK-BENCHMARK-CUSTOMER-METRICS-001-v1
"""
from __future__ import annotations

import statistics
from typing import Optional

# ── Benchmark Objective Labels ───────────────────────────────────────────────
OBJECTIVE_SINGLE_SESSION = "single_session_decode_speed"
OBJECTIVE_PEAK_THROUGHPUT = "peak_throughput_sweep"

OBJECTIVE_LABELS_ZH = {
    OBJECTIVE_SINGLE_SESSION:  "单会话最大生成速度",
    OBJECTIVE_PEAK_THROUGHPUT: "峰值吞吐扫描",
}
OBJECTIVE_LABELS_EN = {
    OBJECTIVE_SINGLE_SESSION:  "Single-session Decode Speed",
    OBJECTIVE_PEAK_THROUGHPUT: "Peak Throughput Sweep",
}

# ── Benchmark Mode Labels ────────────────────────────────────────────────────
MODE_REAL_API = "real_api_experience"
MODE_ENGINE_CORE = "engine_core"

MODE_LABELS_ZH = {
    MODE_REAL_API:    "真实 API 体验测试",
    MODE_ENGINE_CORE: "引擎核心性能测试",
}
MODE_LABELS_EN = {
    MODE_REAL_API:    "Real API Experience",
    MODE_ENGINE_CORE: "Engine-Core Performance",
}

# ── Mode metadata ────────────────────────────────────────────────────────────
BENCHMARK_MODE_META = {
    MODE_REAL_API: {
        "benchmark_mode":           MODE_REAL_API,
        "api_protocol":             "openai_chat_completions",
        "input_type":               "text_prompt",
        "client_overhead_included": True,
        "chat_template_included":   True,
        "sse_parser_included":      True,
        "engine_backend":           None,
    },
    MODE_ENGINE_CORE: {
        "benchmark_mode":           MODE_ENGINE_CORE,
        "api_protocol":             None,
        "input_type":               "token_ids",
        "client_overhead_included": False,
        "chat_template_included":   False,
        "sse_parser_included":      False,
        "engine_backend":           None,  # filled in by caller (sglang_native, vllm, etc.)
    },
}

# ── Default workloads ────────────────────────────────────────────────────────
WORKLOAD_SINGLE_SESSION_DEFAULT = {
    "concurrency":      1,
    "total_requests":   5,
    "input_tokens":     1024,
    "output_tokens":    2048,
    "stream":           True,
    "temperature":      0.0,
    "min_tokens":       2048,
    "ignore_eos":       True,
}

WORKLOAD_PEAK_SWEEP_DEFAULT_LEVELS = [1, 2, 4, 8, 16, 24, 32]
WORKLOAD_PEAK_SWEEP_EXTENDED_LEVELS = [1, 2, 4, 8, 16, 24, 32, 48, 64]
WORKLOAD_PEAK_SWEEP_DEFAULT = {
    "input_tokens":  1024,
    "output_tokens": 2048,
    "stream":        True,
    "temperature":   0.0,
    "min_tokens":    2048,
    "ignore_eos":    True,
}


# ── Workload label builder ────────────────────────────────────────────────────

def build_workload_label(concurrency: int, input_tokens: Optional[int],
                         output_tokens: Optional[int], workload_index: int = 1) -> str:
    """Build a workload label string, e.g. 'W1_C1_I1024_O2048'."""
    i_part = f"_I{input_tokens}" if input_tokens else ""
    o_part = f"_O{output_tokens}" if output_tokens else ""
    return f"W{workload_index}_C{concurrency}{i_part}{o_part}"


# ── Peak-sweep request count formula ─────────────────────────────────────────

def compute_peak_sweep_requests(concurrency: int, quick_mode: bool = False) -> int:
    """Compute request count for a given concurrency level.

    Formal rule: max(5 * concurrency, 20)
    """
    return max(5 * concurrency, 20)


def compute_tier_requests(concurrency: int, tier: str) -> int:
    """Compute the request count for a concurrency level given a sweep tier name.

    Tier rules:
      quick    → max(2 * concurrency,  8)
      formal   → max(5 * concurrency, 20)
      extended → max(5 * concurrency, 50)
      custom   → raises ValueError (no built-in rule for custom tier)

    Args:
        concurrency: number of concurrent requests
        tier:        "quick" | "formal" | "extended" | "custom"

    Returns:
        int: total request count for this concurrency level
    """
    if tier == "quick":
        return max(2 * concurrency, 8)
    if tier == "formal":
        return max(5 * concurrency, 20)
    if tier == "extended":
        return max(5 * concurrency, 50)
    raise ValueError(f"No built-in request count rule for tier={tier!r}. "
                     "Custom tier requires a user-defined rule.")


# ── Single-session decode speed ───────────────────────────────────────────────

def compute_single_session_metrics(summary: dict) -> dict:
    """Compute customer-facing single-session decode speed metrics.

    Primary formula:
        single_session_decode_tok_s = 1000 / mean_tpot_ms
                                    = 1.0  / tpot_avg_s   (since tpot_avg is in seconds)

    This measures decode-phase generation speed only, NOT including prefill/TTFT.
    Do NOT use: completion_tokens / e2e_latency (that conflates prefill + decode).

    Alternative when token timestamps available:
        single_session_decode_tok_s = completion_tokens / (last_token_time - first_token_time)
        (not yet available in this tool — using TPOT formula)

    Also reports:
        e2e_output_tok_s_per_request_avg = avg(completion_tokens / e2e_latency)
        This is the user-perceived end-to-end speed per request (includes prefill wait).

    Args:
        summary: output of aggregate_results()

    Returns:
        dict with single_session_decode_tok_s and e2e_output_tok_s_per_request_avg
    """
    result: dict = {}

    # ── Primary: 1/TPOT (decode only, prefill excluded) ──
    tpot_avg = summary.get("tpot_avg")
    stream_mode = summary.get("stream_mode", False)
    if stream_mode and tpot_avg and tpot_avg > 0:
        single_session_decode_tok_s = round(1.0 / tpot_avg, 2)
        # Convert mean TPOT ms representation for clarity
        mean_tpot_ms = round(tpot_avg * 1000, 3)
    else:
        single_session_decode_tok_s = None
        mean_tpot_ms = None

    result["single_session_decode_tok_s"] = single_session_decode_tok_s
    result["mean_tpot_ms"] = mean_tpot_ms  # ms unit for report clarity

    # ── Auxiliary: e2e per-request (prefill + decode) ──
    # per_request_output_tps_avg already exists in summary (completion_tokens / e2e_latency avg)
    e2e_output_tok_s = summary.get("per_request_output_tps_avg")
    result["e2e_output_tok_s_per_request_avg"] = (
        round(e2e_output_tok_s, 2) if e2e_output_tok_s and e2e_output_tok_s > 0 else None
    )

    # ── TPOT percentiles in ms for report ──
    result["tpot_p50_ms"] = round(summary.get("tpot_p50", 0) * 1000, 1) if summary.get("tpot_p50") else None
    result["tpot_p95_ms"] = round(summary.get("tpot_p95", 0) * 1000, 1) if summary.get("tpot_p95") else None

    # ── TTFT in ms ──
    ttft_avg = summary.get("ttft_avg", 0)
    result["mean_ttft_ms"] = round(ttft_avg * 1000, 1) if ttft_avg and ttft_avg > 0 else None

    # ── E2E latency in ms ──
    e2e_avg = summary.get("e2e_latency_avg", 0)
    result["mean_e2e_latency_ms"] = round(e2e_avg * 1000, 1) if e2e_avg else None

    return result


# ── Peak validity filter ──────────────────────────────────────────────────────

def _is_valid_peak_point(case: dict, prev_case: Optional[dict] = None) -> tuple[bool, str]:
    """Return (is_valid, reason_if_invalid) for a sweep case point.

    Validity criteria:
      1. success_rate == 100%  AND  fail_count == 0
      2. No timeout errors in fail_detail
      3. No OOM / service_restart (if available)
      4. p99_tpot <= 2 × mean_tpot (if p99 available)
      5. p95_e2e does not abnormally explode vs previous point
    """
    s = case.get("benchmark_summary", {})
    sr = s.get("success_rate", 0)
    fail = s.get("fail", 0)

    # Criterion 1: 100% success
    if sr < 100.0 or fail > 0:
        return False, f"success_rate={sr:.1f}% fail={fail}"

    # Criterion 2: no timeouts
    fail_detail = s.get("fail_detail", [])
    timeout_count = sum(1 for r in fail_detail if "timeout" in (r.get("error_type") or ""))
    if timeout_count > 0:
        return False, f"timeout_count={timeout_count}"

    # Criterion 3: OOM / service restart (future extension; skip if keys absent)
    if s.get("oom_detected") or s.get("service_restart_detected"):
        return False, "OOM or service restart detected"

    # Criterion 4: p99_tpot <= 2 × mean_tpot
    tpot_avg = s.get("tpot_avg", 0)
    tpot_p99 = s.get("tpot_p99", 0)
    if tpot_avg and tpot_p99 and tpot_p99 > 2.0 * tpot_avg:
        return False, f"p99_tpot ({tpot_p99:.3f}s) > 2×mean_tpot ({tpot_avg:.3f}s)"

    # Criterion 5: p95_e2e latency explosion (>3× previous point)
    if prev_case is not None:
        ps_prev = prev_case.get("benchmark_summary", {})
        e2e_prev = ps_prev.get("e2e_latency_p95", 0) or 0
        e2e_cur  = s.get("e2e_latency_p95", 0) or 0
        if e2e_prev > 0 and e2e_cur > 3.0 * e2e_prev:
            return False, (f"p95_e2e exploded {e2e_cur:.3f}s vs previous {e2e_prev:.3f}s "
                           f"(×{e2e_cur / e2e_prev:.1f})")

    return True, ""


def select_peak_valid_point(cases: list[dict]) -> Optional[dict]:
    """Return the case with the highest output token throughput among valid cases."""
    if not cases:
        return None
    valid_cases = []
    for i, case in enumerate(cases):
        prev = cases[i - 1] if i > 0 else None
        ok, _ = _is_valid_peak_point(case, prev)
        if ok:
            valid_cases.append(case)
    if not valid_cases:
        return None
    return max(valid_cases,
               key=lambda ca: ca["benchmark_summary"].get("system_output_tps", 0) or 0)


def _select_recommended_production(
        cases: list[dict], peak_case: Optional[dict]) -> Optional[dict]:
    """Select the recommended production concurrency.

    Rule: find the highest concurrency that satisfies all validity criteria AND
    reaches ≥ 80% of peak output throughput while having a p95_e2e latency
    significantly better (≤ 80%) than the absolute peak point.

    If the peak point itself has good latency or no alternative qualifies,
    return peak_case.
    """
    if peak_case is None:
        return None

    peak_tps = peak_case["benchmark_summary"].get("system_output_tps", 0) or 0
    peak_p95_e2e = peak_case["benchmark_summary"].get("e2e_latency_p95", 0) or 0
    peak_conc = peak_case["concurrency"]

    # Collect all valid points below peak concurrency
    candidate = None
    for i, case in enumerate(cases):
        if case["concurrency"] >= peak_conc:
            continue
        prev = cases[i - 1] if i > 0 else None
        ok, _ = _is_valid_peak_point(case, prev)
        if not ok:
            continue
        tps = case["benchmark_summary"].get("system_output_tps", 0) or 0
        p95_e2e = case["benchmark_summary"].get("e2e_latency_p95", 0) or 0
        # Must reach ≥ 80% of peak throughput
        if peak_tps > 0 and tps < 0.80 * peak_tps:
            continue
        # Must have meaningfully better latency than peak
        if peak_p95_e2e > 0 and p95_e2e >= 0.80 * peak_p95_e2e:
            continue
        candidate = case  # take highest such concurrency below peak

    return candidate  # None → use peak as recommended


# ── Peak throughput sweep summary ─────────────────────────────────────────────

def compute_peak_throughput_summary(cases: list[dict]) -> dict:
    """Compute customer-facing peak throughput summary from sweep cases.

    Returns:
      peak_output_throughput     — best valid concurrency point
      peak_total_token_throughput — best total-token point (may differ)
      recommended_production_concurrency — stability-aware recommendation
      all_valid_points           — list of valid case summaries
      warning_messages           — advisory notices
    """
    if not cases:
        return {
            "peak_output_throughput": None,
            "peak_total_token_throughput": None,
            "recommended_production_concurrency": None,
            "all_valid_points": [],
            "warning_messages": ["No sweep cases available."],
        }

    warnings: list[str] = []

    # Annotate cases with validity
    annotated = []
    for i, case in enumerate(cases):
        prev = cases[i - 1] if i > 0 else None
        ok, reason = _is_valid_peak_point(case, prev)
        annotated.append((case, ok, reason))

    valid_cases = [ca for ca, ok, _ in annotated if ok]

    # ── Peak output throughput (primary) ──
    peak_out_case = (
        max(valid_cases,
            key=lambda ca: ca["benchmark_summary"].get("system_output_tps", 0) or 0)
        if valid_cases else None
    )

    # ── Peak total token throughput (secondary) ──
    peak_tot_case = (
        max(valid_cases,
            key=lambda ca: ca["benchmark_summary"].get("system_total_tps", 0) or 0)
        if valid_cases else None
    )

    def _case_to_summary(ca: dict) -> dict:
        s = ca["benchmark_summary"]
        return {
            "concurrency":                 ca["concurrency"],
            "total_requests":              ca.get("total_requests", 0),
            "success_rate":                s.get("success_rate", 0),
            "output_token_throughput_tok_s": round(s.get("system_output_tps", 0) or 0, 1),
            "total_token_throughput_tok_s":  round(s.get("system_total_tps",   0) or 0, 1),
            "mean_ttft_ms":    round(s.get("ttft_avg", 0) * 1000, 1) if s.get("ttft_avg") else None,
            "mean_tpot_ms":    round(s.get("tpot_avg", 0) * 1000, 1) if s.get("tpot_avg") else None,
            "p95_tpot_ms":     round(s.get("tpot_p95", 0) * 1000, 1) if s.get("tpot_p95") else None,
            "mean_e2e_latency_ms": round(s.get("e2e_latency_avg", 0) * 1000, 1),
            "p95_e2e_latency_ms":  round(s.get("e2e_latency_p95", 0) * 1000, 1),
        }

    # ── Recommended production concurrency ──
    rec_case = _select_recommended_production(cases, peak_out_case)
    if rec_case is not None and rec_case is not peak_out_case:
        rec_conc = rec_case["concurrency"]
        peak_conc = peak_out_case["concurrency"] if peak_out_case else "N/A"
        rec_reason = (
            f"C{rec_conc} 达到接近峰值吞吐（≥80%）同时延迟更稳定，"
            f"比最大吞吐并发 C{peak_conc} 的 P95 E2E 延迟显著更低。"
        )
    elif peak_out_case is not None:
        rec_case = peak_out_case
        rec_conc = peak_out_case["concurrency"]
        rec_reason = f"C{rec_conc} 为有效峰值点，延迟与吞吐综合表现最优。"
    else:
        rec_case = None
        rec_reason = "无有效点满足筛选条件，请检查扫测结果。"

    # ── Gather all valid points ──
    all_valid = [_case_to_summary(ca) for ca, ok, _ in annotated if ok]
    invalid_cases = [(ca, r) for ca, ok, r in annotated if not ok]
    if invalid_cases:
        excluded = ", ".join(
            f"C{ca['concurrency']}({r})" for ca, r in invalid_cases)
        warnings.append(f"以下并发点因不满足有效性条件被排除: {excluded}")

    # ── Warning: total token throughput depends on input length ──
    warnings.append(
        "峰值总 token 吞吐包含输入 token，受 prompt 长度影响较大。"
        "峰值输出 token 吞吐（生成部分）为主要生成吞吐指标。"
    )

    return {
        "peak_output_throughput": _case_to_summary(peak_out_case) if peak_out_case else None,
        "peak_total_token_throughput": _case_to_summary(peak_tot_case) if peak_tot_case else None,
        "recommended_production_concurrency": {
            "concurrency": rec_case["concurrency"] if rec_case else None,
            "reason": rec_reason,
            **(  _case_to_summary(rec_case) if rec_case else {}),
        },
        "all_valid_points": all_valid,
        "warning_messages": warnings,
    }


# ── Customer Acceptance Report Section ───────────────────────────────────────

def generate_customer_acceptance_section(
        summary: dict,
        benchmark_objective: str,
        benchmark_mode: str,
        sweep_peak_summary: Optional[dict] = None,
        workload_label: Optional[str] = None,
        language: str = "zh",
) -> str:
    """Generate the Customer Acceptance Metrics (客户验收指标) report section.

    Args:
        summary:             aggregate_results() output (single benchmark summary)
        benchmark_objective: OBJECTIVE_SINGLE_SESSION or OBJECTIVE_PEAK_THROUGHPUT
        benchmark_mode:      MODE_REAL_API or MODE_ENGINE_CORE
        sweep_peak_summary:  output of compute_peak_throughput_summary() for sweep
        workload_label:      e.g. "W1_C1_I1024_O2048"
        language:            "zh" or "en" (currently zh, en labels shown inline)

    Returns:
        Formatted text section string.
    """
    sep = "─" * 58
    lines: list[str] = []

    lines.append("")
    lines.append("=" * 60)
    lines.append("  ★ 客户验收指标 / Customer Acceptance Metrics")
    lines.append("=" * 60)

    # ── Mode disclaimer ──────────────────────────────────────────────────────
    mode_zh = MODE_LABELS_ZH.get(benchmark_mode, benchmark_mode)
    mode_en = MODE_LABELS_EN.get(benchmark_mode, benchmark_mode)
    obj_zh  = OBJECTIVE_LABELS_ZH.get(benchmark_objective, benchmark_objective)
    obj_en  = OBJECTIVE_LABELS_EN.get(benchmark_objective, benchmark_objective)

    lines.append(f"  测试目标:  {obj_zh} / {obj_en}")
    lines.append(f"  测试模式:  {mode_zh} / {mode_en}")

    if workload_label:
        lines.append(f"  工作负载:  {workload_label}")

    lines.append("")
    if benchmark_mode == MODE_REAL_API:
        lines.append("  【真实 API 体验测试】")
        lines.append("    本测试使用 OpenAI-compatible /v1/chat/completions 接口，")
        lines.append("    包含 API 层、Chat Template、SSE 流式解析和客户端侧开销。")
        lines.append("    结果代表客户实际可见的 API 体验性能，适用于客户验收场景。")
    elif benchmark_mode == MODE_ENGINE_CORE:
        lines.append("  【引擎核心性能测试 — 警告】")
        lines.append("    本测试使用框架原生 benchmark 路径（如 token-ids 输入），")
        lines.append("    不包含 API 层、Chat Template 和 SSE 解析开销。")
        lines.append("  ⚠ 引擎核心性能测试结果代表硬件/框架核心能力，")
        lines.append("    不应作为客户 API 体验指标呈现。")
        lines.append("    如需客户验收，请使用「真实 API 体验测试」模式重新测试。")
    lines.append("")

    # ── Single-session decode speed section ─────────────────────────────────
    if benchmark_objective == OBJECTIVE_SINGLE_SESSION:
        lines.append(sep)
        lines.append("  单会话最大生成速度 / Single-session Decode Speed")
        lines.append(sep)

        ss = compute_single_session_metrics(summary)
        conc = summary.get("concurrency", 1)
        total_req = summary.get("total", 0)
        input_tok = summary.get("actual_prompt_tokens_avg") or summary.get("target_input_tokens")
        output_tok = summary.get("actual_completion_tokens_avg") or summary.get("max_tokens")

        lines.append(f"  Workload:  C{conc} / N{total_req} / "
                     f"I{int(input_tok) if input_tok else '?'} / "
                     f"O{int(output_tok) if output_tok else '?'}")
        lines.append(f"  Success Rate:           {summary.get('success_rate', 0):.1f}%")

        decode_tok_s = ss.get("single_session_decode_tok_s")
        if decode_tok_s is not None:
            mean_tpot_ms = ss.get("mean_tpot_ms")
            lines.append("")
            lines.append("  ┌─────────────────────────────────────────────────────┐")
            lines.append(f"  │ 单会话最大生成速度:  {decode_tok_s:>8.1f} tok/s               │")
            lines.append(f"  │   (= 1000 / Mean TPOT {mean_tpot_ms:.1f} ms)                │")
            lines.append("  └─────────────────────────────────────────────────────┘")
        else:
            lines.append("  ⚠ 单会话最大生成速度: N/A（非流式模式或无有效 TPOT 数据）")

        e2e_tok_s = ss.get("e2e_output_tok_s_per_request_avg")
        if e2e_tok_s is not None:
            lines.append(f"  E2E 感知速度（含 TTFT）: {e2e_tok_s:.1f} tok/s")
            lines.append("    （E2E 感知速度含等待首字时间，不反映纯解码速度）")

        lines.append("")
        lines.append("  TTFT 延迟指标:")
        ttft_ms = ss.get("mean_ttft_ms")
        if ttft_ms is not None:
            lines.append(f"    Mean TTFT:  {ttft_ms:.1f} ms")
        else:
            lines.append("    Mean TTFT:  N/A")

        lines.append("  TPOT 延迟指标:")
        tpot_ms = ss.get("mean_tpot_ms")
        tpot_p50 = ss.get("tpot_p50_ms")
        tpot_p95 = ss.get("tpot_p95_ms")
        if tpot_ms is not None:
            lines.append(f"    Mean TPOT:  {tpot_ms:.1f} ms/tok")
            if tpot_p50:
                lines.append(f"    p50 TPOT:   {tpot_p50:.1f} ms/tok")
            if tpot_p95:
                lines.append(f"    p95 TPOT:   {tpot_p95:.1f} ms/tok")
        else:
            lines.append("    Mean TPOT:  N/A（流式模式下可用）")

        e2e_ms = ss.get("mean_e2e_latency_ms")
        if e2e_ms:
            lines.append(f"  Mean E2E 延迟:  {e2e_ms:.1f} ms")

        output_tps = summary.get("system_output_tps", 0)
        total_tps  = summary.get("system_total_tps",  0)
        lines.append("")
        lines.append("  系统级吞吐（单会话，供参考）:")
        lines.append(f"    输出 Token 吞吐:  {output_tps:.1f} tok/s")
        lines.append(f"    总 Token 吞吐:    {total_tps:.1f} tok/s  （输入+输出）")
        lines.append("")
        lines.append("  ★ PMO 验收口径:")
        if decode_tok_s is not None:
            lines.append(
                f"    在 C{conc} / I{int(input_tok) if input_tok else '?'} / "
                f"O{int(output_tok) if output_tok else '?'} 下，"
            )
            lines.append(
                f"    按 Mean TPOT {tpot_ms:.1f} ms 换算为 {decode_tok_s:.1f} tok/s 单会话最大生成速度。"
            )
        else:
            lines.append("    ⚠ 请以流式模式运行单会话测试以获得 TPOT 指标。")

    # ── Peak throughput sweep section ────────────────────────────────────────
    elif benchmark_objective == OBJECTIVE_PEAK_THROUGHPUT and sweep_peak_summary:
        lines.append(sep)
        lines.append("  峰值吞吐扫描 / Peak Throughput Sweep")
        lines.append(sep)

        peak_out = sweep_peak_summary.get("peak_output_throughput")
        peak_tot = sweep_peak_summary.get("peak_total_token_throughput")
        rec      = sweep_peak_summary.get("recommended_production_concurrency")

        if peak_out:
            p_conc  = peak_out.get("concurrency", "?")
            p_otps  = peak_out.get("output_token_throughput_tok_s", 0)
            p_ttps  = peak_out.get("total_token_throughput_tok_s", 0)
            p_sr    = peak_out.get("success_rate", 0)
            p_tpot  = peak_out.get("mean_tpot_ms")
            p_p95t  = peak_out.get("p95_tpot_ms")
            p_p95e  = peak_out.get("p95_e2e_latency_ms")

            lines.append("")
            lines.append("  ┌─────────────────────────────────────────────────────┐")
            lines.append(f"  │ 峰值输出吞吐:  {p_otps:>8.1f} output tok/s  @C{p_conc}        │")
            lines.append(f"  │ 峰值总 token:  {p_ttps:>8.1f} total  tok/s  @C{p_conc}        │")
            lines.append(f"  │   (总 token = 输入 + 输出 token，受 prompt 长度影响)  │")
            lines.append("  └─────────────────────────────────────────────────────┘")
            lines.append(f"  Success Rate:    {p_sr:.1f}%")
            if p_tpot:
                lines.append(f"  Mean TPOT:       {p_tpot:.1f} ms")
            if p_p95t:
                lines.append(f"  p95 TPOT:        {p_p95t:.1f} ms")
            if p_p95e:
                lines.append(f"  p95 E2E Latency: {p_p95e:.1f} ms")
        else:
            lines.append("  ⚠ 无有效峰值点（所有并发点均未通过有效性筛选）")

        if rec and rec.get("concurrency"):
            r_conc = rec["concurrency"]
            r_otps = rec.get("output_token_throughput_tok_s", 0)
            r_reason = rec.get("reason", "")
            lines.append("")
            lines.append(f"  推荐生产并发: C{r_conc}  ({r_otps:.1f} output tok/s)")
            lines.append(f"    原因: {r_reason}")

        # ── Per-concurrency table ──
        all_valid = sweep_peak_summary.get("all_valid_points", [])
        if all_valid:
            lines.append("")
            lines.append("  有效并发点汇总:")
            lines.append(f"  {'并发':>5}  {'输出吞吐':>10}  {'总吞吐':>10}  {'成功率':>7}  "
                         f"{'TPOT':>8}  {'P95 E2E':>9}")
            lines.append(f"  {'C':>5}  {'tok/s':>10}  {'tok/s':>10}  {'%':>7}  "
                         f"{'ms':>8}  {'ms':>9}")
            lines.append("  " + "-" * 56)
            for pt in all_valid:
                tpot_s = f"{pt['mean_tpot_ms']:.1f}" if pt.get("mean_tpot_ms") else "N/A"
                p95e_s = f"{pt['p95_e2e_latency_ms']:.1f}" if pt.get("p95_e2e_latency_ms") else "N/A"
                lines.append(
                    f"  {pt['concurrency']:>5}  "
                    f"{pt['output_token_throughput_tok_s']:>10.1f}  "
                    f"{pt['total_token_throughput_tok_s']:>10.1f}  "
                    f"{pt['success_rate']:>6.1f}%  "
                    f"{tpot_s:>8}  "
                    f"{p95e_s:>9}"
                )

        # ── Warnings ──
        sw = sweep_peak_summary.get("warning_messages", [])
        if sw:
            lines.append("")
            for w in sw:
                lines.append(f"  ⚠ {w}")

        lines.append("")
        lines.append("  ★ PMO 验收口径:")
        if peak_out:
            lines.append(
                f"    在 C{peak_out['concurrency']} 并发下达到最高稳定输出吞吐，"
                f"{peak_out['output_token_throughput_tok_s']:.1f} output tok/s。"
            )
            lines.append(
                f"    在同一并发下，总 token 吞吐为 "
                f"{peak_out['total_token_throughput_tok_s']:.1f} total tok/s。"
            )
            lines.append("    总 token = 输入 token + 输出 token，受 prompt 长度影响。")
        else:
            lines.append("    ⚠ 无有效峰值点，无法输出 PMO 验收口径。")

    else:
        lines.append("  （本次测试无客户验收指标。）")

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


# ── Field augmentation for aggregate_results ─────────────────────────────────

def augment_summary_with_customer_fields(
        summary: dict,
        config: dict,
) -> dict:
    """Add customer-facing fields to an aggregate_results() summary dict.

    Called AFTER aggregate_results(); modifies summary in-place and returns it.

    New fields added:
      benchmark_objective, benchmark_mode, workload, input_type,
      api_protocol, engine_backend, client_overhead_included,
      chat_template_included, sse_parser_included,
      single_session_decode_tok_s, e2e_output_tok_s_per_request_avg,
      output_token_throughput_tok_s, total_token_throughput_tok_s
    """
    objective = config.get("benchmark_objective", OBJECTIVE_SINGLE_SESSION)
    mode      = config.get("benchmark_mode",      MODE_REAL_API)
    mode_meta = BENCHMARK_MODE_META.get(mode, BENCHMARK_MODE_META[MODE_REAL_API])

    # ── Objective / mode / workload ──
    summary["benchmark_objective"] = objective
    summary["benchmark_mode"]      = mode
    summary.setdefault("input_type",               mode_meta["input_type"])
    summary.setdefault("api_protocol",              mode_meta["api_protocol"])
    summary.setdefault("engine_backend",            config.get("engine_backend",
                                                               mode_meta["engine_backend"]))
    summary.setdefault("client_overhead_included",  mode_meta["client_overhead_included"])
    summary.setdefault("chat_template_included",    mode_meta["chat_template_included"])
    summary.setdefault("sse_parser_included",       mode_meta["sse_parser_included"])

    # ── Workload label ──
    conc = summary.get("concurrency", 1)
    target_in  = config.get("target_input_tokens")  or summary.get("actual_prompt_tokens_avg")
    target_out = config.get("target_output_tokens")  or summary.get("max_tokens")
    summary["workload"] = build_workload_label(conc, target_in, target_out)

    # ── Single-session decode speed ──
    ss = compute_single_session_metrics(summary)
    summary["single_session_decode_tok_s"]      = ss.get("single_session_decode_tok_s")
    summary["e2e_output_tok_s_per_request_avg"] = ss.get("e2e_output_tok_s_per_request_avg")
    summary["mean_tpot_ms"]                     = ss.get("mean_tpot_ms")
    summary["mean_ttft_ms"]                     = ss.get("mean_ttft_ms")

    # ── Throughput aliases (tok/s naming for JSON clarity) ──
    summary["output_token_throughput_tok_s"] = summary.get("system_output_tps", 0)
    summary["total_token_throughput_tok_s"]  = summary.get("system_total_tps",  0)

    return summary
