"""result_db.py — Result SQLite DB schema and I/O.

Contains: _create_result_db_schema, _init_result_db, _seed_default_profiles,
          _result_db_save_benchmark_run, _result_db_save_sweep_run,
          _result_db_get_all_profiles, _result_db_save_profile,
          _result_db_delete_profile, _ensure_result_db_snapshot_cols,
          _insert_benchmark_run_row, _make_slug, _make_run_dir,
          _test_result_db_schema, _BENCHMARK_RUN_COLUMNS.

Extracted from llm_benchmark.py (TASK-LLM-BENCHMARK-MODULE-SPLIT-TOKEN-HC-001-v1).
No behavior changes — identical logic to the original.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime

# ── Path resolution (resolved relative to llm_benchmark/ parent of this package) ──
_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RESULT_DB_PATH: str = os.path.join(_SCRIPT_DIR, "data", "benchmark_results.db")
RESULTS_ROOT: str   = os.path.join(_SCRIPT_DIR, "results")

# Built-in default profile names (stable identity keys)
_DEFAULT_ENV_NAME   = "Default - Dual RTX 5090 vLLM qwen3.6-27b-fp8"
_DEFAULT_HW_NAME    = "Dual RTX 5090 Workstation"
_DEFAULT_SW_NAME    = "vLLM OpenAI Server - Dual RTX 5090"
_DEFAULT_MODEL_NAME = "qwen3.6-27b-fp8"


def _ensure_result_db_snapshot_cols(conn):
    """Add snapshot columns to benchmark_runs if missing (forward migration)."""
    try:
        existing = {row[1] for row in
                    conn.execute("PRAGMA table_info(benchmark_runs)").fetchall()}
    except Exception:
        return
    snapshot_cols = [
        ("environment_profile_name_snapshot",    "TEXT"),
        ("hardware_profile_name_snapshot",       "TEXT"),
        ("software_stack_profile_name_snapshot", "TEXT"),
        ("model_profile_name_snapshot",          "TEXT"),
        ("gpu_model_snapshot",                   "TEXT"),
        ("gpu_count_snapshot",                   "TEXT"),
        ("backend_snapshot",                     "TEXT"),
        ("backend_version_snapshot",             "TEXT"),
        ("api_type_snapshot",                    "TEXT"),
        ("deployment_type_snapshot",             "TEXT"),
        ("reasoning_parser_snapshot",            "TEXT"),
        ("model_family_snapshot",                "TEXT"),
        ("model_size_snapshot",                  "TEXT"),
        ("quantization_snapshot",                "TEXT"),
        ("model_type_snapshot",                  "TEXT"),
        # Applied-environment-params tracking (added in v2)
        ("applied_environment_params",           "INTEGER"),
        ("applied_fields_json",                  "TEXT"),
        ("overridden_fields_json",               "TEXT"),
    ]
    for col, typ in snapshot_cols:
        if col not in existing:
            try:
                conn.execute(f"ALTER TABLE benchmark_runs ADD COLUMN {col} {typ}")
            except Exception:
                pass
    try:
        conn.commit()
    except Exception:
        pass


def _init_result_db(db_path: str | None = None) -> bool:
    """Initialize the result database. Returns True on success."""
    if db_path is None:
        db_path = RESULT_DB_PATH
    try:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        _create_result_db_schema(conn)
        _ensure_result_db_snapshot_cols(conn)
        conn.commit()
        conn.close()
        _seed_default_profiles(db_path)
        return True
    except Exception as e:
        logging.warning("result DB init failed: %s", e)
        return False


def _create_result_db_schema(conn):
    """Create all result DB tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS hardware_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_name TEXT NOT NULL,
            hostname TEXT,
            ip_address TEXT,
            gpu_model TEXT,
            gpu_model_custom TEXT,
            gpu_count TEXT,
            gpu_count_custom TEXT,
            network_type TEXT,
            network_type_custom TEXT,
            storage_type TEXT,
            storage_type_custom TEXT,
            cpu_model TEXT,
            memory_gb TEXT,
            notes TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS software_stack_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_name TEXT NOT NULL,
            backend TEXT,
            backend_custom TEXT,
            backend_version TEXT,
            api_type TEXT,
            api_type_custom TEXT,
            deployment_type TEXT,
            deployment_type_custom TEXT,
            reasoning_parser TEXT,
            reasoning_parser_custom TEXT,
            api_url TEXT,
            container_image TEXT,
            python_version TEXT,
            startup_args TEXT,
            notes TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS model_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_name TEXT NOT NULL,
            display_name TEXT,
            api_model_name TEXT,
            model_path TEXT,
            model_family TEXT,
            model_family_custom TEXT,
            model_size TEXT,
            model_size_custom TEXT,
            quantization TEXT,
            quantization_custom TEXT,
            model_type TEXT,
            model_type_custom TEXT,
            context_length TEXT,
            tensor_parallel TEXT,
            pipeline_parallel TEXT,
            data_parallel TEXT,
            notes TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS environment_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            environment_name TEXT NOT NULL,
            hardware_profile_id INTEGER,
            software_stack_profile_id INTEGER,
            model_profile_id INTEGER,
            notes TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS benchmark_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            run_type TEXT,
            created_at TEXT,
            tool_version TEXT,
            git_commit TEXT,
            api_url TEXT,
            model_name TEXT,
            environment_profile_id INTEGER,
            hardware_profile_id INTEGER,
            software_stack_profile_id INTEGER,
            model_profile_id INTEGER,
            preset TEXT,
            concurrency INTEGER,
            total_requests INTEGER,
            max_tokens INTEGER,
            temperature REAL,
            stream_mode INTEGER,
            output_length_mode TEXT,
            fixed_output_tokens INTEGER,
            ignore_eos INTEGER,
            success INTEGER,
            fail INTEGER,
            success_rate REAL,
            status TEXT,
            duration_sec REAL,
            report_dir TEXT,
            environment_profile_name_snapshot TEXT,
            hardware_profile_name_snapshot TEXT,
            software_stack_profile_name_snapshot TEXT,
            model_profile_name_snapshot TEXT,
            gpu_model_snapshot TEXT,
            gpu_count_snapshot TEXT,
            backend_snapshot TEXT,
            backend_version_snapshot TEXT,
            api_type_snapshot TEXT,
            deployment_type_snapshot TEXT,
            reasoning_parser_snapshot TEXT,
            model_family_snapshot TEXT,
            model_size_snapshot TEXT,
            quantization_snapshot TEXT,
            model_type_snapshot TEXT
        );
        CREATE TABLE IF NOT EXISTS benchmark_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            request_throughput REAL,
            output_token_throughput REAL,
            total_token_throughput REAL,
            per_request_output_tps_avg REAL,
            ttft_avg REAL, ttft_p50 REAL, ttft_p95 REAL, ttft_p99 REAL,
            first_generated_avg REAL, first_generated_p50 REAL,
            first_generated_p95 REAL, first_generated_p99 REAL,
            first_answer_avg REAL, first_answer_p50 REAL,
            first_answer_p95 REAL, first_answer_p99 REAL,
            first_reasoning_avg REAL, first_reasoning_p50 REAL,
            first_reasoning_p95 REAL, first_reasoning_p99 REAL,
            tpot_avg REAL, tpot_p50 REAL, tpot_p95 REAL, tpot_p99 REAL,
            itl_avg REAL, itl_p50 REAL, itl_p95 REAL, itl_p99 REAL,
            e2el_avg REAL, e2el_p50 REAL, e2el_p95 REAL, e2el_p99 REAL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            token_source TEXT
        );
        CREATE TABLE IF NOT EXISTS parser_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            parser_mode TEXT,
            generated_fields_json TEXT,
            observed_delta_keys_json TEXT,
            unknown_delta_keys_json TEXT,
            answer_field_observed INTEGER,
            reasoning_field_observed INTEGER,
            usage_supported INTEGER,
            usage_source TEXT,
            stream_warnings_json TEXT
        );
        CREATE TABLE IF NOT EXISTS benchmark_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            artifact_type TEXT,
            path TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sweep_cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            case_index INTEGER,
            concurrency INTEGER,
            total_requests INTEGER,
            success INTEGER,
            fail INTEGER,
            duration_sec REAL,
            output_token_throughput REAL,
            request_throughput REAL,
            e2el_avg REAL,
            e2el_p95 REAL,
            ttft_avg REAL,
            ttft_p95 REAL,
            tpot_avg REAL,
            itl_avg REAL,
            throughput_efficiency REAL,
            artifact_dir TEXT
        );
    """)


def _test_result_db_schema():
    """Validate result DB schema by creating a temp DB and checking all tables."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_path = f.name
    try:
        conn = sqlite3.connect(tmp_path)
        _create_result_db_schema(conn)
        conn.commit()
        required_tables = [
            "hardware_profiles", "software_stack_profiles", "model_profiles",
            "environment_profiles", "benchmark_runs", "benchmark_metrics",
            "parser_profiles", "benchmark_artifacts", "sweep_cases",
        ]
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for t in required_tables:
            assert t in tables, f"Table missing: {t}"
        conn.close()
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def _make_slug(s: str, maxlen: int = 80) -> str:
    """Convert a string to a filesystem-safe slug."""
    if not s:
        return "unknown"
    import re
    s = str(s).lower().strip()
    s = re.sub(r'[/\\:*?"<>|]', '-', s)
    s = re.sub(r'\s+', '-', s)
    s = re.sub(r'-+', '-', s)
    s = s.strip('-')
    return s[:maxlen] if s else "unknown"


