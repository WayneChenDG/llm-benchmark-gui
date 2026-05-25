"""high_concurrency_runner.py — HC async runner helpers.

Module-level helper functions for the high-concurrency (asyncio + aiohttp) sweep runner.
Extracted from llm_benchmark.py (TASK-LLM-BENCHMARK-MODULE-SPLIT-TOKEN-HC-001-v1).
Updated in TASK-LLM-BENCHMARK-TOKEN-CALIBRATION-HIGH-CONCURRENCY-001-v1:
  - Added connection_reset to failure taxonomy
  - Windows WinError 10054 → connection_reset (connection reset by peer)
  - Windows WinError 10060 → connect_timeout (WSAETIMEDOUT, connection attempt timed out)
Updated in TASK-LLM-BENCHMARK-TOKEN-CALIBRATION-HC-RUNNER-001-v1:
  - Taxonomy extended: connect_timeout as separate category from timeout
  - WinError 10060 corrected to connect_timeout (was connect_error)
"""
from __future__ import annotations


def _hc_classify_error(exc: Exception) -> str:
    """Classify an exception into a failure taxonomy string.

    Taxonomy:
      timeout            — request/read timed out (generic)
      connect_error      — could not connect (DNS, refused, etc.)
      connect_timeout    — connection attempt timed out (separate from read timeout)
      connection_reset   — peer reset the connection mid-flight
      read_error         — server disconnected during response read
      server_5xx         — server returned 5xx status
      server_4xx         — server returned 4xx status
      json_parse_error   — response JSON could not be parsed
      stream_parse_error — SSE/stream parsing error
      client_resource_error — OS resource exhaustion (too many files, etc.)
      cancelled          — asyncio task cancelled
      unknown            — unclassified

    Windows error codes:
      WinError 10054 → connection_reset  (WSAECONNRESET, peer reset the connection)
      WinError 10060 → connect_timeout   (WSAETIMEDOUT, connection attempt timed out)
    """
    exc_str = str(exc)
    name = type(exc).__name__.lower()
    msg  = exc_str.lower()
    # Windows-specific error codes (must check before generic patterns)
    if "WinError 10054" in exc_str or "winerror 10054" in exc_str:
        return "connection_reset"
    if "WinError 10060" in exc_str or "winerror 10060" in exc_str:
        return "connect_timeout"
    # Generic patterns (order matters: more specific checks first)
    if "connecttimeout" in name or "connect_timeout" in msg or "wsaetimedout" in msg:
        return "connect_timeout"
    if "timeout" in name or "timeout" in msg:
        return "timeout"
    if "reset" in msg or "connection_reset" in msg:
        return "connection_reset"
    # disconnect must be checked before connect (disconnect contains "connect")
    if "disconnect" in name or "disconnect" in msg:
        return "read_error"
    if "connector" in name or ("connect" in msg and "disconnect" not in msg):
        return "connect_error"
    if "read" in name:
        return "read_error"
    if "oserror" in name or "connectionerror" in name:
        return "client_resource_error"
    if "json" in name or "json" in msg:
        return "json_parse_error"
    if "cancel" in name or "cancel" in msg:
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
