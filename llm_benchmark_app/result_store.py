"""result_store.py — ResultStore: filesystem-based run storage.

TASK-BENCHMARK-RESULTS-HISTORY-REPORT-V2-001

Provides a durable, filesystem-first result storage layer.
Every completed benchmark run (single or sweep) is written to:

  results/runs/<run_id>/
    result.json
    summary.json
    report.txt
    report.md
    config.json
    charts/
      e2e_latency_histogram.png   (if matplotlib available)
      e2e_latency_histogram.json
    artifacts_manifest.json

run_id format:
  YYYYMMDD_HHMMSS__<model_slug>__<mode_slug>__<obj_slug>__<workload>

History scanning: rs_list_runs() scans results/runs/*/summary.json.
If summary.json is missing but result.json exists, it is lazily generated.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime

# ── Path resolution ──────────────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_ROOT: str = os.path.join(_SCRIPT_DIR, "results")

# Report format version tag
REPORT_VERSION = "v2"


# ── Slug helpers ─────────────────────────────────────────────────────────────

def _slug(s: str, maxlen: int = 32) -> str:
    """Convert a string to a filesystem-safe slug."""
    import re
    if not s:
        return "unknown"
    s = str(s).lower().strip()
    s = re.sub(r'[/\\:*?"<>|]', '-', s)
    s = re.sub(r'\s+', '-', s)
    s = re.sub(r'-+', '-', s)
    s = s.strip('-')
    return s[:maxlen] if s else "unknown"


# ── Run ID ───────────────────────────────────────────────────────────────────

def rs_make_run_id(summary: dict, run_type: str = "single") -> str:
    """Create a stable, readable, filesystem-safe run_id.

    Format:
      YYYYMMDD_HHMMSS__<model>__<mode>__<obj>__C<c>_N<n>_I<i>_O<o>
    """
    ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
    model = _slug(summary.get("model", "unknown"), 20)
    mode  = _slug(summary.get("benchmark_mode", "real_api"), 14)
    obj   = _slug(summary.get("benchmark_objective", "single"), 14)
    total = max(summary.get("total", 1), 1)
    conc  = summary.get("concurrency", 0)
    n     = summary.get("total", 0)
    i_avg = int((summary.get("total_input_tokens",  0) or 0) / total)
    o_avg = int((summary.get("total_output_tokens", 0) or 0) / total)
    wl    = f"C{conc}_N{n}_I{i_avg}_O{o_avg}"
    rid   = f"{ts}__{model}__{mode}__{obj}__{wl}"
    return rid[:200] if len(rid) > 200 else rid


def rs_make_sweep_run_id(sweep_result: dict) -> str:
    """Create a run_id for a sweep run."""
    ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
    model = _slug(sweep_result.get("model", "unknown"), 20)
    mode  = _slug(sweep_result.get("benchmark_mode", "real_api"), 14)
    obj   = _slug(sweep_result.get("benchmark_objective", "sweep"), 14)
    levels = sweep_result.get("concurrency_levels", [])
    wl    = f"sweep_C{min(levels) if levels else 0}-C{max(levels) if levels else 0}"
    rid   = f"{ts}__{model}__{mode}__{obj}__{wl}"
    return rid[:200] if len(rid) > 200 else rid


# ── Directory creation ────────────────────────────────────────────────────────

def rs_make_run_dir(results_root: str, run_id: str) -> str:
    """Create and return results/runs/<run_id>/."""
    path = os.path.join(results_root, "runs", run_id)
    os.makedirs(path, exist_ok=True)
    os.makedirs(os.path.join(path, "charts"), exist_ok=True)
    return path


# ── Individual file savers ────────────────────────────────────────────────────

def rs_save_json(path: str, data: dict) -> str:
    """Write a dict to a JSON file. Returns path."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    return path