def _make_run_dir(results_root: str, run_type: str, model_slug: str,
                  backend_slug: str, gpu_slug: str, gpu_count: str,
                  params_slug: str) -> str:
    """Create and return a structured artifact directory for a run."""
    date_str = datetime.now().strftime("%Y%m%d")
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    gpu_str = f"{gpu_slug}x{gpu_count}" if gpu_count and gpu_count != "unknown" else gpu_slug
    dirname = f"{model_slug}__{backend_slug}__{gpu_str}__{params_slug}__{ts_str}"
    # Truncate if too long
    if len(dirname) > 200:
        dirname = dirname[:196] + "_trunc"
    path = os.path.join(results_root, run_type, date_str, dirname)
    os.makedirs(path, exist_ok=True)
    return path


# Canonical column list for benchmark_runs INSERT — defines the exact set of
# fields written at run time.  Any column not listed here is left NULL.
# IMPORTANT: keep in sync with _ensure_result_db_snapshot_cols() and the
# benchmark_runs CREATE TABLE statement above.
_BENCHMARK_RUN_COLUMNS = [
    "run_id", "run_type", "created_at", "tool_version", "git_commit",
    "api_url", "model_name",
    "environment_profile_id", "hardware_profile_id",
    "software_stack_profile_id", "model_profile_id",
    "preset", "concurrency", "total_requests", "max_tokens", "temperature",
    "stream_mode", "output_length_mode", "fixed_output_tokens", "ignore_eos",
    "success", "fail", "success_rate", "status", "duration_sec", "report_dir",
    "environment_profile_name_snapshot", "hardware_profile_name_snapshot",
    "software_stack_profile_name_snapshot", "model_profile_name_snapshot",
    "gpu_model_snapshot", "gpu_count_snapshot",
    "backend_snapshot", "backend_version_snapshot",
    "api_type_snapshot", "deployment_type_snapshot", "reasoning_parser_snapshot",
    "model_family_snapshot", "model_size_snapshot",
    "quantization_snapshot", "model_type_snapshot",
    "applied_environment_params", "applied_fields_json", "overridden_fields_json",
]


