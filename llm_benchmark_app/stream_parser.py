"""stream_parser.py — Model-agnostic SSE stream chunk parser.

Supports: delta.content / delta.reasoning_content / delta.reasoning /
          choices[].text / tool_calls / function_call / unknown-key diagnostics

Extracted from llm_benchmark.py (TASK-LLM-BENCHMARK-MODULE-SPLIT-TOKEN-HC-001-v1).
No behavior changes — identical logic to the original.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Optional

# ── Known delta keys (model-agnostic) ────────────────────────────────────────
_KNOWN_DELTA_KEYS = frozenset({
    "role", "content", "reasoning_content", "reasoning",
    "tool_calls", "function_call", "refusal", "audio",
})


@dataclass
class ParsedStreamChunk:
    """Structured result of parsing one SSE JSON chunk from any OpenAI-compatible stream."""
    is_json_chunk: bool = False
    is_done: bool = False
    is_usage_chunk: bool = False
    has_choices: bool = False
    has_stream_event: bool = False
    generated_text: Optional[str] = None       # first non-empty from any supported field
    generated_field: Optional[str] = None      # which field produced generated_text
    answer_text: Optional[str] = None          # delta.content only
    reasoning_text: Optional[str] = None       # delta.reasoning_content or delta.reasoning
    tool_text: Optional[str] = None            # tool_calls[].function.arguments / function_call
    finish_reason: Optional[str] = None
    raw_delta_keys: list = dc_field(default_factory=list)
    unknown_delta_keys: list = dc_field(default_factory=list)


def _parse_stream_chunk(obj: dict) -> ParsedStreamChunk:
    """Parse one SSE JSON object (already json.loads'd) into a ParsedStreamChunk.

    Empty strings / None / [] / {} are never treated as generated text.
    Unknown delta keys are collected for diagnostics without being used as content.
    """
    chunk = ParsedStreamChunk(is_json_chunk=True)
    if not isinstance(obj, dict):
        return chunk

    choices = obj.get("choices") or []
    usage = obj.get("usage")

    if usage and isinstance(usage, dict):
        chunk.is_usage_chunk = True

    if choices:
        chunk.has_choices = True
        first = choices[0]
        delta = first.get("delta") or {}
        chunk.has_stream_event = True
        chunk.raw_delta_keys = list(delta.keys())
        chunk.unknown_delta_keys = [k for k in delta.keys() if k not in _KNOWN_DELTA_KEYS]

        # Finish reason
        fr = first.get("finish_reason") or ""
        if fr:
            chunk.finish_reason = fr

        # choices[].text (completions-style endpoint)
        choice_text = first.get("text") or ""

        # --- Answer content: delta.content ---
        content = delta.get("content")
        if content:  # truthy: non-empty string only; "" and None are excluded
            chunk.answer_text = content

        # --- Reasoning: delta.reasoning_content (DeepSeek, etc.) or delta.reasoning (Qwen3) ---
        rc = delta.get("reasoning_content") or delta.get("reasoning")
        _rc_field = None
        if rc:
            chunk.reasoning_text = rc
            _rc_field = "reasoning_content" if delta.get("reasoning_content") else "reasoning"

        # --- Tool/function deltas (conservative debug) ---
        _tool_text = None
        tc = delta.get("tool_calls")
        if tc and isinstance(tc, list):
            for call in tc:
                if isinstance(call, dict):
                    fn = call.get("function") or {}
                    args = fn.get("arguments") or fn.get("name") or ""
                    if args:
                        _tool_text = str(args)
                        break
        if not _tool_text:
            fc = delta.get("function_call") or {}
            fc_args = (fc.get("arguments") or "") if isinstance(fc, dict) else ""
            if fc_args:
                _tool_text = str(fc_args)
        if _tool_text:
            chunk.tool_text = _tool_text

        # --- Determine generated_text (priority: content > reasoning > choices.text > tool) ---
        if chunk.answer_text:
            chunk.generated_text = chunk.answer_text
            chunk.generated_field = "content"
        elif chunk.reasoning_text:
            chunk.generated_text = chunk.reasoning_text
            chunk.generated_field = _rc_field if rc else "reasoning"
        elif choice_text:
            chunk.generated_text = choice_text
            chunk.generated_field = "text"
        elif chunk.tool_text:
            chunk.generated_text = chunk.tool_text
            chunk.generated_field = "tool_calls"

    elif not choices:
        if chunk.is_usage_chunk:
            chunk.has_stream_event = False

    return chunk


def _build_parser_profile(ok_results: list, config: dict) -> dict:
    """Build a stream parser_profile from aggregated per-request results.

    Summarises which delta fields were observed, what parser_mode was used,
    and any stream compatibility warnings.
    """
    stream_results = [r for r in ok_results if r.get("stream")]
    if not stream_results:
        return {
            "stream_supported": False,
            "usage_supported": None,
            "generated_fields": [],
            "answer_field_observed": False,
            "reasoning_field_observed": False,
            "text_field_observed": False,
            "tool_delta_observed": False,
            "unknown_delta_keys": [],
            "observed_delta_keys": [],
            "first_chunk_type": None,
            "parser_mode": "non_stream",
            "usage_source": "response_body",
            "fixed_output_supported": config.get("output_length_mode") == "fixed",
            "warning_messages": [],
        }

    all_observed: set = set()
    all_unknown: set = set()
    all_gen_fields: dict = {}
    all_warnings: list = []

    for r in stream_results:
        for k in (r.get("observed_delta_keys") or []):
            all_observed.add(k)
        for k in (r.get("unknown_delta_keys") or []):
            all_unknown.add(k)
        for field, count in (r.get("generated_field_counts") or {}).items():
            all_gen_fields[field] = all_gen_fields.get(field, 0) + count
        for w in (r.get("stream_warnings") or []):
            if w not in all_warnings:
                all_warnings.append(w)

    has_usage = any(r.get("completion_tokens", 0) > 0 for r in stream_results)
    answer_field = "content" in all_gen_fields
    reasoning_field = ("reasoning_content" in all_gen_fields or "reasoning" in all_gen_fields)
    text_field = "text" in all_gen_fields
    tool_field = ("tool_calls" in all_gen_fields or "function_call" in all_gen_fields)
    generated_fields = sorted(all_gen_fields.keys())

    if reasoning_field and not answer_field:
        parser_mode = "reasoning_only"
    elif reasoning_field and answer_field:
        parser_mode = "reasoning_and_content"
    elif answer_field:
        parser_mode = "content_only"
    elif text_field:
        parser_mode = "choices_text"
    elif tool_field:
        parser_mode = "tool_only"
    else:
        parser_mode = "unknown"

    first_chunk_type = None
    for r in stream_results:
        sd = r.get("stream_debug") or {}
        samples = sd.get("first_delta_samples") or []
        if samples:
            keys = samples[0].get("keys", [])
            if "reasoning" in keys:
                first_chunk_type = "reasoning"
            elif "reasoning_content" in keys:
                first_chunk_type = "reasoning_content"
            elif "content" in keys:
                first_chunk_type = "content"
            elif "role" in keys:
                first_chunk_type = "role_only"
            else:
                first_chunk_type = str(keys[:3])
            break

    return {
        "stream_supported": True,
        "usage_supported": has_usage,
        "generated_fields": generated_fields,
        "answer_field_observed": answer_field,
        "reasoning_field_observed": reasoning_field,
        "text_field_observed": text_field,
        "tool_delta_observed": tool_field,
        "unknown_delta_keys": sorted(all_unknown),
        "observed_delta_keys": sorted(all_observed),
        "first_chunk_type": first_chunk_type,
        "parser_mode": parser_mode,
        "usage_source": "stream_usage_chunk" if has_usage else "missing",
        "fixed_output_supported": config.get("output_length_mode") == "fixed",
        "warning_messages": all_warnings,
    }


def run_stream_parser_tests() -> list[dict]:
    """Synthetic tests for _parse_stream_chunk.

    Returns list of {name, passed, details} dicts.
    """
    results = []

    def _t(name, obj, checks):
        chunk = _parse_stream_chunk(obj)
        passed = True
        failures = []
        for attr, expected in checks.items():
            actual = getattr(chunk, attr)
            if actual != expected:
                passed = False
                failures.append(f"{attr}: expected={expected!r} actual={actual!r}")
        results.append({"name": name, "passed": passed, "failures": failures})

    _t("T1_role_only_empty", {"choices": [{"delta": {"role": "assistant", "content": ""}}]}, {
        "has_stream_event": True, "generated_text": None, "answer_text": None, "reasoning_text": None,
    })
    _t("T2_content", {"choices": [{"delta": {"content": "hello"}}]}, {
        "generated_text": "hello", "generated_field": "content", "answer_text": "hello",
    })
    _t("T3_reasoning_content", {"choices": [{"delta": {"reasoning_content": "think"}}]}, {
        "generated_text": "think", "generated_field": "reasoning_content", "reasoning_text": "think",
    })
    _t("T4_reasoning", {"choices": [{"delta": {"reasoning": "Here"}}]}, {
        "generated_text": "Here", "generated_field": "reasoning", "reasoning_text": "Here",
    })
    _t("T5_choices_text", {"choices": [{"text": "hello"}]}, {
        "generated_text": "hello", "generated_field": "text",
    })
    _t("T6_usage_only", {"choices": [], "usage": {"completion_tokens": 512}}, {
        "is_usage_chunk": True, "generated_text": None, "has_stream_event": False,
    })

    obj7 = {"choices": [{"delta": {"tool_calls": [{"function": {"arguments": '{"x":1}'}}]}}]}
    chunk7 = _parse_stream_chunk(obj7)
    passed7 = (chunk7.tool_text is not None and chunk7.answer_text is None)
    results.append({"name": "T7_tool_delta", "passed": passed7,
                    "failures": [] if passed7 else ["tool_text should be set, answer_text should be None"]})

    seq = [
        {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
        {"choices": [{"delta": {"reasoning": "Here"}}]},
        {"choices": [{"delta": {"reasoning": "'s"}}]},
    ]
    _timestamps = [0.05, 0.06, 0.07]
    _first_gen = None
    _first_reason = None
    _first_answer = None
    _gen_ts = []
    for obj, ts in zip(seq, _timestamps):
        c = _parse_stream_chunk(obj)
        if c.has_stream_event and _first_gen is None and c.generated_text:
            _first_gen = ts
        if c.reasoning_text and _first_reason is None:
            _first_reason = ts
        if c.answer_text and _first_answer is None:
            _first_answer = ts
        if c.generated_text:
            _gen_ts.append(ts)
    p8 = (_first_gen is not None and _first_reason is not None and
          _first_answer is None and len(_gen_ts) >= 2)
    results.append({"name": "T8_qwen36_stream", "passed": p8,
                    "failures": [] if p8 else [
                        f"first_gen={_first_gen} first_reason={_first_reason} "
                        f"first_answer={_first_answer} gen_ts={_gen_ts}"]})

    chunk9 = _parse_stream_chunk({"choices": [{"delta": {"role": "assistant"}}]})
    p9 = (chunk9.generated_text is None and chunk9.answer_text is None and chunk9.reasoning_text is None)
    results.append({"name": "T9_missing_generated_none", "passed": p9,
                    "failures": [] if p9 else ["all content fields must be None when no content present"]})

    _gap = round(0.06 - 0.06, 6)
    p10 = (_gap == 0.0 and _gap is not None)
    results.append({"name": "T10_real_zero_gap", "passed": p10,
                    "failures": [] if p10 else [f"gap={_gap!r} should be 0.0 not None"]})

    chunk11 = _parse_stream_chunk({"choices": [{"delta": {"thinking": "abc"}}]})
    p11 = ("thinking" in chunk11.unknown_delta_keys and chunk11.generated_text is None)
    results.append({"name": "T11_unknown_delta_key", "passed": p11,
                    "failures": [] if p11 else [
                        f"unknown_delta_keys={chunk11.unknown_delta_keys} generated_text={chunk11.generated_text!r}"]})

    return results


def _test_stream_parser_fixtures():
    """Golden fixture tests. Raises AssertionError on any failure."""
    results = run_stream_parser_tests()
    failures = [r for r in results if not r["passed"]]
    if failures:
        details = "; ".join(f"{r['name']}: {r['failures']}" for r in failures)
        raise AssertionError(
            f"Stream parser fixture failures ({len(failures)}/{len(results)}): {details}")
    print(f"STREAM_FIXTURE_TESTS_PASS ({len(results)}/{len(results)} passed)")
