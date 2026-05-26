"""Tests for llm_benchmark_app/result_store.py.
TASK-BENCHMARK-RESULTS-HISTORY-REPORT-V2-001
"""
import json
import os
import tempfile
import pytest

from llm_benchmark_app.result_store import (
    _slug,
    rs_make_run_id,
    rs_make_sweep_run_id,
    rs_make_run_dir,
    rs_build_summary,
    rs_build_sweep_summary,
    rs_save_e2e_histogram_json,
    rs_save_single_run,
    rs_save_sweep_run,
    rs_list_runs,
    rs_load_report,
    rs_load_result,
    rs_load_summary,
    rs_get_e2e_histogram_png,
    rs_compare_runs,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_root(tmp_path):
    return str(tmp_path)


_SINGLE_SUMMARY = {
    "model": "qwen3.6-27b-fp8",
    "api_url": "http://localhost:8000/v1/chat/completions",
    "benchmark_mode": "real_api_experience",
    "benchmark_objective": "single_session_decode_speed",
    "concurrency": 1,
    "total": 10,
    "success": 10,
    "fail": 0,
    "success_rate": 100.0,
    "total_input_tokens": 310,
    "total_output_tokens": 1650,
    "total_tokens": 1960,
    "e2e_latency_avg": 3.15,
    "e2e_latency_p50": 3.10,
    "e2e_latency_p95": 3.48,
    "e2e_latency_p99": 3.52,
    "ttft_avg": 0.82,
    "tpot_avg": 0.0191,
    "tpot_p50": 0.019,
    "tpot_p95": 0.020,
    "tpot_p99": 0.021,
    "itl_avg": 0.019,
    "itl_p50": 0.019,
    "itl_p95": 0.020,
    "itl_p99": 0.021,
    "system_output_tps": 52.3,
    "system_total_tps": 55.8,
    "request_throughput_rps": 0.32,
    "per_request_output_tps_avg": 52.3,
    "per_request_output_tps_p50": 52.1,
    "per_request_output_tps_p95": 54.2,
    "stream_mode": True,
    "detail": [
        {"ok": True, "e2e_latency": 3.1, "latency": 3.1},
        {"ok": True, "e2e_latency": 3.2, "latency": 3.2},
        {"ok": False, "error": "timeout", "error_type": "timeout",
         "e2e_latency": 0, "latency": 0},
    ],
}

_SWEEP_RESULT = {
    "model": "qwen3.6-27b-fp8",
    "api_url": "http://localhost:8000/v1/chat/completions",
    "benchmark_mode": "real_api_experience",
    "benchmark_objective": "peak_throughput_sweep",
    "concurrency_levels": [1, 2, 4, 8, 16],
    "finished_at": "2026-05-26 10:00:00",
    "cases": [
        {"concurrency": 1, "total_requests": 10,
         "benchmark_summary": {"success": 10, "fail": 0, "system_output_tps": 52.0}},
        {"concurrency": 4, "total_requests": 20,
         "benchmark_summary": {"success": 20, "fail": 0, "system_output_tps": 180.0}},
        {"concurrency": 16, "total_requests": 80,
         "benchmark_summary": {"success": 80, "fail": 0, "system_output_tps": 420.0}},
    ],
}


# ── _slug ──────────────────────────────────────────────────────────────────────

class TestSlug:
    def test_basic(self):
        assert _slug("hello world") == "hello-world"

    def test_special_chars(self):
        assert _slug("foo/bar:baz") == "foo-bar-baz"

    def test_max_len(self):
        assert len(_slug("a" * 100, maxlen=20)) == 20

    def test_empty(self):
        assert _slug("") == "unknown"

    def test_chinese_passthrough(self):
        s = _slug("qwen3.6-27b-fp8")
        assert "qwen3" in s


# ── rs_make_run_id ─────────────────────────────────────────────────────────────

class TestMakeRunId:
    def test_format_contains_model(self):
        rid = rs_make_run_id(_SINGLE_SUMMARY)
        assert "qwen" in rid

    def test_format_contains_workload(self):
        rid = rs_make_run_id(_SINGLE_SUMMARY)
        assert "C1" in rid

    def test_max_len(self):
        s = dict(_SINGLE_SUMMARY, model="x" * 200)
        rid = rs_make_run_id(s)
        assert len(rid) <= 200

    def test_determinism_same_second(self):
        # Two calls in same test will likely have same timestamp prefix
        r1 = rs_make_run_id(_SINGLE_SUMMARY)
        assert isinstance(r1, str) and len(r1) > 10

    def test_sweep_run_id(self):
        rid = rs_make_sweep_run_id(_SWEEP_RESULT)
        assert "sweep" in rid.lower() or "qwen" in rid


# ── rs_make_run_dir ────────────────────────────────────────────────────────────

class TestMakeRunDir:
    def test_creates_directory(self, tmp_root):
        run_id = "20260526_120000__test"
        path = rs_make_run_dir(tmp_root, run_id)
        assert os.path.isdir(path)

    def test_creates_charts_subdir(self, tmp_root):
        path = rs_make_run_dir(tmp_root, "test_run_id")
        assert os.path.isdir(os.path.join(path, "charts"))

    def test_path_contains_run_id(self, tmp_root):
        run_id = "my_run_id_123"
        path = rs_make_run_dir(tmp_root, run_id)
        assert run_id in path


# ── rs_build_summary ───────────────────────────────────────────────────────────

class TestBuildSummary:
    def test_required_fields(self, tmp_root):
        run_id  = "test_run"
        run_dir = rs_make_run_dir(tmp_root, run_id)
        s = rs_build_summary(run_id, run_dir, _SINGLE_SUMMARY)
        for field in ["run_id", "created_at", "model", "api_url",
                      "benchmark_mode", "benchmark_objective",
                      "workload", "concurrency", "total_requests",
                      "successful_requests", "failed_requests", "success_rate",
                      "mean_e2e_latency_ms", "p95_e2e_latency_ms",
                      "mean_ttft_ms", "mean_tpot_ms",
                      "output_token_throughput_tok_s",
                      "report_txt_path", "report_md_path",
                      "e2e_histogram_path", "result_json_path", "run_dir"]:
            assert field in s, f"Missing field: {field}"

    def test_decode_tps_calculated(self, tmp_root):
        run_id  = "test_decode"
        run_dir = rs_make_run_dir(tmp_root, run_id)
        s = rs_build_summary(run_id, run_dir, _SINGLE_SUMMARY)
        assert s["single_session_decode_tok_s"] is not None
        # 1000 / (0.0191 * 1000) ≈ 52.4
        assert abs(s["single_session_decode_tok_s"] - 52.4) < 2.0

    def test_decode_tps_none_when_no_tpot(self, tmp_root):
        run_id  = "test_no_tpot"
        run_dir = rs_make_run_dir(tmp_root, run_id)
        s2 = dict(_SINGLE_SUMMARY, tpot_avg=0)
        s = rs_build_summary(run_id, run_dir, s2)
        assert s["single_session_decode_tok_s"] is None

    def test_workload_format(self, tmp_root):
        run_id  = "test_wl"
        run_dir = rs_make_run_dir(tmp_root, run_id)
        s = rs_build_summary(run_id, run_dir, _SINGLE_SUMMARY)
        assert s["workload"].startswith("C1/N10/")

    def test_latency_in_ms(self, tmp_root):
        run_id  = "test_ms"
        run_dir = rs_make_run_dir(tmp_root, run_id)
        s = rs_build_summary(run_id, run_dir, _SINGLE_SUMMARY)
        # e2e_latency_avg=3.15s → 3150ms
        assert abs(s["mean_e2e_latency_ms"] - 3150) < 5

    def test_mode_label_zh(self, tmp_root):
        run_id  = "test_mode"
        run_dir = rs_make_run_dir(tmp_root, run_id)
        s = rs_build_summary(run_id, run_dir, _SINGLE_SUMMARY)
        assert s["benchmark_mode_label_zh"] == "真实 API 体验测试"

    def test_engine_core_label_zh(self, tmp_root):
        run_id  = "test_ec"
        run_dir = rs_make_run_dir(tmp_root, run_id)
        s2 = dict(_SINGLE_SUMMARY, benchmark_mode="engine_core")
        s = rs_build_summary(run_id, run_dir, s2)
        assert s["benchmark_mode_label_zh"] == "引擎核心性能测试"


# ── rs_build_sweep_summary ─────────────────────────────────────────────────────

class TestBuildSweepSummary:
    def test_peak_tps(self, tmp_root):
        run_id  = "test_sweep"
        run_dir = rs_make_run_dir(tmp_root, run_id)
        s = rs_build_sweep_summary(run_id, run_dir, _SWEEP_RESULT)
        assert s["peak_output_token_throughput_tok_s"] == 420.0
        assert s["max_throughput_concurrency"] == 16

    def test_run_type(self, tmp_root):
        run_id  = "test_sweep_type"
        run_dir = rs_make_run_dir(tmp_root, run_id)
        s = rs_build_sweep_summary(run_id, run_dir, _SWEEP_RESULT)
        assert s["run_type"] == "sweep"


# ── rs_save_e2e_histogram_json ─────────────────────────────────────────────────

class TestSaveE2EHistogramJson:
    def test_creates_file(self, tmp_root):
        run_id  = "hist_test"
        run_dir = rs_make_run_dir(tmp_root, run_id)
        lats = [1.0, 1.5, 2.0, 2.5]
        path = rs_save_e2e_histogram_json(run_dir, lats, _SINGLE_SUMMARY)
        assert os.path.isfile(path)

    def test_content(self, tmp_root):
        run_id  = "hist_content"
        run_dir = rs_make_run_dir(tmp_root, run_id)
        lats = [1.0, 2.0, 3.0]
        path = rs_save_e2e_histogram_json(run_dir, lats, _SINGLE_SUMMARY)
        with open(path) as f:
            data = json.load(f)
        assert data["sample_count"] == 3
        assert data["min"] == 1.0
        assert data["max"] == 3.0


# ── rs_save_single_run ────────────────────────────────────────────────────────

class TestSaveSingleRun:
    def test_returns_run_dir(self, tmp_root):
        out = rs_save_single_run(tmp_root, _SINGLE_SUMMARY, "report text", "",
                                  [1.0, 1.5, 2.0], {})
        assert os.path.isdir(out["run_dir"])

    def test_result_json_exists(self, tmp_root):
        out = rs_save_single_run(tmp_root, _SINGLE_SUMMARY, "report text", "",
                                  [1.0], {})
        assert os.path.isfile(out["result_path"])

    def test_summary_json_exists(self, tmp_root):
        out = rs_save_single_run(tmp_root, _SINGLE_SUMMARY, "report text", "",
                                  [1.0], {})
        assert os.path.isfile(out["summary_path"])

    def test_report_txt_exists(self, tmp_root):
        out = rs_save_single_run(tmp_root, _SINGLE_SUMMARY, "my report", "",
                                  [1.0], {})
        assert os.path.isfile(out["report_txt_path"])
        with open(out["report_txt_path"]) as f:
            assert "my report" in f.read()

    def test_histogram_json_created(self, tmp_root):
        out = rs_save_single_run(tmp_root, _SINGLE_SUMMARY, "r", "",
                                  [1.0, 2.0], {})
        assert os.path.isfile(out["e2e_histogram_json"])

    def test_artifacts_manifest_created(self, tmp_root):
        out = rs_save_single_run(tmp_root, _SINGLE_SUMMARY, "r", "",
                                  [1.0], {})
        manifest = os.path.join(out["run_dir"], "artifacts_manifest.json")
        assert os.path.isfile(manifest)

    def test_run_id_in_results_runs(self, tmp_root):
        out = rs_save_single_run(tmp_root, _SINGLE_SUMMARY, "r", "",
                                  [1.0], {})
        runs_dir = os.path.join(tmp_root, "runs")
        assert os.path.isdir(runs_dir)
        subdirs = os.listdir(runs_dir)
        assert len(subdirs) == 1
        assert out["run_id"] in subdirs[0] or subdirs[0] == out["run_id"]


# ── rs_save_sweep_run ─────────────────────────────────────────────────────────

class TestSaveSweepRun:
    def test_result_json_exists(self, tmp_root):
        out = rs_save_sweep_run(tmp_root, _SWEEP_RESULT, "sweep report md", {})
        assert os.path.isfile(out["result_path"])

    def test_summary_json_exists(self, tmp_root):
        out = rs_save_sweep_run(tmp_root, _SWEEP_RESULT, "sweep report md", {})
        assert os.path.isfile(out["summary_path"])


# ── rs_list_runs ──────────────────────────────────────────────────────────────

class TestListRuns:
    def test_empty_on_missing_dir(self, tmp_root):
        missing = os.path.join(tmp_root, "does_not_exist")
        assert rs_list_runs(missing) == []

    def test_finds_saved_run(self, tmp_root):
        rs_save_single_run(tmp_root, _SINGLE_SUMMARY, "r", "", [1.0], {})
        runs = rs_list_runs(tmp_root)
        assert len(runs) == 1

    def test_sorted_newest_first(self, tmp_root):
        import time
        rs_save_single_run(tmp_root, _SINGLE_SUMMARY, "r1", "", [1.0], {})
        time.sleep(0.01)
        s2 = dict(_SINGLE_SUMMARY, model="model-b")
        rs_save_single_run(tmp_root, s2, "r2", "", [2.0], {})
        runs = rs_list_runs(tmp_root)
        assert len(runs) == 2
        # Second saved run should appear first (newest)
        assert runs[0]["model"] == "model-b"

    def test_lazy_summary_generation(self, tmp_root):
        """If summary.json missing but result.json exists, generates lazily."""
        run_dir = rs_make_run_dir(tmp_root, "lazy_run_test")
        import json
        with open(os.path.join(run_dir, "result.json"), "w") as f:
            json.dump(_SINGLE_SUMMARY, f)
        # No summary.json at this point
        runs = rs_list_runs(tmp_root)
        assert len(runs) == 1
        # And summary.json should now be created
        assert os.path.isfile(os.path.join(run_dir, "summary.json"))

    def test_finds_multiple_runs(self, tmp_root):
        for i in range(3):
            s = dict(_SINGLE_SUMMARY, model=f"model-{i}")
            rs_save_single_run(tmp_root, s, "r", "", [1.0], {})
        runs = rs_list_runs(tmp_root)
        assert len(runs) == 3


# ── rs_load_report ────────────────────────────────────────────────────────────

class TestLoadReport:
    def test_loads_report_txt(self, tmp_root):
        out = rs_save_single_run(tmp_root, _SINGLE_SUMMARY, "hello report", "",
                                  [1.0], {})
        text = rs_load_report(out["run_dir"])
        assert "hello report" in text

    def test_returns_none_on_missing(self, tmp_root):
        run_dir = rs_make_run_dir(tmp_root, "empty_run")
        result = rs_load_report(run_dir)
        assert result is None


# ── rs_load_result ────────────────────────────────────────────────────────────

class TestLoadResult:
    def test_loads_result_json(self, tmp_root):
        out = rs_save_single_run(tmp_root, _SINGLE_SUMMARY, "r", "", [1.0], {})
        data = rs_load_result(out["run_dir"])
        assert data is not None
        assert data["model"] == "qwen3.6-27b-fp8"

    def test_returns_none_on_missing(self, tmp_root):
        run_dir = rs_make_run_dir(tmp_root, "no_result")
        assert rs_load_result(run_dir) is None


# ── rs_get_e2e_histogram_png ──────────────────────────────────────────────────

class TestGetE2EHistogramPng:
    def test_returns_none_when_no_png(self, tmp_root):
        run_dir = rs_make_run_dir(tmp_root, "no_png")
        assert rs_get_e2e_histogram_png(run_dir) is None

    def test_returns_path_when_exists(self, tmp_root):
        run_dir = rs_make_run_dir(tmp_root, "with_png")
        png_path = os.path.join(run_dir, "charts", "e2e_latency_histogram.png")
        open(png_path, "w").close()  # create empty file
        result = rs_get_e2e_histogram_png(run_dir)
        assert result == png_path


# ── rs_compare_runs ───────────────────────────────────────────────────────────

class TestCompareRuns:
    def _make_summary(self, **kwargs):
        base = {
            "mean_tpot_ms": 20.0,
            "single_session_decode_tok_s": 50.0,
            "mean_ttft_ms": 800.0,
            "mean_e2e_latency_ms": 3000.0,
            "p95_e2e_latency_ms": 3500.0,
            "p95_tpot_ms": 22.0,
            "output_token_throughput_tok_s": 52.0,
            "total_token_throughput_tok_s": 55.0,
            "success_rate": 100.0,
        }
        base.update(kwargs)
        return base

    def test_returns_empty_on_single(self):
        assert rs_compare_runs([self._make_summary()]) == []

    def test_delta_calculated(self):
        b = self._make_summary(mean_tpot_ms=20.0)
        c = self._make_summary(mean_tpot_ms=18.0)  # improvement
        rows = rs_compare_runs([b, c])
        tpot_row = next(r for r in rows if r["key"] == "mean_tpot_ms")
        assert tpot_row["delta"] == pytest.approx(-2.0)

    def test_direction_better_for_lower_tpot(self):
        b = self._make_summary(mean_tpot_ms=20.0)
        c = self._make_summary(mean_tpot_ms=18.0)  # lower is better
        rows = rs_compare_runs([b, c])
        tpot_row = next(r for r in rows if r["key"] == "mean_tpot_ms")
        assert tpot_row["direction"] == "better"

    def test_direction_worse_for_lower_tps(self):
        b = self._make_summary(output_token_throughput_tok_s=52.0)
        c = self._make_summary(output_token_throughput_tok_s=45.0)  # lower is worse
        rows = rs_compare_runs([b, c])
        tps_row = next(r for r in rows if r["key"] == "output_token_throughput_tok_s")
        assert tps_row["direction"] == "worse"

    def test_pct_change(self):
        b = self._make_summary(mean_tpot_ms=20.0)
        c = self._make_summary(mean_tpot_ms=22.0)
        rows = rs_compare_runs([b, c])
        tpot_row = next(r for r in rows if r["key"] == "mean_tpot_ms")
        assert tpot_row["pct_change"] == pytest.approx(10.0)

    def test_neutral_on_no_change(self):
        b = self._make_summary(mean_tpot_ms=20.0)
        c = self._make_summary(mean_tpot_ms=20.0)
        rows = rs_compare_runs([b, c])
        tpot_row = next(r for r in rows if r["key"] == "mean_tpot_ms")
        assert tpot_row["direction"] == "neutral"

    def test_missing_key_skipped(self):
        b = {"mean_tpot_ms": 20.0}
        c = {"mean_tpot_ms": 18.0}
        rows = rs_compare_runs([b, c])
        assert any(r["key"] == "mean_tpot_ms" for r in rows)
        # No crash for missing keys

    def test_all_required_metrics_present(self):
        b = self._make_summary()
        c = self._make_summary(mean_tpot_ms=18.0)
        rows = rs_compare_runs([b, c])
        keys = {r["key"] for r in rows}
        for k in ["mean_tpot_ms", "mean_ttft_ms", "mean_e2e_latency_ms",
                  "output_token_throughput_tok_s", "success_rate"]:
            assert k in keys