def _insert_benchmark_run_row(conn, run_data: dict) -> None:
    """Insert one row into benchmark_runs using named parameters (:col_name).

    Column names come from _BENCHMARK_RUN_COLUMNS.  Missing keys in run_data
    default to None.  Using named parameters eliminates positional
    column/value count mismatches.
    """
    cols = _BENCHMARK_RUN_COLUMNS
    placeholders = ", ".join(f":{c}" for c in cols)
    sql = (f"INSERT INTO benchmark_runs ({', '.join(cols)}) "
           f"VALUES ({placeholders})")
    payload = {col: run_data.get(col) for col in cols}
    conn.execute(sql, payload)


def _result_db_save_benchmark_run(db_path: str, summary: dict,
                                  env_profile_id: int | None,
                                  hw_id: int | None, sw_id: int | None,
                                  model_id: int | None, report_dir: str = "",
                                  snapshots: dict | None = None,
                                  applied_info: dict | None = None) -> str | None:
    """Save a single benchmark result to the result DB. Returns run_id or None."""
    if snapshots is None:
        snapshots = {}
    if applied_info is None:
        applied_info = {}
    try:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        _create_result_db_schema(conn)
        _ensure_result_db_snapshot_cols(conn)
        run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S_%f")
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        run_data = {
            "run_id":                  run_id,
            "run_type":                "single",
            "created_at":              created_at,
            "tool_version":            None,
            "git_commit":              None,
            "api_url":                 summary.get("api_url", ""),
            "model_name":              summary.get("model", ""),
            "environment_profile_id":         env_profile_id,
            "hardware_profile_id":            hw_id,
            "software_stack_profile_id":      sw_id,
            "model_profile_id":               model_id,
            "preset":                  summary.get("preset"),
            "concurrency":             summary.get("concurrency", 0),
            "total_requests":          summary.get("total", 0),
            "max_tokens":              summary.get("max_tokens", 0),
            "temperature":             summary.get("temperature", 0),
            "stream_mode":             int(bool(summary.get("stream_mode", False))),
            "output_length_mode":      summary.get("output_length_mode", "normal"),
            "fixed_output_tokens":     summary.get("fixed_output_tokens"),
            "ignore_eos":              int(summary.get("output_length_mode", "") == "fixed"),
            "success":                 summary.get("success", 0),
            "fail":                    summary.get("fail", 0),
            "success_rate":            summary.get("success_rate", 0),
            "status":                  ("completed" if summary.get("fail", 0) == 0
                                        else "completed_with_failures"),
            "duration_sec":            summary.get("duration_sec", 0),
            "report_dir":              report_dir,
            # ── Environment snapshots ──
            "environment_profile_name_snapshot":    snapshots.get("env_name", ""),
            "hardware_profile_name_snapshot":        snapshots.get("hw_name", ""),
            "software_stack_profile_name_snapshot":  snapshots.get("sw_name", ""),
            "model_profile_name_snapshot":           snapshots.get("model_name", ""),
            "gpu_model_snapshot":                    snapshots.get("gpu_model", ""),
            "gpu_count_snapshot":       str(snapshots.get("gpu_count", "") or ""),
            "backend_snapshot":                      snapshots.get("backend", ""),
            "backend_version_snapshot":              snapshots.get("backend_version", ""),
            "api_type_snapshot":                     snapshots.get("api_type", ""),
            "deployment_type_snapshot":              snapshots.get("deployment_type", ""),
            "reasoning_parser_snapshot":             snapshots.get("reasoning_parser", ""),
            "model_family_snapshot":                 snapshots.get("model_family", ""),
            "model_size_snapshot":                   snapshots.get("model_size", ""),
            "quantization_snapshot":                 snapshots.get("quantization", ""),
            "model_type_snapshot":                   snapshots.get("model_type", ""),
            # ── Applied-environment-params tracking ──
            "applied_environment_params": int(bool(
                applied_info.get("applied_environment_params", False))),
            "applied_fields_json":   json.dumps(
                applied_info.get("applied_fields_json", [])),
            "overridden_fields_json": json.dumps(
                applied_info.get("overridden_fields_json", [])),
        }
        _insert_benchmark_run_row(conn, run_data)
        conn.execute("""INSERT INTO benchmark_metrics
            (run_id, request_throughput, output_token_throughput,
             per_request_output_tps_avg,
             ttft_avg, ttft_p50, ttft_p95, ttft_p99,
             tpot_avg, tpot_p50, tpot_p95, tpot_p99,
             itl_avg, itl_p50, itl_p95, itl_p99,
             e2el_avg, e2el_p50, e2el_p95, e2el_p99,
             input_tokens, output_tokens, total_tokens)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id,
             summary.get("request_throughput_rps", 0),
             summary.get("system_output_tps", 0),
             summary.get("per_request_output_tps_avg", 0),
             summary.get("ttft_avg", 0), summary.get("ttft_p50", 0),
             summary.get("ttft_p95", 0), summary.get("ttft_p99", 0),
             summary.get("tpot_avg", 0), summary.get("tpot_p50", 0),
             summary.get("tpot_p95", 0), summary.get("tpot_p99", 0),
             summary.get("itl_avg", 0), summary.get("itl_p50", 0),
             summary.get("itl_p95", 0), summary.get("itl_p99", 0),
             summary.get("e2el_avg", 0), summary.get("e2el_p50", 0),
             summary.get("e2el_p95", 0), summary.get("e2el_p99", 0),
             summary.get("total_input_tokens", 0),
             summary.get("total_output_tokens", 0),
             summary.get("total_tokens", 0)))
        conn.commit()
        conn.close()
        return run_id
    except Exception as e:
        logging.warning("result DB save failed: %s", e)
        return None


def _result_db_save_sweep_run(db_path: str, sweep_result: dict,
                               env_profile_id: int | None,
                               hw_id: int | None, sw_id: int | None,
                               model_id: int | None, report_dir: str = "",
                               snapshots: dict | None = None,
                               applied_info: dict | None = None) -> str | None:
    """Save a sweep result to the result DB. Returns run_id or None."""
    if snapshots is None:
        snapshots = {}
    if applied_info is None:
        applied_info = {}
    try:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        _create_result_db_schema(conn)
        _ensure_result_db_snapshot_cols(conn)
        run_id = sweep_result.get("sweep_id") or datetime.now().strftime("sweep_%Y%m%d_%H%M%S")
        created_at = sweep_result.get("finished_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cases = sweep_result.get("cases", [])
        levels = sweep_result.get("concurrency_levels", [])
        total_success = sum(c.get("benchmark_summary", {}).get("success", 0) for c in cases)
        total_fail    = sum(c.get("benchmark_summary", {}).get("fail", 0)    for c in cases)
        run_data = {
            "run_id":                  run_id,
            "run_type":                "sweep",
            "created_at":              created_at,
            "tool_version":            None,
            "git_commit":              None,
            "api_url":                 sweep_result.get("api_url", ""),
            "model_name":              sweep_result.get("model", ""),
            "environment_profile_id":         env_profile_id,
            "hardware_profile_id":            hw_id,
            "software_stack_profile_id":      sw_id,
            "model_profile_id":               model_id,
            "preset":                  None,
            "concurrency":             len(levels),
            "total_requests":          sum(c.get("total_requests", 0) for c in cases),
            "max_tokens":              sweep_result.get("fixed_output_tokens") or 0,
            "temperature":             0,
            "stream_mode":             1,
            "output_length_mode":      sweep_result.get("output_length_mode", "normal"),
            "fixed_output_tokens":     sweep_result.get("fixed_output_tokens"),
            "ignore_eos":              None,
            "success":                 total_success,
            "fail":                    total_fail,
            "success_rate":            0,
            "status":                  "completed",
            "duration_sec":            None,
            "report_dir":              report_dir,
            # ── Environment snapshots ──
            "environment_profile_name_snapshot":    snapshots.get("env_name", ""),
            "hardware_profile_name_snapshot":        snapshots.get("hw_name", ""),
            "software_stack_profile_name_snapshot":  snapshots.get("sw_name", ""),
            "model_profile_name_snapshot":           snapshots.get("model_name", ""),
            "gpu_model_snapshot":                    snapshots.get("gpu_model", ""),
            "gpu_count_snapshot":       str(snapshots.get("gpu_count", "") or ""),
            "backend_snapshot":                      snapshots.get("backend", ""),
            "backend_version_snapshot":              snapshots.get("backend_version", ""),
            "api_type_snapshot":                     snapshots.get("api_type", ""),
            "deployment_type_snapshot":              snapshots.get("deployment_type", ""),
            "reasoning_parser_snapshot":             snapshots.get("reasoning_parser", ""),
            "model_family_snapshot":                 snapshots.get("model_family", ""),
            "model_size_snapshot":                   snapshots.get("model_size", ""),
            "quantization_snapshot":                 snapshots.get("quantization", ""),
            "model_type_snapshot":                   snapshots.get("model_type", ""),
            # ── Applied-environment-params tracking ──
            "applied_environment_params": int(bool(
                applied_info.get("applied_environment_params", False))),
            "applied_fields_json":   json.dumps(
                applied_info.get("applied_fields_json", [])),
            "overridden_fields_json": json.dumps(
                applied_info.get("overridden_fields_json", [])),
        }
        _insert_benchmark_run_row(conn, run_data)
        for idx, case in enumerate(cases):
            s = case.get("benchmark_summary", {})
            am = case.get("analysis_metrics", {})
            conn.execute("""INSERT INTO sweep_cases
                (run_id, case_index, concurrency, total_requests,
                 success, fail, duration_sec,
                 output_token_throughput, request_throughput,
                 e2el_avg, e2el_p95, ttft_avg, ttft_p95,
                 tpot_avg, itl_avg, throughput_efficiency)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, idx, case.get("concurrency", 0),
                 case.get("total_requests", 0),
                 s.get("success", 0), s.get("fail", 0),
                 s.get("duration_sec", 0),
                 s.get("system_output_tps", 0),
                 s.get("request_throughput_rps", 0),
                 s.get("e2el_avg", 0), s.get("e2el_p95", 0),
                 s.get("ttft_avg", 0), s.get("ttft_p95", 0),
                 s.get("tpot_avg", 0), s.get("itl_avg", 0),
                 am.get("throughput_efficiency", 0)))
        conn.commit()
        conn.close()
        return run_id
    except Exception as e:
        logging.warning("result DB sweep save failed: %s", e)
        return None


