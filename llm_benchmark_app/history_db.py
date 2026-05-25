"""history_db.py — History SQLite DB schema and I/O.

Contains: _create_latest_schema, init_db, _ensure_history_columns,
          save_result, save_sweep_history, load_history.

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

# ── Module-level configuration — overridable by parent module after import ────
# Set: import llm_benchmark_app.history_db as _hdb; _hdb.DB_PATH = DB_PATH
DB_PATH: str = os.path.join(_SCRIPT_DIR, "llm_benchmark_history.db")
DB_SCHEMA_VERSION: int = 2
DEBUG_MODE: bool = False


def _create_latest_schema(conn):
    """Create the latest benchmarks + benchmark_meta tables."""
    conn.execute("""CREATE TABLE benchmarks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        record_type TEXT NOT NULL DEFAULT 'single',
        status TEXT NOT NULL DEFAULT 'completed',

        api_url TEXT NOT NULL,
        model TEXT NOT NULL,
        prompt TEXT,
        max_tokens INTEGER,
        temperature REAL,
        concurrency INTEGER NOT NULL,
        total INTEGER NOT NULL,
        stream_mode INTEGER NOT NULL,

        metric_standard TEXT NOT NULL,
        metric_references_json TEXT,
        metric_warnings_json TEXT,

        success INTEGER NOT NULL,
        fail INTEGER NOT NULL,
        success_rate REAL,

        duration_sec REAL,
        request_throughput_rps REAL,
        request_throughput REAL,

        total_input_tokens INTEGER,
        total_output_tokens INTEGER,
        total_tokens INTEGER,

        system_output_tps REAL,
        system_total_tps REAL,
        output_token_throughput REAL,

        e2e_latency_min REAL,
        e2e_latency_avg REAL,
        e2e_latency_max REAL,
        e2e_latency_p50 REAL,
        e2e_latency_p95 REAL,
        e2e_latency_p99 REAL,

        e2el_avg REAL,
        e2el_p50 REAL,
        e2el_p95 REAL,
        e2el_p99 REAL,

        ttft_avg REAL,
        ttft_p50 REAL,
        ttft_p95 REAL,
        ttft_p99 REAL,

        tpot_avg REAL,
        tpot_p50 REAL,
        tpot_p95 REAL,
        tpot_p99 REAL,

        itl_avg REAL,
        itl_p50 REAL,
        itl_p95 REAL,
        itl_p99 REAL,

        per_request_output_tps_avg REAL,
        per_request_output_tps_p50 REAL,
        per_request_output_tps_p95 REAL,

        detail_json TEXT,
        fail_detail_json TEXT,
        summary_json TEXT,

        config_summary TEXT,
        primary_metric TEXT,
        json_path TEXT,
        markdown_path TEXT,
        png_path TEXT,

        concurrency_levels TEXT,
        max_output_tps REAL,
        max_output_tps_concurrency INTEGER,
        recommended_concurrency INTEGER,
        analysis_summary TEXT
    )""")
    conn.execute("""CREATE TABLE benchmark_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""")
    conn.execute(
        "INSERT OR REPLACE INTO benchmark_meta(key, value) VALUES ('schema_version', ?)",
        (str(DB_SCHEMA_VERSION),),
    )
    conn.execute(
        "INSERT OR REPLACE INTO benchmark_meta(key, value) VALUES ('metric_standard', ?)",
        ("JISUMAN LLM Benchmark Standard v1",),
    )


def init_db():
    """Initialize or upgrade the database to the latest standard schema.

    If the DB file exists but its schema_version != DB_SCHEMA_VERSION, the old
    file is backed up to *.bak.YYYYmmdd_HHMMSS and a fresh schema is created.
    """
    import shutil

    if not os.path.exists(DB_PATH):
        conn = sqlite3.connect(DB_PATH)
        _create_latest_schema(conn)
        conn.commit()
        _ensure_history_columns(conn)
        conn.close()
        return

    # DB exists — check version
    try:
        conn = sqlite3.connect(DB_PATH)
        meta = dict(conn.execute("SELECT key, value FROM benchmark_meta").fetchall())
        version = int(meta.get("schema_version", 0))
        if version == DB_SCHEMA_VERSION:
            _ensure_history_columns(conn)
            conn.close()
            return  # already latest
        conn.close()
    except Exception:
        pass  # no meta table or unreadable — needs rebuild

    # Backup old DB then rebuild
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{DB_PATH}.bak.{ts}"
    try:
        shutil.copy2(DB_PATH, backup_path)
        if DEBUG_MODE:
            logging.info("DB backed up to %s", backup_path)
    except Exception as e:
        if DEBUG_MODE:
            logging.warning("DB backup failed: %s", e)

    os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    _create_latest_schema(conn)
    conn.commit()
    _ensure_history_columns(conn)
    conn.close()


def _ensure_history_columns(conn):
    """Add additive history fields used by both single and sweep records."""
    has_benchmarks = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='benchmarks'"
    ).fetchone()
    if not has_benchmarks:
        _create_latest_schema(conn)
        conn.commit()
        return
    existing = {row[1] for row in conn.execute("PRAGMA table_info(benchmarks)").fetchall()}
    columns = {
        "record_type": "TEXT NOT NULL DEFAULT 'single'",
        "status": "TEXT NOT NULL DEFAULT 'completed'",
        "config_summary": "TEXT",
        "primary_metric": "TEXT",
        "json_path": "TEXT",
        "markdown_path": "TEXT",
        "png_path": "TEXT",
        "concurrency_levels": "TEXT",
        "max_output_tps": "REAL",
        "max_output_tps_concurrency": "INTEGER",
        "recommended_concurrency": "INTEGER",
        "analysis_summary": "TEXT",
    }
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE benchmarks ADD COLUMN {name} {ddl}")
    conn.commit()


def save_result(d: dict):
    conn = sqlite3.connect(DB_PATH)
    _ensure_history_columns(conn)
    cur = conn.execute(
        """INSERT INTO benchmarks
           (created_at, api_url, model, prompt, max_tokens, temperature,
            concurrency, total, stream_mode,
            metric_standard, metric_references_json, metric_warnings_json,
            success, fail, success_rate,
            duration_sec, request_throughput_rps, request_throughput,
            total_input_tokens, total_output_tokens, total_tokens,
            system_output_tps, system_total_tps, output_token_throughput,
            e2e_latency_min, e2e_latency_avg, e2e_latency_max,
            e2e_latency_p50, e2e_latency_p95, e2e_latency_p99,
            e2el_avg, e2el_p50, e2el_p95, e2el_p99,
            ttft_avg, ttft_p50, ttft_p95, ttft_p99,
            tpot_avg, tpot_p50, tpot_p95, tpot_p99,
            itl_avg, itl_p50, itl_p95, itl_p99,
            per_request_output_tps_avg, per_request_output_tps_p50, per_request_output_tps_p95,
            detail_json, fail_detail_json, summary_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            d["api_url"], d["model"], d.get("prompt", ""), d["max_tokens"], d["temperature"],
            d["concurrency"], d["total"], int(d.get("stream_mode", False)),
            d.get("metric_standard", "JISUMAN LLM Benchmark Standard v1"),
            json.dumps(d.get("metric_references", []), ensure_ascii=False),
            json.dumps(d.get("metric_warnings", []), ensure_ascii=False),
            d["success"], d["fail"], d.get("success_rate"),
            d.get("duration_sec"), d.get("request_throughput_rps"), d.get("request_throughput"),
            d.get("total_input_tokens"), d.get("total_output_tokens"), d.get("total_tokens"),
            d.get("system_output_tps"), d.get("system_total_tps"), d.get("output_token_throughput"),
            d.get("e2e_latency_min"), d.get("e2e_latency_avg"), d.get("e2e_latency_max"),
            d.get("e2e_latency_p50"), d.get("e2e_latency_p95"), d.get("e2e_latency_p99"),
            d.get("e2el_avg"), d.get("e2el_p50"), d.get("e2el_p95"), d.get("e2el_p99"),
            d.get("ttft_avg"), d.get("ttft_p50"), d.get("ttft_p95"), d.get("ttft_p99"),
            d.get("tpot_avg"), d.get("tpot_p50"), d.get("tpot_p95"), d.get("tpot_p99"),
            d.get("itl_avg"), d.get("itl_p50"), d.get("itl_p95"), d.get("itl_p99"),
            d.get("per_request_output_tps_avg"), d.get("per_request_output_tps_p50"), d.get("per_request_output_tps_p95"),
            json.dumps(d.get("detail", []), ensure_ascii=False),
            json.dumps(d.get("fail_detail", []), ensure_ascii=False),
            json.dumps(d, ensure_ascii=False),  # full summary as JSON
        ),
    )
    config_summary = (
        f"C{d.get('concurrency')} / N{d.get('total')} / "
        f"max_tokens={d.get('max_tokens')}"
    )
    primary_metric = (
        f"Output TPS {d.get('system_output_tps', 0) or 0:.1f} | "
        f"E2E P95 {d.get('e2e_latency_p95', 0) or 0:.3f}s | "
        f"Success {d.get('success_rate', 0) or 0:.0f}%"
    )
    conn.execute(
        """UPDATE benchmarks
           SET record_type='single', status=?, config_summary=?, primary_metric=?,
               json_path='', markdown_path='', png_path=''
           WHERE id=?""",
        ("completed" if d.get("fail", 0) == 0 else "completed_with_failures",
         config_summary, primary_metric, cur.lastrowid),
    )
    conn.commit()
    conn.close()


