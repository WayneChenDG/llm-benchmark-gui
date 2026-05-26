"""tests/test_customer_metrics.py — Unit tests for customer_metrics.py.

Covers:
  - C1 single-session decode speed calculation
  - Sweep peak selection (validity filter, max output TPS)
  - Total token throughput vs output token throughput distinction
  - real_api_experience mode labels and metadata
  - engine_core mode labels and metadata
  - Sweep scale/tier preset system (TASK-BENCHMARK-SWEEP-PRESETS-SIMPLIFY-001-v2)

TASK-BENCHMARK-CUSTOMER-METRICS-001-v1
TASK-BENCHMARK-SWEEP-PRESETS-SIMPLIFY-001-v2
"""
from __future__ import annotations

import sys
import os
import math

# Ensure package is importable without installation
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from llm_benchmark_app.customer_metrics import (
    OBJECTIVE_SINGLE_SESSION,
    OBJECTIVE_PEAK_THROUGHPUT,
    MODE_REAL_API,
    MODE_ENGINE_CORE,
    MODE_LABELS_ZH,
    MODE_LABELS_EN,
    OBJECTIVE_LABELS_ZH,
    OBJECTIVE_LABELS_EN,
    BENCHMARK_MODE_META,
    compute_single_session_metrics,
    compute_peak_throughput_summary,
    select_peak_valid_point,
    _is_valid_peak_point,
    build_workload_label,
    compute_peak_sweep_requests,
    compute_tier_requests,
    generate_customer_acceptance_section,
    augment_summary_with_customer_fields,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_single_session_summary(
    tpot_avg_s: float = 0.02,     # 20 ms TPOT → 50 tok/s
    stream_mode: bool = True,
    per_request_output_tps_avg: float = 45.0,
    ttft_avg: float = 0.080,
    e2e_latency_avg: float = 2.0,
    tpot_p50: float = 0.018,
    tpot_p95: float = 0.025,
    concurrency: int = 1,
    success_rate: float = 100.0,
) -> dict:
    """Build a minimal single-session aggregate_results() summary dict."""
    return {
        "concurrency":               concurrency,
        "total":                     5,
        "success_rate":              success_rate,
        "tpot_avg":                  tpot_avg_s,
        "stream_mode":               stream_mode,
        "per_request_output_tps_avg": per_request_output_tps_avg,
        "ttft_avg":                  ttft_avg,
        "e2e_latency_avg":           e2e_latency_avg,
        "tpot_p50":                  tpot_p50,
        "tpot_p95":                  tpot_p95,
        "system_output_tps":         48.0,
        "system_total_tps":          120.0,
    }


def _make_sweep_case(
    concurrency: int,
    output_tps: float = 100.0,
    total_tps: float = 250.0,
    success_rate: float = 100.0,
    fail: int = 0,
    tpot_avg: float = 0.020,
    tpot_p99: float = 0.030,
    e2e_p95: float = 2.0,
) -> dict:
    """Build a minimal sweep case dict."""
    return {
        "concurrency": concurrency,
        "total_requests": concurrency * 5,
        "benchmark_summary": {
            "success_rate":      success_rate,
            "fail":              fail,
            "fail_detail":       [],
            "system_output_tps": output_tps,
            "system_total_tps":  total_tps,
            "tpot_avg":          tpot_avg,
            "tpot_p99":          tpot_p99,
            "e2e_latency_p95":   e2e_p95,
            "ttft_avg":          0.050,
            "tpot_p95":          0.025,
            "e2e_latency_avg":   1.5,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tests: C1 Single-session Decode Speed Calculation
# ─────────────────────────────────────────────────────────────────────────────

class TestSingleSessionMetrics:
    """C1 single-session decode speed formula validation."""

    def test_primary_formula_20ms_tpot(self):
        """tpot_avg = 0.020 s (20 ms) → decode_tok_s = 50.0"""
        summary = _make_single_session_summary(tpot_avg_s=0.020)
        result = compute_single_session_metrics(summary)
        assert result["single_session_decode_tok_s"] == pytest.approx(50.0, abs=0.1)

    def test_primary_formula_10ms_tpot(self):
        """tpot_avg = 0.010 s (10 ms) → decode_tok_s = 100.0"""
        summary = _make_single_session_summary(tpot_avg_s=0.010)
        result = compute_single_session_metrics(summary)
        assert result["single_session_decode_tok_s"] == pytest.approx(100.0, abs=0.1)

    def test_primary_formula_50ms_tpot(self):
        """tpot_avg = 0.050 s (50 ms) → decode_tok_s = 20.0"""
        summary = _make_single_session_summary(tpot_avg_s=0.050)
        result = compute_single_session_metrics(summary)
        assert result["single_session_decode_tok_s"] == pytest.approx(20.0, abs=0.1)

    def test_mean_tpot_ms_reported(self):
        """mean_tpot_ms should be in milliseconds (tpot_avg_s * 1000)."""
        summary = _make_single_session_summary(tpot_avg_s=0.020)
        result = compute_single_session_metrics(summary)
        assert result["mean_tpot_ms"] == pytest.approx(20.0, abs=0.1)

    def test_non_stream_returns_none(self):
        """Non-streaming mode: single_session_decode_tok_s must be None."""
        summary = _make_single_session_summary(stream_mode=False)
        result = compute_single_session_metrics(summary)
        assert result["single_session_decode_tok_s"] is None
        assert result["mean_tpot_ms"] is None

    def test_zero_tpot_returns_none(self):
        """Zero TPOT (e.g. server doesn't report) → None, not division error."""
        summary = _make_single_session_summary(tpot_avg_s=0.0)
        result = compute_single_session_metrics(summary)
        assert result["single_session_decode_tok_s"] is None

    def test_e2e_output_tok_s_is_distinct(self):
        """E2E tok/s is reported separately and differs from decode speed."""
        summary = _make_single_session_summary(
            tpot_avg_s=0.020,               # decode: 50.0 tok/s
            per_request_output_tps_avg=35.0,  # e2e: 35.0 tok/s (includes TTFT wait)
        )
        result = compute_single_session_metrics(summary)
        decode = result["single_session_decode_tok_s"]
        e2e    = result["e2e_output_tok_s_per_request_avg"]
        assert decode == pytest.approx(50.0, abs=0.1)
        assert e2e    == pytest.approx(35.0, abs=0.1)
        # The two must differ (prefill wait makes e2e < decode)
        assert decode != e2e

    def test_ttft_in_ms(self):
        """TTFT is reported in milliseconds."""
        summary = _make_single_session_summary(ttft_avg=0.080)
        result = compute_single_session_metrics(summary)
        assert result["mean_ttft_ms"] == pytest.approx(80.0, abs=0.5)

    def test_tpot_percentiles_in_ms(self):
        """p50 and p95 TPOT percentiles are in milliseconds."""
        summary = _make_single_session_summary(tpot_p50=0.018, tpot_p95=0.030)
        result = compute_single_session_metrics(summary)
        assert result["tpot_p50_ms"] == pytest.approx(18.0, abs=0.5)
        assert result["tpot_p95_ms"] == pytest.approx(30.0, abs=0.5)

    def test_workload_label_c1(self):
        """build_workload_label for C1/I1024/O2048."""
        label = build_workload_label(1, 1024, 2048, workload_index=1)
        assert label == "W1_C1_I1024_O2048"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Sweep Peak Selection
# ─────────────────────────────────────────────────────────────────────────────

class TestPeakSweepSelection:
    """Validity filter and peak output TPS selection."""

    def test_all_valid_selects_max_output_tps(self):
        """All cases valid → select the one with highest output_tps."""
        cases = [
            _make_sweep_case(1,  output_tps=50.0),
            _make_sweep_case(4,  output_tps=120.0),
            _make_sweep_case(8,  output_tps=200.0),   # ← highest
            _make_sweep_case(16, output_tps=180.0),
        ]
        peak = select_peak_valid_point(cases)
        assert peak is not None
        assert peak["concurrency"] == 8
        assert peak["benchmark_summary"]["system_output_tps"] == pytest.approx(200.0)

    def test_failure_case_excluded(self):
        """Case with success_rate < 100% is excluded from peak selection."""
        cases = [
            _make_sweep_case(1,  output_tps=50.0),
            _make_sweep_case(4,  output_tps=120.0),
            _make_sweep_case(8,  output_tps=300.0, success_rate=95.0, fail=2),  # invalid
        ]
        peak = select_peak_valid_point(cases)
        assert peak is not None
        assert peak["concurrency"] == 4  # 8 excluded

    def test_p99_tpot_explosion_excluded(self):
        """Case where p99_tpot > 2× mean_tpot is excluded."""
        cases = [
            _make_sweep_case(1, output_tps=50.0, tpot_avg=0.020, tpot_p99=0.025),  # valid
            _make_sweep_case(4, output_tps=200.0, tpot_avg=0.020, tpot_p99=0.200),  # p99=10×mean → invalid
        ]
        peak = select_peak_valid_point(cases)
        assert peak is not None
        assert peak["concurrency"] == 1

    def test_p95_e2e_explosion_excluded(self):
        """Case where p95_e2e > 3× previous case's p95_e2e is excluded."""
        cases = [
            _make_sweep_case(1, output_tps=80.0,  e2e_p95=1.0),
            _make_sweep_case(4, output_tps=200.0, e2e_p95=4.0),   # 4× previous → invalid
        ]
        peak = select_peak_valid_point(cases)
        assert peak is not None
        assert peak["concurrency"] == 1  # C4 excluded

    def test_no_valid_cases_returns_none(self):
        """All cases invalid → select_peak_valid_point returns None."""
        cases = [
            _make_sweep_case(1, success_rate=90.0, fail=1),
            _make_sweep_case(4, success_rate=80.0, fail=4),
        ]
        result = select_peak_valid_point(cases)
        assert result is None

    def test_empty_cases_returns_none(self):
        assert select_peak_valid_point([]) is None

    def test_is_valid_peak_point_100pct(self):
        """100% success case is valid."""
        case = _make_sweep_case(1, success_rate=100.0, fail=0)
        ok, reason = _is_valid_peak_point(case)
        assert ok is True
        assert reason == ""

    def test_is_valid_peak_point_fail_nonzero(self):
        """fail > 0 is invalid even if success_rate appears high (boundary guard)."""
        case = _make_sweep_case(1, success_rate=100.0, fail=1)
        ok, reason = _is_valid_peak_point(case)
        assert ok is False

    def test_timeout_in_fail_detail_excluded(self):
        """Timeout errors in fail_detail cause exclusion."""
        case = _make_sweep_case(4, success_rate=100.0, fail=0)
        # Inject a timeout failure record into fail_detail
        case["benchmark_summary"]["fail_detail"] = [{"error_type": "timeout"}]
        ok, reason = _is_valid_peak_point(case)
        assert ok is False
        assert "timeout" in reason


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Output vs Total Token Throughput Distinction
# ─────────────────────────────────────────────────────────────────────────────

class TestThroughputDistinction:
    """Output token throughput must be distinct from total token throughput."""

    def test_output_tps_vs_total_tps_in_case_summary(self):
        """A sweep case has distinct output_tps and total_tps."""
        case = _make_sweep_case(8, output_tps=200.0, total_tps=500.0)
        s = case["benchmark_summary"]
        assert s["system_output_tps"] == pytest.approx(200.0)
        assert s["system_total_tps"]  == pytest.approx(500.0)
        assert s["system_total_tps"] > s["system_output_tps"]

    def test_peak_throughput_summary_separates_out_and_total(self):
        """compute_peak_throughput_summary returns separate peak_output and peak_total keys."""
        cases = [
            _make_sweep_case(1,  output_tps=50.0,  total_tps=130.0),
            _make_sweep_case(4,  output_tps=120.0, total_tps=310.0),
            _make_sweep_case(8,  output_tps=200.0, total_tps=520.0),
            _make_sweep_case(16, output_tps=180.0, total_tps=480.0),
        ]
        result = compute_peak_throughput_summary(cases)
        peak_out = result["peak_output_throughput"]
        peak_tot = result["peak_total_token_throughput"]
        assert peak_out is not None
        assert peak_tot is not None
        # Both keys exist
        assert "output_token_throughput_tok_s" in peak_out
        assert "total_token_throughput_tok_s"  in peak_out
        # Total > output (prompt tokens included)
        assert peak_out["total_token_throughput_tok_s"] > peak_out["output_token_throughput_tok_s"]

    def test_augment_adds_throughput_aliases(self):
        """augment_summary_with_customer_fields adds output_token_throughput_tok_s and total_token_throughput_tok_s."""
        summary = _make_single_session_summary()
        summary["system_output_tps"] = 48.0
        summary["system_total_tps"]  = 120.0
        config = {
            "benchmark_objective": OBJECTIVE_SINGLE_SESSION,
            "benchmark_mode":      MODE_REAL_API,
        }
        augment_summary_with_customer_fields(summary, config)
        assert summary["output_token_throughput_tok_s"] == pytest.approx(48.0)
        assert summary["total_token_throughput_tok_s"]  == pytest.approx(120.0)
        assert summary["output_token_throughput_tok_s"] != summary["total_token_throughput_tok_s"]

    def test_augment_adds_single_session_decode_speed(self):
        """augment adds single_session_decode_tok_s from TPOT."""
        summary = _make_single_session_summary(tpot_avg_s=0.020)
        config = {
            "benchmark_objective": OBJECTIVE_SINGLE_SESSION,
            "benchmark_mode":      MODE_REAL_API,
        }
        augment_summary_with_customer_fields(summary, config)
        assert summary["single_session_decode_tok_s"] == pytest.approx(50.0, abs=0.1)

    def test_warning_message_total_includes_input(self):
        """Peak throughput summary warns that total token includes input tokens."""
        cases = [_make_sweep_case(1), _make_sweep_case(4, output_tps=150.0)]
        result = compute_peak_throughput_summary(cases)
        warnings = result.get("warning_messages", [])
        assert any("总 token" in w or "输入 token" in w or "total" in w.lower()
                   for w in warnings), \
            f"No total-token warning found in: {warnings}"

    def test_pmo_text_has_three_sentences(self):
        """PMO 验收口径 for peak sweep includes 3 distinct acceptance sentences."""
        cases = [
            _make_sweep_case(1,  output_tps=50.0,  total_tps=130.0),
            _make_sweep_case(8,  output_tps=200.0, total_tps=520.0),
            _make_sweep_case(16, output_tps=180.0, total_tps=480.0),
        ]
        peak_summary = compute_peak_throughput_summary(cases)
        single_summary = _make_single_session_summary()
        text = generate_customer_acceptance_section(
            summary=single_summary,
            benchmark_objective=OBJECTIVE_PEAK_THROUGHPUT,
            benchmark_mode=MODE_REAL_API,
            sweep_peak_summary=peak_summary,
        )
        # Must mention: peak output tok/s, total tok/s, concurrency
        assert "output tok/s" in text
        assert "total tok/s" in text
        assert "PMO" in text or "验收口径" in text


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Mode Labels — real_api_experience
# ─────────────────────────────────────────────────────────────────────────────

class TestRealApiExperienceModeLabels:
    """real_api_experience mode has correct labels and metadata."""

    def test_mode_constant_value(self):
        assert MODE_REAL_API == "real_api_experience"

    def test_zh_label_exists(self):
        assert MODE_REAL_API in MODE_LABELS_ZH
        label = MODE_LABELS_ZH[MODE_REAL_API]
        assert "真实" in label or "API" in label, f"Unexpected zh label: {label!r}"

    def test_en_label_exists(self):
        assert MODE_REAL_API in MODE_LABELS_EN
        label = MODE_LABELS_EN[MODE_REAL_API]
        assert "API" in label or "Experience" in label, f"Unexpected en label: {label!r}"

    def test_metadata_protocol_is_openai(self):
        meta = BENCHMARK_MODE_META[MODE_REAL_API]
        assert meta["api_protocol"] == "openai_chat_completions"

    def test_metadata_input_type_is_text_prompt(self):
        meta = BENCHMARK_MODE_META[MODE_REAL_API]
        assert meta["input_type"] == "text_prompt"

    def test_metadata_client_overhead_included(self):
        meta = BENCHMARK_MODE_META[MODE_REAL_API]
        assert meta["client_overhead_included"] is True

    def test_metadata_chat_template_included(self):
        meta = BENCHMARK_MODE_META[MODE_REAL_API]
        assert meta["chat_template_included"] is True

    def test_metadata_sse_parser_included(self):
        meta = BENCHMARK_MODE_META[MODE_REAL_API]
        assert meta["sse_parser_included"] is True

    def test_acceptance_section_mentions_openai(self):
        """Acceptance section for real_api mode mentions API/SSE."""
        summary = _make_single_session_summary()
        text = generate_customer_acceptance_section(
            summary=summary,
            benchmark_objective=OBJECTIVE_SINGLE_SESSION,
            benchmark_mode=MODE_REAL_API,
        )
        # Should mention API experience or client overhead
        assert ("API" in text) or ("客户" in text) or ("SSE" in text)

    def test_acceptance_section_no_engine_core_warning(self):
        """real_api mode must NOT show engine-core warning."""
        summary = _make_single_session_summary()
        text = generate_customer_acceptance_section(
            summary=summary,
            benchmark_objective=OBJECTIVE_SINGLE_SESSION,
            benchmark_mode=MODE_REAL_API,
        )
        assert "不应作为客户 API 体验指标" not in text

    def test_augment_real_api_metadata_in_summary(self):
        """augment_summary_with_customer_fields adds real_api metadata fields."""
        summary = _make_single_session_summary()
        config = {
            "benchmark_objective": OBJECTIVE_SINGLE_SESSION,
            "benchmark_mode":      MODE_REAL_API,
        }
        augment_summary_with_customer_fields(summary, config)
        assert summary["benchmark_mode"]             == MODE_REAL_API
        assert summary["api_protocol"]               == "openai_chat_completions"
        assert summary["input_type"]                 == "text_prompt"
        assert summary["client_overhead_included"]   is True
        assert summary["chat_template_included"]     is True
        assert summary["sse_parser_included"]        is True


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Mode Labels — engine_core
# ─────────────────────────────────────────────────────────────────────────────

class TestEngineCoreModeLabels:
    """engine_core mode has correct labels and metadata; acceptance section warns."""

    def test_mode_constant_value(self):
        assert MODE_ENGINE_CORE == "engine_core"

    def test_zh_label_exists(self):
        assert MODE_ENGINE_CORE in MODE_LABELS_ZH
        label = MODE_LABELS_ZH[MODE_ENGINE_CORE]
        assert "引擎" in label or "engine" in label.lower(), f"Unexpected zh label: {label!r}"

    def test_en_label_exists(self):
        assert MODE_ENGINE_CORE in MODE_LABELS_EN
        label = MODE_LABELS_EN[MODE_ENGINE_CORE]
        assert "Engine" in label or "Core" in label, f"Unexpected en label: {label!r}"

    def test_metadata_input_type_is_token_ids(self):
        meta = BENCHMARK_MODE_META[MODE_ENGINE_CORE]
        assert meta["input_type"] == "token_ids"

    def test_metadata_client_overhead_not_included(self):
        meta = BENCHMARK_MODE_META[MODE_ENGINE_CORE]
        assert meta["client_overhead_included"] is False

    def test_metadata_chat_template_not_included(self):
        meta = BENCHMARK_MODE_META[MODE_ENGINE_CORE]
        assert meta["chat_template_included"] is False

    def test_metadata_sse_parser_not_included(self):
        meta = BENCHMARK_MODE_META[MODE_ENGINE_CORE]
        assert meta["sse_parser_included"] is False

    def test_metadata_api_protocol_is_none(self):
        meta = BENCHMARK_MODE_META[MODE_ENGINE_CORE]
        assert meta["api_protocol"] is None

    def test_acceptance_section_shows_warning(self):
        """engine_core acceptance section MUST show a prominent warning."""
        summary = _make_single_session_summary()
        text = generate_customer_acceptance_section(
            summary=summary,
            benchmark_objective=OBJECTIVE_SINGLE_SESSION,
            benchmark_mode=MODE_ENGINE_CORE,
        )
        # Must contain a clear warning that this is not customer API performance
        assert ("不应作为客户 API 体验指标" in text) or ("⚠" in text and "引擎" in text)

    def test_acceptance_section_warns_use_real_api_instead(self):
        """engine_core section should recommend using real_api mode for acceptance."""
        summary = _make_single_session_summary()
        text = generate_customer_acceptance_section(
            summary=summary,
            benchmark_objective=OBJECTIVE_SINGLE_SESSION,
            benchmark_mode=MODE_ENGINE_CORE,
        )
        # Recommend real API test
        assert "真实 API" in text or "real_api" in text.lower()

    def test_augment_engine_core_metadata_in_summary(self):
        """augment_summary_with_customer_fields adds engine_core metadata fields."""
        summary = _make_single_session_summary()
        config = {
            "benchmark_objective": OBJECTIVE_PEAK_THROUGHPUT,
            "benchmark_mode":      MODE_ENGINE_CORE,
        }
        augment_summary_with_customer_fields(summary, config)
        assert summary["benchmark_mode"]            == MODE_ENGINE_CORE
        assert summary["input_type"]                == "token_ids"
        assert summary["client_overhead_included"]  is False
        assert summary["chat_template_included"]    is False
        assert summary["sse_parser_included"]       is False
        assert summary["api_protocol"]              is None


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Miscellaneous / edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestMiscellaneous:
    """Edge cases, formula constants, utility functions."""

    def test_compute_peak_sweep_requests_formula(self):
        """max(5 * concurrency, 20)."""
        assert compute_peak_sweep_requests(1)  == max(5 * 1,  20)   # 20
        assert compute_peak_sweep_requests(2)  == max(5 * 2,  20)   # 20
        assert compute_peak_sweep_requests(4)  == max(5 * 4,  20)   # 20
        assert compute_peak_sweep_requests(5)  == max(5 * 5,  20)   # 25
        assert compute_peak_sweep_requests(8)  == max(5 * 8,  20)   # 40
        assert compute_peak_sweep_requests(16) == max(5 * 16, 20)   # 80
        assert compute_peak_sweep_requests(32) == max(5 * 32, 20)   # 160

    def test_objective_constants(self):
        assert OBJECTIVE_SINGLE_SESSION  == "single_session_decode_speed"
        assert OBJECTIVE_PEAK_THROUGHPUT == "peak_throughput_sweep"

    def test_objective_labels_zh_exist(self):
        assert OBJECTIVE_SINGLE_SESSION  in OBJECTIVE_LABELS_ZH
        assert OBJECTIVE_PEAK_THROUGHPUT in OBJECTIVE_LABELS_ZH

    def test_objective_labels_en_exist(self):
        assert OBJECTIVE_SINGLE_SESSION  in OBJECTIVE_LABELS_EN
        assert OBJECTIVE_PEAK_THROUGHPUT in OBJECTIVE_LABELS_EN

    def test_build_workload_label_full(self):
        assert build_workload_label(8, 1024, 2048, 1) == "W1_C8_I1024_O2048"

    def test_build_workload_label_no_io(self):
        label = build_workload_label(4, None, None, 2)
        assert label == "W2_C4"

    def test_peak_throughput_summary_empty(self):
        result = compute_peak_throughput_summary([])
        assert result["peak_output_throughput"] is None
        assert result["peak_total_token_throughput"] is None
        assert result["recommended_production_concurrency"] is None
        assert result["all_valid_points"] == []

    def test_augment_does_not_raise_on_minimal_summary(self):
        """augment_summary_with_customer_fields must not raise on minimal input."""
        summary = {"concurrency": 1, "total": 5, "success_rate": 100.0}
        config  = {"benchmark_objective": OBJECTIVE_SINGLE_SESSION,
                   "benchmark_mode": MODE_REAL_API}
        # Should not raise
        augment_summary_with_customer_fields(summary, config)
        assert "benchmark_objective" in summary
        assert "benchmark_mode"      in summary


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Sweep Scale / Tier Preset System
# (TASK-BENCHMARK-SWEEP-PRESETS-SIMPLIFY-001-v2)
# ─────────────────────────────────────────────────────────────────────────────

class TestSweepTierRequests:
    """compute_tier_requests() formula validation for all three named tiers."""

    # ── quick: max(2*C, 8) ──────────────────────────────────────────────────

    def test_quick_c1(self):
        assert compute_tier_requests(1, "quick") == max(2 * 1, 8)   # 8

    def test_quick_c4(self):
        assert compute_tier_requests(4, "quick") == max(2 * 4, 8)   # 8

    def test_quick_c5(self):
        assert compute_tier_requests(5, "quick") == max(2 * 5, 8)   # 10

    def test_quick_c16(self):
        assert compute_tier_requests(16, "quick") == max(2 * 16, 8)  # 32

    def test_quick_c32(self):
        assert compute_tier_requests(32, "quick") == max(2 * 32, 8)  # 64

    def test_quick_minimum_is_8(self):
        for c in [1, 2, 3, 4]:
            assert compute_tier_requests(c, "quick") == 8

    # ── formal: max(5*C, 20) ────────────────────────────────────────────────

    def test_formal_c1(self):
        assert compute_tier_requests(1, "formal") == max(5 * 1, 20)  # 20

    def test_formal_c4(self):
        assert compute_tier_requests(4, "formal") == max(5 * 4, 20)  # 20

    def test_formal_c5(self):
        assert compute_tier_requests(5, "formal") == max(5 * 5, 20)  # 25

    def test_formal_c8(self):
        assert compute_tier_requests(8, "formal") == max(5 * 8, 20)  # 40

    def test_formal_c32(self):
        assert compute_tier_requests(32, "formal") == max(5 * 32, 20)  # 160

    def test_formal_minimum_is_20(self):
        for c in [1, 2, 3, 4]:
            assert compute_tier_requests(c, "formal") == 20

    # ── extended: max(5*C, 50) ──────────────────────────────────────────────

    def test_extended_c1(self):
        assert compute_tier_requests(1, "extended") == max(5 * 1, 50)   # 50

    def test_extended_c10(self):
        assert compute_tier_requests(10, "extended") == max(5 * 10, 50)  # 50

    def test_extended_c11(self):
        assert compute_tier_requests(11, "extended") == max(5 * 11, 50)  # 55

    def test_extended_c32(self):
        assert compute_tier_requests(32, "extended") == max(5 * 32, 50)  # 160

    def test_extended_minimum_is_50(self):
        for c in [1, 2, 4, 8, 10]:
            assert compute_tier_requests(c, "extended") == 50

    # ── custom tier raises ───────────────────────────────────────────────────

    def test_custom_tier_raises(self):
        with pytest.raises(ValueError, match="custom"):
            compute_tier_requests(8, "custom")

    def test_unknown_tier_raises(self):
        with pytest.raises(ValueError):
            compute_tier_requests(4, "unknown_tier")

    # ── Tier ordering: quick < formal ≤ extended for same C ─────────────────

    def test_formal_ge_quick(self):
        for c in [1, 4, 8, 16, 32]:
            assert compute_tier_requests(c, "formal") >= compute_tier_requests(c, "quick"), \
                f"C={c}: formal({compute_tier_requests(c,'formal')}) < quick({compute_tier_requests(c,'quick')})"

    def test_extended_ge_formal(self):
        for c in [1, 4, 8, 16, 32]:
            assert compute_tier_requests(c, "extended") >= compute_tier_requests(c, "formal"), \
                f"C={c}: extended({compute_tier_requests(c,'extended')}) < formal({compute_tier_requests(c,'formal')})"


class TestSweepScaleDefsIntegrity:
    """Validate that SWEEP_SCALE_DEFS in llm_benchmark.py has correct structure.

    Imported from the main module (not customer_metrics) since scale defs live there.
    """

    def test_import_scale_defs(self):
        """SWEEP_SCALE_DEFS must be importable from llm_benchmark module."""
        import importlib
        import sys
        # Import with dummy tkinter guard to avoid display requirement
        try:
            import tkinter  # noqa: F401 — check if tkinter available
            # If tkinter present, just verify constants exist
            # (full Tk init not needed for constants)
        except ImportError:
            pytest.skip("tkinter not available on this system")

        # We can't init the Tk app, but we can parse the source for the constant
        import ast, os
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "llm_benchmark.py")
        with open(src_path) as f:
            source = f.read()
        assert "SWEEP_SCALE_DEFS" in source, "SWEEP_SCALE_DEFS not found in llm_benchmark.py"
        assert "SWEEP_TIER_DEFS"  in source, "SWEEP_TIER_DEFS not found in llm_benchmark.py"

    def test_scale_names_present(self):
        """All expected scale names are present in source."""
        import os
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "llm_benchmark.py")
        with open(src_path) as f:
            source = f.read()
        for scale in ("small", "medium", "large", "extreme", "custom"):
            assert f'"{scale}"' in source or f"'{scale}'" in source, \
                f"Scale '{scale}' not found in llm_benchmark.py"

    def test_tier_names_present(self):
        """All expected tier names are present in source."""
        import os
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "llm_benchmark.py")
        with open(src_path) as f:
            source = f.read()
        for tier in ("quick", "formal", "extended", "custom"):
            assert f'"{tier}"' in source or f"'{tier}'" in source, \
                f"Tier '{tier}' not found in llm_benchmark.py"

    def test_medium_formal_concurrency_list(self):
        """Medium+Formal concurrency list is [1,2,4,8,16,24,32] per spec."""
        import os, ast
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "llm_benchmark.py")
        with open(src_path) as f:
            source = f.read()
        # Extract SWEEP_SCALE_DEFS via regex to avoid Tk import
        import re
        # Find the "medium" block and check it contains 24 and 32 for formal
        medium_block = re.search(r'"medium".*?"formal".*?\[([^\]]+)\]', source, re.DOTALL)
        assert medium_block is not None, "Could not find medium+formal concurrency list"
        conc_str = medium_block.group(1)
        conc_list = [int(x.strip()) for x in conc_str.split(",") if x.strip().isdigit()]
        assert conc_list == [1, 2, 4, 8, 16, 24, 32], \
            f"medium+formal list expected [1,2,4,8,16,24,32], got {conc_list}"

    def test_large_formal_concurrency_list(self):
        """Large+Formal concurrency list is [1,2,4,8,16,24,32,48,64] per spec."""
        import os, re
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "llm_benchmark.py")
        with open(src_path) as f:
            source = f.read()
        # find "large" ... "formal" ... [...]
        large_block = re.search(r'"large".*?"formal".*?\[([^\]]+)\]', source, re.DOTALL)
        assert large_block is not None, "Could not find large+formal concurrency list"
        conc_str = large_block.group(1)
        conc_list = [int(x.strip()) for x in conc_str.split(",") if x.strip().isdigit()]
        assert conc_list == [1, 2, 4, 8, 16, 24, 32, 48, 64], \
            f"large+formal list expected [1,2,4,8,16,24,32,48,64], got {conc_list}"

    def test_extreme_extended_contains_256(self):
        """Extreme+Extended list must include 256 per spec."""
        import os, re
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "llm_benchmark.py")
        with open(src_path) as f:
            source = f.read()
        extreme_block = re.search(r'"extreme".*?"extended".*?\[([^\]]+)\]', source, re.DOTALL)
        assert extreme_block is not None, "Could not find extreme+extended concurrency list"
        conc_str = extreme_block.group(1)
        assert "256" in conc_str, "extreme+extended list must include 256"


