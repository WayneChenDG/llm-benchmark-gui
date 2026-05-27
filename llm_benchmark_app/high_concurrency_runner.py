"""high_concurrency_runner.py — HC async runner helpers.

TASK-BENCHMARK-RESULTS-HISTORY-REPORT-V2-001
Extracted from llm_benchmark.py (TASK-LLM-BENCHMARK-MODULE-SPLIT-TOKEN-HC-001-v1).

Updated in TASK-LLM-BENCHMARK-TOKEN-CALIBRATION-HIGH-CONCURRENCY-001-v1:
  - Added connection_reset to failure taxonomy
  - Windows WinError 10054 → connection_reset (connection reset by peer)
  - Windows WinError 10060 → connect_timeout (WSAETIMEDOUT, connection attempt timed out)

Updated in TASK-LLM-BENCHMARK-TOKEN-CALIBRATION-HC-RUNNER-001-v1:
  - Taxonomy extended: connect_timeout as separate category from timeout
  - WinError 10060 corrected to connect_timeout (was connect_error)

Updated in TASK-LLM-BENCHMARK-HC-RUNNER-EVENT-LOOP-CLOSE-FIX-001-v1:
  - Add run_async_clean() — safe async entry point with full event-loop lifecycle
  - Cancel pending tasks, drain asyncgens, shutdown executor before loop.close()
  - Compatible with Python 3.10 / 3.11 / 3.12 / 3.14

Updated in TASK-LLM-BENCHMARK-HC-RUNNER-WINDOWS-SELECT-FD-FIX-001-v1:
  - Remove WindowsSelectorEventLoopPolicy — select() cannot handle C512+ sockets
  - run_async_clean() accepts max_concurrency kwarg
  - client_resource_error taxonomy: "too many file descriptors in select()"

Updated in TASK-LLM-BENCHMARK-HC-RUNNER-WINDOWS-EVENTLOOP-REGRESSION-FIX-002-v1:
  - Rename loop factory: old name → _new_hc_event_loop
  - Use asyncio.new_event_loop() uniformly (respects Python default policy)
  - Python 3.8+ default on Windows IS ProactorEventLoop — no explicit call needed
  - Post-creation SelectorEventLoop guard on Windows C512+: detect-and-raise
  - Never construct ProactorEventLoop directly (policy-unaware, fragile)
"""
from __future__ import annotations


def _new_hc_event_loop(max_concurrency: int):
    """Create an event loop for *max_concurrency* concurrent sockets.

    Design
    ------
    We call ``asyncio.new_event_loop()`` unconditionally and let Python's
    current event-loop *policy* decide the concrete implementation.

    **Why not instantiate ProactorEventLoop directly?**

    Calling the class directly bypasses the registered policy.  It also
    breaks if the class requires arguments or is subclassed by a third-party
    framework.  More importantly, it does not match the historical behaviour
    that made Windows C512/C1024 work: Python 3.8+ registers
    ``DefaultEventLoopPolicy`` whose ``new_event_loop()`` factory returns a
    ``ProactorEventLoop`` on Windows.  Calling ``asyncio.new_event_loop()``
    reproduces that factory call exactly.

    **WindowsSelectorEventLoopPolicy is explicitly forbidden here.**

    ``WindowsSelectorEventLoopPolicy.new_event_loop()`` returns a
    ``SelectorEventLoop``, which uses the Win32 ``select()`` syscall.
    That syscall is hard-limited to FD_SETSIZE=512 file descriptors, so
    any C512+ sweep immediately raises::

        OSError: too many file descriptors in select()

    We perform a *post-creation* type check: if the returned loop happens to
    be a SelectorEventLoop on Windows when max_concurrency >= 512, we raise
    with actionable guidance rather than crashing mid-sweep.

    Platform summary
    ----------------
    * Windows (any concurrency) → ``asyncio.new_event_loop()``
      Produces ``ProactorEventLoop`` (IOCP) by default in Python 3.8+.
      Guard raises ``RuntimeError`` if a SelectorEventLoop sneaks in for
      concurrency >= 512.
    * Linux / macOS             → ``asyncio.new_event_loop()``
      Produces ``SelectorEventLoop`` backed by epoll/kqueue — no FD ceiling.
    """
    import asyncio
    import sys

    loop = asyncio.new_event_loop()

    # Post-creation guard: detect a SelectorEventLoop on Windows C512+.
    # This happens when the process-level policy was overridden to
    # WindowsSelectorEventLoopPolicy before run_async_clean() was called.
    if sys.platform.startswith("win") and max_concurrency >= 512:
        loop_name = type(loop).__name__
        if "Selector" in loop_name:
            loop.close()
            raise RuntimeError(
                f"Windows C512+ high-concurrency sweep cannot run on "
                f"{loop_name} / select() — select() is limited to "
                f"FD_SETSIZE=512 file descriptors and will raise "
                f"\"too many file descriptors in select()\" immediately.\n\n"
                f"Cause: the process event-loop policy was set to "
                f"WindowsSelectorEventLoopPolicy (or equivalent) before the "
                f"HC runner started.\n\n"
                f"Fix: do not set WindowsSelectorEventLoopPolicy anywhere in "
                f"this process, or run C512+ sweep on Linux."
            )

    return loop