def save_sweep_history(sweep_result: dict, json_path: str = "",
                       markdown_path: str = "", png_path: str = "",
                       status: str = "completed"):
    """Persist a sweep record without storing PNG binary data."""
    conn = sqlite3.connect(DB_PATH)
    _ensure_history_columns(conn)

    cases = sweep_result.get("cases", [])
    levels = sweep_result.get("concurrency_levels", [])
    multiplier = sweep_result.get("requests_multiplier", "")
    model = sweep_result.get("model", "")
    api_url = sweep_result.get("api_url", "")
    analysis_summary = sweep_result.get("analysis_summary", [])

    max_output_tps = 0.0
    max_output_tps_concurrency = None
    recommended_concurrency = None
    success_concs = []
    for case in cases:
        c = case.get("concurrency")
        summary = case.get("benchmark_summary", {})
        tps = summary.get("system_output_tps", 0) or 0
        if tps >= max_output_tps:
            max_output_tps = tps
            max_output_tps_concurrency = c
        if (summary.get("success_rate", 0) or 0) >= 95:
            success_concs.append(c)
    if success_concs:
        recommended_concurrency = success_concs[-1]
    elif max_output_tps_concurrency is not None:
        recommended_concurrency = max_output_tps_concurrency

    config_summary = f"{','.join('C' + str(c) for c in levels)} / multiplier={multiplier}"
    primary_metric = (
        f"Max TPS {max_output_tps:.0f} tok/s @ C{max_output_tps_concurrency or '-'} | "
        f"Recommended C{recommended_concurrency or '-'}"
    )
    created_at = sweep_result.get("finished_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn.execute(
        """INSERT INTO benchmarks
           (created_at, record_type, status, api_url, model, prompt,
            max_tokens, temperature, concurrency, total, stream_mode,
            metric_standard, success, fail, success_rate,
            summary_json, config_summary, primary_metric,
            json_path, markdown_path, png_path,
            concurrency_levels, max_output_tps, max_output_tps_concurrency,
            recommended_concurrency, analysis_summary)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            created_at, "sweep", status, api_url, model, "",
            None, None, 0, sum(c.get("total_requests", 0) for c in cases), 0,
            "JISUMAN LLM Benchmark Standard v1",
            sum((c.get("benchmark_summary", {}).get("success", 0) or 0) for c in cases),
            sum((c.get("benchmark_summary", {}).get("fail", 0) or 0) for c in cases),
            None,
            json.dumps(sweep_result, ensure_ascii=False, default=str),
            config_summary, primary_metric,
            json_path or "", markdown_path or "", png_path or "",
            json.dumps(levels, ensure_ascii=False),
            max_output_tps, max_output_tps_concurrency,
            recommended_concurrency,
            json.dumps(analysis_summary, ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()


def load_history(limit=50):
    conn = sqlite3.connect(DB_PATH)
    _ensure_history_columns(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, created_at, record_type, status, model, api_url, "
        "config_summary, primary_metric, json_path, markdown_path, png_path, "
        "concurrency_levels, max_output_tps, max_output_tps_concurrency, "
        "recommended_concurrency, analysis_summary, summary_json, "
        "concurrency, total, "
        "success, fail, success_rate, "
        "e2e_latency_avg, e2e_latency_p95, e2el_avg, e2el_p95, "
        "ttft_avg, "
        "system_output_tps, output_token_throughput, "
        "request_throughput_rps, request_throughput, "
        "total_output_tokens, total_tokens, "
        "itl_avg, stream_mode, duration_sec, "
        "metric_standard, metric_warnings_json "
        "FROM benchmarks ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return rows
