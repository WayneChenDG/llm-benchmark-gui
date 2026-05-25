"""llm_benchmark_app — modular core for JISUMAN LLM Benchmark GUI.

Modules:
  stream_parser         — SSE chunk parsing, ParsedStreamChunk
  metrics               — percentile, aggregate_results, validate_metric_consistency
  runner                — call_llm, run_benchmark, build_chat_payload
  high_concurrency_runner — HC async runner (asyncio + aiohttp)
  history_db            — history SQLite DB schema and I/O
  result_db             — result SQLite DB schema and I/O
  token_calibration     — input/output token calibration (stub)
  artifact_resolver     — report directory scanning (stub)
"""
from .stream_parser import ParsedStreamChunk, _parse_stream_chunk, _build_parser_profile
from .metrics import (percentile, clean_numbers, aggregate_results,
                      validate_metric_consistency, get_metric_standard_definitions,
                      STANDARD_METRIC_ALIASES, DB_SCHEMA_VERSION)
from .runner import call_llm, run_benchmark, build_chat_payload, normalize_api_url, check_server_reachable, fetch_models
from .high_concurrency_runner import _hc_classify_error, _hc_fail_result
from .history_db import (
    _create_latest_schema, init_db, _ensure_history_columns,
    save_result, save_sweep_history, load_history,
)
from .result_db import (
    _init_result_db, _create_result_db_schema, _ensure_result_db_snapshot_cols,
    _insert_benchmark_run_row, _result_db_save_benchmark_run, _result_db_save_sweep_run,
    _seed_default_profiles, _result_db_get_all_profiles, _result_db_save_profile,
    _result_db_delete_profile, _make_slug, _make_run_dir, _BENCHMARK_RUN_COLUMNS,
    RESULT_DB_PATH, RESULTS_ROOT,
)

__all__ = [
    # stream_parser
    "ParsedStreamChunk", "_parse_stream_chunk", "_build_parser_profile",
    # metrics
    "percentile", "clean_numbers", "aggregate_results", "validate_metric_consistency",
    "get_metric_standard_definitions", "STANDARD_METRIC_ALIASES", "DB_SCHEMA_VERSION",
    # runner
    "call_llm", "run_benchmark", "build_chat_payload", "normalize_api_url",
    "check_server_reachable", "fetch_models",
    # high_concurrency_runner
    "_hc_classify_error", "_hc_fail_result",
    # history_db
    "_create_latest_schema", "init_db", "_ensure_history_columns",
    "save_result", "save_sweep_history", "load_history",
    # result_db
    "_init_result_db", "_create_result_db_schema", "_ensure_result_db_snapshot_cols",
    "_insert_benchmark_run_row", "_result_db_save_benchmark_run", "_result_db_save_sweep_run",
    "_seed_default_profiles", "_result_db_get_all_profiles", "_result_db_save_profile",
    "_result_db_delete_profile", "_make_slug", "_make_run_dir", "_BENCHMARK_RUN_COLUMNS",
    "RESULT_DB_PATH", "RESULTS_ROOT",
]
