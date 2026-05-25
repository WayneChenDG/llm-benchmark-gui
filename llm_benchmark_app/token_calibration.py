"""token_calibration.py — Token calibration utilities.

Implements:
  - TokenCalibrationResult dataclass
  - calibrate_by_server_usage()    — binary-search prompt length via server usage API
  - calibrate_by_local_tokenizer() — estimate via transformers (lazy import)
  - generate_same_length_variants() — N prompt variants of equal token length
  - generate_synthetic_prompt()     — deterministic synthetic filler prompt

TASK-LLM-BENCHMARK-TOKEN-CALIBRATION-HC-RUNNER-001-v1
"""
from __future__ import annotations

import urllib.request
import urllib.error
import json
import socket
from dataclasses import dataclass, field as dc_field
from typing import Optional


# ── Deterministic filler text (stable, reproducible, neutral) ─────────────────
_FILLER_TEXT = (
    "补充背景：本测试采用统一输入长度和统一输出长度，以减少不同请求之间的随机波动，"
    "保证不同硬件、不同后端和不同模型之间的横向对比更公平。"
    "测试提示词经过 Token 校准，实际输入 Token 数量接近目标值。"
    "如有需要，可通过调整提示词长度进一步对齐服务端实际 Token 计数。"
    "本基准测试工具支持服务端 usage 校准和本地 tokenizer 估算两种模式，"
    "推荐使用服务端 usage 校准以获得最准确的结果。"
    "测试参数：并发数、请求总数、最大生成 Token 数、温度参数均可在参数设置中配置。"
)

# Same-Length Variants: short neutral clauses for deterministic variation
_VARIANT_CLAUSES = [
    "（请求编号：{seq_id}）",
    "（场景编号：{seq_id}，本次测试独立计数）",
    "（本条请求为第 {seq_id} 号，保持统一输入规格）",
    "（并发测试场景 {seq_id}，Token 长度保持一致）",
    "（基准测试请求 {seq_id}，统一输出规格）",
    "（测试序列 {seq_id}，控制变量：输入输出长度固定）",
    "（吞吐量测试，请求 ID={seq_id}，保持一致 Token 长度）",
    "（压力测试请求 #{seq_id}，不含随机性）",
]


@dataclass
class TokenCalibrationResult:
    """Result of a token calibration attempt."""
    target_input_tokens: int
    actual_prompt_tokens: Optional[int]
    target_output_tokens: int
    system_prompt: str
    user_prompt: str
    calibration_method: str        # "server_usage" | "local_tokenizer" | "manual"
    prompt_mode: str               # "fixed" | "variants" | "synthetic"
    tolerance: int
    passed: bool
    warnings: list = dc_field(default_factory=list)
    attempts: int = 0