def _seed_default_profiles(db_path: str) -> None:
    """Insert built-in default profiles if they don't already exist (idempotent).
    Uses stable profile_name / environment_name as identity keys — safe to call
    multiple times; will never create duplicates."""
    try:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        _create_result_db_schema(conn)
        _ensure_result_db_snapshot_cols(conn)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # ── Hardware profile ──
        hw_row = conn.execute(
            "SELECT id FROM hardware_profiles WHERE profile_name=?",
            (_DEFAULT_HW_NAME,)).fetchone()
        if hw_row:
            hw_id = hw_row[0]
        else:
            cur = conn.execute(
                """INSERT INTO hardware_profiles
                   (profile_name, hostname, ip_address,
                    gpu_model, gpu_count,
                    network_type, storage_type, notes,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (_DEFAULT_HW_NAME,
                 "ai-srv-5090x2", "192.168.1.250",
                 "RTX 5090", "2",
                 "PCIe Single Node", "NVMe SSD",
                 "Default built-in profile for JISUMAN dual RTX 5090 benchmark environment.",
                 now, now))
            hw_id = cur.lastrowid

        # ── Software stack profile ──
        sw_row = conn.execute(
            "SELECT id FROM software_stack_profiles WHERE profile_name=?",
            (_DEFAULT_SW_NAME,)).fetchone()
        if sw_row:
            sw_id = sw_row[0]
        else:
            cur = conn.execute(
                """INSERT INTO software_stack_profiles
                   (profile_name, backend, api_type,
                    deployment_type, reasoning_parser,
                    api_url, notes, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (_DEFAULT_SW_NAME,
                 "vLLM",
                 "OpenAI-compatible Chat Completions",
                 "Docker", "qwen3",
                 "http://192.168.1.250:8000/v1/chat/completions",
                 "Default vLLM OpenAI-compatible endpoint for dual RTX 5090 testing.",
                 now, now))
            sw_id = cur.lastrowid

        # ── Model profile ──
        model_row = conn.execute(
            "SELECT id FROM model_profiles WHERE profile_name=?",
            (_DEFAULT_MODEL_NAME,)).fetchone()
        if model_row:
            model_id = model_row[0]
        else:
            cur = conn.execute(
                """INSERT INTO model_profiles
                   (profile_name, display_name, api_model_name, model_path,
                    model_family, model_size,
                    quantization, model_type,
                    tensor_parallel, notes,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_DEFAULT_MODEL_NAME,
                 "Qwen3.6-27B-FP8", "qwen3.6-27b-fp8",
                 "/data/models/Qwen3.6-27B-FP8",
                 "Qwen", "27B", "FP8", "Reasoning", "2",
                 "Default model profile used for qwen3.6-27b-fp8 dual RTX 5090 baseline.",
                 now, now))
            model_id = cur.lastrowid

        # ── Environment profile ──
        env_row = conn.execute(
            "SELECT id FROM environment_profiles WHERE environment_name=?",
            (_DEFAULT_ENV_NAME,)).fetchone()
        if not env_row:
            conn.execute(
                """INSERT INTO environment_profiles
                   (environment_name,
                    hardware_profile_id, software_stack_profile_id, model_profile_id,
                    notes, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (_DEFAULT_ENV_NAME, hw_id, sw_id, model_id,
                 "Built-in default profile. Duplicate and edit for your own environment.",
                 now, now))

        conn.commit()
        conn.close()
    except Exception as e:
        logging.warning("seed default profiles failed: %s", e)


def _result_db_get_all_profiles(db_path: str, table: str) -> list:
    """Load all rows from a profile table. Returns list of dicts."""
    try:
        if not os.path.exists(db_path):
            return []
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _result_db_save_profile(db_path: str, table: str, data: dict) -> int | None:
    """Upsert a profile row. Returns the row id or None on error."""
    try:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        _create_result_db_schema(conn)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        pid = data.get("id")
        if pid:
            data = dict(data)
            data["updated_at"] = now
            data.pop("id", None)
            cols = ", ".join(f"{k}=?" for k in data)
            conn.execute(f"UPDATE {table} SET {cols} WHERE id=?",
                         [*data.values(), pid])
        else:
            data = dict(data)
            data["created_at"] = now
            data["updated_at"] = now
            data.pop("id", None)
            cols = ", ".join(data.keys())
            placeholders = ", ".join("?" * len(data))
            cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
                               list(data.values()))
            pid = cur.lastrowid
        conn.commit()
        conn.close()
        return pid
    except Exception as e:
        logging.warning("profile save failed (%s): %s", table, e)
        return None


def _result_db_delete_profile(db_path: str, table: str, pid: int) -> bool:
    """Delete a profile row. Returns True on success."""
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(f"DELETE FROM {table} WHERE id=?", (pid,))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False