def rs_save_text(path: str, text: str) -> str:
    """Write a text file. Returns path."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


# ── Summary builder ───────────────────────────────────────────────────────────

def rs_build_summary(run_id: str, run_dir: str, summary: dict,
                     env_info: dict | None = None, run_type: str = "single") -> dict:
    """Build a summary.json-compatible dict from a single-run result dict."""
    if env_info is None:
        env_info = {}
    total = max(summary.get("total", 1), 1)
    success = summary.get("success", 0)
    fail    = summary.get("fail", 0)
    conc    = summary.get("concurrency", 0)
    n       = summary.get("total", 0)
    i_avg   = int((summary.get("total_input_tokens",  0) or 0) / total)
    o_avg   = int((summary.get("total_output_tokens", 0) or 0) / total)
    snaps   = env_info.get("snapshots", {})

    tpot_avg = summary.get("tpot_avg", 0) or 0
    single_decode = round(1000.0 / (tpot_avg * 1000), 2) if tpot_avg > 0 else None

    return {
        "run_id": run_id,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "run_type": run_type,
        "model": summary.get("model", ""),
        "api_url": summary.get("api_url", ""),
        "benchmark_mode": summary.get("benchmark_mode", "real_api_experience"),
        "benchmark_mode_label_zh": (
            "真实 API 体验测试"
            if summary.get("benchmark_mode") != "engine_core"
            else "引擎核心性能测试"
        ),
        "benchmark_objective": summary.get("benchmark_objective", "single_session_decode_speed"),
        "benchmark_objective_label_zh": (
            "单会话最大生成速度"
            if summary.get("benchmark_objective") != "peak_throughput_sweep"
            else "峰值吞吐扫描"
        ),
        "workload": f"C{conc}/N{n}/I{i_avg}/O{o_avg}",
        "concurrency": conc,
        "total_requests": n,
        "successful_requests": success,
        "failed_requests": fail,
        "success_rate": round(success / total * 100, 2),
        "input_tokens_total": summary.get("total_input_tokens", 0),
        "output_tokens_total": summary.get("total_output_tokens", 0),
        "total_tokens": summary.get("total_tokens", 0),
        "mean_e2e_latency_ms": round((summary.get("e2e_latency_avg", 0) or 0) * 1000, 2),
        "p50_e2e_latency_ms":  round((summary.get("e2e_latency_p50", 0) or 0) * 1000, 2),
        "p95_e2e_latency_ms":  round((summary.get("e2e_latency_p95", 0) or 0) * 1000, 2),
        "p99_e2e_latency_ms":  round((summary.get("e2e_latency_p99", 0) or 0) * 1000, 2),
        "mean_ttft_ms":  round((summary.get("ttft_avg", 0) or 0) * 1000, 2),
        "mean_tpot_ms":  round((summary.get("tpot_avg", 0) or 0) * 1000, 2),
        "p50_tpot_ms":   round((summary.get("tpot_p50", 0) or 0) * 1000, 2),
        "p95_tpot_ms":   round((summary.get("tpot_p95", 0) or 0) * 1000, 2),
        "p99_tpot_ms":   round((summary.get("tpot_p99", 0) or 0) * 1000, 2),
        "single_session_decode_tok_s": single_decode,
        "e2e_output_tok_s_per_request_avg": summary.get("per_request_output_tps_avg"),
        "output_token_throughput_tok_s": round(summary.get("system_output_tps", 0) or 0, 2),
        "total_token_throughput_tok_s":  round(summary.get("system_total_tps", 0) or 0, 2),
        "report_txt_path": os.path.join(run_dir, "report.txt"),
        "report_md_path":  os.path.join(run_dir, "report.md"),
        "e2e_histogram_path": os.path.join(run_dir, "charts", "e2e_latency_histogram.png"),
        "result_json_path": os.path.join(run_dir, "result.json"),
        "run_dir": run_dir,
        "environment_profile_name": snaps.get("env_name", ""),
        "hardware":      snaps.get("gpu_model", ""),
        "backend":       snaps.get("backend", ""),
        "model_profile": snaps.get("model_name", ""),
        "warnings": summary.get("metric_warnings", []),
    }


def rs_build_sweep_summary(run_id: str, run_dir: str,
                           sweep_result: dict, env_info: dict | None = None) -> dict:
    """Build a summary.json-compatible dict from a sweep result."""
    if env_info is None:
        env_info = {}
    snaps  = env_info.get("snapshots", {})
    cases  = sweep_result.get("cases", [])
    levels = sweep_result.get("concurrency_levels", [])
    total_success = sum(c.get("benchmark_summary", {}).get("success", 0) for c in cases)
    total_fail    = sum(c.get("benchmark_summary", {}).get("fail", 0) for c in cases)
    total_req     = sum(c.get("total_requests", 0) for c in cases)
    total_all     = max(total_req, 1)

    peak_tps = 0.0
    max_conc  = 0
    for c in cases:
        s = c.get("benchmark_summary", {})
        ot = s.get("system_output_tps", 0) or 0
        if ot > peak_tps:
            peak_tps = ot
            max_conc = c.get("concurrency", 0)

    return {
        "run_id": run_id,
        "created_at": sweep_result.get("finished_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        "run_type": "sweep",
        "model": sweep_result.get("model", ""),
        "api_url": sweep_result.get("api_url", ""),
        "benchmark_mode": sweep_result.get("benchmark_mode", "real_api_experience"),
        "benchmark_mode_label_zh": (
            "真实 API 体验测试"
            if sweep_result.get("benchmark_mode") != "engine_core"
            else "引擎核心性能测试"
        ),
        "benchmark_objective": sweep_result.get("benchmark_objective", "peak_throughput_sweep"),
        "benchmark_objective_label_zh": "峰值吞吐扫描",
        "workload": f"sweep / {len(levels)} levels: {levels}",
        "concurrency": len(levels),
        "total_requests": total_req,
        "successful_requests": total_success,
        "failed_requests": total_fail,
        "success_rate": round(total_success / total_all * 100, 2),
        "input_tokens_total": 0,
        "output_tokens_total": 0,
        "total_tokens": 0,
        "peak_output_token_throughput_tok_s": round(peak_tps, 2),
        "max_throughput_concurrency": max_conc,
        "sweep_scale": sweep_result.get("sweep_scale"),
        "sweep_tier":  sweep_result.get("sweep_tier"),
        "concurrency_list": levels,
        "report_txt_path": os.path.join(run_dir, "report.txt"),
        "report_md_path":  os.path.join(run_dir, "report.md"),
        "result_json_path": os.path.join(run_dir, "result.json"),
        "run_dir": run_dir,
        "environment_profile_name": snaps.get("env_name", ""),
        "hardware":      snaps.get("gpu_model", ""),
        "backend":       snaps.get("backend", ""),
        "model_profile": snaps.get("model_name", ""),
        "warnings": [],
    }


# ── E2E histogram PNG (matplotlib) ───────────────────────────────────────────

def rs_save_e2e_histogram_png(run_dir: str, latencies: list[float],
                               summary: dict) -> str | None:
    """Save E2E latency histogram as PNG using matplotlib.
    Returns path or None if matplotlib unavailable or no data."""
    if not latencies:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np

        fig, ax = plt.subplots(figsize=(8, 4))
        ax.hist(latencies, bins=min(30, max(5, len(latencies) // 2)), color="#5339FD",
                edgecolor="white", linewidth=0.5, alpha=0.85)

        p50 = summary.get("e2e_latency_p50", 0)
        p95 = summary.get("e2e_latency_p95", 0)
        p99 = summary.get("e2e_latency_p99", 0)
        if p50:
            ax.axvline(p50, color="#F59E0B", linestyle="--", linewidth=1.5,
                       label=f"P50={p50:.3f}s")
        if p95:
            ax.axvline(p95, color="#EF4444", linestyle="--", linewidth=1.5,
                       label=f"P95={p95:.3f}s")
        if p99:
            ax.axvline(p99, color="#7C3AED", linestyle=":", linewidth=1.2,
                       label=f"P99={p99:.3f}s")

        ax.set_xlabel("E2E Latency (s)")
        ax.set_ylabel("Count")
        ax.set_title("E2E Latency Distribution")
        if p50 or p95:
            ax.legend(fontsize=8)
        fig.tight_layout()

        path = os.path.join(run_dir, "charts", "e2e_latency_histogram.png")
        fig.savefig(path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        return path
    except Exception as e:
        logging.debug("E2E histogram PNG save failed: %s", e)
        return None


def rs_save_e2e_histogram_json(run_dir: str, latencies: list[float],
                                summary: dict) -> str:
    """Save E2E latency histogram metadata as JSON."""
    path = os.path.join(run_dir, "charts", "e2e_latency_histogram.json")
    data = {
        "metric_name": "e2e_latency",
        "unit": "seconds",
        "sample_count": len(latencies),
        "min": round(min(latencies), 6) if latencies else None,
        "max": round(max(latencies), 6) if latencies else None,
        "p50": summary.get("e2e_latency_p50"),
        "p95": summary.get("e2e_latency_p95"),
        "p99": summary.get("e2e_latency_p99"),
        "source_result_json": os.path.join(run_dir, "result.json"),
    }
    rs_save_json(path, data)
    return path


# ── Artifacts manifest ────────────────────────────────────────────────────────

def rs_write_artifacts_manifest(run_dir: str, artifacts: list[dict]) -> str:
    """Write artifacts_manifest.json listing all generated files."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for a in artifacts:
        if "created_at" not in a:
            a["created_at"] = now
        p = a.get("relative_path", "")
        full = os.path.join(run_dir, p) if p else ""
        if full and os.path.isfile(full):
            a["size_bytes"] = os.path.getsize(full)
        else:
            a["size_bytes"] = 0
    path = os.path.join(run_dir, "artifacts_manifest.json")
    rs_save_json(path, artifacts)
    return path