def run_async_clean(coro, *, max_concurrency: int = 1):
    """Run *coro* in a fresh event loop with full, safe cleanup on exit.

    Replaces bare ``asyncio.run()`` or ``loop.run_until_complete()`` /
    ``loop.close()`` patterns that leave dangling async generators or pending
    tasks, causing ``RuntimeError: Event loop is closed`` on Windows and
    Python 3.14.

    The loop is created by :func:`_new_hc_event_loop` which calls
    ``asyncio.new_event_loop()`` and relies on the Python default policy.
    On Windows Python 3.8+ the default policy returns ``ProactorEventLoop``
    (IOCP), which handles thousands of simultaneous sockets without the
    ``select()`` FD_SETSIZE=512 ceiling that causes
    ``too many file descriptors in select()`` at C512+.

    Shutdown sequence (mirrors ``asyncio.run()`` internals):
      1. Cancel all pending tasks in the loop
      2. Await the cancelled tasks (return_exceptions=True)
      3. ``loop.shutdown_asyncgens()`` — closes async-generator finalizers
         that would otherwise call ``loop.call_soon`` on a closed loop
      4. ``loop.shutdown_default_executor()`` — drains the thread pool
      5. ``asyncio.set_event_loop(None)`` then ``loop.close()``

    Parameters
    ----------
    coro:
        The top-level coroutine to run.
    max_concurrency:
        Maximum number of concurrent sockets/requests.  Forwarded to
        :func:`_new_hc_event_loop` for the Windows SelectorEventLoop guard.

    Compatible with Python 3.10 / 3.11 / 3.12 / 3.14.
    """
    import asyncio

    loop = _new_hc_event_loop(max_concurrency)
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            # ── Step 1 & 2: cancel and drain every pending task ──────────
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True))
            # ── Step 3: close async generators ──────────────────────────
            # Prevents "RuntimeError: Event loop is closed" from generator
            # __del__ or __aexit__ that call loop.call_soon/call_soon_threadsafe
            loop.run_until_complete(loop.shutdown_asyncgens())
            # ── Step 4: drain default executor (thread pool) ─────────────
            try:
                loop.run_until_complete(loop.shutdown_default_executor())
            except Exception:
                pass  # not available on older Pythons; safe to skip
        finally:
            # ── Step 5: detach and close ─────────────────────────────────
            asyncio.set_event_loop(None)
            loop.close()


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
      client_resource_error — OS resource exhaustion (too many files / too many file
                              descriptors in select(), EMFILE, etc.)
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
    # OS resource exhaustion — too many open files / too many file descriptors in select()
    if ("too many file descriptors" in msg or "too many open files" in msg
            or "emfile" in msg or "[errno 24]" in msg):
        return "client_resource_error"
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
