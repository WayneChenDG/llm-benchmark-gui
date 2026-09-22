#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WCAG 2.1 contrast audit + design-token discipline scanner for the JISUMAN
LLM Benchmark GUI.

Reads the colour tokens straight out of the ``C_STYLE = {...}`` literal in
``llm_benchmark.py`` using a text-level brace-matching parser, so the module is
never imported (importing it boots Tk).

Outputs a markdown table of *intent pairs* (foreground token, background token,
purpose, WCAG threshold) with the measured contrast ratio and PASS/FAIL, plus a
statistics block and a "hard-coded colour" scan that proves (or disproves)
design-token discipline.

Exit codes:
    0  every pair PASSes
    1  parse failure (C_STYLE not found / unreadable / no colour tokens)
    2  at least one pair FAILs

Stdlib only. Usage:
    python3 scripts/uiux_contrast_audit.py [--json] [--source PATH] [--min-passes N]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

DEFAULT_SOURCE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "llm_benchmark_app", "ui_theme.py",
)
# 令牌权威在 ui_theme.py（TOKENS）；硬编码色值纪律扫描针对主程序文件
DEFAULT_SCAN_SOURCE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "llm_benchmark.py",
)

EXIT_OK = 0
EXIT_PARSE_ERROR = 1
EXIT_CONTRAST_FAIL = 2

# WCAG 2.1 thresholds
THRESH_TEXT = 4.5      # normal body text (SC 1.4.3)
THRESH_LARGE_NONTEXT = 3.0   # large text (>=18pt/14pt bold) and non-text UI (SC 1.4.11)

HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}(?![0-9A-Fa-f])")
TOKEN_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*"(#[0-9A-Fa-f]{6})"')
C_STYLE_START_RE = re.compile(r"^TOKENS(?::[^=]*)?\s*=\s*\{", re.MULTILINE)


# ───────────────────────────── parsing ─────────────────────────────

class ParseError(RuntimeError):
    pass