# ── High-level savers ─────────────────────────────────────────────────────────

def rs_save_single_run(results_root: str,
                        summary: dict,
                        report_txt: str,
                        report_md: str,
                        e2e_latencies: list[float],
                        env_info: dict | None = None,
                        config: dict | None = None) -> dict:
    """Save a complete single-run result package.

    Returns a dict with keys:
      run_id, run_dir, summary_path, result_path,
      report_txt_path, report_md_path,
      e2e_histogram_png, e2e_histogram_json
    """
    if env_info is None:
        env_info = {}
    if config is None:
        config = {}

    run_id  = rs_make_run_id(summary, run_type="single")
    run_dir = rs_make_run_dir(results_root, run_id)

    artifacts = []

    # result.json — full summary dict
    result_path = os.path.join(run_dir, "result.json")
    rs_save_json(result_path, summary)
    artifacts.append({"name": "result.json", "relative_path": "result.json",
                       "file_type": "json"})

    # summary.json — lightweight index
    summary_data = rs_build_summary(run_id, run_dir, summary, env_info)
    summary_path = os.path.join(run_dir, "summary.json")
    rs_save_json(summary_path, summary_data)
    artifacts.append({"name": "summary.json", "relative_path": "summary.json",
                       "file_type": "json"})

    # report.txt
    report_txt_path = os.path.join(run_dir, "report.txt")
    rs_save_text(report_txt_path, report_txt)
    artifacts.append({"name": "report.txt", "relative_path": "report.txt",
                       "file_type": "text"})

    # report.md
    report_md_path = os.path.join(run_dir, "report.md")
    rs_save_text(report_md_path, report_md if report_md else report_txt)
    artifacts.append({"name": "report.md", "relative_path": "report.md",
                       "file_type": "markdown"})

    # config.json
    if config:
        config_path = os.path.join(run_dir, "config.json")
        rs_save_json(config_path, config)
        artifacts.append({"name": "config.json", "relative_path": "config.json",
                           "file_type": "json"})

    # E2E histogram
    hist_png  = rs_save_e2e_histogram_png(run_dir, e2e_latencies, summary)
    hist_json = rs_save_e2e_histogram_json(run_dir, e2e_latencies, summary)
    if hist_png:
        artifacts.append({"name": "e2e_latency_histogram.png",
                           "relative_path": "charts/e2e_latency_histogram.png",
                           "file_type": "image"})
    artifacts.append({"name": "e2e_latency_histogram.json",
                       "relative_path": "charts/e2e_latency_histogram.json",
                       "file_type": "json"})

    # Manifest
    rs_write_artifacts_manifest(run_dir, artifacts)

    return {
        "run_id": run_id,
        "run_dir": run_dir,
        "summary_path": summary_path,
        "result_path": result_path,
        "report_txt_path": report_txt_path,
        "report_md_path": report_md_path,
        "e2e_histogram_png": hist_png or "",
        "e2e_histogram_json": hist_json,
    }