class TestSweepTierFormulaSymmetry:
    """compute_tier_requests() is consistent with SWEEP_TIER_DEFS formula strings."""

    @pytest.mark.parametrize("c", [1, 2, 4, 8, 16, 32, 64])
    def test_quick_formula(self, c):
        """quick formula matches max(2*C, 8)."""
        assert compute_tier_requests(c, "quick") == max(2 * c, 8)

    @pytest.mark.parametrize("c", [1, 2, 4, 8, 16, 32, 64])
    def test_formal_formula(self, c):
        """formal formula matches max(5*C, 20)."""
        assert compute_tier_requests(c, "formal") == max(5 * c, 20)

    @pytest.mark.parametrize("c", [1, 2, 4, 8, 16, 32, 64, 96, 128])
    def test_extended_formula(self, c):
        """extended formula matches max(5*C, 50)."""
        assert compute_tier_requests(c, "extended") == max(5 * c, 50)

    def test_compute_peak_sweep_requests_equals_formal(self):
        """compute_peak_sweep_requests() == compute_tier_requests(c, 'formal')."""
        for c in [1, 4, 8, 16, 32, 64]:
            assert compute_peak_sweep_requests(c) == compute_tier_requests(c, "formal"), \
                f"C={c}: compute_peak_sweep_requests({c})={compute_peak_sweep_requests(c)}, " \
                f"compute_tier_requests({c},'formal')={compute_tier_requests(c, 'formal')}"
