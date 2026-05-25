"""high_concurrency_runner.py — HC async runner helpers.

Module-level helper functions for the high-concurrency (asyncio + aiohttp) sweep runner.
Extracted from llm_benchmark.py (TASK-LLM-BENCHMARK-MODULE-SPLIT-TOKEN-HC-001-v1).
No behavior changes — identical logic to the original.
"""
from __future__ import annotations


def _hc_classify_error(exc: Exception) -> str:
    """Classify an exception into a failure taxonomy string.
    Taxonomy: timeout, connect_error, read_error, server_5xx, server_4xx,
              json_parse_error, stream_parse_error, cancelled,
              client_resource_error, unknown.
    """
    name = type(exc).__name__.lower()
    msg  = str(exc).lower()
    if "timeout" in name or "timeout" in msg:
        return "timeout"
    if "connector" in name or "connect" in msg:
        return "connect_error"
    if "disconnect" in name or "read" in name:
        return "read_error"
    if "oserror" in name or "connectionerror" in name:
        return "client_resource_error"
    if "json" in name or "json" in msg:
        return "json_parse_error"
    if "cancel" in name:
        return "cancelled"
    return "unknown"


def _hc_fail_result(err_type: str, err_msg: str, e2e: float = 0.0) -> dict:
    """Build a failure result dict compatible with aggregate_results()."""
    return {
        "ok": False,
        "e2e_latency": round(e2e, 6),
        "latency": round(e2e, 6),
        "ttft": None, "tpot": None, "itl_avg": None, "itl_values": [],
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "per_request_output_tps_e2e": 0.0, "per_request_decode_tps": None,
        "finish_reason": "",
        "stream": True,
        "error": err_msg,
        "error_type": err_type,
    }