def rs_save_sweep_run(results_root: str,
                       sweep_result: dict,
                       report_md: str,
                       env_info: dict | None = None) -> dict:
    """Save a complete sweep run result package.

    Returns a dict with run_id, run_dir, and file paths.
    """
    if env_info is None:
        env_info = {}

    run_id  = rs_make_sweep_run_id(sweep_result)
    run_dir = rs_make_run_dir(results_root, run_id)

    artifacts = []

    # result.json
    result_path = os.path.join(run_dir, "result.json")
    rs_save_json(result_path, sweep_result)
    artifacts.append({"name": "result.json", "relative_path": "result.json",
                       "file_type": "json"})

    # summary.json
    summary_data = rs_build_sweep_summary(run_id, run_dir, sweep_result, env_info)
    summary_path = os.path.join(run_dir, "summary.json")
    rs_save_json(summary_path, summary_data)
    artifacts.append({"name": "summary.json", "relative_path": "summary.json",
                       "file_type": "json"})

    # report.md
    report_md_path = os.path.join(run_dir, "report.md")
    rs_save_text(report_md_path, report_md)
    artifacts.append({"name": "report.md", "relative_path": "report.md",
                       "file_type": "markdown"})

    # report.txt (same as report.md for sweeps)
    report_txt_path = os.path.join(run_dir, "report.txt")
    rs_save_text(report_txt_path, report_md)
    artifacts.append({"name": "report.txt", "relative_path": "report.txt",
                       "file_type": "text"})

    # Manifest
    rs_write_artifacts_manifest(run_dir, artifacts)

    return {
        "run_id": run_id,
        "run_dir": run_dir,
        "summary_path": summary_path,
        "result_path": result_path,
        "report_md_path": report_md_path,
        "report_txt_path": report_txt_path,
    }