def read_source(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise ParseError("cannot read source file %r: %s" % (path, exc)) from exc


def find_c_style_span(text: str) -> tuple[int, int]:
    """Return (start_offset, end_offset_exclusive) of the C_STYLE dict literal."""
    m = C_STYLE_START_RE.search(text)
    if not m:
        raise ParseError(
            "could not locate a top-level 'C_STYLE = {' assignment in the source "
            "(the token palette has been moved or renamed)."
        )
    open_idx = text.index("{", m.start())
    depth = 0
    in_str = False
    quote = ""
    escaped = False
    i = open_idx
    while i < len(text):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_str = False
            i += 1
            continue
        if ch in "\"'":
            in_str = True
            quote = ch
            i += 1
            continue
        if ch == "#":  # comment to end of line
            nl = text.find("\n", i)
            i = len(text) if nl == -1 else nl
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return open_idx, i + 1
        i += 1
    raise ParseError("unbalanced braces while scanning the C_STYLE literal.")


def parse_tokens(block: str) -> dict[str, str]:
    tokens = {key: val.upper() for key, val in TOKEN_RE.findall(block)}
    if not tokens:
        raise ParseError(
            "found the C_STYLE block but extracted zero '\"name\": \"#RRGGBB\"' "
            "colour tokens from it — palette format changed?"
        )
    return tokens


# ──────────────────────────── colour maths ────────────────────────────

def _srgb_channel_to_linear(value: int) -> float:
    c = value / 255.0
    if c <= 0.03928:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    h = hex_str.lstrip("#")
    if len(h) != 6 or not re.fullmatch(r"[0-9A-Fa-f]{6}", h):
        raise ValueError("not a #RRGGBB colour: %r" % hex_str)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def relative_luminance(hex_str: str) -> float:
    r, g, b = hex_to_rgb(hex_str)
    return (
        0.2126 * _srgb_channel_to_linear(r)
        + 0.7152 * _srgb_channel_to_linear(g)
        + 0.0722 * _srgb_channel_to_linear(b)
    )


def contrast_ratio(fg: str, bg: str) -> float:
    l1 = relative_luminance(fg)
    l2 = relative_luminance(bg)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


# ─────────────────────────── intent pair matrix ───────────────────────────

class Pair:
    __slots__ = ("usage", "fg_key", "bg_key", "threshold", "fg", "bg", "ratio", "passed",
                 "gate", "note")

    def __init__(self, usage, fg_key, bg_key, threshold, gate=True, note=""):
        self.usage = usage
        self.fg_key = fg_key
        self.bg_key = bg_key
        self.threshold = threshold
        self.fg: str = ""
        self.bg: str = ""
        self.ratio = 0.0
        self.passed = False
        self.gate = gate          # True = 门禁项（影响退出码）；False = 信息项
        self.note = note


def build_pairs() -> list[Pair]:
    """Intent pairs: (fg token, bg token, usage, threshold).

    门禁（gate=True）：正文/标签/提示文字、语义文字、品牌强调文字、焦点环。
    信息（gate=False）：结构性细线/层次面（WCAG 1.4.11 针对"承载信息的边界"，
      细分割线与相邻表面层次属装饰，企业级 UI 惯例保留低对比），以及
      「白字压语义实心色」——该组合在本设计系统中被明令禁止，
      单独由 scan_forbidden_usage() 在源码中检查是否真的没被使用。
    """
    pairs: list[Pair] = []

    def add(usage, fg, bg, threshold, gate=True, note=""):
        pairs.append(Pair(usage, fg, bg, threshold, gate, note))

    surfaces_text = ("bg_main", "bg_card", "bg_header")

    # ── body / label / secondary / hint text on every reading surface ──
    for bg in surfaces_text:
        add("正文 text_primary / %s" % bg, "text_primary", bg, THRESH_TEXT)
        add("标签 text_secondary / %s" % bg, "text_secondary", bg, THRESH_TEXT)
        add("提示 text_muted / %s" % bg, "text_muted", bg, THRESH_TEXT)
    add("输入框正文 text_primary / bg_input", "text_primary", "bg_input", THRESH_TEXT)
    add("输入框提示 text_muted / bg_input", "text_muted", "bg_input", THRESH_TEXT)
    add("条纹行文字 text_primary / bg_stripe", "text_primary", "bg_stripe", THRESH_TEXT)
    add("条纹行次级 text_secondary / bg_stripe", "text_secondary", "bg_stripe", THRESH_TEXT)
    add("悬停行文字 text_primary / bg_hover", "text_primary", "bg_hover", THRESH_TEXT)
    add("悬停行次级 text_secondary / bg_hover", "text_secondary", "bg_hover", THRESH_TEXT)

    # ── semantic text on its own tinted surface ──
    for name in ("success", "warning", "error", "info"):
        add("%s_text / %s_bg" % (name, name),
            "%s_text" % name, "%s_bg" % name, THRESH_TEXT)

    # ── inverse (white) text on solid fills：仅品牌蓝允许（主按钮），语义实心色禁止 ──
    add("主按钮反白 text_inverse / accent", "text_inverse", "accent", THRESH_TEXT)
    for name in ("success", "error", "warning", "info"):
        add("禁用组合 白字压 %s 实心色" % name, "text_inverse", name, THRESH_TEXT,
            gate=False, note="设计系统禁止：语义实心色底一律改用 %s_text on %s_bg" % (name, name))

    # ── accent as foreground ──
    add("强调链接 accent / bg_card", "accent", "bg_card", THRESH_TEXT)
    add("强调链接 accent / bg_main", "accent", "bg_main", THRESH_TEXT)
    add("强调文字 accent / accent_light", "accent", "accent_light", THRESH_TEXT)
    add("强调文字 accent_text / bg_card", "accent_text", "bg_card", THRESH_TEXT)
    add("强调文字 accent_text / accent_light", "accent_text", "accent_light", THRESH_TEXT)
    add("品牌蓝浅底上焦 accent_text / accent_soft", "accent_text", "accent_soft", THRESH_TEXT)
    add("提示文字 text_muted / bg_inset", "text_muted", "bg_inset", THRESH_TEXT)
    add("标签 text_secondary / bg_inset", "text_secondary", "bg_inset", THRESH_TEXT)
    add("强调悬停 accent_hover / bg_card", "accent_hover", "bg_card", THRESH_TEXT)
    add("强调文字 accent / accent_soft", "accent", "accent_soft", THRESH_TEXT,
        gate=False, note="accent_soft 浅底上应使用 accent_text（已达标），不用 accent")

    # ── non-text: focus ring is the gated one (SC 1.4.11) ──
    add("焦点环 border_focus / bg_card", "border_focus", "bg_card", THRESH_LARGE_NONTEXT)
    add("焦点环 border_focus / bg_main", "border_focus", "bg_main", THRESH_LARGE_NONTEXT)
    # 结构性细线 / 表面层次：装饰性，不承载信息，仅记录数值
    for usage, fg, bg in (
        ("卡片边框 border / bg_card", "border", "bg_card"),
        ("卡片边框 border / bg_main", "border", "bg_main"),
        ("卡片边框 border / bg_hover", "border", "bg_hover"),
        ("分割线 border_light / bg_card", "border_light", "bg_card"),
        ("分割线 border_light / bg_main", "border_light", "bg_main"),
        ("分割线 border_light / bg_header", "border_light", "bg_header"),
        ("强调色块 accent / accent_light", "accent", "accent_light"),
        ("表面层次 bg_stripe / bg_main", "bg_stripe", "bg_main"),
        ("表面层次 bg_hover / bg_main", "bg_hover", "bg_main"),
        ("表面层次 bg_inset / bg_main", "bg_inset", "bg_main"),
    ):
        add(usage, fg, bg, THRESH_LARGE_NONTEXT, gate=False,
            note="结构性/装饰性，不承载信息")
    return pairs


FORBIDDEN_FG_RE = re.compile(r'(fg|foreground)\s*=\s*("white"|\'white\'|"#FFF"|"#FFFFFF")',
                             re.IGNORECASE)
SEMANTIC_FILL_RE = re.compile(
    r'(bg|background)\s*=\s*(?:"|\')?(TOKENS\[["\']|C_STYLE\[["\'])?'
    r'(success|warning|error|info|danger)(_solid|_fill)?(?:["\']|\]|[,)])',
    re.IGNORECASE)


def scan_forbidden_usage(paths: list[str]) -> list[tuple[str, int, str]]:
    """源码级检查「白字压语义实心色」是否真的没被使用（门禁项）。"""
    hits: list[tuple[str, int, str]] = []
    for path in paths:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                for i, line in enumerate(fh, 1):
                    if FORBIDDEN_FG_RE.search(line) and SEMANTIC_FILL_RE.search(line):
                        hits.append((path, i, line.strip()[:160]))
        except OSError:
            continue
    return hits


def evaluate(pairs: list[Pair], tokens: dict[str, str]) -> None:
    for p in pairs:
        for role, key in (("fg", p.fg_key), ("bg", p.bg_key)):
            if key not in tokens:
                raise ParseError(
                    "intent pair '%s' references token '%s', which is not defined in "
                    "C_STYLE. Defined tokens: %s"
                    % (p.usage, key, ", ".join(sorted(tokens)))
                )
        p.fg = tokens[p.fg_key]
        p.bg = tokens[p.bg_key]
        p.ratio = contrast_ratio(p.fg, p.bg)
        p.passed = p.ratio >= p.threshold


# ───────────────────────── token discipline scan ─────────────────────────

def scan_hardcoded(text: str, block_span: tuple[int, int]) -> dict:
    start, end = block_span
    all_matches = list(HEX_RE.finditer(text))
    inside = [m for m in all_matches if start <= m.start() < end]
    outside = [m for m in all_matches if not (start <= m.start() < end)]

    def counts(matches):
        table: dict[str, int] = {}
        for m in matches:
            value = m.group(0).upper()
            table[value] = table.get(value, 0) + 1
        return table

    all_counts = counts(all_matches)
    outside_counts = counts(outside)

    def line_of(offset):
        return text.count("\n", 0, offset) + 1

    outside_lines: dict[str, list[int]] = {}
    for m in outside:
        outside_lines.setdefault(m.group(0).upper(), []).append(line_of(m.start()))

    return {
        "total_hex_literals": len(all_matches),
        "inside_c_style": len(inside),
        "outside_c_style": len(outside),
        "outside_ratio": (len(outside) / len(all_matches)) if all_matches else 0.0,
        "top10_all": sorted(all_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10],
        "top10_outside": sorted(outside_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10],
        "outside_lines": outside_lines,
        "unique_colors_total": len(all_counts),
        "unique_colors_outside": len(outside_counts),
    }


# ─────────────────────────────── reporting ───────────────────────────────

def fmt(ratio: float) -> str:
    return "%.2f" % ratio


def render_markdown(pairs, stats, source, block_span, hardcoded) -> str:
    start_line = source_text.count("\n", 0, block_span[0]) + 1
    end_line = source_text.count("\n", 0, block_span[1]) + 1
    out = []
    out.append("# JISUMAN LLM Benchmark — WCAG 2.1 Contrast & Token-Discipline Audit")
    out.append("")
    out.append("- source: `%s`" % source_path)
    out.append("- C_STYLE block: lines %d–%d" % (start_line, end_line))
    out.append("- thresholds: normal text %.1f (SC 1.4.3), large text / non-text %.1f "
               "(SC 1.4.11)" % (THRESH_TEXT, THRESH_LARGE_NONTEXT))
    out.append("")
    out.append("| 用途 | 前景 | 背景 | 对比度 | 阈值 | 结果 |")
    out.append("|---|---|---|---:|---:|:---:|")
    for p in stats.get("all_pairs", pairs):
        mark = "PASS" if p.passed else "**FAIL**"
        if not p.gate:
            mark = "INFO" if not p.passed else "PASS"
        out.append("| %s%s | `%s` %s | `%s` %s | %s | %.1f | %s |"
                   % (p.usage, "（门禁）" if p.gate else "（信息）", p.fg_key, p.fg,
                      p.bg_key, p.bg, fmt(p.ratio), p.threshold, mark))
    out.append("")
    out.append("## 统计（门禁项）")
    out.append("")
    out.append("- 门禁配对数: **%d**" % stats["total"])
    out.append("- PASS: **%d**" % stats["passed"])
    out.append("- FAIL: **%d**" % stats["failed"])
    out.append("- 信息项（结构性/禁用组合，不计入退出码）: **%d**，其中未达阈值 %d 项"
               % (stats.get("info_total", 0), stats.get("info_failed", 0)))
    hits = stats.get("forbidden_hits") or []
    out.append("- 禁用组合源码命中（白字压语义实心色）: **%d**" % len(hits))
    out.append("")
    out.append("### 最差的三对（对比度最低）")
    out.append("")
    out.append("| # | 用途 | 前景 | 背景 | 对比度 | 阈值 | 结果 |")
    out.append("|---:|---|---|---|---:|---:|:---:|")
    for i, p in enumerate(stats["worst"], 1):
        out.append("| %d | %s | `%s` %s | `%s` %s | %s | %.1f | %s |"
                   % (i, p.usage, p.fg_key, p.fg, p.bg_key, p.bg, fmt(p.ratio),
                      p.threshold, "PASS" if p.passed else "**FAIL**"))
    if stats["failed_details"]:
        out.append("")
        out.append("### FAIL 明细（需要修复的设计问题）")
        out.append("")
        for p in stats["failed_details"]:
            out.append("- `%s` %s on `%s` %s — %s (需 %.1f，差 %s)"
                       % (p.fg_key, p.fg, p.bg_key, p.bg, fmt(p.ratio), p.threshold,
                          fmt(p.threshold - p.ratio)))
    out.append("")
    out.append("## 硬编码色值扫描（设计令牌纪律）")
    out.append("")
    out.append("- `#RRGGBB` 字面量总出现次数: **%d**"
               % hardcoded["total_hex_literals"])
    out.append("- 其中位于 C_STYLE 定义块内: **%d**" % hardcoded["inside_c_style"])
    out.append("- 其中位于 C_STYLE 定义块之外: **%d** (%.1f%%)"
               % (hardcoded["outside_c_style"], hardcoded["outside_ratio"] * 100))
    out.append("- 全文件不同色值数: %d；C_STYLE 之外不同色值数: %d"
               % (hardcoded["unique_colors_total"], hardcoded["unique_colors_outside"]))
    out.append("")
    out.append("### 出现次数最多的前 10 个色值")
    out.append("")
    out.append("| # | 色值 | 总出现次数 | 块外出现次数 |")
    out.append("|---:|---|---:|---:|")
    outside_counts = dict(hardcoded["top10_outside"])
    for i, (value, count) in enumerate(hardcoded["top10_all"], 1):
        out.append("| %d | `%s` | %d | %d |"
                   % (i, value, count, outside_counts.get(value, 0)))
    if hardcoded["top10_outside"]:
        out.append("")
        out.append("### C_STYLE 块外出现最多的 10 个色值（纪律违规热点）")
        out.append("")
        out.append("| # | 色值 | 块外次数 | 行号 |")
        out.append("|---:|---|---:|---|")
        for i, (value, count) in enumerate(hardcoded["top10_outside"], 1):
            lines = hardcoded["outside_lines"].get(value, [])
            shown = ", ".join(str(x) for x in lines[:8]) + (" …" if len(lines) > 8 else "")
            out.append("| %d | `%s` | %d | %s |" % (i, value, count, shown))
    return "\n".join(out)


def render_json(pairs, stats, source_path, block_span, hardcoded, exit_code) -> str:
    payload = {
        "source": source_path,
        "c_style_lines": [
            source_text.count("\n", 0, block_span[0]) + 1,
            source_text.count("\n", 0, block_span[1]) + 1,
        ],
        "thresholds": {"normal_text": THRESH_TEXT, "large_or_non_text": THRESH_LARGE_NONTEXT},
        "pairs": [
            {
                "usage": p.usage,
                "fg_token": p.fg_key,
                "fg": p.fg,
                "bg_token": p.bg_key,
                "bg": p.bg,
                "ratio": round(p.ratio, 4),
                "threshold": p.threshold,
                "result": "PASS" if p.passed else "FAIL",
            }
            for p in pairs
        ],
        "stats": {
            "total": stats["total"],
            "pass": stats["passed"],
            "fail": stats["failed"],
            "worst_three": [
                {
                    "usage": p.usage,
                    "fg_token": p.fg_key,
                    "bg_token": p.bg_key,
                    "ratio": round(p.ratio, 4),
                    "threshold": p.threshold,
                    "result": "PASS" if p.passed else "FAIL",
                }
                for p in stats["worst"]
            ],
        },
        "token_discipline": {
            "hex_literals_total": hardcoded["total_hex_literals"],
            "inside_c_style": hardcoded["inside_c_style"],
            "outside_c_style": hardcoded["outside_c_style"],
            "outside_ratio": round(hardcoded["outside_ratio"], 4),
            "unique_colors_total": hardcoded["unique_colors_total"],
            "unique_colors_outside": hardcoded["unique_colors_outside"],
            "top10_all": [
                {"hex": h, "count": c} for h, c in hardcoded["top10_all"]
            ],
            "top10_outside": [
                {"hex": h, "count": c, "lines": hardcoded["outside_lines"].get(h, [])}
                for h, c in hardcoded["top10_outside"]
            ],
        },
        "exit_code": exit_code,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


# ─────────────────────────────── main ───────────────────────────────

source_text = ""
source_path = DEFAULT_SOURCE
scan_path = DEFAULT_SCAN_SOURCE


def _scan_text(scan_path: str, source_path: str, source_text: str) -> str:
    """硬编码扫描用的文本：默认读主程序文件；失败则退回令牌文件。"""
    try:
        if scan_path and scan_path != source_path and os.path.isfile(scan_path):
            return read_source(scan_path)
    except Exception:
        pass
    return source_text


def main(argv=None) -> int:
    global source_text, source_path, scan_path

    parser = argparse.ArgumentParser(
        description="WCAG 2.1 contrast audit + design-token discipline scan "
                    "for the JISUMAN LLM Benchmark GUI."
    )
    parser.add_argument("--json", action="store_true",
                        help="emit the table and statistics as JSON instead of markdown")
    parser.add_argument("--source", default=DEFAULT_SOURCE,
                        help="path to the token file (default: %(default)s)")
    parser.add_argument("--scan-file", default=DEFAULT_SCAN_SOURCE,
                        help="file scanned for hardcoded hex colours (default: %(default)s)")
    parser.add_argument("--out", default=None,
                        help="also write the report to this file")
    args = parser.parse_args(argv)

    source_path = os.path.abspath(args.source)
    scan_path = os.path.abspath(args.scan_file)

    try:
        source_text = read_source(source_path)
        block_span = find_c_style_span(source_text)
        tokens = parse_tokens(source_text[block_span[0]:block_span[1]])
        pairs = build_pairs()
        evaluate(pairs, tokens)
    except (ParseError, ValueError) as exc:
        sys.stderr.write("PARSE ERROR: %s\n" % exc)
        sys.stderr.write("Aborting — refusing to report contrast numbers that were not "
                         "derived from a successfully parsed C_STYLE palette.\n")
        return EXIT_PARSE_ERROR

    gated = [p for p in pairs if p.gate]
    info = [p for p in pairs if not p.gate]
    failed = [p for p in gated if not p.passed]
    forbidden_hits = scan_forbidden_usage([
        scan_path,
        os.path.join(os.path.dirname(scan_path), "llm_benchmark_app", "ui_theme.py"),
        os.path.join(os.path.dirname(scan_path), "llm_benchmark_app", "ui_pages.py"),
    ])
    stats = {
        "total": len(gated),
        "passed": len(gated) - len(failed),
        "failed": len(failed),
        "info_total": len(info),
        "info_failed": sum(1 for p in info if not p.passed),
        "worst": sorted(gated, key=lambda p: p.ratio)[:3],
        "failed_details": sorted(failed, key=lambda p: p.ratio),
        "forbidden_hits": forbidden_hits,
        "all_pairs": pairs,
    }
    hardcoded = scan_hardcoded(_scan_text(scan_path, source_path, source_text), (0, 0))

    exit_code = EXIT_CONTRAST_FAIL if (failed or forbidden_hits) else EXIT_OK
    report = (render_json(pairs, stats, source_path, block_span, hardcoded, exit_code)
              if args.json else
              render_markdown(pairs, stats, source_path, block_span, hardcoded))

    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")

    if not args.json:
        print("")
        print("exit_code=%d (%s)" % (exit_code,
              "FAIL present" if exit_code == EXIT_CONTRAST_FAIL else "all PASS"))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