def _http_post_json(api_url: str, api_key: str, body: dict,
                    timeout: float = 60.0) -> Optional[dict]:
    """Minimal urllib POST returning parsed JSON or None."""
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(api_url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def calibrate_by_server_usage(
    api_url: str,
    model: str,
    system_prompt: str,
    base_user_prompt: str,
    target_input_tokens: int,
    target_output_tokens: int = 2048,
    tolerance: int = 8,
    max_attempts: int = 12,
    timeout: float = 120.0,
    api_key: str = "",
) -> TokenCalibrationResult:
    """Binary-search prompt length to match target_input_tokens using server usage.

    Sends lightweight requests (max_tokens=1, stream=False) and reads
    usage.prompt_tokens from the response. Uses deterministic filler text.

    Args:
        api_url:             Full chat completions endpoint URL
        model:               Model name
        system_prompt:       System message (included in token count)
        base_user_prompt:    Base user message text
        target_input_tokens: Desired prompt token count
        target_output_tokens: Max tokens for verification (not used during calibration)
        tolerance:           Acceptable deviation in tokens (±)
        max_attempts:        Maximum binary-search iterations
        timeout:             HTTP request timeout in seconds
        api_key:             Bearer token for Authorization header

    Returns:
        TokenCalibrationResult
    """
    # Normalize API URL to chat completions endpoint
    url = api_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url = url + "/chat/completions"

    unit = base_user_prompt.strip() or "请介绍人工智能的基本原理与应用场景。"
    filler = _FILLER_TEXT

    # Helper: build messages and send lightweight probe
    def _probe(user_text: str) -> Optional[int]:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_text})
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": 1,
            "temperature": 0.0,
            "stream": False,
        }
        resp = _http_post_json(url, api_key, body, timeout)
        if resp is None:
            return None
        usage = resp.get("usage") or {}
        pt = usage.get("prompt_tokens")
        return int(pt) if pt is not None else None

    # Get baseline token count for base prompt alone
    baseline = _probe(unit)
    if baseline is None:
        return TokenCalibrationResult(
            target_input_tokens=target_input_tokens,
            actual_prompt_tokens=None,
            target_output_tokens=target_output_tokens,
            system_prompt=system_prompt,
            user_prompt=unit,
            calibration_method="server_usage",
            prompt_mode="fixed",
            tolerance=tolerance,
            passed=False,
            warnings=["服务端未返回 usage.prompt_tokens，无法使用 server usage 自动校准。"],
            attempts=1,
        )

    if baseline <= 0:
        return TokenCalibrationResult(
            target_input_tokens=target_input_tokens,
            actual_prompt_tokens=0,
            target_output_tokens=target_output_tokens,
            system_prompt=system_prompt,
            user_prompt=unit,
            calibration_method="server_usage",
            prompt_mode="fixed",
            tolerance=tolerance,
            passed=False,
            warnings=["服务端返回 prompt_tokens=0，无法校准。"],
            attempts=1,
        )

    need_extra = target_input_tokens - baseline
    current_text = unit
    current_tokens = baseline
    attempts = 1

    if need_extra > 0:
        # Estimate chars needed from filler
        chars_per_tok = len(filler) / max(len(filler.encode("utf-8")) // 3, 1)
        filler_repeat = (filler + "\n") * 10
        lo, hi = 0, len(filler_repeat) * 4

        for _i in range(max_attempts):
            mid = (lo + hi) // 2
            candidate_extra = filler_repeat[:max(mid, 1)]
            candidate = unit + "\n" + candidate_extra
            got = _probe(candidate)
            attempts += 1
            if got is None:
                break
            current_text = candidate
            current_tokens = got
            diff = got - target_input_tokens
            if abs(diff) <= tolerance:
                break
            if diff < 0:
                lo = mid + 1
            else:
                hi = max(mid - 1, 0)
    elif need_extra < 0:
        # Base prompt is already too long — trim unit chars
        lo, hi = 0, len(unit) - 1
        for _i in range(max_attempts):
            mid = (lo + hi) // 2
            candidate = unit[:max(mid, 1)]
            got = _probe(candidate)
            attempts += 1
            if got is None:
                break
            current_text = candidate
            current_tokens = got
            diff = got - target_input_tokens
            if abs(diff) <= tolerance:
                break
            if diff < 0:
                lo = mid + 1
            else:
                hi = max(mid - 1, 0)

    passed = abs(current_tokens - target_input_tokens) <= tolerance
    warnings = []
    if not passed:
        warnings.append(
            f"校准未收敛: actual={current_tokens} target={target_input_tokens} "
            f"diff={current_tokens - target_input_tokens:+d} tolerance=±{tolerance}"
        )

    return TokenCalibrationResult(
        target_input_tokens=target_input_tokens,
        actual_prompt_tokens=current_tokens,
        target_output_tokens=target_output_tokens,
        system_prompt=system_prompt,
        user_prompt=current_text,
        calibration_method="server_usage",
        prompt_mode="fixed",
        tolerance=tolerance,
        passed=passed,
        warnings=warnings,
        attempts=attempts,
    )


def calibrate_by_local_tokenizer(
    model_path_or_tokenizer_path: str,
    system_prompt: str,
    base_user_prompt: str,
    target_input_tokens: int,
    target_output_tokens: int = 2048,
    tolerance: int = 8,
) -> TokenCalibrationResult:
    """Estimate prompt length using a local tokenizer (lazy import transformers).

    Args:
        model_path_or_tokenizer_path: HuggingFace model/tokenizer path or name
        system_prompt:                System message
        base_user_prompt:             Base user message
        target_input_tokens:          Desired prompt token count
        target_output_tokens:         Target output token count (for result record)
        tolerance:                    Acceptable deviation in tokens (±)

    Returns:
        TokenCalibrationResult with calibration_method="local_tokenizer"
    """
    unit = base_user_prompt.strip() or "请介绍人工智能的基本原理与应用场景。"
    filler = _FILLER_TEXT
    warnings = []

    try:
        from transformers import AutoTokenizer  # lazy import
    except ImportError:
        return TokenCalibrationResult(
            target_input_tokens=target_input_tokens,
            actual_prompt_tokens=None,
            target_output_tokens=target_output_tokens,
            system_prompt=system_prompt,
            user_prompt=unit,
            calibration_method="local_tokenizer",
            prompt_mode="fixed",
            tolerance=tolerance,
            passed=False,
            warnings=["transformers 未安装。请 pip install transformers 后重试。"],
            attempts=0,
        )

    try:
        tok = AutoTokenizer.from_pretrained(
            model_path_or_tokenizer_path, trust_remote_code=True)
    except Exception as e:
        return TokenCalibrationResult(
            target_input_tokens=target_input_tokens,
            actual_prompt_tokens=None,
            target_output_tokens=target_output_tokens,
            system_prompt=system_prompt,
            user_prompt=unit,
            calibration_method="local_tokenizer",
            prompt_mode="fixed",
            tolerance=tolerance,
            passed=False,
            warnings=[f"加载 tokenizer 失败: {e}"],
            attempts=0,
        )

    has_chat_template = getattr(tok, "chat_template", None) is not None

    def _count(text: str) -> int:
        msgs = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        msgs.append({"role": "user", "content": text})
        try:
            if has_chat_template:
                ids = tok.apply_chat_template(msgs, add_generation_prompt=True,
                                              tokenize=True)
                return len(ids)
        except Exception:
            pass
        return len(tok.encode(text))

    if not has_chat_template:
        warnings.append(
            "该 tokenizer 未定义 chat_template，Token 数量可能与服务端有出入。"
            "建议事后用「验证 Token 用量」确认。"
        )

    baseline = _count(unit)
    need_extra = target_input_tokens - baseline
    current_text = unit
    current_tokens = baseline
    attempts = 1

    filler_repeat = (filler + "\n") * 10

    if need_extra > 0:
        lo, hi = 0, len(filler_repeat) * 4
        for _i in range(20):
            mid = (lo + hi) // 2
            candidate = unit + "\n" + filler_repeat[:max(mid, 1)]
            got = _count(candidate)
            attempts += 1
            current_text = candidate
            current_tokens = got
            diff = got - target_input_tokens
            if abs(diff) <= tolerance:
                break
            if diff < 0:
                lo = mid + 1
            else:
                hi = max(mid - 1, 0)
    elif need_extra < 0:
        lo, hi = 0, len(unit) - 1
        for _i in range(20):
            mid = (lo + hi) // 2
            candidate = unit[:max(mid, 1)]
            got = _count(candidate)
            attempts += 1
            current_text = candidate
            current_tokens = got
            diff = got - target_input_tokens
            if abs(diff) <= tolerance:
                break
            if diff < 0:
                lo = mid + 1
            else:
                hi = max(mid - 1, 0)

    passed = abs(current_tokens - target_input_tokens) <= tolerance
    if not passed:
        warnings.append(
            f"校准未收敛: actual={current_tokens} target={target_input_tokens} "
            f"diff={current_tokens - target_input_tokens:+d}"
        )

    return TokenCalibrationResult(
        target_input_tokens=target_input_tokens,
        actual_prompt_tokens=current_tokens,
        target_output_tokens=target_output_tokens,
        system_prompt=system_prompt,
        user_prompt=current_text,
        calibration_method="local_tokenizer",
        prompt_mode="fixed",
        tolerance=tolerance,
        passed=passed,
        warnings=warnings,
        attempts=attempts,
    )


def generate_same_length_variants(
    base_prompt: str,
    num_variants: int = 8,
    seq_id_start: int = 1,
) -> list[str]:
    """Generate N prompt variants with approximately the same length as base_prompt.

    Each variant includes a short deterministic clause to break prefix-cache
    while keeping total character length close to original.

    The clause replaces a matched-length suffix to keep overall length stable.

    Args:
        base_prompt:   Calibrated base user prompt
        num_variants:  Number of variants to generate
        seq_id_start:  Starting sequence ID

    Returns:
        List of prompt variant strings
    """
    variants = []
    for i in range(num_variants):
        seq_id = seq_id_start + i
        clause_template = _VARIANT_CLAUSES[i % len(_VARIANT_CLAUSES)]
        clause = clause_template.format(seq_id=seq_id)
        # Replace tail of base prompt with the clause (same total length ±clause)
        # This keeps token count drift small while adding uniqueness
        trim_len = max(0, len(base_prompt) - len(clause))
        variant = base_prompt[:trim_len] + clause
        variants.append(variant)
    return variants


def generate_synthetic_prompt(
    target_input_tokens: int,
    system_prompt: str = "",
    seed: int = 42,
) -> str:
    """Generate a deterministic synthetic prompt targeting approximately target_input_tokens.

    Uses a fixed vocabulary of neutral technical sentences repeated/trimmed to length.

    Args:
        target_input_tokens: Approximate target token count
        system_prompt:       System prompt (used to estimate overhead)
        seed:                Deterministic seed (unused in v1, kept for interface stability)

    Returns:
        Synthetic user prompt string
    """
    # Estimate system prompt overhead (~1.3 chars/token for CJK, rough)
    sys_overhead = len(system_prompt) // 3 if system_prompt else 0
    user_target_chars = max(1, (target_input_tokens - sys_overhead) * 3)

    sentences = [
        "本测试采用固定长度输入输出，确保对比公平。",
        "大型语言模型的推理性能受到并发请求数、批次大小和硬件规格的影响。",
        "吞吐量通常以每秒输出 Token 数（TPS）衡量，延迟以毫秒为单位。",
        "首包延迟（TTFT）是从请求发送到收到第一个生成 Token 的时间。",
        "每 Token 输出时间（TPOT）衡量生成每个 Token 的平均耗时。",
        "高并发压测旨在评估服务端在极限负载下的稳定性和吞吐能力。",
        "本基准工具支持流式与非流式模式，覆盖主流 OpenAI 兼容接口。",
        "请求总数与并发数的比例决定了队列深度和排队延迟的分布。",
    ]
    block = " ".join(sentences)
    repeated = (block + " ") * (user_target_chars // len(block) + 2)
    return repeated[:user_target_chars].strip()