# ── History scanning ──────────────────────────────────────────────────────────

def rs_list_runs(results_root: str) -> list[dict]:
    """Scan results/runs/*/summary.json. Returns list of summary dicts, newest first.
    If summary.json is missing but result.json exists, lazily generates it.
    """
    runs_dir = os.path.join(results_root, "runs")
    if not os.path.isdir(runs_dir):
        return []

    results = []
    try:
        entries = list(os.scandir(runs_dir))
    except OSError:
        return []

    for entry in entries:
        if not entry.is_dir():
            continue
        summary_path = os.path.join(entry.path, "summary.json")
        if os.path.isfile(summary_path):
            try:
                with open(summary_path, encoding="utf-8") as f:
                    s = json.load(f)
                s["_run_dir"] = entry.path
                s.setdefault("run_id", entry.name)
                results.append(s)
            except Exception:
                pass
            continue

        # Lazy: generate summary.json from result.json
        result_path = os.path.join(entry.path, "result.json")
        if not os.path.isfile(result_path):
            continue
        try:
            with open(result_path, encoding="utf-8") as f:
                result_data = json.load(f)
            run_id  = entry.name
            run_type = result_data.get("run_type", "single")
            if run_type == "sweep":
                s = rs_build_sweep_summary(run_id, entry.path, result_data)
            else:
                s = rs_build_summary(run_id, entry.path, result_data)
            s["_migrated"] = True
            s["_run_dir"] = entry.path
            # Persist for next time
            try:
                rs_save_json(summary_path, s)
            except Exception:
                pass
            results.append(s)
        except Exception as e:
            logging.debug("rs_list_runs: could not process %s: %s", entry.path, e)

    # Sort newest first
    results.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return results


# ── File loaders ──────────────────────────────────────────────────────────────

def rs_load_report(run_dir: str, preferred: str = "txt") -> str | None:
    """Load report.txt or report.md from run_dir. Returns text or None."""
    candidates = (
        [("report.txt", "report.md")] if preferred == "txt"
        else [("report.md", "report.txt")]
    )[0]
    for fname in candidates:
        p = os.path.join(run_dir, fname)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return f.read()
            except Exception:
                pass
    return None


def rs_load_result(run_dir: str) -> dict | None:
    """Load result.json from run_dir. Returns dict or None."""
    path = os.path.join(run_dir, "result.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def rs_load_summary(run_dir: str) -> dict | None:
    """Load summary.json from run_dir. Returns dict or None."""
    path = os.path.join(run_dir, "summary.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def rs_get_e2e_histogram_png(run_dir: str) -> str | None:
    """Return path to e2e_latency_histogram.png if it exists."""
    p = os.path.join(run_dir, "charts", "e2e_latency_histogram.png")
    return p if os.path.isfile(p) else None


# ── Regeneration ──────────────────────────────────────────────────────────────

def rs_regenerate_report(run_dir: str, report_generator_fn) -> bool:
    """Regenerate report.txt from result.json using a callback.

    report_generator_fn(result_dict) -> str
    Returns True on success.
    """
    result = rs_load_result(run_dir)
    if result is None:
        return False
    try:
        text = report_generator_fn(result)
        rs_save_text(os.path.join(run_dir, "report.txt"), text)
        rs_save_text(os.path.join(run_dir, "report.md"), text)
        return True
    except Exception as e:
        logging.warning("rs_regenerate_report failed: %s", e)
        return False


def rs_regenerate_chart(run_dir: str) -> str | None:
    """Regenerate E2E histogram PNG from result.json.
    Returns path or None if failed."""
    result = rs_load_result(run_dir)
    if result is None:
        return None
    latencies = []
    for r in result.get("detail", []):
        if not r.get("ok"):
            continue
        v = r.get("e2e_latency", r.get("latency"))
        if isinstance(v, (int, float)) and v > 0:
            latencies.append(float(v))
    if not latencies:
        return None
    return rs_save_e2e_histogram_png(run_dir, latencies, result)


# ── Comparison helper ─────────────────────────────────────────────────────────

_COMPARE_METRICS: list[tuple[str, str, str]] = [
    # (key, label_zh, unit)
    ("mean_tpot_ms",                  "TPOT avg",              "ms"),
    ("single_session_decode_tok_s",   "单会话解码速度",         "tok/s"),
    ("mean_ttft_ms",                  "TTFT avg",              "ms"),
    ("mean_e2e_latency_ms",           "E2E avg",               "ms"),
    ("p95_e2e_latency_ms",            "E2E P95",               "ms"),
    ("p95_tpot_ms",                   "TPOT P95",              "ms"),
    ("output_token_throughput_tok_s", "Output TPS",            "tok/s"),
    ("total_token_throughput_tok_s",  "Total TPS",             "tok/s"),
    ("success_rate",                  "成功率",                "%"),
]


def rs_compare_runs(summaries: list[dict]) -> list[dict]:
    """Compare 2+ run summaries. First is baseline.

    Returns a list of metric comparison rows:
      { key, label, unit, baseline, current, delta, pct_change, direction }

    direction: "better" | "worse" | "neutral"
    """
    if len(summaries) < 2:
        return []
    baseline = summaries[0]
    current  = summaries[1]

    # Keys where lower is better
    lower_is_better = {"mean_tpot_ms", "mean_ttft_ms", "mean_e2e_latency_ms",
                        "p95_e2e_latency_ms", "p99_e2e_latency_ms", "p95_tpot_ms",
                        "p99_tpot_ms"}

    rows = []
    for key, label, unit in _COMPARE_METRICS:
        bv = baseline.get(key)
        cv = current.get(key)
        if bv is None and cv is None:
            continue
        bv_f = float(bv) if bv is not None else None
        cv_f = float(cv) if cv is not None else None

        if bv_f is not None and cv_f is not None and bv_f != 0:
            delta      = round(cv_f - bv_f, 4)
            pct_change = round((cv_f - bv_f) / abs(bv_f) * 100, 2)
            if key in lower_is_better:
                direction = "better" if delta < 0 else ("worse" if delta > 0 else "neutral")
            else:
                direction = "better" if delta > 0 else ("worse" if delta < 0 else "neutral")
        else:
            delta      = None
            pct_change = None
            direction  = "neutral"

        rows.append({
            "key": key, "label": label, "unit": unit,
            "baseline": bv_f, "current": cv_f,
            "delta": delta, "pct_change": pct_change,
            "direction": direction,
        })
    return rows
