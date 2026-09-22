#!/usr/bin/env python3
"""LLM Benchmark GUI — 并发性能测试工具
纯标准库实现：tkinter + sqlite3 + urllib + threading，无需额外安装依赖。
支持 OpenAI 兼容 API（通义千问 / DeepSeek / GLM / GPT 等）。

Metrics aligned with:
  • vLLM bench serve:  ttft, tpot, itl, e2el
  • NVIDIA GenAI-Perf: output_token_throughput, request_throughput, ttft, itl
  • NIM Benchmark:      output_token_throughput, request_throughput
"""
# ── 发行版本（单一来源：packaging/make-release.sh 读取此行）──────────────────
APP_VERSION = "2.0.0"

import json
import logging
import os
import sqlite3
import statistics
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from configparser import ConfigParser
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from tkinter import messagebox, ttk
from typing import Optional
from urllib import request, error
from urllib.parse import urlparse, urlunparse
import asyncio
import queue

# Optional aiohttp for high-concurrency (C>256) sweep
try:
    import aiohttp as _aiohttp_mod
    _AIOHTTP_AVAILABLE = True
except ImportError:
    _aiohttp_mod = None
    _AIOHTTP_AVAILABLE = False

# Windows asyncio: do NOT force WindowsSelectorEventLoopPolicy globally.
# select() is limited to FD_SETSIZE=512 which causes "too many file descriptors
# in select()" at C512+ concurrency.  The HC runner uses asyncio.new_event_loop()
# via _new_hc_event_loop() — Python 3.8+ default on Windows IS ProactorEventLoop.

# Resolve paths relative to script location (fixes double-click on Windows)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_SCRIPT_DIR, "llm_benchmark_history.db")
INI_PATH = os.path.join(_SCRIPT_DIR, "llm_benchmark.ini")
LOG_PATH = os.path.join(_SCRIPT_DIR, "llm_benchmark.log")
CRASH_LOG = os.path.join(_SCRIPT_DIR, "llm_benchmark_crash.log")
RESULT_DB_PATH = os.path.join(_SCRIPT_DIR, "data", "benchmark_results.db")
RESULTS_ROOT = os.path.join(_SCRIPT_DIR, "results")
# Built-in default profile names (used as stable identity keys)
_DEFAULT_ENV_NAME  = "Default - Dual RTX 5090 vLLM qwen3.6-27b-fp8"
_DEFAULT_HW_NAME   = "Dual RTX 5090 Workstation"
_DEFAULT_SW_NAME   = "vLLM OpenAI Server - Dual RTX 5090"
_DEFAULT_MODEL_NAME = "qwen3.6-27b-fp8"
DEBUG_MODE = False
# ── Modular imports (TASK-LLM-BENCHMARK-MODULE-SPLIT-TOKEN-HC-001-v1) ──────────
# Pure functions extracted to llm_benchmark_app/ subpackage.
from llm_benchmark_app.stream_parser import (
    ParsedStreamChunk, _parse_stream_chunk, _build_parser_profile,
    run_stream_parser_tests, _test_stream_parser_fixtures,
    _KNOWN_DELTA_KEYS,
)
from llm_benchmark_app.metrics import (
    percentile, clean_numbers, aggregate_results, validate_metric_consistency,
    get_metric_standard_definitions, STANDARD_METRIC_ALIASES, DB_SCHEMA_VERSION,
)
from llm_benchmark_app.runner import (
    call_llm, run_benchmark, build_chat_payload,
    normalize_api_url, check_server_reachable, fetch_models,
)
import llm_benchmark_app.runner as _runner_module
from llm_benchmark_app.high_concurrency_runner import (
    _hc_classify_error, _hc_fail_result, run_async_clean,
)
from llm_benchmark_app.history_db import (
    _create_latest_schema, init_db, _ensure_history_columns,
    save_result, save_sweep_history, load_history,
)
import llm_benchmark_app.history_db as _history_db_module
from llm_benchmark_app.result_db import (
    _init_result_db, _create_result_db_schema, _ensure_result_db_snapshot_cols,
    _insert_benchmark_run_row, _result_db_save_benchmark_run, _result_db_save_sweep_run,
    _seed_default_profiles, _result_db_get_all_profiles, _result_db_save_profile,
    _result_db_delete_profile, _make_slug, _make_run_dir, _BENCHMARK_RUN_COLUMNS,
    _test_result_db_schema,
)
from llm_benchmark_app.result_store import (
    rs_save_single_run, rs_save_sweep_run, rs_list_runs,
    rs_load_report, rs_load_result, rs_load_summary,
    rs_get_e2e_histogram_png, rs_regenerate_report, rs_regenerate_chart,
    rs_compare_runs, RESULTS_ROOT as RS_RESULTS_ROOT,
)
from llm_benchmark_app.artifact_resolver import (
    resolve_history_artifacts as _module_resolve_history_artifacts,
    list_dir_brief as _artifact_list_dir_brief,
)
# ── end module imports ────────────────────────────────────────────────────────

def open_directory(path: str) -> None:
    """Open *path* in the native file manager (cross-platform).

    Windows: os.startfile  |  macOS: open  |  Linux: xdg-open
    Raises FileNotFoundError if *path* does not exist.
    """
    import subprocess as _sp, sys as _sys
    if not os.path.isdir(path):
        raise FileNotFoundError(f"Directory not found: {path}")
    if _sys.platform == "win32":
        os.startfile(path)
    elif _sys.platform == "darwin":
        _sp.Popen(["open", path])
    else:
        _sp.Popen(["xdg-open", path])


def is_sweep_record(record: dict, result_payload: dict | None = None) -> bool:
    """Determine whether a history record represents a sweep (multi-concurrency) run.

    Decision rules — ANY matching condition returns True.  The test is
    deliberately broad so that sweep records are never mis-routed to the
    single-benchmark detail renderer.

    IMPORTANT: the presence or absence of aggregated metrics such as
    ``output_tps``, ``ttft_p95``, or ``e2e_p95`` MUST NOT influence this
    decision.  Those fields are display-only and have no bearing on run type.

    Positive indicators (checked in order):
      1. ``record["run_type"]`` / ``record["type"]`` / ``record["record_type"]``
         contains ``"sweep"`` or ``"并发扫测"``.
      2. ``record["config"]`` / ``record["config_summary"]`` / ``record["workload"]``
         starts with or contains ``"sweep /"`` or ``"sweep_"``.
      3. ``record["run_id"]`` / ``record["rid"]`` contains ``"sweep"``.
      4. *result_payload* (the loaded result.json) contains any of the keys:
         ``cases``, ``sweep_results``, ``sweep_cases``, ``concurrency_results``,
         ``concurrency_levels``, ``levels``
         where the value is a non-empty list or dict.
    """
    # ── 1. Explicit type fields ───────────────────────────────────────────
    for key in ("run_type", "type", "record_type"):
        val = (record.get(key) or "").lower()
        if "sweep" in val or "并发扫测" in val:
            return True

    # ── 2. Config / workload strings ──────────────────────────────────────
    for key in ("config", "config_summary", "workload"):
        val = (record.get(key) or "").lower()
        if "sweep /" in val or "sweep_" in val or val.startswith("sweep"):
            return True

    # ── 3. Run ID ─────────────────────────────────────────────────────────
    for key in ("run_id", "rid"):
        val = (record.get(key) or "").lower()
        if "sweep" in val:
            return True

    # ── 4. Result payload keys ────────────────────────────────────────────
    if result_payload:
        for key in ("cases", "sweep_results", "sweep_cases",
                    "concurrency_results", "concurrency_levels", "levels"):
            val = result_payload.get(key)
            if isinstance(val, (list, dict)) and val:
                return True

    return False


def setup_logging():
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(threadName)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
        ],
    )
    logging.info("=== LLM Benchmark GUI debug session started ===")
# ---------- 并发建议 ----------
# ---------- 并发建议 ----------
BENCHMARK_PRESETS = {
    "快速校准 — C1/N5":        {"concurrency": 1,  "total": 5,   "desc": "快速校准，验证连接与延迟基线"},
    "标准基线 — C8/N80（默认）": {"concurrency": 8,  "total": 80,  "desc": "标准并发基线测试，默认推荐"},
    "中高并发 — C16/N160":      {"concurrency": 16, "total": 160, "desc": "中高并发，验证服务端排队行为"},
    "压力测试 — C32/N320":      {"concurrency": 32, "total": 320, "desc": "压力测试，接近服务端上限（需确认）"},
    # ── 客户验收正式预设 ──
    "单会话最大生成速度 — C1/N5":    {
        "concurrency": 1, "total": 5,
        "desc": "单会话最大生成速度测试 (C1/N5/I1024/O2048, stream, fixed-output)",
        "benchmark_objective": "single_session_decode_speed",
        "benchmark_mode":      "real_api_experience",
    },
    "客户验收 — 单用户 1024/2048": {
        "concurrency": 1, "total": 5,
        "desc": "客户验收单用户 1024in/2048out tokens",
        "benchmark_objective": "single_session_decode_speed",
        "benchmark_mode":      "real_api_experience",
    },
    "自定义":                    {"concurrency": 64, "total": 640, "desc": "自定义并发与请求数，可手动修改"},
}
DEFAULT_PRESET_KEY = "标准基线 — C8/N80（默认）"

# ---------- 并发扫测预设 ----------
SWEEP_PRESETS = {
    "自定义": None,
    # ── 客户验收正式预设 ──
    "峰值吞吐扫描 — 1024/2048": {
        "concurrency_levels": "1,2,4,8,16,24,32",
        "max_tokens": 2048,
        "output_length_mode": "fixed",
        "temperature": 0.0,
        "stream": True,
        "request_rule": "x5",
        "benchmark_objective": "peak_throughput_sweep",
        "benchmark_mode":      "real_api_experience",
    },
    "峰值吞吐扫描（扩展）— 1024/2048": {
        "concurrency_levels": "1,2,4,8,16,24,32,48,64",
        "max_tokens": 2048,
        "output_length_mode": "fixed",
        "temperature": 0.0,
        "stream": True,
        "request_rule": "x5",
        "benchmark_objective": "peak_throughput_sweep",
        "benchmark_mode":      "real_api_experience",
    },
    "客户验收峰值吞吐 — 1024/2048": {
        "concurrency_levels": "1,2,4,8,16,32,64,128,256,512,768,1024",
        "max_tokens": 2048,
        "output_length_mode": "fixed",
        "temperature": 0.0,
        "stream": True,
        "request_rule": "x2",
        "benchmark_objective": "peak_throughput_sweep",
        "benchmark_mode":      "real_api_experience",
    },
}

# ── Sweep Scale / Tier preset tables ─────────────────────────────────────────
# Scale defines concurrency levels per tier; tier defines request count rule.
SWEEP_SCALE_DEFS = {
    "small": {
        "label_zh":       "小型扫测",
        "description_zh": "适用于单卡、小模型、快速验证。",
        "quick":    [1, 2, 4, 8],
        "formal":   [1, 2, 4, 8, 16],
        "extended": [1, 2, 4, 8, 16, 24, 32],
    },
    "medium": {
        "label_zh":       "中型扫测",
        "description_zh": "适用于双卡/四卡 PCIe 工作站和常规多卡测试。",
        "quick":    [1, 2, 4, 8, 16],
        "formal":   [1, 2, 4, 8, 16, 24, 32],
        "extended": [1, 2, 4, 8, 16, 24, 32, 48, 64],
    },
    "large": {
        "label_zh":       "大型扫测",
        "description_zh": "适用于多卡大模型服务器、H200/B200/B300 整机的正式验收。",
        "quick":    [1, 2, 4, 8, 16, 32],
        "formal":   [1, 2, 4, 8, 16, 24, 32, 48, 64],
        "extended": [1, 2, 4, 8, 16, 24, 32, 48, 64, 96, 128],
    },
    "extreme": {
        "label_zh":       "极限扫测",
        "description_zh": "适用于 NVLink / NVSwitch 整机和峰值吞吐深度压测。",
        "quick":    [1, 2, 4, 8, 16, 32, 64],
        "formal":   [1, 2, 4, 8, 16, 32, 64, 96, 128],
        "extended": [1, 2, 4, 8, 16, 32, 64, 96, 128, 192, 256],
    },
    "custom": {
        "label_zh":       "自定义扫测",
        "description_zh": "手动指定并发列表和请求数规则。",
    },
}

SWEEP_TIER_DEFS = {
    "quick":    {"label_zh": "快速",   "formula": "max(2 × C,  8)",  "rule_key": "quick"},
    "formal":   {"label_zh": "正式",   "formula": "max(5 × C, 20)",  "rule_key": "formal"},
    "extended": {"label_zh": "深度",   "formula": "max(5 × C, 50)",  "rule_key": "extended"},
    "custom":   {"label_zh": "自定义", "formula": "",                 "rule_key": "custom"},
}

SWEEP_SCALE_LABELS_ZH = {
    "small":   "小型", "medium": "中型", "large": "大型",
    "extreme": "极限", "custom": "自定义",
}
SWEEP_TIER_LABELS_ZH = {
    "quick": "快速", "formal": "正式", "extended": "深度", "custom": "自定义",
}

# Ordered code lists for index-based combo lookup (language-independent)
_SCALE_CODES = ["small", "medium", "large", "extreme", "custom"]
_TIER_CODES  = ["quick", "formal", "extended", "custom"]

# ---------- 字体自动检测 ----------
def _detect_font_family() -> str:
    """Detect the best available font for CJK + Latin rendering.
    Returns a font family name that exists on the current system."""
    # Try to query font families via tkinter if available
    try:
        import tkinter.font as tkfont
        root = __import__('tkinter').Tk()
        root.withdraw()
        fonts = set(tkfont.families())
        root.destroy()
    except Exception:
        fonts = set()
    # Priority: fonts that render both CJK and Latin well
    candidates = [
        "Noto Sans CJK SC",       # Linux — best CJK+Latin rendering
        "Microsoft YaHei UI",     # Windows 10/11 — native CJK
        "Microsoft YaHei",        # Windows 7/8
        "PingFang SC",            # macOS
        "Droid Sans Fallback",    # Linux fallback
        "DejaVu Sans",            # generic fallback
    ]
    for c in candidates:
        if c in fonts:
            return c
    # If no tkinter, check via fc-list
    try:
        import subprocess
        out = subprocess.check_output(["fc-list", ":lang=zh", "family"], text=True)
        for c in candidates:
            if c in out:
                return c
    except Exception:
        pass
    return "sans-serif"  # ultimate fallback

FONT_FAMILY = _detect_font_family()

I18N = {
    "zh_CN": {
        "app.title": f"JISUMEN LLM Benchmark GUI v{APP_VERSION}",
        "app.subtitle": "OpenAI 兼容接口并发性能测试",
        "language.label": "语言",
        "language.zh": "简体中文",
        "language.en": "English",
        "tab.overview": "总览",
        "tab.settings": "新建测试",
        "tab.benchmark": "测试与结果",
        "tab.sweep": "并发扫测",
        "tab.history": "历史对比",
        "tab.export": "报告与证据",
        # ── 总览页（UI/UX v2）──
        "ov.subtitle": "最近一轮关键指标、当前配置与环境、待处理事项",
        "ov.demo": "演示数据（本地 mock 端点，非真实硬件/模型性能）",
        "ov.kpi_title": "最近一轮关键指标",
        "ov.kpi_empty_title": "还没有任何测试记录",
        "ov.kpi_empty_body": "完成一次测试后，这里会显示 TTFT、吞吐、成功率与失败原因。",
        "ov.act_new": "新建测试",
        "ov.act_import_demo": "载入演示配置",
        "ov.act_last_report": "查看最近报告",
        "ov.cfg_title": "当前配置与环境",
        "ov.pending_title": "待处理事项",
        "ov.no_pending": "无待处理事项",
        "ov.pending_fail": "最近一轮存在失败请求：{n} 条（可在结果详情按错误分类查看）",
        "ov.pending_demo": "当前数据含演示数据，不可用于对外性能结论",
        "ov.pending_no_env": "尚未绑定环境档案，结果缺少硬件/软件栈快照",
        "ov.recent_title": "最近运行",
        "ov.col_time": "时间",
        "ov.col_model": "模型",
        "ov.col_workload": "负载",
        "ov.col_source": "数据来源",
        "ov.source_demo": "演示数据",
        "ov.source_real": "真实跑测",
        "ov.recent_empty": "暂无运行记录",
        # ── 历史对比页 ──
        "cmp.subtitle": "选择两次运行比较；配置或口径不同时会显式标注，避免误读为同条件对比",
        "cmp.pick_a": "运行 A（基准）",
        "cmp.pick_b": "运行 B（对照）",
        "cmp.btn_compare": "开始对比",
        "cmp.btn_clear": "清除选择",
        "cmp.same_note": "两次运行的配置与口径一致，可直接比较。",
        "cmp.diff_warn": "不可直接比较：存在 {n} 项配置/口径差异（见下表）",
        "cmp.diff_col": "差异项",
        "cmp.metric": "指标",
        "cmp.unit": "单位",
        "cmp.delta": "差值",
        "cmp.delta_pct": "变化",
        "cmp.need_two": "请在上方选择两条记录（A 为基准，B 为对照）后开始对比。",
        "cmp.guide_title": "还没有可对比的运行",
        "cmp.guide_body": "先完成两次测试（或不同配置各一次），再回来对比；差异会自动列出。",
        # ── 报告与证据页 ──
        "exp.subtitle": "选择一次运行，查看并导出交付件（报告、图表、原始数据、清单）",
        "exp.pick_run": "选择运行",
        "exp.artifacts_title": "交付件清单",
        "exp.col_artifact": "交付件",
        "exp.col_kind": "类型",
        "exp.col_size": "大小",
        "exp.provenance_title": "运行证据（Run Provenance）",
        "exp.btn_open": "打开所在目录",
        "exp.btn_bundle": "打包证据（ZIP）",
        "exp.btn_refresh": "刷新",
        "exp.bundle_done": "证据包已生成：{path}",
        "exp.empty_title": "还没有可交付的运行记录",
        "exp.empty_body": "完成一次测试后，报告 TXT/Markdown、图表、原始 JSON 与交付清单会汇总在这里，可直接打包给客户。",
        "exp.no_artifacts": "该运行没有可用交付件",
        "exp.need_run": "请先在下拉框选择一次运行",
        "exp.missing": "未记录",
        "exp.demo_warn": "该运行含演示数据，导出件已标注，不可作为真实性能结论交付",
        # ── 通用 ──
        "common.refresh": "刷新",
        "common.demo_badge": "演示数据",
        "status.idle": "空闲",
        "status.ready": "就绪",
        "status.benchmarking": "测试中",
        "status.sweeping": "扫测中",
        "status.completed": "已完成",
        "status.failed": "失败",
        "status.no_history": "暂无历史记录",
        "status.select_history": "请选择一条历史记录查看详情",
        "status.history_count": "共 {count} 条记录，双击行查看完整详情",
        "status.ready_action": "就绪 — 请配置参数后开始测试",
        "status.reset_action": "已重置 — 请配置参数后开始测试",
        "status.saved_config": "配置已保存到 llm_benchmark.ini",
        "run.busy_benchmark": "基准测试正在运行，请等待完成后再开始扫测。",
        "run.busy_sweep": "并发扫测正在运行，请等待完成后再开始基准测试。",
        "section.api": "API 配置",
        "section.test_params": "测试参数",
        "section.generation": "生成参数",
        "section.load": "负载参数",
        "section.save_options": "保存选项",
        "section.actions": "操作",
        "section.result_summary": "结果摘要",
        "section.e2e_distribution": "E2E 端到端延迟分布（e2el）",
        "section.detail_report": "详细报告",
        "section.sweep_config": "扫测配置与操作",
        "section.sweep_status": "扫测状态",
        "section.chart_analysis": "图形分析",
        "section.expert_summary": "专家分析简评",
        "section.output_files": "输出文件",
        "section.history_preview": "历史详情预览",
        "section.sweep_overview": "扫测概览",
        "section.sweep_case_detail": "并发档位明细",
        "button.start_benchmark": "开始基准测试",
        "button.benchmarking": "基准测试中...",
        "button.start_sweep": "开始扫测",
        "button.sweeping": "正在进行扫测......",
        "button.save_config": "保存配置",
        "button.reset_config": "重置配置",
        "button.query": "查询",
        "button.show": "显示",
        "button.refresh": "↻ 刷新",
        "button.clear_records": "✕ 清空记录",
        "button.close": "关闭",
        "button.confirm_selection": "确认选择",
        "button.skip": "跳过",
        "button.view_report_detail": "在报告中查看详情",
        "label.api_url": "API 地址",
        "label.api_key": "API 密钥",
        "label.model_optional": "模型名称（选填）",
        "label.system_prompt": "系统提示词",
        "label.user_prompt": "用户提示词",
        "label.max_tokens": "Max Tokens",
        "label.output_length_mode": "输出长度模式",
        "label.output_mode_normal": "普通模式（max_tokens 为上限）",
        "label.output_mode_fixed": "固定输出模式（vLLM bench 兼容）",
        "label.output_mode_normal_short": "普通模式",
        "label.output_mode_fixed_short": "固定输出模式",
        "label.fixed_output_tokens": "固定输出 Token",
        "label.fixed_output_validation_passed": "固定输出长度校验通过",
        "label.fixed_output_validation_failed": "固定输出模式已启用，但实际输出 token 明显低于目标值，可能是服务端不支持 min_tokens / ignore_eos，或上下文长度 / stop 条件限制。",
        "label.temperature": "Temperature",
        "label.stream": "流式输出",
        "label.warmup": "预热请求",
        "label.concurrency": "并发级别",
        "label.requests_multiplier": "请求倍数",
        "label.resource_monitoring": "资源监测",
        "label.history_type": "类型",
        "label.save_report": "保存测试报告",
        "label.auto_save": "自动保存配置",
        "label.total_requests": "总请求数",
        "label.custom_concurrency": "自定义并发",
        "hint.concurrency": "例如: 1,5,10,20,40",
        "hint.multiplier": "每个并发级别: 请求数 = 并发数 × 倍数",
        "hint.resource_disabled": "MVP 阶段资源监测暂不可用",
        "sweep.save_json": "保存扫测数据 JSON",
        "sweep.save_md": "保存文字分析报告 Markdown",
        "sweep.save_png": "保存图表 PNG",
        "sweep.save_history": "保存到历史记录",
        "history.id": "ID",
        "history.time": "时间",
        "history.type": "类型",
        "history.model": "模型",
        "history.config": "配置",
        "history.key_result": "核心结果",
        "history.status": "状态",
        "history.single": "单次测试",
        "history.sweep": "并发扫测",
        "history.type_all": "全部",
        "history.type_single": "单次测试",
        "history.type_sweep": "并发扫测",
        "chart.sweep_title": "推理性能并发扫测分析报告",
        "chart.latency_vs_concurrency": "延迟随并发变化趋势",
        "chart.throughput_vs_concurrency": "Token 吞吐随并发变化",
        "chart.first_generation": "首包 / 首字 / 生成速度趋势",
        "chart.efficiency_stability": "并发效率与稳定性分析",
        "chart.concurrency_axis": "总并发数",
        "chart.latency_axis": "延迟 (s)",
        "chart.throughput_axis": "吞吐 (tok/s)",
        "chart.time_axis": "时间 (s)",
        "chart.success_rate_axis": "成功率 (%)",
        "chart.hist_empty": "暂无延迟数据",
        "chart.hist_latency_axis": "延迟 (秒)",
        "chart.hist_count_axis": "请求数",
        "msg.error": "错误",
        "msg.warning": "提示",
        "msg.confirm": "确认",
        "msg.close": "关闭",
        "msg.input_error": "输入错误",
        "msg.clear_history_confirm": "确定要清空所有历史记录吗？",
        "msg.running": "已有测试正在进行，请等待完成。",
        "msg.api_required": "请输入 API 地址",
        "msg.api_required_settings": "请在「参数设置」中填写 API 地址",
        "msg.prompt_required": "请输入用户提示词",
        "msg.prompt_required_settings": "请在「参数设置」中填写用户提示词",
        "msg.multiplier_positive": "请求倍数必须大于 0",
        "sweep.preset_label": "预设",
        "sweep.preset_custom": "自定义",
        "sweep.preset_hc_peak": "客户验收峰值吞吐 — 1024/2048",
        "sweep.request_rule_label": "请求规则",
        "sweep.request_rule_x1": "× 1",
        "sweep.request_rule_x2": "× 2（推荐）",
        "sweep.request_rule_x5": "× 5",
        "sweep.request_rule_x10": "× 10",
        "sweep.request_rule_fixed": "固定总请求数",
        "sweep.fixed_total_label": "固定请求总数",
        "sweep.hc_warning_title": "高并发确认",
        "sweep.hc_warning_body": (
            "当前扫测包含 C512 或更高并发，可能同时建立大量 HTTP streaming 连接。\n"
            "这会显著增加客户端、网络和服务端压力，并可能持续较长时间。\n"
            "建议先确认服务端已稳定运行，并优先使用 concurrency × 1 或 × 2 进行试跑。\n\n"
            "⚠ Windows 客户端提示：C512/C768/C1024 需要 ProactorEventLoop (IOCP)。\n"
            "  本工具将自动使用 Proactor；如出现 select/file descriptor 错误，\n"
            "  请改用 Linux 客户端或将并发降至 C384 以下。\n\n"
            "是否继续？"
        ),
        "sweep.aiohttp_required": (
            "高并发扫测（C>256）需要 aiohttp，请安装：\n"
            "pip install aiohttp\n\n"
            "当前将使用线程池模式（C≤256 时推荐）。"
        ),
        "sweep.hc_mode_title": "正在进行高并发扫测...",
        "sweep.peak_output_tps": "峰值输出吞吐 (Peak Output TPS)",
        "sweep.peak_concurrency": "峰值并发档位",
        "sweep.peak_stable_concurrency": "峰值稳定并发",
        "sweep.hc_success_rate": "C{conc} 成功率",
        "sweep.hc_fail_count": "C{conc} 失败数",
        "sweep.hc_failure_summary": "失败原因分布",
        "sweep.hc_threshold_8k": "是否达到单实例 >8000 tok/s",
        "sweep.hc_threshold_25k": "是否达到双实例 >25000 tok/s",
        "sweep.ulimit_hint": "提示: Linux 客户端建议 ulimit -n >= 65535",
        "stream.parser_profile": "流式解析能力档案",
        "stream.first_generated_token": "首个生成内容延迟",
        "stream.first_answer_token": "首个回答正文延迟",
        "stream.first_reasoning_token": "首个推理内容延迟",
        "stream.first_generated_gap": "首包到首个生成内容间隔",
        "stream.first_answer_gap": "首包到首个回答内容间隔",
        "stream.generated_itl": "生成内容 ITL",
        "stream.stream_event_itl": "流式事件 ITL（校准参考）",
        "stream.answer_itl": "回答内容 ITL",
        "stream.reasoning_itl": "推理内容 ITL",
        "stream.warn_no_generated_field": "服务端返回了 completion_tokens，但工具没有捕获到任何生成内容字段。请检查流式 chunk 字段格式。",
        "stream.warn_reasoning_only": "该模型本次流式输出包含 reasoning 字段，但未捕获到回答正文 content。若前端隐藏 reasoning，用户首字体验应参考 First Answer Token。",
        "stream.warn_unknown_delta": "检测到未识别的流式 delta 字段。已保存 stream_debug 供兼容性分析。",
        "progress.benchmarking": "正在进行基准测试...",
        "progress.sweeping": "正在进行扫测...",
        "progress.preparing": "正在准备...",
        "progress.finishing": "正在收尾...",
        "progress.elapsed": "已运行",
        "tab.env_profiles": "环境档案",
        "tab.hw_profiles": "硬件环境",
        "tab.sw_profiles": "软件栈",
        "tab.model_profiles": "模型部署",
        "tab.db_settings": "数据库设置",
        "env.selector_label": "被测环境",
        "env.manage_btn": "管理环境",
        "env.read_params_btn": "读取环境参数",
        "env.read_params_no_env": "当前未选择环境档案，请先在上方选择一个环境档案。",
        "env.read_params_missing_fields": "环境档案缺少必要参数，请到「环境档案」页补齐后再读取：",
        "env.read_params_overwrite": "当前参数已有手动填写内容，与环境档案不一致。\n是否用环境档案参数覆盖当前填写内容？",
        "env.read_params_applied": "环境参数已成功读取并填入。",
        "env.no_env": "未指定环境 / Unspecified",
        "env.unspecified_warning": "未绑定环境档案，本次结果不建议用于长期横向对比。",
        "env.profile_name": "档案名称",
        "env.hostname": "主机名",
        "env.ip_address": "IP 地址",
        "env.gpu_model": "GPU 型号",
        "env.gpu_count": "GPU 数量",
        "env.network_type": "互联 / 网络",
        "env.storage_type": "存储类型",
        "env.cpu_model": "CPU 型号",
        "env.memory_gb": "内存 (GB)",
        "env.notes": "备注",
        "env.backend": "推理后端",
        "env.backend_version": "后端版本",
        "env.api_type": "API 类型",
        "env.deployment_type": "部署方式",
        "env.reasoning_parser": "Reasoning Parser",
        "env.container_image": "容器镜像",
        "env.startup_args": "启动参数",
        "env.model_family": "模型系列",
        "env.model_size": "模型参数量",
        "env.quantization": "精度 / 量化",
        "env.model_type": "模型类型",
        "env.context_length": "上下文长度",
        "env.tensor_parallel": "Tensor Parallel",
        "env.hardware_profile": "硬件档案",
        "env.software_profile": "软件栈档案",
        "env.model_profile": "模型档案",
        "env.environment_name": "环境名称",
        "env.new": "新建",
        "env.duplicate": "复制",
        "env.save": "保存",
        "env.delete": "删除",
        "env.saved_ok": "档案已保存",
        "env.deleted_ok": "档案已删除",
        "env.select_first": "请先选择档案",
        "env.confirm_delete": "确认删除此档案？",
        "env.custom_input": "自定义值",
        "env.other": "其它",
        "db.path_label": "数据库路径",
        "db.results_root": "Results Root Directory",
        "db.init_btn": "初始化 / 修复数据库",
        "db.test_btn": "测试写入",
        "db.init_ok": "数据库初始化成功",
        "db.test_ok": "写入测试成功",
        "db.test_fail": "写入测试失败",
        "report.env_summary": "被测环境",
        "report.env_unspecified": "未指定",
        # ── Token calibration section (TASK-LLM-BENCHMARK-INPUT-TOKEN-CALIBRATION-UI-FIX-001-v1) ──
        "section.token_calibration": "输入 Token 校准",
        "calib.target_input_tokens": "目标输入 Token 数",
        "calib.target_output_tokens": "目标输出 Token 数",
        "calib.prompt_mode": "输入模式",
        "calib.calibration_method": "校准方式",
        "calib.system_prompt": "系统提示词（校准用）",
        "calib.user_prompt": "用户提示词（校准用）",
        "button.generate_input": "自动生成输入",
        "button.calibrate_input": "校准输入 Token 数",
        "button.verify_token_usage": "验证 Token 用量",
        "button.apply_to_benchmark": "应用到测试参数",
        "calib.status_not_calibrated": "尚未校准 — 点击「校准输入 Token 数」开始",
        "calib.apply_no_result": "当前没有有效的输入 Token 校准结果，未应用到测试参数。",
        "calib.apply_success": "已将输入 Token 校准结果应用到测试参数。",
        # ── Sweep Scale / Tier preset system (TASK-BENCHMARK-SWEEP-PRESETS-SIMPLIFY-001-v2) ──
        "sweep.scale_label":         "扫测规模",
        "sweep.tier_label":          "扫测档次",
        "sweep.conc_list_label":     "并发列表",
        "sweep.conc_list_hint":      "支持最大 C1024，可手动编辑（编辑后标记为自定义修改）",
        "sweep.fixed_total_hint":    "（所有并发档位使用相同总请求数）",
        "sweep.scale.small":         "小型",
        "sweep.scale.medium":        "中型",
        "sweep.scale.large":         "大型",
        "sweep.scale.extreme":       "极限",
        "sweep.scale.custom":        "自定义",
        "sweep.scale.desc.small":    "适用于单卡、小模型、快速验证。",
        "sweep.scale.desc.medium":   "适用于双卡/四卡 PCIe 工作站和常规多卡测试。",
        "sweep.scale.desc.large":    "适用于多卡大模型服务器、H200/B200/B300 整机的正式验收。",
        "sweep.scale.desc.extreme":  "适用于 NVLink / NVSwitch 整机和峰值吞吐深度压测。",
        "sweep.scale.desc.custom":   "手动指定并发列表和请求数规则。",
        "sweep.tier.quick":          "快速",
        "sweep.tier.formal":         "正式",
        "sweep.tier.extended":       "深度",
        "sweep.tier.custom":         "自定义",
        "sweep.tier_formula_prefix": "每档请求数 = ",
        "sweep.preset_status.applied":     "已应用: {scale} + {tier}",
        "sweep.preset_status.modified":    "自定义修改（基于 {src_scale} + {src_tier}）",
        "sweep.preset_status.full_custom": "完全自定义",
        "sweep.custom_rule.x2":    "× 2",
        "sweep.custom_rule.x5":    "× 5（正式）",
        "sweep.custom_rule.x10":   "× 10",
        "sweep.custom_rule.fixed": "固定总请求数",
    },
    "en_US": {
        "app.title": f"JISUMEN LLM Benchmark GUI v{APP_VERSION}",
        "app.subtitle": "OpenAI-compatible API concurrency benchmark",
        "language.label": "Language",
        "language.zh": "简体中文",
        "language.en": "English",
        "tab.overview": "Overview",
        "tab.settings": "New Test",
        "tab.benchmark": "Test & Results",
        "tab.sweep": "Concurrency Sweep",
        "tab.history": "History & Compare",
        "tab.export": "Reports & Evidence",
        # ── Overview page (UI/UX v2) ──
        "ov.subtitle": "Latest key metrics, current configuration and environment, open items",
        "ov.demo": "DEMO DATA (local mock endpoint — not real hardware/model performance)",
        "ov.kpi_title": "Latest key metrics",
        "ov.kpi_empty_title": "No test runs yet",
        "ov.kpi_empty_body": "After one run, TTFT, throughput, success rate and failure reasons appear here.",
        "ov.act_new": "New test",
        "ov.act_import_demo": "Load demo config",
        "ov.act_last_report": "Open latest report",
        "ov.cfg_title": "Current configuration & environment",
        "ov.pending_title": "Open items",
        "ov.no_pending": "Nothing pending",
        "ov.pending_fail": "Last run had {n} failed requests (inspect by error class in run detail)",
        "ov.pending_demo": "Current data includes demo data — not usable for external performance claims",
        "ov.pending_no_env": "No environment profile bound — hardware/software stack snapshot missing",
        "ov.recent_title": "Recent runs",
        "ov.col_time": "Time",
        "ov.col_model": "Model",
        "ov.col_workload": "Workload",
        "ov.col_source": "Source",
        "ov.source_demo": "Demo",
        "ov.source_real": "Measured",
        "ov.recent_empty": "No runs yet",
        # ── Compare page ──
        "cmp.subtitle": "Compare two runs; configuration or metric-scope differences are flagged explicitly",
        "cmp.pick_a": "Run A (baseline)",
        "cmp.pick_b": "Run B (candidate)",
        "cmp.btn_compare": "Compare",
        "cmp.btn_clear": "Clear selection",
        "cmp.same_note": "Both runs share the same configuration and metric scope — directly comparable.",
        "cmp.diff_warn": "NOT directly comparable: {n} configuration/scope difference(s) below",
        "cmp.diff_col": "Difference",
        "cmp.metric": "Metric",
        "cmp.unit": "Unit",
        "cmp.delta": "Delta",
        "cmp.delta_pct": "Change",
        "cmp.need_two": "Select two runs above (A = baseline, B = candidate) and compare.",
        "cmp.guide_title": "No comparable runs yet",
        "cmp.guide_body": "Run two tests (ideally with different settings) and come back; differences are listed automatically.",
        # ── Reports & evidence page ──
        "exp.subtitle": "Pick a run and export its deliverables (reports, charts, raw data, manifest)",
        "exp.pick_run": "Select run",
        "exp.artifacts_title": "Deliverables",
        "exp.col_artifact": "Artifact",
        "exp.col_kind": "Type",
        "exp.col_size": "Size",
        "exp.provenance_title": "Run provenance",
        "exp.btn_open": "Open folder",
        "exp.btn_bundle": "Bundle evidence (ZIP)",
        "exp.btn_refresh": "Refresh",
        "exp.bundle_done": "Evidence bundle created: {path}",
        "exp.empty_title": "No exportable runs yet",
        "exp.empty_body": "After a run, report TXT/Markdown, charts, raw JSON and the artifact manifest are collected here for delivery.",
        "exp.no_artifacts": "No artifacts for this run",
        "exp.need_run": "Select a run first",
        "exp.missing": "not recorded",
        "exp.demo_warn": "This run contains demo data and is labelled as such — not valid as a real performance claim",
        # ── Common ──
        "common.refresh": "Refresh",
        "common.demo_badge": "DEMO",
        "status.idle": "Idle",
        "status.ready": "Ready",
        "status.benchmarking": "Benchmarking",
        "status.sweeping": "Sweeping",
        "status.completed": "Completed",
        "status.failed": "Failed",
        "status.no_history": "No history records",
        "status.select_history": "Select a history record to view details",
        "status.history_count": "{count} records. Double-click a row for full details",
        "status.ready_action": "Ready — configure parameters before starting",
        "status.reset_action": "Reset complete — configure parameters before starting",
        "status.saved_config": "Config saved to llm_benchmark.ini",
        "run.busy_benchmark": "A benchmark is already running. Please wait before starting a sweep.",
        "run.busy_sweep": "A concurrency sweep is already running. Please wait before starting a benchmark.",
        "section.api": "API Configuration",
        "section.test_params": "Test Parameters",
        "section.generation": "Generation Parameters",
        "section.load": "Load Parameters",
        "section.save_options": "Save Options",
        "section.actions": "Actions",
        "section.result_summary": "Result Summary",
        "section.e2e_distribution": "E2E Latency Distribution (e2el)",
        "section.detail_report": "Detailed Report",
        "section.sweep_config": "Sweep Configuration and Actions",
        "section.sweep_status": "Sweep Status",
        "section.chart_analysis": "Chart Analysis",
        "section.expert_summary": "Expert Analysis Summary",
        "section.output_files": "Output Files",
        "section.history_preview": "History Detail Preview",
        "section.sweep_overview": "Sweep Overview",
        "section.sweep_case_detail": "Concurrency Case Details",
        "button.start_benchmark": "Start Benchmark",
        "button.benchmarking": "Benchmarking...",
        "button.start_sweep": "Start Sweep",
        "button.sweeping": "Sweeping......",
        "button.save_config": "Save Config",
        "button.reset_config": "Reset Config",
        "button.query": "Query",
        "button.show": "Show",
        "button.refresh": "↻ Refresh",
        "button.clear_records": "✕ Clear Records",
        "button.close": "Close",
        "button.confirm_selection": "Confirm Selection",
        "button.skip": "Skip",
        "button.view_report_detail": "View Details in Report",
        "label.api_url": "API URL",
        "label.api_key": "API Key",
        "label.model_optional": "Model (optional)",
        "label.system_prompt": "System Prompt",
        "label.user_prompt": "User Prompt",
        "label.max_tokens": "Max Tokens",
        "label.output_length_mode": "Output Length Mode",
        "label.output_mode_normal": "Normal Mode (max_tokens as upper bound)",
        "label.output_mode_fixed": "Fixed Output Mode (vLLM bench compatible)",
        "label.output_mode_normal_short": "Normal",
        "label.output_mode_fixed_short": "Fixed",
        "label.fixed_output_tokens": "Fixed Output Tokens",
        "label.fixed_output_validation_passed": "Fixed output length validation passed",
        "label.fixed_output_validation_failed": "Fixed output mode is enabled, but actual completion tokens are far below the target. The server may not support min_tokens / ignore_eos, or context/stop constraints may apply.",
        "label.temperature": "Temperature",
        "label.stream": "Stream",
        "label.warmup": "Warmup Requests",
        "label.concurrency": "Concurrency Levels",
        "label.requests_multiplier": "Request Multiplier",
        "label.resource_monitoring": "Resource Monitoring",
        "label.history_type": "Type",
        "label.save_report": "Save Test Report",
        "label.auto_save": "Auto-save Config",
        "label.total_requests": "Total Requests",
        "label.custom_concurrency": "Custom Concurrency",
        "hint.concurrency": "Example: 1,5,10,20,40",
        "hint.multiplier": "Requests per level = concurrency × multiplier",
        "hint.resource_disabled": "Resource monitoring is unavailable in MVP",
        "sweep.save_json": "Save Sweep JSON",
        "sweep.save_md": "Save Markdown Analysis Report",
        "sweep.save_png": "Save PNG Chart",
        "sweep.save_history": "Save to History",
        "history.id": "ID",
        "history.time": "Time",
        "history.type": "Type",
        "history.model": "Model",
        "history.config": "Config",
        "history.key_result": "Key Result",
        "history.status": "Status",
        "history.single": "Single",
        "history.sweep": "Sweep",
        "history.type_all": "All",
        "history.type_single": "Single",
        "history.type_sweep": "Sweep",
        "chart.sweep_title": "Inference Performance Concurrency Sweep Analysis Report",
        "chart.latency_vs_concurrency": "Latency vs Concurrency",
        "chart.throughput_vs_concurrency": "Token Throughput vs Concurrency",
        "chart.first_generation": "First Chunk / First Visible Token / Generation Speed",
        "chart.efficiency_stability": "Concurrency Efficiency and Stability",
        "chart.concurrency_axis": "Concurrency",
        "chart.latency_axis": "Latency (s)",
        "chart.throughput_axis": "Throughput (tok/s)",
        "chart.time_axis": "Time (s)",
        "chart.success_rate_axis": "Success Rate (%)",
        "chart.hist_empty": "No latency data",
        "chart.hist_latency_axis": "Latency (s)",
        "chart.hist_count_axis": "Requests",
        "msg.error": "Error",
        "msg.warning": "Notice",
        "msg.confirm": "Confirm",
        "msg.close": "Close",
        "msg.input_error": "Input Error",
        "msg.clear_history_confirm": "Clear all history records?",
        "msg.running": "A test is already running. Please wait until it finishes.",
        "msg.api_required": "Enter an API URL.",
        "msg.api_required_settings": "Enter an API URL in Settings.",
        "msg.prompt_required": "Enter a user prompt.",
        "msg.prompt_required_settings": "Enter a user prompt in Settings.",
        "msg.multiplier_positive": "Request multiplier must be greater than 0.",
        "sweep.preset_label": "Preset",
        "sweep.preset_custom": "Custom",
        "sweep.preset_hc_peak": "Peak Throughput Acceptance — 1024/2048",
        "sweep.request_rule_label": "Request Rule",
        "sweep.request_rule_x1": "× 1",
        "sweep.request_rule_x2": "× 2 (recommended)",
        "sweep.request_rule_x5": "× 5",
        "sweep.request_rule_x10": "× 10",
        "sweep.request_rule_fixed": "Fixed Total",
        "sweep.fixed_total_label": "Fixed Total Requests",
        "sweep.hc_warning_title": "High-Concurrency Confirmation",
        "sweep.hc_warning_body": (
            "This sweep includes C512 or higher concurrency and may open many HTTP streaming connections.\n"
            "It can heavily load the client, network, and server, and may run for a long time.\n"
            "Start with concurrency × 1 or × 2 before formal validation.\n\n"
            "⚠ Windows clients: C512/C768/C1024 requires ProactorEventLoop (IOCP).\n"
            "  This tool selects Proactor automatically. If you see select() or\n"
            "  file descriptor errors, switch to a Linux client or reduce concurrency\n"
            "  to C384 or below.\n\n"
            "Continue?"
        ),
        "sweep.aiohttp_required": (
            "High-concurrency sweep (C>256) requires aiohttp. Please install:\n"
            "pip install aiohttp\n\n"
            "Thread-pool mode will be used (recommended for C<=256)."
        ),
        "sweep.hc_mode_title": "High-concurrency sweep running...",
        "sweep.peak_output_tps": "Peak Output TPS",
        "sweep.peak_concurrency": "Peak Concurrency",
        "sweep.peak_stable_concurrency": "Peak Stable Concurrency",
        "sweep.hc_success_rate": "C{conc} Success Rate",
        "sweep.hc_fail_count": "C{conc} Fail Count",
        "sweep.hc_failure_summary": "Failure Distribution",
        "sweep.hc_threshold_8k": "Meets single-instance >8000 tok/s",
        "sweep.hc_threshold_25k": "Meets dual-instance >25000 tok/s",
        "sweep.ulimit_hint": "Tip: Linux client recommends ulimit -n >= 65535",
        "stream.parser_profile": "Stream Parser Profile",
        "stream.first_generated_token": "First Generated Token Latency",
        "stream.first_answer_token": "First Answer Token Latency",
        "stream.first_reasoning_token": "First Reasoning Token Latency",
        "stream.first_generated_gap": "First Generated Gap",
        "stream.first_answer_gap": "First Answer Gap",
        "stream.generated_itl": "Generated ITL",
        "stream.stream_event_itl": "Stream Event ITL (calibration)",
        "stream.answer_itl": "Answer ITL",
        "stream.reasoning_itl": "Reasoning ITL",
        "stream.warn_no_generated_field": "The server returned completion_tokens, but no generated streaming text field was captured. Check the streaming chunk format.",
        "stream.warn_reasoning_only": "This stream contains reasoning fields but no answer content was captured. If the frontend hides reasoning, user-visible latency should use First Answer Token.",
        "stream.warn_unknown_delta": "Unknown streaming delta fields detected. stream_debug has been saved for compatibility analysis.",
        "progress.benchmarking": "Benchmarking...",
        "progress.sweeping": "Sweeping...",
        "progress.preparing": "Preparing...",
        "progress.finishing": "Finishing...",
        "progress.elapsed": "Elapsed",
        "tab.env_profiles": "Environment Profiles",
        "tab.hw_profiles": "Hardware",
        "tab.sw_profiles": "Software Stack",
        "tab.model_profiles": "Model Deployment",
        "tab.db_settings": "Database Settings",
        "env.selector_label": "Test Environment",
        "env.manage_btn": "Manage Profiles",
        "env.read_params_btn": "Read Environment Params",
        "env.read_params_no_env": "No environment profile selected. Please select one above first.",
        "env.read_params_missing_fields": "The environment profile is missing required parameters. Please complete it in the Environment Profiles tab:",
        "env.read_params_overwrite": "Some current parameters differ from the selected environment profile.\nOverwrite current values with environment profile values?",
        "env.read_params_applied": "Environment parameters applied successfully.",
        "env.no_env": "Unspecified Environment",
        "env.unspecified_warning": "No environment profile linked. Results are not recommended for long-term comparison.",
        "env.profile_name": "Profile Name",
        "env.hostname": "Hostname",
        "env.ip_address": "IP Address",
        "env.gpu_model": "GPU Model",
        "env.gpu_count": "GPU Count",
        "env.network_type": "Network / Interconnect",
        "env.storage_type": "Storage Type",
        "env.cpu_model": "CPU Model",
        "env.memory_gb": "Memory (GB)",
        "env.notes": "Notes",
        "env.backend": "Inference Backend",
        "env.backend_version": "Backend Version",
        "env.api_type": "API Type",
        "env.deployment_type": "Deployment Type",
        "env.reasoning_parser": "Reasoning Parser",
        "env.container_image": "Container Image",
        "env.startup_args": "Startup Args",
        "env.model_family": "Model Family",
        "env.model_size": "Model Size",
        "env.quantization": "Precision / Quantization",
        "env.model_type": "Model Type",
        "env.context_length": "Context Length",
        "env.tensor_parallel": "Tensor Parallel",
        "env.hardware_profile": "Hardware Profile",
        "env.software_profile": "Software Stack Profile",
        "env.model_profile": "Model Profile",
        "env.environment_name": "Environment Name",
        "env.new": "New",
        "env.duplicate": "Duplicate",
        "env.save": "Save",
        "env.delete": "Delete",
        "env.saved_ok": "Profile saved",
        "env.deleted_ok": "Profile deleted",
        "env.select_first": "Please select a profile first",
        "env.confirm_delete": "Delete this profile?",
        "env.custom_input": "Custom value",
        "env.other": "Other",
        "db.path_label": "Database Path",
        "db.results_root": "Results Root Directory",
        "db.init_btn": "Initialize / Repair Database",
        "db.test_btn": "Test Write",
        "db.init_ok": "Database initialized successfully",
        "db.test_ok": "Write test passed",
        "db.test_fail": "Write test failed",
        "report.env_summary": "Test Environment",
        "report.env_unspecified": "Unspecified",
        # ── Token calibration section (TASK-LLM-BENCHMARK-INPUT-TOKEN-CALIBRATION-UI-FIX-001-v1) ──
        "section.token_calibration": "Input Token Calibration",
        "calib.target_input_tokens": "Target Input Tokens",
        "calib.target_output_tokens": "Output Token Count",
        "calib.prompt_mode": "Prompt Mode",
        "calib.calibration_method": "Calibration Method",
        "calib.system_prompt": "System Prompt (Calibration)",
        "calib.user_prompt": "User Prompt",
        "button.generate_input": "Generate Input",
        "button.calibrate_input": "Calibrate Input Tokens",
        "button.verify_token_usage": "Verify Token Usage",
        "button.apply_to_benchmark": "Apply to Benchmark Settings",
        "calib.status_not_calibrated": "Not calibrated — click Calibrate Input Tokens to start",
        "calib.apply_no_result": "No valid input-token calibration result is available. Benchmark settings were not updated.",
        "calib.apply_success": "Input-token calibration result has been applied to benchmark settings.",
        # ── Sweep Scale / Tier preset system (TASK-BENCHMARK-SWEEP-PRESETS-SIMPLIFY-001-v2) ──
        "sweep.scale_label":         "Sweep Scale",
        "sweep.tier_label":          "Sweep Tier",
        "sweep.conc_list_label":     "Concurrency List",
        "sweep.conc_list_hint":      "Max C1024. Editable — manual edits mark the preset as modified.",
        "sweep.fixed_total_hint":    "(Same count for all concurrency levels)",
        "sweep.scale.small":         "Small",
        "sweep.scale.medium":        "Medium",
        "sweep.scale.large":         "Large",
        "sweep.scale.extreme":       "Extreme",
        "sweep.scale.custom":        "Custom",
        "sweep.scale.desc.small":    "Single GPU, small model, quick validation.",
        "sweep.scale.desc.medium":   "Dual/quad PCIe GPU workstation or standard multi-GPU tests.",
        "sweep.scale.desc.large":    "Multi-GPU servers, H200/B200/B300 formal acceptance testing.",
        "sweep.scale.desc.extreme":  "NVLink/NVSwitch full-system deep peak throughput tests.",
        "sweep.scale.desc.custom":   "Manually specify concurrency list and request count rule.",
        "sweep.tier.quick":          "Quick",
        "sweep.tier.formal":         "Formal",
        "sweep.tier.extended":       "Extended",
        "sweep.tier.custom":         "Custom",
        "sweep.tier_formula_prefix": "Requests per concurrency = ",
        "sweep.preset_status.applied":     "Applied: {scale} + {tier}",
        "sweep.preset_status.modified":    "Modified from preset ({src_scale} + {src_tier})",
        "sweep.preset_status.full_custom": "Fully custom",
        "sweep.custom_rule.x2":    "× 2",
        "sweep.custom_rule.x5":    "× 5 (formal)",
        "sweep.custom_rule.x10":   "× 10",
        "sweep.custom_rule.fixed": "Fixed Total",
    },
}

I18N_TEXT_KEYS = {
    text: key
    for key, text in I18N["zh_CN"].items()
    if isinstance(text, str) and text
}
I18N_TEXT_KEYS.update({
    "E2E Latency Distribution (e2el)": "section.e2e_distribution",
    "Max Tokens": "label.max_tokens",
    "Temperature": "label.temperature",
    "Stream": "label.stream",
})

# ---------- UI 样式常量 ----------
# Design: 企业级中性观感 —— 见 docs/uiux-v2/DESIGN_SPEC.md。
#         令牌权威实现在 llm_benchmark_app/ui_theme.py（TOKENS/TYPE/SPACE）；
#         此处保留同名键以兼容既有调用点，全部由 ui_theme 派生，勿再写死十六进制。
from llm_benchmark_app import ui_theme as _ui_theme

_TOK = _ui_theme.TOKENS
_SPC = _ui_theme.SPACE
_FONT_MONO = _ui_theme.FONT_MONO

C_STYLE = {
    # ── surfaces ──
    "bg_main": _TOK["bg_main"],
    "bg_card": _TOK["bg_card"],
    "bg_header": _TOK["bg_header"],
    "bg_input": _TOK["bg_input"],
    "bg_inset": _TOK["bg_inset"],
    "bg_hover": _TOK["bg_hover"],
    "bg_stripe": _TOK["bg_stripe"],
    # ── text ──
    "text_primary": _TOK["text_primary"],
    "text_secondary": _TOK["text_secondary"],
    "text_muted": _TOK["text_muted"],
    "text_inverse": _TOK["text_inverse"],
    "text_disabled": _TOK["text_disabled"],
    # ── borders ──
    "border": _TOK["border"],
    "border_light": _TOK["border_light"],
    "border_strong": _TOK["border_strong"],
    "border_focus": _TOK["border_focus"],
    # ── accent（极算门品牌蓝）──
    "accent": _TOK["accent"],
    "accent_hover": _TOK["accent_hover"],
    "accent_pressed": _TOK["accent_pressed"],
    "accent_light": _TOK["accent_light"],
    "accent_soft": _TOK["accent_soft"],
    # ── semantic ──
    "success": _TOK["success"],
    "success_bg": _TOK["success_bg"],
    "success_text": _TOK["success_text"],
    "warning": _TOK["warning"],
    "warning_bg": _TOK["warning_bg"],
    "warning_text": _TOK["warning_text"],
    "error": _TOK["error"],
    "error_bg": _TOK["error_bg"],
    "error_text": _TOK["error_text"],
    "info": _TOK["info"],
    "info_bg": _TOK["info_bg"],
    "info_text": _TOK["info_text"],
    "neutral": _TOK["neutral"],
    "neutral_bg": _TOK["neutral_bg"],
    "neutral_text": _TOK["neutral_text"],
    # ── 图表系列色（仅图表 identity，不做装饰）──
    "series_1": _TOK["series_1"],
    "series_2": _TOK["series_2"],
    "series_3": _TOK["series_3"],
    "series_4": _TOK["series_4"],
    "series_baseline": _TOK["series_baseline"],
    # ── semantic aliases (backward-compat) ──
    "text_accent": _TOK["accent_text"],
    # ── fonts ──
    "font_title": _ui_theme.TYPE["metric_lg"],
    "font_subtitle": _ui_theme.TYPE["meta"],
    "font_section": _ui_theme.TYPE["section"],
    "font_label": _ui_theme.TYPE["label"],
    "font_body": _ui_theme.TYPE["body"],
    "font_status": _ui_theme.TYPE["metric_md"],
    "font_metric": _ui_theme.TYPE["metric_num"],
    "font_small": _ui_theme.TYPE["caption"],
    "font_meta": _ui_theme.TYPE["meta"],
    "font_num": _ui_theme.TYPE["num"],
    "font_code": (_FONT_MONO, 10),
    # ── spacing（4/8 节奏）──
    "radius_card": _ui_theme.RADIUS["card"],
    "radius_btn": _ui_theme.RADIUS["btn"],
    "radius_input": _ui_theme.RADIUS["input"],
    "pad_lg": _SPC["xl"],
    "pad_md": _SPC["lg"],
    "pad_sm": _SPC["sm"],
    "gap_lg": _SPC["xl"],
    "gap_md": _SPC["md"],
    "gap_sm": _SPC["sm"],
    # ── accent bars ──
    "bar_width": 3,
}
class ScrollableFrame(tk.Frame):
    """A scrollable container that can hold any content.
    Mousewheel scrolling auto-binds on enter/leave."""
    def __init__(self, parent, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0,
                                bg=C_STYLE["bg_main"])
        self.scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.content = tk.Frame(self.canvas, bg=C_STYLE["bg_main"])
        self.window_id = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.content.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            self.window_id, width=e.width))
        self._bind_mousewheel()
    def _bind_mousewheel(self):
        def _mw(event):
            try:
                if hasattr(event, 'delta'):
                    self.canvas.yview_scroll(int(-event.delta / 120), "units")
                elif event.num == 4:
                    self.canvas.yview_scroll(-1, "units")
                elif event.num == 5:
                    self.canvas.yview_scroll(1, "units")
            except Exception:
                pass
        self.canvas.bind("<Enter>", lambda e: (
            self.canvas.bind_all("<MouseWheel>", _mw),
            self.canvas.bind_all("<Button-4>", _mw),
            self.canvas.bind_all("<Button-5>", _mw)))
        self.canvas.bind("<Leave>", lambda e: (
            self.canvas.unbind_all("<MouseWheel>"),
            self.canvas.unbind_all("<Button-4>"),
            self.canvas.unbind_all("<Button-5>")))
class SectionCard(tk.Frame):
    """卡片容器：白色背景 + 1px浅灰边框 + 标题分隔线 + 内边距
    Supports collapsible mode: click title to toggle content visibility."""

    def __init__(self, parent, title: str = "",
                 collapsible: bool = False, expanded: bool = True, **kw):
        # Extract our params from kw so they don't propagate to tk.Frame
        super().__init__(parent, bg=C_STYLE["bg_card"],
                         highlightbackground=C_STYLE["border"],
                         highlightthickness=1, bd=0, **kw)
        self._title = title
        self._collapsible = collapsible
        self._expanded = expanded
        self._sep = None       # separator frame
        self._hdr = None       # header frame
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self._build()

    def _build(self):
        inner = tk.Frame(self, bg=C_STYLE["bg_card"])
        inner.grid(row=0, column=0, sticky="nsew",
                   padx=C_STYLE["pad_lg"], pady=C_STYLE["pad_lg"])
        inner.columnconfigure(0, weight=1)
        inner.rowconfigure(0, weight=0)
        inner.rowconfigure(2, weight=1)
        if self._title:
            self._hdr = tk.Frame(inner, bg=C_STYLE["bg_card"])
            self._hdr.grid(row=0, column=0, sticky="ew", pady=(0, C_STYLE["pad_md"]))
            self.title_lbl = ttk.Label(self._hdr, text=self._title,
                                       style="Section.TLabel")
            self.title_lbl.pack(side=tk.LEFT)
            self._sep = tk.Frame(inner, height=1, bg=C_STYLE["border"])
            self._sep.grid(row=1, column=0, sticky="ew", pady=(0, C_STYLE["pad_md"]))
            self.content = tk.Frame(inner, bg=C_STYLE["bg_card"])
            self.content.grid(row=2, column=0, sticky="nsew")

            # Collapsible behavior
            if self._collapsible:
                self.title_lbl.configure(cursor="hand2")
                self.title_lbl.bind("<Button-1>", lambda e: self.toggle())
                self._apply_state()
        else:
            self.content = tk.Frame(inner, bg=C_STYLE["bg_card"])
            self.content.grid(row=0, column=0, sticky="nsew")
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(0, weight=1)

    def _apply_state(self):
        """Show or hide content based on _expanded state."""
        if not self._collapsible or not self._title:
            return
        prefix = "▼ " if self._expanded else "▶ "
        self.title_lbl.config(text=prefix + self._title)
        if self._sep:
            self._sep.grid() if self._expanded else self._sep.grid_remove()
        self.content.grid() if self._expanded else self.content.grid_remove()

    def toggle(self):
        """Toggle the collapsed/expanded state. Safe to call from any thread
        via root.after if needed."""
        if not self._collapsible:
            return
        self._expanded = not self._expanded
        self._apply_state()

    def expand(self):
        """Expand the section (show content)."""
        if not self._collapsible:
            return
        self._expanded = True
        self._apply_state()

    def collapse(self):
        """Collapse the section (hide content)."""
        if not self._collapsible:
            return
        self._expanded = False
        self._apply_state()
class MetricItem(tk.Frame):
    """指标卡片：彩色左边条 + 大数值优先 + 小标签在下（Datadog风格）+ hover tooltip"""
    # 语义色只用于表达「健康 / 异常」；性能类指标统一品牌色调（规范 §2.3：禁彩虹色、禁语义误用）
    COLORS = {
        "ttft": C_STYLE["accent"], "visible_ttft": C_STYLE["accent"],
        "tpot": C_STYLE["accent"], "itl": C_STYLE["accent"],
        "e2e_p95": C_STYLE["accent"],
        "system_output_tps": C_STYLE["accent"], "tps": C_STYLE["accent"],
        "agg_tps": C_STYLE["accent"], "rps": C_STYLE["accent"],
        "total_tokens": C_STYLE["accent"], "output_tokens": C_STYLE["accent"],
        "success_rate": C_STYLE["success"],
    }
    # ── tooltip definitions ──
    TOOLTIPS = {
        "ttft": "TTFT / First Stream Chunk：请求发出到首个流式响应 chunk 到达时间；\n用于对齐 vLLM bench serve ttft。",
        "visible_ttft": "FVT / First Visible Token：请求发出到首个非空\ndelta.content / reasoning_content；用于衡量用户首字体验。",
        "e2e_p95": "E2E P95：请求发出到完整响应结束的 P95 延迟。",
        "system_output_tps": "Output TPS：total_output_tokens / duration_sec，\n只统计输出 token。",
        "rps": "RPS：success / duration_sec。",
        "tpot": "TPOT：(E2E - TTFT) / (completion_tokens - 1)。",
        "itl": "ITL：相邻流式输出 chunk/token 间隔；\n当前按 SSE chunk 估算。",
        "success_rate": "Success Rate：success / total_requests。",
    }
    def __init__(self, parent, label: str, value: str = "—", metric_key: str = "", **kw):
        super().__init__(parent, bg=C_STYLE["bg_card"],
                         highlightbackground=C_STYLE["border"],
                         highlightthickness=1, bd=0, **kw)
        self._label = label
        self._value = value
        self._key = metric_key
        self._bar_color = self.COLORS.get(metric_key, C_STYLE["accent"])
        self._tooltip_text = self.TOOLTIPS.get(metric_key, "")
        self._tooltip_win = None
        self._tooltip_after = None
        self._build()
    def _build(self):
        # left accent bar
        bar = tk.Frame(self, bg=self._bar_color, width=C_STYLE["bar_width"])
        bar.pack(side=tk.LEFT, fill=tk.Y)
        bar.pack_propagate(False)
        # content area
        inner = tk.Frame(self, bg=C_STYLE["bg_card"])
        inner.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                   padx=C_STYLE["pad_md"], pady=C_STYLE["pad_md"])
        # large metric number — first, prominent
        self.val_lbl = tk.Label(inner, text=self._value,
                                font=C_STYLE["font_metric"],
                                bg=C_STYLE["bg_card"],
                                fg=C_STYLE["text_primary"],
                                anchor="w")
        self.val_lbl.pack(fill=tk.X)
        # small label — below
        lbl = tk.Label(inner, text=self._label, font=C_STYLE["font_small"],
                 bg=C_STYLE["bg_card"],
                 fg=C_STYLE["text_secondary"],
                 anchor="w")
        lbl.pack(fill=tk.X)
        # ── tooltip hover ──
        if self._tooltip_text:
            for w in (self, inner, self.val_lbl, lbl, bar):
                w.bind("<Enter>", self._show_tooltip)
                w.bind("<Leave>", self._hide_tooltip)
    def _show_tooltip(self, event=None):
        if self._tooltip_win:
            return
        self._tooltip_win = tw = tk.Toplevel(self)
        tw.wm_overrideredirect(True)
        tw.configure(bg=C_STYLE["bg_card"], highlightbackground=C_STYLE["border"],
                     highlightthickness=1, bd=0)
        lbl = tk.Label(tw, text=self._tooltip_text, font=C_STYLE["font_small"],
                       bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"],
                       justify=tk.LEFT, anchor="w",
                       padx=C_STYLE["pad_sm"], pady=C_STYLE["pad_sm"])
        lbl.pack()
        # position below the widget（留 18px 间隙：提示窗压在指针下会导致 <Leave> 永不触发，提示常驻）
        tw.update_idletasks()
        x = self.winfo_rootx() + 4
        y = self.winfo_rooty() + self.winfo_height() + 18
        tw.geometry(f"+{x}+{y}")
        # 兜底：6s 后自动隐藏 + 点击即隐藏
        self._tooltip_after = self.after(6000, self._hide_tooltip)
        tw.bind("<Button-1>", self._hide_tooltip)
    def _hide_tooltip(self, event=None):
        after_id = getattr(self, "_tooltip_after", None)
        if after_id:
            try:
                self.after_cancel(after_id)
            except Exception:
                pass
            self._tooltip_after = None
        if self._tooltip_win:
            self._tooltip_win.destroy()
            self._tooltip_win = None
    def set_value(self, value: str):
        self._value = value
        self.val_lbl.config(text=value)
class StatusCard(tk.Frame):
    """状态卡片：左侧彩色指示条 + 大号状态值 + 小标题（线性风格）"""
    BAR_COLORS = {"idle": "#CBD5E1", "checking": C_STYLE["warning"],
                  "pass": C_STYLE["success"], "fail": C_STYLE["error"]}
    def __init__(self, parent, title: str, **kw):
        super().__init__(parent, bg=C_STYLE["bg_card"],
                         highlightbackground=C_STYLE["border"],
                         highlightthickness=1, bd=0, **kw)
        self._title = title
        self._build()
    def _build(self):
        # left status bar — colored by state
        self._bar = tk.Frame(self, bg=self.BAR_COLORS["idle"], width=C_STYLE["bar_width"])
        self._bar.pack(side=tk.LEFT, fill=tk.Y)
        self._bar.pack_propagate(False)
        # content
        inner = tk.Frame(self, bg=C_STYLE["bg_card"])
        inner.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                   padx=C_STYLE["pad_md"], pady=C_STYLE["pad_md"])
        self.title_lbl = tk.Label(inner, text=self._title, font=C_STYLE["font_small"],
                                  bg=C_STYLE["bg_card"], fg=C_STYLE["text_secondary"],
                                  anchor="w")
        self.title_lbl.pack(fill=tk.X)
        self.val_lbl = tk.Label(inner, text="等待中",
                                font=C_STYLE["font_status"],
                                bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"],
                                anchor="w")
        self.val_lbl.pack(fill=tk.X)
    def set_state(self, state: str, detail: str = ""):
        self._bar.configure(bg=self.BAR_COLORS.get(state, "#CBD5E1"))
        self.val_lbl.config(text=detail if detail else state)
class NoticeBanner(tk.Frame):
    """诊断/提示横幅：浅色背景 + 左侧色条 + 1px边框"""
    COLORS = {"info": C_STYLE["info"], "warn": C_STYLE["warning"],
              "error": C_STYLE["error"], "success": C_STYLE["success"]}
    BG = {"info": C_STYLE["info_bg"], "warn": C_STYLE["warning_bg"],
          "error": C_STYLE["error_bg"], "success": C_STYLE["success_bg"]}
    def __init__(self, parent, level: str = "info", **kw):
        super().__init__(parent, bg=C_STYLE["bg_card"],
                         highlightbackground=C_STYLE["border"],
                         highlightthickness=1, bd=0, **kw)
        self._level = level
        self.columnconfigure(1, weight=1)
        self._build()
    def _build(self):
        c = self.COLORS.get(self._level, C_STYLE["accent"])
        bg = self.BG.get(self._level, C_STYLE["info_bg"])
        bar = tk.Frame(self, bg=c, width=4, height=24)
        bar.grid(row=0, column=0, sticky="ns", padx=(C_STYLE["pad_lg"], C_STYLE["pad_sm"]),
                 pady=C_STYLE["pad_sm"])
        bar.grid_propagate(False)
        self.text_lbl = tk.Label(self, text="", font=C_STYLE["font_body"],
                                 bg=bg, fg=C_STYLE["text_primary"],
                                 wraplength=420, justify=tk.LEFT,
                                 anchor="w")
        self.text_lbl.grid(row=0, column=1, sticky="w",
                          padx=(0, C_STYLE["pad_lg"]), pady=C_STYLE["pad_sm"])
        # 折行宽度跟随横幅实际宽度（原先固定 420px，在 1300px 卡片里被折成 3 行且标点孤立）
        self.bind("<Configure>",
                  lambda e: self.text_lbl.config(wraplength=max(e.width - 72, 280)))
    def set_text(self, text: str):
        self.text_lbl.config(text=text)
        if text:
            self.grid()
        else:
            self.grid_remove()

# ============================================================
# GUI
# ============================================================
class LLMBenchmarkApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"JISUMEN LLM Benchmark GUI v{APP_VERSION}")
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        # 规范 §5：窗口可缩放、布局自适应；默认尺寸按屏幕取合理区间，
        # 不再随屏幕等比缩小（旧实现 1099/1920 比例在 1366×768 上会挤成一团）。
        w = max(1180, min(1440, int(sw * 0.78)))
        h = max(760, min(960, int(sh * 0.88)))
        self.root.geometry(f"{w}x{h}")
        self.root.resizable(True, True)
        self.root.minsize(1100, 700)
        self.root.configure(bg=C_STYLE["bg_main"])
        self._benchmark_running = False
        self._sweep_running = False
        self._active_run_type = None
        self._run_lock = threading.Lock()
        self._smoke_latency = 0.0
        self._run_started_at = None
        self._run_completed = 0
        self._run_total = 0
        self._run_fail = 0
        self._run_phase = ""
        self._run_current_label = ""
        self._spinner_index = 0
        self._spinner_after_id = None
        self._icon_pulse_id = None
        self._latest_e2e_latencies = []
        self._hist_redraw_after_id = None
        # ── progress overlay ──
        self.progress_overlay = None
        self.progress_overlay_canvas = None  # holds the ttk.Progressbar widget
        self.progress_overlay_label = None
        self.progress_overlay_detail_var = tk.StringVar()
        self.progress_overlay_anim_index = 0
        self.progress_overlay_after_id = None
        self.progress_overlay_running = False
        # Environment parameter read-back tracking
        self._applied_environment_params = False
        self._applied_fields_json: list = []
        self._overridden_fields_json: list = []
        # ── History navigation state ──
        self.history_item_records: dict = {}   # iid → ref dict
        self.history_display_refs: list = []   # ordered for prev/next nav
        self.history_detail_window = None      # active detail Toplevel
        self.history_detail_current_index = None
        # ── Sweep artifact paths for PDF generation ──
        self._current_sweep_json_path: str = ""
        self._current_sweep_md_path: str = ""
        self._current_sweep_png_path: str = ""
        # ── High-concurrency sweep runtime state (written by HC runner, read by poll) ──
        self.sweep_status_queue: queue.Queue = queue.Queue(maxsize=1000)
        self.sweep_runtime_state: dict = {
            "running": False,
            "case_index": 0,
            "case_total": 0,
            "current_concurrency": None,
            "completed_requests": 0,
            "total_requests": 0,
            "success": 0,
            "fail": 0,
            "output_tokens": 0,
            "elapsed_sec": 0.0,
            "rolling_output_tps": None,
            "rolling_rps": None,
            "last_error_summary": None,
        }
        self.sweep_ui_poll_after_id = None
        self._sweep_hc_mode: bool = False  # True when max(concurrency) > 256
        # ── Token calibration state (TASK-LLM-BENCHMARK-TOKEN-CALIBRATION-HC-RUNNER-001-v1) ──
        self._actual_prompt_tokens: int = 0
        self._calibration_method: str = ""
        self._calibrated_user_prompt: str = ""
        self._calibrated_system_prompt: str = ""
        self._target_input_tokens: int = 1024
        self._target_output_tokens: int = 2048
        self._prompt_mode: str = "fixed"  # fixed | same_length_variants | synthetic_random
        self.lang_code = self._load_language_config()
        self.language_var = tk.StringVar(
            value=I18N[self.lang_code]["language.en"]
            if self.lang_code == "en_US" else I18N["zh_CN"]["language.zh"])
        self._i18n_widgets = []
        self._i18n_callbacks = []
        init_db()
        # Initialize result DB and seed default profiles (non-blocking, best-effort)
        try:
            _init_result_db(RESULT_DB_PATH)
        except Exception:
            pass
        self._setup_styles()
        self._build_header()
        self._build_body()
        self._build_statusbar()
        self._load_config()
        self._refresh_ui_language()
        # 配置载入后再刷新新页面（总览/对比/报告），确保显示的是已保存配置而非默认值
        try:
            self._refresh_side_pages()
        except Exception:
            pass

    def tr(self, key, default=None):
        return I18N.get(self.lang_code, I18N["zh_CN"]).get(
            key, default if default is not None else key)

    def _load_language_config(self):
        cfg = ConfigParser()
        try:
            cfg.read(INI_PATH, encoding="utf-8")
            lang = cfg.get("ui", "language", fallback="zh_CN")
        except Exception:
            lang = "zh_CN"
        return lang if lang in I18N else "zh_CN"

    def _save_language_config(self):
        cfg = ConfigParser()
        try:
            cfg.read(INI_PATH, encoding="utf-8")
        except Exception:
            pass
        if not cfg.has_section("ui"):
            cfg.add_section("ui")
        cfg.set("ui", "language", self.lang_code)
        with open(INI_PATH, "w", encoding="utf-8") as f:
            cfg.write(f)

    def _register_i18n_widget(self, widget, key, attr="text"):
        self._i18n_widgets.append((widget, key, attr))
        try:
            widget.configure(**{attr: self.tr(key)})
        except Exception:
            pass
        return widget

    def _register_i18n_callback(self, callback):
        self._i18n_callbacks.append(callback)
        return callback

    def _bind_existing_i18n_widgets(self, parent=None):
        parent = parent or self.root
        zh_reverse = I18N_TEXT_KEYS
        for child in parent.winfo_children():
            try:
                text = child.cget("text")
                key = getattr(child, "_i18n_key", None) or zh_reverse.get(text)
                if key:
                    child._i18n_key = key
                    child.configure(text=self.tr(key))
            except Exception:
                pass
            if isinstance(child, SectionCard):
                key = getattr(child, "_i18n_title_key", None) or zh_reverse.get(child._title)
                if key:
                    child._i18n_title_key = key
                    child._title = self.tr(key)
                    child._apply_state()
            self._bind_existing_i18n_widgets(child)

    def _set_language(self, lang_code):
        if lang_code not in I18N:
            return
        self.lang_code = lang_code
        desired = I18N[lang_code]["language.en"] if lang_code == "en_US" else I18N["zh_CN"]["language.zh"]
        if self.language_var.get() != desired:
            self.language_var.set(desired)
        self._save_language_config()
        self._refresh_ui_language()

    def _on_language_selected(self, event=None):
        display = self.language_var.get()
        lang_code = "en_US" if display == I18N["en_US"]["language.en"] else "zh_CN"
        self._set_language(lang_code)

    def _refresh_ui_language(self):
        self.root.title(self.tr("app.title"))
        self._bind_existing_i18n_widgets()
        for widget, key, attr in getattr(self, "_i18n_widgets", []):
            try:
                widget.configure(**{attr: self.tr(key)})
            except Exception:
                pass
        for cb in getattr(self, "_i18n_callbacks", []):
            try:
                cb()
            except Exception:
                pass
        if hasattr(self, "nb"):
            for frame, key in (
                (getattr(self, "overview_frame", None), "tab.overview"),
                (getattr(self, "settings_frame", None), "tab.settings"),
                (getattr(self, "bench_frame", None), "tab.benchmark"),
                (getattr(self, "sweep_frame", None), "tab.sweep"),
                (getattr(self, "history_frame", None), "tab.history"),
                (getattr(self, "export_frame", None), "tab.export"),
                (getattr(self, "env_profiles_frame", None), "tab.env_profiles"),
            ):
                if frame is None:
                    continue
                try:
                    self.nb.tab(frame, text=f"  {self.tr(key)}  ")
                except Exception:
                    pass
        self._refresh_history_headers()
        if hasattr(self, "hist_tree"):
            self._refresh_history()
        if hasattr(self, "history_detail_text") and not self.hist_tree.selection():
            self._set_history_detail_text(self.tr("status.select_history"))
        if hasattr(self, "_status_badge_lbl") and not (self._benchmark_running or getattr(self, "_sweep_running", False)):
            self._status_badge_lbl.config(text=self.tr("status.idle"))
        self._refresh_run_buttons()
        if hasattr(self, "hist_canvas"):
            self.root.after_idle(self._redraw_e2e_histogram)

    def _load_header_logo(self, path=None, max_height=28, max_width=160):
        paths = [
            path,
            os.path.join(_SCRIPT_DIR, "assets", "brand", "jisumen-mark.png"),
            os.path.join(_SCRIPT_DIR, "logo.png"),
            "/home/jisuman/logo.png",
        ]
        logo_path = next((p for p in paths if p and os.path.exists(p)), None)
        if not logo_path:
            return None
        try:
            from PIL import Image, ImageTk
            img = Image.open(logo_path)
            img.thumbnail((max_width, max_height), Image.LANCZOS)
            return ImageTk.PhotoImage(img)
        except Exception:
            try:
                img = tk.PhotoImage(file=logo_path)
                w = img.width()
                h = img.height()
                if w <= 0 or h <= 0:
                    return None
                scale_h = max(1, (h + max_height - 1) // max_height)
                scale_w = max(1, (w + max_width - 1) // max_width)
                scale = max(scale_h, scale_w)
                if scale > 1:
                    img = img.subsample(scale, scale)
                return img
            except Exception:
                return None
    # ---------- style ----------
    def _setup_styles(self):
        # 企业级中性观感由 ui_theme 统一实现（规范 §2），此处只做应用级补充，
        # 保证既有风格名（Primary/Secondary/App.* 等）继续可用。
        st = _ui_theme.apply_ttk_styles(self.root)
        st.configure("App.Treeview", rowheight=28, font=C_STYLE["font_body"],
                     background=C_STYLE["bg_card"], fieldbackground=C_STYLE["bg_card"],
                     foreground=C_STYLE["text_primary"])
        st.configure("App.Treeview.Heading", font=C_STYLE["font_label"],
                     background=C_STYLE["bg_inset"], foreground=C_STYLE["text_secondary"],
                     relief="flat", padding=(C_STYLE["pad_sm"], C_STYLE["pad_sm"]))
        st.map("App.Treeview",
               background=[("selected", C_STYLE["accent_light"])],
               foreground=[("selected", C_STYLE["text_primary"])])
    def _build_header(self):
        h = tk.Frame(self.root, bg=C_STYLE["bg_header"], height=56,
                     highlightbackground=C_STYLE["border"],
                     highlightthickness=1, bd=0)
        h.pack(fill=tk.X, side=tk.TOP)
        h.pack_propagate(False)
        inner = tk.Frame(h, bg=C_STYLE["bg_header"])
        inner.pack(fill=tk.BOTH, expand=True,
                   padx=(C_STYLE["pad_lg"], 0), pady=C_STYLE["pad_sm"])
        left = tk.Frame(inner, bg=C_STYLE["bg_header"])
        left.pack(side=tk.LEFT)
        # icon + title in one line
        title_row = tk.Frame(left, bg=C_STYLE["bg_header"])
        title_row.pack(anchor="w")
        self.logo_image = self._load_header_logo(max_height=22, max_width=170)
        if self.logo_image is not None:
            icon_lbl = tk.Label(title_row, image=self.logo_image,
                                bg=C_STYLE["bg_header"])
            icon_lbl.pack(side=tk.LEFT, padx=(0, 10))
            self._icon_lbl = None
        else:
            # 极算门字标（文本降级，不使用 emoji 图标 —— 规范 §3）
            icon_lbl = tk.Label(title_row, text="JISUMEN", font=(FONT_FAMILY, 15, "bold"),
                                bg=C_STYLE["bg_header"], fg=C_STYLE["accent"])
            icon_lbl.pack(side=tk.LEFT, padx=(0, 8))
            self._icon_lbl = icon_lbl
        title_lbl = ttk.Label(title_row, text=self.tr("app.title"), style="Title.TLabel")
        title_lbl.pack(side=tk.LEFT)
        self._register_i18n_widget(title_lbl, "app.title")
        subtitle_lbl = ttk.Label(left, text=self.tr("app.subtitle"), style="Subtitle.TLabel")
        subtitle_lbl.pack(anchor="w")
        self._register_i18n_widget(subtitle_lbl, "app.subtitle")
        right = tk.Frame(inner, bg=C_STYLE["bg_header"])
        right.pack(side=tk.RIGHT)
        lang_frame = tk.Frame(right, bg=C_STYLE["bg_header"])
        lang_frame.pack(side=tk.RIGHT, padx=(C_STYLE["pad_sm"], 0))
        lang_lbl = tk.Label(lang_frame, text=self.tr("language.label"),
                            font=C_STYLE["font_small"],
                            bg=C_STYLE["bg_header"], fg=C_STYLE["text_secondary"])
        lang_lbl.pack(side=tk.LEFT, padx=(0, 6))
        self._register_i18n_widget(lang_lbl, "language.label")
        self.language_combo = ttk.Combobox(
            lang_frame, textvariable=self.language_var,
            values=[I18N["zh_CN"]["language.zh"], I18N["en_US"]["language.en"]],
            width=10, state="readonly")
        self.language_combo.pack(side=tk.LEFT)
        self.language_combo.bind("<<ComboboxSelected>>", self._on_language_selected)
        # status pill (fixed min-width to prevent overflow)
        pill = tk.Frame(right, bg=C_STYLE["bg_stripe"], highlightbackground=C_STYLE["border"],
                        highlightthickness=1, bd=0, width=360)
        pill.pack(side=tk.RIGHT, padx=(C_STYLE["pad_sm"], 0))
        pill.pack_propagate(False)  # lock width
        pill_inner = tk.Frame(pill, bg=C_STYLE["bg_stripe"])
        pill_inner.pack(padx=C_STYLE["pad_sm"], pady=3, fill=tk.X)
        self._status_dot = tk.Label(pill_inner, text=" ●", font=(FONT_FAMILY, 9),
                                    bg=C_STYLE["bg_stripe"], fg=C_STYLE["text_muted"])
        self._status_dot.pack(side=tk.LEFT)
        self._status_badge_lbl = tk.Label(pill_inner, text="空闲",
                                         font=C_STYLE["font_small"],
                                         bg=C_STYLE["bg_stripe"],
                                         fg=C_STYLE["text_secondary"])
        self._status_badge_lbl.pack(side=tk.LEFT)
    def _build_body(self):
        body = tk.Frame(self.root, bg=C_STYLE["bg_main"])
        body.pack(fill=tk.BOTH, expand=True, side=tk.TOP,
                  padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["pad_lg"]))
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        self.nb = ttk.Notebook(body, style="App.TNotebook")
        self.nb.grid(row=0, column=0, sticky="nsew")
        # 页面顺序即用户动线：总览 → 新建测试 → 测试与结果 → 并发扫测 → 历史对比 → 报告与证据 → 环境档案
        self.overview_frame = tk.Frame(self.nb, bg=C_STYLE["bg_main"])
        self.settings_frame = tk.Frame(self.nb, bg=C_STYLE["bg_main"])
        self.bench_frame = tk.Frame(self.nb, bg=C_STYLE["bg_main"])
        self.sweep_frame = tk.Frame(self.nb, bg=C_STYLE["bg_main"])
        self.history_frame = tk.Frame(self.nb, bg=C_STYLE["bg_main"])
        self.export_frame = tk.Frame(self.nb, bg=C_STYLE["bg_main"])
        self.env_profiles_frame = tk.Frame(self.nb, bg=C_STYLE["bg_main"])
        self.nb.add(self.overview_frame, text="  总览  ")
        self.nb.add(self.settings_frame, text="  新建测试  ")
        self.nb.add(self.bench_frame, text="  测试与结果  ")
        self.nb.add(self.sweep_frame, text="  并发扫测  ")
        self.nb.add(self.history_frame, text="  历史对比  ")
        self.nb.add(self.export_frame, text="  报告与证据  ")
        self.nb.add(self.env_profiles_frame, text="  环境档案  ")
        self._build_overview_tab()
        self._build_settings_tab()
        self._build_results_tab()
        self._build_sweep_tab()
        self._build_history_tab()
        self._build_export_tab()
        self._build_env_profiles_tab()
        self._bind_shortcuts()

    def _bind_shortcuts(self):
        """键盘可达性（规范 §5）：Ctrl+1..7 切页、F5 刷新当前数据、Esc 关闭浮层。"""
        for i in range(1, self.nb.index("end") + 1):
            self.root.bind_all(f"<Control-Key-{i}>",
                               lambda e, idx=i - 1: self._select_tab(idx))
        self.root.bind_all("<F5>", lambda e: self._shortcut_refresh())
        self.root.bind_all("<Escape>", lambda e: self._shortcut_escape())

    def _select_tab(self, idx: int):
        try:
            if idx < self.nb.index("end"):
                self.nb.select(idx)
                self.nb.focus_set()
                for cb in getattr(self, "_tab_change_callbacks", []):
                    cb(idx)
        except Exception:
            pass
        return "break"

    def _shortcut_refresh(self):
        """F5：刷新当前页可见数据（历史/总览/报告）。"""
        for name in ("_refresh_history", "_refresh_overview", "_refresh_export"):
            fn = getattr(self, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
        return "break"

    def _shortcut_escape(self):
        """Esc：关闭最上层浮层（详情窗/对话框），不关闭主窗口。"""
        try:
            for w in reversed(self.root.winfo_children()):
                if isinstance(w, tk.Toplevel) and w.winfo_exists():
                    w.destroy()
                    return "break"
        except Exception:
            pass
        return "break"

    def _build_overview_tab(self):
        from llm_benchmark_app import ui_pages
        ui_pages.build_overview_tab(self, self.overview_frame)

    def _build_export_tab(self):
        from llm_benchmark_app import ui_pages
        ui_pages.build_export_tab(self, self.export_frame)
    def _build_settings_tab(self):
        sf = self.settings_frame
        sf.grid_columnconfigure(0, weight=1)
        sf.grid_rowconfigure(0, weight=1)

        canvas = tk.Canvas(sf, bg=C_STYLE["bg_main"], highlightthickness=0)
        scroll = ttk.Scrollbar(sf, orient=tk.VERTICAL, command=canvas.yview)
        inner = tk.Frame(canvas, bg=C_STYLE["bg_main"])
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw", tags="inner")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Bind canvas resize to update inner window width
        def _on_canvas_resize(event):
            canvas.itemconfig("inner", width=event.width)
        canvas.bind("<Configure>", _on_canvas_resize, add="+")

        # Enable mousewheel scrolling
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        col = tk.Frame(inner, bg=C_STYLE["bg_main"])
        col.pack(fill=tk.X, padx=C_STYLE["pad_lg"], pady=C_STYLE["pad_lg"])

        # ── Environment selector (top of settings) ──
        env_card = tk.Frame(col, bg=C_STYLE["bg_card"],
                            highlightbackground=C_STYLE["border"],
                            highlightthickness=1, bd=0)
        env_card.pack(fill=tk.X, pady=(0, C_STYLE["gap_lg"]))
        env_inner = tk.Frame(env_card, bg=C_STYLE["bg_card"])
        env_inner.pack(fill=tk.X, padx=C_STYLE["pad_lg"], pady=C_STYLE["pad_md"])
        tk.Label(env_inner, text=self.tr("env.selector_label"),
                 font=C_STYLE["font_body"], bg=C_STYLE["bg_card"],
                 fg=C_STYLE["text_primary"]).pack(side=tk.LEFT,
                 padx=(0, C_STYLE["pad_sm"]))
        self.env_profile_var = tk.StringVar(value="")
        self._env_profile_cb = ttk.Combobox(env_inner, textvariable=self.env_profile_var,
                                             values=[], width=30, state="readonly")
        self._env_profile_cb.pack(side=tk.LEFT)
        ttk.Button(env_inner, text=self.tr("env.manage_btn"),
                   style="Secondary.TButton",
                   command=lambda: self.nb.select(self.env_profiles_frame)
                   ).pack(side=tk.LEFT, padx=(C_STYLE["pad_sm"], 0))
        ttk.Button(env_inner, text=self.tr("env.read_params_btn"),
                   style="Secondary.TButton",
                   command=self._read_selected_environment_params
                   ).pack(side=tk.LEFT, padx=(C_STYLE["pad_sm"], 0))

        card_a = SectionCard(col, "API 配置", collapsible=True, expanded=True)
        card_a.pack(fill=tk.X, pady=(0, C_STYLE["gap_lg"]))
        self.url_var = tk.StringVar(value="http://192.168.1.12:8000/v1")
        self._labeled_input(card_a.content, "API 地址", self.url_var, 0, width=40)
        self.key_var = tk.StringVar(value="change-me-before-production")
        kf = tk.Frame(card_a.content, bg=C_STYLE["bg_card"])
        kf.grid(row=1, column=1, sticky="ew", pady=(0, C_STYLE["gap_md"]))
        tk.Label(card_a.content, text="API 密钥", font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"]).grid(
            row=1, column=0, sticky="w",
            padx=(0, C_STYLE["pad_sm"]), pady=(0, C_STYLE["gap_md"]))
        self.key_entry = ttk.Entry(kf, textvariable=self.key_var, width=28, show="*")
        self.key_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(kf, text="显示", variable=self.show_key,
                        command=self._toggle_key_visibility).pack(side=tk.LEFT, padx=4)
        self.model_var = tk.StringVar(value="qwen3.5-122b-a10b-fp8")
        ttk.Label(card_a.content, text="模型名称（选填）", style="Body.TLabel",
                  background=C_STYLE["bg_card"]).grid(
            row=2, column=0, sticky="w",
            padx=(0, C_STYLE["pad_sm"]), pady=(0, C_STYLE["gap_sm"]))
        mf = tk.Frame(card_a.content, bg=C_STYLE["bg_card"])
        mf.grid(row=2, column=1, sticky="ew", pady=(0, C_STYLE["gap_md"]))
        mf.columnconfigure(0, weight=1)
        ttk.Entry(mf, textvariable=self.model_var, width=36).grid(row=0, column=0, sticky="ew")
        ttk.Button(mf, text="查询", command=self._on_query_models).grid(
            row=0, column=1, padx=(C_STYLE["pad_sm"], 0))
        self.system_var = tk.StringVar(value="你是一个有帮助的助手。")
        self._labeled_text(card_a.content, "系统提示词", self.system_var, 3, height=2)
        # Store stable reference so Apply can update the widget directly
        self._api_system_prompt_widget = self._text_widgets.get("系统提示词")
        self.prompt_var = tk.StringVar(value="请用300字左右介绍机器学习。")
        self._labeled_text(card_a.content, "用户提示词", self.prompt_var, 4, height=2)
        # Store stable reference so Apply can update the widget directly
        self._api_user_prompt_widget = self._text_widgets.get("用户提示词")
        card_b = SectionCard(col, "测试参数", collapsible=True, expanded=True)
        card_b.pack(fill=tk.X, pady=(0, C_STYLE["gap_lg"]))
        self._build_test_params(card_b.content)

        # ── 输入 Token 校准 (Input Token Calibration) ────────────────────────────
        card_token_calib = SectionCard(col, self.tr("section.token_calibration"),
                                       collapsible=True, expanded=False)
        card_token_calib.pack(fill=tk.X, pady=(0, C_STYLE["gap_lg"]))
        self._build_token_calib_section(card_token_calib.content)

        card_c = SectionCard(col, "操作", collapsible=True, expanded=True)
        card_c.pack(fill=tk.X)
        btn_row = tk.Frame(card_c.content, bg=C_STYLE["bg_card"])
        btn_row.pack(fill=tk.X, pady=(0, C_STYLE["gap_sm"]))
        self.start_btn = ttk.Button(btn_row, text="开始基准测试",
                                    style="Primary.TButton",
                                    command=self._start_benchmark)
        self.start_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(btn_row, text="保存配置", style="Secondary.TButton",
                   command=self._save_config).pack(side=tk.LEFT, fill=tk.X,
                   expand=True, padx=(C_STYLE["pad_sm"], 0))
        ttk.Button(btn_row, text="重置配置", style="Secondary.TButton",
                   command=self._reset_config).pack(side=tk.LEFT, fill=tk.X,
                   expand=True, padx=(C_STYLE["pad_sm"], 0))
        self._action_status = tk.Label(card_c.content,
                                       text="就绪 — 请配置参数后开始测试",
                                       font=C_STYLE["font_small"],
                                       bg=C_STYLE["bg_card"],
                                       fg=C_STYLE["text_secondary"])
        self._action_status.pack(anchor="w", pady=(C_STYLE["pad_sm"], 0))
        self.progress = ttk.Progressbar(card_c.content, mode="determinate",
                                        style="Accent.Horizontal.TProgressbar")
        self.progress.pack(fill=tk.X, pady=(C_STYLE["pad_sm"], 0))
    def _build_test_params(self, parent):
        """Build test parameters area with 3 logical groups: 生成参数, 负载参数, 保存选项."""
        LABEL_W = 14  # uniform label width
        SPIN_W = 8    # uniform spinbox width

        def _lbl(pr, text, row, col, **kw):
            tk.Label(pr, text=text, font=C_STYLE["font_body"], width=LABEL_W,
                     bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"],
                     anchor="w").grid(row=row, column=col, sticky="w", **kw)

        # ═══ Group 1: 生成参数 ═══
        gen = ttk.LabelFrame(parent, text="生成参数", padding=C_STYLE["pad_md"])
        gen.pack(fill=tk.X, pady=(0, C_STYLE["gap_md"]))
        gen.columnconfigure(1, weight=1)
        gen.columnconfigure(3, weight=1)

        self.max_tokens_var = tk.IntVar(value=512)
        _lbl(gen, "最大 Token 数", 0, 0, padx=(0, C_STYLE["pad_sm"]), pady=(C_STYLE["gap_sm"], 0))
        _max_tok_frame = tk.Frame(gen, bg=C_STYLE["bg_card"])
        _max_tok_frame.grid(row=0, column=1, sticky="w", pady=(C_STYLE["gap_sm"], 0))
        ttk.Spinbox(_max_tok_frame, from_=16, to=8192, increment=16,
                    textvariable=self.max_tokens_var, width=SPIN_W).pack(side=tk.LEFT)
        # Max token quick-select buttons: 512 / 1024 / 2048
        for _qtok in (512, 1024, 2048):
            ttk.Button(_max_tok_frame, text=str(_qtok), width=5,
                       command=lambda v=_qtok: self.max_tokens_var.set(v)
                       ).pack(side=tk.LEFT, padx=(2, 0))

        self.temp_var = tk.DoubleVar(value=0.0)
        _lbl(gen, "温度参数", 0, 2, padx=(C_STYLE["gap_lg"], C_STYLE["pad_sm"]), pady=(C_STYLE["gap_sm"], 0))
        ttk.Spinbox(gen, from_=0.0, to=2.0, increment=0.1,
                    textvariable=self.temp_var, width=SPIN_W).grid(
            row=0, column=3, sticky="w", pady=(C_STYLE["gap_sm"], 0))

        self.stream_var = tk.StringVar(value="是")
        _lbl(gen, "流式模式", 1, 0, padx=(0, C_STYLE["pad_sm"]), pady=(C_STYLE["gap_sm"], 0))
        ttk.Combobox(gen, textvariable=self.stream_var,
                     values=["是", "否"], width=10, state="readonly").grid(
            row=1, column=1, sticky="w", pady=(C_STYLE["gap_sm"], 0))

        self.output_length_mode_var = tk.StringVar(value="normal")
        output_mode_lbl = tk.Label(gen, text=self.tr("label.output_length_mode"),
                                   font=C_STYLE["font_body"], width=LABEL_W,
                                   bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"],
                                   anchor="w")
        output_mode_lbl.grid(row=2, column=0, sticky="w",
                             padx=(0, C_STYLE["pad_sm"]), pady=(C_STYLE["gap_sm"], 0))
        self._register_i18n_widget(output_mode_lbl, "label.output_length_mode")
        output_mode_frame = tk.Frame(gen, bg=C_STYLE["bg_card"])
        output_mode_frame.grid(row=2, column=1, columnspan=3, sticky="w",
                               pady=(C_STYLE["gap_sm"], 0))
        normal_radio = ttk.Radiobutton(
            output_mode_frame,
            text=self.tr("label.output_mode_normal"),
            variable=self.output_length_mode_var,
            value="normal")
        normal_radio.pack(side=tk.LEFT)
        self._register_i18n_widget(normal_radio, "label.output_mode_normal")
        fixed_radio = ttk.Radiobutton(
            output_mode_frame,
            text=self.tr("label.output_mode_fixed"),
            variable=self.output_length_mode_var,
            value="fixed")
        fixed_radio.pack(side=tk.LEFT, padx=(C_STYLE["pad_lg"], 0))
        self._register_i18n_widget(fixed_radio, "label.output_mode_fixed")

        # ═══ Group 2: 负载参数 ═══
        load = ttk.LabelFrame(parent, text="负载参数", padding=C_STYLE["pad_md"])
        load.pack(fill=tk.X, pady=(0, C_STYLE["gap_md"]))
        load.columnconfigure(1, weight=1)
        load.columnconfigure(3, weight=1)

        self.concurrency_var = tk.StringVar(value=DEFAULT_PRESET_KEY)
        _lbl(load, "并发预设", 0, 0, padx=(0, C_STYLE["pad_sm"]), pady=(C_STYLE["gap_sm"], 0))
        ttk.Combobox(load, textvariable=self.concurrency_var,
                     values=list(BENCHMARK_PRESETS.keys()),
                     width=30, state="readonly").grid(
            row=0, column=1, columnspan=3, sticky="w", pady=(C_STYLE["gap_sm"], 0))

        self.custom_conc_var = tk.StringVar(value="8")
        self.total_var = tk.StringVar(value="80")
        _lbl(load, "并发数", 1, 0, padx=(0, C_STYLE["pad_sm"]), pady=(C_STYLE["gap_sm"], 0))
        self.custom_conc_spin = ttk.Spinbox(load, from_=1, to=512, increment=1,
                                            textvariable=self.custom_conc_var, width=SPIN_W)
        self.custom_conc_spin.grid(row=1, column=1, sticky="w", pady=(C_STYLE["gap_sm"], 0))
        self.custom_conc_spin.configure(state="disabled")

        _lbl(load, "请求总数", 1, 2, padx=(C_STYLE["gap_lg"], C_STYLE["pad_sm"]), pady=(C_STYLE["gap_sm"], 0))
        self.total_spin = ttk.Spinbox(load, from_=1, to=9999, increment=5,
                                      textvariable=self.total_var, width=SPIN_W)
        self.total_spin.grid(row=1, column=3, sticky="w", pady=(C_STYLE["gap_sm"], 0))
        self.total_spin.configure(state="disabled")

        self.warmup_var = tk.IntVar(value=2)
        _lbl(load, "预热请求数", 2, 0, padx=(0, C_STYLE["pad_sm"]), pady=(C_STYLE["gap_sm"], 0))
        ttk.Spinbox(load, from_=0, to=20, increment=1,
                    textvariable=self.warmup_var, width=SPIN_W).grid(
            row=2, column=1, sticky="w", pady=(C_STYLE["gap_sm"], 0))

        # sync logic: preset <-> concurrency/total (safe parse to avoid TclError)
        CUSTOM_KEY = "自定义"

        def _safe_int(s, default=0):
            try:
                v = str(s).strip()
                return int(float(v)) if v else default
            except Exception:
                return default

        def _sync_total_to_concurrency(*args):
            label = self.concurrency_var.get()
            if label == CUSTOM_KEY:
                self.custom_conc_spin.configure(state="normal")
                self.total_spin.configure(state="normal")
            else:
                self.custom_conc_spin.configure(state="disabled")
                self.total_spin.configure(state="disabled")
                preset_cfg = BENCHMARK_PRESETS.get(label, {})
                conc = preset_cfg.get("concurrency", 8)
                ptotal = preset_cfg.get("total", conc)
                self.custom_conc_var.set(str(conc))
                self.total_var.set(str(ptotal))

        self.concurrency_var.trace_add("write", _sync_total_to_concurrency)
        self.custom_conc_var.trace_add("write", lambda *a: (
            self.total_var.set(str(_safe_int(self.custom_conc_var.get()) * 10))
            if self.concurrency_var.get() == CUSTOM_KEY and _safe_int(self.custom_conc_var.get()) > 0
            else None
        ))

        # ═══ Group 3: 保存选项 ═══
        saveg = ttk.LabelFrame(parent, text="保存选项", padding=C_STYLE["pad_md"])
        saveg.pack(fill=tk.X, pady=(0, C_STYLE["gap_md"]))
        saveg.columnconfigure(1, weight=1)
        saveg.columnconfigure(3, weight=1)

        self.save_report_var = tk.StringVar(value="否")
        _lbl(saveg, "保存测试报告", 0, 0, padx=(0, C_STYLE["pad_sm"]), pady=(C_STYLE["gap_sm"], 0))
        ttk.Combobox(saveg, textvariable=self.save_report_var,
                     values=["否", "是"], width=10, state="readonly").grid(
            row=0, column=1, sticky="w", pady=(C_STYLE["gap_sm"], 0))

        self.auto_save_var = tk.StringVar(value="否")
        _lbl(saveg, "自动保存配置", 0, 2, padx=(C_STYLE["gap_lg"], C_STYLE["pad_sm"]), pady=(C_STYLE["gap_sm"], 0))
        ttk.Combobox(saveg, textvariable=self.auto_save_var,
                     values=["否", "是"], width=10, state="readonly").grid(
            row=0, column=3, sticky="w", pady=(C_STYLE["gap_sm"], 0))

        # wire auto-save traces
        for v in (self.url_var, self.key_var, self.model_var,
                  self.max_tokens_var, self.temp_var, self.total_var,
                  self.concurrency_var, self.save_report_var,
                  self.stream_var, self.output_length_mode_var,
                  self.warmup_var, self.auto_save_var):
            v.trace_add("write", lambda *a: self._auto_save_check())

    # ── 输入 Token 校准 (Input Token Calibration) methods ───────────────────────
    def _build_token_calib_section(self, parent):
        """Build Input Token Calibration UI section.

        All labels and buttons use self.tr() for i18n — no bilingual slash strings.

        Fields:
          - 目标输入 Tokens / Target Input Tokens
          - 目标输出 Token 数（校准用）/ Output Token Count (calibration)
          - 输入模式 / Prompt Mode
          - 校准方式 / Calibration Method
          - System Prompt（校准用）
          - User Prompt text area

        Buttons: 自动生成输入 · 校准输入 Token · 验证 Token 用量 · 应用到测试参数
        """
        LABEL_W = 20
        BG = C_STYLE["bg_card"]

        def _lbl(pr, i18n_key, row, col=0, **kw):
            lbl = tk.Label(pr, text=self.tr(i18n_key), font=C_STYLE["font_body"],
                           width=LABEL_W, bg=BG, fg=C_STYLE["text_primary"], anchor="w")
            lbl.grid(row=row, column=col, sticky="w",
                     padx=(0, C_STYLE["pad_sm"]), pady=(C_STYLE["gap_sm"], 0), **kw)
            self._register_i18n_widget(lbl, i18n_key)
            return lbl

        frame = tk.Frame(parent, bg=BG)
        frame.pack(fill=tk.X, pady=(0, C_STYLE["gap_sm"]))
        frame.columnconfigure(1, weight=1)

        # ── Target Input Tokens ──
        self.target_input_tokens_var = tk.IntVar(value=self._target_input_tokens)
        _lbl(frame, "calib.target_input_tokens", 0)
        _inp_frame = tk.Frame(frame, bg=BG)
        _inp_frame.grid(row=0, column=1, sticky="w", pady=(C_STYLE["gap_sm"], 0))
        ttk.Spinbox(_inp_frame, from_=64, to=32768, increment=64,
                    textvariable=self.target_input_tokens_var, width=8).pack(side=tk.LEFT)
        for _v in (512, 1024, 2048, 4096):
            ttk.Button(_inp_frame, text=str(_v), width=5,
                       command=lambda v=_v: self.target_input_tokens_var.set(v)
                       ).pack(side=tk.LEFT, padx=(2, 0))

        # ── Output Token Count (for calibration probes only) ──
        self.target_output_tokens_var = tk.IntVar(value=self._target_output_tokens)
        _lbl(frame, "calib.target_output_tokens", 1)
        _out_frame = tk.Frame(frame, bg=BG)
        _out_frame.grid(row=1, column=1, sticky="w", pady=(C_STYLE["gap_sm"], 0))
        ttk.Spinbox(_out_frame, from_=64, to=32768, increment=64,
                    textvariable=self.target_output_tokens_var, width=8).pack(side=tk.LEFT)
        for _v in (512, 1024, 2048):
            ttk.Button(_out_frame, text=str(_v), width=5,
                       command=lambda v=_v: self.target_output_tokens_var.set(v)
                       ).pack(side=tk.LEFT, padx=(2, 0))

        # ── Prompt Mode ──
        self._prompt_mode_var = tk.StringVar(value="固定提示词")
        _lbl(frame, "calib.prompt_mode", 2)
        ttk.Combobox(frame, textvariable=self._prompt_mode_var,
                     values=["固定提示词", "等长变体", "合成随机"],
                     width=22, state="readonly").grid(
            row=2, column=1, sticky="w", pady=(C_STYLE["gap_sm"], 0))

        # ── Calibration Method ──
        self._calibration_method_var = tk.StringVar(value="服务端 usage 校准")
        _lbl(frame, "calib.calibration_method", 3)
        ttk.Combobox(frame, textvariable=self._calibration_method_var,
                     values=["服务端 usage 校准", "本地 tokenizer 估算", "手动"],
                     width=22, state="readonly").grid(
            row=3, column=1, sticky="w", pady=(C_STYLE["gap_sm"], 0))

        # ── System Prompt (calibration-local) ──
        _lbl(frame, "calib.system_prompt", 4)
        self._calib_system_var = tk.StringVar(
            value="你是一个严谨、简洁、可靠的中文技术助手。")
        ttk.Entry(frame, textvariable=self._calib_system_var, width=44).grid(
            row=4, column=1, sticky="ew", pady=(C_STYLE["gap_sm"], 0))

        # ── User Prompt text area ──
        _lbl(frame, "calib.user_prompt", 5)
        _ta_frame = tk.Frame(frame, bg=BG)
        _ta_frame.grid(row=5, column=1, sticky="ew", pady=(C_STYLE["gap_sm"], 0))
        _ta_frame.columnconfigure(0, weight=1)
        self._calib_prompt_text = tk.Text(_ta_frame, height=4, width=44,
                                          font=C_STYLE["font_small"],
                                          wrap=tk.WORD,
                                          bg=C_STYLE.get("bg_input", "#ffffff"),
                                          fg=C_STYLE["text_primary"])
        self._calib_prompt_text.grid(row=0, column=0, sticky="ew")
        _ta_sb = ttk.Scrollbar(_ta_frame, command=self._calib_prompt_text.yview)
        _ta_sb.grid(row=0, column=1, sticky="ns")
        self._calib_prompt_text.configure(yscrollcommand=_ta_sb.set)
        self._calib_prompt_text.insert("1.0", "请详细介绍机器学习的基本原理、常见算法以及在工业界的典型应用场景。")

        # ── Buttons row — all i18n-registered ──
        btn_row = tk.Frame(parent, bg=BG)
        btn_row.pack(fill=tk.X, pady=(C_STYLE["gap_sm"], 0))
        self._register_i18n_widget(
            ttk.Button(btn_row, text=self.tr("button.generate_input"),
                       command=self._on_generate_input),
            "button.generate_input"
        ).pack(side=tk.LEFT)
        self._register_i18n_widget(
            ttk.Button(btn_row, text=self.tr("button.calibrate_input"),
                       command=self._on_calibrate_input_token),
            "button.calibrate_input"
        ).pack(side=tk.LEFT, padx=(C_STYLE["pad_sm"], 0))
        self._register_i18n_widget(
            ttk.Button(btn_row, text=self.tr("button.verify_token_usage"),
                       command=self._on_verify_token_usage),
            "button.verify_token_usage"
        ).pack(side=tk.LEFT, padx=(C_STYLE["pad_sm"], 0))
        self._register_i18n_widget(
            ttk.Button(btn_row, text=self.tr("button.apply_to_benchmark"),
                       command=self._on_apply_calibration_to_test),
            "button.apply_to_benchmark"
        ).pack(side=tk.LEFT, padx=(C_STYLE["pad_sm"], 0))

        # ── Status label ──
        self._calib_status_var = tk.StringVar(
            value=self.tr("calib.status_not_calibrated"))
        tk.Label(parent, textvariable=self._calib_status_var,
                 font=C_STYLE["font_small"], bg=BG,
                 fg=C_STYLE["text_secondary"], anchor="w",
                 wraplength=520, justify="left").pack(
            fill=tk.X, pady=(C_STYLE["pad_sm"], 0))

    def _calibrate_input_tokens_server_usage(self, api_url, api_key, model,
                                              system_prompt, target_tokens,
                                              tolerance=8, max_iter=12) -> tuple:
        """Binary-search the prompt length to match target_tokens (server-side).

        Sends lightweight requests (max_tokens=1, stream=False) and reads
        usage.prompt_tokens from the response.  Returns (adjusted_prompt, actual_tokens).

        Taxonomy: calibration_method = "server_usage"
        """
        base_prompt = self.prompt_var.get() or "请用300字左右介绍机器学习。"
        # Seed prompt with repeated content so we can adjust length
        unit = base_prompt.strip()
        if not unit:
            unit = "请介绍人工智能。"

        # Estimate current token density: get baseline
        messages = [{"role": "user", "content": unit}]
        if system_prompt:
            messages = [{"role": "system", "content": system_prompt}] + messages
        result = call_llm(api_url, api_key, model, messages, max_tokens=1,
                          temperature=0.0, stream=False)
        if not result.get("ok"):
            return unit, 0

        current_tokens = result.get("prompt_tokens", 0)
        if current_tokens <= 0:
            return unit, 0

        # Binary search on number of repeated units
        char_per_token = len(unit) / max(current_tokens, 1)
        estimated_chars = int(target_tokens * char_per_token)
        lo, hi = max(1, estimated_chars // 2), estimated_chars * 3
        best_prompt = unit
        best_tokens = current_tokens

        for _i in range(max_iter):
            mid = (lo + hi) // 2
            candidate = (unit * ((mid // len(unit)) + 1))[:mid]
            msgs = [{"role": "user", "content": candidate}]
            if system_prompt:
                msgs = [{"role": "system", "content": system_prompt}] + msgs
            r = call_llm(api_url, api_key, model, msgs, max_tokens=1,
                         temperature=0.0, stream=False)
            if not r.get("ok"):
                break
            got = r.get("prompt_tokens", 0)
            best_prompt = candidate
            best_tokens = got
            diff = got - target_tokens
            if abs(diff) <= tolerance:
                break
            if diff < 0:
                lo = mid + 1
            else:
                hi = mid - 1

        return best_prompt, best_tokens

    def _calibrate_input_tokens_local(self, model_name_or_path, system_prompt,
                                      target_tokens, tolerance=8) -> tuple:
        """Estimate prompt length using a local tokenizer (lazy import of transformers).

        Returns (adjusted_prompt, estimated_tokens).
        Taxonomy: calibration_method = "local_tokenizer"
        """
        try:
            from transformers import AutoTokenizer  # lazy import — optional dependency
        except ImportError:
            return self.prompt_var.get(), 0

        base_prompt = self.prompt_var.get() or "请用300字左右介绍机器学习。"
        unit = base_prompt.strip() or "请介绍人工智能。"
        try:
            tok = AutoTokenizer.from_pretrained(model_name_or_path, trust_remote_code=True)
        except Exception:
            return unit, 0

        msgs = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        msgs.append({"role": "user", "content": unit})
        try:
            encoded = tok.apply_chat_template(msgs, add_generation_prompt=True,
                                              tokenize=True)
            current_tokens = len(encoded)
        except Exception:
            current_tokens = len(tok.encode(unit))

        char_per_token = len(unit) / max(current_tokens, 1)
        estimated_chars = int(target_tokens * char_per_token)
        for _step in range(20):
            candidate = (unit * ((estimated_chars // len(unit)) + 2))[:estimated_chars]
            try:
                msgs2 = []
                if system_prompt:
                    msgs2.append({"role": "system", "content": system_prompt})
                msgs2.append({"role": "user", "content": candidate})
                got = len(tok.apply_chat_template(msgs2, add_generation_prompt=True,
                                                  tokenize=True))
            except Exception:
                got = len(tok.encode(candidate))
            diff = got - target_tokens
            if abs(diff) <= tolerance:
                return candidate, got
            estimated_chars += int(-diff * char_per_token)
            estimated_chars = max(1, estimated_chars)
        return candidate, got

    def _get_calib_system_prompt(self) -> str:
        """Get system prompt from calibration section (if available) or fall back to main."""
        try:
            return self._calib_system_var.get().strip()
        except Exception:
            return self.system_var.get().strip()

    def _get_calib_user_prompt(self) -> str:
        """Get user prompt from calibration text area (if available) or fall back to main."""
        try:
            return self._calib_prompt_text.get("1.0", "end-1c").strip()
        except Exception:
            return self.prompt_var.get().strip()

    def _on_generate_input(self):
        """Generate synthetic input prompt targeting Target Input Tokens.

        Uses the selected Prompt Mode:
          - Fixed Prompt: uses calibrated or base prompt as-is
          - Same-Length Variants: generates variant 1 for preview
          - Synthetic Random: generates deterministic synthetic prompt
        """
        from llm_benchmark_app.token_calibration import (
            generate_synthetic_prompt, generate_same_length_variants, _FILLER_TEXT)
        target_in = self.target_input_tokens_var.get()
        prompt_mode = getattr(self, "_prompt_mode_var", None)
        mode = prompt_mode.get() if prompt_mode else "固定提示词"
        sys_prompt = self._get_calib_system_prompt()

        if mode == "合成随机":
            generated = generate_synthetic_prompt(target_in, sys_prompt)
        elif mode == "等长变体":
            base = self._get_calib_user_prompt() or "请介绍人工智能。"
            variants = generate_same_length_variants(base, num_variants=1)
            generated = variants[0] if variants else base
        else:
            # 固定提示词 — use filler-augmented base prompt
            base = self._get_calib_user_prompt() or "请介绍人工智能。"
            # Append calibration filler as suffix preview
            generated = base + "\n" + _FILLER_TEXT[:200]

        try:
            self._calib_prompt_text.delete("1.0", tk.END)
            self._calib_prompt_text.insert("1.0", generated)
        except Exception:
            pass
        # Make generated text immediately available to Apply (no calibration required)
        self._calibrated_user_prompt = generated
        self._calibrated_system_prompt = sys_prompt
        self._actual_prompt_tokens = 0  # unknown until server calibration runs
        self._calibration_method = "generated"
        self._calib_status_var.set(
            f"已生成提示词（{len(generated)} 字符）  模式={mode}  "
            f"点击「校准输入 Token 数」验证实际 Token 数量")

    def _on_calibrate_input_token(self):
        """Button handler: calibrate prompt length to match Target Input Tokens.

        Reads System Prompt and User Prompt from the calibration section.
        Updates _calibrated_user_prompt, _actual_prompt_tokens, _calibration_method,
        _calibrated_system_prompt, prompt_mode.
        """
        api_url = self.url_var.get().strip()
        api_key = self.key_var.get().strip()
        model = self.model_var.get().strip()
        system_prompt = self._get_calib_system_prompt()
        base_prompt = self._get_calib_user_prompt()
        target_in = self.target_input_tokens_var.get()
        target_out = self.target_output_tokens_var.get()
        method = self._calibration_method_var.get()
        prompt_mode = getattr(self, "_prompt_mode_var", None)
        mode_str = prompt_mode.get() if prompt_mode else "固定提示词"

        self._calib_status_var.set("正在校准…")
        self._target_input_tokens = target_in
        self._target_output_tokens = target_out

        def _do_calib():
            from llm_benchmark_app.token_calibration import (
                calibrate_by_server_usage, calibrate_by_local_tokenizer,
                TokenCalibrationResult)
            try:
                if method == "手动":
                    # 手动模式: record as-is, mark calibration_method=manual
                    actual = 0  # unknown until server verifies
                    calib_method = "manual"
                    prompt = base_prompt
                    warnings_out = ["手动模式: Token 数量未经校准，请使用「验证 Token 用量」确认。"]
                elif method == "本地 tokenizer 估算":
                    r: TokenCalibrationResult = calibrate_by_local_tokenizer(
                        model, system_prompt, base_prompt, target_in,
                        target_output_tokens=target_out)
                    prompt = r.user_prompt
                    actual = r.actual_prompt_tokens or 0
                    calib_method = "local_tokenizer"
                    warnings_out = r.warnings
                else:
                    r: TokenCalibrationResult = calibrate_by_server_usage(
                        api_url, model, system_prompt, base_prompt, target_in,
                        target_output_tokens=target_out, api_key=api_key)
                    prompt = r.user_prompt
                    actual = r.actual_prompt_tokens or 0
                    calib_method = "server_usage"
                    warnings_out = r.warnings

                self._calibrated_user_prompt = prompt
                self._calibrated_system_prompt = system_prompt
                self._actual_prompt_tokens = actual
                self._calibration_method = calib_method
                _mode_code_map = {
                    "固定提示词": "fixed",
                    "等长变体":   "same_length_variants",
                    "合成随机":   "synthetic_random",
                }
                self._prompt_mode = _mode_code_map.get(
                    mode_str, mode_str.lower().replace(" ", "_").replace("-", "_"))

                warn_str = f"\n⚠ {warnings_out[0]}" if warnings_out else ""
                status_msg = (
                    f"校准完成: actual_prompt_tokens={actual} "
                    f"target={target_in} calibration_method={calib_method} "
                    f"prompt_mode={self._prompt_mode}\n"
                    f"提示词: {len(prompt)} 字符  "
                    f"目标输出 Token 数: {target_out}{warn_str}")
                self.root.after(0, lambda: self._calib_status_var.set(status_msg))
                # Update text area with calibrated prompt
                self.root.after(0, lambda: (
                    self._calib_prompt_text.delete("1.0", tk.END),
                    self._calib_prompt_text.insert("1.0", prompt)
                ))
            except Exception as exc:
                self.root.after(0, lambda: self._calib_status_var.set(
                    f"校准失败: {exc}"))
        threading.Thread(target=_do_calib, daemon=True).start()

    def _on_verify_token_usage(self):
        """Button handler: send one request and display actual prompt/completion tokens."""
        api_url = self.url_var.get().strip()
        api_key = self.key_var.get().strip()
        model = self.model_var.get().strip()
        system_prompt = self._get_calib_system_prompt()
        prompt = self._calibrated_user_prompt or self._get_calib_user_prompt()
        max_tok = self.target_output_tokens_var.get()

        self._calib_status_var.set("正在验证 Token 用量…")

        def _do_verify():
            try:
                messages = [{"role": "user", "content": prompt}]
                if system_prompt:
                    messages = [{"role": "system", "content": system_prompt}] + messages
                r = call_llm(api_url, api_key, model, messages,
                             max_tokens=max_tok, temperature=0.0, stream=False)
                if r.get("ok"):
                    pt = r.get("prompt_tokens", 0)
                    ct = r.get("completion_tokens", 0)
                    target_in = self._target_input_tokens
                    target_out = self._target_output_tokens
                    # input_token_validation_passed: actual vs target within tolerance
                    ivp = (abs(pt - target_in) <= 8) if pt > 0 else False
                    # fixed_output_validation_passed: completion_tokens vs target
                    fov = (abs(ct - target_out) <= 32) if ct > 0 else False
                    # Mark manual as verified if usage returned
                    if self._calibration_method == "manual" and pt > 0:
                        self._actual_prompt_tokens = pt
                    status = (
                        f"验证通过: prompt_tokens={pt} completion_tokens={ct}\n"
                        f"  target_input_tokens={target_in}  "
                        f"input_token_validation_passed={ivp}\n"
                        f"  target_output_tokens={target_out}  "
                        f"fixed_output_validation_passed={fov}")
                    self.root.after(0, lambda: self._calib_status_var.set(status))
                else:
                    err = r.get("error", "unknown")
                    self.root.after(0, lambda: self._calib_status_var.set(
                        f"验证失败: {err}"))
            except Exception as exc:
                self.root.after(0, lambda: self._calib_status_var.set(
                    f"验证异常: {exc}"))
        threading.Thread(target=_do_verify, daemon=True).start()

    def _update_text_field(self, var: tk.StringVar, widget, value: str):
        """Update a StringVar AND its associated tk.Text widget (Text has no textvariable).

        Args:
            var:    The StringVar bound to the field
            widget: The tk.Text widget (or None if not available)
            value:  New text value
        """
        var.set(value)
        if widget is not None:
            try:
                widget.delete("1.0", tk.END)
                widget.insert("1.0", value)
            except Exception:
                pass

    def _apply_input_token_calibration_to_settings(self):
        """Single authoritative helper: apply calibration prompts to API config.

        Resolution order for user prompt:
          1. self._calibrated_user_prompt  (set by calibration thread)
          2. current text in self._calib_prompt_text  (typed/generated by user)
        Resolution order for system prompt:
          1. self._calibrated_system_prompt  (set by calibration thread)
          2. current value of self._calib_system_var  (calibration Entry widget)

        Both tk.Text widgets AND their backing StringVars are updated synchronously
        so the runner reads the correct value on the very next benchmark/sweep start.

        Does NOT touch max_tokens_var or output_length_mode_var — those remain
        under the user's control.
        """
        # ── Resolve user prompt ────────────────────────────────────────────────
        user_prompt = self._calibrated_user_prompt
        if not user_prompt:
            # Fall back to what's currently typed in the calibration text box
            try:
                user_prompt = self._calib_prompt_text.get("1.0", "end-1c").strip()
            except Exception:
                user_prompt = ""

        if not user_prompt:
            self._calib_status_var.set(self.tr("calib.apply_no_result"))
            return

        # ── Resolve system prompt ──────────────────────────────────────────────
        system_prompt = self._calibrated_system_prompt
        if not system_prompt:
            try:
                system_prompt = self._calib_system_var.get().strip()
            except Exception:
                system_prompt = ""

        # ── Update API User Prompt (StringVar + Text widget) ──────────────────
        self.prompt_var.set(user_prompt)
        _wp = getattr(self, "_api_user_prompt_widget", None)
        if _wp is not None:
            try:
                _wp.delete("1.0", "end")
                _wp.insert("1.0", user_prompt)
            except Exception:
                pass

        # ── Update API System Prompt (StringVar + Text widget) ────────────────
        if system_prompt:
            self.system_var.set(system_prompt)
            _ws = getattr(self, "_api_system_prompt_widget", None)
            if _ws is not None:
                try:
                    _ws.delete("1.0", "end")
                    _ws.insert("1.0", system_prompt)
                except Exception:
                    pass

        # ── Keep internal calibration state consistent ─────────────────────────
        self._calibrated_user_prompt = user_prompt
        self._calibrated_system_prompt = system_prompt

        # ── Update status immediately (no root.after) ──────────────────────────
        actual = self._actual_prompt_tokens
        method = self._calibration_method or "—"
        status = self.tr("calib.apply_success")
        if actual:
            status = (
                f"{status}  "
                f"actual_prompt_tokens={actual}  calibration_method={method}"
            )
        self._calib_status_var.set(status)

    def _on_apply_calibration_to_test(self):
        """Button command: delegate to the authoritative apply helper."""
        self._apply_input_token_calibration_to_settings()

    def _build_results_tab(self):
        bf = self.bench_frame
        bf.grid_columnconfigure(0, weight=1)
        bf.grid_rowconfigure(0, weight=1)

        bench_scroll = ScrollableFrame(bf, bg=C_STYLE["bg_main"])
        bench_scroll.grid(row=0, column=0, sticky="nsew")
        bf_inner = tk.Frame(bench_scroll.content, bg=C_STYLE["bg_main"])
        bf_inner.pack(fill=tk.BOTH, expand=True, padx=C_STYLE["pad_lg"], pady=C_STYLE["pad_lg"])
        # redirect bf to inner for existing widget parents
        bf = bf_inner
        bf.grid_columnconfigure(0, weight=1)
        bf.grid_rowconfigure(0, weight=0)  # status cards — fixed
        bf.grid_rowconfigure(1, weight=0)  # metrics — fixed
        bf.grid_rowconfigure(2, weight=0)  # notice — fixed
        bf.grid_rowconfigure(3, weight=0)  # histogram — fixed height
        bf.grid_rowconfigure(4, weight=1)  # report — expandable
        status_row = tk.Frame(bf, bg=C_STYLE["bg_main"])
        status_row.grid(row=0, column=0, sticky="ew", pady=(0, C_STYLE["gap_lg"]))
        for i in range(4):
            status_row.grid_columnconfigure(i, weight=1, uniform="sc")
        self.indicators: dict[str, StatusCard] = {}
        sc_defs = [("connectivity", "服务连接"), ("smoke", "基础测试"),
                    ("benchmark", "压力测试"), ("duration", "测试耗时")]
        for i, (key, title) in enumerate(sc_defs):
            sc = StatusCard(status_row, title)
            sc.grid(row=0, column=i, sticky="ew",
                    padx=(0 if i == 0 else C_STYLE["pad_sm"], 0))
            self.indicators[key] = sc
        metrics_card = SectionCard(bf, "结果摘要")
        metrics_card.grid(row=1, column=0, sticky="ew", pady=(0, C_STYLE["gap_lg"]))
        self.metrics: dict[str, MetricItem] = {}
        metric_grid = tk.Frame(metrics_card.content, bg=C_STYLE["bg_card"])
        metric_grid.pack(fill=tk.X)
        # 指标按口径分组：延迟（单请求）/ 吞吐（系统级）/ 可靠性（规范 §2.3、§3.4）
        metric_groups = (
            ("延迟（单请求）", (
                ("ttft", "首包延迟 TTFT"), ("visible_ttft", "首字延迟 FVT"),
                ("tpot", "单 Token 耗时 TPOT"), ("itl", "Token 间隔 ITL"),
                ("e2e_p95", "E2E P95"),
            )),
            ("吞吐（系统级）", (
                ("system_output_tps", "输出吞吐 TPS"), ("rps", "请求吞吐 RPS"),
            )),
            ("可靠性（单请求）", (
                ("success_rate", "成功率"),
            )),
        )
        for gi, (group_title, items) in enumerate(metric_groups):
            grow = tk.Frame(metric_grid, bg=C_STYLE["bg_card"])
            grow.pack(fill=tk.X, pady=(0 if gi == 0 else C_STYLE["gap_md"], 0))
            tk.Label(grow, text=group_title, font=C_STYLE["font_small"],
                     bg=C_STYLE["bg_card"], fg=C_STYLE["text_muted"],
                     width=12, anchor="w", justify=tk.LEFT).pack(side=tk.LEFT, fill=tk.Y)
            for i, (key, label) in enumerate(items):
                mi = MetricItem(grow, label, metric_key=key)
                mi.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                        padx=(0 if i == 0 else C_STYLE["gap_md"], 0))
                self.metrics[key] = mi
        self.notice_banner = NoticeBanner(bf, "info")
        self.notice_banner.grid(row=2, column=0, sticky="ew",
                                pady=(0, C_STYLE["gap_lg"]))
        hist_card = SectionCard(bf, "E2E Latency Distribution (e2el)",
                                collapsible=True, expanded=False)
        self._hist_card = hist_card
        hist_card.grid(row=3, column=0, sticky="nsew",
                       pady=(0, C_STYLE["gap_lg"]))
        hist_card.columnconfigure(0, weight=1)
        hist_card.content.grid_columnconfigure(0, weight=1)
        hist_card.content.grid_rowconfigure(0, weight=1)
        self.hist_canvas = tk.Canvas(hist_card.content, height=260,
                                     bg=C_STYLE["bg_card"],
                                     highlightthickness=0, bd=0)
        self.hist_canvas.grid(row=0, column=0, sticky="nsew")
        self.hist_canvas.bind("<Configure>",
                              lambda e: self._redraw_e2e_histogram(),
                              add="+")
        report_card = SectionCard(bf, "详细报告")
        report_card.grid(row=4, column=0, sticky="nsew",
                         pady=(0, C_STYLE["gap_lg"]))
        report_card.columnconfigure(0, weight=1)
        self.result_text = tk.Text(report_card.content, font=C_STYLE["font_code"],
                                   wrap=tk.WORD, bg=C_STYLE["bg_card"],
                                   fg=C_STYLE["text_primary"],
                                   relief=tk.FLAT, borderwidth=0,
                                   state=tk.DISABLED)
        self.result_text.grid(row=0, column=0, sticky="nsew")
        rscroll = ttk.Scrollbar(report_card.content, orient=tk.VERTICAL,
                                command=self.result_text.yview)
        rscroll.grid(row=0, column=1, sticky="ns")
        self.result_text.configure(yscrollcommand=rscroll.set)

        # Collapse report by default — click title to expand
        self._report_card = report_card
        self._report_collapsed = True
        report_card.content.grid_remove()
        report_card.title_lbl.config(text="▶ 详细报告（点击展开）")

        def _toggle_report(e=None):
            if self._report_collapsed:
                report_card.content.grid()
                report_card.title_lbl.config(text="▼ 详细报告")
                self._report_collapsed = False
            else:
                report_card.content.grid_remove()
                report_card.title_lbl.config(text="▶ 详细报告（点击展开）")
                self._report_collapsed = True

        report_card.title_lbl.bind("<Button-1>", _toggle_report)
        # Also make the whole header frame clickable
        for child in [report_card.title_lbl]:
            child.configure(cursor="hand2")
    def _labeled_input(self, parent, label, var, row, width=44):
        ttk.Label(parent, text=label, style="Body.TLabel",
                  background=C_STYLE["bg_card"]).grid(
            row=row, column=0, sticky="w",
            padx=(0, C_STYLE["pad_sm"]), pady=(0, C_STYLE["gap_sm"]))
        e = ttk.Entry(parent, textvariable=var, width=width)
        e.grid(row=row, column=1, sticky="ew", pady=(0, C_STYLE["gap_md"]))
        parent.columnconfigure(1, weight=1)
    def _labeled_text(self, parent, label, var, row, height=2):
        ttk.Label(parent, text=label, style="Body.TLabel",
                  background=C_STYLE["bg_card"]).grid(
            row=row, column=0, sticky="nw",
            padx=(0, C_STYLE["pad_sm"]), pady=(0, C_STYLE["gap_sm"]))
        t = tk.Text(parent, height=height, font=C_STYLE["font_body"],
                    bg=C_STYLE["bg_input"], fg=C_STYLE["text_primary"],
                    highlightbackground=C_STYLE["border"],
                    highlightthickness=1, relief=tk.FLAT, borderwidth=0,
                    padx=8, pady=6, wrap=tk.WORD,
                    insertbackground=C_STYLE["accent"])
        t.grid(row=row, column=1, sticky="ew", pady=(0, C_STYLE["gap_md"]))
        t.insert("1.0", var.get())
        t.bind("<FocusOut>", lambda e, v=var, w=t: [v.set(w.get("1.0", "end-1c")), self._auto_save_check()])
        parent.columnconfigure(1, weight=1)
        if not hasattr(self, '_text_widgets'):
            self._text_widgets = {}
        self._text_widgets[label] = t
    def _labeled_spin(self, parent, label, var, from_, to, row, col, step=1):
        ttk.Label(parent, text=label, style="Body.TLabel",
                  background=C_STYLE["bg_card"]).grid(
            row=row, column=col * 2, sticky="w",
            padx=(0 if col == 0 else C_STYLE["gap_lg"], C_STYLE["pad_sm"]),
            pady=(C_STYLE["gap_sm"], 0))
        inc = step if isinstance(step, int) else 1
        s = ttk.Spinbox(parent, from_=from_, to=to, increment=inc,
                        textvariable=var, width=10)
        s.grid(row=row, column=col * 2 + 1, sticky="w",
               padx=(0, 0), pady=(C_STYLE["gap_sm"], 0))
    # ── lightweight status animation (text-only, root.after) ──
    SPINNER_FRAMES = ["|", "/", "-", "\\"]

    def _phase_display_name(self, phase: str) -> str:
        return {
            "benchmark": self.tr("status.benchmarking"),
            "sweep": self.tr("status.sweeping"),
            "preflight": self.tr("status.ready"),
        }.get(phase, "运行中")

    def _start_status_animation(self, phase: str, total: int = 0,
                                current_label: str = ""):
        self._benchmark_running = phase == "benchmark"
        self._run_started_at = time.perf_counter()
        self._run_completed = 0
        self._run_total = int(total or 0)
        self._run_fail = 0
        self._run_phase = phase
        self._run_current_label = current_label
        self._spinner_index = 0
        if self._spinner_after_id:
            try:
                self.root.after_cancel(self._spinner_after_id)
            except Exception:
                pass
            self._spinner_after_id = None
        self._animate_status_badge()

    def _update_status_animation(self, completed: int | None = None,
                                 total: int | None = None,
                                 fail: int | None = None,
                                 phase: str | None = None,
                                 current_label: str | None = None):
        if completed is not None:
            self._run_completed = int(completed)
        if total is not None:
            self._run_total = int(total)
        if fail is not None:
            self._run_fail = int(fail)
        if phase is not None:
            self._run_phase = phase
        if current_label is not None:
            self._run_current_label = current_label
        self._render_status_badge(
            self.SPINNER_FRAMES[self._spinner_index % len(self.SPINNER_FRAMES)])

    def _animate_status_badge(self):
        if not (self._benchmark_running or getattr(self, "_sweep_running", False)):
            return
        frame = self.SPINNER_FRAMES[self._spinner_index % len(self.SPINNER_FRAMES)]
        self._spinner_index += 1
        self._render_status_badge(frame)
        self._spinner_after_id = self.root.after(500, self._animate_status_badge)

    def _render_status_badge(self, prefix: str):
        phase_name = self._phase_display_name(self._run_phase)
        progress = f"{self._run_completed}/{self._run_total}" if self._run_total else f"{self._run_completed}"
        parts = [f"{prefix} {phase_name}", progress]
        if self._run_current_label:
            parts.append(self._run_current_label)
        parts.append(f"fail={self._run_fail}")
        parts.append(self._format_elapsed())
        try:
            self._status_dot.config(text="", fg=C_STYLE["accent"])
            self._status_badge_lbl.config(
                text=" · ".join(parts),
                fg=C_STYLE["text_primary"])
        except Exception:
            pass

    def _stop_status_animation(self, success: bool = True, completed: int | None = None,
                               total: int | None = None, fail: int | None = None,
                               message: str | None = None):
        if completed is not None:
            self._run_completed = int(completed)
        if total is not None:
            self._run_total = int(total)
        if fail is not None:
            self._run_fail = int(fail)
        self._benchmark_running = False
        if self._spinner_after_id:
            try:
                self.root.after_cancel(self._spinner_after_id)
            except Exception:
                pass
            self._spinner_after_id = None
        prefix = "✓" if success else "✕"
        label = message or (self.tr("status.completed") if success else self.tr("status.failed"))
        progress = f"{self._run_completed}/{self._run_total}" if self._run_total else f"{self._run_completed}"
        if success:
            text = f"{prefix} {label} · {progress} · fail={self._run_fail} · {self._format_elapsed()}"
        else:
            text = f"{prefix} {label} · fail={self._run_fail} · {self._format_elapsed()}"
        try:
            self._status_dot.config(text="", fg=C_STYLE["text_muted"])
            self._status_badge_lbl.config(
                text=text, fg=C_STYLE["success_text"] if success else C_STYLE["error_text"])
        except Exception:
            pass

    def _start_icon_pulse(self, mode="基准测试", mark_benchmark=True):
        phase = "benchmark" if mark_benchmark else "sweep"
        self._start_status_animation(phase=phase, total=self._run_total,
                                     current_label=self._run_current_label)

    def _stop_icon_pulse(self, mark_benchmark=True, status_text="空闲"):
        msg = self.tr("status.idle") if status_text == "空闲" else status_text
        self._stop_status_animation(success=True, message=msg)
    # ── end lightweight status animation ──

    # ── progress overlay ──────────────────────────────────────────────────────
    def _show_progress_overlay(self, run_type: str, hc_mode: bool = False):
        """Create and display the non-modal progress overlay.
        Must only be called on the main (Tk) thread.
        hc_mode=True → larger window with 2-line detail for high-concurrency sweeps."""
        # Destroy any lingering overlay first
        if self.progress_overlay is not None:
            try:
                self.progress_overlay.destroy()
            except Exception:
                pass
            self.progress_overlay = None

        if hc_mode:
            title_text = self.tr("sweep.hc_mode_title")
        elif run_type == "benchmark":
            title_text = self.tr("progress.benchmarking")
        else:
            title_text = self.tr("progress.sweeping")

        ov = tk.Toplevel(self.root)
        ov.title(f"JISUMEN LLM Benchmark v{APP_VERSION}")
        ov.transient(self.root)          # child of main window — non-modal
        ov.resizable(False, False)
        ov.configure(bg=C_STYLE["bg_card"],
                     highlightbackground=C_STYLE["border"],
                     highlightthickness=1)
        # Disable the close button so it cannot be closed mid-run
        ov.protocol("WM_DELETE_WINDOW", lambda: None)
        # NOTE: do NOT call grab_set() or wait_window() — non-modal required

        # Size and center over main window (larger for HC mode)
        W, H = (560, 145) if hc_mode else (460, 120)
        self.root.update_idletasks()
        rx = self.root.winfo_x()
        ry = self.root.winfo_y()
        rw = self.root.winfo_width()
        rh = self.root.winfo_height()
        x = rx + (rw - W) // 2
        y = ry + (rh - H) // 2
        ov.geometry(f"{W}x{H}+{x}+{y}")

        # ── content ──
        frame = tk.Frame(ov, bg=C_STYLE["bg_card"])
        frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=12)

        lbl_title = tk.Label(
            frame,
            text=title_text,
            font=(FONT_FAMILY, 13, "bold"),
            bg=C_STYLE["bg_card"],
            fg=C_STYLE["text_primary"],
            anchor="w",
        )
        lbl_title.pack(fill=tk.X)

        self.progress_overlay_detail_var.set(self.tr("progress.preparing"))
        lbl_detail = tk.Label(
            frame,
            textvariable=self.progress_overlay_detail_var,
            font=(FONT_FAMILY, 10 if hc_mode else 11),
            bg=C_STYLE["bg_card"],
            fg=C_STYLE["text_secondary"],
            anchor="w",
            justify=tk.LEFT,
            wraplength=W - 48,
        )
        lbl_detail.pack(fill=tk.X, pady=(2, 8))
        self.progress_overlay_label = lbl_detail

        # Indeterminate progress bar — lightweight, native
        # HC mode: slower animation (500ms) to reduce CPU; normal: 150ms
        pb = ttk.Progressbar(frame, mode="indeterminate", length=W - 40)
        pb.pack(fill=tk.X)
        pb.start(500 if hc_mode else 150)
        self.progress_overlay_canvas = pb  # store reference for stop/destroy

        self.progress_overlay = ov
        self.progress_overlay_running = True

    def _hide_progress_overlay(self):
        """Close the progress overlay and release resources.
        Must only be called on the main (Tk) thread."""
        self.progress_overlay_running = False
        # Cancel any pending after callbacks
        if self.progress_overlay_after_id is not None:
            try:
                self.root.after_cancel(self.progress_overlay_after_id)
            except Exception:
                pass
            self.progress_overlay_after_id = None
        # Stop the indeterminate progressbar
        if self.progress_overlay_canvas is not None:
            try:
                self.progress_overlay_canvas.stop()
            except Exception:
                pass
            self.progress_overlay_canvas = None
        # Destroy the Toplevel
        if self.progress_overlay is not None:
            try:
                self.progress_overlay.destroy()
            except Exception:
                pass
            self.progress_overlay = None
        self.progress_overlay_label = None

    def _update_progress_overlay(self, detail: str | None = None):
        """Update the overlay detail line.  Must only be called on main thread."""
        if not self.progress_overlay_running:
            return
        if detail is not None:
            try:
                self.progress_overlay_detail_var.set(detail)
            except Exception:
                pass

    def _animate_progress_overlay(self):
        """Reserved animation tick.
        No-op: ttk.Progressbar(indeterminate) self-animates via start()."""
        pass

    def _safe_set_progress_overlay_detail(self, detail: str):
        """Thread-safe helper: schedule a detail update on the main thread."""
        self.root.after(0, lambda d=detail: self._update_progress_overlay(d))
    # ── end progress overlay ──────────────────────────────────────────────────

    def _format_elapsed(self) -> str:
        if self._run_started_at is None:
            return "00:00"
        sec = int(time.perf_counter() - self._run_started_at)
        return f"{sec // 60:02d}:{sec % 60:02d}"

    def _reset_config(self):
        self.url_var.set("http://192.168.1.12:8000/v1")
        self.key_var.set("change-me-before-production")
        self.model_var.set("qwen3.5-122b-a10b-fp8")
        self.system_var.set("你是一个有帮助的助手。")
        self.prompt_var.set("请用300字左右介绍机器学习。")
        self.max_tokens_var.set(512)
        if hasattr(self, "output_length_mode_var"):
            self.output_length_mode_var.set("normal")
        self.temp_var.set(0.0)
        self.total_var.set("80")
        self.concurrency_var.set(DEFAULT_PRESET_KEY)
        self.stream_var.set("是")
        self.warmup_var.set(2)
        self._load_config()  # overlay INI values if available
        self._action_status.config(text=self.tr("status.reset_action"))

    def _can_start_run(self, run_type):
        return self._active_run_type is None

    def _begin_run(self, run_type):
        if not hasattr(self, "_run_lock"):
            self._run_lock = threading.Lock()
        with self._run_lock:
            if getattr(self, "_active_run_type", None) is not None:
                return False
            self._active_run_type = run_type
        self._refresh_run_buttons()
        return True

    def _end_run(self, run_type=None):
        if not hasattr(self, "_run_lock"):
            self._run_lock = threading.Lock()
        with self._run_lock:
            if run_type is None or getattr(self, "_active_run_type", None) == run_type:
                self._active_run_type = None
        self._refresh_run_buttons()

    def _refresh_run_buttons(self):
        active = getattr(self, "_active_run_type", None)
        if hasattr(self, "start_btn"):
            if active == "benchmark":
                self.start_btn.configure(
                    text=self.tr("button.benchmarking", "基准测试中..."),
                    state="disabled")
            elif active == "sweep":
                self.start_btn.configure(
                    text=self.tr("button.start_benchmark", "开始基准测试"),
                    state="disabled")
            else:
                self.start_btn.configure(
                    text=self.tr("button.start_benchmark", "开始基准测试"),
                    state="normal")
        if hasattr(self, "sweep_start_btn"):
            if active == "benchmark":
                self.sweep_start_btn.configure(
                    text=self.tr("button.start_sweep", "开始扫测"),
                    state="disabled")
            elif active == "sweep":
                self.sweep_start_btn.configure(
                    text=self.tr("button.sweeping", "正在进行扫测......"),
                    state="disabled")
            else:
                self.sweep_start_btn.configure(
                    text=self.tr("button.start_sweep", "开始扫测"),
                    state="normal")

    def _show_run_busy(self, requested_run_type):
        active = getattr(self, "_active_run_type", None)
        if active == "benchmark":
            msg = self.tr("run.busy_benchmark")
        elif active == "sweep":
            msg = self.tr("run.busy_sweep")
        else:
            msg = self.tr("msg.running")
        try:
            self.status_label.config(text=msg)
        except Exception:
            pass
        messagebox.showwarning(self.tr("msg.warning"), msg)

    def _set_sweep_running_state(self, is_running: bool):
        self._sweep_running = bool(is_running)
        self._refresh_run_buttons()

    def _load_config(self):
        """Load defaults from INI file. Silently skip if file missing or malformed."""
        cfg = ConfigParser()
        try:
            cfg.read(INI_PATH, encoding="utf-8")
        except Exception:
            return
        if not cfg.sections():
            return
        if cfg.has_section("api"):
            self.url_var.set(cfg.get("api", "url",
                             fallback=self.url_var.get()))
            self.key_var.set(cfg.get("api", "key",
                             fallback=self.key_var.get()))
            self.model_var.set(cfg.get("api", "model",
                               fallback=self.model_var.get()))
        if cfg.has_section("prompt"):
            self.system_var.set(cfg.get("prompt", "system",
                                fallback=self.system_var.get()))
            self.prompt_var.set(cfg.get("prompt", "user",
                                fallback=self.prompt_var.get()))
        if cfg.has_section("test"):
            self.max_tokens_var.set(cfg.getint("test", "max_tokens",
                                    fallback=self.max_tokens_var.get()))
            self.temp_var.set(cfg.getfloat("test", "temperature",
                              fallback=self.temp_var.get()))
            self.total_var.set(str(int(cfg.getint("test", "total_requests",
                               fallback=80))))
            concurrency_label = cfg.get("test", "concurrency",
                                        fallback=self.concurrency_var.get())
            if concurrency_label in BENCHMARK_PRESETS:
                self.concurrency_var.set(concurrency_label)
            self.save_report_var.set(cfg.get("test", "save_report",
                                     fallback=self.save_report_var.get()))
            self.stream_var.set(cfg.get("test", "stream_mode",
                                  fallback=self.stream_var.get()))
            output_length_mode = cfg.get("test", "output_length_mode",
                                         fallback=self.output_length_mode_var.get())
            if output_length_mode in ("normal", "fixed"):
                self.output_length_mode_var.set(output_length_mode)
            self.warmup_var.set(cfg.getint("test", "warmup",
                                fallback=2))
            self.auto_save_var.set(cfg.get("test", "auto_save",
                                   fallback=self.auto_save_var.get()))
        # sync Text widgets
        for label, t in getattr(self, '_text_widgets', {}).items():
            if "系统" in label:
                t.delete("1.0", tk.END); t.insert("1.0", self.system_var.get())
            elif "用户" in label:
                t.delete("1.0", tk.END); t.insert("1.0", self.prompt_var.get())
    def _save_config(self, silent: bool = False):
        """Write current settings to INI file."""
        cfg = ConfigParser()
        cfg["api"] = {
            "url": self.url_var.get(),
            "key": self.key_var.get(),
            "model": self.model_var.get(),
        }
        cfg["prompt"] = {
            "system": self.system_var.get(),
            "user": self.prompt_var.get(),
        }
        cfg["test"] = {
            "max_tokens": str(self.max_tokens_var.get()),
            "temperature": str(self.temp_var.get()),
            "total_requests": str(self.total_var.get()),
            "concurrency": self.concurrency_var.get(),
            "save_report": self.save_report_var.get(),
            "stream_mode": self.stream_var.get(),
            "output_length_mode": self.output_length_mode_var.get(),
            "warmup": str(self.warmup_var.get()),
            "auto_save": self.auto_save_var.get(),
        }
        cfg["ui"] = {"language": self.lang_code}
        with open(INI_PATH, "w", encoding="utf-8") as f:
            cfg.write(f)
        if not silent:
            self._action_status.config(text=self.tr("status.saved_config"))
    def _auto_save_check(self):
        """Auto-save if enabled. Silently skip if disabled."""
        if self.auto_save_var.get() == "是":
            try:
                self._save_config(silent=True)
            except Exception:
                pass  # never disrupt user for auto-save failures
    def _build_statusbar(self):
        sb = tk.Frame(self.root, bg=C_STYLE["bg_header"], height=32)
        sb.pack(fill=tk.X, side=tk.BOTTOM)
        sb.pack_propagate(False)
        inner = tk.Frame(sb, bg=C_STYLE["bg_header"])
        inner.pack(fill=tk.BOTH, expand=True,
                   padx=C_STYLE["pad_lg"], pady=2)
        self.status_label = tk.Label(inner, text=self.tr("status.ready"),
                                     font=C_STYLE["font_small"],
                                     bg=C_STYLE["bg_header"],
                                     fg=C_STYLE["text_secondary"])
        self.status_label.pack(side=tk.LEFT)
    def _toggle_key_visibility(self):
        self.key_entry.config(show="" if self.show_key.get() else "*")
    def _reset_indicators(self):
        for key in self.indicators:
            self.indicators[key].set_state("idle", "等待中")
    def _set_indicator(self, key: str, state: str, detail: str = ""):
        if key in self.indicators:
            self.indicators[key].set_state(state, detail)
    # ---------- error popup ----------
    def _show_error_popup(self, title: str, error_summary: str, advice: str):
        """Show a styled error dialog with categorized advice."""
        top = tk.Toplevel(self.root)
        top.title(title)
        top.configure(bg=C_STYLE["bg_main"])
        top.resizable(False, False)
        top.transient(self.root)
        top.grab_set()
        # header bar
        hdr = tk.Frame(top, bg=C_STYLE["error"], height=4)
        hdr.pack(fill=tk.X)
        hdr.pack_propagate(False)
        # body
        body = tk.Frame(top, bg=C_STYLE["bg_card"])
        body.pack(fill=tk.BOTH, expand=True, padx=C_STYLE["pad_lg"], pady=C_STYLE["pad_lg"])
        # icon + title
        title_row = tk.Frame(body, bg=C_STYLE["bg_card"])
        title_row.pack(fill=tk.X, pady=(0, C_STYLE["gap_md"]))
        tk.Label(title_row, text="⚠", font=(FONT_FAMILY, 24),
                 bg=C_STYLE["bg_card"], fg=C_STYLE["error"]).pack(side=tk.LEFT,
                 padx=(0, C_STYLE["pad_sm"]))
        tk.Label(title_row, text=title, font=C_STYLE["font_section"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"]).pack(
            side=tk.LEFT)
        # error summary
        tk.Label(body, text=error_summary, font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"],
                 wraplength=420, justify=tk.LEFT, anchor="w").pack(
            fill=tk.X, pady=(0, C_STYLE["gap_md"]))
        # advice section
        if advice:
            sep = tk.Frame(body, height=1, bg=C_STYLE["border"])
            sep.pack(fill=tk.X, pady=(0, C_STYLE["gap_md"]))
            tk.Label(body, text="排查建议", font=C_STYLE["font_small"],
                     bg=C_STYLE["bg_card"], fg=C_STYLE["text_muted"],
                     anchor="w").pack(fill=tk.X)
            tk.Label(body, text=advice, font=C_STYLE["font_body"],
                     bg=C_STYLE["bg_card"], fg=C_STYLE["text_secondary"],
                     wraplength=420, justify=tk.LEFT, anchor="w").pack(
                fill=tk.X, pady=(4, 0))
        # buttons
        btn_row = tk.Frame(body, bg=C_STYLE["bg_card"])
        btn_row.pack(fill=tk.X, pady=(C_STYLE["gap_md"], 0))
        ttk.Button(btn_row, text="在报告中查看详情", style="Secondary.TButton",
                   command=lambda: [top.destroy(), self.nb.select(self.bench_frame)]).pack(
            side=tk.LEFT)
        ttk.Button(btn_row, text="关闭", style="Primary.TButton",
                   command=top.destroy).pack(side=tk.RIGHT)
        # size and center
        top.update_idletasks()
        w, h = 480, top.winfo_reqheight()
        x = self.root.winfo_x() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - h) // 2
        top.geometry(f"{w}x{h}+{x}+{y}")

    @staticmethod
    def _categorize_api_error(err_type: str, err_msg: str) -> tuple[str, str]:
        """Return (error_summary, advice) for a given API error."""
        combined = err_type + " " + err_msg
        rules = [
            (["网络连接失败", "Connection refused", "timed out",
              "Name or service not known", "No route to host",
              "Connection reset", "getaddrinfo"],
             "目标主机通讯失败",
             "无法连接到 API 服务器，请检查：\n"
             "• API 地址是否正确（如 http://192.168.1.12:8000/v1）\n"
             "• 目标主机是否在运行，端口是否开放\n"
             "• 网络 / VPN / 防火墙是否阻止了连接\n"
             "• 尝试在浏览器中访问该地址验证"),
            (["401"],
             "API 密钥认证失败",
             "API Key 错误或已过期，请检查：\n"
             "• Key 是否完整复制（无多余空格）\n"
             "• Key 是否在 API 管理后台仍然有效\n"
             "• 是否需要重新生成 Key"),
            (["403"],
             "接口权限不足",
             "API Key 没有访问此模型的权限，请检查：\n"
             "• 账户是否有该模型的访问配额\n"
             "• Key 的权限范围是否包含此接口"),
            (["404"],
             "API 接口不存在",
             "请求的接口路径或模型名称有误，请检查：\n"
             "• URL 是否以 /v1/chat/completions 结尾\n"
             "• 模型名称是否拼写正确（区分大小写）\n"
             "• 该模型是否已在服务端部署"),
            (["429"],
             "请求被限流",
             "请求频率超过 API 限额，请尝试：\n"
             "• 降低并发数后重试\n"
             "• 等待配额重置（通常 1 分钟后恢复）\n"
             "• 联系服务提供方提升配额"),
            (["500", "502", "503", "504"],
             "服务器内部错误",
             "API 服务端出现临时故障，请尝试：\n"
             "• 等待几分钟后重试\n"
             "• 如持续出现，联系服务提供方\n"
             "• 查看服务端日志排查"),
            (["响应格式错误", "JSONDecodeError", "Expecting value"],
             "响应格式不兼容",
             "API 返回的内容不符合 OpenAI 格式，请检查：\n"
             "• 目标地址是否为 OpenAI 兼容接口\n"
             "• 服务端是否返回了错误页面（如 HTML）\n"
             "• 尝试用 curl 直接测试该接口"),
        ]
        for keywords, summary, advice in rules:
            if any(k in combined for k in keywords):
                return summary, advice
        return ("未知错误",
                f"错误详情: {err_msg[:200]}\n\n请检查 API 地址、Key 和模型名称后重试。")
    def _show_model_picker(self, models: list[str], current_model: str) -> str | None:
        """Show a dialog to let user pick a model from the fetched list.
        Returns the selected model ID, or None if user skips/cancels.
        Blocks until user makes a choice (modal dialog on main thread)."""
        result = [None]  # boxed for closure
        picked = threading.Event()

        def _ok():
            sel = listbox.curselection()
            if sel:
                result[0] = models[sel[0]]
            picked.set()
            dlg.destroy()

        def _skip():
            result[0] = None
            picked.set()
            dlg.destroy()

        dlg = tk.Toplevel(self.root)
        dlg.title("选择模型")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.configure(bg=C_STYLE["bg_card"])
        # Center on parent
        dlg.update_idletasks()
        pw = self.root.winfo_width()
        ph = self.root.winfo_height()
        px = self.root.winfo_rootx()
        py = self.root.winfo_rooty()
        w, h = 480, 400
        x = px + (pw - w) // 2
        y = py + (ph - h) // 2
        dlg.geometry(f"{w}x{h}+{x}+{y}")

        # Header
        hdr = tk.Frame(dlg, bg=C_STYLE["bg_card"])
        hdr.pack(fill=tk.X, padx=C_STYLE["pad_lg"], pady=(C_STYLE["pad_lg"], C_STYLE["pad_sm"]))
        tk.Label(hdr, text="请选择要测试的模型",
                 font=C_STYLE["font_section"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"]).pack(anchor="w")
        tk.Label(hdr, text=f"API 返回了 {len(models)} 个可用模型",
                 font=C_STYLE["font_small"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_secondary"]).pack(anchor="w", pady=(4, 0))

        # Listbox with scrollbar
        lf = tk.Frame(dlg, bg=C_STYLE["bg_card"])
        lf.pack(fill=tk.BOTH, expand=True, padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["pad_sm"]))
        sb = ttk.Scrollbar(lf, orient=tk.VERTICAL)
        listbox = tk.Listbox(lf, font=C_STYLE["font_body"],
                             bg=C_STYLE["bg_input"],
                             fg=C_STYLE["text_primary"],
                             selectbackground=C_STYLE["accent"],
                             selectforeground=C_STYLE["text_inverse"],
                             yscrollcommand=sb.set,
                             borderwidth=1, relief="solid",
                             highlightthickness=0)
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        sb.config(command=listbox.yview)
        for m in models:
            listbox.insert(tk.END, m)
        # Pre-select current model if it exists in the list
        try:
            idx = models.index(current_model)
            listbox.selection_set(idx)
            listbox.activate(idx)
            listbox.see(idx)
        except ValueError:
            pass
        # Double-click to confirm
        listbox.bind("<Double-Button-1>", lambda e: _ok())

        # Buttons
        btnf = tk.Frame(dlg, bg=C_STYLE["bg_card"])
        btnf.pack(fill=tk.X, padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["pad_lg"]))
        skip_btn = ttk.Button(btnf, text="跳过", command=_skip)
        skip_btn.pack(side=tk.LEFT, padx=(0, C_STYLE["pad_sm"]))
        tk.Label(btnf, text="（将使用已填写的模型）",
                 font=C_STYLE["font_small"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_secondary"]).pack(side=tk.LEFT)
        ttk.Button(btnf, text="确认选择", style="Primary.TButton", command=_ok).pack(side=tk.RIGHT)

        # Return key to confirm
        dlg.bind("<Return>", lambda e: _ok())
        dlg.bind("<Escape>", lambda e: _skip())
        dlg.protocol("WM_DELETE_WINDOW", _skip)

        # Wait for user choice
        dlg.wait_window()
        picked.wait(timeout=120)
        return result[0]
    def _on_query_models(self):
        """Handle「查询」button click: fetch models from API and let user pick."""
        api_url = self.url_var.get().strip()
        if not api_url:
            messagebox.showerror("错误", "请先填写 API 地址")
            return
        api_url = normalize_api_url(api_url)
        api_key = self.key_var.get().strip()
        self._action_status.config(text="正在获取模型列表...")
        self.root.update_idletasks()
        # Fetch models in a background thread to avoid freezing UI
        result_box = []
        def _fetch():
            models, err = fetch_models(api_url, api_key)
            result_box.append((models, err))
            # Show picker on main thread
            self.root.after(0, lambda: self._show_query_result(models, err))
        threading.Thread(target=_fetch, daemon=True).start()
    def _show_query_result(self, models: list[str], err: str):
        if models:
            picked = self._show_model_picker(models, self.model_var.get().strip())
            if picked is not None:
                self.model_var.set(picked)
            self._action_status.config(text="就绪 — 请配置参数后开始测试")
        else:
            self._action_status.config(text="就绪 — 请配置参数后开始测试")
            title, detail = self._categorize_query_error(err)
            messagebox.showerror(title, detail)
    def _categorize_query_error(self, err: str) -> tuple[str, str]:
        """Categorize model fetch error into friendly title + detail message."""
        err_lower = err.lower()
        # Connection-level errors
        if any(k in err_lower for k in ("connection refused", "no route to host",
                                          "name or service not known", "getaddrinfo",
                                          " network ", "unreachable", "econnrefused")):
            return ("连接失败",
                "无法连接到 API 服务器。\n\n"
                "请检查：\n"
                "• API 地址是否正确（如 http://192.168.1.12:8000/v1）\n"
                "• 服务端是否在运行、端口是否开放\n"
                "• 网络 / 防火墙是否正常")
        # Timeout
        if "time" in err_lower and "out" in err_lower:
            return ("连接超时",
                "连接 API 服务器超时。\n\n"
                "请检查：\n"
                "• 网络连接是否正常\n"
                "• API 地址是否可达\n"
                "• 稍后重试")
        # HTTP errors
        if "http error 401" in err_lower or "unauthorized" in err_lower:
            return ("认证失败",
                "API 密钥认证失败（401 未授权）。\n\n"
                "请检查：\n"
                "• API 密钥是否正确\n"
                "• 密钥是否已过期或被禁用")
        if "http error 403" in err_lower or "forbidden" in err_lower:
            return ("权限不足",
                "没有权限访问模型列表（403 禁止访问）。\n\n"
                "请检查：\n"
                "• API 密钥是否有该接口的访问权限\n"
                "• 账户配额是否充足")
        if "http error 404" in err_lower or "not found" in err_lower:
            return ("接口不存在",
                "API 接口不存在（404 未找到）。\n\n"
                "请检查：\n"
                "• API 地址路径是否正确\n"
                "• 该服务是否支持 /v1/models 接口\n"
                "• 尝试在浏览器中打开该地址")
        if "http error 429" in err_lower:
            return ("请求限流",
                "请求频率超过限额（429 限流）。\n\n"
                "请稍后重试，通常等待 1 分钟后恢复。")
        if "http error 5" in err_lower or "server error" in err_lower or \
           "internal server" in err_lower:
            return ("服务器错误",
                "API 服务端出现临时故障。\n\n"
                "请稍后重试，如持续出现请联系服务提供方。")
        # JSON / format errors
        if any(k in err_lower for k in ("json", "expecting value", "decode")):
            return ("响应格式异常",
                "API 返回的内容不符合预期格式。\n\n"
                "请确认该地址是否为 OpenAI 兼容接口。")
        # Fallback — sanitize the raw error
        safe_err = err[:200] if len(err) > 200 else err
        if not safe_err:
            safe_err = "API 未返回任何模型"
        return ("获取模型失败",
            f"无法获取模型列表。\n\n错误：{safe_err}\n\n"
            "请手动输入模型名称后重试。")
    def _on_preflight_fail(self, step: str, payload):
        self.progress["value"] = 0
        self._stop_status_animation(success=False, completed=self._run_completed,
                                    total=self._run_total, fail=max(self._run_fail, 1),
                                    message=self.tr("status.failed"))
        self.result_text.config(state=tk.NORMAL)
        self.result_text.delete("1.0", tk.END)
        if step == "connectivity":
            self._set_indicator("connectivity", "fail", "连接失败")
            self.status_label.config(text="服务器连接失败")
            self.result_text.insert(tk.END,
                "服务器连通性检测失败\n\n"
                f"错误: {payload}\n\n"
                "处理方法:\n"
                "• 检查 API 地址是否正确（如 http://192.168.1.12/v1）\n"
                "• 确认服务端是否在运行，端口是否开放\n"
                "• 检查网络 / VPN / 代理是否正常\n"
                "• 尝试在浏览器中访问该地址")
            summary, advice = self._categorize_api_error("网络连接失败", str(payload))
            self.root.after(50, lambda: self._show_error_popup("连通性检测失败", summary, advice))
        else:
            self._set_indicator("connectivity", "pass", "连接正常")
            self._set_indicator("smoke", "fail", "测试失败")
            self.status_label.config(text="基线测试失败")
            err_msg = payload.get("error", "未知错误") if isinstance(payload, dict) else str(payload)
            err_type = payload.get("error_type", "") if isinstance(payload, dict) else ""
            self.result_text.insert(tk.END,
                f"基线测试失败（单次请求未通过，不进入压力测试）\n\n"
                f"错误类型: {err_type}\n错误详情: {err_msg}\n\n")
            _, advice = self._analyze_failures([payload] if isinstance(payload, dict) else
                                               [{"error": str(payload), "error_type": ""}])
            self.result_text.insert(tk.END, advice + "\n\n请修正配置后重新测试。")
            summary, popup_advice = self._categorize_api_error(err_type, err_msg)
            self.root.after(50, lambda: self._show_error_popup("基线测试失败", summary, popup_advice))
        self.result_text.config(state=tk.DISABLED)
    def _start_benchmark(self):
        active = getattr(self, "_active_run_type", None)
        if active == "benchmark":
            return
        if active is not None:
            self._show_run_busy("benchmark")
            return
        api_url = self.url_var.get().strip()
        api_key = self.key_var.get().strip()
        model = self.model_var.get().strip()
        system_prompt = self.system_var.get().strip()
        user_prompt = self.prompt_var.get().strip()
        max_tokens = self.max_tokens_var.get()
        output_length_mode = self.output_length_mode_var.get()
        temperature = self.temp_var.get()
        try:
            total = int(float(str(self.total_var.get()).strip()))
        except Exception:
            total = 1
        concurrency_label = self.concurrency_var.get()
        CUSTOM_KEY2 = "自定义"
        if concurrency_label == CUSTOM_KEY2:
            try:
                concurrency = int(float(str(self.custom_conc_var.get()).strip()))
            except Exception:
                concurrency = 1
        else:
            preset_cfg = BENCHMARK_PRESETS.get(concurrency_label, {})
            concurrency = preset_cfg.get("concurrency", 8)
        preset_name = concurrency_label
        stream = self.stream_var.get() == "是"
        try:
            warmup = int(float(str(self.warmup_var.get()).strip()))
        except Exception:
            warmup = 0
        if not api_url:
            messagebox.showerror(self.tr("msg.error"), self.tr("msg.api_required"))
            return
        if not user_prompt:
            messagebox.showerror(self.tr("msg.error"), self.tr("msg.prompt_required"))
            return

        # C32+ pressure test confirmation
        if concurrency >= 32:
            ok = messagebox.askyesno(
                "压力测试确认",
                f"您选择了「{preset_name}」\n\n"
                f"并发={concurrency} 请求={total}\n\n"
                "高并发压力测试可能导致：\n"
                "• 服务端排队严重，延迟大幅上升\n"
                "• API 限流 (429) 或服务端超时\n"
                "• GPU 显存压力增大\n\n"
                "确定要继续吗？"
            )
            if not ok:
                return

        api_url = normalize_api_url(api_url)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        if DEBUG_MODE:
            logging.info("benchmark requested: url=%s model=%s concurrency=%d total=%d warmup=%d preset=%s",
                         api_url, model, concurrency, total, warmup, preset_name)
        if not self._begin_run("benchmark"):
            self._show_run_busy("benchmark")
            return
        self._show_progress_overlay("benchmark")
        self._start_status_animation(phase="benchmark", total=total)
        self._action_status.config(text="准备开始...")
        self._reset_indicators()
        self.progress["value"] = 0
        self.status_label.config(text="准备开始...")
        self.nb.select(self.bench_frame)
        self.result_text.config(state=tk.NORMAL)
        self.result_text.delete("1.0", tk.END)
        self.result_text.insert(tk.END, "等待测试...\n")
        self.result_text.config(state=tk.DISABLED)
        self.hist_canvas.delete("all")
        for mi in self.metrics.values():
            mi.set_value("—")
        self.notice_banner.set_text("")
        t = threading.Thread(
            target=self._run_benchmark_thread,
            args=(api_url, api_key, model, messages, max_tokens, temperature,
                  concurrency, total, stream, warmup, preset_name,
                  output_length_mode),
            daemon=True,
        )
        t.start()

    def _run_benchmark_thread(self, api_url, api_key, model, messages,
                              max_tokens, temperature, concurrency, total, stream,
                              warmup=0, preset_name="",
                              output_length_mode="normal"):
        try:
            self._run_preflight_and_benchmark(
                api_url, api_key, model, messages, max_tokens, temperature,
                concurrency, total, stream, warmup, preset_name,
                output_length_mode)
        except Exception as e:
            if DEBUG_MODE:
                logging.exception("benchmark worker failed: %s", e)
            self.root.after(0, lambda: self._stop_status_animation(
                success=False, fail=max(self._run_fail, 1),
                message=self.tr("status.failed")))
            self.root.after(0, lambda err=str(e): self.status_label.config(
                text=f"{self.tr('status.failed')}: {err}"))
        finally:
            self.root.after(0, self._hide_progress_overlay)
            self.root.after(0, lambda: self._end_run("benchmark"))

    def _run_preflight_and_benchmark(self, api_url, api_key, model, messages,
                                      max_tokens, temperature, concurrency, total, stream,
                                      warmup=0, preset_name="",
                                      output_length_mode="normal"):
        if DEBUG_MODE:
            logging.info("preflight: step 1 — connectivity check")
        self.root.after(0, lambda: self._set_indicator("connectivity", "checking"))
        self.root.after(0, lambda: self.status_label.config(
            text="正在检测服务器连通性...", fg=C_STYLE["accent"]))
        reachable, err = check_server_reachable(api_url)
        if not reachable:
            if DEBUG_MODE:
                logging.warning("preflight FAIL at connectivity: %s", err)
            self.root.after(0, lambda: self._on_preflight_fail("connectivity", err))
            return
        self.root.after(0, lambda: self._set_indicator("connectivity", "pass", "连接正常"))
        # ── Step 1.5: fetch model list and let user pick if needed ──
        if DEBUG_MODE:
            logging.info("preflight: step 1.5 — fetch model list")
        self.root.after(0, lambda: self.status_label.config(
            text="正在获取模型列表...", fg=C_STYLE["accent"]))
        models, fetch_err = fetch_models(api_url, api_key)
        if models:
            model_trimmed = model.strip()
            if model_trimmed and model_trimmed in models:
                # Model is valid — skip picker, use it directly
                self.root.after(0, lambda: self.status_label.config(
                    text="模型已验证，跳过选择...", fg=C_STYLE["success"]))
                if DEBUG_MODE:
                    logging.info("model '%s' found in API list, skipping picker", model)
            else:
                # Model is empty or not in list — show picker
                if DEBUG_MODE:
                    logging.info("model '%s' not in API list, showing picker", model)
                model_event = threading.Event()
                model_result = [None]
                def _pick_model():
                    model_result[0] = self._show_model_picker(models, model)
                    model_event.set()
                self.root.after(0, _pick_model)
                self.root.after(0, lambda: self.status_label.config(
                    text="请在弹出的窗口中选择模型", fg=C_STYLE["accent"]))
                model_event.wait()
                if model_result[0] is not None:
                    model = model_result[0]
                    self.root.after(0, lambda: self.model_var.set(model))
                    if DEBUG_MODE:
                        logging.info("user selected model: %s", model)
                else:
                    if DEBUG_MODE:
                        logging.info("user skipped model picker, using: %s", model)
        elif fetch_err:
            if DEBUG_MODE:
                logging.warning("model fetch failed, using manual entry: %s", fetch_err)
        # ── Step 2: warmup + smoke test ──
        if warmup > 0:
            if DEBUG_MODE:
                logging.info("preflight: warmup — sending %d warmup requests", warmup)
            self.root.after(0, lambda: self.status_label.config(
                text=f"预热中 ({warmup} 次请求)...", fg=C_STYLE["accent"]))
            for i in range(warmup):
                call_llm(api_url, api_key, model, messages, max_tokens, temperature,
                        stream=stream, output_length_mode=output_length_mode)
                self.root.after(0, lambda c=i+1: self._action_status.config(
                    text=f"预热中 — {c}/{warmup}"))

        if DEBUG_MODE:
            logging.info("preflight: step 2 — smoke test")
        self.root.after(0, lambda: self._set_indicator("smoke", "checking"))
        self.root.after(0, lambda: self.status_label.config(
            text="正在执行基线测试 (1 次请求)...", fg=C_STYLE["accent"]))
        smoke = call_llm(api_url, api_key, model, messages, max_tokens, temperature,
                        stream=stream, output_length_mode=output_length_mode)
        if not smoke["ok"]:
            if DEBUG_MODE:
                logging.warning("preflight FAIL at smoke: type=%s error=%s",
                               smoke.get("error_type", ""), smoke.get("error", ""))
            self.root.after(0, lambda: self._on_preflight_fail("smoke", smoke))
            return
        lat = smoke.get("latency", 0)
        self._smoke_latency = lat
        self.root.after(0, lambda: self._set_indicator("smoke", "pass", f"{lat:.2f}s"))
        if DEBUG_MODE:
            logging.info("preflight: step 3 — full benchmark (concurrency=%d, total=%d)",
                        concurrency, total)
        self.root.after(0, lambda: self._set_indicator("benchmark", "checking"))
        self.root.after(0, lambda: self.status_label.config(
            text="正在执行压力测试...", fg=C_STYLE["accent"]))
        self.root.after(0, lambda: self.progress.configure(maximum=total))

        # Objective and mode are fixed for single-run benchmarks
        _bench_objective = "single_session_decode_speed"
        _bench_mode      = "real_api_experience"

        def _done_with_warmup(s):
            s["warmup_requests"] = warmup
            # Inject token calibration fields if calibration was applied
            if self._actual_prompt_tokens:
                s["actual_prompt_tokens"] = self._actual_prompt_tokens
            if self._target_input_tokens:
                s["target_input_tokens"] = self._target_input_tokens
            if self._target_output_tokens:
                s["target_output_tokens"] = self._target_output_tokens
            if self._calibration_method:
                s["calibration_method"] = self._calibration_method
            if self._prompt_mode:
                s["prompt_mode"] = self._prompt_mode
            s["token_tolerance"] = 8
            # ── Customer-facing fields ──
            if _bench_objective:
                s["benchmark_objective"] = _bench_objective
            if _bench_mode:
                s["benchmark_mode"] = _bench_mode
            self._on_done(s)

        run_benchmark(api_url, api_key, model, messages, max_tokens, temperature,
                      concurrency, total, self._on_progress, _done_with_warmup,
                      stream=stream, preset_name=preset_name,
                      output_length_mode=output_length_mode)
    def _on_progress(self, completed, total, fail=0):
        self.root.after(0, lambda: self._update_progress(completed, total, fail))
    def _update_progress(self, completed, total, fail=0):
        self.progress["value"] = completed
        if total:
            self.progress["maximum"] = total
        self._update_status_animation(completed=completed, total=total,
                                      fail=fail, phase="benchmark")
        elapsed = self._format_elapsed()
        self.status_label.config(text=f"进度: {completed}/{total}")
        self._action_status.config(
            text=f"测试中 — {completed}/{total} · fail={fail} · {elapsed}")
        self._set_indicator("benchmark", "checking", f"{completed}/{total}")
        self._update_progress_overlay(f"{completed} / {total} · fail={fail} · {elapsed}")
    def _on_done(self, summary: dict):
        self.root.after(0, lambda: self._show_results(summary))
    def _show_results(self, summary: dict):
        elapsed = self._format_elapsed()
        self._action_status.config(
            text=f"已完成 — {summary['total']} 请求 · success={summary['success']} · fail={summary['fail']} · {elapsed}")
        total_req = max(summary["total"], 1)
        success_rate = summary["success"] / total_req * 100
        if summary["fail"] == 0:
            self._set_indicator("benchmark", "pass", f"{success_rate:.0f}% 通过")
            self._stop_status_animation(success=True, completed=summary["total"],
                                        total=summary["total"], fail=summary["fail"],
                                        message=self.tr("status.completed"))
            self.status_label.config(text="测试完成")
        elif summary["success"] > 0:
            self._set_indicator("benchmark", "fail", f"{success_rate:.0f}% 通过")
            self._stop_status_animation(success=True, completed=summary["success"],
                                        total=summary["total"], fail=summary["fail"],
                                        message=self.tr("status.completed"))
            self.status_label.config(text="测试完成（部分失败）")
        else:
            self._set_indicator("benchmark", "fail", "全部失败")
            self._stop_status_animation(success=False, completed=0,
                                        total=summary["total"], fail=summary["fail"],
                                        message=self.tr("status.failed"))
            self.status_label.config(text="测试完成（全部失败）")
            # popup with categorized error for first failure
            fail_detail = summary.get("fail_detail", [])
            if fail_detail:
                r = fail_detail[0]
                err_type = r.get("error_type", "")
                err_msg = r.get("error", "未知错误")
                esum, eadv = self._categorize_api_error(err_type, err_msg)
                self.root.after(100, lambda: self._show_error_popup(
                    "压力测试全部失败", esum, eadv))
        self._set_indicator("duration", "idle", f"{summary['duration_sec']:.1f}s")
        # ── 8 metric cards ──
        # Row 0
        self.metrics["ttft"].set_value(
            f"{summary['ttft_avg']:.3f}s" if summary.get("stream_mode") and summary.get("ttft_avg", 0) > 0 else "N/A")
        vt = summary.get("visible_ttft_avg")
        self.metrics["visible_ttft"].set_value(
            f"{vt:.3f}s" if summary.get("stream_mode") and vt is not None and vt > 0 else "N/A")
        self.metrics["system_output_tps"].set_value(
            f"{summary['system_output_tps']:.1f} tok/s")
        self.metrics["rps"].set_value(f"{summary['request_throughput_rps']:.2f} req/s")
        # Row 1
        tpot_val = summary.get("tpot_avg", 0)
        self.metrics["tpot"].set_value(
            f"{tpot_val:.3f}s" if summary.get("stream_mode") and tpot_val > 0 else "N/A")
        itl_val = summary.get("itl_avg", 0)
        self.metrics["itl"].set_value(
            f"{itl_val:.3f}s" if summary.get("stream_mode") and itl_val > 0 else "N/A")
        self.metrics["e2e_p95"].set_value(f"{summary['e2e_latency_p95']:.3f}s")
        self.metrics["success_rate"].set_value(f"{summary.get('success_rate', 0):.1f}%")
        diag = self._diagnose(summary)
        level = "success" if summary["fail"] == 0 and len(diag) == 1 else \
                "warn" if summary["fail"] == 0 else "error"
        self.notice_banner._level = level
        self.notice_banner.set_text("\n".join(diag) if diag else "")
        if DEBUG_MODE:
            logging.info("result: success=%d fail=%d rate=%.1f%% e2e_avg=%.3fs e2e_p95=%.3fs output_tps=%.1f",
                         summary["success"], summary["fail"], success_rate,
                         summary["e2e_latency_avg"], summary["e2e_latency_p95"],
                         summary["system_output_tps"])
        try:
            save_result(summary)
        except Exception as e:
            if DEBUG_MODE:
                logging.warning("save to db failed: %s", e)
        env_info = self._get_active_env_info()
        report = self._generate_report_v2(summary, env_info)
        # Append failure analysis if present
        fail_detail = summary.get("fail_detail", [])
        if fail_detail:
            error_summary, advice = self._analyze_failures(fail_detail)
            report += "\n\n  ═══════════ 失败请求分析 ═══════════\n\n"
            report += error_summary + "\n\n"
            report += advice

        # ── Auto-save to ResultStore (always) ──
        results_root = (getattr(self, "results_root_var", None) or
                        tk.StringVar(value=RESULTS_ROOT)).get() or RESULTS_ROOT
        _rs_run_dir = ""
        try:
            e2e_lats = self._get_success_e2e_latencies(summary)
            _rs_out = rs_save_single_run(
                results_root, summary, report, report,
                e2e_lats, env_info,
                config={"concurrency": summary.get("concurrency"),
                        "total_requests": summary.get("total"),
                        "max_tokens": summary.get("max_tokens"),
                        "temperature": summary.get("temperature"),
                        "stream_mode": summary.get("stream_mode"),
                        "output_length_mode": summary.get("output_length_mode"),
                        "api_url": summary.get("api_url"),
                        "model": summary.get("model")}
            )
            _rs_run_dir = _rs_out.get("run_dir", "")
            setattr(self, "_last_single_run_dir", _rs_run_dir)
            logging.info("ResultStore saved to: %s", _rs_run_dir)
        except Exception as _rs_err:
            logging.warning("ResultStore save failed: %s", _rs_err)

        # ── Legacy result DB save ──
        try:
            db_path = getattr(self, "result_db_path_var", tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
            hw = env_info.get("hw", {})
            sw = env_info.get("sw", {})
            gpu_slug = _make_slug(hw.get("gpu_model") or hw.get("gpu_model_custom") or "unknown")
            gpu_count = hw.get("gpu_count") or hw.get("gpu_count_custom") or "1"
            backend_slug = _make_slug(sw.get("backend") or sw.get("backend_custom") or "unknown")
            model_slug = _make_slug(summary.get("model", "unknown"))
            conc = summary.get("concurrency", 0)
            total = summary.get("total", 0)
            mt = summary.get("max_tokens", 0)
            mode = summary.get("output_length_mode", "normal")
            params_slug = f"C{conc}-N{total}__out{mt}-{_make_slug(mode)}"
            _legacy_run_dir = (_rs_run_dir or
                               _make_run_dir(results_root, "single", model_slug, backend_slug,
                                             gpu_slug, str(gpu_count), params_slug))
            _result_db_save_benchmark_run(
                db_path, summary,
                env_info.get("env_profile_id"),
                env_info.get("hw_profile_id"),
                env_info.get("sw_profile_id"),
                env_info.get("model_profile_id"),
                _legacy_run_dir,
                snapshots=env_info.get("snapshots", {}),
                applied_info={
                    "applied_environment_params": self._applied_environment_params,
                    "applied_fields_json":        self._applied_fields_json,
                    "overridden_fields_json":     self._overridden_fields_json,
                })
        except Exception as e:
            logging.warning("result DB save failed: %s", e)

        # ── Legacy per-run report file (if "save report" enabled) ──
        if self.save_report_var.get() == "是":
            try:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                report_path = os.path.join(_SCRIPT_DIR, f"llm_benchmark_report_{ts}.txt")
                with open(report_path, "w", encoding="utf-8") as f:
                    f.write(report)
            except Exception:
                pass

        self.result_text.config(state=tk.NORMAL)
        self.result_text.delete("1.0", tk.END)
        self.result_text.insert(tk.END, report)
        self.result_text.config(state=tk.DISABLED)

        # Auto-expand report to show results
        if self._report_collapsed:
            self._report_card.content.grid()
            self._report_card.title_lbl.config(text="▼ 详细报告")
            self._report_collapsed = False
        self._draw_histogram(summary)
        self._refresh_history()
        self._refresh_side_pages()
        self._notice_demo_data()

    def _refresh_side_pages(self):
        """跑测/清空后同步刷新总览、历史对比、报告与证据（UI/UX v2 新页面）。"""
        for name in ("_refresh_overview", "_refresh_compare_options", "_refresh_export"):
            fn = getattr(self, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass

    def _notice_demo_data(self):
        """演示数据可见标注（规范 §6）：当前端点为本地 mock 时在结果页提示。"""
        try:
            from llm_benchmark_app.ui_pages import detect_data_source
            if detect_data_source({"api_url": self.url_var.get(),
                                   "model": self.model_var.get()}) == "demo":
                self.notice_banner.set_text(
                    "演示数据：本次结果来自本地 mock 端点（非真实硬件/模型性能），"
                    "不可用于对外性能结论。")
        except Exception:
            pass

    def _diagnose(self, summary: dict) -> list[str]:
        tips = []
        total = max(summary["total"], 1)
        concurrency = summary["concurrency"]
        fail_detail = summary.get("fail_detail", [])
        stream_mode = summary.get("stream_mode", False)

        if not stream_mode:
            tips.append("  ℹ 非流式模式：TTFT/TPOT/ITL 不可用 (vLLM bench serve 需 stream)。")

        if summary.get("output_length_mode") == "fixed":
            if summary.get("fixed_output_validation_passed"):
                tips.append(f"  ✓ {self.tr('label.fixed_output_validation_passed')}")
            else:
                tips.append(f"  ⚠ {self.tr('label.fixed_output_validation_failed')}")

        if fail_detail:
            tips.append("  ⚠ 存在失败请求，按类型分布：")
            cats: dict[str, int] = {}
            for r in fail_detail:
                et = r.get("error_type", "其他")
                cats[et] = cats.get(et, 0) + 1
            for cat, count in cats.items():
                tips.append(f"    - {cat}: {count} 次")
            tips.append("  处理方法请参见下方的错误分析。")
        if self._smoke_latency > 0 and summary["success"] > 0:
            ratio = summary["e2e_latency_avg"] / max(self._smoke_latency, 0.001)
            if ratio > 2.0 and concurrency > 1:
                tips.append(
                    f"  ⚠ 并发延迟放大: 平均 E2E ({summary['e2e_latency_avg']:.2f}s)"
                    f" 是基线 ({self._smoke_latency:.2f}s) 的 {ratio:.1f}x。")
                if ratio > 5:
                    tips.append("    服务端可能已达并发上限，建议降低并发数。")
                elif ratio > 3:
                    tips.append("    服务端负载较高，可适当降低并发数以获更低延迟。")
                else:
                    tips.append("    并发带来了可接受的延迟增加。")
        if concurrency >= 16 and summary['e2e_latency_p95'] > summary['e2e_latency_avg'] * 1.5:
            tips.append(
                f"  ⚠ P95 E2E ({summary['e2e_latency_p95']:.2f}s) 显著高于平均"
                f" ({summary['e2e_latency_avg']:.2f}s)，表明高并发下存在排队等待。")
            tips.append("    建议: 检查 vLLM --max-num-seqs / --max-model-len 等并发限制参数。")
        if summary["success"] > 0 and summary["duration_sec"] > 0:
            sys_tps = summary.get("system_output_tps", 0)
            per_req_tps = summary.get("per_request_output_tps_avg", 0)
            if concurrency >= 32 and sys_tps < per_req_tps * concurrency * 0.5:
                tips.append(
                    f"  ⚠ Output Token Throughput (~{sys_tps:.0f} tok/s) 远低于"
                    f" 理论值 ({per_req_tps * concurrency:.0f} tok/s)，"
                    f"服务端可能已达吞吐上限。")
                tips.append("    建议: 检查 vLLM --max-num-seqs 或 GPU 利用率。")

        # ── TTFT split diagnostics ──
        gap_avg = summary.get("first_visible_gap_avg") or 0
        gap_p95 = summary.get("first_visible_gap_p95") or 0
        if gap_avg > 0.5:
            tips.append(f"  ℹ 首包到首字间隔 (first_visible_gap_avg={gap_avg:.3f}s) > 0.5s，"
                        f"服务端已较早开始流式响应，但首个可见输出较晚出现。请关注 First Visible Token Latency。")
        if gap_p95 > 2.0:
            tips.append(f"  ⚠ 首包到首字 P95 长尾 ({gap_p95:.2f}s)，用户首字体验可能受影响。")

        tpot_avg = summary.get("tpot_avg", 0)
        itl_avg = summary.get("itl_avg", 0)
        if tpot_avg > 0 and itl_avg > 0 and abs(tpot_avg - itl_avg) / max(itl_avg, 1e-9) > 0.3:
            tips.append(f"  ⚠ TPOT ({tpot_avg:.4f}s) 与 ITL ({itl_avg:.4f}s) 差异较大，"
                        f"请检查 chunk/token 口径、completion_tokens 和 TTFT 口径。")

        visible_tpot_avg = summary.get("visible_tpot_avg") or 0
        if visible_tpot_avg > 0 and itl_avg > 0 and abs(visible_tpot_avg - itl_avg) / max(itl_avg, 1e-9) > 0.5:
            tips.append(f"  ℹ Visible TPOT ({visible_tpot_avg:.4f}s) 受首字延迟影响，仅供诊断，不作为主 TPOT。")
        if summary["fail"] == 0 and len(tips) <= (1 if not stream_mode else 0):
            tips.append("  ✓ 所有检查通过，未发现异常。")
        return tips
    def _generate_report(self, summary: dict) -> str:
        total = max(summary["total"], 1)
        success_rate = summary.get("success_rate", summary["success"] / total * 100)
        stream_mode = summary.get("stream_mode", False)
        sep = "─" * 58
        r = []
        r.append("=" * 60)
        r.append("  LLM Benchmark GUI — 性能测试报告")
        r.append("=" * 60)
        r.append(f"  测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        r.append("")
        r.append(f"  Metric Standard: {summary.get('metric_standard', 'JISUMAN LLM Benchmark Standard v1')}")
        r.append(f"  Reference: vLLM bench serve + NVIDIA GenAI-Perf/NIM-style serving benchmark")

        # ── 一、测试配置 ──
        r.append("")
        r.append(sep)
        r.append("  一、测试配置")
        r.append(sep)
        r.append(f"  API URL:     {summary['api_url']}")
        r.append(f"  Model:       {summary['model']}")
        r.append(f"  Concurrency: {summary['concurrency']}")
        r.append(f"  Total Req:   {summary['total']}")
        r.append(f"  Max Tokens:  {summary['max_tokens']}")
        output_mode = summary.get("output_length_mode", "normal")
        output_mode_label = (
            self.tr("label.output_mode_fixed_short")
            if output_mode == "fixed"
            else self.tr("label.output_mode_normal_short")
        )
        r.append(f"  {self.tr('label.output_length_mode')}: {output_mode_label}")
        if output_mode == "fixed":
            r.append(f"  {self.tr('label.fixed_output_tokens')}: {summary.get('fixed_output_tokens', 'N/A')}")
            r.append(f"  min_tokens_sent: {summary.get('min_tokens_sent', 'N/A')}")
            r.append(f"  ignore_eos: {str(summary.get('ignore_eos', False)).lower()}")
        r.append(f"  Temperature: {summary['temperature']}")
        r.append(f"  Stream Mode: {'流式 (stream=True)' if stream_mode else '非流式 (stream=False)'}")
        r.append(f"  Load Mode:   fixed concurrency")
        preset = summary.get("benchmark_preset_name", "")
        warmup_r = summary.get("warmup_requests", 0)
        if preset:
            r.append(f"  Benchmark Preset: {preset}")
        if warmup_r:
            r.append(f"  Warmup Requests:  {warmup_r}")

        # ── 二、成功失败 ──
        r.append("")
        r.append(sep)
        r.append("  二、成功 / 失败")
        r.append(sep)
        r.append(f"  Success:      {summary['success']}")
        r.append(f"  Fail:         {summary['fail']}")
        r.append(f"  Success Rate: {success_rate:.1f}%")
        fail_detail = summary.get("fail_detail", [])
        if fail_detail:
            from collections import Counter
            error_types = Counter(r_.get("error_type", "unknown") for r_ in fail_detail)
            r.append("  Error Types:")
            for et, count in error_types.most_common():
                r.append(f"    - {et}: {count}")

        # ── 三、延迟指标 (Latency Metrics) ──
        # Aligned with: vLLM bench serve (ttft/tpot/itl/e2el), NVIDIA GenAI-Perf
        r.append("")
        r.append(sep)
        r.append("  三、延迟指标 (Latency Metrics)")
        r.append(sep)
        r.append(f"  E2E Latency / E2EL（端到端延迟）— 请求发出到完整响应结束:")
        r.append(f"    min: {summary['e2e_latency_min']:.3f}s  avg: {summary['e2e_latency_avg']:.3f}s  max: {summary['e2e_latency_max']:.3f}s")
        r.append(f"    p50: {summary['e2e_latency_p50']:.3f}s  p95: {summary['e2e_latency_p95']:.3f}s  p99: {summary['e2e_latency_p99']:.3f}s")
        if stream_mode and summary.get("ttft_avg", 0) > 0:
            # ── TTFT / First Stream Chunk（首包延迟）──
            r.append(f"  TTFT / First Stream Chunk（首包延迟）:")
            r.append(f"    请求发出 → 首个 SSE data JSON chunk / 首个流式响应 chunk。")
            r.append(f"    不一定等于用户看到第一个可见文字的时间。")
            r.append(f"    avg: {summary['ttft_avg']:.3f}s  p50: {summary.get('ttft_p50', 0):.3f}s  p95: {summary.get('ttft_p95', 0):.3f}s  p99: {summary.get('ttft_p99', 0):.3f}s")
            # ── First Generated Token Latency（首个生成内容延迟）──
            _fgt = summary.get("first_generated_token_avg")
            _fgt_s = f"{_fgt:.3f}s" if _fgt is not None else "N/A"
            _fgt_p50 = summary.get("first_generated_token_p50")
            _fgt_p95 = summary.get("first_generated_token_p95")
            _fgt_p99 = summary.get("first_generated_token_p99")
            r.append(f"  First Generated Token / First Visible Token（首个生成内容延迟）:")
            r.append(f"    请求发出 → 首个非空 delta.content / delta.reasoning_content / delta.reasoning / choices[].text。")
            r.append(f"    avg: {_fgt_s}  "
                     f"p50: {f'{_fgt_p50:.3f}s' if _fgt_p50 is not None else 'N/A'}  "
                     f"p95: {f'{_fgt_p95:.3f}s' if _fgt_p95 is not None else 'N/A'}  "
                     f"p99: {f'{_fgt_p99:.3f}s' if _fgt_p99 is not None else 'N/A'}")
            # ── First Answer Token Latency（首个回答正文延迟）──
            _fat = summary.get("first_answer_token_avg")
            _fat_s = f"{_fat:.3f}s" if _fat is not None else "N/A"
            _fat_p50 = summary.get("first_answer_token_p50")
            _fat_p95 = summary.get("first_answer_token_p95")
            _fat_p99 = summary.get("first_answer_token_p99")
            r.append(f"  First Answer Token Latency（首个回答正文延迟）:")
            r.append(f"    请求发出 → 首个非空 delta.content。若模型只输出 reasoning，可能为 N/A。")
            r.append(f"    avg: {_fat_s}  "
                     f"p50: {f'{_fat_p50:.3f}s' if _fat_p50 is not None else 'N/A'}  "
                     f"p95: {f'{_fat_p95:.3f}s' if _fat_p95 is not None else 'N/A'}  "
                     f"p99: {f'{_fat_p99:.3f}s' if _fat_p99 is not None else 'N/A'}")
            # ── First Reasoning Token Latency（首个推理内容延迟）──
            _frt = summary.get("first_reasoning_token_avg")
            _frt_s = f"{_frt:.3f}s" if _frt is not None else "N/A"
            _frt_p50 = summary.get("first_reasoning_token_p50")
            _frt_p95 = summary.get("first_reasoning_token_p95")
            _frt_p99 = summary.get("first_reasoning_token_p99")
            r.append(f"  First Reasoning Token Latency（首个推理内容延迟）:")
            r.append(f"    请求发出 → 首个非空 delta.reasoning_content / delta.reasoning。")
            r.append(f"    avg: {_frt_s}  "
                     f"p50: {f'{_frt_p50:.3f}s' if _frt_p50 is not None else 'N/A'}  "
                     f"p95: {f'{_frt_p95:.3f}s' if _frt_p95 is not None else 'N/A'}  "
                     f"p99: {f'{_frt_p99:.3f}s' if _frt_p99 is not None else 'N/A'}")
            # ── First Generated Gap（首包到首个生成内容间隔）──
            _fgg = summary.get("first_generated_gap_avg")
            _fgg_s = f"{_fgg:.3f}s" if _fgg is not None else "N/A"
            _fgg_p50 = summary.get("first_generated_gap_p50")
            _fgg_p95 = summary.get("first_generated_gap_p95")
            r.append(f"  First Generated Gap（首包到首个生成内容间隔）:")
            r.append(f"    First Generated Token − First JSON Chunk / First Stream Chunk。")
            r.append(f"    avg: {_fgg_s}  "
                     f"p50: {f'{_fgg_p50:.3f}s' if _fgg_p50 is not None else 'N/A'}  "
                     f"p95: {f'{_fgg_p95:.3f}s' if _fgg_p95 is not None else 'N/A'}")
            # ── TPOT / Time Per Output Token（单 Token 耗时）──
            r.append(f"  TPOT / Time Per Output Token（单 Token 耗时）:")
            r.append(f"    (E2E − TTFT) / (completion_tokens − 1)，使用 First Stream Chunk 作为 TTFT。")
            r.append(f"    avg: {summary.get('tpot_avg', 0):.3f}s  p50: {summary.get('tpot_p50', 0):.3f}s  p95: {summary.get('tpot_p95', 0):.3f}s  p99: {summary.get('tpot_p99', 0):.3f}s")
            # ── Visible TPOT（仅供诊断）──
            _vt = summary.get("visible_tpot_avg")
            _vt_s = f"{_vt:.3f}s" if _vt is not None else "N/A"
            _vt_p50 = summary.get("visible_tpot_p50")
            _vt_p95 = summary.get("visible_tpot_p95")
            _vt_p99 = summary.get("visible_tpot_p99")
            r.append(f"  Visible TPOT（仅供诊断）:")
            r.append(f"    (E2E − First Generated Token) / (completion_tokens − 1)。")
            r.append(f"    受首字延迟影响，不作为主 benchmark TPOT。")
            r.append(f"    avg: {_vt_s}  "
                     f"p50: {f'{_vt_p50:.3f}s' if _vt_p50 is not None else 'N/A'}  "
                     f"p95: {f'{_vt_p95:.3f}s' if _vt_p95 is not None else 'N/A'}  "
                     f"p99: {f'{_vt_p99:.3f}s' if _vt_p99 is not None else 'N/A'}")
            # ── Generated ITL / ITL（生成内容间隔）──
            r.append(f"  Generated ITL（生成内容间隔 / Inter-Token Latency）:")
            r.append(f"    相邻生成内容 chunk 的时间间隔（content/reasoning/text 统一计算）。")
            r.append(f"    avg: {summary.get('itl_avg', 0):.3f}s  p50: {summary.get('itl_p50', 0):.3f}s  p95: {summary.get('itl_p95', 0):.3f}s  p99: {summary.get('itl_p99', 0):.3f}s")
            _gen_itl = summary.get("generated_itl_avg_agg")
            _ans_itl = summary.get("answer_itl_avg_agg")
            _rsn_itl = summary.get("reasoning_itl_avg_agg")
            _se_itl  = summary.get("stream_event_itl_avg_agg")
            if _ans_itl is not None or _rsn_itl is not None or _se_itl is not None:
                r.append(f"    Answer ITL: {f'{_ans_itl:.3f}s' if _ans_itl is not None else 'N/A'}  "
                         f"Reasoning ITL: {f'{_rsn_itl:.3f}s' if _rsn_itl is not None else 'N/A'}  "
                         f"Stream Event ITL: {f'{_se_itl:.3f}s' if _se_itl is not None else 'N/A'}")
        else:
            r.append("  TTFT / TPOT / ITL: N/A（非流式模式无法真实测量）")

        # ── TTFT Debug ──
        ok_count_rpt = summary.get("success", 0)
        if stream_mode and ok_count_rpt > 0:
            r.append("")
            r.append("  TTFT Debug / Stream Timing (校准参考):")
            r.append(f"    first_data_line      avg: {summary.get('first_data_line_avg', 0):.4f}s  p50: {summary.get('first_data_line_p50', 0):.4f}s")
            r.append(f"    first_json_chunk     avg: {summary.get('first_json_chunk_avg', 0):.4f}s  p50: {summary.get('first_json_chunk_p50', 0):.4f}s")
            _fvt = summary.get("first_generated_token_avg")
            _fvt_p50 = summary.get("first_generated_token_p50")
            _fvt_s4 = f"{_fvt:.4f}s" if _fvt is not None else "N/A"
            _fvt_p50_s4 = f"{_fvt_p50:.4f}s" if _fvt_p50 is not None else "N/A"
            r.append(f"    first_generated_token avg: {_fvt_s4}  p50: {_fvt_p50_s4}")
            _fat2 = summary.get("first_answer_token_avg")
            _fat2_s4 = f"{_fat2:.4f}s" if _fat2 is not None else "N/A"
            r.append(f"    first_answer_token    avg: {_fat2_s4}")
            _frsn = summary.get("first_reasoning_token_avg")
            _frsn_s4 = f"{_frsn:.4f}s" if _frsn is not None else "N/A"
            r.append(f"    first_reasoning_token avg: {_frsn_s4}")
            _fgg2 = summary.get("first_generated_gap_avg")
            _fgg2_s4 = f"{_fgg2:.4f}s" if _fgg2 is not None else "N/A"
            r.append(f"    first_generated_gap   avg: {_fgg2_s4}")

        # ── Stream Parser Profile ──
        _pp = summary.get("parser_profile")
        if _pp and _pp.get("stream_supported"):
            r.append("")
            r.append("  Stream Parser Profile（流式解析能力档案）:")
            r.append(f"    parser_mode:             {_pp.get('parser_mode', 'unknown')}")
            r.append(f"    generated_fields:        {_pp.get('generated_fields', [])}")
            r.append(f"    observed_delta_keys:     {_pp.get('observed_delta_keys', [])}")
            r.append(f"    answer_field_observed:   {_pp.get('answer_field_observed')}")
            r.append(f"    reasoning_field_observed:{_pp.get('reasoning_field_observed')}")
            r.append(f"    usage_supported:         {_pp.get('usage_supported')}")
            r.append(f"    usage_source:            {_pp.get('usage_source')}")
            if _pp.get("unknown_delta_keys"):
                r.append(f"    unknown_delta_keys:      {_pp['unknown_delta_keys']}")
            if _pp.get("warning_messages"):
                r.append("    stream_warnings:")
                for _sw in _pp["warning_messages"][:4]:
                    r.append(f"      - {_sw[:120]}")

        # ── 四、Token 统计 (Token Counts) ──
        r.append("")
        r.append(sep)
        r.append("  四、Token 统计 (Token Counts)")
        r.append(sep)
        r.append(f"  Input Tokens:  {summary.get('total_input_tokens', 0)}")
        r.append(f"  Output Tokens: {summary.get('total_output_tokens', 0)}")
        r.append(f"  Total Tokens:  {summary.get('total_tokens', 0)}")
        has_output = summary.get("total_output_tokens", 0) > 0
        r.append(f"  Token Source: {'usage (服务端返回)' if has_output else 'missing_usage (服务端未返回 usage，token 不可信)'}")

        # ── 五、吞吐指标 (Throughput) ──
        # Aligned with: NVIDIA GenAI-Perf output_token_throughput / request_throughput
        r.append("")
        r.append(sep)
        r.append("  五、吞吐指标 (Throughput)")
        r.append(sep)
        r.append(f"  Request Throughput / RPS（请求吞吐）: {summary.get('request_throughput_rps', 0):.2f} req/s  (= success / duration)")
        r.append(f"  Output Token Throughput（输出 Token 吞吐）: {summary.get('system_output_tps', 0):.1f} tok/s  (= output_tokens / duration)")
        r.append(f"  Total Token Throughput:                                {summary.get('system_total_tps', 0):.1f} tok/s  (= total_tokens / duration)")
        r.append(f"  Per-request Output Token Throughput (avg):              {summary.get('per_request_output_tps_avg', 0):.2f} tok/s")
        r.append(f"    p50: {summary.get('per_request_output_tps_p50', 0):.2f}  p95: {summary.get('per_request_output_tps_p95', 0):.2f}")

        # ── 六、口径说明 (Metric Definitions) ──
        # Aligned with: vLLM bench serve, NVIDIA GenAI-Perf, NIM Benchmark
        r.append("")
        r.append(sep)
        r.append("  六、口径说明 (Metric Definitions — vLLM / NVIDIA GenAI-Perf)")
        r.append(sep)
        r.append("  本工具的指标口径对齐以下行业标准：")
        r.append("    • vLLM bench serve (ttft, tpot, itl, e2el)")
        r.append("    • NVIDIA GenAI-Perf / NIM Benchmark (ttft, itl, output_token_throughput, request_throughput)")
        r.append("")
        r.append("  TTFT / First Stream Chunk (Time to First Token):")
        r.append("    请求发出 → 首个 SSE JSON data chunk 到达。")
        r.append("    对应 vLLM bench serve --percentile-metrics ttft")
        r.append("")
        r.append("  First Generated Token Latency（首个生成内容延迟）:")
        r.append("    请求发出 → 首个非空 delta.content / delta.reasoning_content / delta.reasoning / choices[].text。")
        r.append("")
        r.append("  First Answer Token Latency（首个回答正文延迟）:")
        r.append("    请求发出 → 首个非空 delta.content。若模型只输出 reasoning，可能为 N/A。")
        r.append("")
        r.append("  First Reasoning Token Latency（首个推理内容延迟）:")
        r.append("    请求发出 → 首个非空 delta.reasoning_content / delta.reasoning。")
        r.append("")
        r.append("  First Generated Gap（首包到首个生成内容间隔）:")
        r.append("    First Generated Token − First JSON Chunk。")
        r.append("")
        r.append("  E2E Latency (End-to-End, vLLM: e2el):")
        r.append("    请求发出 → 完整响应结束 (最后一个 chunk 到达)")
        r.append("    对应 vLLM bench serve --percentile-metrics e2el")
        r.append("")
        r.append("  TPOT (Time per Output Token):")
        r.append("    (e2el - ttft) / (completion_tokens - 1)  当 completion_tokens >= 2")
        r.append("    对应 vLLM bench serve --percentile-metrics tpot")
        r.append("")
        r.append("  ITL (Inter-Token Latency):")
        r.append("    相邻流式响应 chunk 的时间间隔")
        r.append("    对应 vLLM bench serve --percentile-metrics itl")
        r.append("    ⚠ 本工具无 tokenizer，ITL 基于 SSE chunk 估算")
        r.append("      若服务端一个 chunk 包含多个 token，ITL 为近似值（上界）")
        r.append("")
        r.append("  Output Token Throughput:")
        r.append("    total_output_tokens / duration_sec")
        r.append("    对应 NVIDIA GenAI-Perf output_token_throughput")
        r.append("")
        r.append("  Request Throughput:")
        r.append("    success / duration_sec")
        r.append("    对应 NVIDIA GenAI-Perf request_throughput")
        r.append("")
        r.append("  负载模式: fixed concurrency (固定并发数)")

        # ── 七、诊断与建议 ──
        r.append("")
        r.append(sep)
        r.append("  七、诊断与建议")
        r.append(sep)
        diag = self._diagnose(summary)

        # ── metric consistency check ──
        mw = summary.get("metric_warnings", [])
        if mw:
            r.append("")
            r.append("  ⚠ 指标一致性警告 (Metric Consistency Warnings):")
            for w in mw:
                r.append(f"    - {w}")
        else:
            r.append("")
            r.append("  ✓ 指标一致性检查通过 (Metric Consistency Check Passed)。")

        if diag:
            r.append("")
            r.extend(diag)
        else:
            r.append("  (无特殊建议)")

        # ── ★ 客户验收指标 / Customer Acceptance Metrics ──
        try:
            from llm_benchmark_app.customer_metrics import (
                generate_customer_acceptance_section,
                OBJECTIVE_SINGLE_SESSION, OBJECTIVE_PEAK_THROUGHPUT,
                MODE_REAL_API,
            )
            objective  = summary.get("benchmark_objective", OBJECTIVE_SINGLE_SESSION)
            bench_mode = summary.get("benchmark_mode", MODE_REAL_API)
            workload   = summary.get("workload")
            cust_section = generate_customer_acceptance_section(
                summary, objective, bench_mode,
                sweep_peak_summary=None,
                workload_label=workload,
            )
            r.append(cust_section)
        except Exception:
            pass  # never break existing report

        r.append("")
        r.append("=" * 60)
        return "\n".join(r)

    # ──────────────────────────────────────────────────────────────────────────
    def _generate_report_v2(self, summary: dict, env_info: dict | None = None) -> str:
        """Generate a v2 structured text report.

        Structure:
          0. Header (run_id, time, standard)
          1. 摘要结论 (Executive Summary)
          2. 被测环境 (Environment)
          3. 测试配置 (Config)
          4. 成功 / 失败
          5. 客户验收指标 (Customer Acceptance Metrics)
          6. 延迟指标 (Compact latency table)
          7. Token 统计
          8. 吞吐指标
          9. 图表路径
         10. 诊断建议
        [附录] 指标定义
        """
        if env_info is None:
            env_info = {}
        from llm_benchmark_app.customer_metrics import (
            generate_customer_acceptance_section,
            OBJECTIVE_SINGLE_SESSION, OBJECTIVE_PEAK_THROUGHPUT,
            MODE_REAL_API, MODE_ENGINE_CORE,
        )

        total = max(summary.get("total", 1), 1)
        success = summary.get("success", 0)
        fail    = summary.get("fail", 0)
        success_rate = summary.get("success_rate", success / total * 100)
        stream_mode  = summary.get("stream_mode", False)
        conc  = summary.get("concurrency", 0)
        n     = summary.get("total", 0)
        i_avg = int((summary.get("total_input_tokens",  0) or 0) / total)
        o_avg = int((summary.get("total_output_tokens", 0) or 0) / total)
        workload_str = f"C{conc} / N{n} / I{i_avg} / O{o_avg}"
        objective  = summary.get("benchmark_objective", OBJECTIVE_SINGLE_SESSION)
        bench_mode = summary.get("benchmark_mode", MODE_REAL_API)

        run_id  = getattr(self, "_last_single_run_dir", "")
        results_root = (getattr(self, "results_root_var", None) or
                        tk.StringVar(value=RESULTS_ROOT)).get() or RESULTS_ROOT
        sep_h = "─" * 60  # section header
        sep_t = "─" * 58  # table rule

        r = []
        # ── 0. Header ──────────────────────────────────────────────────────
        r.append("=" * 62)
        r.append("  LLM Benchmark Report  /  LLM 性能测试报告")
        r.append("=" * 62)
        r.append(f"  Time:    {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        r.append(f"  Version: Report {summary.get('report_version', 'v2')} | "
                 f"Standard: {summary.get('metric_standard', 'JISUMAN LLM Benchmark v1')}")
        result_dir = getattr(self, "_last_single_run_dir", "") or results_root
        r.append(f"  Results: {result_dir}")
        r.append("")

        # ── 1. 摘要结论 ───────────────────────────────────────────────────
        r.append(sep_h)
        r.append("  一、摘要结论 / Executive Summary")
        r.append(sep_h)
        model_str = summary.get("model", "-")
        mode_zh = "真实 API 体验测试" if bench_mode != MODE_ENGINE_CORE else "引擎核心性能测试"
        obj_zh  = ("单会话最大生成速度"
                   if objective == OBJECTIVE_SINGLE_SESSION else "峰值吞吐扫描")
        r.append(f"  模型:    {model_str}")
        r.append(f"  测试:    {mode_zh} / {obj_zh}")
        r.append(f"  负载:    {workload_str}")
        r.append(f"  成功率:  {success_rate:.1f}%  (成功 {success} / 失败 {fail} / 共 {n})")
        r.append("")
        tpot_avg   = summary.get("tpot_avg", 0) or 0
        tpot_ms    = tpot_avg * 1000
        decode_tps = (1000.0 / tpot_ms) if tpot_ms > 0 else None
        out_tps    = summary.get("system_output_tps", 0) or 0
        total_tps  = summary.get("system_total_tps", 0) or 0
        ttft_avg   = summary.get("ttft_avg", 0) or 0
        e2e_avg    = summary.get("e2e_latency_avg", 0) or 0
        if decode_tps is not None and stream_mode:
            r.append(f"  ● 单会话最大生成速度:  {decode_tps:.1f} tok/s  (= 1000 / TPOT)")
        r.append(f"  ● 输出 Token 吞吐:     {out_tps:.1f} tok/s")
        r.append(f"  ● 总 Token 吞吐:       {total_tps:.1f} tok/s")
        if stream_mode and ttft_avg > 0:
            r.append(f"  ● 平均 TTFT:           {ttft_avg:.3f}s ({ttft_avg*1000:.0f}ms)")
        if stream_mode and tpot_ms > 0:
            r.append(f"  ● 平均 TPOT:           {tpot_ms:.1f} ms/tok")
        r.append(f"  ● 平均 E2E 延迟:       {e2e_avg:.3f}s")
        # warn if objective mismatch
        if objective == OBJECTIVE_SINGLE_SESSION and conc > 1:
            r.append("")
            r.append(f"  ⚠ 当前并发 C{conc} > 1，非标准单会话测试。")
            r.append(f"    '单会话最大生成速度' 为按每请求 TPOT 推导的参考值，不代表严格 C1 测试。")
        r.append("")

        # ── 2. 被测环境 ───────────────────────────────────────────────────
        r.append(sep_h)
        r.append("  二、被测环境 / Environment")
        r.append(sep_h)
        hw    = env_info.get("hw", {})
        sw    = env_info.get("sw", {})
        model = env_info.get("model", {})
        env_name = env_info.get("env_name") or ""
        if not env_name:
            r.append("  ⚠ 未绑定环境档案，本次结果不建议用于长期横向对比。")
        else:
            r.append(f"  环境档案: {env_name}")
        if hw:
            gpu = hw.get("gpu_model") or hw.get("gpu_model_custom") or "-"
            gc  = hw.get("gpu_count") or hw.get("gpu_count_custom") or "-"
            r.append(f"  GPU:      {gpu}  × {gc}")
        if sw:
            be  = sw.get("backend") or sw.get("backend_custom") or "-"
            bev = sw.get("backend_version") or ""
            r.append(f"  Backend:  {be} {bev}".rstrip())
        if model:
            q  = model.get("quantization") or model.get("quantization_custom") or "-"
            mp = model.get("profile_name", "-")
            r.append(f"  Model:    {mp}  (Quant: {q})")
        r.append(f"  API URL:  {summary.get('api_url', '-')}")
        r.append("")

        # ── 3. 测试配置 ───────────────────────────────────────────────────
        r.append(sep_h)
        r.append("  三、测试配置 / Test Configuration")
        r.append(sep_h)
        r.append(f"  模型 (model):        {summary.get('model', '-')}")
        r.append(f"  并发 (concurrency):  C{conc}")
        r.append(f"  总请求数:            N{n}")
        r.append(f"  Max Tokens:          {summary.get('max_tokens', '-')}")
        output_mode = summary.get("output_length_mode", "normal")
        output_mode_label = (
            self.tr("label.output_mode_fixed_short") if output_mode == "fixed"
            else self.tr("label.output_mode_normal_short")
        )
        r.append(f"  {self.tr('label.output_length_mode')}: {output_mode_label}")
        if output_mode == "fixed":
            r.append(f"  Fixed Output Tokens: {summary.get('fixed_output_tokens', 'N/A')}")
            r.append(f"  ignore_eos:          {str(summary.get('ignore_eos', False)).lower()}")
        r.append(f"  Temperature:         {summary.get('temperature', '-')}")
        r.append(f"  Stream:              {'流式 (stream=True)' if stream_mode else '非流式 (stream=False)'}")
        preset = summary.get("benchmark_preset_name", "")
        if preset:
            r.append(f"  Benchmark Preset:    {preset}")
        warmup_r = summary.get("warmup_requests", 0)
        if warmup_r:
            r.append(f"  Warmup Requests:     {warmup_r}")
        r.append(f"  测试目标:            {obj_zh}")
        r.append(f"  测试模式:            {mode_zh}")
        r.append("")

        # ── 4. 成功 / 失败 ────────────────────────────────────────────────
        r.append(sep_h)
        r.append("  四、成功 / 失败")
        r.append(sep_h)
        r.append(f"  成功: {success}   失败: {fail}   成功率: {success_rate:.1f}%")
        fail_detail = summary.get("fail_detail", [])
        if fail_detail:
            from collections import Counter
            error_types = Counter(req.get("error_type", "unknown") for req in fail_detail)
            r.append("  错误类型:")
            for et, count in error_types.most_common():
                r.append(f"    - {et}: {count}")
        r.append("")

        # ── 5. 客户验收指标 ───────────────────────────────────────────────
        r.append(sep_h)
        r.append("  五、客户验收指标 / Customer Acceptance Metrics")
        r.append(sep_h)
        if bench_mode == MODE_ENGINE_CORE:
            r.append("  ⚠ 引擎核心性能测试：结果代表硬件/框架核心能力，")
            r.append("    不应作为客户 API 体验指标呈现。")
        else:
            r.append("  ✓ 真实 API 体验测试：结果代表客户实际 API 调用体验。")
        r.append("")
        try:
            cust_section = generate_customer_acceptance_section(
                summary, objective, bench_mode,
                sweep_peak_summary=None,
                workload_label=workload_str,
            )
            r.append(cust_section)
        except Exception:
            pass  # never break existing report
        r.append("")

        # ── 6. 延迟指标 ───────────────────────────────────────────────────
        r.append(sep_h)
        r.append("  六、延迟指标 / Latency Metrics")
        r.append(sep_h)
        if stream_mode:
            # Compact table
            r.append(f"  {'指标':<28} {'avg':>10} {'p50':>10} {'p95':>10} {'p99':>10}")
            r.append("  " + sep_t)
            def _fmt_ms(v, unit="s"):
                if v is None or v == 0:
                    return "—"
                return f"{v:.3f}{unit}" if unit == "s" else f"{v*1000:.1f}ms"
            def _row(label, key_prefix, unit="s"):
                avg = summary.get(f"{key_prefix}_avg", 0) or 0
                p50 = summary.get(f"{key_prefix}_p50", 0) or 0
                p95 = summary.get(f"{key_prefix}_p95", 0) or 0
                p99 = summary.get(f"{key_prefix}_p99", 0) or 0
                if avg == 0:
                    return None
                if unit == "ms":
                    return (f"  {label:<28} {avg*1000:>10.1f}ms {p50*1000:>9.1f}ms"
                            f" {p95*1000:>9.1f}ms {p99*1000:>9.1f}ms")
                return (f"  {label:<28} {avg:>10.3f}s {p50:>9.3f}s"
                        f" {p95:>9.3f}s {p99:>9.3f}s")

            _e2e_row = _row("E2E Latency (e2el)", "e2e_latency")
            if _e2e_row: r.append(_e2e_row)

            _ttft_row = _row("TTFT / First Stream Chunk", "ttft")
            if _ttft_row: r.append(_ttft_row)

            _fgt = summary.get("first_generated_token_avg")
            if _fgt:
                _fgt_row = _row("First Generated Token", "first_generated_token")
                if _fgt_row: r.append(_fgt_row)

            _fat = summary.get("first_answer_token_avg")
            if _fat:
                _fat_row = _row("First Answer Token", "first_answer_token")
                if _fat_row: r.append(_fat_row)

            _frt = summary.get("first_reasoning_token_avg")
            if _frt:
                _frt_row = _row("First Reasoning Token", "first_reasoning_token")
                if _frt_row: r.append(_frt_row)

            _tpot_row = _row("TPOT (Time per Output Token)", "tpot", "ms")
            if _tpot_row: r.append(_tpot_row)

            _itl_row = _row("ITL (Inter-Token Latency)", "itl", "ms")
            if _itl_row: r.append(_itl_row)
        else:
            r.append(f"  E2E avg: {e2e_avg:.3f}s  "
                     f"p50: {summary.get('e2e_latency_p50', 0):.3f}s  "
                     f"p95: {summary.get('e2e_latency_p95', 0):.3f}s  "
                     f"p99: {summary.get('e2e_latency_p99', 0):.3f}s")
            r.append("  TTFT / TPOT / ITL: N/A（非流式模式）")
        r.append("")

        # ── 7. Token 统计 ──────────────────────────────────────────────────
        r.append(sep_h)
        r.append("  七、Token 统计 / Token Counts")
        r.append(sep_h)
        in_tok  = summary.get("total_input_tokens",  0) or 0
        out_tok = summary.get("total_output_tokens", 0) or 0
        tot_tok = summary.get("total_tokens", 0) or 0
        r.append(f"  Input:   {in_tok:>8}  (avg {i_avg}/req)")
        r.append(f"  Output:  {out_tok:>8}  (avg {o_avg}/req)")
        r.append(f"  Total:   {tot_tok:>8}")
        has_output = out_tok > 0
        r.append(f"  Source:  {'usage (服务端返回)' if has_output else '⚠ 服务端未返回 usage，token 数不可信'}")
        r.append("")

        # ── 8. 吞吐指标 ───────────────────────────────────────────────────
        r.append(sep_h)
        r.append("  八、吞吐指标 / Throughput")
        r.append(sep_h)
        rps = summary.get("request_throughput_rps", 0) or 0
        per_req_avg = summary.get("per_request_output_tps_avg", 0) or 0
        per_req_p50 = summary.get("per_request_output_tps_p50", 0) or 0
        per_req_p95 = summary.get("per_request_output_tps_p95", 0) or 0
        r.append(f"  请求吞吐 (RPS):              {rps:.2f} req/s")
        r.append(f"  输出 Token 吞吐:             {out_tps:.1f} tok/s  (= output_tokens / duration)")
        r.append(f"  总 Token 吞吐:               {total_tps:.1f} tok/s  (= total_tokens / duration)")
        r.append(f"  单请求输出 Token 速率 avg:   {per_req_avg:.2f} tok/s")
        r.append(f"    p50: {per_req_p50:.2f}  p95: {per_req_p95:.2f}")
        r.append("")

        # ── 9. 图表 ────────────────────────────────────────────────────────
        last_dir = getattr(self, "_last_single_run_dir", "")
        if last_dir:
            hist_png = os.path.join(last_dir, "charts", "e2e_latency_histogram.png")
            if os.path.isfile(hist_png):
                r.append(sep_h)
                r.append("  九、图表 / Charts")
                r.append(sep_h)
                r.append(f"  E2E Histogram: {hist_png}")
                r.append("")

        # ── 10. 诊断建议 ──────────────────────────────────────────────────
        r.append(sep_h)
        r.append("  十、诊断建议 / Diagnostics")
        r.append(sep_h)
        # Metric consistency
        mw = summary.get("metric_warnings", [])
        if mw:
            r.append("  ⚠ 指标一致性警告:")
            for w in mw:
                r.append(f"    - {w}")
        else:
            r.append("  ✓ 指标一致性检查通过。")
        diag = self._diagnose(summary)
        if diag:
            r.append("")
            r.extend(diag)
        r.append("")

        # ── 附录. 指标定义 ─────────────────────────────────────────────────
        r.append(sep_h)
        r.append("  [附录] 指标定义 / Metric Definitions Appendix")
        r.append(sep_h)
        r.append("  (对齐 vLLM bench serve + NVIDIA GenAI-Perf / NIM)")
        r.append("")
        r.append("  TTFT  Time to First Token — 请求发出 → 首个 SSE JSON chunk 到达")
        r.append("  TPOT  Time per Output Token — (E2E − TTFT) / (tokens − 1)")
        r.append("  ITL   Inter-Token Latency — 相邻流式 chunk 间隔（chunk 级估算）")
        r.append("  E2E   End-to-End Latency — 请求发出 → 完整响应结束")
        r.append("  Output Token Throughput — total_output_tokens / duration_sec")
        r.append("  Request Throughput (RPS) — success / duration_sec")
        r.append("  Fixed Concurrency — 所有请求由固定数量并发 worker 发出")
        r.append("")
        r.append("=" * 62)
        return "\n".join(r)

    def _draw_histogram(self, summary: dict):
        """Store E2E latencies and redraw after the histogram canvas is laid out."""
        self._latest_e2e_latencies = self._get_success_e2e_latencies(summary)
        if hasattr(self, "_hist_card"):
            try:
                self._hist_card.expand()
            except Exception:
                pass
        self.root.after_idle(self._redraw_e2e_histogram)
        self.root.after(100, self._redraw_e2e_histogram)

    def _get_success_e2e_latencies(self, summary):
        latencies = []
        for r in summary.get("detail", []):
            if not r.get("ok"):
                continue
            v = r.get("e2e_latency", r.get("latency"))
            if isinstance(v, (int, float)) and v > 0:
                latencies.append(float(v))
        return latencies

    def _redraw_e2e_histogram(self):
        canvas = getattr(self, "hist_canvas", None)
        if canvas is None:
            return
        try:
            canvas.delete("all")
            latencies = getattr(self, "_latest_e2e_latencies", [])
            canvas.update_idletasks()
            w = canvas.winfo_width()
            h = canvas.winfo_height()
            if not latencies:
                canvas.create_text(
                    max(w // 2, 160), max(h // 2, 80),
                    text=self.tr("chart.hist_empty", "暂无延迟数据"),
                    font=C_STYLE["font_body"],
                    fill=C_STYLE["text_secondary"],
                )
                return
            if w < 100 or h < 80:
                if getattr(self, "_hist_redraw_after_id", None):
                    try:
                        self.root.after_cancel(self._hist_redraw_after_id)
                    except Exception:
                        pass
                self._hist_redraw_after_id = self.root.after(
                    100, self._redraw_e2e_histogram)
                return
            self._hist_redraw_after_id = None
            self._draw_popup_histogram(canvas, latencies)
        except Exception as e:
            try:
                canvas.delete("all")
                canvas.create_text(
                    180, 80,
                    text=f"{self.tr('chart.hist_empty', '暂无延迟数据')}: {e}",
                    font=C_STYLE["font_small"],
                    fill=C_STYLE["error_text"],
                )
            except Exception:
                pass
    def _analyze_failures(self, fail_detail: list) -> tuple[str, str]:
        if not fail_detail:
            return "", ""
        categorized: dict[str, list[str]] = {}
        for r in fail_detail:
            err_msg = r.get("error", "未知错误")
            err_type = r.get("error_type", "")
            if err_type == "网络连接失败":
                cat = "网络连接失败"
            elif "401" in err_type or "401" in err_msg:
                cat = "认证失败 (401)"
            elif "403" in err_type or "403" in err_msg:
                cat = "权限不足 (403)"
            elif "404" in err_type or "404" in err_msg:
                cat = "接口不存在 (404)"
            elif "429" in err_type or "429" in err_msg:
                cat = "请求限流 (429)"
            elif any(c in err_type for c in ["500", "502", "503"]):
                cat = "服务器错误 (5xx)"
            elif err_type == "响应格式错误":
                cat = "响应格式错误"
            else:
                cat = "其他错误"
            categorized.setdefault(cat, []).append(err_msg)
        lines = []
        for cat, errs in categorized.items():
            sample = errs[0][:150]
            lines.append(f"  [{cat}]  ({len(errs)}次)  {sample}")
        advice_map = {
            "网络连接失败": (
                "• 检查 API 地址是否正确（如 http://192.168.1.12/v1）\n"
                "• 确认服务端是否在运行，端口是否开放\n"
                "• 如使用 VPN/代理，检查连接是否正常"
            ),
            "认证失败 (401)": (
                "• API Key 错误或已过期\n"
                "• 在 API 管理后台检查或重新生成 Key"
            ),
            "权限不足 (403)": (
                "• API Key 没有访问该模型的权限\n"
                "• 检查账户配额与模型授权"
            ),
            "接口不存在 (404)": (
                "• API 地址路径可能有误\n"
                "• 确认 URL 以 /v1/chat/completions 结尾\n"
                "• 检查模型名称是否拼写正确"
            ),
            "请求限流 (429)": (
                "• 请求频率超过限额，降低并发数后重试\n"
                "• 等待配额重置（通常 1 分钟后恢复）"
            ),
            "服务器错误 (5xx)": (
                "• 服务端暂时不可用，稍后重试\n"
                "• 如持续出现请联系服务提供方"
            ),
            "响应格式错误": (
                "• 返回内容不是合法 JSON\n"
                "• 确认 API 地址是否为 OpenAI 兼容接口\n"
                "• 检查服务端日志"
            ),
            "其他错误": (
                "• 查看上方错误详情定位具体原因\n"
                "• 常见原因：API 地址、Key 或模型名称不正确"
            ),
        }
        error_summary = "\n".join(lines)
        seen = set()
        advice_lines = []
        for cat in categorized:
            if cat in advice_map and cat not in seen:
                seen.add(cat)
                advice_lines.append(f"【{cat} 处理方法】\n{advice_map[cat]}")
        advice = "\n\n".join(advice_lines)
        return error_summary, advice
    def _build_history_tab(self):
        hf = self.history_frame
        hf.configure(bg=C_STYLE["bg_main"])
        hf.grid_columnconfigure(0, weight=1)
        hf.grid_rowconfigure(0, weight=0)  # toolbar
        hf.grid_rowconfigure(1, weight=1)  # table
        hf.grid_rowconfigure(2, weight=0)  # detail preview
        # toolbar with subtle background
        toolbar = tk.Frame(hf, bg=C_STYLE["bg_card"],
                           highlightbackground=C_STYLE["border"],
                           highlightthickness=1, bd=0)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, C_STYLE["gap_lg"]))
        toolbar_inner = tk.Frame(toolbar, bg=C_STYLE["bg_card"])
        toolbar_inner.pack(fill=tk.X, padx=C_STYLE["pad_lg"], pady=C_STYLE["pad_sm"])
        ttk.Button(toolbar_inner, text="↻ 刷新", style="Secondary.TButton",
                   command=self._refresh_history).pack(side=tk.LEFT)
        tk.Label(toolbar_inner, text="类型", font=C_STYLE["font_small"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_secondary"]).pack(
                     side=tk.LEFT, padx=(C_STYLE["pad_md"], C_STYLE["pad_sm"]))
        self.history_type_filter_var = tk.StringVar(value="All")
        type_filter = ttk.Combobox(toolbar_inner,
                                   textvariable=self.history_type_filter_var,
                                   values=[self.tr("history.type_all"), self.tr("history.type_single"),
                                           self.tr("history.type_sweep")],
                                   width=12, state="readonly", style="App.TCombobox")
        self.history_type_filter = type_filter
        type_filter.pack(side=tk.LEFT)
        type_filter.bind("<<ComboboxSelected>>", lambda e: self._refresh_history())
        ttk.Button(toolbar_inner, text="⇆ 对比选中", style="Secondary.TButton",
                   command=self._compare_selected_runs).pack(side=tk.LEFT, padx=C_STYLE["pad_sm"])
        ttk.Button(toolbar_inner, text="✕ 清空记录", style="Danger.TButton",
                   command=self._clear_history).pack(side=tk.LEFT, padx=C_STYLE["pad_sm"])
        self.history_status_var = tk.StringVar(value="")
        lbl = tk.Label(toolbar_inner, textvariable=self.history_status_var,
                       font=C_STYLE["font_small"],
                       bg=C_STYLE["bg_card"], fg=C_STYLE["text_muted"])
        lbl.pack(side=tk.RIGHT)

        # Direct table display. History must not be wrapped in a collapsible section.
        table_card = tk.Frame(hf, bg=C_STYLE["bg_card"],
                              highlightbackground=C_STYLE["border"],
                              highlightthickness=1, bd=0)
        table_card.grid(row=1, column=0, sticky="nsew")
        table_card.grid_columnconfigure(0, weight=1)
        table_card.grid_rowconfigure(0, weight=1)
        cols = ("id", "Time", "Type", "Model", "Environment",
                "GPU", "Backend", "Quant", "Config", "Output TPS", "TTFT P95", "E2E P95", "Status")
        self.hist_tree = ttk.Treeview(table_card, columns=cols,
                                      show="headings", selectmode="extended",
                                      style="App.Treeview")
        # 13 列在 1248px 最小窗宽下必然超宽：固定列宽 + 横向滚动，不压缩表头（规范 §3.4）
        col_widths = {
            "id": 44, "Time": 132, "Type": 76, "Model": 150, "Environment": 104,
            "GPU": 96, "Backend": 84, "Quant": 66, "Config": 118,
            "Output TPS": 176, "TTFT P95": 140, "E2E P95": 132, "Status": 88,
        }
        # 表头带单位；未在 _refresh_history_headers 映射的列直接给出中文+单位（规范 §3.4）
        col_heads = {
            "Environment": "环境", "GPU": "GPU", "Backend": "框架", "Quant": "量化",
            "Output TPS": "Output TPS (tok/s)", "TTFT P95": "TTFT P95 (ms)",
            "E2E P95": "E2E P95 (ms)",
        }
        for c in cols:
            self.hist_tree.heading(c, text=col_heads.get(c, c))
            self.hist_tree.column(
                c, width=col_widths.get(c, 80),
                minwidth=min(col_widths.get(c, 80), 90),
                stretch=(c == "Model"),
                anchor="e" if c in ("Output TPS", "TTFT P95", "E2E P95") else "center")
        scrollbar = ttk.Scrollbar(table_card, orient=tk.VERTICAL,
                                  command=self.hist_tree.yview)
        hscrollbar = ttk.Scrollbar(table_card, orient=tk.HORIZONTAL,
                                   command=self.hist_tree.xview)
        self.hist_tree.configure(yscrollcommand=scrollbar.set,
                                 xscrollcommand=hscrollbar.set)
        self.hist_tree.grid(row=0, column=0, sticky="nsew",
                            padx=(C_STYLE["pad_lg"], 0),
                            pady=C_STYLE["pad_lg"])
        scrollbar.grid(row=0, column=1, sticky="ns",
                       padx=(0, C_STYLE["pad_lg"]),
                       pady=C_STYLE["pad_lg"])
        hscrollbar.grid(row=1, column=0, sticky="ew",
                        padx=(C_STYLE["pad_lg"], 0),
                        pady=(0, C_STYLE["pad_sm"]))
        self.hist_tree.bind("<<TreeviewSelect>>", self._on_history_select)
        self.hist_tree.bind("<Double-1>", self._on_history_double_click)

        detail_card = tk.Frame(hf, bg=C_STYLE["bg_card"],
                               highlightbackground=C_STYLE["border"],
                               highlightthickness=1, bd=0)
        detail_card.grid(row=2, column=0, sticky="ew", pady=(C_STYLE["gap_lg"], 0))
        detail_card.grid_columnconfigure(0, weight=1)
        tk.Label(detail_card, text="历史详情预览", font=C_STYLE["font_section"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"]).grid(
                     row=0, column=0, sticky="w",
                     padx=C_STYLE["pad_lg"], pady=(C_STYLE["pad_md"], C_STYLE["gap_sm"]))
        self.history_detail_text = tk.Text(detail_card, height=7, wrap=tk.WORD,
                                           font=C_STYLE["font_small"],
                                           bg=C_STYLE["bg_input"],
                                           fg=C_STYLE["text_primary"],
                                           relief=tk.FLAT, borderwidth=0,
                                           state=tk.DISABLED)
        self.history_detail_text.grid(row=1, column=0, sticky="ew",
                                      padx=C_STYLE["pad_lg"],
                                      pady=(0, C_STYLE["pad_md"]))
        # 历史对比面板（UI/UX v2，规范 §4）：子选择 A/B + 差异标注 + 指标并排
        self.hist_tree.configure(height=6)
        from llm_benchmark_app import ui_pages
        ui_pages.build_compare_panel(self, hf)
        self._refresh_history()

    def _refresh_history_headers(self):
        if not hasattr(self, "hist_tree"):
            return
        headers = {
            "id": "history.id",
            "Time": "history.time",
            "Type": "history.type",
            "Model": "history.model",
            "Config": "history.config",
            "Key Result": "history.key_result",
            "Status": "history.status",
        }
        for col, key in headers.items():
            try:
                self.hist_tree.heading(col, text=self.tr(key))
            except Exception:
                pass

    def _build_env_profiles_tab(self):
        """Build the Environment Profiles tab with 4 sub-tabs."""
        epf = self.env_profiles_frame
        epf.configure(bg=C_STYLE["bg_main"])
        epf.grid_rowconfigure(0, weight=1)
        epf.grid_columnconfigure(0, weight=1)

        sub_nb = ttk.Notebook(epf)
        sub_nb.grid(row=0, column=0, sticky="nsew",
                    padx=C_STYLE["pad_lg"], pady=C_STYLE["pad_lg"])

        self._hw_frame = tk.Frame(sub_nb, bg=C_STYLE["bg_main"])
        self._sw_frame = tk.Frame(sub_nb, bg=C_STYLE["bg_main"])
        self._model_frame = tk.Frame(sub_nb, bg=C_STYLE["bg_main"])
        self._env_frame = tk.Frame(sub_nb, bg=C_STYLE["bg_main"])
        self._db_frame = tk.Frame(sub_nb, bg=C_STYLE["bg_main"])

        sub_nb.add(self._hw_frame, text="  硬件环境  ")
        sub_nb.add(self._sw_frame, text="  软件栈  ")
        sub_nb.add(self._model_frame, text="  模型部署  ")
        sub_nb.add(self._env_frame, text="  环境档案  ")
        sub_nb.add(self._db_frame, text="  数据库设置  ")

        self._build_hw_profiles_sub(self._hw_frame)
        self._build_sw_profiles_sub(self._sw_frame)
        self._build_model_profiles_sub(self._model_frame)
        self._build_env_sub(self._env_frame)
        self._build_db_settings_sub(self._db_frame)

    def _profile_panel(self, parent, list_var_name: str, on_new, on_duplicate,
                       on_save, on_delete, on_select):
        """Build a standard left-panel (list + CRUD buttons) for profile tabs.
        Returns (list_frame, listbox, scrollbar) so caller can populate it."""
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=0)  # left panel fixed width
        parent.grid_columnconfigure(1, weight=1)  # right panel expands

        # Left panel
        left = tk.Frame(parent, bg=C_STYLE["bg_card"],
                        highlightbackground=C_STYLE["border"],
                        highlightthickness=1, bd=0, width=200)
        left.grid(row=0, column=0, sticky="nsew",
                  padx=(0, C_STYLE["gap_md"]), pady=0)
        left.grid_propagate(False)
        left.grid_rowconfigure(0, weight=1)
        left.grid_columnconfigure(0, weight=1)

        lb_frame = tk.Frame(left, bg=C_STYLE["bg_card"])
        lb_frame.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        lb_frame.grid_rowconfigure(0, weight=1)
        lb_frame.grid_columnconfigure(0, weight=1)

        lb = tk.Listbox(lb_frame, font=C_STYLE["font_body"],
                        bg=C_STYLE["bg_input"], fg=C_STYLE["text_primary"],
                        selectbackground=C_STYLE["accent"],
                        selectforeground=C_STYLE["text_inverse"],
                        relief=tk.FLAT, highlightthickness=0,
                        activestyle="none")
        lb.grid(row=0, column=0, sticky="nsew")
        lb_scroll = ttk.Scrollbar(lb_frame, orient=tk.VERTICAL, command=lb.yview)
        lb.configure(yscrollcommand=lb_scroll.set)
        lb_scroll.grid(row=0, column=1, sticky="ns")
        lb.bind("<<ListboxSelect>>", on_select)

        btn_frame = tk.Frame(left, bg=C_STYLE["bg_card"])
        btn_frame.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 4))
        for text, cmd in [("新建", on_new), ("复制", on_duplicate),
                          ("保存", on_save), ("删除", on_delete)]:
            ttk.Button(btn_frame, text=text, style="Secondary.TButton",
                       command=cmd).pack(fill=tk.X, pady=1)

        return left, lb

    def _preset_row(self, parent, label: str, row: int, var_name_preset: str,
                    var_name_custom: str, values: list, result_dict: dict):
        """Build a label + combobox + optional custom entry row.
        Stores tk vars in result_dict[var_name_preset] and result_dict[var_name_custom]."""
        tk.Label(parent, text=label, font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"],
                 anchor="w", width=18).grid(row=row, column=0, sticky="w",
                 padx=(0, C_STYLE["pad_sm"]), pady=2)
        pv = tk.StringVar()
        cv = tk.StringVar()
        result_dict[var_name_preset] = pv
        result_dict[var_name_custom] = cv
        cb = ttk.Combobox(parent, textvariable=pv,
                          values=values + ["其它"], width=20, state="readonly")
        cb.grid(row=row, column=1, sticky="w", pady=2)
        custom_entry = ttk.Entry(parent, textvariable=cv, width=20)

        def _on_preset_change(e=None):
            if pv.get() == "其它":
                custom_entry.grid(row=row, column=2, padx=(4, 0), pady=2)
            else:
                custom_entry.grid_remove()

        cb.bind("<<ComboboxSelected>>", _on_preset_change)

    def _text_row(self, parent, label: str, row: int, var_name: str,
                  result_dict: dict, width: int = 30):
        """Build a label + entry row. Stores tk.StringVar in result_dict[var_name]."""
        tk.Label(parent, text=label, font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"],
                 anchor="w", width=18).grid(row=row, column=0, sticky="w",
                 padx=(0, C_STYLE["pad_sm"]), pady=2)
        v = tk.StringVar()
        result_dict[var_name] = v
        ttk.Entry(parent, textvariable=v, width=width).grid(
            row=row, column=1, columnspan=2, sticky="w", pady=2)

    def _build_hw_profiles_sub(self, parent):
        """Hardware profiles sub-tab."""
        GPU_MODELS = ["RTX 4090", "RTX 5090", "RTX PRO 6000 Blackwell",
                      "L40S", "A100", "H100", "H200", "B200", "GB10 / DGX Spark"]
        GPU_COUNTS = ["1", "2", "4", "8"]
        NETWORKS = ["PCIe Single Node", "NVLink", "Ethernet 10G", "Ethernet 25G",
                    "Ethernet 100G", "RoCE 100G", "RoCE 200G",
                    "InfiniBand 200G", "InfiniBand 400G"]
        STORAGES = ["SATA SSD", "NVMe SSD", "RAID NVMe", "NAS"]

        self._hw_vars = {}
        self._hw_profiles_data = []
        self._hw_current_id = None

        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=0)
        parent.grid_columnconfigure(1, weight=1)

        # Left panel
        left_panel, hw_lb = self._profile_panel(
            parent, "_hw_lb",
            on_new=lambda: self._hw_profile_new(),
            on_duplicate=lambda: self._hw_profile_duplicate(),
            on_save=lambda: self._hw_profile_save(),
            on_delete=lambda: self._hw_profile_delete(),
            on_select=lambda e: self._hw_profile_select(hw_lb))
        self._hw_lb = hw_lb

        # Right panel (scrollable form)
        right = ScrollableFrame(parent, bg=C_STYLE["bg_main"])
        right.grid(row=0, column=1, sticky="nsew")
        form = right.content
        form.configure(bg=C_STYLE["bg_card"],
                       highlightbackground=C_STYLE["border"],
                       highlightthickness=1)
        form.columnconfigure(1, weight=1)
        form.columnconfigure(2, weight=1)

        pad = {"padx": C_STYLE["pad_lg"], "pady": C_STYLE["pad_md"]}
        inner = tk.Frame(form, bg=C_STYLE["bg_card"])
        inner.pack(fill=tk.BOTH, expand=True, **pad)
        inner.columnconfigure(1, weight=1)
        inner.columnconfigure(2, weight=1)

        r = 0
        self._text_row(inner, "档案名称 *", r, "profile_name", self._hw_vars); r += 1
        self._text_row(inner, "主机名", r, "hostname", self._hw_vars); r += 1
        self._text_row(inner, "IP 地址", r, "ip_address", self._hw_vars); r += 1
        self._text_row(inner, "CPU 型号", r, "cpu_model", self._hw_vars); r += 1
        self._text_row(inner, "内存 (GB)", r, "memory_gb", self._hw_vars, width=10); r += 1
        self._preset_row(inner, "GPU 型号", r, "gpu_model", "gpu_model_custom",
                         GPU_MODELS, self._hw_vars); r += 1
        self._preset_row(inner, "GPU 数量", r, "gpu_count", "gpu_count_custom",
                         GPU_COUNTS, self._hw_vars); r += 1
        self._preset_row(inner, "互联 / 网络", r, "network_type", "network_type_custom",
                         NETWORKS, self._hw_vars); r += 1
        self._preset_row(inner, "存储类型", r, "storage_type", "storage_type_custom",
                         STORAGES, self._hw_vars); r += 1
        self._text_row(inner, "备注", r, "notes", self._hw_vars, width=40); r += 1

        self._hw_profile_refresh()

    def _hw_profile_refresh(self):
        db_path = getattr(self, "result_db_path_var", tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
        self._hw_profiles_data = _result_db_get_all_profiles(db_path, "hardware_profiles")
        if hasattr(self, "_hw_lb"):
            self._hw_lb.delete(0, tk.END)
            for p in self._hw_profiles_data:
                self._hw_lb.insert(tk.END, p.get("profile_name", f"#{p['id']}"))
        self._refresh_env_dropdowns()

    def _hw_profile_select(self, lb):
        sel = lb.curselection()
        if not sel:
            return
        p = self._hw_profiles_data[sel[0]]
        self._hw_current_id = p["id"]
        for k, v in self._hw_vars.items():
            if isinstance(v, tk.StringVar):
                v.set(str(p.get(k, "") or ""))

    def _hw_profile_new(self):
        self._hw_current_id = None
        for v in self._hw_vars.values():
            if isinstance(v, tk.StringVar):
                v.set("")

    def _hw_profile_duplicate(self):
        sel = self._hw_lb.curselection()
        if not sel:
            return
        p = dict(self._hw_profiles_data[sel[0]])
        p.pop("id", None)
        p["profile_name"] = p.get("profile_name", "") + " (复制)"
        self._hw_current_id = None
        for k, v in self._hw_vars.items():
            if isinstance(v, tk.StringVar):
                v.set(str(p.get(k, "") or ""))

    def _hw_profile_save(self):
        db_path = getattr(self, "result_db_path_var", tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
        data = {k: (v.get() if isinstance(v, tk.StringVar) else "")
                for k, v in self._hw_vars.items()}
        data["id"] = self._hw_current_id
        pid = _result_db_save_profile(db_path, "hardware_profiles", data)
        if pid:
            self._hw_current_id = pid
        self._hw_profile_refresh()

    def _hw_profile_delete(self):
        if not self._hw_current_id:
            return
        if messagebox.askyesno(self.tr("msg.confirm"), self.tr("env.confirm_delete")):
            db_path = getattr(self, "result_db_path_var", tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
            _result_db_delete_profile(db_path, "hardware_profiles", self._hw_current_id)
            self._hw_current_id = None
            self._hw_profile_refresh()

    def _build_sw_profiles_sub(self, parent):
        """Software stack profiles sub-tab."""
        BACKENDS = ["vLLM", "SGLang", "TensorRT-LLM", "llama.cpp", "Ollama", "TGI", "LMDeploy"]
        API_TYPES = ["OpenAI-compatible Chat Completions", "OpenAI-compatible Completions",
                     "Native API"]
        DEPLOYMENT_TYPES = ["Docker", "Docker Compose", "Bare Metal", "Kubernetes", "systemd"]
        PARSERS = ["none", "deepseek-r1", "qwen3", "granite"]

        self._sw_vars = {}
        self._sw_profiles_data = []
        self._sw_current_id = None

        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=0)
        parent.grid_columnconfigure(1, weight=1)

        left_panel, sw_lb = self._profile_panel(
            parent, "_sw_lb",
            on_new=lambda: self._sw_profile_new(),
            on_duplicate=lambda: self._sw_profile_duplicate(),
            on_save=lambda: self._sw_profile_save(),
            on_delete=lambda: self._sw_profile_delete(),
            on_select=lambda e: self._sw_profile_select(sw_lb))
        self._sw_lb = sw_lb

        right = ScrollableFrame(parent, bg=C_STYLE["bg_main"])
        right.grid(row=0, column=1, sticky="nsew")
        inner = tk.Frame(right.content, bg=C_STYLE["bg_card"],
                         highlightbackground=C_STYLE["border"],
                         highlightthickness=1)
        inner.pack(fill=tk.BOTH, expand=True,
                   padx=C_STYLE["pad_lg"], pady=C_STYLE["pad_md"])
        inner.columnconfigure(1, weight=1)
        inner.columnconfigure(2, weight=1)

        r = 0
        self._text_row(inner, "档案名称 *", r, "profile_name", self._sw_vars); r += 1
        self._preset_row(inner, "推理后端", r, "backend", "backend_custom",
                         BACKENDS, self._sw_vars); r += 1
        self._text_row(inner, "后端版本", r, "backend_version", self._sw_vars); r += 1
        self._preset_row(inner, "API 类型", r, "api_type", "api_type_custom",
                         API_TYPES, self._sw_vars); r += 1
        self._preset_row(inner, "部署方式", r, "deployment_type", "deployment_type_custom",
                         DEPLOYMENT_TYPES, self._sw_vars); r += 1
        self._preset_row(inner, "Reasoning Parser", r, "reasoning_parser",
                         "reasoning_parser_custom", PARSERS, self._sw_vars); r += 1
        self._text_row(inner, "API URL", r, "api_url", self._sw_vars); r += 1
        self._text_row(inner, "容器镜像", r, "container_image", self._sw_vars); r += 1
        self._text_row(inner, "Python 版本", r, "python_version", self._sw_vars, width=15); r += 1
        self._text_row(inner, "启动参数", r, "startup_args", self._sw_vars, width=40); r += 1
        self._text_row(inner, "备注", r, "notes", self._sw_vars, width=40); r += 1

        self._sw_profile_refresh()

    def _sw_profile_refresh(self):
        db_path = getattr(self, "result_db_path_var", tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
        self._sw_profiles_data = _result_db_get_all_profiles(db_path, "software_stack_profiles")
        if hasattr(self, "_sw_lb"):
            self._sw_lb.delete(0, tk.END)
            for p in self._sw_profiles_data:
                self._sw_lb.insert(tk.END, p.get("profile_name", f"#{p['id']}"))
        self._refresh_env_dropdowns()

    def _sw_profile_select(self, lb):
        sel = lb.curselection()
        if not sel:
            return
        p = self._sw_profiles_data[sel[0]]
        self._sw_current_id = p["id"]
        for k, v in self._sw_vars.items():
            if isinstance(v, tk.StringVar):
                v.set(str(p.get(k, "") or ""))

    def _sw_profile_new(self):
        self._sw_current_id = None
        for v in self._sw_vars.values():
            if isinstance(v, tk.StringVar):
                v.set("")

    def _sw_profile_duplicate(self):
        sel = self._sw_lb.curselection()
        if not sel:
            return
        p = dict(self._sw_profiles_data[sel[0]])
        p.pop("id", None)
        p["profile_name"] = p.get("profile_name", "") + " (复制)"
        self._sw_current_id = None
        for k, v in self._sw_vars.items():
            if isinstance(v, tk.StringVar):
                v.set(str(p.get(k, "") or ""))

    def _sw_profile_save(self):
        db_path = getattr(self, "result_db_path_var", tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
        data = {k: (v.get() if isinstance(v, tk.StringVar) else "")
                for k, v in self._sw_vars.items()}
        data["id"] = self._sw_current_id
        pid = _result_db_save_profile(db_path, "software_stack_profiles", data)
        if pid:
            self._sw_current_id = pid
        self._sw_profile_refresh()

    def _sw_profile_delete(self):
        if not self._sw_current_id:
            return
        if messagebox.askyesno(self.tr("msg.confirm"), self.tr("env.confirm_delete")):
            db_path = getattr(self, "result_db_path_var", tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
            _result_db_delete_profile(db_path, "software_stack_profiles", self._sw_current_id)
            self._sw_current_id = None
            self._sw_profile_refresh()

    def _build_model_profiles_sub(self, parent):
        """Model deployment profiles sub-tab."""
        FAMILIES = ["Qwen", "DeepSeek", "Llama", "Mixtral", "Yi", "GLM",
                    "Kimi", "InternLM"]
        SIZES = ["7B", "14B", "27B", "32B", "70B", "72B", "122B", "671B"]
        QUANTS = ["BF16", "FP16", "FP8", "NVFP4", "INT8", "INT4",
                  "AWQ", "GPTQ", "GGUF"]
        TYPES = ["Chat", "Instruct", "Reasoning", "Coder", "MoE", "Base"]

        self._model_vars = {}
        self._model_profiles_data = []
        self._model_current_id = None

        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=0)
        parent.grid_columnconfigure(1, weight=1)

        left_panel, model_lb = self._profile_panel(
            parent, "_model_lb",
            on_new=lambda: self._model_profile_new(),
            on_duplicate=lambda: self._model_profile_duplicate(),
            on_save=lambda: self._model_profile_save(),
            on_delete=lambda: self._model_profile_delete(),
            on_select=lambda e: self._model_profile_select(model_lb))
        self._model_lb = model_lb

        right = ScrollableFrame(parent, bg=C_STYLE["bg_main"])
        right.grid(row=0, column=1, sticky="nsew")
        inner = tk.Frame(right.content, bg=C_STYLE["bg_card"],
                         highlightbackground=C_STYLE["border"],
                         highlightthickness=1)
        inner.pack(fill=tk.BOTH, expand=True,
                   padx=C_STYLE["pad_lg"], pady=C_STYLE["pad_md"])
        inner.columnconfigure(1, weight=1)
        inner.columnconfigure(2, weight=1)

        r = 0
        self._text_row(inner, "档案名称 *", r, "profile_name", self._model_vars); r += 1
        self._text_row(inner, "显示名称", r, "display_name", self._model_vars); r += 1
        self._text_row(inner, "API 模型名", r, "api_model_name", self._model_vars); r += 1
        self._text_row(inner, "模型路径", r, "model_path", self._model_vars, width=40); r += 1
        self._preset_row(inner, "模型系列", r, "model_family", "model_family_custom",
                         FAMILIES, self._model_vars); r += 1
        self._preset_row(inner, "参数量", r, "model_size", "model_size_custom",
                         SIZES, self._model_vars); r += 1
        self._preset_row(inner, "精度 / 量化", r, "quantization", "quantization_custom",
                         QUANTS, self._model_vars); r += 1
        self._preset_row(inner, "模型类型", r, "model_type", "model_type_custom",
                         TYPES, self._model_vars); r += 1
        self._text_row(inner, "上下文长度", r, "context_length", self._model_vars, width=10); r += 1
        self._text_row(inner, "Tensor Parallel", r, "tensor_parallel", self._model_vars, width=5); r += 1
        self._text_row(inner, "Pipeline Parallel", r, "pipeline_parallel", self._model_vars, width=5); r += 1
        self._text_row(inner, "Data Parallel", r, "data_parallel", self._model_vars, width=5); r += 1
        self._text_row(inner, "备注", r, "notes", self._model_vars, width=40); r += 1

        self._model_profile_refresh()

    def _model_profile_refresh(self):
        db_path = getattr(self, "result_db_path_var", tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
        self._model_profiles_data = _result_db_get_all_profiles(db_path, "model_profiles")
        if hasattr(self, "_model_lb"):
            self._model_lb.delete(0, tk.END)
            for p in self._model_profiles_data:
                self._model_lb.insert(tk.END, p.get("profile_name", f"#{p['id']}"))
        self._refresh_env_dropdowns()

    def _model_profile_select(self, lb):
        sel = lb.curselection()
        if not sel:
            return
        p = self._model_profiles_data[sel[0]]
        self._model_current_id = p["id"]
        for k, v in self._model_vars.items():
            if isinstance(v, tk.StringVar):
                v.set(str(p.get(k, "") or ""))

    def _model_profile_new(self):
        self._model_current_id = None
        for v in self._model_vars.values():
            if isinstance(v, tk.StringVar):
                v.set("")

    def _model_profile_duplicate(self):
        sel = self._model_lb.curselection()
        if not sel:
            return
        p = dict(self._model_profiles_data[sel[0]])
        p.pop("id", None)
        p["profile_name"] = p.get("profile_name", "") + " (复制)"
        self._model_current_id = None
        for k, v in self._model_vars.items():
            if isinstance(v, tk.StringVar):
                v.set(str(p.get(k, "") or ""))

    def _model_profile_save(self):
        db_path = getattr(self, "result_db_path_var", tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
        data = {k: (v.get() if isinstance(v, tk.StringVar) else "")
                for k, v in self._model_vars.items()}
        data["id"] = self._model_current_id
        pid = _result_db_save_profile(db_path, "model_profiles", data)
        if pid:
            self._model_current_id = pid
        self._model_profile_refresh()

    def _model_profile_delete(self):
        if not self._model_current_id:
            return
        if messagebox.askyesno(self.tr("msg.confirm"), self.tr("env.confirm_delete")):
            db_path = getattr(self, "result_db_path_var", tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
            _result_db_delete_profile(db_path, "model_profiles", self._model_current_id)
            self._model_current_id = None
            self._model_profile_refresh()

    def _build_env_sub(self, parent):
        """Environment profiles (linking HW + SW + Model) sub-tab."""
        self._env_vars = {}
        self._env_profiles_data = []
        self._env_current_id = None

        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=0)
        parent.grid_columnconfigure(1, weight=1)

        left_panel, env_lb = self._profile_panel(
            parent, "_env_lb",
            on_new=lambda: self._env_profile_new(),
            on_duplicate=lambda: self._env_profile_duplicate(),
            on_save=lambda: self._env_profile_save(),
            on_delete=lambda: self._env_profile_delete(),
            on_select=lambda e: self._env_profile_select(env_lb))
        self._env_lb_widget = env_lb

        right = tk.Frame(parent, bg=C_STYLE["bg_card"],
                         highlightbackground=C_STYLE["border"],
                         highlightthickness=1, bd=0)
        right.grid(row=0, column=1, sticky="nsew")
        inner = tk.Frame(right, bg=C_STYLE["bg_card"])
        inner.pack(fill=tk.BOTH, expand=True,
                   padx=C_STYLE["pad_lg"], pady=C_STYLE["pad_md"])
        inner.columnconfigure(1, weight=1)

        r = 0
        self._text_row(inner, "环境名称 *", r, "environment_name", self._env_vars); r += 1

        # Hardware profile selector
        tk.Label(inner, text="硬件档案", font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"],
                 anchor="w", width=18).grid(row=r, column=0, sticky="w",
                 padx=(0, C_STYLE["pad_sm"]), pady=2)
        self._env_hw_var = tk.StringVar()
        self._env_vars["hw_sel"] = self._env_hw_var
        self._env_hw_cb = ttk.Combobox(inner, textvariable=self._env_hw_var,
                                        values=[], width=30, state="readonly")
        self._env_hw_cb.grid(row=r, column=1, sticky="w", pady=2); r += 1

        # Software stack selector
        tk.Label(inner, text="软件栈档案", font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"],
                 anchor="w", width=18).grid(row=r, column=0, sticky="w",
                 padx=(0, C_STYLE["pad_sm"]), pady=2)
        self._env_sw_var = tk.StringVar()
        self._env_vars["sw_sel"] = self._env_sw_var
        self._env_sw_cb = ttk.Combobox(inner, textvariable=self._env_sw_var,
                                        values=[], width=30, state="readonly")
        self._env_sw_cb.grid(row=r, column=1, sticky="w", pady=2); r += 1

        # Model selector
        tk.Label(inner, text="模型档案", font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"],
                 anchor="w", width=18).grid(row=r, column=0, sticky="w",
                 padx=(0, C_STYLE["pad_sm"]), pady=2)
        self._env_model_var = tk.StringVar()
        self._env_vars["model_sel"] = self._env_model_var
        self._env_model_cb = ttk.Combobox(inner, textvariable=self._env_model_var,
                                           values=[], width=30, state="readonly")
        self._env_model_cb.grid(row=r, column=1, sticky="w", pady=2); r += 1

        self._text_row(inner, "备注", r, "notes", self._env_vars, width=40); r += 1

        self._env_profile_refresh()

    def _env_profile_refresh(self):
        db_path = getattr(self, "result_db_path_var", tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
        self._env_profiles_data = _result_db_get_all_profiles(db_path, "environment_profiles")
        if hasattr(self, "_env_lb_widget"):
            self._env_lb_widget.delete(0, tk.END)
            for p in self._env_profiles_data:
                self._env_lb_widget.insert(tk.END, p.get("environment_name", f"#{p['id']}"))
        # Update linked dropdowns in env form
        hw_names = [p.get("profile_name", "") for p in
                    _result_db_get_all_profiles(db_path, "hardware_profiles")]
        sw_names = [p.get("profile_name", "") for p in
                    _result_db_get_all_profiles(db_path, "software_stack_profiles")]
        model_names = [p.get("profile_name", "") for p in
                       _result_db_get_all_profiles(db_path, "model_profiles")]
        if hasattr(self, "_env_hw_cb"):
            self._env_hw_cb["values"] = [""] + hw_names
        if hasattr(self, "_env_sw_cb"):
            self._env_sw_cb["values"] = [""] + sw_names
        if hasattr(self, "_env_model_cb"):
            self._env_model_cb["values"] = [""] + model_names
        # Update main env selector in settings tab
        self._refresh_env_selector()

    def _env_profile_select(self, lb):
        sel = lb.curselection()
        if not sel:
            return
        p = self._env_profiles_data[sel[0]]
        self._env_current_id = p["id"]
        if "environment_name" in self._env_vars:
            self._env_vars["environment_name"].set(p.get("environment_name", ""))
        if "notes" in self._env_vars:
            self._env_vars["notes"].set(p.get("notes", "") or "")
        # Set linked profile names by ID
        db_path = getattr(self, "result_db_path_var", tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
        hw_id = p.get("hardware_profile_id")
        sw_id = p.get("software_stack_profile_id")
        model_id = p.get("model_profile_id")
        hw_name = ""
        sw_name = ""
        model_name = ""
        for hw in _result_db_get_all_profiles(db_path, "hardware_profiles"):
            if hw["id"] == hw_id:
                hw_name = hw.get("profile_name", "")
        for sw in _result_db_get_all_profiles(db_path, "software_stack_profiles"):
            if sw["id"] == sw_id:
                sw_name = sw.get("profile_name", "")
        for m in _result_db_get_all_profiles(db_path, "model_profiles"):
            if m["id"] == model_id:
                model_name = m.get("profile_name", "")
        self._env_hw_var.set(hw_name)
        self._env_sw_var.set(sw_name)
        self._env_model_var.set(model_name)

    def _env_profile_new(self):
        self._env_current_id = None
        for k, v in self._env_vars.items():
            if isinstance(v, tk.StringVar):
                v.set("")

    def _env_profile_duplicate(self):
        sel = self._env_lb_widget.curselection()
        if not sel:
            return
        p = dict(self._env_profiles_data[sel[0]])
        p.pop("id", None)
        p["environment_name"] = p.get("environment_name", "") + " (复制)"
        self._env_current_id = None
        if "environment_name" in self._env_vars:
            self._env_vars["environment_name"].set(p.get("environment_name", ""))

    def _env_profile_save(self):
        db_path = getattr(self, "result_db_path_var", tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
        env_name = self._env_vars.get("environment_name", tk.StringVar()).get()
        if not env_name:
            messagebox.showwarning(self.tr("msg.warning"), "请填写环境名称")
            return
        # Resolve HW/SW/Model IDs from selected names
        hw_name = self._env_hw_var.get()
        sw_name = self._env_sw_var.get()
        model_name = self._env_model_var.get()
        hw_id = next((p["id"] for p in _result_db_get_all_profiles(db_path, "hardware_profiles")
                      if p.get("profile_name") == hw_name), None)
        sw_id = next((p["id"] for p in _result_db_get_all_profiles(db_path, "software_stack_profiles")
                      if p.get("profile_name") == sw_name), None)
        model_id = next((p["id"] for p in _result_db_get_all_profiles(db_path, "model_profiles")
                         if p.get("profile_name") == model_name), None)
        data = {
            "id": self._env_current_id,
            "environment_name": env_name,
            "hardware_profile_id": hw_id,
            "software_stack_profile_id": sw_id,
            "model_profile_id": model_id,
            "notes": self._env_vars.get("notes", tk.StringVar()).get(),
        }
        pid = _result_db_save_profile(db_path, "environment_profiles", data)
        if pid:
            self._env_current_id = pid
        self._env_profile_refresh()

    def _env_profile_delete(self):
        if not self._env_current_id:
            return
        if messagebox.askyesno(self.tr("msg.confirm"), self.tr("env.confirm_delete")):
            db_path = getattr(self, "result_db_path_var", tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
            _result_db_delete_profile(db_path, "environment_profiles", self._env_current_id)
            self._env_current_id = None
            self._env_profile_refresh()

    def _refresh_env_selector(self):
        """Update the environment profile combobox in the settings tab.
        Default is empty (未指定环境); user must explicitly select a profile."""
        if not hasattr(self, "_env_profile_cb"):
            return
        db_path = getattr(self, "result_db_path_var", tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
        # Ensure DB + defaults exist on first call
        if not os.path.exists(db_path):
            _init_result_db(db_path)
        profiles = _result_db_get_all_profiles(db_path, "environment_profiles")
        names = [p.get("environment_name", "") for p in profiles]
        current = self.env_profile_var.get()
        self._env_profile_cb["values"] = [""] + names
        if current in names:
            pass  # keep current selection
        else:
            self.env_profile_var.set("")  # default: unspecified (未指定环境)

    def _refresh_env_dropdowns(self):
        """Refresh all environment-related dropdowns (called when profiles change)."""
        try:
            self._refresh_env_selector()
        except Exception:
            pass

    # ── Environment Parameter Reader ─────────────────────────────────────────

    def _validate_environment_required_params(self, sw: dict, model: dict) -> list:
        """Return list of human-readable missing required field names.
        Required: software_stack_profile.api_url, model_profile.api_model_name."""
        sw = sw or {}
        model = model or {}
        missing = []
        if not str(sw.get("api_url") or "").strip():
            missing.append("API URL  (software_stack_profile → api_url)")
        if not str(model.get("api_model_name") or "").strip():
            missing.append("API Model Name  (model_profile → api_model_name)")
        return missing

    def _build_environment_param_map(self, sw: dict, model: dict) -> dict:
        """Build mapping of param_key → (tk.Variable, new_value, display_label)
        from the active environment profile's software stack and model profile."""
        sw = sw or {}
        model = model or {}
        param_map: dict = {}
        # Required parameters
        api_url = str(sw.get("api_url") or "").strip()
        api_model_name = str(model.get("api_model_name") or "").strip()
        if api_url:
            param_map["api_url"] = (self.url_var, api_url, "API URL")
        if api_model_name:
            param_map["model_name"] = (self.model_var, api_model_name, "Model Name")
        return param_map

    def _apply_environment_param_map(self, param_map: dict) -> tuple:
        """Apply param_map to UI variables with overwrite-protection dialog.

        Partition fields into:
          - directly_applicable: target field is empty or already matches
          - conflicting: target field has a different non-empty value

        Conflicting fields are presented in a single confirmation dialog.
        Returns (applied_fields: list, overridden_fields: list).
        """
        directly_applicable: dict = {}
        conflicting: dict = {}

        for key, (var, new_val, label) in param_map.items():
            try:
                current = str(var.get()).strip()
            except Exception:
                current = ""
            if not current or current == new_val:
                directly_applicable[key] = (var, new_val)
            else:
                conflicting[key] = (var, new_val, label, current)

        applied_fields: list = []
        overridden_fields: list = []

        # Ask once for all conflicting fields
        if conflicting:
            field_lines = "\n".join(
                f"  {label}: {repr(cur)!s} → {repr(new)!s}"
                for key, (var, new, label, cur) in conflicting.items()
            )
            msg = self.tr("env.read_params_overwrite") + "\n\n" + field_lines
            if messagebox.askyesno(self.tr("msg.confirm"), msg, parent=self.root):
                for key, (var, new_val, label, _) in conflicting.items():
                    var.set(new_val)
                    applied_fields.append(key)
                    overridden_fields.append(key)

        # Apply non-conflicting fields directly
        for key, (var, new_val) in directly_applicable.items():
            var.set(new_val)
            applied_fields.append(key)

        return applied_fields, overridden_fields

    def _read_selected_environment_params(self):
        """Read the active environment profile and auto-fill benchmark parameters.

        Validates required fields (api_url, api_model_name), applies them to
        the settings tab, and updates applied_environment_params tracking state
        for inclusion in benchmark_run DB records.

        Must be called from the main thread only (UI method).
        """
        env_name = self.env_profile_var.get()
        if not env_name:
            messagebox.showinfo(
                self.tr("env.read_params_btn"),
                self.tr("env.read_params_no_env"),
                parent=self.root)
            return

        env_info = self._get_active_env_info()
        sw = env_info.get("sw", {})
        model = env_info.get("model", {})

        # Validate required params
        missing = self._validate_environment_required_params(sw, model)
        if missing:
            msg = (self.tr("env.read_params_missing_fields") + "\n" +
                   "\n".join(f"  - {m}" for m in missing))
            messagebox.showwarning(self.tr("env.read_params_btn"), msg, parent=self.root)
            return

        # Build and apply param map with overwrite protection
        param_map = self._build_environment_param_map(sw, model)
        applied_fields, overridden_fields = self._apply_environment_param_map(param_map)

        # Track applied_environment_params metadata for DB persistence
        self._applied_environment_params = bool(applied_fields)
        self._applied_fields_json = list(applied_fields)
        self._overridden_fields_json = list(overridden_fields)

        if applied_fields:
            messagebox.showinfo(
                self.tr("env.read_params_btn"),
                self.tr("env.read_params_applied"),
                parent=self.root)

    def _get_active_env_info(self) -> dict:
        """Return info about the currently selected environment profile,
        including a 'snapshots' dict with human-readable values for DB storage."""
        db_path = getattr(self, "result_db_path_var", tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
        env_name = getattr(self, "env_profile_var", tk.StringVar()).get()
        result = {
            "env_name": env_name or "",
            "env_profile_id": None,
            "hw_profile_id": None,
            "sw_profile_id": None,
            "model_profile_id": None,
            "hw": {},
            "sw": {},
            "model": {},
            "snapshots": {
                "env_name": env_name or "未指定环境",
                "hw_name": "", "sw_name": "", "model_name": "",
                "gpu_model": "", "gpu_count": "",
                "backend": "", "backend_version": "",
                "api_type": "", "deployment_type": "",
                "reasoning_parser": "",
                "model_family": "", "model_size": "",
                "quantization": "", "model_type": "",
            },
        }
        if not env_name:
            return result
        profiles = _result_db_get_all_profiles(db_path, "environment_profiles")
        env = next((p for p in profiles if p.get("environment_name") == env_name), None)
        if not env:
            return result
        result["env_profile_id"] = env["id"]
        hw_id = env.get("hardware_profile_id")
        sw_id = env.get("software_stack_profile_id")
        model_id = env.get("model_profile_id")
        result["hw_profile_id"] = hw_id
        result["sw_profile_id"] = sw_id
        result["model_profile_id"] = model_id
        if hw_id:
            hw_list = _result_db_get_all_profiles(db_path, "hardware_profiles")
            hw = next((p for p in hw_list if p["id"] == hw_id), {})
            result["hw"] = hw
            result["snapshots"]["hw_name"]   = hw.get("profile_name", "")
            result["snapshots"]["gpu_model"] = (hw.get("gpu_model") or
                                                hw.get("gpu_model_custom") or "")
            result["snapshots"]["gpu_count"] = (hw.get("gpu_count") or
                                                hw.get("gpu_count_custom") or "")
        if sw_id:
            sw_list = _result_db_get_all_profiles(db_path, "software_stack_profiles")
            sw = next((p for p in sw_list if p["id"] == sw_id), {})
            result["sw"] = sw
            result["snapshots"]["sw_name"]          = sw.get("profile_name", "")
            result["snapshots"]["backend"]           = (sw.get("backend") or
                                                        sw.get("backend_custom") or "")
            result["snapshots"]["backend_version"]   = sw.get("backend_version", "") or ""
            result["snapshots"]["api_type"]          = (sw.get("api_type") or
                                                        sw.get("api_type_custom") or "")
            result["snapshots"]["deployment_type"]   = (sw.get("deployment_type") or
                                                        sw.get("deployment_type_custom") or "")
            result["snapshots"]["reasoning_parser"]  = (sw.get("reasoning_parser") or
                                                        sw.get("reasoning_parser_custom") or "")
        if model_id:
            model_list = _result_db_get_all_profiles(db_path, "model_profiles")
            model = next((p for p in model_list if p["id"] == model_id), {})
            result["model"] = model
            result["snapshots"]["model_name"]   = model.get("profile_name", "")
            result["snapshots"]["model_family"] = (model.get("model_family") or
                                                   model.get("model_family_custom") or "")
            result["snapshots"]["model_size"]   = (model.get("model_size") or
                                                   model.get("model_size_custom") or "")
            result["snapshots"]["quantization"] = (model.get("quantization") or
                                                   model.get("quantization_custom") or "")
            result["snapshots"]["model_type"]   = (model.get("model_type") or
                                                   model.get("model_type_custom") or "")
        return result

    def _build_db_settings_sub(self, parent):
        """Database settings sub-tab."""
        parent.configure(bg=C_STYLE["bg_main"])
        card = SectionCard(parent, "数据库设置")
        card.pack(fill=tk.X, padx=C_STYLE["pad_lg"], pady=C_STYLE["pad_lg"])

        inner = card.content
        inner.columnconfigure(1, weight=1)

        self.result_db_path_var = tk.StringVar(value=RESULT_DB_PATH)
        self.results_root_var = tk.StringVar(value=RESULTS_ROOT)

        r = 0
        tk.Label(inner, text=self.tr("db.path_label"), font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"], anchor="w"
                 ).grid(row=r, column=0, sticky="w",
                        padx=(0, C_STYLE["pad_sm"]), pady=C_STYLE["gap_sm"])
        ttk.Entry(inner, textvariable=self.result_db_path_var, width=50
                  ).grid(row=r, column=1, sticky="ew",
                         padx=(0, C_STYLE["pad_sm"]), pady=C_STYLE["gap_sm"]); r += 1

        tk.Label(inner, text=self.tr("db.results_root"), font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"], anchor="w"
                 ).grid(row=r, column=0, sticky="w",
                        padx=(0, C_STYLE["pad_sm"]), pady=C_STYLE["gap_sm"])
        ttk.Entry(inner, textvariable=self.results_root_var, width=50
                  ).grid(row=r, column=1, sticky="ew",
                         padx=(0, C_STYLE["pad_sm"]), pady=C_STYLE["gap_sm"]); r += 1

        btn_frame = tk.Frame(inner, bg=C_STYLE["bg_card"])
        btn_frame.grid(row=r, column=0, columnspan=2, sticky="w",
                       pady=C_STYLE["gap_md"]); r += 1
        ttk.Button(btn_frame, text=self.tr("db.init_btn"),
                   command=self._db_init_action).pack(side=tk.LEFT,
                   padx=(0, C_STYLE["pad_sm"]))
        ttk.Button(btn_frame, text=self.tr("db.test_btn"),
                   command=self._db_test_action).pack(side=tk.LEFT)

        self._db_status_lbl = tk.Label(inner, text="", font=C_STYLE["font_small"],
                                       bg=C_STYLE["bg_card"], fg=C_STYLE["text_muted"])
        self._db_status_lbl.grid(row=r, column=0, columnspan=2, sticky="w",
                                 pady=C_STYLE["gap_sm"])

    def _db_init_action(self):
        db_path = getattr(self, "result_db_path_var", tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
        ok = _init_result_db(db_path)
        msg = self.tr("db.init_ok") if ok else "数据库初始化失败"
        if hasattr(self, "_db_status_lbl"):
            self._db_status_lbl.config(text=msg)
        self._hw_profile_refresh()
        self._sw_profile_refresh()
        self._model_profile_refresh()
        self._env_profile_refresh()

    def _db_test_action(self):
        db_path = getattr(self, "result_db_path_var", tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
        try:
            _init_result_db(db_path)
            conn = sqlite3.connect(db_path)
            conn.execute("SELECT 1 FROM hardware_profiles LIMIT 1")
            conn.close()
            msg = self.tr("db.test_ok")
        except Exception as e:
            msg = f"{self.tr('db.test_fail')}: {e}"
        if hasattr(self, "_db_status_lbl"):
            self._db_status_lbl.config(text=msg)

    def _format_env_summary(self, env_info: dict) -> str:
        """Format environment info for inclusion in text reports."""
        lines = []
        lines.append("  ═══════════ 被测环境 (report_dir) ═══════════")
        env_name = env_info.get("env_name") or "未指定"
        lines.append(f"  Environment Profile: {env_name}")
        hw = env_info.get("hw", {})
        sw = env_info.get("sw", {})
        model = env_info.get("model", {})
        if hw:
            gpu = hw.get("gpu_model") or hw.get("gpu_model_custom") or "-"
            gpu_count = hw.get("gpu_count") or hw.get("gpu_count_custom") or "-"
            lines.append(f"  Hardware: {hw.get('profile_name', '-')}")
            lines.append(f"  GPU: {gpu}  GPU Count: {gpu_count}")
        else:
            lines.append("  Hardware: -")
        if sw:
            backend = sw.get("backend") or sw.get("backend_custom") or "-"
            backend_ver = sw.get("backend_version") or "-"
            deployment = sw.get("deployment_type") or sw.get("deployment_type_custom") or "-"
            reasoning_parser = sw.get("reasoning_parser") or sw.get("reasoning_parser_custom") or "-"
            lines.append(f"  Backend: {backend} {backend_ver}  Deployment: {deployment}")
            lines.append(f"  Reasoning Parser: {reasoning_parser}")
        else:
            lines.append("  Backend: -")
        if model:
            quant = model.get("quantization") or model.get("quantization_custom") or "-"
            mtype = model.get("model_type") or model.get("model_type_custom") or "-"
            lines.append(f"  Model Profile: {model.get('profile_name', '-')}")
            lines.append(f"  Quantization: {quant}  Type: {mtype}")
        else:
            lines.append("  Model Profile: -")
        if not env_info.get("env_name"):
            lines.append(f"  ⚠ 未绑定环境档案，本次结果不建议用于长期横向对比。")
        lines.append("  " + "─" * 55)
        lines.append("")
        return "\n".join(lines) + "\n"

    def _build_sweep_tab(self):
        """Build the 并发扫测 (concurrency sweep) tab."""
        sf = self.sweep_frame
        sf.grid_columnconfigure(0, weight=1)
        sf.grid_rowconfigure(0, weight=1)

        sweep_scroll = ScrollableFrame(sf, bg=C_STYLE["bg_main"])
        sweep_scroll.grid(row=0, column=0, sticky="nsew")
        sf_inner = tk.Frame(sweep_scroll.content, bg=C_STYLE["bg_main"])
        sf_inner.pack(fill=tk.BOTH, expand=True, padx=C_STYLE["pad_lg"], pady=C_STYLE["pad_lg"])
        sf_inner.grid_columnconfigure(0, weight=1)
        sf_inner.grid_rowconfigure(0, weight=0)  # config card — fixed
        sf_inner.grid_rowconfigure(1, weight=0)  # status area — fixed
        sf_inner.grid_rowconfigure(2, weight=0)  # chart area — fixed height
        sf_inner.grid_rowconfigure(3, weight=0)  # expert — fixed
        sf_inner.grid_rowconfigure(4, weight=1)  # output files — expandable

        # ── Config Card ──
        config_card = SectionCard(sf_inner, "扫测配置与操作",
                                  collapsible=True, expanded=True)
        config_card.grid(row=0, column=0, sticky="ew", pady=(0, C_STYLE["gap_lg"]))
        self._sweep_config_card = config_card

        cfg = config_card.content
        cfg.grid_columnconfigure(1, weight=1)

        # ── Row 0: Sweep Scale + Tier selectors (i18n) ──
        _scale_lbl = tk.Label(cfg, text=self.tr("sweep.scale_label"),
                              font=C_STYLE["font_body"],
                              bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"])
        _scale_lbl.grid(row=0, column=0, sticky="w",
                        padx=(0, C_STYLE["pad_sm"]), pady=(0, C_STYLE["gap_sm"]))
        self._register_i18n_widget(_scale_lbl, "sweep.scale_label")
        scale_row = tk.Frame(cfg, bg=C_STYLE["bg_card"])
        scale_row.grid(row=0, column=1, columnspan=2, sticky="ew", pady=(0, C_STYLE["gap_sm"]))

        # Internal code StringVars (always hold English code like "medium", "formal")
        self._sweep_scale_var = tk.StringVar(value="medium")
        self._sweep_tier_var  = tk.StringVar(value="formal")

        # Display StringVars (hold the localized label shown in combo)
        self._sweep_scale_display_var = tk.StringVar(value=self.tr("sweep.scale.medium"))
        self._sweep_tier_display_var  = tk.StringVar(value=self.tr("sweep.tier.formal"))

        # Scale combobox — values rebuilt on language switch via i18n callback
        self._scale_combo = ttk.Combobox(scale_row, textvariable=self._sweep_scale_display_var,
                                         values=[self.tr(f"sweep.scale.{c}") for c in _SCALE_CODES],
                                         width=16, state="readonly")
        self._scale_combo.pack(side=tk.LEFT)

        _tier_sep_lbl = tk.Label(scale_row, text=f"  {self.tr('sweep.tier_label')}",
                                  font=C_STYLE["font_body"],
                                  bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"])
        _tier_sep_lbl.pack(side=tk.LEFT)
        self._register_i18n_widget(_tier_sep_lbl, "sweep.tier_label",
                                   attr="text")  # prefix space handled in callback

        # Tier combobox — values rebuilt on language switch via i18n callback
        self._tier_combo = ttk.Combobox(scale_row, textvariable=self._sweep_tier_display_var,
                                        values=[self.tr(f"sweep.tier.{c}") for c in _TIER_CODES],
                                        width=16, state="readonly")
        self._tier_combo.pack(side=tk.LEFT, padx=(C_STYLE["pad_sm"], 0))

        # Bind change events (index-based lookup — language independent)
        self._scale_combo.bind("<<ComboboxSelected>>", lambda e: self._on_sweep_scale_tier_change())
        self._tier_combo.bind("<<ComboboxSelected>>",  lambda e: self._on_sweep_scale_tier_change())

        # ── Row 0b: Scale description + preset status ──
        self._sweep_desc_var          = tk.StringVar(value=self.tr("sweep.scale.desc.medium"))
        self._sweep_preset_status_var = tk.StringVar(
            value=self.tr("sweep.preset_status.applied").format(
                scale=self.tr("sweep.scale.medium"),
                tier=self.tr("sweep.tier.formal")))
        desc_row = tk.Frame(cfg, bg=C_STYLE["bg_card"])
        desc_row.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, C_STYLE["gap_sm"]))
        tk.Label(desc_row, textvariable=self._sweep_desc_var, font=C_STYLE["font_small"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_muted"]).pack(side=tk.LEFT)
        tk.Label(desc_row, text="  |  ", font=C_STYLE["font_small"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_muted"]).pack(side=tk.LEFT)
        self._sweep_status_lbl = tk.Label(desc_row, textvariable=self._sweep_preset_status_var,
                                          font=C_STYLE["font_small"],
                                          bg=C_STYLE["bg_card"],
                                          fg=C_STYLE.get("text_accent", C_STYLE["accent"]))
        self._sweep_status_lbl.pack(side=tk.LEFT)

        # ── Row 2: Concurrency list (auto-generated, editable) ──
        _conc_lbl = tk.Label(cfg, text=self.tr("sweep.conc_list_label"),
                             font=C_STYLE["font_body"],
                             bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"])
        _conc_lbl.grid(row=2, column=0, sticky="w",
                       padx=(0, C_STYLE["pad_sm"]), pady=(0, C_STYLE["gap_sm"]))
        self._register_i18n_widget(_conc_lbl, "sweep.conc_list_label")
        # Initialize with medium+formal preset
        _medium_formal = SWEEP_SCALE_DEFS["medium"]["formal"]
        self.sweep_conc_var = tk.StringVar(value=",".join(str(c) for c in _medium_formal))
        # Track programmatic vs manual edits
        self._applying_sweep_preset = False
        self._sweep_preset_modified = tk.BooleanVar(value=False)
        self._sweep_source_scale    = "medium"
        self._sweep_source_tier     = "formal"
        self.sweep_conc_var.trace_add("write", self._on_sweep_conc_var_changed)
        ttk.Entry(cfg, textvariable=self.sweep_conc_var, width=40).grid(
            row=2, column=1, sticky="ew", pady=(0, C_STYLE["gap_sm"]))
        _conc_hint_lbl = tk.Label(cfg, text=self.tr("sweep.conc_list_hint"),
                                   font=C_STYLE["font_small"],
                                   bg=C_STYLE["bg_card"], fg=C_STYLE["text_muted"])
        _conc_hint_lbl.grid(row=2, column=2, sticky="w",
                             padx=(C_STYLE["pad_sm"], 0), pady=(0, C_STYLE["gap_sm"]))
        self._register_i18n_widget(_conc_hint_lbl, "sweep.conc_list_hint")

        # ── Row 3: Request count rule ──
        _rule_lbl = tk.Label(cfg, text=self.tr("sweep.request_rule_label"),
                             font=C_STYLE["font_body"],
                             bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"])
        _rule_lbl.grid(row=3, column=0, sticky="w",
                       padx=(0, C_STYLE["pad_sm"]), pady=(0, C_STYLE["gap_sm"]))
        self._register_i18n_widget(_rule_lbl, "sweep.request_rule_label")
        # Tier formula label (shown when tier != custom)
        self._sweep_tier_formula_var = tk.StringVar(
            value=self.tr("sweep.tier_formula_prefix") + SWEEP_TIER_DEFS["formal"]["formula"])
        self._sweep_tier_rule_lbl_frame = tk.Frame(cfg, bg=C_STYLE["bg_card"])
        self._sweep_tier_rule_lbl_frame.grid(row=3, column=1, columnspan=2, sticky="ew",
                                              pady=(0, C_STYLE["gap_sm"]))
        tk.Label(self._sweep_tier_rule_lbl_frame, textvariable=self._sweep_tier_formula_var,
                 font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"]).pack(side=tk.LEFT)
        # Custom rule radio buttons (shown when tier == custom, i18n labels)
        self.sweep_request_rule_var = tk.StringVar(value="formal")  # default = formal tier
        self._sweep_custom_rule_frame = tk.Frame(cfg, bg=C_STYLE["bg_card"])
        self._sweep_custom_rule_frame.grid(row=3, column=1, columnspan=2, sticky="ew",
                                           pady=(0, C_STYLE["gap_sm"]))
        # Store radio button refs for i18n refresh
        self._sweep_custom_rule_rbs = []
        _custom_rule_opts = [
            ("sweep.custom_rule.x2",    "x2"),
            ("sweep.custom_rule.x5",    "x5"),
            ("sweep.custom_rule.x10",   "x10"),
            ("sweep.custom_rule.fixed", "fixed"),
        ]
        for i18n_key, val in _custom_rule_opts:
            rb = ttk.Radiobutton(self._sweep_custom_rule_frame, text=self.tr(i18n_key),
                                 variable=self.sweep_request_rule_var,
                                 value=val, command=self._on_request_rule_change)
            rb.pack(side=tk.LEFT, padx=(0, C_STYLE["pad_sm"]))
            self._sweep_custom_rule_rbs.append((rb, i18n_key))
        self._sweep_custom_rule_frame.grid_remove()  # hidden when tier != custom

        # Fixed total entry (shown only for custom tier + "fixed" rule)
        self.sweep_fixed_total_var = tk.IntVar(value=100)
        self._sweep_fixed_total_frame = tk.Frame(cfg, bg=C_STYLE["bg_card"])
        self._sweep_fixed_total_frame.grid(row=4, column=0, columnspan=3, sticky="w",
                                           pady=(0, C_STYLE["gap_sm"]))
        _ftl = tk.Label(self._sweep_fixed_total_frame, text=self.tr("sweep.fixed_total_label"),
                        font=C_STYLE["font_body"],
                        bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"])
        _ftl.pack(side=tk.LEFT, padx=(0, C_STYLE["pad_sm"]))
        self._register_i18n_widget(_ftl, "sweep.fixed_total_label")
        ttk.Spinbox(self._sweep_fixed_total_frame, from_=1, to=99999, increment=10,
                    textvariable=self.sweep_fixed_total_var, width=8).pack(side=tk.LEFT)
        _fth = tk.Label(self._sweep_fixed_total_frame, text=self.tr("sweep.fixed_total_hint"),
                        font=C_STYLE["font_small"], bg=C_STYLE["bg_card"],
                        fg=C_STYLE["text_muted"])
        _fth.pack(side=tk.LEFT, padx=(C_STYLE["pad_sm"], 0))
        self._register_i18n_widget(_fth, "sweep.fixed_total_hint")
        self._sweep_fixed_total_frame.grid_remove()  # hidden by default

        # Register i18n callback to rebuild combo options + status on language switch
        self._register_i18n_callback(self._refresh_sweep_scale_tier_i18n)

        # Keep sweep_mult_var for backward compatibility
        self.sweep_mult_var = tk.IntVar(value=2)

        # Row 5: Resource mode (always disabled for MVP)
        tk.Label(cfg, text="资源监测", font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"]).grid(
            row=5, column=0, sticky="w", padx=(0, C_STYLE["pad_sm"]), pady=(0, C_STYLE["gap_sm"]))
        self.sweep_resource_var = tk.StringVar(value="disabled")
        res_combo = ttk.Combobox(cfg, textvariable=self.sweep_resource_var,
                                 values=["disabled"], width=12, state="readonly")
        res_combo.grid(row=5, column=1, sticky="w", pady=(0, C_STYLE["gap_sm"]))
        res_combo.current(0)
        tk.Label(cfg, text="MVP 阶段资源监测暂不可用",
                 font=C_STYLE["font_small"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_muted"]).grid(
            row=5, column=2, sticky="w", padx=(C_STYLE["pad_sm"], 0), pady=(0, C_STYLE["gap_sm"]))

        # Row 6: Save options
        save_lbl = tk.Label(cfg, text="保存选项", font=C_STYLE["font_body"],
                            bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"])
        save_lbl.grid(row=6, column=0, sticky="w",
                      padx=(0, C_STYLE["pad_sm"]), pady=(C_STYLE["gap_sm"], 0))
        save_row = tk.Frame(cfg, bg=C_STYLE["bg_card"])
        save_row.grid(row=6, column=1, columnspan=2, sticky="ew",
                      pady=(C_STYLE["gap_sm"], 0))
        self.sweep_save_json_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(save_row, text="保存扫测数据 JSON",
                        variable=self.sweep_save_json_var).pack(side=tk.LEFT)
        self.sweep_save_md_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(save_row, text="保存文字分析报告 Markdown",
                        variable=self.sweep_save_md_var).pack(side=tk.LEFT,
                        padx=(C_STYLE["pad_sm"], 0))
        self.sweep_save_png_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(save_row, text="保存图表 PNG",
                        variable=self.sweep_save_png_var).pack(side=tk.LEFT,
                        padx=(C_STYLE["pad_sm"], 0))
        self.sweep_save_history_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(save_row, text="保存到历史记录",
                        variable=self.sweep_save_history_var).pack(side=tk.LEFT,
                        padx=(C_STYLE["pad_sm"], 0))

        # Row 7: Start button
        btn_row = tk.Frame(cfg, bg=C_STYLE["bg_card"])
        btn_row.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(C_STYLE["gap_sm"], 0))
        self.sweep_start_btn = ttk.Button(btn_row, text="开始扫测",
                                          style="Primary.TButton",
                                          command=self._start_sweep)
        self.sweep_start_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # ── Status Card (row 1, collapsed) ──
        status_card = SectionCard(sf_inner, "扫测状态",
                                  collapsible=True, expanded=False)
        status_card.grid(row=1, column=0, sticky="ew", pady=(0, C_STYLE["gap_lg"]))
        self._sweep_status_card = status_card
        sc = status_card.content
        self.sweep_status_text = tk.Text(sc, font=C_STYLE["font_small"],
                                         height=6, wrap=tk.WORD,
                                         bg=C_STYLE["bg_input"],
                                         fg=C_STYLE["text_primary"],
                                         highlightbackground=C_STYLE["border"],
                                         highlightthickness=1,
                                         relief=tk.FLAT, borderwidth=0,
                                         padx=8, pady=6,
                                         state=tk.DISABLED)
        self.sweep_status_text.pack(fill=tk.X)

        # ── Chart Frame (row 2, collapsed) ──
        chart_card = SectionCard(sf_inner, "图形分析",
                                 collapsible=True, expanded=False)
        chart_card.grid(row=2, column=0, sticky="ew", pady=(0, C_STYLE["gap_lg"]))
        self._sweep_chart_card = chart_card
        chart_card.columnconfigure(0, weight=1)
        chart_card.rowconfigure(0, weight=1)
        self.sweep_chart_frame = tk.Frame(chart_card.content, bg=C_STYLE["bg_card"],
                                          height=520)
        self.sweep_chart_frame.pack(fill="both", expand=True)
        self.sweep_chart_frame.pack_propagate(False)
        # Status label inside chart frame (shown when matplotlib missing)
        self.sweep_chart_status_var = tk.StringVar(value="")
        self._sweep_chart_status_lbl = tk.Label(
            self.sweep_chart_frame, textvariable=self.sweep_chart_status_var,
            font=C_STYLE["font_body"],
            bg=C_STYLE["bg_card"], fg=C_STYLE["text_secondary"],
            wraplength=400)
        self._sweep_chart_status_lbl.place(relx=0.5, rely=0.5, anchor="center")
        # state
        self._sweep_chart_canvas = None
        self._sweep_chart_figure = None

        # ── Output Files Card (row 4, collapsed) ──
        results_card = SectionCard(sf_inner, "输出文件",
                                   collapsible=True, expanded=False)
        results_card.grid(row=4, column=0, sticky="nsew", pady=(C_STYLE["gap_lg"], 0))
        self._sweep_output_card = results_card
        results_card.columnconfigure(0, weight=1)
        results_card.rowconfigure(0, weight=1)
        self.sweep_result_text = tk.Text(results_card.content,
                                         font=C_STYLE["font_code"],
                                         wrap=tk.WORD,
                                         bg=C_STYLE["bg_card"],
                                         fg=C_STYLE["text_primary"],
                                         relief=tk.FLAT, borderwidth=0,
                                         state=tk.DISABLED)
        self.sweep_result_text.grid(row=0, column=0, sticky="nsew")
        rscroll = ttk.Scrollbar(results_card.content, orient=tk.VERTICAL,
                                command=self.sweep_result_text.yview)
        rscroll.grid(row=0, column=1, sticky="ns")
        self.sweep_result_text.configure(yscrollcommand=rscroll.set)

        # sweep state variables
        self._sweep_running = False
        self._sweep_result = None

        # ── Expert Analysis Card (row 4, collapsible, shown after sweep) ──
        self._expert_card = SectionCard(sf_inner, "专家分析简评",
                                        collapsible=True, expanded=False)
        self._expert_card.grid(row=3, column=0, sticky="ew")
        self._expert_card.columnconfigure(0, weight=1)
        self._expert_text = tk.Text(self._expert_card.content,
                                    font=C_STYLE["font_body"],
                                    wrap=tk.WORD,
                                    bg=C_STYLE["bg_card"],
                                    fg=C_STYLE["text_primary"],
                                    relief=tk.FLAT, borderwidth=0,
                                    height=6,
                                    state=tk.DISABLED)
        self._expert_text.pack(fill=tk.X)

    def _on_request_rule_change(self):
        """Show/hide the fixed-total-requests entry based on selected rule (custom tier only)."""
        if self.sweep_request_rule_var.get() == "fixed":
            self._sweep_fixed_total_frame.grid()
        else:
            self._sweep_fixed_total_frame.grid_remove()

    def _on_sweep_conc_var_changed(self, *_):
        """Detect manual edits to sweep_conc_var and mark preset as modified."""
        if getattr(self, "_applying_sweep_preset", False):
            return  # programmatic update — not a user edit
        if not hasattr(self, "_sweep_preset_modified"):
            return
        self._sweep_preset_modified.set(True)
        self._update_sweep_preset_status_label()

    def _on_sweep_scale_tier_change(self, *_):
        """Apply the selected scale+tier preset to concurrency list and request rule."""
        scale_combo = getattr(self, "_scale_combo", None)
        tier_combo  = getattr(self, "_tier_combo",  None)
        if scale_combo is None or tier_combo is None:
            return
        scale_idx = scale_combo.current()
        tier_idx  = tier_combo.current()
        scale = _SCALE_CODES[scale_idx] if 0 <= scale_idx < len(_SCALE_CODES) else "medium"
        tier  = _TIER_CODES[tier_idx]   if 0 <= tier_idx  < len(_TIER_CODES)  else "formal"
        self._sweep_scale_var.set(scale)
        self._sweep_tier_var.set(tier)

        # Auto-generate concurrency list when scale != custom
        if scale != "custom":
            scale_def = SWEEP_SCALE_DEFS.get(scale, {})
            # Use tier concurrency if tier is named; else fall back to formal
            tier_key  = tier if tier in ("quick", "formal", "extended") else "formal"
            conc_list = scale_def.get(tier_key, [])
            if conc_list:
                self._applying_sweep_preset = True
                try:
                    self.sweep_conc_var.set(",".join(str(c) for c in conc_list))
                finally:
                    self._applying_sweep_preset = False
            # Track source preset
            self._sweep_source_scale = scale
            self._sweep_source_tier  = tier if tier != "custom" else "formal"

        # Update description label (i18n)
        if hasattr(self, "_sweep_desc_var"):
            self._sweep_desc_var.set(self.tr(f"sweep.scale.desc.{scale}"))

        # Update request rule display
        self._update_sweep_tier_rule_ui(tier)

        # Reset modified flag (fresh preset applied)
        self._sweep_preset_modified.set(False)
        self._update_sweep_preset_status_label()

    def _update_sweep_tier_rule_ui(self, tier: str):
        """Show tier formula label or custom rule radio buttons."""
        if tier == "custom":
            # Hide formula label, show custom radio buttons
            if hasattr(self, "_sweep_tier_rule_lbl_frame"):
                self._sweep_tier_rule_lbl_frame.grid_remove()
            if hasattr(self, "_sweep_custom_rule_frame"):
                self._sweep_custom_rule_frame.grid()
            # Don't overwrite sweep_request_rule_var — keep whatever user chose
        else:
            # Show formula label, hide custom radio buttons
            if hasattr(self, "_sweep_custom_rule_frame"):
                self._sweep_custom_rule_frame.grid_remove()
            if hasattr(self, "_sweep_fixed_total_frame"):
                self._sweep_fixed_total_frame.grid_remove()
            formula = SWEEP_TIER_DEFS.get(tier, {}).get("formula", "")
            if hasattr(self, "_sweep_tier_formula_var"):
                self._sweep_tier_formula_var.set(self.tr("sweep.tier_formula_prefix") + formula)
            if hasattr(self, "_sweep_tier_rule_lbl_frame"):
                self._sweep_tier_rule_lbl_frame.grid()
            # Set the internal rule variable to the tier name
            self.sweep_request_rule_var.set(tier)

    def _update_sweep_preset_status_label(self):
        """Update the preset status label text (i18n-aware)."""
        if not hasattr(self, "_sweep_preset_status_var"):
            return
        scale = getattr(self, "_sweep_scale_var", None)
        scale = scale.get() if scale else "custom"
        tier  = getattr(self, "_sweep_tier_var", None)
        tier  = tier.get() if tier else "custom"
        modified = getattr(self, "_sweep_preset_modified", None)
        modified = modified.get() if modified else False

        if modified:
            src_s_code = getattr(self, "_sweep_source_scale", scale)
            src_t_code = getattr(self, "_sweep_source_tier",  tier)
            status = self.tr("sweep.preset_status.modified").format(
                src_scale=self.tr(f"sweep.scale.{src_s_code}"),
                src_tier=self.tr(f"sweep.tier.{src_t_code}"))
            if hasattr(self, "_sweep_status_lbl"):
                self._sweep_status_lbl.config(fg=C_STYLE.get("warning", "#E8A000"))
        elif scale == "custom" and tier == "custom":
            status = self.tr("sweep.preset_status.full_custom")
            if hasattr(self, "_sweep_status_lbl"):
                self._sweep_status_lbl.config(fg=C_STYLE.get("text_muted", "#888"))
        else:
            status = self.tr("sweep.preset_status.applied").format(
                scale=self.tr(f"sweep.scale.{scale}"),
                tier=self.tr(f"sweep.tier.{tier}"))
            if hasattr(self, "_sweep_status_lbl"):
                self._sweep_status_lbl.config(fg=C_STYLE.get("text_accent", "#4A90E2"))
        self._sweep_preset_status_var.set(status)

    def _apply_sweep_preset(self):
        """Legacy method stub — kept for backward compatibility only."""
        pass  # old single-dropdown preset logic removed; replaced by _on_sweep_scale_tier_change

    def _refresh_sweep_scale_tier_i18n(self):
        """Refresh all sweep scale/tier UI text after a language switch (i18n callback)."""
        scale_code = self._sweep_scale_var.get() if hasattr(self, "_sweep_scale_var") else "medium"
        tier_code  = self._sweep_tier_var.get()  if hasattr(self, "_sweep_tier_var")  else "formal"

        # Rebuild combo values in new language, re-select current index
        scale_vals = [self.tr(f"sweep.scale.{c}") for c in _SCALE_CODES]
        tier_vals  = [self.tr(f"sweep.tier.{c}")  for c in _TIER_CODES]
        if hasattr(self, "_scale_combo"):
            self._scale_combo.configure(values=scale_vals)
            try:
                self._scale_combo.current(_SCALE_CODES.index(scale_code))
            except ValueError:
                self._scale_combo.current(1)  # fallback: medium
        if hasattr(self, "_tier_combo"):
            self._tier_combo.configure(values=tier_vals)
            try:
                self._tier_combo.current(_TIER_CODES.index(tier_code))
            except ValueError:
                self._tier_combo.current(1)  # fallback: formal

        # Update description label
        if hasattr(self, "_sweep_desc_var"):
            self._sweep_desc_var.set(self.tr(f"sweep.scale.desc.{scale_code}"))

        # Update tier formula label (only shown when tier != custom)
        if hasattr(self, "_sweep_tier_formula_var") and tier_code != "custom":
            formula = SWEEP_TIER_DEFS.get(tier_code, {}).get("formula", "")
            self._sweep_tier_formula_var.set(self.tr("sweep.tier_formula_prefix") + formula)

        # Update custom rule radio button labels
        for rb, i18n_key in getattr(self, "_sweep_custom_rule_rbs", []):
            try:
                rb.configure(text=self.tr(i18n_key))
            except Exception:
                pass

        # Update status label
        self._update_sweep_preset_status_label()

    def _parse_concurrency_levels(self, text: str) -> list[int]:
        """Parse comma-separated concurrency levels. Returns sorted unique ints.
        Supports up to C1024. Raises ValueError for invalid input."""
        parts = [p.strip() for p in text.split(",") if p.strip()]
        if not parts:
            raise ValueError("并发级别不能为空")
        levels = []
        for p in parts:
            try:
                v = int(p)
            except ValueError:
                raise ValueError(f"无效的并发值: '{p}'，请输入逗号分隔的整数，例如: 1,5,10,20,40")
            if v < 1:
                raise ValueError(f"并发数必须大于 0，收到: {v}")
            if v > 1024:
                raise ValueError(f"并发数不能超过 1024，收到: {v}")
            levels.append(v)
        # Sort and deduplicate
        levels = sorted(set(levels))
        if not levels:
            raise ValueError("并发级别不能为空")
        return levels

    @staticmethod
    def _is_missing(value) -> bool:
        return value is None

    def _format_number(self, value, digits=3, default="N/A") -> str:
        if self._is_missing(value):
            return default
        try:
            return f"{float(value):.{digits}f}"
        except Exception:
            return default

    def _format_seconds(self, value, digits=3, default="N/A") -> str:
        if self._is_missing(value):
            return default
        try:
            return f"{float(value):.{digits}f}s"
        except Exception:
            return default

    def _row_get(self, row, key, default=None):
        """Safely read a field from either a sqlite3.Row or a dict row."""
        if row is None:
            return default
        if isinstance(row, dict):
            return row.get(key, default)
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return default

    def _detail_truncation_warning(self, concurrency, detail_count, success):
        if self.lang_code == "en_US":
            return (
                f"Detail records truncated at C{concurrency}: "
                f"detail_count={detail_count} / success={success}. "
                "Summary metrics remain usable, but detail should not be treated "
                "as complete per-request samples.")
        return (
            f"明细记录已截断：C{concurrency} detail_count={detail_count} / success={success}。"
            "summary 汇总指标仍可使用，但 detail 不适合视为完整单请求样本。")

    def _detect_detail_truncation_notes(self, cases: list[dict]) -> list[str]:
        notes = []
        for case in cases:
            summary = case.get("benchmark_summary", {})
            metrics = case.get("analysis_metrics", {})
            success = summary.get("success", case.get("success"))
            detail_count = metrics.get("detail_count", len(summary.get("detail") or []))
            detail_truncated = metrics.get("detail_truncated")
            if detail_truncated is None:
                detail_truncated = success is not None and detail_count < success
            if detail_truncated and success is not None:
                notes.append(self._detail_truncation_warning(
                    case.get("concurrency"), detail_count, success))
        return notes

    def _detect_non_monotonic_sweep_anomalies(self, cases: list[dict]) -> list[dict]:
        anomalies = []
        metrics = [
            ("e2e_p95", "E2E P95"),
            ("ttft_p95", "TTFT P95"),
            ("e2e_p99", "E2E P99"),
        ]
        for i, lower in enumerate(cases):
            lower_c = lower.get("concurrency")
            lower_m = lower.get("analysis_metrics", {})
            for higher in cases[i + 1:]:
                higher_c = higher.get("concurrency")
                higher_m = higher.get("analysis_metrics", {})
                if higher_c is None or lower_c is None or higher_c <= lower_c:
                    continue
                for key, label in metrics:
                    lower_v = lower_m.get(key)
                    higher_v = higher_m.get(key)
                    if lower_v is None or higher_v is None or higher_v <= 0:
                        continue
                    if lower_v > higher_v * 1.3:
                        anomalies.append({
                            "type": "non_monotonic_latency",
                            "metric": key,
                            "metric_label": label,
                            "lower_concurrency": lower_c,
                            "lower_value": lower_v,
                            "higher_concurrency": higher_c,
                            "higher_value": higher_v,
                        })
        # Keep the report concise: unique lower C / metric pairs, up to 5.
        deduped = []
        seen = set()
        for item in anomalies:
            sig = (item["lower_concurrency"], item["metric"])
            if sig in seen:
                continue
            seen.add(sig)
            deduped.append(item)
            if len(deduped) >= 5:
                break
        return deduped

    def _format_non_monotonic_anomaly(self, anomaly: dict) -> str:
        label = anomaly["metric_label"]
        low_c = anomaly["lower_concurrency"]
        high_c = anomaly["higher_concurrency"]
        low_v = anomaly["lower_value"]
        high_v = anomaly["higher_value"]
        if self.lang_code == "en_US":
            return (
                f"Non-monotonic tail latency detected: C{low_c} {label} "
                f"({low_v:.3f}s) is higher than C{high_c} ({high_v:.3f}s). "
                f"Retest C{low_c} before including it in recommendations.")
        return (
            f"检测到非单调长尾：C{low_c} 的 {label} ({low_v:.3f}s) "
            f"高于更高并发 C{high_c} ({high_v:.3f}s)，建议复测该档位。")

    def _recommend_sweep_concurrency_range(self, cases: list[dict]) -> dict:
        if not cases:
            return {
                "interactive_range": None,
                "balanced_candidate": None,
                "throughput_stress_point": None,
                "notes": ["需要更多数据或降低并发重新测试"],
            }

        anomalies = self._detect_non_monotonic_sweep_anomalies(cases)
        anomaly_concs = {a["lower_concurrency"] for a in anomalies}
        candidates = []
        for case in cases:
            c = case.get("concurrency")
            m = case.get("analysis_metrics", {})
            success_rate = m.get("success_rate")
            e2e_p95 = m.get("e2e_p95")
            ttft_p95 = m.get("ttft_p95")
            eff = m.get("throughput_efficiency")
            if success_rate is None or e2e_p95 is None or eff is None:
                continue
            ttft_ok = True if ttft_p95 is None else ttft_p95 <= 2.0
            if (success_rate >= 99 and e2e_p95 <= 15.0 and ttft_ok
                    and eff >= 0.25 and c not in anomaly_concs):
                candidates.append(case)

        balanced = None
        if candidates:
            balanced = max(candidates, key=lambda case: (
                case.get("analysis_metrics", {}).get("output_tps") or 0))
            balanced_tps = balanced.get("analysis_metrics", {}).get("output_tps") or 0
            if balanced_tps > 0:
                candidates = [
                    case for case in candidates
                    if (case.get("analysis_metrics", {}).get("output_tps") or 0) >= balanced_tps * 0.5
                ] or [balanced]

        max_tps_case = max(cases, key=lambda case: (
            case.get("analysis_metrics", {}).get("output_tps") or 0))
        max_m = max_tps_case.get("analysis_metrics", {})
        stress_only = (
            (max_m.get("e2e_p95") is not None and max_m.get("e2e_p95") > 20.0)
            or (max_m.get("ttft_p95") is not None and max_m.get("ttft_p95") > 5.0)
        )

        notes = []
        if candidates:
            concs = [c["concurrency"] for c in candidates]
            if self.lang_code == "en_US":
                if len(concs) == 1:
                    notes.append(f"C{concs[0]} is a better interactive serving candidate.")
                else:
                    notes.append(f"C{min(concs)}-C{max(concs)} is a better interactive serving candidate range.")
                notes.append(
                    f"C{balanced['concurrency']} is the balanced candidate with higher output TPS inside the interactive thresholds.")
            else:
                if len(concs) == 1:
                    notes.append(f"C{concs[0]} 更适合作为交互服务候选并发。")
                else:
                    notes.append(f"C{min(concs)}-C{max(concs)} 更适合作为交互服务候选区间。")
                notes.append(
                    f"平衡候选并发为 C{balanced['concurrency']}，在交互阈值内输出吞吐相对更高。")
        else:
            notes.append(
                "No interactive candidate satisfies success rate, E2E P95, TTFT P95, and throughput efficiency thresholds."
                if self.lang_code == "en_US"
                else "未找到同时满足成功率、E2E P95、TTFT P95 与吞吐效率阈值的交互候选。")

        if stress_only:
            if self.lang_code == "en_US":
                notes.append(
                    f"C{max_tps_case['concurrency']} is the current maximum throughput point, "
                    "but E2E/TTFT tail latency is high. Treat it as a throughput stress or batch point, "
                    "not the default interactive concurrency.")
            else:
                notes.append(
                    f"C{max_tps_case['concurrency']} 为当前最大吞吐点，但 E2E/TTFT 长尾较高，"
                    "更适合吞吐压榨或批处理，不建议作为交互默认并发。")
        else:
            notes.append(
                f"C{max_tps_case['concurrency']} is the current maximum throughput point."
                if self.lang_code == "en_US"
                else f"C{max_tps_case['concurrency']} 为当前最大吞吐点。")

        if anomaly_concs:
            conc_text = ", ".join(f"C{c}" for c in sorted(anomaly_concs))
            notes.append(
                f"{conc_text} has non-monotonic tail latency. Retest before including it in recommendations."
                if self.lang_code == "en_US"
                else "、".join(f"C{c}" for c in sorted(anomaly_concs))
                + " 出现非单调长尾，建议复测后再纳入推荐范围。")

        return {
            "interactive_range": [min(c["concurrency"] for c in candidates),
                                  max(c["concurrency"] for c in candidates)] if candidates else None,
            "balanced_candidate": balanced["concurrency"] if balanced else None,
            "throughput_stress_point": max_tps_case["concurrency"] if stress_only else None,
            "max_tps_concurrency": max_tps_case["concurrency"],
            "notes": notes,
            "non_monotonic_anomalies": anomalies,
        }

    def _compute_analysis_metrics(self, summary: dict, baseline_output_tps: float,
                                   baseline_concurrency: int) -> dict:
        """Compute extracted analysis metrics from a benchmark summary.
        Returns a dict with all analysis_metrics fields."""
        e2e_avg = summary.get("e2e_latency_avg", 0) or None
        e2e_p50 = summary.get("e2e_latency_p50", 0) or None
        e2e_p95 = summary.get("e2e_latency_p95", 0) or None
        e2e_p99 = summary.get("e2e_latency_p99", 0) or None
        ttft_avg = summary.get("ttft_avg", None)
        ttft_p95 = summary.get("ttft_p95", None)
        first_visible_token_avg = summary.get("first_visible_token_avg", None)
        first_visible_gap_avg = summary.get("first_visible_gap_avg", None)
        first_visible_gap_p50 = summary.get("first_visible_gap_p50", None)
        first_visible_gap_p95 = summary.get("first_visible_gap_p95", None)
        first_visible_gap_p99 = summary.get("first_visible_gap_p99", None)
        tpot_avg = summary.get("tpot_avg", None)
        itl_avg = summary.get("itl_avg", None)
        output_tps = summary.get("system_output_tps", 0) or None
        total_tps = summary.get("system_total_tps", 0) or None
        rps = summary.get("request_throughput_rps", 0) or None
        success_rate = summary.get("success_rate", None)
        per_request_output_tps = summary.get("per_request_output_tps_avg", 0) or None

        # Throughput efficiency
        concurrency = summary.get("concurrency", baseline_concurrency)
        if baseline_output_tps and baseline_output_tps > 0 and concurrency > 0:
            throughput_efficiency = output_tps / (baseline_output_tps * concurrency) if output_tps else None
        else:
            throughput_efficiency = None
        detail_count = len(summary.get("detail") or [])
        success = summary.get("success")
        detail_truncated = success is not None and detail_count < success

        return {
            "e2e_avg": e2e_avg,
            "e2e_p50": e2e_p50,
            "e2e_p95": e2e_p95,
            "e2e_p99": e2e_p99,
            "ttft_avg": ttft_avg,
            "ttft_p95": ttft_p95,
            "first_visible_token_avg": first_visible_token_avg,
            "first_visible_gap_avg": first_visible_gap_avg,
            "first_visible_gap_p50": first_visible_gap_p50,
            "first_visible_gap_p95": first_visible_gap_p95,
            "first_visible_gap_p99": first_visible_gap_p99,
            "tpot_avg": tpot_avg,
            "itl_avg": itl_avg,
            "output_tps": output_tps,
            "total_tps": total_tps,
            "rps": rps,
            "success_rate": success_rate,
            "per_request_output_tps": per_request_output_tps,
            "throughput_efficiency": throughput_efficiency,
            "detail_count": detail_count,
            "detail_truncated": detail_truncated,
            "detail_truncation_note": self._detail_truncation_warning(
                concurrency, detail_count, success) if detail_truncated else "",
        }

    def _generate_analysis_summary(self, cases: list[dict]) -> list[str]:
        """Generate automatic text analysis based on sweep results.
        Returns a list of Chinese analysis strings."""
        if len(cases) < 2:
            return ["数据点不足，无法判断吞吐拐点。"]

        lines = []
        conc = [c["concurrency"] for c in cases]
        s = [c["benchmark_summary"] for c in cases]

        # 1. Max output_tps concurrency
        tps_vals = [(i, cs.get("system_output_tps", 0)) for i, cs in enumerate(s)]
        max_tps = max(tps_vals, key=lambda x: x[1])
        if max_tps[1] > 0:
            lines.append(
                f"最大输出吞吐出现在并发={conc[max_tps[0]]}，"
                f"Output TPS = {max_tps[1]:.1f} tok/s。")

        # 2. Output TPS growth slowdown
        if len(cases) >= 3:
            tps_seq = [cs.get("system_output_tps", 0) for cs in s]
            growth_rates = []
            for i in range(1, len(tps_seq)):
                if tps_seq[i - 1] > 0:
                    growth_rates.append(tps_seq[i] / tps_seq[i - 1])
            if growth_rates:
                # Check if growth is slowing
                slowdown = all(
                    growth_rates[j] < growth_rates[j - 1] * 0.95
                    for j in range(1, len(growth_rates))
                )
                if slowdown and len(growth_rates) >= 2:
                    lines.append(
                        "输出吞吐增速持续放缓，可能已接近服务端吞吐上限。")
                elif growth_rates[-1] < 1.05 and len(growth_rates) >= 2:
                    lines.append(
                        f"并发从 {conc[-2]} 增加到 {conc[-1]} 时吞吐几乎不再增长，"
                        f"拐点约在并发={conc[-2]}。")

        # 3. E2E P95/P99 growth
        p95_vals = [cs.get("e2e_latency_p95", 0) for cs in s]
        p99_vals = [cs.get("e2e_latency_p99", 0) for cs in s]
        if len(p95_vals) >= 2 and p95_vals[0] > 0:
            p95_growth = p95_vals[-1] / p95_vals[0]
            if p95_growth > 3:
                lines.append(
                    f"E2E P95 从 {p95_vals[0]:.2f}s 增长到 {p95_vals[-1]:.2f}s "
                    f"（{p95_growth:.1f}x），高并发下延迟长尾明显。")
            elif p95_growth > 1.5:
                lines.append(
                    f"E2E P95 增长 {p95_growth:.1f}x，高并发下存在一定的排队延迟。")
        if len(p99_vals) >= 2 and p99_vals[0] > 0:
            p99_growth = p99_vals[-1] / p99_vals[0]
            if p99_growth > 5:
                lines.append(
                    f"E2E P99 增长 {p99_growth:.1f}x，极端延迟大幅恶化，"
                    f"建议降低并发或检查服务端队列配置。")

        # 4. TPOT/ITL stability
        tpot_vals = [cs.get("tpot_avg", 0) or 0 for cs in s]
        itl_vals = [cs.get("itl_avg", 0) or 0 for cs in s]
        valid_tpot = [v for v in tpot_vals if v > 0]
        valid_itl = [v for v in itl_vals if v > 0]
        if valid_tpot:
            tpot_range = max(valid_tpot) / min(valid_tpot) if min(valid_tpot) > 0 else 1
            if tpot_range > 2.0:
                lines.append(
                    f"TPOT 随并发恶化 {tpot_range:.1f}x，"
                    f"单 Token 生成速度在高并发下明显下降。")
            elif tpot_range > 1.3:
                lines.append(f"TPOT 略有上升（{tpot_range:.1f}x），生成速度轻度受影响。")
            else:
                lines.append("TPOT 随并发保持稳定，单 Token 生成速度未受影响。")
        if valid_itl:
            itl_range = max(valid_itl) / min(valid_itl) if min(valid_itl) > 0 else 1
            if itl_range > 2.0:
                lines.append(f"ITL 随并发恶化 {itl_range:.1f}x，Token 间隔显著增大。")
            elif itl_range <= 1.3:
                lines.append("ITL 保持稳定，Token 流式输出间隔未恶化。")

        # 5. First Visible Gap
        gap_vals = [cs.get("first_visible_gap_avg", 0) or 0 for cs in s]
        if any(v > 0.5 for v in gap_vals):
            max_gap = max(gap_vals)
            lines.append(
                f"首包到首字间隔 (First Visible Gap) 最大 {max_gap:.3f}s (>0.5s)，"
                f"服务端已较早开始流式响应，但首个可见输出较晚。")

        # 6. Detail truncation, non-monotonic anomalies, and recommendation.
        for note in self._detect_detail_truncation_notes(cases):
            lines.append(note)

        recommendation = self._recommend_sweep_concurrency_range(cases)
        for anomaly in recommendation.get("non_monotonic_anomalies", []):
            lines.append(self._format_non_monotonic_anomaly(anomaly))
        lines.extend(recommendation.get("notes", []))

        if not lines:
            lines.append("数据点不足，无法判断吞吐拐点。")
        return lines

    def _generate_expert_commentary(self, cases: list[dict],
                                     analysis_summary: list[str]) -> str:
        """Generate a 150-300 character professional expert commentary.
        Based on data, restrained tone, no absolute claims."""
        if len(cases) < 2:
            return ("本轮仅测试单一并发档位，无法进行趋势判断。"
                    "建议至少使用 3 档并发（如 1/5/20）进行复测，"
                    "以获得可靠的吞吐-延迟关系。")

        s = [c["benchmark_summary"] for c in cases]
        conc = [c["concurrency"] for c in cases]

        # Build commentary from actual data
        parts = []

        # Overall trend
        tps_first = s[0].get("system_output_tps", 0) or 0
        tps_last = s[-1].get("system_output_tps", 0) or 0
        e2e_first = s[0].get("e2e_latency_avg", 0) or 0
        e2e_last = s[-1].get("e2e_latency_avg", 0) or 0

        if tps_last > tps_first * 1.05:
            parts.append(
                f"从本轮并发扫测看，系统吞吐随并发提升整体呈上升趋势"
                f"（{tps_first:.0f} → {tps_last:.0f} tok/s）。")
        else:
            parts.append("本轮扫测中系统吞吐未随并发明显提升，可能已接近当前配置下的有效吞吐上限。")

        # Slowdown detection
        if len(cases) >= 3:
            tps_vals = [cs.get("system_output_tps", 0) or 0 for cs in s]
            last_growth = tps_vals[-1] / max(tps_vals[-2], 1)
            if last_growth < 1.05:
                parts.append(
                    f"在 C{conc[-2]}→C{conc[-1]} 区间吞吐已近停滞，"
                    f"说明服务端可能逐步接近当前配置下的有效吞吐区间。")

        # Latency analysis
        e2e_p95_first = s[0].get("e2e_latency_p95", 0) or 0
        e2e_p95_last = s[-1].get("e2e_latency_p95", 0) or 0
        e2e_p99_first = s[0].get("e2e_latency_p99", 0) or 0
        e2e_p99_last = s[-1].get("e2e_latency_p99", 0) or 0

        if e2e_p95_first > 0 and e2e_p95_last > e2e_p95_first * 1.5:
            parts.append(
                f"E2E P95/P99 的增长幅度高于平均延迟，"
                f"说明高并发下已经出现一定长尾。")

        # TPOT/ITL stability
        tpot_first = s[0].get("tpot_avg", 0) or 0
        tpot_last = s[-1].get("tpot_avg", 0) or 0
        itl_first = s[0].get("itl_avg", 0) or 0
        itl_last = s[-1].get("itl_avg", 0) or 0

        if tpot_first > 0 and tpot_last > 0:
            tpot_ratio = tpot_last / tpot_first
            if tpot_ratio <= 1.3:
                parts.append(
                    "TPOT 与 ITL 整体保持稳定，"
                    "说明 decode 阶段本身仍较稳定，主要压力更可能来自排队、prefill 或调度侧。")
            elif tpot_ratio <= 2.0:
                parts.append(
                    f"TPOT 随并发略有上升（{tpot_ratio:.1f}x），"
                    f"decode 阶段开始受到一定影响，但尚未成为主要瓶颈。")
            else:
                parts.append(
                    f"TPOT 随并发明显恶化（{tpot_ratio:.1f}x），"
                    f"decode 阶段已受到较大压力。")

        # First Visible Gap
        gap_vals = [cs.get("first_visible_gap_avg", 0) or 0 for cs in s]
        if any(v > 0.5 for v in gap_vals):
            parts.append(
                "First Visible Gap 在高并发下扩大，"
                "用户首字体验可能先于总吞吐成为体验瓶颈。")

        recommendation = self._recommend_sweep_concurrency_range(cases)
        notes = recommendation.get("notes", [])
        if notes:
            parts.append("".join(notes[:3]))

        commentary = "".join(parts)
        # Truncate to ~300 chars
        if len(commentary) > 350:
            commentary = commentary[:347] + "..."
        return commentary

    def _generate_next_steps(self, cases: list[dict],
                              analysis_summary: list[str]) -> list[str]:
        """Generate 3-5 actionable next-step recommendations."""
        steps = []
        conc = [c["concurrency"] for c in cases]
        s = [c["benchmark_summary"] for c in cases]

        # 1. If only a few concurrency levels, suggest finer-grained sweep
        if len(conc) <= 3:
            steps.append(
                "建议增加更细并发档位（如 C8/C16/C32）复测，"
                "以便更精确地定位吞吐拐点和延迟转折点。")

        # 2. If failures exist
        has_failures = any(cs.get("fail", 0) > 0 for cs in s)
        if has_failures:
            steps.append(
                "存在失败请求，建议优先排查 timeout 配置、HTTP 错误码和 vLLM 服务日志。")

        # 3. Fix prompt/output length
        steps.append(
            "建议固定 prompt 和输出长度（如设置 max_tokens 并验证实际输出一致性），"
            "减少随机输出长度对 E2E 延迟的干扰。")

        # 4. Repeat near saturation point
        tps_vals = [cs.get("system_output_tps", 0) or 0 for cs in s]
        if len(tps_vals) >= 3:
            # Find where growth starts slowing
            growth = [tps_vals[i] / max(tps_vals[i - 1], 1)
                      for i in range(1, len(tps_vals))]
            slow_idx = next((i for i, g in enumerate(growth) if g < 1.1), -1)
            if slow_idx >= 0:
                steps.append(
                    f"吞吐拐点疑似在 C{conc[slow_idx]}-C{conc[slow_idx + 1]} 附近，"
                    "建议对该区间重复测试 3 轮以确认重复性。")

        # 5. First Visible Gap investigation
        gap_vals = [cs.get("first_visible_gap_avg", 0) or 0 for cs in s]
        if any(v > 0.5 for v in gap_vals):
            steps.append(
                "建议关注 First Visible Gap 是否由模型思考输出、"
                "模板前缀、流式 chunk 聚合行为或请求调度导致，"
                "可通过对比不同 prompt 模板进一步缩小原因。")

        # 6. If throughput efficiency drops significantly
        if len(s) >= 2:
            tps_first = s[0].get("system_output_tps", 0) or 0
            tps_last = s[-1].get("system_output_tps", 0) or 0
            c_last = conc[-1]
            if tps_first > 0 and c_last > 0:
                eff = tps_last / (tps_first * c_last)
                if eff < 0.3:
                    steps.append(
                        f"Throughput Efficiency 已降至 {eff:.2f}，"
                        "建议检查服务端 --max-num-seqs / --max-model-len 等并发限制参数。")

        # Limit to 5 steps
        return steps[:5]

    def _generate_sweep_markdown_report(self, sweep_result: dict) -> str:
        """Generate a full 8-section Markdown analysis report.
        Returns the report content as a string."""
        cases = sweep_result.get("cases", [])
        analysis_summary = sweep_result.get("analysis_summary", [])
        concurrency_levels = sweep_result.get("concurrency_levels", [])

        if not cases:
            empty = "数据为空，无法生成报告。" if self.lang_code == "zh_CN" else "No data. Report cannot be generated."
            return f"# {self.tr('chart.sweep_title')}\n\n{empty}\n"

        s = [c["benchmark_summary"] for c in cases]
        conc = [c["concurrency"] for c in cases]
        stream_mode = s[0].get("stream_mode", False) if s else False

        # Helper: safe format. 0.0 is a valid measurement and must not become N/A.
        def _f(v, fmt=".2f", default="N/A"):
            if v is None:
                return default
            try:
                return f"{v:{fmt}}"
            except Exception:
                return default

        def _fs(v, digits=3, default="N/A"):
            return self._format_seconds(v, digits=digits, default=default)

        def _fmt_list(vals, fmt=".2f", default="N/A"):
            formatted = []
            for v in vals:
                if v is None:
                    formatted.append(default)
                else:
                    try:
                        formatted.append(f"{v:{fmt}}")
                    except Exception:
                        formatted.append(default)
            return formatted

        # ── Build report ──
        md = []
        headings = {
            "overview": "一、测试概览" if self.lang_code == "zh_CN" else "1. Test Overview",
            "conclusion": "二、核心结论" if self.lang_code == "zh_CN" else "2. Key Conclusions",
            "latency": "三、延迟分析" if self.lang_code == "zh_CN" else "3. Latency Analysis",
            "throughput": "四、吞吐分析" if self.lang_code == "zh_CN" else "4. Throughput Analysis",
            "first": "五、首包 / 首字 / 生成速度分析" if self.lang_code == "zh_CN" else "5. First Chunk / First Visible Token / Generation Speed",
            "stability": "六、稳定性分析" if self.lang_code == "zh_CN" else "6. Stability Analysis",
            "expert": "七、专家简评" if self.lang_code == "zh_CN" else "7. Expert Summary",
            "next": "八、下一步建议" if self.lang_code == "zh_CN" else "8. Next Steps",
        }
        md.append(f"# {self.tr('chart.sweep_title')}")
        md.append("")

        # ── 一、测试概览 ──
        md.append(f"## {headings['overview']}")
        md.append("")
        md.append(f"- **API URL**: `{sweep_result.get('api_url', 'N/A')}`")
        md.append(f"- **Model**: `{sweep_result.get('model', 'N/A')}`")
        md.append(f"- **并发档位**: {concurrency_levels}")
        # ── Sweep Scale / Tier metadata (new fields) ──
        _rpt_scale = sweep_result.get("sweep_scale")
        _rpt_tier  = sweep_result.get("sweep_tier")
        if _rpt_scale:
            _rpt_scale_lbl = sweep_result.get("sweep_scale_label",
                                              SWEEP_SCALE_LABELS_ZH.get(_rpt_scale, _rpt_scale))
            md.append(f"- **扫测规模 (Sweep Scale)**: {_rpt_scale_lbl} (`{_rpt_scale}`)")
        if _rpt_tier:
            _rpt_tier_lbl = sweep_result.get("sweep_tier_label",
                                             SWEEP_TIER_LABELS_ZH.get(_rpt_tier, _rpt_tier))
            md.append(f"- **扫测档次 (Sweep Tier)**: {_rpt_tier_lbl} (`{_rpt_tier}`)")
        _rpt_modified = sweep_result.get("preset_modified", False)
        if _rpt_modified:
            _src_s = sweep_result.get("source_preset_scale") or ""
            _src_t = sweep_result.get("source_preset_tier")  or ""
            _src_s_lbl = SWEEP_SCALE_LABELS_ZH.get(_src_s, _src_s)
            _src_t_lbl = SWEEP_TIER_LABELS_ZH.get(_src_t,  _src_t)
            md.append(f"- **预设已修改 (Preset Modified)**: Yes")
            if _src_s or _src_t:
                md.append(f"- **来源预设 (Source Preset)**: {_src_s_lbl} + {_src_t_lbl}")
        else:
            if _rpt_scale or _rpt_tier:
                md.append(f"- **预设已修改 (Preset Modified)**: No")
        _rpt_rule = sweep_result.get("request_count_rule")
        if _rpt_rule:
            _rpt_rule_type = sweep_result.get("request_count_rule_type", "")
            md.append(f"- **请求数规则 (Request Count Rule)**: `{_rpt_rule}`  _{_rpt_rule_type}_")
        _rpt_req_map = sweep_result.get("request_counts_by_concurrency")
        if _rpt_req_map:
            _rpt_req_str = "  ".join(f"C{c}={n}" for c, n in sorted(
                _rpt_req_map.items(), key=lambda kv: int(kv[0])))
            md.append(f"- **各档位请求数**: {_rpt_req_str}")
        else:
            md.append(f"- **每档请求规则**: 总请求数 = 并发数 × {sweep_result.get('requests_multiplier', 10)}")
        md.append(f"- **Max Tokens**: {s[0].get('max_tokens', 'N/A')}")
        output_mode = sweep_result.get("output_length_mode", s[0].get("output_length_mode", "normal"))
        output_mode_label = (
            self.tr("label.output_mode_fixed_short")
            if output_mode == "fixed"
            else self.tr("label.output_mode_normal_short")
        )
        md.append(f"- **{self.tr('label.output_length_mode')}**: {output_mode_label}")
        if output_mode == "fixed":
            fixed_tokens = sweep_result.get("fixed_output_tokens", s[0].get("fixed_output_tokens", "N/A"))
            min_tokens_sent = sweep_result.get("min_tokens_sent", s[0].get("min_tokens_sent", "N/A"))
            ignore_eos = sweep_result.get("ignore_eos", s[0].get("ignore_eos", False))
            md.append(f"- **{self.tr('label.fixed_output_tokens')}**: {fixed_tokens}")
            md.append(f"- **min_tokens_sent**: {min_tokens_sent}")
            md.append(f"- **ignore_eos**: {str(ignore_eos).lower()}")
        md.append(f"- **Temperature**: {s[0].get('temperature', 'N/A')}")
        md.append(f"- **Stream Mode**: {'是 (stream=True)' if stream_mode else '否 (stream=False)'}")
        md.append(f"- **测试开始**: {sweep_result.get('started_at', 'N/A')}")
        md.append(f"- **测试结束**: {sweep_result.get('finished_at', 'N/A')}")

        # Sample size warning
        min_req = min(c.get("total_requests", 0) for c in cases) if cases else 0
        if min_req < 5:
            md.append("")
            md.append("> ⚠ **注意**: 部分并发档位请求数较少（<5），样本量偏小，结论仅供快速参考。")
        if len(cases) < 2:
            md.append("")
            md.append("> ⚠ **注意**: 仅测试了单一并发档位，数据点不足，无法进行趋势判断。")
        detail_notes = self._detect_detail_truncation_notes(cases)
        for note in detail_notes:
            md.append("")
            md.append(f"> ⚠ **Detail Truncation**: {note}")
        if output_mode == "fixed" and any(
            cs.get("fixed_output_validation_passed") is False for cs in s
        ):
            md.append("")
            md.append(f"> ⚠ **Fixed Output**: {self.tr('label.fixed_output_validation_failed')}")
        md.append("")

        # ── 二、核心结论 ──
        md.append(f"## {headings['conclusion']}")
        md.append("")
        for line in analysis_summary:
            md.append(f"- {line}")
        md.append("")

        # ── 三、延迟分析 ──
        md.append(f"## {headings['latency']}")
        md.append("")
        if len(cases) < 2:
            md.append("数据点不足，无法判断明确趋势。")
        else:
            # Build table
            md.append("| 并发 | E2E Avg (s) | E2E P50 (s) | E2E P95 (s) | E2E P99 (s) |")
            md.append("|------|-------------|-------------|-------------|-------------|")
            for i, c_val in enumerate(conc):
                e2e_avg = _fs(s[i].get("e2e_latency_avg"), 3)
                e2e_p50 = _fs(s[i].get("e2e_latency_p50"), 3)
                e2e_p95 = _fs(s[i].get("e2e_latency_p95"), 3)
                e2e_p99 = _fs(s[i].get("e2e_latency_p99"), 3)
                md.append(f"| {c_val} | {e2e_avg} | {e2e_p50} | {e2e_p95} | {e2e_p99} |")
            md.append("")

            # Analysis text
            e2e_p50_vals = [cs.get("e2e_latency_p50", 0) or 0 for cs in s]
            e2e_p99_vals = [cs.get("e2e_latency_p99", 0) or 0 for cs in s]
            e2e_p95_vals = [cs.get("e2e_latency_p95", 0) or 0 for cs in s]

            if e2e_p50_vals[-1] > 0 and e2e_p99_vals[-1] > 0:
                tail_ratio = e2e_p99_vals[-1] / e2e_p50_vals[-1] if e2e_p50_vals[-1] > 0 else 0
                if tail_ratio > 2.0:
                    md.append(f"P99/P50 比值达 {tail_ratio:.1f}x，高并发下长尾延迟明显。")
                p95_ratio = e2e_p95_vals[-1] / e2e_p50_vals[-1] if e2e_p50_vals[-1] > 0 else 0
                if p95_ratio > 1.5:
                    md.append(f"P95/P50 比值达 {p95_ratio:.1f}x，P95 延迟已有扩散。")

            # Decode vs queuing
            tpot_first = s[0].get("tpot_avg", 0) or 0
            tpot_last = s[-1].get("tpot_avg", 0) or 0
            if tpot_first > 0 and tpot_last > 0:
                ratio = tpot_last / tpot_first
                if ratio <= 1.3:
                    md.append("TPOT/ITL 保持稳定，说明 decode 阶段本身未明显恶化，高并发下延迟增长主要来自排队或调度。")
                else:
                    md.append(f"TPOT 从 {tpot_first:.3f}s 增长到 {tpot_last:.3f}s（{ratio:.1f}x），decode 阶段开始受到压力。")
            else:
                md.append("TPOT 数据不可用（非流式模式或数据不足），无法判断 decode 阶段是否受压。")
        md.append("")

        # ── 四、吞吐分析 ──
        md.append(f"## {headings['throughput']}")
        md.append("")
        if len(cases) < 2:
            md.append("数据点不足，无法判断吞吐趋势。")
        else:
            md.append("| 并发 | Output TPS (tok/s) | Total TPS (tok/s) | RPS (req/s) | Per-req TPS |")
            md.append("|------|---------------------|-------------------|-------------|-------------|")
            for i, c_val in enumerate(conc):
                otps = _f(s[i].get("system_output_tps"), ".1f")
                ttps = _f(s[i].get("system_total_tps"), ".1f")
                rrps = _f(s[i].get("request_throughput_rps"), ".2f")
                prtps = _f(s[i].get("per_request_output_tps_avg"), ".1f")
                md.append(f"| {c_val} | {otps} | {ttps} | {rrps} | {prtps} |")
            md.append("")

            # Max TPS
            tps_vals = [(i, cs.get("system_output_tps", 0) or 0) for i, cs in enumerate(s)]
            max_idx, max_tps = max(tps_vals, key=lambda x: x[1])
            if max_tps > 0:
                md.append(f"最大输出吞吐出现在 C{conc[max_idx]}，Output TPS = {max_tps:.1f} tok/s。")

            # Growth analysis
            if len(cases) >= 3:
                growth_ok = True
                for i in range(1, len(conc)):
                    tps_g = (s[i].get("system_output_tps", 0) or 0) / max((s[i - 1].get("system_output_tps", 0) or 1), 1)
                    conc_g = conc[i] / conc[i - 1] if conc[i - 1] > 0 else 1
                    if tps_g < conc_g * 0.5:
                        md.append(f"C{conc[i - 1]}→C{conc[i]}：吞吐增长效率明显下降（TPS {tps_g:.2f}x vs 并发 {conc_g:.1f}x），疑似接近拐点。")
                        growth_ok = False
                        break
                if growth_ok and all(
                    s[i].get("system_output_tps", 0) or 0 > (s[i - 1].get("system_output_tps", 0) or 0) * 1.05
                    for i in range(1, len(s))
                ):
                    md.append("吞吐随并发持续增长，尚未进入明显平台期。")
        md.append("")

        # ── 五、首包/首字/生成速度分析 ──
        md.append(f"## {headings['first']}")
        md.append("")
        if not stream_mode:
            md.append("非流式模式，TTFT/TPOT/ITL 数据不可用。")
        else:
            md.append("| 并发 | TTFT (s) | FVT (s) | FVG Avg (s) | FVG P50 (s) | FVG P95 (s) | FVG P99 (s) | TPOT (s) | ITL (s) |")
            md.append("|------|----------|---------|-------------|-------------|-------------|-------------|----------|---------|")
            for i, c_val in enumerate(conc):
                ttft = _fs(s[i].get("ttft_avg"), 3)
                fvt = _fs(s[i].get("first_visible_token_avg"), 3)
                fvg = _fs(s[i].get("first_visible_gap_avg"), 3)
                fvg_p50 = _fs(s[i].get("first_visible_gap_p50"), 3)
                fvg_p95 = _fs(s[i].get("first_visible_gap_p95"), 3)
                fvg_p99 = _fs(s[i].get("first_visible_gap_p99"), 3)
                tpot = _fs(s[i].get("tpot_avg"), 3)
                itl = _fs(s[i].get("itl_avg"), 3)
                md.append(f"| {c_val} | {ttft} | {fvt} | {fvg} | {fvg_p50} | {fvg_p95} | {fvg_p99} | {tpot} | {itl} |")
            md.append("")

            # TTFT stability
            ttft_vals = [cs.get("ttft_avg", 0) or 0 for cs in s]
            valid_ttft = [v for v in ttft_vals if v > 0]
            if valid_ttft:
                ttft_range = max(valid_ttft) / min(valid_ttft) if min(valid_ttft) > 0 else 1
                if ttft_range > 2.0:
                    md.append(f"TTFT 随并发恶化 {ttft_range:.1f}x，首包延迟在高并发下显著上升。")
                elif ttft_range > 1.3:
                    md.append(f"TTFT 略有上升（{ttft_range:.1f}x），首包延迟轻度受影响。")
                else:
                    md.append("TTFT 随并发保持稳定。")

            # First Visible Gap
            fvg_vals = [cs.get("first_visible_gap_avg", 0) or 0 for cs in s]
            if any(v > 0.5 for v in fvg_vals):
                md.append("First Visible Gap > 0.5s，首字延迟需要关注。")
            fvg_p95 = [cs.get("first_visible_gap_p95", 0) or 0 for cs in s]
            if any(v > 2.0 for v in fvg_p95):
                md.append("First Visible Gap P95 > 2.0s，首字长尾可能影响交互体验。")

            # TPOT/ITL consistency
            tpot_vals = [cs.get("tpot_avg", 0) or 0 for cs in s]
            itl_vals = [cs.get("itl_avg", 0) or 0 for cs in s]
            if tpot_vals[-1] > 0 and itl_vals[-1] > 0:
                diff = abs(tpot_vals[-1] - itl_vals[-1]) / max(itl_vals[-1], 1e-9)
                if diff > 0.3:
                    md.append("TPOT 与 ITL 差异 >30%，请注意 chunk/token 口径差异。")
            if tpot_vals[-1] > 0 and tpot_vals[0] > 0:
                tpot_g = tpot_vals[-1] / tpot_vals[0]
                if tpot_g > 1.3:
                    md.append(f"TPOT 从最低并发到最高并发增长 {tpot_g:.1f}x，decode 阶段开始受压。")
                else:
                    md.append("TPOT 整体稳定，decode 阶段本身稳定，主要压力可能来自排队或调度。")
        md.append("")

        # ── 六、稳定性分析 ──
        md.append(f"## {headings['stability']}")
        md.append("")
        md.append("| 并发 | Success Rate (%) | Fail | RPS (req/s) | Throughput Efficiency |")
        md.append("|------|------------------|------|-------------|----------------------|")
        # Throughput efficiency
        baseline_tps = s[0].get("system_output_tps", 0) or 0 if s else 0
        for i, c_val in enumerate(conc):
            sr = _f(s[i].get("success_rate"), ".1f", "N/A")
            fail = s[i].get("fail", 0)
            rps = _f(s[i].get("request_throughput_rps"), ".2f")
            tps_val = s[i].get("system_output_tps", 0) or 0
            if baseline_tps > 0 and c_val > 0 and tps_val > 0:
                eff = tps_val / (baseline_tps * c_val)
                eff_s = f"{eff:.3f}"
            else:
                eff_s = "N/A"
            md.append(f"| {c_val} | {sr} | {fail} | {rps} | {eff_s} |")
        md.append("")

        has_failures = any(cs.get("fail", 0) > 0 for cs in s)
        if has_failures:
            total_fail = sum(cs.get("fail", 0) for cs in s)
            md.append(f"存在失败请求（共 {total_fail} 次），需排查 error_type 分布。")
        else:
            md.append("所有档位成功率均为 100%。")

        # Success rate warning
        if any((cs.get("success_rate") or 100) < 99 for cs in s):
            md.append("部分档位成功率 <99%，建议关注。")
        md.append("")

        # ── 七、专家简评 ──
        md.append(f"## {headings['expert']}")
        md.append("")
        commentary = self._generate_expert_commentary(cases, analysis_summary)
        md.append(commentary)
        md.append("")

        # ── 八、下一步建议 ──
        md.append(f"## {headings['next']}")
        md.append("")
        next_steps = self._generate_next_steps(cases, analysis_summary)
        for i, step in enumerate(next_steps, 1):
            md.append(f"{i}. {step}")
        md.append("")

        # ── ★ 客户验收指标 / Customer Acceptance Metrics ──
        try:
            from llm_benchmark_app.customer_metrics import (
                generate_customer_acceptance_section,
                OBJECTIVE_PEAK_THROUGHPUT, MODE_REAL_API,
            )
            _peak_s = sweep_result.get("peak_throughput_summary")
            _obj  = sweep_result.get("benchmark_objective", OBJECTIVE_PEAK_THROUGHPUT)
            _mode = sweep_result.get("benchmark_mode", MODE_REAL_API)
            if _peak_s is None:
                from llm_benchmark_app.customer_metrics import compute_peak_throughput_summary
                _peak_s = compute_peak_throughput_summary(cases)
            # Use C1 case as single-session summary if available
            _c1_cases = [ca for ca in cases if ca["concurrency"] == 1]
            _single_s = _c1_cases[0]["benchmark_summary"] if _c1_cases else (s[0] if s else {})
            cust_text = generate_customer_acceptance_section(
                _single_s, _obj, _mode,
                sweep_peak_summary=_peak_s,
                workload_label=f"W1_C{_single_s.get('concurrency', '?')}_I1024_O2048",
            )
            # Convert to Markdown by wrapping in code block for alignment
            md.append("---")
            md.append("")
            md.append("## ★ 客户验收指标 / Customer Acceptance Metrics")
            md.append("")
            md.append("```")
            md.append(cust_text)
            md.append("```")
            md.append("")
        except Exception:
            pass

        return "\n".join(md)

    def _start_sweep(self):
        """Validate inputs and start sweep in a background thread."""
        active = getattr(self, "_active_run_type", None)
        if active == "sweep":
            return
        if active is not None:
            self._show_run_busy("sweep")
            return

        api_url = self.url_var.get().strip()
        if not api_url:
            messagebox.showerror(self.tr("msg.error"), self.tr("msg.api_required_settings"))
            return

        user_prompt = self.prompt_var.get().strip()
        if not user_prompt:
            messagebox.showerror(self.tr("msg.error"), self.tr("msg.prompt_required_settings"))
            return

        # Parse concurrency levels
        try:
            concurrency_levels = self._parse_concurrency_levels(
                self.sweep_conc_var.get())
        except ValueError as e:
            messagebox.showerror(self.tr("msg.input_error"), str(e))
            return

        # Request count rule
        request_rule = getattr(self, "sweep_request_rule_var",
                               tk.StringVar(value="formal")).get()
        fixed_total = 0
        try:
            fixed_total = int(self.sweep_fixed_total_var.get())
        except Exception:
            fixed_total = 100
        if request_rule == "fixed" and fixed_total < 1:
            messagebox.showerror(self.tr("msg.input_error"), "固定请求总数必须大于 0")
            return

        # ── Collect sweep scale/tier metadata ──
        _sw_scale    = getattr(self, "_sweep_scale_var", None)
        _sw_tier     = getattr(self, "_sweep_tier_var",  None)
        _sw_modified = getattr(self, "_sweep_preset_modified", None)
        sweep_scale    = _sw_scale.get()    if _sw_scale    else "custom"
        sweep_tier     = _sw_tier.get()     if _sw_tier     else "custom"
        sweep_modified = _sw_modified.get() if _sw_modified else False
        src_scale = getattr(self, "_sweep_source_scale", sweep_scale if sweep_scale != "custom" else None)
        src_tier  = getattr(self, "_sweep_source_tier",  sweep_tier  if sweep_tier  != "custom" else None)
        # If scale or tier is "custom" and was never set from a preset, src stays None
        if sweep_scale == "custom":
            src_scale = None
        if sweep_tier == "custom":
            src_tier = None

        # Request count rule metadata
        _tier_rules = {"quick", "formal", "extended"}
        if request_rule in _tier_rules:
            rct_type = "multiplier_rule"
            rct_formula = SWEEP_TIER_DEFS.get(request_rule, {}).get("formula", "")
        elif request_rule == "fixed":
            rct_type = "fixed_per_point"
            rct_formula = f"固定 {fixed_total} 个请求"
        elif request_rule in ("x1", "x2", "x5", "x10"):
            mult = request_rule.lstrip("x")
            rct_type = "multiplier_rule"
            rct_formula = f"{mult} × C"
        else:
            rct_type = "multiplier_rule"
            rct_formula = request_rule

        # Pre-compute request counts for every concurrency level
        request_counts_by_concurrency = {
            str(c): self._compute_num_requests(c, request_rule, fixed_total)
            for c in concurrency_levels
        }

        # Store on self so background threads can access it
        self._current_sweep_meta = {
            "sweep_scale":       sweep_scale,
            "sweep_scale_label": SWEEP_SCALE_DEFS.get(sweep_scale, {}).get("label_zh", sweep_scale),
            "sweep_tier":        sweep_tier,
            "sweep_tier_label":  SWEEP_TIER_DEFS.get(sweep_tier,  {}).get("label_zh", sweep_tier),
            "source_preset_scale": src_scale,
            "source_preset_tier":  src_tier,
            "preset_modified":     sweep_modified,
            "concurrency_list":    concurrency_levels,
            "request_count_rule_type":           rct_type,
            "request_count_rule":                rct_formula,
            "request_counts_by_concurrency":     request_counts_by_concurrency,
        }

        # High-concurrency warning: max >= 512
        if max(concurrency_levels) >= 512:
            if not messagebox.askyesno(
                    self.tr("sweep.hc_warning_title"),
                    self.tr("sweep.hc_warning_body")):
                return

        # Check aiohttp availability for HC mode
        hc_mode = max(concurrency_levels) > 256
        if hc_mode and not _AIOHTTP_AVAILABLE:
            messagebox.showwarning("aiohttp 未安装", self.tr("sweep.aiohttp_required"))
            hc_mode = False  # fall back to thread pool

        # Linux: show ulimit hint once per session for HC mode
        if hc_mode and sys.platform.startswith("linux"):
            import resource as _res
            try:
                soft, _ = _res.getrlimit(_res.RLIMIT_NOFILE)
                if soft < 65535:
                    messagebox.showinfo(
                        "系统资源提示",
                        f"当前 ulimit -n = {soft}，高并发扫测建议 >= 65535。\n"
                        f"可临时执行: ulimit -n 65535\n\n"
                        f"此提示不影响扫测继续。")
            except Exception:
                pass

        api_key = self.key_var.get().strip()
        model = self.model_var.get().strip()
        system_prompt = self.system_var.get().strip()
        max_tokens = self.max_tokens_var.get()
        output_length_mode = self.output_length_mode_var.get()
        temperature = self.temp_var.get()
        stream = self.stream_var.get() == "是"
        try:
            warmup = int(float(str(self.warmup_var.get()).strip()))
        except Exception:
            warmup = 0

        api_url = normalize_api_url(api_url)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # Reset sweep_runtime_state
        self.sweep_runtime_state.update({
            "running": True,
            "case_index": 0,
            "case_total": len(concurrency_levels),
            "current_concurrency": None,
            "completed_requests": 0,
            "total_requests": 0,
            "success": 0,
            "fail": 0,
            "output_tokens": 0,
            "elapsed_sec": 0.0,
            "rolling_output_tps": None,
            "rolling_rps": None,
            "last_error_summary": None,
        })
        self._sweep_hc_mode = hc_mode

        # Update UI
        if not self._begin_run("sweep"):
            self._show_run_busy("sweep")
            return
        self._show_progress_overlay("sweep", hc_mode=hc_mode)
        self._set_sweep_running_state(True)
        first_label = f"C={concurrency_levels[0]}" if concurrency_levels else ""
        self._start_status_animation(phase="sweep", total=len(concurrency_levels),
                                     current_label=first_label)
        self._sweep_status_card.expand()
        self.sweep_status_text.config(state=tk.NORMAL)
        self.sweep_status_text.delete("1.0", tk.END)
        self.sweep_status_text.insert(tk.END, "准备开始扫测...\n")
        self.sweep_status_text.config(state=tk.DISABLED)
        self.sweep_result_text.config(state=tk.NORMAL)
        self.sweep_result_text.delete("1.0", tk.END)
        self.sweep_result_text.insert(tk.END, "等待扫测结果...\n")
        self.sweep_result_text.config(state=tk.DISABLED)

        # Start throttled UI poll (every 500ms)
        self._start_sweep_ui_poll()

        t = threading.Thread(
            target=self._run_sweep_thread,
            args=(api_url, api_key, model, messages, max_tokens, temperature,
                  concurrency_levels, request_rule, fixed_total, stream, warmup,
                  output_length_mode, hc_mode),
            daemon=True,
        )
        t.start()

    def _run_sweep_thread(self, api_url, api_key, model, messages,
                          max_tokens, temperature,
                          concurrency_levels, request_rule, fixed_total,
                          stream, warmup, output_length_mode="normal",
                          hc_mode=False):
        try:
            if hc_mode and _AIOHTTP_AVAILABLE:
                self._run_sweep_hc_entry(
                    api_url, api_key, model, messages, max_tokens, temperature,
                    concurrency_levels, request_rule, fixed_total, stream, warmup,
                    output_length_mode)
            else:
                self._run_sweep(api_url, api_key, model, messages, max_tokens,
                                temperature, concurrency_levels, request_rule,
                                fixed_total, stream, warmup, output_length_mode)
        except Exception as e:
            self._append_sweep_status(f"\n[FAIL] 扫测异常: {e}\n")
            self.root.after(0, lambda err=str(e): self._sweep_done(None, err))
        finally:
            self.sweep_runtime_state["running"] = False
            self.root.after(0, self._stop_sweep_ui_poll)
            self.root.after(0, self._hide_progress_overlay)
            self.root.after(0, lambda: self._set_sweep_running_state(False))
            self.root.after(0, lambda: self._end_run("sweep"))

    # ── Throttled UI poll for sweep progress (runs on main thread) ─────────────
    def _start_sweep_ui_poll(self):
        """Start the 500ms throttled UI poll for sweep progress."""
        self._stop_sweep_ui_poll()
        self.sweep_ui_poll_after_id = self.root.after(500, self._poll_sweep_ui_status)

    def _stop_sweep_ui_poll(self):
        """Cancel any pending poll callback."""
        if self.sweep_ui_poll_after_id is not None:
            try:
                self.root.after_cancel(self.sweep_ui_poll_after_id)
            except Exception:
                pass
            self.sweep_ui_poll_after_id = None

    def _poll_sweep_ui_status(self):
        """Main-thread poll: read sweep_runtime_state and update UI (max 2/s)."""
        state = dict(self.sweep_runtime_state)  # shallow copy — thread-safe for simple dicts

        # Build overlay detail text
        case_i    = state.get("case_index", 0)
        case_tot  = state.get("case_total", 0)
        conc      = state.get("current_concurrency")
        done      = state.get("completed_requests", 0)
        total     = state.get("total_requests", 0)
        succ      = state.get("success", 0)
        fail      = state.get("fail", 0)
        elapsed   = state.get("elapsed_sec", 0.0)
        tps       = state.get("rolling_output_tps")

        if conc is not None:
            m, s = divmod(int(elapsed), 60)
            elapsed_str = f"{m:02d}:{s:02d}"
            if self._sweep_hc_mode:
                line1 = (f"case {case_i} / {case_tot} · C={conc} · "
                         f"done={done}/{total} · success={succ} · fail={fail} · {elapsed_str}")
                tps_str = f"{tps:.0f}" if tps is not None else "--"
                line2 = f"Rolling Output TPS: {tps_str} tok/s"
                detail = f"{line1}\n{line2}"
            else:
                detail = (f"case {case_i} / {case_tot} · C={conc} · "
                          f"fail={fail} · {elapsed_str}")
            try:
                self._update_progress_overlay(detail)
            except Exception:
                pass

        # Reschedule if sweep is still running
        if state.get("running"):
            self.sweep_ui_poll_after_id = self.root.after(500, self._poll_sweep_ui_status)
        else:
            self.sweep_ui_poll_after_id = None
    # ─────────────────────────────────────────────────────────────────────────────

    def _compute_num_requests(self, c: int, rule: str, fixed_total: int) -> int:
        """Compute number of requests for a given concurrency level and rule.

        Tier-named rules (from SWEEP_TIER_DEFS):
          quick    → max(2 * c,  8)
          formal   → max(5 * c, 20)
          extended → max(5 * c, 50)

        Legacy multiplier rules:
          x1/x2/x5/x10 → c * multiplier
          fixed         → fixed_total
        """
        # Tier-named rules
        if rule == "quick":
            return max(2 * c, 8)
        if rule == "formal":
            return max(5 * c, 20)
        if rule == "extended":
            return max(5 * c, 50)
        # Legacy multiplier rules
        rule_map = {"x1": 1, "x2": 2, "x5": 5, "x10": 10}
        if rule in rule_map:
            return c * rule_map[rule]
        if rule == "fixed":
            return max(1, fixed_total)
        # Legacy: treat rule as multiplier string or fallback to ×2
        try:
            mult = int(rule)
            return c * mult
        except (ValueError, TypeError):
            return c * 2

    def _run_sweep(self, api_url, api_key, model, messages,
                   max_tokens, temperature,
                   concurrency_levels, request_rule="x2", fixed_total=0,
                   stream=True, warmup=0,
                   output_length_mode="normal"):
        """Run a concurrency sweep in the current (background) thread (thread-pool path)."""
        sweep_id = datetime.now().strftime("sweep_%Y%m%d_%H%M%S")
        started_at = datetime.now().isoformat()
        self.root.after(0, lambda: setattr(self, "_current_sweep_run_dir", ""))
        multiplier = 2  # kept for display; actual per-level count from rule

        self._append_sweep_status(f"扫测开始 — {sweep_id}\n")
        self._append_sweep_status(f"API: {api_url}\n")
        self._append_sweep_status(f"Model: {model}\n")
        self._append_sweep_status(f"并发级别: {concurrency_levels}\n")
        self._append_sweep_status(f"请求规则: {request_rule}\n\n")

        # Quick connectivity check
        reachable, err = check_server_reachable(api_url)
        if not reachable:
            self._append_sweep_status(f"✕ 服务器连接失败: {err}\n")
            self.root.after(0, lambda: self._sweep_done(None, f"连接失败: {err}"))
            return
        self._append_sweep_status("✓ 服务器连通性检测通过\n")

        # Warmup (single round before all cases)
        if warmup > 0:
            self._append_sweep_status(f"预热: 发送 {warmup} 次请求...\n")
            for i in range(warmup):
                call_llm(api_url, api_key, model, messages, max_tokens,
                        temperature, stream=stream,
                        output_length_mode=output_length_mode)
            self._append_sweep_status("✓ 预热完成\n\n")

        cases = []
        total_levels = len(concurrency_levels)
        baseline_output_tps = 0.0
        baseline_concurrency = concurrency_levels[0] if concurrency_levels else 1
        sweep_fail_count = 0

        for idx, c in enumerate(concurrency_levels):
            num_requests = self._compute_num_requests(c, request_rule, fixed_total)
            # Update sweep_runtime_state for UI poll (no direct Tk calls from here)
            self.sweep_runtime_state.update({
                "case_index": idx + 1,
                "current_concurrency": c,
                "total_requests": num_requests,
                "completed_requests": 0,
                "success": 0,
                "fail": 0,
            })
            self.root.after(0, lambda i=idx, total=total_levels, cc=c, fail=sweep_fail_count:
                            self._update_status_animation(
                                completed=i, total=total, fail=fail,
                                phase="sweep", current_label=f"C={cc}"))
            self._append_sweep_status(
                f"[{idx + 1}/{total_levels}] 并发={c}, 请求数={num_requests}... ")

            # Run benchmark synchronously in this thread
            result_box = []
            done_event = threading.Event()

            def _progress(completed, total, fail=0):
                pass  # sweep progress is text-based

            def _done(summary):
                result_box.append(summary)
                done_event.set()

            run_benchmark(api_url, api_key, model, messages, max_tokens,
                         temperature, c, num_requests,
                         _progress, _done,
                         stream=stream,
                         preset_name=f"sweep_C{c}",
                         output_length_mode=output_length_mode)

            done_event.wait(timeout=600)

            if result_box:
                summary = result_box[0]
                # Capture baseline from the first (lowest concurrency) case
                if idx == 0:
                    baseline_output_tps = summary.get("system_output_tps", 0) or 0
                    baseline_concurrency = c
                analysis_metrics = self._compute_analysis_metrics(
                    summary, baseline_output_tps, baseline_concurrency)
                case = {
                    "concurrency": c,
                    "total_requests": num_requests,
                    "benchmark_summary": summary,
                    "analysis_metrics": analysis_metrics,
                    "detail_count": analysis_metrics.get("detail_count"),
                    "detail_truncated": analysis_metrics.get("detail_truncated"),
                    "detail_truncation_note": analysis_metrics.get("detail_truncation_note", ""),
                    "failure_summary": {},  # no per-error taxonomy in thread-pool mode
                }
                cases.append(case)
                sweep_fail_count += summary.get("fail", 0) or 0
                self.root.after(0, lambda i=idx + 1, total=total_levels, cc=c, fail=sweep_fail_count:
                                self._update_status_animation(
                                    completed=i, total=total, fail=fail,
                                    phase="sweep", current_label=f"C={cc}"))
                self._append_sweep_status(
                    f"[OK] success={summary['success']} fail={summary['fail']} "
                    f"E2E_avg={summary['e2e_latency_avg']:.3f}s "
                    f"Output_TPS={summary['system_output_tps']:.1f} tok/s\n")
                # Case cooldown: 0s for normal thread-pool sweep
                if idx < total_levels - 1:
                    time.sleep(0)  # explicit 0s cooldown (no-op; reserved for normal mode)
            else:
                sweep_fail_count += 1
                self.root.after(0, lambda i=idx + 1, total=total_levels, cc=c, fail=sweep_fail_count:
                                self._update_status_animation(
                                    completed=i, total=total, fail=fail,
                                    phase="sweep", current_label=f"C={cc}"))
                self._append_sweep_status(f"[FAIL] 未返回结果\n")

        finished_at = datetime.now().isoformat()
        self._safe_set_progress_overlay_detail(self.tr("progress.finishing"))
        analysis_summary = self._generate_analysis_summary(cases)
        sweep_diagnostics = {
            "detail_truncation_notes": self._detect_detail_truncation_notes(cases),
            "non_monotonic_anomalies": self._detect_non_monotonic_sweep_anomalies(cases),
            "recommendation": self._recommend_sweep_concurrency_range(cases),
        }

        # ── Customer-facing peak throughput summary ──
        # Objective and mode are fixed for sweep benchmarks
        _bench_objective = "peak_throughput_sweep"
        _bench_mode      = "real_api_experience"
        try:
            from llm_benchmark_app.customer_metrics import compute_peak_throughput_summary
            _peak_summary = compute_peak_throughput_summary(cases)
        except Exception:
            _peak_summary = {}

        _sweep_meta = getattr(self, "_current_sweep_meta", {})
        sweep_result = {
            "sweep_id": sweep_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "api_url": api_url,
            "model": model,
            "benchmark_objective": _bench_objective,
            "benchmark_mode":      _bench_mode,
            "concurrency_levels": concurrency_levels,
            "requests_multiplier": multiplier,
            "output_length_mode": output_length_mode,
            "fixed_output_tokens": max_tokens if output_length_mode == "fixed" else None,
            "min_tokens_sent": max_tokens if output_length_mode == "fixed" else None,
            "ignore_eos": output_length_mode == "fixed",
            "runner": "threadpool",
            "cases": cases,
            "analysis_summary": analysis_summary,
            "sweep_diagnostics": sweep_diagnostics,
            "hc_peak_metrics": self._compute_hc_peak_metrics(cases, concurrency_levels),
            # ── customer acceptance fields ──
            "peak_throughput_summary": _peak_summary,
            "peak_output_throughput":  _peak_summary.get("peak_output_throughput"),
            "peak_total_token_throughput": _peak_summary.get("peak_total_token_throughput"),
            "recommended_production_concurrency": _peak_summary.get(
                "recommended_production_concurrency"),
            # ── sweep scale/tier metadata ──
            **_sweep_meta,
        }
        self._sweep_result = sweep_result

        # Export — respect user save options
        json_path = ""
        png_path = ""
        md_path = ""
        if self.sweep_save_json_var.get():
            json_path = self._export_sweep_json(sweep_result)
        else:
            self._append_sweep_status("⊘ JSON 保存已跳过（用户选项）\n")
        if self.sweep_save_png_var.get():
            png_path = self._export_sweep_png(sweep_result) or ""
        else:
            self._append_sweep_status("⊘ PNG 保存已跳过（用户选项）\n")
        if self.sweep_save_md_var.get():
            md_path = self._export_sweep_markdown(sweep_result)
        else:
            self._append_sweep_status("⊘ Markdown 保存已跳过（用户选项）\n")
        sweep_result["result_json"] = json_path or ""
        sweep_result["report_png"] = png_path or ""
        sweep_result["report_md"] = md_path or ""
        if self.sweep_save_history_var.get():
            try:
                save_sweep_history(sweep_result, json_path, md_path, png_path or "")
                self._append_sweep_status("✓ 已保存到历史记录\n")
                self.root.after(0, self._refresh_history)
            except Exception as e:
                self._append_sweep_status(f"✕ 历史记录保存失败: {e}\n")
        else:
            self._append_sweep_status("⊘ 历史记录保存已跳过（用户选项）\n")

        self.root.after(0, lambda: self._sweep_done(sweep_result, None,
                                                     json_path, png_path, md_path))

    # ── High-Concurrency Async Runner (asyncio + aiohttp) ────────────────────
    def _run_sweep_hc_entry(self, api_url, api_key, model, messages,
                             max_tokens, temperature,
                             concurrency_levels, request_rule, fixed_total,
                             stream, warmup, output_length_mode):
        """Entry point for HC runner: uses run_async_clean() for safe event-loop lifecycle.

        run_async_clean() cancels pending tasks, drains async generators, and
        shuts down the executor before closing the loop — preventing the
        'RuntimeError: Event loop is closed' errors that arise from dangling
        aiohttp async generators or semaphore waiters on Windows / Python 3.14.
        """
        run_async_clean(
            self._run_sweep_hc_async(
                api_url, api_key, model, messages, max_tokens, temperature,
                concurrency_levels, request_rule, fixed_total,
                stream, warmup, output_length_mode),
            max_concurrency=max(concurrency_levels) if concurrency_levels else 1)

    async def _run_sweep_hc_async(self, api_url, api_key, model, messages,
                                   max_tokens, temperature,
                                   concurrency_levels, request_rule, fixed_total,
                                   stream, warmup, output_length_mode):
        """Full high-concurrency sweep using asyncio + aiohttp."""
        import aiohttp

        sweep_id = datetime.now().strftime("sweep_%Y%m%d_%H%M%S")
        started_at = datetime.now().isoformat()
        self.root.after(0, lambda: setattr(self, "_current_sweep_run_dir", ""))

        self._append_sweep_status(f"扫测开始 (HC) — {sweep_id}\n")
        self._append_sweep_status(f"API: {api_url}\n")
        self._append_sweep_status(f"并发级别: {concurrency_levels}\n")
        self._append_sweep_status(f"请求规则: {request_rule}\n")
        self._append_sweep_status(f"高并发模式: asyncio + aiohttp\n\n")

        max_conc = max(concurrency_levels)

        # ── Windows C512+ guard ──────────────────────────────────────────
        # This coroutine runs inside the loop created by run_async_clean() via
        # _new_hc_event_loop().  If somehow a SelectorEventLoop
        # sneaked in (e.g. an outer policy set it), fail fast with a clear message
        # rather than crashing mid-sweep with "too many file descriptors in select()".
        if sys.platform.startswith("win") and max_conc >= 512:
            _running_loop = asyncio.get_running_loop()
            if "Selector" in type(_running_loop).__name__:
                raise RuntimeError(
                    "Windows C512+ 高并发扫测不能在 SelectorEventLoop / select() 下运行。\n"
                    "select() 最多只能管理 512 个 socket（FD_SETSIZE 限制），"
                    "超出后会抛出 \"too many file descriptors in select()\"。\n"
                    "请使用 ProactorEventLoop，或在 Linux 客户端运行高并发扫测。\n\n"
                    "Windows C512+ sweep cannot run on SelectorEventLoop/select(). "
                    "Use ProactorEventLoop or run high-concurrency sweep on Linux."
                )

        connector = aiohttp.TCPConnector(
            limit=max_conc,
            limit_per_host=max_conc,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
        timeout = aiohttp.ClientTimeout(
            total=3600,    # per spec: total_timeout=3600 for long 2048-token runs
            connect=30,
            sock_connect=30,
            sock_read=900, # per spec: sock_read_timeout=900 for 2048-token outputs
        )

        body_dict_base = build_chat_payload(model, messages, max_tokens, temperature,
                                            stream=stream,
                                            output_length_mode=output_length_mode)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        cases = []
        baseline_output_tps = 0.0
        baseline_concurrency = concurrency_levels[0] if concurrency_levels else 1
        sweep_fail_count = 0
        total_levels = len(concurrency_levels)

        async with aiohttp.ClientSession(
                connector=connector, timeout=timeout, headers=headers) as session:

            # Warmup (serial)
            if warmup > 0:
                self._append_sweep_status(f"预热: 发送 {warmup} 次请求...\n")
                for _ in range(warmup):
                    try:
                        await self._hc_call_one_async(session, api_url, body_dict_base, stream)
                    except Exception:
                        pass
                self._append_sweep_status("[OK] 预热完成\n\n")

            for idx, c in enumerate(concurrency_levels):
                num_requests = self._compute_num_requests(c, request_rule, fixed_total)
                self._append_sweep_status(
                    f"[{idx + 1}/{total_levels}] 并发={c}, 请求数={num_requests}... ")

                # Reset per-case counters in shared state
                case_start_wall = asyncio.get_running_loop().time()
                self.sweep_runtime_state.update({
                    "case_index": idx + 1,
                    "current_concurrency": c,
                    "total_requests": num_requests,
                    "completed_requests": 0,
                    "success": 0,
                    "fail": 0,
                    "output_tokens": 0,
                    "elapsed_sec": 0.0,
                })

                # Update status animation (from main thread)
                self.root.after(0, lambda i=idx, total=total_levels, cc=c, fail=sweep_fail_count:
                                self._update_status_animation(
                                    completed=i, total=total, fail=fail,
                                    phase="sweep", current_label=f"C={cc}"))

                results, duration, failure_counts = await self._run_hc_case_async(
                    session, api_url, body_dict_base, c, num_requests, stream,
                    case_start_wall)

                # Aggregate
                config = {
                    "api_url": api_url,
                    "model": model,
                    "prompt": messages[-1]["content"][:100] if messages else "",
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "concurrency": c,
                    "total": num_requests,
                    "stream_mode": stream,
                    "benchmark_preset_name": f"sweep_hc_C{c}",
                    "benchmark_preset_type": "fixed_concurrency",
                    "output_length_mode": output_length_mode,
                }
                summary = aggregate_results(results, duration, config)
                summary["benchmark_preset_name"] = f"sweep_hc_C{c}"
                summary["benchmark_preset_type"] = "fixed_concurrency"

                if idx == 0:
                    baseline_output_tps = summary.get("system_output_tps", 0) or 0
                    baseline_concurrency = c
                analysis_metrics = self._compute_analysis_metrics(
                    summary, baseline_output_tps, baseline_concurrency)
                case = {
                    "concurrency": c,
                    "total_requests": num_requests,
                    "benchmark_summary": summary,
                    "analysis_metrics": analysis_metrics,
                    "detail_count": analysis_metrics.get("detail_count"),
                    "detail_truncated": analysis_metrics.get("detail_truncated"),
                    "detail_truncation_note": analysis_metrics.get("detail_truncation_note", ""),
                    "failure_summary": failure_counts,
                }
                cases.append(case)
                sweep_fail_count += summary.get("fail", 0) or 0

                self.root.after(0, lambda i=idx + 1, total=total_levels, cc=c, fail=sweep_fail_count:
                                self._update_status_animation(
                                    completed=i, total=total, fail=fail,
                                    phase="sweep", current_label=f"C={cc}"))

                # Log failure summary if any
                fail_log = ""
                if failure_counts:
                    parts = [f"{k}={v}" for k, v in sorted(failure_counts.items()) if v > 0]
                    fail_log = f" [{', '.join(parts)}]"
                self._append_sweep_status(
                    f"[OK] success={summary['success']} fail={summary['fail']}{fail_log} "
                    f"E2E_avg={summary['e2e_latency_avg']:.3f}s "
                    f"Output_TPS={summary['system_output_tps']:.1f} tok/s\n")

                # Case cooldown: 10s between HC cases (让服务端自然恢复)
                if idx < total_levels - 1:
                    self._append_sweep_status("  (冷却 10s...)\n")
                    await asyncio.sleep(10)

        finished_at = datetime.now().isoformat()
        self._safe_set_progress_overlay_detail(self.tr("progress.finishing"))
        analysis_summary = self._generate_analysis_summary(cases)
        sweep_diagnostics = {
            "detail_truncation_notes": self._detect_detail_truncation_notes(cases),
            "non_monotonic_anomalies": self._detect_non_monotonic_sweep_anomalies(cases),
            "recommendation": self._recommend_sweep_concurrency_range(cases),
        }

        # HC peak metrics
        peak_case = max(cases, key=lambda ca: ca["benchmark_summary"].get("system_output_tps", 0),
                        default=None)
        hc_peak_metrics = self._compute_hc_peak_metrics(cases, concurrency_levels)

        # ── Customer-facing peak throughput summary (HC path) ──
        # Objective and mode are fixed for HC sweep benchmarks
        _hc_bench_objective = "peak_throughput_sweep"
        _hc_bench_mode      = "real_api_experience"
        try:
            from llm_benchmark_app.customer_metrics import compute_peak_throughput_summary
            _hc_peak_summary = compute_peak_throughput_summary(cases)
        except Exception:
            _hc_peak_summary = {}

        _hc_sweep_meta = getattr(self, "_current_sweep_meta", {})
        sweep_result = {
            "sweep_id": sweep_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "api_url": api_url,
            "model": model,
            "benchmark_objective": _hc_bench_objective,
            "benchmark_mode":      _hc_bench_mode,
            "concurrency_levels": concurrency_levels,
            "requests_multiplier": request_rule,
            "output_length_mode": output_length_mode,
            "fixed_output_tokens": max_tokens if output_length_mode == "fixed" else None,
            "min_tokens_sent": max_tokens if output_length_mode == "fixed" else None,
            "ignore_eos": output_length_mode == "fixed",
            "runner": "asyncio+aiohttp",
            "high_concurrency": True,
            "cases": cases,
            "analysis_summary": analysis_summary,
            "sweep_diagnostics": sweep_diagnostics,
            "hc_peak_metrics": hc_peak_metrics,
            # ── customer acceptance fields ──
            "peak_throughput_summary": _hc_peak_summary,
            "peak_output_throughput":  _hc_peak_summary.get("peak_output_throughput"),
            "peak_total_token_throughput": _hc_peak_summary.get("peak_total_token_throughput"),
            "recommended_production_concurrency": _hc_peak_summary.get(
                "recommended_production_concurrency"),
            # ── sweep scale/tier metadata ──
            **_hc_sweep_meta,
        }
        self._sweep_result = sweep_result

        # Export (same as thread-pool path)
        json_path = ""
        png_path = ""
        md_path = ""
        if self.sweep_save_json_var.get():
            json_path = self._export_sweep_json(sweep_result)
        else:
            self._append_sweep_status("⊘ JSON 保存已跳过\n")
        if self.sweep_save_png_var.get():
            png_path = self._export_sweep_png(sweep_result) or ""
        else:
            self._append_sweep_status("⊘ PNG 保存已跳过\n")
        if self.sweep_save_md_var.get():
            md_path = self._export_sweep_markdown(sweep_result)
        else:
            self._append_sweep_status("⊘ Markdown 保存已跳过\n")
        sweep_result["result_json"] = json_path or ""
        sweep_result["report_png"] = png_path or ""
        sweep_result["report_md"] = md_path or ""
        if self.sweep_save_history_var.get():
            try:
                save_sweep_history(sweep_result, json_path, md_path, png_path or "")
                self._append_sweep_status("[OK] 已保存到历史记录\n")
                self.root.after(0, self._refresh_history)
            except Exception as e:
                self._append_sweep_status(f"[FAIL] 历史记录保存失败: {e}\n")
        else:
            self._append_sweep_status("⊘ 历史记录保存已跳过\n")

        self.root.after(0, lambda: self._sweep_done(sweep_result, None,
                                                     json_path, png_path, md_path))

    async def _run_hc_case_async(self, session, api_url, body_dict_base, concurrency,
                                  num_requests, stream, case_start_wall):
        """Run all requests for one concurrency level asynchronously.
        Returns (results_list, duration_sec, failure_counts_dict).

        Task lifecycle (Python 3.10–3.14 safe):
          - All coroutines are wrapped in asyncio.create_task() immediately so
            the event loop owns their lifetime (no bare coroutine objects left
            for the GC to close).
          - gather(..., return_exceptions=True) waits for every task.
          - The finally block cancels any task that survived (edge-case guard)
            and re-awaits them so no task outlives this coroutine.
        CancelledError:
          - Propagates out of `async with sem` so the semaphore's __aexit__
            runs while the loop is still alive (not after loop.close()).
          - Caught outside the sem block and recorded as a failure result.
        """
        results = []
        failure_counts: dict = {}
        state = self.sweep_runtime_state
        loop = asyncio.get_running_loop()

        sem = asyncio.Semaphore(concurrency)

        async def one_request():
            _result = None
            try:
                async with sem:
                    try:
                        _result = await self._hc_call_one_async(
                            session, api_url, body_dict_base, stream)
                    except asyncio.CancelledError:
                        # Re-raise so __aexit__ runs while loop is still alive,
                        # then the outer except catches it for result recording.
                        raise
                    except Exception as exc:
                        exc_s = str(exc)
                        # WinError inline check (WSAECONNRESET=10054, WSAETIMEDOUT=10060)
                        if "WinError 10054" in exc_s or "winerror 10054" in exc_s:
                            err_type = "connection_reset"
                        elif "WinError 10060" in exc_s or "winerror 10060" in exc_s:
                            err_type = "connect_timeout"
                        else:
                            err_type = _hc_classify_error(exc)
                        _result = _hc_fail_result(err_type, exc_s[:200])
            except asyncio.CancelledError:
                # Semaphore __aexit__ has already run (loop still open); record failure.
                _result = _hc_fail_result("cancelled", "cancelled")

            if _result is None:
                return  # Guard: should not happen with the structure above

            results.append(_result)
            try:
                elapsed = loop.time() - case_start_wall
            except RuntimeError:
                elapsed = 0.0  # loop closed unexpectedly; best-effort
            # Update shared state (asyncio single-threaded — no lock needed)
            state["elapsed_sec"] = elapsed
            state["completed_requests"] += 1
            if _result["ok"]:
                state["success"] += 1
                tok = _result.get("completion_tokens", 0) or 0
                state["output_tokens"] += tok
                if elapsed > 0:
                    state["rolling_output_tps"] = state["output_tokens"] / elapsed
                    state["rolling_rps"] = state["success"] / elapsed
            else:
                state["fail"] += 1
                _err_type = _result.get("error_type", "unknown")
                failure_counts[_err_type] = failure_counts.get(_err_type, 0) + 1

        t0 = loop.time()
        tasks = [asyncio.create_task(one_request()) for _ in range(num_requests)]
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            # Ensure no tasks outlive this coroutine (edge-case: early return/raise)
            for t in tasks:
                if not t.done():
                    t.cancel()
            _lingering = [t for t in tasks if not t.done()]
            if _lingering:
                await asyncio.gather(*_lingering, return_exceptions=True)
        duration = loop.time() - t0
        return results, duration, dict(failure_counts)

    async def _hc_call_one_async(self, session, api_url, body_dict_base, stream: bool) -> dict:
        """Async HTTP call for one request. Returns a result dict compatible with aggregate_results()."""
        import aiohttp as _aio
        body_dict = dict(body_dict_base)
        body = json.dumps(body_dict).encode("utf-8")
        # get_running_loop() is always safe inside a coroutine (3.7+).
        # Avoids the deprecated get_event_loop() which emits DeprecationWarning in 3.12+ and
        # may behave differently under Python 3.14.
        loop = asyncio.get_running_loop()
        t0 = loop.time()

        try:
            async with session.post(api_url, data=body) as resp:
                if resp.status >= 500:
                    text = await resp.text()
                    e2e = loop.time() - t0
                    return _hc_fail_result("server_5xx", text[:200], e2e)
                if resp.status >= 400:
                    text = await resp.text()
                    e2e = loop.time() - t0
                    return _hc_fail_result("server_4xx", text[:200], e2e)

                if not stream:
                    raw = await resp.read()
                    e2e = loop.time() - t0
                    try:
                        data = json.loads(raw)
                        choice = data.get("choices", [{}])[0]
                        usage = data.get("usage", {})
                        prompt_tokens = usage.get("prompt_tokens", 0)
                        completion_tokens = usage.get("completion_tokens", 0)
                        total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)
                        per_req_tps = completion_tokens / e2e if e2e > 0 and completion_tokens > 0 else 0.0
                        return {
                            "ok": True,
                            "e2e_latency": round(e2e, 6),
                            "latency": round(e2e, 6),
                            "ttft": None, "tpot": None, "itl_avg": None, "itl_values": [],
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": completion_tokens,
                            "total_tokens": total_tokens,
                            "per_request_output_tps_e2e": round(per_req_tps, 2),
                            "per_request_decode_tps": None,
                            "finish_reason": choice.get("finish_reason", "unknown"),
                            "stream": False,
                        }
                    except Exception as exc:
                        return _hc_fail_result("json_parse_error", str(exc)[:200], e2e)

                # Streaming SSE path
                first_data_line_time = None
                first_json_chunk_time = None
                completion_tokens = 0
                prompt_tokens = 0
                finish_reason = "unknown"

                async for raw_line in resp.content:
                    now = loop.time()
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    if first_data_line_time is None:
                        first_data_line_time = now
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        obj = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if first_json_chunk_time is None:
                        first_json_chunk_time = now
                    # Use central parser for token counting
                    _chunk = _parse_stream_chunk(obj)
                    if _chunk.finish_reason:
                        finish_reason = _chunk.finish_reason
                    # Token counting from usage field or accumulated
                    usage = obj.get("usage") or {}
                    if usage.get("completion_tokens"):
                        completion_tokens = usage["completion_tokens"]
                        prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                    elif _chunk.generated_text:
                        # Approximate: count by whitespace (fast, good enough for HC)
                        completion_tokens += len((_chunk.generated_text or "").split())

                e2e = loop.time() - t0
                ttft = (first_data_line_time - t0) if first_data_line_time else None
                per_req_tps = completion_tokens / e2e if e2e > 0 and completion_tokens > 0 else 0.0
                tpot = None
                if ttft is not None and completion_tokens >= 2 and (e2e - ttft) > 0:
                    tpot = (e2e - ttft) / max(completion_tokens - 1, 1)
                return {
                    "ok": True,
                    "e2e_latency": round(e2e, 6),
                    "latency": round(e2e, 6),
                    "ttft": round(ttft, 6) if ttft is not None else None,
                    "tpot": round(tpot, 6) if tpot is not None else None,
                    "itl_avg": None, "itl_values": [],
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                    "per_request_output_tps_e2e": round(per_req_tps, 2),
                    "per_request_decode_tps": None,
                    "finish_reason": finish_reason,
                    "stream": True,
                }

        except asyncio.TimeoutError as exc:
            return _hc_fail_result("timeout", str(exc)[:200],
                                   loop.time() - t0)
        except _aio.ServerTimeoutError as exc:
            return _hc_fail_result("timeout", str(exc)[:200],
                                   loop.time() - t0)
        except _aio.ClientConnectorError as exc:
            return _hc_fail_result("connect_error", str(exc)[:200],
                                   loop.time() - t0)
        except _aio.ServerDisconnectedError as exc:
            return _hc_fail_result("read_error", str(exc)[:200],
                                   loop.time() - t0)
        except _aio.ClientResponseError as exc:
            err_type = ("server_5xx" if exc.status >= 500
                        else "server_4xx" if exc.status >= 400
                        else "read_error")
            return _hc_fail_result(err_type, str(exc)[:200],
                                   loop.time() - t0)
        except (OSError, ConnectionError) as exc:
            return _hc_fail_result("client_resource_error", str(exc)[:200],
                                   loop.time() - t0)
        except json.JSONDecodeError as exc:
            return _hc_fail_result("json_parse_error", str(exc)[:200],
                                   loop.time() - t0)
        except Exception as exc:
            return _hc_fail_result("unknown", str(exc)[:200],
                                   loop.time() - t0)

    def _compute_hc_peak_metrics(self, cases: list, concurrency_levels: list) -> dict:
        """Compute high-concurrency peak metrics for report."""
        if not cases:
            return {}
        peak_tps = max((c["benchmark_summary"].get("system_output_tps", 0) or 0
                        for c in cases), default=0)
        peak_case = max(cases,
                        key=lambda ca: ca["benchmark_summary"].get("system_output_tps", 0) or 0,
                        default=None)
        peak_conc = peak_case["concurrency"] if peak_case else None

        # Stable concurrency: highest concurrency where success_rate >= 95%
        stable_conc = None
        for ca in reversed(cases):
            sr = ca["benchmark_summary"].get("success_rate", 0) or 0
            if sr >= 95.0:
                stable_conc = ca["concurrency"]
                break

        # C1024 specific metrics
        c1024_case = next((ca for ca in cases if ca["concurrency"] == 1024), None)
        c1024_metrics = {}
        if c1024_case:
            s = c1024_case["benchmark_summary"]
            c1024_metrics = {
                "success_rate": s.get("success_rate"),
                "fail_count": s.get("fail"),
                "output_tps": s.get("system_output_tps"),
                "failure_summary": c1024_case.get("failure_summary", {}),
            }

        # Find highest tested concurrency
        max_tested_conc = max(concurrency_levels) if concurrency_levels else 0

        return {
            "peak_output_tps": round(peak_tps, 1),
            "peak_concurrency": peak_conc,
            "peak_stable_concurrency": stable_conc,
            "max_tested_concurrency": max_tested_conc,
            "c1024_metrics": c1024_metrics,
            "meets_8k_threshold": peak_tps >= 8000,
            "meets_25k_threshold": peak_tps >= 25000,
        }
    # ── End HC runner ─────────────────────────────────────────────────────────

    def _append_sweep_status(self, text: str):
        """Append text to the sweep status widget (thread-safe via root.after)."""
        self.root.after(0, lambda: self._do_append_sweep_status(text))

    def _do_append_sweep_status(self, text: str):
        """Actually append to the status widget (on main thread)."""
        try:
            self.sweep_status_text.config(state=tk.NORMAL)
            self.sweep_status_text.insert(tk.END, text)
            self.sweep_status_text.see(tk.END)
            self.sweep_status_text.config(state=tk.DISABLED)
        except Exception:
            pass

    def _sweep_done(self, sweep_result, error=None, json_path=None, png_path=None,
                    md_path=None):
        """Called on main thread when sweep completes or fails."""
        self._set_sweep_running_state(False)

        if error:
            self._stop_status_animation(success=False, fail=max(self._run_fail, 1),
                                        message=self.tr("status.failed"))
            self._do_append_sweep_status(f"\n✕ 扫测失败: {error}\n")
            return

        if not sweep_result or not sweep_result.get("cases"):
            self._stop_status_animation(success=False, fail=max(self._run_fail, 1),
                                        message=self.tr("status.failed"))
            self._do_append_sweep_status("\n✕ 扫测未产生结果\n")
            return

        cases = sweep_result["cases"]
        self._stop_status_animation(success=True, completed=len(cases),
                                    total=len(sweep_result.get("concurrency_levels", cases)),
                                    fail=self._run_fail, message=self.tr("status.completed"))
        self._do_append_sweep_status(
            f"\n✓ 扫测完成 — {len(cases)} 个并发级别\n")

        # Display summary in results area
        self.sweep_result_text.config(state=tk.NORMAL)
        self.sweep_result_text.delete("1.0", tk.END)
        r = []
        r.append("=" * 60)
        r.append(f"  {self.tr('chart.sweep_title')}")
        r.append("=" * 60)
        r.append(f"  Sweep ID: {sweep_result['sweep_id']}")
        r.append(f"  API URL:  {sweep_result['api_url']}")
        r.append(f"  Model:    {sweep_result['model']}")
        r.append(f"  并发级别: {sweep_result['concurrency_levels']}")
        r.append(f"  请求倍数: {sweep_result['requests_multiplier']}")
        r.append("")
        r.append(f"{'C':>4} {'Req':>5} {'Success':>7} {'Fail':>5} {'Rate':>6} "
                 f"{'E2E_avg':>8} {'E2E_P95':>8} {'TTFT_avg':>9} "
                 f"{'Out_TPS':>8} {'RPS':>7}")
        r.append("-" * 78)
        for case in cases:
            s = case["benchmark_summary"]
            r.append(
                f"{case['concurrency']:>4} {case['total_requests']:>5} "
                f"{s['success']:>7} {s['fail']:>5} "
                f"{s.get('success_rate', 0):>5.1f}% "
                f"{s['e2e_latency_avg']:>8.3f} {s['e2e_latency_p95']:>8.3f} "
                f"{s.get('ttft_avg', 0):>9.3f} "
                f"{s.get('system_output_tps', 0):>8.1f} "
                f"{s.get('request_throughput_rps', 0):>7.2f}")
        r.append("")
        # Per-case failure summary (HC mode)
        has_failure_summary = any(case.get("failure_summary") for case in cases)
        if has_failure_summary:
            r.append("-" * 78)
            r.append("  失败原因分布 (Failure Taxonomy)")
            r.append("-" * 78)
            for case in cases:
                fs = case.get("failure_summary") or {}
                if fs:
                    parts = [f"{k}={v}" for k, v in sorted(fs.items()) if v > 0]
                    if parts:
                        r.append(f"  C={case['concurrency']:>4}: {', '.join(parts)}")
            r.append("")
        # HC peak metrics section
        hc_peak = sweep_result.get("hc_peak_metrics") or {}
        if hc_peak:
            r.append("-" * 78)
            r.append("  高并发峰值指标 (Peak Metrics)")
            r.append("-" * 78)
            r.append(f"  Peak Output TPS:       {hc_peak.get('peak_output_tps', 0):.1f} tok/s")
            r.append(f"  Peak Concurrency:      C={hc_peak.get('peak_concurrency')}")
            r.append(f"  Peak Stable Conc:      C={hc_peak.get('peak_stable_concurrency')}")
            r.append(f"  Max Tested Conc:       C={hc_peak.get('max_tested_concurrency')}")
            r.append(f"  Meets >8000 tok/s:     {'Yes' if hc_peak.get('meets_8k_threshold') else 'No'}")
            r.append(f"  Meets >25000 tok/s:    {'Yes' if hc_peak.get('meets_25k_threshold') else 'No'}")
            c1024 = hc_peak.get("c1024_metrics") or {}
            if c1024:
                r.append(f"  C1024 Success Rate:    {c1024.get('success_rate', 0):.1f}%")
                r.append(f"  C1024 Fail Count:      {c1024.get('fail_count', 0)}")
                c1024_fs = c1024.get("failure_summary") or {}
                if c1024_fs:
                    fs_parts = [f"{k}={v}" for k, v in sorted(c1024_fs.items()) if v > 0]
                    r.append(f"  C1024 Failures:        {', '.join(fs_parts)}")
            r.append("")
        r.append("-" * 78)
        r.append("  自动分析摘要")
        r.append("-" * 78)
        for i, line in enumerate(sweep_result.get("analysis_summary", []), 1):
            r.append(f"  {i}. {line}")
        r.append("")
        if json_path:
            r.append(f"  JSON 结果已导出: {json_path}")
        else:
            r.append("  JSON 结果: 未保存")
        png_path_result = sweep_result.get("report_png", "")
        if png_path_result:
            r.append(f"  PNG 趋势图已导出: {png_path_result}")
        elif png_path is None:
            r.append("  PNG 趋势图: 未生成 (matplotlib unavailable)")
        else:
            r.append("  PNG 趋势图: 未保存")
        md_path_result = sweep_result.get("report_md", "")
        if md_path_result:
            r.append(f"  Markdown 分析报告: {md_path_result}")
        else:
            r.append("  Markdown 分析报告: 未保存")
        self.sweep_result_text.insert(tk.END, "\n".join(r) + "\n")
        self.sweep_result_text.config(state=tk.DISABLED)
        self._current_sweep_json_path = json_path or ""
        self._current_sweep_md_path = md_path or ""
        self._current_sweep_png_path = png_path or ""

        # ── Populate expert analysis area ──
        commentary = self._generate_expert_commentary(cases,
                         sweep_result.get("analysis_summary", []))
        next_steps = self._generate_next_steps(cases,
                         sweep_result.get("analysis_summary", []))

        expert_content = []
        expert_content.append("── 核心结论 ──")
        for i, line in enumerate(sweep_result.get("analysis_summary", []), 1):
            expert_content.append(f"  {i}. {line}")
        expert_content.append("")
        expert_content.append("── 专家简评 ──")
        expert_content.append(commentary)
        expert_content.append("")
        expert_content.append("── 下一步建议 ──")
        for i, step in enumerate(next_steps, 1):
            expert_content.append(f"  {i}. {step}")

        self._expert_text.config(state=tk.NORMAL)
        self._expert_text.delete("1.0", tk.END)
        self._expert_text.insert(tk.END, "\n".join(expert_content))
        self._expert_text.config(state=tk.DISABLED)

        # Auto-expand chart and expert cards after sweep completes
        self._sweep_chart_card.expand()
        self._expert_card.expand()
        self._sweep_output_card.expand()

        # ── Render chart in GUI ──
        self._render_sweep_chart(sweep_result)

        # ── Auto-save sweep to ResultStore (always) ──
        env_info = self._get_active_env_info()
        _rs_sweep_dir = ""
        try:
            _rs_results_root = (getattr(self, "results_root_var", None) or
                                tk.StringVar(value=RESULTS_ROOT)).get() or RESULTS_ROOT
            _sweep_report_md = sweep_result.get("report_md", "") or ""
            if not _sweep_report_md:
                _sweep_report_md = self._generate_sweep_markdown_report(sweep_result)
            _rs_sweep_out = rs_save_sweep_run(
                _rs_results_root, sweep_result, _sweep_report_md, env_info)
            _rs_sweep_dir = _rs_sweep_out.get("run_dir", "")
            setattr(self, "_last_sweep_run_dir", _rs_sweep_dir)
            logging.info("ResultStore sweep saved to: %s", _rs_sweep_dir)
        except Exception as _rs_sweep_err:
            logging.warning("ResultStore sweep save failed: %s", _rs_sweep_err)

        # ── Legacy result DB save ──
        try:
            db_path = getattr(self, "result_db_path_var", tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
            run_dir = _rs_sweep_dir or getattr(self, "_current_sweep_run_dir", "") or ""
            _result_db_save_sweep_run(
                db_path, sweep_result,
                env_info.get("env_profile_id"),
                env_info.get("hw_profile_id"),
                env_info.get("sw_profile_id"),
                env_info.get("model_profile_id"),
                run_dir,
                snapshots=env_info.get("snapshots", {}),
                applied_info={
                    "applied_environment_params": self._applied_environment_params,
                    "applied_fields_json":        self._applied_fields_json,
                    "overridden_fields_json":     self._overridden_fields_json,
                })
        except Exception as e:
            logging.warning("sweep result DB save failed: %s", e)

    # ── Matplotlib helpers ──
    @staticmethod
    def _matplotlib_available() -> bool:
        """Return True if matplotlib is importable (lazy check)."""
        try:
            import matplotlib
            return True
        except ImportError:
            return False

    @staticmethod
    def _configure_matplotlib_cjk_fonts():
        """Detect and configure a CJK-compatible font for matplotlib.
        Must be called after importing matplotlib but before creating any figure."""
        import matplotlib.font_manager as fm
        import matplotlib as mpl

        mpl.rcParams["axes.unicode_minus"] = False

        # Find available CJK fonts by family name or known platform font paths.
        cjk_candidates = [
            "Noto Sans CJK SC",
            "Noto Sans Mono CJK SC",
            "WenQuanYi Micro Hei",
            "WenQuanYi Zen Hei",
            "SimHei",
            "Microsoft YaHei",
            "PingFang SC",
            "Droid Sans Fallback",
        ]
        available = {f.name for f in fm.fontManager.ttflist}
        selected = None
        for name in cjk_candidates:
            if name in available:
                selected = name
                break

        if not selected:
            font_paths = [
                r"C:\Windows\Fonts\msyh.ttc",
                r"C:\Windows\Fonts\simhei.ttf",
                r"C:\Windows\Fonts\simsun.ttc",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
                "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
                "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
                "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf",
            ]
            for path in font_paths:
                if os.path.exists(path):
                    try:
                        fm.fontManager.addfont(path)
                        selected = fm.FontProperties(fname=path).get_name()
                        break
                    except Exception:
                        continue

        if selected:
            # Insert as first sans-serif fallback
            current = mpl.rcParams.get("font.sans-serif", [])
            if not isinstance(current, list):
                current = [current] if current else []
            if selected not in current:
                current.insert(0, selected)
            mpl.rcParams["font.sans-serif"] = current
            mpl.rcParams["axes.unicode_minus"] = False
            # Rebuild font cache
            fm._load_fontmanager(try_read_cache=False)
            LLMBenchmarkApp._last_cjk_font_available = True
            return True
        LLMBenchmarkApp._last_cjk_font_available = False
        return False

    def _build_sweep_analysis_figure(self, sweep_result: dict):
        """Build the 2x2 sweep analysis figure. Returns matplotlib.figure.Figure.
        Used for both GUI embedding and PNG export."""
        import matplotlib
        matplotlib.use("Agg")
        self._configure_matplotlib_cjk_fonts()
        import matplotlib.pyplot as plt

        cases = sweep_result.get("cases", [])
        if not cases:
            return None

        conc = [c["concurrency"] for c in cases]
        s = [c["benchmark_summary"] for c in cases]

        fig, axes = plt.subplots(2, 2, figsize=(10, 7.5))
        fig.suptitle(self.tr("chart.sweep_title"), fontsize=13, fontweight="bold")

        # Subplot 1: E2E latency trend
        ax1 = axes[0, 0]
        e2e_avg = [cs.get("e2e_latency_avg", 0) or None for cs in s]
        e2e_p50 = [cs.get("e2e_latency_p50", 0) or None for cs in s]
        e2e_p95 = [cs.get("e2e_latency_p95", 0) or None for cs in s]
        e2e_p99 = [cs.get("e2e_latency_p99", 0) or None for cs in s]
        ax1.plot(conc, e2e_avg, "o-", color="#533AFD", linewidth=2, label="E2E Avg")
        ax1.plot(conc, e2e_p50, "s--", color="#10B981", linewidth=1.5, label="E2E P50")
        ax1.plot(conc, e2e_p95, "D--", color="#F59E0B", linewidth=1.5, label="E2E P95")
        ax1.plot(conc, e2e_p99, "^:", color="#EF4444", linewidth=1.5, label="E2E P99")
        ax1.set_xlabel(self.tr("chart.concurrency_axis"))
        ax1.set_ylabel(self.tr("chart.latency_axis"))
        ax1.set_title(self.tr("chart.latency_vs_concurrency"))
        ax1.legend(fontsize=7)
        ax1.grid(True, alpha=0.3)

        # Subplot 2: Token throughput
        ax2 = axes[0, 1]
        out_tps = [cs.get("system_output_tps", 0) or None for cs in s]
        total_tps = [cs.get("system_total_tps", 0) or None for cs in s]
        ax2.plot(conc, out_tps, "o-", color="#533AFD", linewidth=2, label="Output TPS")
        ax2.plot(conc, total_tps, "s--", color="#10B981", linewidth=1.5, label="Total TPS")
        ax2.set_xlabel(self.tr("chart.concurrency_axis"))
        ax2.set_ylabel(self.tr("chart.throughput_axis"))
        ax2.set_title(self.tr("chart.throughput_vs_concurrency"))
        ax2.legend(fontsize=7)
        ax2.grid(True, alpha=0.3)

        # Subplot 3: TTFT / FVT / Generation speed
        ax3 = axes[1, 0]
        ttft_avg = [cs.get("ttft_avg") or None for cs in s]
        fvt_avg = [cs.get("first_visible_token_avg") or None for cs in s]
        fvg_avg = [cs.get("first_visible_gap_avg") or None for cs in s]
        tpot_avg = [cs.get("tpot_avg") or None for cs in s]
        itl_avg = [cs.get("itl_avg") or None for cs in s]
        if any(v is not None and v > 0 for v in ttft_avg):
            ax3.plot(conc, ttft_avg, "o-", color="#533AFD", linewidth=1.5,
                     label="TTFT")
        if any(v is not None and v > 0 for v in fvt_avg):
            ax3.plot(conc, fvt_avg, "s--", color="#10B981", linewidth=1.5,
                     label="FVT")
        if any(v is not None and v > 0 for v in fvg_avg):
            ax3.plot(conc, fvg_avg, "D:", color="#F59E0B", linewidth=1.5,
                     label="FVG")
        if any(v is not None and v > 0 for v in tpot_avg):
            ax3.plot(conc, tpot_avg, "^-", color="#EF4444", linewidth=1.5,
                     label="TPOT")
        if any(v is not None and v > 0 for v in itl_avg):
            ax3.plot(conc, itl_avg, "v--", color="#8B5CF6", linewidth=1.5,
                     label="ITL")
        ax3.set_xlabel(self.tr("chart.concurrency_axis"))
        ax3.set_ylabel(self.tr("chart.time_axis"))
        ax3.set_title(self.tr("chart.first_generation"))
        ax3.legend(fontsize=6)
        ax3.grid(True, alpha=0.3)

        # Subplot 4: Efficiency and stability
        ax4 = axes[1, 1]
        ax4_twin = ax4.twinx()
        rps_vals = [cs.get("request_throughput_rps", 0) or None for cs in s]
        success_rates = [cs.get("success_rate") or None for cs in s]
        pr_tps = [cs.get("per_request_output_tps_avg", 0) or None for cs in s]
        baseline_tps = s[0].get("system_output_tps", 0) or 0 if s else 0
        eff_vals = []
        for i, cs in enumerate(s):
            c_val = conc[i]
            tps_val = cs.get("system_output_tps", 0) or 0
            if baseline_tps > 0 and c_val > 0 and tps_val > 0:
                eff_vals.append(tps_val / (baseline_tps * c_val))
            else:
                eff_vals.append(None)

        ax4.plot(conc, rps_vals, "o-", color="#533AFD", linewidth=1.5,
                 label="RPS")
        ax4.plot(conc, pr_tps, "s--", color="#10B981", linewidth=1.5,
                 label="Per-req TPS")
        if any(v is not None for v in eff_vals):
            ax4.plot(conc, eff_vals, "D:", color="#EF4444", linewidth=1.5,
                     label="效率")
        ax4.set_xlabel(self.tr("chart.concurrency_axis"))
        ax4.set_ylabel("RPS / Per-req TPS", color="#533AFD")
        ax4_twin.plot(conc, success_rates, "v--", color="#F59E0B", linewidth=2,
                      label="成功率 (%)")
        ax4_twin.set_ylabel(self.tr("chart.success_rate_axis"), color="#F59E0B")
        ax4.set_title(self.tr("chart.efficiency_stability"))
        lines1, labels1 = ax4.get_legend_handles_labels()
        lines2, labels2 = ax4_twin.get_legend_handles_labels()
        ax4.legend(lines1 + lines2, labels1 + labels2, fontsize=6, loc="upper left")
        ax4.grid(True, alpha=0.3)

        plt.tight_layout()
        return fig

    def _clear_sweep_chart(self):
        """Destroy any existing embedded chart canvas and close the figure."""
        if getattr(self, "_sweep_chart_canvas", None) is not None:
            try:
                self._sweep_chart_canvas.get_tk_widget().destroy()
            except Exception:
                pass
            self._sweep_chart_canvas = None
        if getattr(self, "_sweep_chart_figure", None) is not None:
            try:
                import matplotlib.pyplot as plt
                plt.close(self._sweep_chart_figure)
            except Exception:
                pass
            self._sweep_chart_figure = None
        # Clear chart status
        if hasattr(self, "sweep_chart_status_var"):
            self.sweep_chart_status_var.set("")

    def _render_sweep_chart(self, sweep_result):
        """Embed a 2x2 sweep analysis chart in the sweep tab.
        Must be called on the main thread."""
        self._clear_sweep_chart()

        if not self._matplotlib_available():
            self.sweep_chart_status_var.set(
                "matplotlib 未安装，无法在界面显示图表。请安装 matplotlib 后重试。")
            return

        try:
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        except Exception:
            self.sweep_chart_status_var.set(
                "matplotlib 未安装，无法在界面显示图表。请安装 matplotlib 后重试。")
            return

        fig = self._build_sweep_analysis_figure(sweep_result)
        if fig is None:
            self.sweep_chart_status_var.set("暂无数据，无法生成图表。")
            return

        self._sweep_chart_figure = fig
        self._sweep_chart_canvas = FigureCanvasTkAgg(fig, master=self.sweep_chart_frame)
        self._sweep_chart_canvas.draw()

        widget = self._sweep_chart_canvas.get_tk_widget()
        widget.pack(fill="both", expand=True)

        if getattr(LLMBenchmarkApp, "_last_cjk_font_available", True):
            self.sweep_chart_status_var.set("✓ 图表已生成")
        else:
            self.sweep_chart_status_var.set("⚠ 未检测到 CJK 字体，中文图表可能显示异常。")

    def _get_sweep_run_dir(self, sweep_result: dict) -> str:
        """Get or create the structured run dir for this sweep."""
        if hasattr(self, "_current_sweep_run_dir") and self._current_sweep_run_dir:
            return self._current_sweep_run_dir
        results_root = getattr(self, "results_root_var", tk.StringVar(value=RESULTS_ROOT)).get() or RESULTS_ROOT
        env_info = self._get_active_env_info()
        hw = env_info.get("hw", {})
        sw = env_info.get("sw", {})
        gpu_slug = _make_slug(hw.get("gpu_model") or hw.get("gpu_model_custom") or "unknown")
        gpu_count = hw.get("gpu_count") or hw.get("gpu_count_custom") or "1"
        backend_slug = _make_slug(sw.get("backend") or sw.get("backend_custom") or "unknown")
        model_slug = _make_slug(sweep_result.get("model", "unknown"))
        levels = sweep_result.get("concurrency_levels", [])
        levels_slug = "-".join(f"C{c}" for c in levels) if levels else "unknown"
        mt = sweep_result.get("fixed_output_tokens") or 0
        mode = sweep_result.get("output_length_mode", "normal")
        params_slug = f"sweep__out{mt}-{_make_slug(mode)}__{levels_slug}"
        run_dir = _make_run_dir(results_root, "sweeps", model_slug, backend_slug,
                                gpu_slug, str(gpu_count), params_slug)
        self._current_sweep_run_dir = run_dir
        return run_dir

    def _export_sweep_json(self, sweep_result: dict) -> str:
        """Export sweep result to JSON file. Returns the file path."""
        run_dir = self._get_sweep_run_dir(sweep_result)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = os.path.join(run_dir, f"result_{ts}.json")
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(sweep_result, f, indent=2, ensure_ascii=False,
                         default=str)
            return json_path
        except Exception as e:
            self._append_sweep_status(f"✕ JSON 导出失败: {e}\n")
            return ""

    def _export_sweep_markdown(self, sweep_result: dict) -> str:
        """Export sweep analysis as Markdown report. Returns the file path."""
        run_dir = self._get_sweep_run_dir(sweep_result)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        md_path = os.path.join(run_dir, f"report_{ts}.md")
        try:
            report = self._generate_sweep_markdown_report(sweep_result)
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(report)
            self._append_sweep_status(f"✓ Markdown 报告已导出: {md_path}\n")
            return md_path
        except Exception as e:
            self._append_sweep_status(f"✕ Markdown 报告导出失败: {e}\n")
            return ""

    def _export_sweep_png(self, sweep_result: dict) -> str | None:
        """Export sweep trend report as PNG using the shared figure builder.
        Returns path or None if skipped."""
        if not self._matplotlib_available():
            self._append_sweep_status("⚠ matplotlib unavailable, PNG report skipped.\n")
            return None

        cases = sweep_result.get("cases", [])
        if not cases:
            return None

        fig = self._build_sweep_analysis_figure(sweep_result)
        if fig is None:
            return None
        if not getattr(LLMBenchmarkApp, "_last_cjk_font_available", True):
            self._append_sweep_status("⚠ 未检测到 CJK 字体，PNG 中文可能显示异常。\n")

        run_dir = self._get_sweep_run_dir(sweep_result)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        png_path = os.path.join(run_dir, f"chart_{ts}.png")
        try:
            fig.savefig(png_path, dpi=150, bbox_inches="tight")
            import matplotlib.pyplot as plt
            plt.close(fig)
            return png_path
        except Exception as e:
            try:
                import matplotlib.pyplot as plt
                plt.close(fig)
            except Exception:
                pass
            self._append_sweep_status(f"✕ PNG 生成失败: {e}\n")
            return None

    def _load_result_db_runs(self) -> list:
        """Load benchmark_runs from the result DB, newest first."""
        db_path = getattr(self, "result_db_path_var",
                          tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
        try:
            if not os.path.exists(db_path):
                return []
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT br.*, bm.output_token_throughput, bm.ttft_p95, bm.e2el_p95
                   FROM benchmark_runs br
                   LEFT JOIN benchmark_metrics bm ON bm.run_id = br.run_id
                   ORDER BY br.id DESC""").fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def _refresh_history(self):
        for item in self.hist_tree.get_children():
            self.hist_tree.delete(item)
        self.history_item_records = {}
        self.history_display_refs = []

        selected_filter = getattr(self, "history_type_filter_var",
                                  tk.StringVar(value="All")).get()
        visible_count = 0
        _seen_run_ids: set = set()

        # ── ResultStore runs (results/runs/*/summary.json) — FIRST SOURCE ──
        _rs_root = (getattr(self, "results_root_var", None) or
                    tk.StringVar(value=RESULTS_ROOT)).get() or RESULTS_ROOT
        try:
            rs_runs = rs_list_runs(_rs_root)
        except Exception:
            rs_runs = []
        for s in rs_runs:
            run_type = s.get("run_type", "single")
            if selected_filter == "Single" and run_type != "single":
                continue
            if selected_filter == "Sweep" and run_type != "sweep":
                continue
            run_id = s.get("run_id", "")
            _seen_run_ids.add(run_id)
            run_dir = s.get("run_dir", s.get("_run_dir", ""))
            created = s.get("created_at", "")
            model   = s.get("model", "-") or "-"
            env_name = s.get("environment_profile_name") or "未指定环境"
            hardware = s.get("hardware") or "-"
            backend  = s.get("backend") or "-"
            workload = s.get("workload", "")
            out_tps  = s.get("output_token_throughput_tok_s") or s.get("peak_output_token_throughput_tok_s")
            ttft_ms  = s.get("mean_ttft_ms")
            e2e_ms   = s.get("p95_e2e_latency_ms")
            if run_type == "sweep":
                type_label = self.tr("history.sweep")
                conc = s.get("concurrency", 0)
                config = f"sweep / {conc} levels"
            else:
                type_label = self.tr("history.single")
                config = workload or f"C{s.get('concurrency',0)}/N{s.get('total_requests',0)}"
            iid = f"rs_{run_id}"
            self.hist_tree.insert("", tk.END, iid=iid,
                                  values=(
                                      f"S:{run_id[-8:] if len(run_id) > 8 else run_id}",
                                      created, type_label, model,
                                      env_name, hardware, backend, "-",
                                      config,
                                      f"{out_tps:.1f}" if out_tps is not None else "-",
                                      f"{ttft_ms:.0f}ms" if ttft_ms is not None else "-",
                                      f"{e2e_ms:.0f}ms" if e2e_ms is not None else "-",
                                      "completed",
                                  ), tags=("result_store",))
            _ref = {
                "source": "result_store",
                "run_id": run_id,
                "run_dir": run_dir,
                "run_type": run_type,
                "index": len(self.history_display_refs),
                "created_at": created,
                "model": model,
                "summary": s,
            }
            self.history_item_records[iid] = _ref
            self.history_display_refs.append(_ref)
            visible_count += 1

        # ── Legacy result DB rows (newest first) ──
        result_rows = self._load_result_db_runs()
        for row in result_rows:
            run_type = row.get("run_type") or "single"
            if selected_filter == "Single" and run_type != "single":
                continue
            if selected_filter == "Sweep" and run_type != "sweep":
                continue
            run_id = row.get("run_id", "")
            _seen_run_ids.add(run_id)
            created = row.get("created_at", "")
            model = row.get("model_name") or row.get("model", "") or "-"
            env_name = row.get("environment_profile_name_snapshot") or "未指定环境"
            gpu_model = row.get("gpu_model_snapshot") or "-"
            gpu_count = row.get("gpu_count_snapshot") or ""
            gpu_str = f"{gpu_model} x{gpu_count}" if gpu_count else gpu_model
            backend = row.get("backend_snapshot") or "-"
            quant = row.get("quantization_snapshot") or "-"
            conc = row.get("concurrency") or 0
            total_req = row.get("total_requests") or 0
            status = row.get("status") or "completed"
            out_tps = row.get("output_token_throughput")
            ttft_p95 = row.get("ttft_p95")
            e2el_p95 = row.get("e2el_p95")
            if run_type == "sweep":
                type_label = self.tr("history.sweep")
                levels = row.get("concurrency") or 0
                config = f"sweep / {levels} levels"
            else:
                type_label = self.tr("history.single")
                config = f"C{conc}/N{total_req}"
            # tag: "result_db:<run_id>"
            self.hist_tree.insert("", tk.END, iid=f"rdb_{run_id}",
                                  values=(
                                      f"R:{run_id[-6:] if len(run_id) > 6 else run_id}",
                                      created, type_label, model,
                                      env_name, gpu_str, backend, quant,
                                      config,
                                      f"{out_tps:.1f}" if out_tps is not None else "-",
                                      f"{ttft_p95:.3f}s" if ttft_p95 is not None else "-",
                                      f"{e2el_p95:.3f}s" if e2el_p95 is not None else "-",
                                      status,
                                  ), tags=("result_db",))
            _ref = {
                "source": "result_db", "run_id": run_id, "run_type": run_type,
                "index": len(self.history_display_refs),
                "created_at": created, "model": model,
            }
            self.history_item_records[f"rdb_{run_id}"] = _ref
            self.history_display_refs.append(_ref)
            visible_count += 1

        # ── Legacy history DB rows ──
        legacy_rows = load_history()
        for row in legacy_rows:
            rid = row["id"]
            created = row["created_at"]
            record_type = row["record_type"] or "single"
            if selected_filter == "Single" and record_type != "single":
                continue
            if selected_filter == "Sweep" and record_type != "sweep":
                continue
            model = row["model"] or "-"
            conc = row["concurrency"]
            total = row["total"]
            e2e_p95 = self._row_get(row, "e2e_latency_p95")
            sys_tps = self._row_get(row, "system_output_tps")
            status = self._row_get(row, "status") or "completed"
            ttft_p95_val = self._row_get(row, "ttft_p95")
            if record_type == "sweep":
                type_label = self.tr("history.sweep")
                config_summary = row["config_summary"] or "-"
            else:
                type_label = self.tr("history.single")
                config_summary = row["config_summary"] or f"C{conc}/N{total}"
            self.hist_tree.insert("", tk.END, iid=f"leg_{rid}",
                                  values=(
                                      rid, created, type_label, model,
                                      "未指定环境", "-", "-", "-",
                                      config_summary,
                                      f"{sys_tps:.1f}" if sys_tps is not None else "-",
                                      f"{ttft_p95_val:.3f}s" if ttft_p95_val is not None else "-",
                                      f"{e2e_p95:.3f}s" if e2e_p95 is not None else "-",
                                      status,
                                  ), tags=("legacy",))
            _ref = {
                "source": "legacy", "rid": rid, "run_type": record_type,
                "index": len(self.history_display_refs),
                "created_at": created, "model": model,
            }
            self.history_item_records[f"leg_{rid}"] = _ref
            self.history_display_refs.append(_ref)
            visible_count += 1

        if visible_count:
            self.history_status_var.set(
                self.tr("status.history_count").format(count=visible_count))
            self._set_history_detail_text(self.tr("status.select_history"))
        else:
            self.history_status_var.set(self.tr("status.no_history"))
            self._set_history_detail_text(self.tr("status.no_history"))

    def _set_history_detail_text(self, text: str):
        if not hasattr(self, "history_detail_text"):
            return
        self.history_detail_text.config(state=tk.NORMAL)
        self.history_detail_text.delete("1.0", tk.END)
        self.history_detail_text.insert(tk.END, text)
        self.history_detail_text.config(state=tk.DISABLED)

    def _load_history_row(self, rid):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM benchmarks WHERE id=?",
                           (rid,)).fetchone()
        conn.close()
        return row

    def _on_history_select(self, event=None):
        sel = self.hist_tree.selection()
        if not sel:
            self._set_history_detail_text(self.tr("status.select_history"))
            return
        iid = sel[0]
        tags = self.hist_tree.item(iid, "tags")
        values = self.hist_tree.item(iid, "values")

        # ── ResultStore row — show report.txt directly ──
        if "result_store" in tags:
            ref = self.history_item_records.get(iid, {})
            run_dir = ref.get("run_dir", "")
            s = ref.get("summary", {})
            lines = []
            lines.append(f"Run ID:   {ref.get('run_id', '-')}")
            lines.append(f"Time:     {ref.get('created_at', '-')}")
            lines.append(f"Model:    {ref.get('model', '-')}")
            lines.append(f"Workload: {s.get('workload', '-')}")
            lines.append(f"Results:  {run_dir}")
            out_tps = s.get("output_token_throughput_tok_s") or s.get("peak_output_token_throughput_tok_s")
            if out_tps:
                lines.append(f"Output TPS: {out_tps:.1f} tok/s")
            lines.append("")
            # Try to show report.txt
            if run_dir:
                try:
                    rpt = rs_load_report(run_dir, preferred="txt")
                    if rpt:
                        # Truncate for preview panel (show first ~3500 chars)
                        preview = rpt[:3500]
                        if len(rpt) > 3500:
                            preview += f"\n\n... [报告共 {len(rpt)} 字符，双击查看完整内容] ..."
                        lines.append(preview)
                    else:
                        lines.append("⚠ 文本报告缺失，可从 result.json 重新生成。")
                        lines.append("  双击该行后点击「重新生成报告」。")
                except Exception:
                    lines.append("⚠ 读取报告失败。")
            self._set_history_detail_text("\n".join(lines))
            return

        if "result_db" in tags:
            # ── Result DB row ──
            run_id_display = values[0]  # "R:xxxxxx"
            # Resolve full run from result DB
            db_path = getattr(self, "result_db_path_var",
                              tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
            try:
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                # get rid from iid "rdb_<run_id>"
                real_run_id = iid[4:] if iid.startswith("rdb_") else ""
                row = conn.execute(
                    """SELECT br.*, bm.output_token_throughput, bm.ttft_p95,
                              bm.e2el_p95, bm.e2el_avg, bm.ttft_avg
                       FROM benchmark_runs br
                       LEFT JOIN benchmark_metrics bm ON bm.run_id = br.run_id
                       WHERE br.run_id=?""", (real_run_id,)).fetchone()
                conn.close()
                row = dict(row) if row else {}
            except Exception:
                row = {}
            # Build env summary from snapshots
            env_name = row.get("environment_profile_name_snapshot") or "未指定环境"
            hw_name  = row.get("hardware_profile_name_snapshot") or "-"
            sw_name  = row.get("software_stack_profile_name_snapshot") or "-"
            mod_name = row.get("model_profile_name_snapshot") or "-"
            gpu_model = row.get("gpu_model_snapshot") or "-"
            gpu_count = row.get("gpu_count_snapshot") or "-"
            backend   = row.get("backend_snapshot") or "-"
            bk_ver    = row.get("backend_version_snapshot") or "-"
            api_type  = row.get("api_type_snapshot") or "-"
            deploy    = row.get("deployment_type_snapshot") or "-"
            parser    = row.get("reasoning_parser_snapshot") or "-"
            mfamily   = row.get("model_family_snapshot") or "-"
            msize     = row.get("model_size_snapshot") or "-"
            quant     = row.get("quantization_snapshot") or "-"
            mtype     = row.get("model_type_snapshot") or "-"
            run_type  = row.get("run_type") or "single"
            lines = [
                "═══ 环境档案 (Environment Summary) ═══",
                f"  Environment Profile: {env_name}",
                f"  Hardware Profile:    {hw_name}",
                f"  GPU:                 {gpu_model}  x{gpu_count}",
                f"  Software Stack:      {sw_name}",
                f"  Backend:             {backend} {bk_ver}",
                f"  API Type:            {api_type}",
                f"  Deployment:          {deploy}",
                f"  Reasoning Parser:    {parser}",
                f"  Model Profile:       {mod_name}",
                f"  API Model Name:      {row.get('model_name', '-')}",
                f"  Model Family/Size:   {mfamily} {msize}",
                f"  Quantization:        {quant}",
                f"  Model Type:          {mtype}",
            ]
            if not row.get("environment_profile_name_snapshot"):
                lines.append("  ⚠ 未绑定环境档案，本次结果不建议用于长期横向对比。")
            lines.append("")
            lines.append("═══ 测试结果 ═══")
            lines.append(f"  Run ID:   {row.get('run_id', '-')}")
            lines.append(f"  Time:     {row.get('created_at', '-')}")
            lines.append(f"  Type:     {run_type}")
            lines.append(f"  Model:    {row.get('model_name', '-')}")
            lines.append(f"  API URL:  {row.get('api_url', '-')}")
            lines.append(f"  Status:   {row.get('status', '-')}")
            out_tps  = row.get("output_token_throughput")
            ttft_p95 = row.get("ttft_p95")
            e2el_p95 = row.get("e2el_p95")
            e2el_avg = row.get("e2el_avg")
            lines.append(f"  Output TPS: {out_tps:.1f} tok/s" if out_tps is not None else "  Output TPS: -")
            lines.append(f"  TTFT P95:   {ttft_p95:.3f}s" if ttft_p95 is not None else "  TTFT P95: -")
            lines.append(f"  E2E Avg:    {e2el_avg:.3f}s" if e2el_avg is not None else "  E2E Avg: -")
            lines.append(f"  E2E P95:    {e2el_p95:.3f}s" if e2el_p95 is not None else "  E2E P95: -")
            if run_type == "sweep":
                lines.append(f"  Report Dir: {row.get('report_dir', '-')}")
                lines.append("")
                lines.append("双击该行查看完整扫测详情。" if self.lang_code == "zh_CN"
                             else "Double-click this row to view full sweep details.")
            else:
                lines.append(f"  Report Dir: {row.get('report_dir', '-')}")
                lines.append("")
                lines.append("双击该行查看完整单次测试详情。" if self.lang_code == "zh_CN"
                             else "Double-click this row to view full benchmark details.")
            self._set_history_detail_text("\n".join(lines))
            return

        # ── Legacy history DB row ──
        rid_str = values[0]
        try:
            rid = int(rid_str)
        except (ValueError, TypeError):
            rid = 0
        row = self._load_history_row(rid)
        if not row:
            self._set_history_detail_text("历史记录不存在或已删除")
            return
        record_type = row["record_type"] or "single"
        if record_type == "sweep":
            try:
                sweep_result = json.loads(row["summary_json"] or "{}")
            except Exception:
                sweep_result = {}
            cases = sweep_result.get("cases", [])
            lines = [
                "  ⚠ 未绑定环境档案，本次结果不建议用于长期横向对比。",
                "",
                f"{self.tr('history.type')}: {self.tr('history.sweep')}",
                f"{self.tr('history.time')}: {row['created_at']}",
                f"{self.tr('history.model')}: {row['model']}",
                f"{self.tr('history.config')}: {row['config_summary'] or '-'}",
                f"{self.tr('history.key_result')}: {row['primary_metric'] or '-'}",
                f"{self.tr('history.status')}: {row['status'] or '-'}",
                f"{'档位数' if self.lang_code == 'zh_CN' else 'Cases'}: {len(cases)}",
                f"JSON: {row['json_path'] or '未保存'}",
                f"Markdown: {row['markdown_path'] or '未保存'}",
                f"PNG: {row['png_path'] or '未保存'}",
                "",
                "双击该行可查看并发档位表、2x2 图形分析、专家分析和 Raw JSON。"
                if self.lang_code == "zh_CN"
                else "Double-click this row to view case table, 2x2 chart, expert summary, and Raw JSON.",
            ]
        else:
            config_summary = row["config_summary"] or f"C{row['concurrency']} / N{row['total']}"
            lines = [
                "  ⚠ 未绑定环境档案，本次结果不建议用于长期横向对比。",
                "",
                f"{self.tr('history.type')}: {self.tr('history.single')}",
                f"{self.tr('history.time')}: {row['created_at']}",
                f"{self.tr('history.model')}: {row['model']}",
                f"{self.tr('history.config')}: {config_summary}",
                f"{self.tr('history.key_result')}: {row['primary_metric'] or '-'}",
                f"{self.tr('history.status')}: {row['status'] or '-'}",
                f"E2E Avg: {row['e2e_latency_avg']:.3f}s" if row["e2e_latency_avg"] else "E2E Avg: -",
                f"E2E P95: {row['e2e_latency_p95']:.3f}s" if row["e2e_latency_p95"] else "E2E P95: -",
                f"Output TPS: {row['system_output_tps']:.1f}" if row["system_output_tps"] else "Output TPS: -",
                "",
                "双击该行可查看完整单次测试详情和 E2E latency chart。"
                if self.lang_code == "zh_CN"
                else "Double-click this row to view full single benchmark details and E2E latency chart.",
            ]
        self._set_history_detail_text("\n".join(lines))

    def _clear_history(self):
        if not messagebox.askyesno(self.tr("msg.confirm"), self.tr("msg.clear_history_confirm")):
            return
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM benchmarks")
        conn.commit()
        conn.close()
        self._refresh_history()

    def _compare_selected_runs(self):
        """Open a comparison popup for the 2+ selected history rows (ResultStore rows only)."""
        zh = (self.lang_code == "zh_CN")
        sel = self.hist_tree.selection()
        # Collect ResultStore summaries for selected rows
        summaries = []
        labels    = []
        for iid in sel:
            ref = self.history_item_records.get(iid, {})
            if ref.get("source") == "result_store":
                s = ref.get("summary", {})
                summaries.append(s)
                labels.append(f"{s.get('created_at','')[:16]}  {s.get('model','-')[:20]}"
                              f"  {s.get('workload','-')}")
        if len(summaries) < 2:
            messagebox.showinfo(
                "提示" if zh else "Info",
                "请在历史列表中选择 2 条或以上的 ResultStore 记录进行对比。\n"
                "（按住 Ctrl/Shift 多选）"
                if zh else
                "Select 2 or more ResultStore runs (Ctrl/Shift+click) to compare."
            )
            return

        compare_rows = rs_compare_runs(summaries)

        win = tk.Toplevel(self.root)
        win.title("性能对比" if zh else "Performance Comparison")
        win.geometry("900x560")
        win.configure(bg=C_STYLE["bg_main"])

        hdr_frame = tk.Frame(win, bg=C_STYLE["bg_card"],
                             highlightbackground=C_STYLE["border"],
                             highlightthickness=1)
        hdr_frame.pack(fill=tk.X, padx=C_STYLE["pad_lg"],
                       pady=(C_STYLE["pad_lg"], C_STYLE["gap_sm"]))
        tk.Label(hdr_frame,
                 text=("对比说明:\n"
                       f"  基准 (Baseline): {labels[0]}\n"
                       f"  当前 (Current):  {labels[1]}"),
                 justify=tk.LEFT, font=C_STYLE["font_small"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"],
                 padx=C_STYLE["pad_md"], pady=C_STYLE["pad_sm"]).pack(anchor="w")

        cols = ("指标" if zh else "Metric",
                "基准" if zh else "Baseline",
                "当前" if zh else "Current",
                "变化" if zh else "Delta",
                "变化%" if zh else "Δ%",
                "趋势" if zh else "Trend")
        tv = ttk.Treeview(win, columns=cols, show="headings",
                          height=min(len(compare_rows), 14),
                          style="App.Treeview")
        cw = {
            "指标" if zh else "Metric": 200,
            "基准" if zh else "Baseline": 110,
            "当前" if zh else "Current": 110,
            "变化" if zh else "Delta": 100,
            "变化%" if zh else "Δ%": 80,
            "趋势" if zh else "Trend": 80,
        }
        for c in cols:
            tv.heading(c, text=c)
            tv.column(c, width=cw.get(c, 90), anchor="center")
        tv.pack(fill=tk.BOTH, expand=True,
                padx=C_STYLE["pad_lg"], pady=(C_STYLE["gap_sm"], C_STYLE["pad_lg"]))

        for row in compare_rows:
            unit = row.get("unit", "")
            bv   = row.get("baseline")
            cv   = row.get("current")
            d    = row.get("delta")
            pct  = row.get("pct_change")
            dir_ = row.get("direction", "neutral")

            def _fmt(v):
                if v is None: return "—"
                return f"{v:.2f} {unit}".rstrip()

            trend = {"better": "✓ 改善" if zh else "✓ Better",
                     "worse":  "✗ 退步" if zh else "✗ Worse",
                     "neutral": "→ 持平" if zh else "→ Neutral"}.get(dir_, "—")
            tag = dir_
            tv.insert("", tk.END,
                      values=(row["label"], _fmt(bv), _fmt(cv),
                              _fmt(d), f"{pct:+.1f}%" if pct is not None else "—",
                              trend),
                      tags=(tag,))

        tv.tag_configure("better",  foreground="#059669")  # green
        tv.tag_configure("worse",   foreground="#DC2626")  # red
        tv.tag_configure("neutral", foreground=C_STYLE["text_muted"])

        ttk.Button(win, text="关闭" if zh else "Close",
                   style="Secondary.TButton",
                   command=win.destroy).pack(side=tk.BOTTOM,
                                             pady=C_STYLE["pad_md"])

    def _draw_popup_histogram(self, canvas: tk.Canvas, latencies: list[float]):
        """Draw a latency distribution histogram on the given canvas."""
        canvas.delete("all")
        if not latencies:
            canvas.create_text(200, 80, text=self.tr("chart.hist_empty"),
                               font=C_STYLE["font_body"],
                               fill=C_STYLE["text_secondary"])
            return
        canvas.update_idletasks()
        w = max(canvas.winfo_width(), 100)
        h = max(canvas.winfo_height(), 80)
        if w < 100 or h < 80:
            canvas.create_text(
                max(w // 2, 50), max(h // 2, 40),
                text=self.tr("chart.hist_empty"),
                font=C_STYLE["font_small"],
                fill=C_STYLE["text_secondary"])
            return

        # 卡片标题已显示图表名，画布内不再重复标题（规范 §3.5）
        margin_l, margin_r, margin_t, margin_b = 58, 26, 26, 48
        plot_w = w - margin_l - margin_r
        plot_h = h - margin_t - margin_b
        if plot_w < 20 or plot_h < 20:
            canvas.create_text(w / 2, h / 2, text=self.tr("chart.hist_empty"),
                               font=C_STYLE["font_small"],
                               fill=C_STYLE["text_secondary"])
            return

        min_l, max_l = min(latencies), max(latencies)
        if max_l == min_l:
            delta = max(min_l * 0.05, 0.001)
            min_l = max(0.0, min_l - delta)
            max_l = max_l + delta
        bin_count = min(25, max(1, min(8, len(latencies)) if len(latencies) < 12 else len(latencies) // 2))
        bin_w = (max_l - min_l) / bin_count
        bins = [0] * bin_count
        for lat in latencies:
            idx = min(int((lat - min_l) / bin_w), bin_count - 1)
            bins[idx] += 1
        max_bin = max(bins) or 1
        # draw bars
        for i, count in enumerate(bins):
            x0 = margin_l + i * plot_w / bin_count
            x1 = margin_l + (i + 1) * plot_w / bin_count - 2
            bar_h = count / max_bin * max(plot_h - 8, 1)
            y0 = margin_t + plot_h - bar_h
            y1 = margin_t + plot_h
            ratio = i / max(bin_count - 1, 1)
            # 品牌色调渐变（accent_light→accent），不使用紫色/彩虹色（规范 §2.3）
            if ratio < 0.25:
                color = C_STYLE["accent_soft"]
            elif ratio < 0.5:
                color = C_STYLE["accent_light"]
            elif ratio < 0.75:
                color = C_STYLE["accent"]
            else:
                color = C_STYLE["accent_pressed"]
            canvas.create_rectangle(x0, y0, x1, y1, fill=color,
                                    outline="", width=0)
            if count > 0:
                canvas.create_text((x0 + x1) / 2, y0 - 10,
                                   text=str(count),
                                   font=C_STYLE["font_small"],
                                   fill=C_STYLE["text_primary"])
        # axes
        canvas.create_line(margin_l, margin_t + plot_h,
                           margin_l + plot_w, margin_t + plot_h,
                           fill=C_STYLE["border"], width=1)
        canvas.create_line(margin_l, margin_t, margin_l,
                           margin_t + plot_h,
                           fill=C_STYLE["border"], width=1)
        # y-axis tick labels
        for tick in range(0, max_bin + 1, max(1, max_bin // 3 or 1)):
            y = margin_t + plot_h - (tick / max_bin * max(plot_h - 8, 1))
            canvas.create_line(margin_l - 4, y, margin_l, y,
                               fill=C_STYLE["border"], width=1)
            canvas.create_text(margin_l - 8, y, text=str(tick),
                               anchor="e", font=C_STYLE["font_small"],
                               fill=C_STYLE["text_secondary"])
        # x-axis labels
        for i in range(0, bin_count + 1, max(1, bin_count // 5)):
            x = margin_l + i * plot_w / bin_count
            val = min_l + i * bin_w
            canvas.create_text(x, margin_t + plot_h + 14,
                               text=f"{val:.2f}s",
                               font=C_STYLE["font_small"],
                               fill=C_STYLE["text_secondary"])
        canvas.create_text(w / 2, h - 8, text=self.tr("chart.hist_latency_axis"),
                           font=C_STYLE["font_small"],
                           fill=C_STYLE["text_secondary"])
        canvas.create_text(14, h / 2, text=self.tr("chart.hist_count_axis"), angle=90,
                           font=C_STYLE["font_small"],
                           fill=C_STYLE["text_secondary"])
        avg = statistics.mean(latencies)
        p95 = percentile(latencies, 95)
        p99 = percentile(latencies, 99)
        stat_text = (
            f"n={len(latencies)}  min={min(latencies):.3f}s  "
            f"avg={avg:.3f}s  p95={p95:.3f}s  p99={p99:.3f}s")
        canvas.create_text(margin_l, 14, text=stat_text, anchor="w",
                           font=C_STYLE["font_small"],
                           fill=C_STYLE["text_secondary"])

    def _show_sweep_history_detail(self, rid, row):
        try:
            sweep_result = json.loads(row["summary_json"] or "{}")
        except Exception:
            sweep_result = {}
        cases = sweep_result.get("cases", [])
        top = tk.Toplevel(self.root)
        top.title(f"扫测详情  #{rid}")
        top.geometry("920x760")
        top.configure(bg=C_STYLE["bg_main"])
        top.minsize(760, 560)
        top.grid_columnconfigure(0, weight=1)
        top.grid_rowconfigure(0, weight=1)

        scroll = ScrollableFrame(top, bg=C_STYLE["bg_main"])
        scroll.grid(row=0, column=0, sticky="nsew")
        inner = scroll.content
        inner.configure(bg=C_STYLE["bg_main"])
        inner.grid_columnconfigure(0, weight=1)

        overview = SectionCard(inner, "扫测概览", collapsible=True, expanded=True)
        overview.grid(row=0, column=0, sticky="ew", padx=C_STYLE["pad_lg"],
                      pady=(C_STYLE["pad_lg"], C_STYLE["gap_lg"]))
        ov = overview.content
        ov_text = (
            f"模型: {row['model']}\n"
            f"API: {row['api_url']}\n"
            f"配置: {row['config_summary'] or '-'}\n"
            f"核心结果: {row['primary_metric'] or '-'}\n"
            f"状态: {row['status'] or '-'}"
        )
        tk.Label(ov, text=ov_text, justify=tk.LEFT, anchor="w",
                 font=C_STYLE["font_body"], bg=C_STYLE["bg_card"],
                 fg=C_STYLE["text_primary"]).pack(fill=tk.X)

        table = SectionCard(inner, "并发档位明细", collapsible=True, expanded=True)
        table.grid(row=1, column=0, sticky="ew", padx=C_STYLE["pad_lg"],
                   pady=(0, C_STYLE["gap_lg"]))
        cols = ("C", "Req", "Success", "Fail", "Success Rate", "E2E P95", "Output TPS", "RPS")
        tv = ttk.Treeview(table.content, columns=cols, show="headings",
                          height=min(max(len(cases), 3), 8), style="App.Treeview")
        for c in cols:
            tv.heading(c, text=c)
            tv.column(c, anchor="center", width=95)
        tv.pack(fill=tk.X)
        for case in cases:
            s = case.get("benchmark_summary", {})
            tv.insert("", tk.END, values=(
                case.get("concurrency", "-"),
                case.get("total_requests", "-"),
                s.get("success", 0),
                s.get("fail", 0),
                f"{s.get('success_rate', 0) or 0:.0f}%",
                f"{s.get('e2e_latency_p95', 0) or 0:.3f}s",
                f"{s.get('system_output_tps', 0) or 0:.1f}",
                f"{s.get('request_throughput_rps', 0) or 0:.2f}",
            ))

        chart = SectionCard(inner, "图形分析", collapsible=True, expanded=True)
        chart.grid(row=2, column=0, sticky="ew", padx=C_STYLE["pad_lg"],
                   pady=(0, C_STYLE["gap_lg"]))
        chart_frame = tk.Frame(chart.content, bg=C_STYLE["bg_card"], height=460)
        chart_frame.pack(fill=tk.BOTH, expand=True)
        chart_frame.pack_propagate(False)
        if self._matplotlib_available() and cases:
            try:
                from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
                fig = self._build_sweep_analysis_figure(sweep_result)
                if fig is not None:
                    canvas = FigureCanvasTkAgg(fig, master=chart_frame)
                    canvas.draw()
                    canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
                    top._sweep_hist_chart_canvas = canvas
                    top._sweep_hist_chart_figure = fig
                else:
                    raise RuntimeError("暂无数据，无法生成图表")
            except Exception as e:
                tk.Label(chart_frame, text=f"matplotlib 图表不可用: {e}",
                         font=C_STYLE["font_body"], bg=C_STYLE["bg_card"],
                         fg=C_STYLE["warning_text"], wraplength=700).pack(expand=True)
        else:
            tk.Label(chart_frame, text="matplotlib 未安装或暂无数据，无法显示图表。",
                     font=C_STYLE["font_body"], bg=C_STYLE["bg_card"],
                     fg=C_STYLE["warning_text"], wraplength=700).pack(expand=True)

        expert = SectionCard(inner, "专家分析简评", collapsible=True, expanded=True)
        expert.grid(row=3, column=0, sticky="ew", padx=C_STYLE["pad_lg"],
                    pady=(0, C_STYLE["gap_lg"]))
        lines = sweep_result.get("analysis_summary") or []
        commentary = self._generate_expert_commentary(cases, lines) if cases else ""
        expert_text = "\n".join([*lines, "", commentary]).strip() or "未生成专家分析。"
        tk.Label(expert.content, text=expert_text, justify=tk.LEFT, anchor="w",
                 font=C_STYLE["font_body"], bg=C_STYLE["bg_card"],
                 fg=C_STYLE["text_primary"], wraplength=820).pack(fill=tk.X)

        paths = SectionCard(inner, "输出文件", collapsible=True, expanded=True)
        paths.grid(row=4, column=0, sticky="ew", padx=C_STYLE["pad_lg"],
                   pady=(0, C_STYLE["gap_lg"]))
        path_text = (
            f"JSON: {row['json_path'] or '未保存'}\n"
            f"Markdown: {row['markdown_path'] or '未保存'}\n"
            f"PNG: {row['png_path'] or '未保存'}"
        )
        tk.Label(paths.content, text=path_text, justify=tk.LEFT, anchor="w",
                 font=C_STYLE["font_body"], bg=C_STYLE["bg_card"],
                 fg=C_STYLE["text_primary"]).pack(fill=tk.X)

        raw = SectionCard(inner, "Raw JSON", collapsible=True, expanded=False)
        raw.grid(row=5, column=0, sticky="ew", padx=C_STYLE["pad_lg"],
                 pady=(0, C_STYLE["pad_lg"]))
        txt = tk.Text(raw.content, height=12, wrap=tk.WORD, font=C_STYLE["font_code"],
                      bg=C_STYLE["bg_input"], fg=C_STYLE["text_primary"],
                      relief=tk.FLAT, borderwidth=0)
        txt.insert(tk.END, json.dumps(sweep_result, ensure_ascii=False, indent=2, default=str))
        txt.config(state=tk.DISABLED)
        txt.pack(fill=tk.BOTH, expand=True)

    def _on_history_double_click(self, event):
        sel = self.hist_tree.selection()
        if not sel:
            return
        iid = sel[0]
        ref = self.history_item_records.get(iid)
        if not ref:
            messagebox.showwarning("提示", "无法找到该历史记录的引用，请刷新历史列表。")
            return
        self._open_history_detail_by_ref(ref)

    def _open_history_detail_by_ref(self, ref):
        idx = ref.get("index", 0)
        self.history_detail_current_index = idx
        self._render_history_detail_window(idx)

    def _show_prev_history_record(self):
        idx = self.history_detail_current_index
        if idx is not None and idx > 0:
            self._render_history_detail_window(idx - 1)

    def _show_next_history_record(self):
        idx = self.history_detail_current_index
        if idx is not None and idx < len(self.history_display_refs) - 1:
            self._render_history_detail_window(idx + 1)

    def _render_history_detail_window(self, index: int):
        """Create or reuse the detail Toplevel and render the record at index."""
        if index < 0 or index >= len(self.history_display_refs):
            return
        self.history_detail_current_index = index
        ref = self.history_display_refs[index]

        zh = (self.lang_code == "zh_CN")
        model_name = ref.get("model", "-")
        created = ref.get("created_at", "-")
        win_title = (f"历史记录详情 - {created} - {model_name}"
                     if zh else
                     f"History Detail - {created} - {model_name}")

        # ── Create or reuse the Toplevel ──────────────────────────────────
        win = self.history_detail_window
        if win is None or not win.winfo_exists():
            win = tk.Toplevel(self.root)
            win.geometry("960x760")
            win.configure(bg=C_STYLE["bg_main"])
            win.minsize(780, 560)
            win.grid_columnconfigure(1, weight=1)
            win.grid_rowconfigure(0, weight=1)
            self.history_detail_window = win

            # Left nav column
            left_nav = tk.Frame(win, bg=C_STYLE["bg_main"], width=44)
            left_nav.grid(row=0, column=0, sticky="ns")
            left_nav.grid_propagate(False)
            self._hist_detail_left_nav = left_nav

            # Center frame (rebuilt on each navigation)
            center = tk.Frame(win, bg=C_STYLE["bg_main"])
            center.grid(row=0, column=1, sticky="nsew")
            self._hist_detail_center = center

            # Right nav column
            right_nav = tk.Frame(win, bg=C_STYLE["bg_main"], width=44)
            right_nav.grid(row=0, column=2, sticky="ns")
            right_nav.grid_propagate(False)
            self._hist_detail_right_nav = right_nav

            # Build nav buttons once
            # Tooltips: 上一条 / Previous   下一条 / Next
            prev_tip = "上一条" if zh else "Previous"
            next_tip = "下一条" if zh else "Next"
            self._hist_prev_btn = tk.Button(
                self._hist_detail_left_nav, text="‹",
                font=("TkDefaultFont", 18, "bold"),
                width=2, bd=0, relief=tk.FLAT,
                bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"],
                activebackground=C_STYLE["bg_main"],
                cursor="hand2",
                command=self._show_prev_history_record)
            self._hist_prev_btn.place(relx=0.5, rely=0.5, anchor="center")
            self._hist_prev_btn._tooltip_text = prev_tip   # 上一条 / Previous

            self._hist_next_btn = tk.Button(
                self._hist_detail_right_nav, text="›",
                font=("TkDefaultFont", 18, "bold"),
                width=2, bd=0, relief=tk.FLAT,
                bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"],
                activebackground=C_STYLE["bg_main"],
                cursor="hand2",
                command=self._show_next_history_record)
            self._hist_next_btn.place(relx=0.5, rely=0.5, anchor="center")
            self._hist_next_btn._tooltip_text = next_tip   # 下一条 / Next

            # Keyboard bindings
            win.bind("<Left>",   lambda e: self._show_prev_history_record())
            win.bind("<Right>",  lambda e: self._show_next_history_record())
            win.bind("<Escape>", lambda e: win.destroy())

        win.title(win_title)
        win.lift()
        win.focus_set()

        # ── Update nav button enabled/disabled state ──────────────────────
        total = len(self.history_display_refs)
        prev_state = tk.NORMAL if index > 0 else tk.DISABLED
        next_state = tk.NORMAL if index < total - 1 else tk.DISABLED
        self._hist_prev_btn.config(state=prev_state)
        self._hist_next_btn.config(state=next_state)

        # ── Nav button text (simple arrows only) ─────────────────────────
        self._hist_prev_btn.config(text="‹")
        self._hist_next_btn.config(text="›")

        # ── Rebuild center content ────────────────────────────────────────
        for w in self._hist_detail_center.winfo_children():
            w.destroy()

        if ref["source"] == "result_store":
            # Hydrate result.json to confirm sweep vs single.
            # is_sweep_record() checks type fields AND payload keys; it never
            # uses metric columns (output_tps, ttft_p95, e2e_p95) as indicators.
            _rs_arts   = self._resolve_history_artifacts(ref)
            _rs_rjson  = _rs_arts.get("result_json") or ""
            _rs_payload: dict = {}
            if _rs_rjson and os.path.isfile(_rs_rjson):
                try:
                    with open(_rs_rjson, encoding="utf-8") as _f:
                        _rs_payload = json.load(_f)
                except Exception:
                    pass
            if is_sweep_record(ref, _rs_payload):
                self._render_rs_sweep_detail(
                    self._hist_detail_center, ref, _rs_payload, _rs_arts)
            else:
                self._render_rs_detail(self._hist_detail_center, ref)
        elif ref["source"] == "result_db":
            if ref["run_type"] == "sweep":
                self._render_rdb_sweep_detail(self._hist_detail_center, ref)
            else:
                self._render_rdb_single_detail(self._hist_detail_center, ref)
        else:
            if ref["run_type"] == "sweep":
                self._render_legacy_sweep_detail(self._hist_detail_center, ref)
            else:
                self._render_legacy_single_detail(self._hist_detail_center, ref)

    # ── ResultStore detail (single + sweep) ──────────────────────────────
    def _render_rs_detail(self, parent: tk.Frame, ref: dict):
        """Render full detail for a ResultStore run (shows report.txt + histogram)."""
        zh    = (self.lang_code == "zh_CN")
        run_dir  = ref.get("run_dir", "")
        run_id   = ref.get("run_id", "")
        run_type = ref.get("run_type", "single")
        s = ref.get("summary", {})

        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)
        scroll = ScrollableFrame(parent, bg=C_STYLE["bg_main"])
        scroll.grid(row=0, column=0, sticky="nsew")
        inner = scroll.content
        inner.configure(bg=C_STYLE["bg_main"])
        inner.grid_columnconfigure(0, weight=1)
        row_i = 0

        # ── Action bar ──────────────────────────────────────────────────
        act_card = SectionCard(inner, "操作" if zh else "Actions",
                               collapsible=False, expanded=True)
        act_card.grid(row=row_i, column=0, sticky="ew",
                      padx=C_STYLE["pad_lg"], pady=(C_STYLE["pad_lg"], C_STYLE["gap_lg"]))
        row_i += 1
        btn_bar = tk.Frame(act_card.content, bg=C_STYLE["bg_card"])
        btn_bar.pack(fill=tk.X, pady=(0, C_STYLE["pad_sm"]))

        # ── Pre-resolve artifacts ONCE for this detail panel ──────────────
        # All three action buttons share this resolved set so paths are
        # consistent and we avoid redundant DB / filesystem queries.
        _resolved_arts = self._resolve_history_artifacts(ref)

        def _open_folder():
            _rdir = _resolved_arts.get("report_dir") or run_dir
            if _rdir and os.path.isdir(_rdir):
                try:
                    open_directory(_rdir)
                except Exception as _e:
                    messagebox.showerror("错误" if zh else "Error", str(_e))
            else:
                _checked = _rdir or run_dir or ("未知" if zh else "unknown")
                _listing = "\n".join(
                    _artifact_list_dir_brief(_checked)
                ) if _checked and os.path.isdir(_checked) else ""
                messagebox.showwarning(
                    "提示" if zh else "Warning",
                    (f"未找到结果目录。\n\n已检查路径：\n{_checked}"
                     + (f"\n\n目录内容：\n{_listing}" if _listing else "")
                     if zh else
                     f"Directory not found.\n\nChecked path:\n{_checked}"
                     + (f"\n\nDirectory contents:\n{_listing}" if _listing else "")),
                )

        def _regen_report():
            _rdir = _resolved_arts.get("report_dir") or run_dir
            _rjson = _resolved_arts.get("result_json") or ""
            if not _rdir:
                messagebox.showwarning(
                    "提示" if zh else "Warning",
                    ("结果目录未知，无法重新生成报告。"
                     if zh else
                     "Result directory unknown; cannot regenerate report."),
                )
                return
            if not _rjson or not os.path.isfile(_rjson):
                _listing_lines = _artifact_list_dir_brief(_rdir)
                _listing = "\n".join(_listing_lines) if _listing_lines else "  (空目录)"
                messagebox.showerror(
                    "失败" if zh else "Error",
                    (f"重新生成报告失败：未找到 result JSON。\n\n"
                     f"已检查目录（{len(_listing_lines)} 个文件）：\n{_rdir}\n\n"
                     f"目录内容：\n{_listing}\n\n"
                     f"请确认目录下存在 result*.json 或 sweep_result*.json。"
                     if zh else
                     f"Report regeneration failed: result JSON not found.\n\n"
                     f"Checked directory ({len(_listing_lines)} files):\n{_rdir}\n\n"
                     f"Directory contents:\n{_listing}\n\n"
                     f"Make sure result*.json or sweep_result*.json exists there."),
                )
                return
            def _gen_fn(result_dict):
                return self._generate_report_v2(result_dict, {})
            ok = rs_regenerate_report(_rdir, _gen_fn, result_json_path=_rjson)
            if ok:
                _reload_report()
                messagebox.showinfo(
                    "完成" if zh else "Done",
                    "报告已重新生成。" if zh else "Report regenerated.",
                )
            else:
                messagebox.showerror(
                    "失败" if zh else "Error",
                    (f"重新生成报告失败：报告生成函数返回错误。\n\n"
                     f"Result JSON：\n{_rjson}"
                     if zh else
                     f"Report regeneration failed: report generator returned an error.\n\n"
                     f"Result JSON:\n{_rjson}"),
                )

        def _regen_chart():
            _rdir = _resolved_arts.get("report_dir") or run_dir
            _rjson = _resolved_arts.get("result_json") or ""
            if not _rdir:
                messagebox.showwarning(
                    "提示" if zh else "Warning",
                    ("结果目录未知，无法重新生成图表。"
                     if zh else
                     "Result directory unknown; cannot regenerate chart."),
                )
                return
            if not _rjson or not os.path.isfile(_rjson):
                _listing_lines = _artifact_list_dir_brief(_rdir)
                _listing = "\n".join(_listing_lines) if _listing_lines else "  (空目录)"
                messagebox.showerror(
                    "失败" if zh else "Error",
                    (f"重新生成图表失败：未找到 result JSON。\n\n"
                     f"已检查目录（{len(_listing_lines)} 个文件）：\n{_rdir}\n\n"
                     f"目录内容：\n{_listing}"
                     if zh else
                     f"Chart regeneration failed: result JSON not found.\n\n"
                     f"Checked directory ({len(_listing_lines)} files):\n{_rdir}\n\n"
                     f"Directory contents:\n{_listing}"),
                )
                return
            p = rs_regenerate_chart(_rdir, result_json_path=_rjson)
            if p:
                _reload_hist_image()
                messagebox.showinfo(
                    "完成" if zh else "Done",
                    f"图表已重新生成:\n{p}" if zh else f"Chart regenerated:\n{p}",
                )
            else:
                messagebox.showerror(
                    "失败" if zh else "Error",
                    (f"重新生成图表失败：\nresult JSON 中未找到有效的 E2E 延迟数据。\n\n"
                     f"Result JSON：\n{_rjson}\n\n"
                     f"请确认 result JSON 中的 detail[] 列表包含 e2e_latency 或 latency 字段，"
                     f"且 ok=true 的请求数 > 0。"
                     if zh else
                     f"Chart regeneration failed:\n"
                     f"No valid E2E latency data found in result JSON.\n\n"
                     f"Result JSON:\n{_rjson}\n\n"
                     f"Make sure detail[] entries in the result JSON contain "
                     f"e2e_latency or latency fields with ok=true."),
                )

        ttk.Button(btn_bar, text="📂 打开结果目录" if zh else "📂 Open Folder",
                   style="Secondary.TButton", command=_open_folder).pack(
                   side=tk.LEFT, padx=(0, C_STYLE["pad_sm"]))
        ttk.Button(btn_bar, text="↻ 重新生成报告" if zh else "↻ Regen Report",
                   style="Secondary.TButton", command=_regen_report).pack(
                   side=tk.LEFT, padx=(0, C_STYLE["pad_sm"]))
        ttk.Button(btn_bar, text="↻ 重新生成图表" if zh else "↻ Regen Chart",
                   style="Secondary.TButton", command=_regen_chart).pack(
                   side=tk.LEFT, padx=(0, C_STYLE["pad_sm"]))

        dir_lbl = tk.Label(btn_bar, text=run_dir or "-",
                           font=C_STYLE["font_small"],
                           bg=C_STYLE["bg_card"], fg=C_STYLE["text_muted"],
                           anchor="w", justify=tk.LEFT)
        dir_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # ── Summary metrics ─────────────────────────────────────────────
        sum_card = SectionCard(inner, "测试摘要" if zh else "Run Summary",
                               collapsible=True, expanded=True)
        sum_card.grid(row=row_i, column=0, sticky="ew",
                      padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_lg"]))
        row_i += 1
        lines = [
            f"Run ID:    {run_id}",
            f"模型:      {s.get('model', '-')}",
            f"负载:      {s.get('workload', '-')}",
            f"成功率:    {s.get('success_rate', '-')}%",
        ]
        if run_type == "single":
            decode_tps = s.get("single_session_decode_tok_s")
            out_tps    = s.get("output_token_throughput_tok_s")
            e2e_avg    = s.get("mean_e2e_latency_ms")
            ttft_ms    = s.get("mean_ttft_ms")
            tpot_ms    = s.get("mean_tpot_ms")
            if decode_tps: lines.append(f"解码速度:  {decode_tps:.1f} tok/s")
            if out_tps:    lines.append(f"输出 TPS:  {out_tps:.1f} tok/s")
            if ttft_ms:    lines.append(f"TTFT avg:  {ttft_ms:.0f} ms")
            if tpot_ms:    lines.append(f"TPOT avg:  {tpot_ms:.1f} ms")
            if e2e_avg:    lines.append(f"E2E avg:   {e2e_avg:.0f} ms")
        else:
            peak_tps = s.get("peak_output_token_throughput_tok_s")
            mc       = s.get("max_throughput_concurrency")
            if peak_tps: lines.append(f"峰值 TPS:  {peak_tps:.1f} tok/s @ C{mc}")
        tk.Label(sum_card.content, text="\n".join(lines),
                 justify=tk.LEFT, anchor="w", font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"]).pack(fill=tk.X)

        # ── E2E Histogram (PNG or canvas fallback) ───────────────────────
        hist_card = SectionCard(inner, "E2E Latency 分布" if zh else "E2E Latency Distribution",
                                collapsible=True, expanded=True)
        hist_card.grid(row=row_i, column=0, sticky="ew",
                       padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_lg"]))
        row_i += 1
        hist_frame = tk.Frame(hist_card.content, bg=C_STYLE["bg_card"], height=240)
        hist_frame.pack(fill=tk.BOTH, expand=True)
        hist_frame.pack_propagate(False)
        _hist_img_ref = [None]

        def _reload_hist_image():
            for w in hist_frame.winfo_children():
                w.destroy()
            _hist_img_ref[0] = None
            png_path = rs_get_e2e_histogram_png(run_dir) if run_dir else None
            if png_path:
                try:
                    from PIL import Image, ImageTk
                    img = Image.open(png_path)
                    img.thumbnail((900, 230), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
                    _hist_img_ref[0] = photo
                    lbl = tk.Label(hist_frame, image=photo,
                                   bg=C_STYLE["bg_card"])
                    lbl.image = photo
                    lbl.pack(fill=tk.BOTH, expand=True)
                    return
                except ImportError:
                    pass  # PIL not available; fall through to canvas
                except Exception:
                    pass
            # Fallback: draw via Tk canvas using result.json latencies
            canvas = tk.Canvas(hist_frame, bg=C_STYLE["bg_card"],
                                highlightthickness=0)
            canvas.pack(fill=tk.BOTH, expand=True)
            if run_dir:
                result_data = rs_load_result(run_dir)
                if result_data:
                    lats = [
                        float(r.get("e2e_latency", r.get("latency", 0)))
                        for r in result_data.get("detail", [])
                        if r.get("ok") and (r.get("e2e_latency", r.get("latency", 0)) or 0) > 0
                    ]
                    if lats:
                        hist_frame.after(100, lambda: self._draw_popup_histogram(canvas, lats))
                        return
            # Nothing available
            canvas.create_text(
                380, 100,
                text=("E2E 直方图缺失\n点击「重新生成图表」可从 result.json 重建"
                      if zh else
                      "E2E histogram not found.\nClick 'Regen Chart' to rebuild from result.json"),
                fill=C_STYLE["text_muted"], font=C_STYLE["font_body"],
                justify=tk.CENTER, width=500,
            )

        _reload_hist_image()

        # ── Full report text ─────────────────────────────────────────────
        rpt_card = SectionCard(inner,
                               "测试报告" if zh else "Test Report",
                               collapsible=True, expanded=True)
        rpt_card.grid(row=row_i, column=0, sticky="ew",
                      padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_lg"]))
        row_i += 1

        rpt_text = tk.Text(rpt_card.content, height=24, wrap=tk.WORD,
                           font=C_STYLE["font_small"],
                           bg=C_STYLE["bg_input"], fg=C_STYLE["text_primary"],
                           relief=tk.FLAT, borderwidth=0)
        rpt_scroll = ttk.Scrollbar(rpt_card.content, orient=tk.VERTICAL,
                                   command=rpt_text.yview)
        rpt_text.configure(yscrollcommand=rpt_scroll.set)
        rpt_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        rpt_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        def _reload_report():
            rpt_text.config(state=tk.NORMAL)
            rpt_text.delete("1.0", tk.END)
            if run_dir:
                rpt = rs_load_report(run_dir, preferred="txt")
                if rpt:
                    rpt_text.insert(tk.END, rpt)
                else:
                    rpt_text.insert(tk.END,
                        "⚠ 文本报告缺失，可点击上方「重新生成报告」从 result.json 重建。"
                        if zh else
                        "⚠ report.txt not found. Click 'Regen Report' above to rebuild from result.json.")
            else:
                rpt_text.insert(tk.END, "⚠ 未找到结果目录。")
            rpt_text.config(state=tk.DISABLED)

        _reload_report()

        # ── Close button ─────────────────────────────────────────────────
        btn_row = tk.Frame(inner, bg=C_STYLE["bg_main"])
        btn_row.grid(row=row_i, column=0, sticky="e",
                     padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["pad_lg"]))
        ttk.Button(btn_row, text="关闭" if zh else "Close",
                   style="Secondary.TButton",
                   command=self.history_detail_window.destroy).pack()

    # ── ResultStore sweep detail ─────────────────────────────────────────
    def _render_rs_sweep_detail(self, parent: tk.Frame, ref: dict,
                                 payload: dict | None = None,
                                 arts: dict | None = None):
        """Render the full sweep detail for a ResultStore record.

        Shows: action bar, sweep overview, concurrency case table,
        2×2 sweep analysis chart (from result.json *cases* data), report
        text, and output-files section.

        Parameters
        ----------
        ref:
            The history display ref dict (source="result_store").
        payload:
            Pre-loaded result.json dict (passed from
            _render_history_detail_window so we don't re-read the file).
            Falls back to loading from arts/run_dir if None.
        arts:
            Pre-resolved artifact paths dict from resolve_history_artifacts().
            Falls back to calling _resolve_history_artifacts(ref) if None.
        """
        zh       = (self.lang_code == "zh_CN")
        run_dir  = ref.get("run_dir", "")
        run_id   = ref.get("run_id", "")
        s        = ref.get("summary", {})

        # ── Ensure we have artifacts and payload ─────────────────────────
        if arts is None:
            arts = self._resolve_history_artifacts(ref)
        _rjson = arts.get("result_json") or ""
        _rdir  = arts.get("report_dir") or run_dir

        if payload is None:
            payload = {}
            if _rjson and os.path.isfile(_rjson):
                try:
                    with open(_rjson, encoding="utf-8") as _f:
                        payload = json.load(_f)
                except Exception:
                    pass

        cases = payload.get("cases", [])

        # ── Scroll container ─────────────────────────────────────────────
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)
        scroll = ScrollableFrame(parent, bg=C_STYLE["bg_main"])
        scroll.grid(row=0, column=0, sticky="nsew")
        inner = scroll.content
        inner.configure(bg=C_STYLE["bg_main"])
        inner.grid_columnconfigure(0, weight=1)
        row_i = 0

        # ── Action bar ───────────────────────────────────────────────────
        act_card = SectionCard(inner, "操作" if zh else "Actions",
                               collapsible=False, expanded=True)
        act_card.grid(row=row_i, column=0, sticky="ew",
                      padx=C_STYLE["pad_lg"],
                      pady=(C_STYLE["pad_lg"], C_STYLE["gap_lg"]))
        row_i += 1
        btn_bar = tk.Frame(act_card.content, bg=C_STYLE["bg_card"])
        btn_bar.pack(fill=tk.X, pady=(0, C_STYLE["pad_sm"]))

        def _open_folder():
            _d = _rdir or run_dir
            if _d and os.path.isdir(_d):
                try:
                    open_directory(_d)
                except Exception as _e:
                    messagebox.showerror("错误" if zh else "Error", str(_e))
            else:
                _checked = _d or ("未知" if zh else "unknown")
                messagebox.showwarning(
                    "提示" if zh else "Warning",
                    (f"未找到结果目录。\n已检查路径：\n{_checked}"
                     if zh else
                     f"Directory not found.\nChecked path:\n{_checked}"),
                )

        def _regen_report_sweep():
            if not _rjson or not os.path.isfile(_rjson):
                _listing = "\n".join(_artifact_list_dir_brief(_rdir or ""))
                messagebox.showerror(
                    "失败" if zh else "Error",
                    (f"重新生成报告失败：未找到 result JSON。\n\n"
                     f"已检查目录：\n{_rdir}\n\n目录内容：\n{_listing}"
                     if zh else
                     f"Report regeneration failed: result JSON not found.\n\n"
                     f"Checked directory:\n{_rdir}\n\nContents:\n{_listing}"),
                )
                return
            _loaded = payload if payload else {}
            if not _loaded:
                messagebox.showerror("失败" if zh else "Error",
                    "无法加载 result.json，报告生成中止。" if zh else
                    "Cannot load result.json; report generation aborted.")
                return
            try:
                report_text = self._generate_sweep_markdown_report(_loaded)
                _txt_path = os.path.join(_rdir, "report.txt")
                _md_path  = os.path.join(_rdir, "report.md")
                with open(_txt_path, "w", encoding="utf-8") as _f:
                    _f.write(report_text)
                with open(_md_path, "w", encoding="utf-8") as _f:
                    _f.write(report_text)
                _reload_report()
                messagebox.showinfo(
                    "完成" if zh else "Done",
                    "扫测报告已重新生成。" if zh else "Sweep report regenerated.",
                )
            except Exception as _e:
                messagebox.showerror("失败" if zh else "Error", str(_e))

        def _regen_chart_sweep():
            if not _rjson or not os.path.isfile(_rjson):
                messagebox.showerror(
                    "失败" if zh else "Error",
                    "未找到 result JSON，无法重新生成图表。" if zh else
                    "result JSON not found; cannot regenerate chart.",
                )
                return
            _loaded = payload if payload else {}
            if not _loaded.get("cases"):
                messagebox.showwarning(
                    "提示" if zh else "Warning",
                    ("result.json 中未找到 cases 数据，无法生成扫测图表。"
                     if zh else
                     "No sweep cases found in result.json; cannot regenerate chart."),
                )
                return
            if not self._matplotlib_available():
                messagebox.showwarning(
                    "提示" if zh else "Warning",
                    "matplotlib 未安装，无法生成图表。\npip install matplotlib"
                    if zh else
                    "matplotlib not installed.\npip install matplotlib",
                )
                return
            try:
                import matplotlib
                matplotlib.use("Agg")
                fig = self._build_sweep_analysis_figure(_loaded)
                if fig is None:
                    raise RuntimeError("图表生成返回空结果")
                os.makedirs(os.path.join(_rdir, "charts"), exist_ok=True)
                _png = os.path.join(_rdir, "charts", "sweep_analysis.png")
                fig.savefig(_png, dpi=150, bbox_inches="tight")
                import matplotlib.pyplot as plt
                plt.close(fig)
                _reload_chart(png_override=_png)
                messagebox.showinfo(
                    "完成" if zh else "Done",
                    f"扫测图表已重新生成:\n{_png}" if zh else
                    f"Sweep chart regenerated:\n{_png}",
                )
            except Exception as _e:
                messagebox.showerror("失败" if zh else "Error", str(_e))

        ttk.Button(btn_bar, text="📂 打开结果目录" if zh else "📂 Open Folder",
                   style="Secondary.TButton",
                   command=_open_folder).pack(side=tk.LEFT,
                                              padx=(0, C_STYLE["pad_sm"]))
        ttk.Button(btn_bar, text="↻ 重新生成报告" if zh else "↻ Regen Report",
                   style="Secondary.TButton",
                   command=_regen_report_sweep).pack(side=tk.LEFT,
                                                     padx=(0, C_STYLE["pad_sm"]))
        ttk.Button(btn_bar, text="↻ 重新生成图表" if zh else "↻ Regen Chart",
                   style="Secondary.TButton",
                   command=_regen_chart_sweep).pack(side=tk.LEFT,
                                                    padx=(0, C_STYLE["pad_sm"]))
        tk.Label(btn_bar, text=_rdir or run_dir or "-",
                 font=C_STYLE["font_small"], bg=C_STYLE["bg_card"],
                 fg=C_STYLE["text_muted"], anchor="w",
                 justify=tk.LEFT).pack(side=tk.LEFT, fill=tk.X, expand=True)

        # ── Sweep overview ───────────────────────────────────────────────
        ov_card = SectionCard(inner, "扫测概览" if zh else "Sweep Overview",
                              collapsible=True, expanded=True)
        ov_card.grid(row=row_i, column=0, sticky="ew",
                     padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_lg"]))
        row_i += 1
        peak_tps = s.get("peak_output_token_throughput_tok_s")
        mc       = s.get("max_throughput_concurrency")
        levels   = (payload.get("concurrency_levels")
                    or s.get("concurrency_list") or [])
        ov_lines = [
            f"Run ID: {run_id}",
            f"模型:   {s.get('model') or payload.get('model') or '-'}",
            f"API:    {payload.get('api_url') or s.get('api_url') or '-'}",
            f"档位数: {len(cases)} 级",
            f"并发级别: {levels}" if levels else "",
            f"峰值 TPS: {peak_tps:.1f} tok/s @ C{mc}" if peak_tps else "",
            f"Run Dir: {_rdir or run_dir or '-'}",
        ]
        tk.Label(ov_card.content, text="\n".join(l for l in ov_lines if l),
                 justify=tk.LEFT, anchor="w", font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"],
                 fg=C_STYLE["text_primary"]).pack(fill=tk.X)

        # ── Concurrency case table ───────────────────────────────────────
        if cases:
            tbl_card = SectionCard(
                inner, "并发档位明细" if zh else "Concurrency Case Details",
                collapsible=True, expanded=True)
            tbl_card.grid(row=row_i, column=0, sticky="ew",
                          padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_lg"]))
            row_i += 1
            cols = ("C", "Req", "Success", "Fail", "Rate%",
                    "E2E Avg(s)", "E2E P95(s)", "TTFT(s)", "Out TPS", "RPS")
            tv = ttk.Treeview(tbl_card.content, columns=cols,
                              show="headings",
                              height=min(max(len(cases), 3), 10),
                              style="App.Treeview")
            for col in cols:
                tv.heading(col, text=col)
                tv.column(col, anchor="center", width=82)
            for case in cases:
                cs = case.get("benchmark_summary", {})
                tv.insert("", tk.END, values=(
                    case.get("concurrency", "-"),
                    case.get("total_requests", "-"),
                    cs.get("success", 0),
                    cs.get("fail", 0),
                    f"{cs.get('success_rate', 0) or 0:.1f}%",
                    f"{cs.get('e2e_latency_avg', 0) or 0:.3f}",
                    f"{cs.get('e2e_latency_p95', 0) or 0:.3f}",
                    f"{cs.get('ttft_avg', 0) or 0:.3f}",
                    f"{cs.get('system_output_tps', 0) or 0:.1f}",
                    f"{cs.get('request_throughput_rps', 0) or 0:.2f}",
                ))
            tv.pack(fill=tk.X)

        # ── Sweep analysis chart ─────────────────────────────────────────
        chart_card = SectionCard(
            inner, "图形分析" if zh else "Chart Analysis",
            collapsible=True, expanded=True)
        chart_card.grid(row=row_i, column=0, sticky="ew",
                        padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_lg"]))
        row_i += 1
        chart_frame = tk.Frame(chart_card.content,
                               bg=C_STYLE["bg_card"], height=460)
        chart_frame.pack(fill=tk.BOTH, expand=True)
        chart_frame.pack_propagate(False)

        # Chart state — reloaded by _reload_chart()
        _chart_canvas_ref = [None]
        _chart_figure_ref = [None]

        def _reload_chart(png_override: str | None = None):
            """Display the sweep chart: PNG > live matplotlib > placeholder."""
            # Destroy old canvas
            if _chart_canvas_ref[0] is not None:
                try:
                    _chart_canvas_ref[0].get_tk_widget().destroy()
                except Exception:
                    pass
                _chart_canvas_ref[0] = None
            if _chart_figure_ref[0] is not None:
                try:
                    import matplotlib.pyplot as plt
                    plt.close(_chart_figure_ref[0])
                except Exception:
                    pass
                _chart_figure_ref[0] = None
            for w in chart_frame.winfo_children():
                w.destroy()

            # 1. Try existing PNG (charts/sweep_analysis.png first, then any PNG)
            _png_to_show = png_override or ""
            if not _png_to_show:
                for _candidate in (
                    os.path.join(_rdir, "charts", "sweep_analysis.png"),
                    arts.get("chart_png") or "",
                ):
                    if _candidate and os.path.isfile(_candidate):
                        _png_to_show = _candidate
                        break

            if _png_to_show and os.path.isfile(_png_to_show):
                try:
                    from PIL import Image, ImageTk
                    _img = Image.open(_png_to_show)
                    _img.thumbnail((920, 450), Image.LANCZOS)
                    _photo = ImageTk.PhotoImage(_img)
                    _lbl = tk.Label(chart_frame, image=_photo,
                                    bg=C_STYLE["bg_card"])
                    _lbl.image = _photo  # keep reference
                    _lbl.pack(fill=tk.BOTH, expand=True)
                    return
                except ImportError:
                    pass  # Pillow unavailable — fall through to matplotlib
                except Exception:
                    pass

            # 2. Live matplotlib from payload.cases
            if cases and self._matplotlib_available():
                try:
                    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
                    _fig = self._build_sweep_analysis_figure(payload)
                    if _fig is not None:
                        _chart_figure_ref[0] = _fig
                        _mpl = FigureCanvasTkAgg(_fig, master=chart_frame)
                        _mpl.draw()
                        _mpl.get_tk_widget().pack(fill=tk.BOTH, expand=True)
                        _chart_canvas_ref[0] = _mpl
                        return
                except Exception as _ce:
                    tk.Label(chart_frame,
                             text=f"图表渲染失败: {_ce}" if zh else f"Chart error: {_ce}",
                             font=C_STYLE["font_body"], bg=C_STYLE["bg_card"],
                             fg=C_STYLE["warning_text"],
                             wraplength=700).pack(expand=True)
                    return

            # 3. Placeholder
            _ph = tk.Canvas(chart_frame, bg=C_STYLE["bg_card"],
                            highlightthickness=0)
            _ph.pack(fill=tk.BOTH, expand=True)
            _ph.after(50, lambda: _ph.create_text(
                max(_ph.winfo_width() // 2, 380), 100,
                text=(
                    "扫测图表缺失。\n"
                    "点击「重新生成图表」从 result.json 重建扫测趋势图。"
                    if zh else
                    "Sweep chart not found.\n"
                    "Click 'Regen Chart' to rebuild from result.json."
                ),
                fill=C_STYLE["text_muted"], font=C_STYLE["font_body"],
                justify=tk.CENTER, width=500,
            ))

        _reload_chart()

        # ── Expert analysis ──────────────────────────────────────────────
        if payload.get("analysis_summary") or cases:
            expert_card = SectionCard(
                inner, "专家分析简评" if zh else "Expert Summary",
                collapsible=True, expanded=True)
            expert_card.grid(row=row_i, column=0, sticky="ew",
                             padx=C_STYLE["pad_lg"],
                             pady=(0, C_STYLE["gap_lg"]))
            row_i += 1
            analysis_lines = payload.get("analysis_summary", [])
            commentary = (self._generate_expert_commentary(cases, analysis_lines)
                          if cases else "")
            expert_txt = (
                "\n".join([*analysis_lines, "", commentary]).strip()
                or ("未生成专家分析。" if zh else "No expert analysis generated.")
            )
            tk.Label(expert_card.content, text=expert_txt,
                     justify=tk.LEFT, anchor="w",
                     font=C_STYLE["font_body"], bg=C_STYLE["bg_card"],
                     fg=C_STYLE["text_primary"], wraplength=820).pack(fill=tk.X)

        # ── Report text ──────────────────────────────────────────────────
        rpt_card = SectionCard(
            inner, "测试报告" if zh else "Test Report",
            collapsible=True, expanded=True)
        rpt_card.grid(row=row_i, column=0, sticky="ew",
                      padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_lg"]))
        row_i += 1
        rpt_text = tk.Text(
            rpt_card.content, height=20, wrap=tk.WORD,
            font=C_STYLE["font_small"],
            bg=C_STYLE["bg_input"], fg=C_STYLE["text_primary"],
            relief=tk.FLAT, borderwidth=0)
        rpt_scroll = ttk.Scrollbar(rpt_card.content, orient=tk.VERTICAL,
                                   command=rpt_text.yview)
        rpt_text.configure(yscrollcommand=rpt_scroll.set)
        rpt_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        rpt_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        def _reload_report():
            rpt_text.config(state=tk.NORMAL)
            rpt_text.delete("1.0", tk.END)
            _loaded_rpt = None
            if _rdir and os.path.isdir(_rdir):
                _loaded_rpt = rs_load_report(_rdir, preferred="md")
            if _loaded_rpt:
                rpt_text.insert(tk.END, _loaded_rpt)
            else:
                rpt_text.insert(tk.END,
                    "⚠ 扫测报告缺失，点击「重新生成报告」从 result.json 重建。"
                    if zh else
                    "⚠ Sweep report not found. Click 'Regen Report' to rebuild.")
            rpt_text.config(state=tk.DISABLED)

        _reload_report()

        # ── Output files ─────────────────────────────────────────────────
        files_card = SectionCard(
            inner, "输出文件" if zh else "Output Files",
            collapsible=True, expanded=False)
        files_card.grid(row=row_i, column=0, sticky="ew",
                        padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_lg"]))
        row_i += 1
        _cust_pdf  = arts.get("customer_pdf", "")
        _cust_docx = arts.get("customer_docx", "")
        _files = [
            f"Report Dir: {_rdir or '-'}",
            f"result.json: {_rjson or '-'}",
            f"report.md:   {arts.get('report_md') or '-'}",
            f"report.txt:  {arts.get('report_txt') or '-'}",
            f"chart PNG:   {arts.get('chart_png') or '-'}",
        ]
        if _cust_pdf:
            _files.append(f"PDF 报告:  {_cust_pdf}")
        if _cust_docx:
            _files.append(f"DOCX 报告: {_cust_docx}")
        tk.Label(files_card.content, text="\n".join(_files),
                 justify=tk.LEFT, anchor="w",
                 font=C_STYLE["font_body"], bg=C_STYLE["bg_card"],
                 fg=C_STYLE["text_primary"]).pack(fill=tk.X)

        # ── Bottom bar: generate customer report + close ─────────────────
        btn_row = tk.Frame(inner, bg=C_STYLE["bg_main"])
        btn_row.grid(row=row_i, column=0, sticky="e",
                     padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["pad_lg"]))
        pdf_btn_txt = "生成客户报告" if zh else "Generate Client Report"
        _j = arts.get("result_json", "")
        _m = arts.get("report_md", "") or arts.get("report_txt", "")
        _p = arts.get("chart_png", "")
        pdf_btn = ttk.Button(
            btn_row, text=pdf_btn_txt, style="Primary.TButton",
            command=lambda: self._generate_customer_report_for_history_record(
                ref, {}, payload, _j, _m, _p, _rdir, btn_row))
        pdf_btn.pack(side=tk.LEFT, padx=(0, C_STYLE["gap_md"]))
        ttk.Button(btn_row, text="关闭" if zh else "Close",
                   style="Secondary.TButton",
                   command=self.history_detail_window.destroy).pack(side=tk.LEFT)

    # ── Result DB sweep detail ────────────────────────────────────────────
    def _render_rdb_sweep_detail(self, parent: tk.Frame, ref: dict):
        zh = (self.lang_code == "zh_CN")
        run_id = ref["run_id"]
        db_path = getattr(self, "result_db_path_var",
                          tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
        row = {}
        cases_rows = []
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            r = conn.execute("SELECT * FROM benchmark_runs WHERE run_id=?",
                             (run_id,)).fetchone()
            row = dict(r) if r else {}
            cr = conn.execute(
                """SELECT * FROM sweep_cases WHERE run_id=? ORDER BY case_index""",
                (run_id,)).fetchall()
            cases_rows = [dict(c) for c in cr]
            conn.close()
        except Exception:
            pass

        # Try to load full sweep JSON from report_dir
        sweep_result = {}
        report_dir = row.get("report_dir", "")
        if report_dir and os.path.isdir(report_dir):
            for fname in sorted(os.listdir(report_dir)):
                if fname.startswith("result_") and fname.endswith(".json"):
                    try:
                        with open(os.path.join(report_dir, fname),
                                  encoding="utf-8") as f:
                            sweep_result = json.load(f)
                        break
                    except Exception:
                        pass

        # Build cases list compatible with sweep renderer
        cases = sweep_result.get("cases") or [
            {
                "concurrency": c.get("concurrency", 0),
                "total_requests": c.get("total_requests", 0),
                "benchmark_summary": {
                    "success": c.get("success", 0),
                    "fail": c.get("fail", 0),
                    "success_rate": (c.get("success", 0) / max(c.get("total_requests", 1), 1) * 100),
                    "e2e_latency_p95": c.get("e2el_p95", 0),
                    "e2e_latency_avg": c.get("e2el_avg", 0),
                    "system_output_tps": c.get("output_token_throughput", 0),
                    "request_throughput_rps": c.get("request_throughput", 0),
                    "ttft_avg": c.get("ttft_avg", 0),
                    "ttft_p95": c.get("ttft_p95", 0),
                    "tpot_avg": c.get("tpot_avg", 0),
                },
                "analysis_metrics": {"throughput_efficiency": c.get("throughput_efficiency", 0)},
            }
            for c in cases_rows
        ]

        # Env summary
        env_name = row.get("environment_profile_name_snapshot") or ""
        env_lines = self._build_env_summary_lines(row, zh)

        # ── Render ────────────────────────────────────────────────────────
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)
        scroll = ScrollableFrame(parent, bg=C_STYLE["bg_main"])
        scroll.grid(row=0, column=0, sticky="nsew")
        inner = scroll.content
        inner.configure(bg=C_STYLE["bg_main"])
        inner.grid_columnconfigure(0, weight=1)

        row_idx = 0
        # Env card
        env_card = SectionCard(inner, "被测环境 / Environment" if zh else "Test Environment",
                               collapsible=True, expanded=True)
        env_card.grid(row=row_idx, column=0, sticky="ew",
                      padx=C_STYLE["pad_lg"], pady=(C_STYLE["pad_lg"], C_STYLE["gap_lg"]))
        tk.Label(env_card.content, text="\n".join(env_lines),
                 justify=tk.LEFT, anchor="w", font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"],
                 wraplength=780).pack(fill=tk.X)
        row_idx += 1

        # Overview card
        ov_card = SectionCard(inner, "扫测概览" if zh else "Sweep Overview",
                              collapsible=True, expanded=True)
        ov_card.grid(row=row_idx, column=0, sticky="ew",
                     padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_lg"]))
        ov_txt = (
            f"模型: {row.get('model_name', '-')}\n"
            f"API: {row.get('api_url', '-')}\n"
            f"Run ID: {run_id}\n"
            f"档位数: {len(cases)}\n"
            f"状态: {row.get('status', '-')}"
        )
        tk.Label(ov_card.content, text=ov_txt, justify=tk.LEFT, anchor="w",
                 font=C_STYLE["font_body"], bg=C_STYLE["bg_card"],
                 fg=C_STYLE["text_primary"]).pack(fill=tk.X)
        row_idx += 1

        # Cases table
        tbl_card = SectionCard(inner, "并发档位明细" if zh else "Concurrency Case Details",
                               collapsible=True, expanded=True)
        tbl_card.grid(row=row_idx, column=0, sticky="ew",
                      padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_lg"]))
        cols = ("C", "Req", "Success", "Fail", "Rate%",
                "E2E Avg(s)", "E2E P95(s)", "TTFT(s)", "Out TPS", "RPS")
        tv = ttk.Treeview(tbl_card.content, columns=cols, show="headings",
                          height=min(max(len(cases), 3), 8), style="App.Treeview")
        for col in cols:
            tv.heading(col, text=col)
            tv.column(col, anchor="center", width=82)
        tv.pack(fill=tk.X)
        for case in cases:
            s = case.get("benchmark_summary", {})
            tv.insert("", tk.END, values=(
                case.get("concurrency", "-"),
                case.get("total_requests", "-"),
                s.get("success", 0),
                s.get("fail", 0),
                f"{s.get('success_rate', 0) or 0:.1f}%",
                f"{s.get('e2e_latency_avg', 0) or 0:.3f}",
                f"{s.get('e2e_latency_p95', 0) or 0:.3f}",
                f"{s.get('ttft_avg', 0) or 0:.3f}",
                f"{s.get('system_output_tps', 0) or 0:.1f}",
                f"{s.get('request_throughput_rps', 0) or 0:.2f}",
            ))
        row_idx += 1

        # Chart card
        chart_card = SectionCard(inner, "图形分析" if zh else "Chart Analysis",
                                 collapsible=True, expanded=True)
        chart_card.grid(row=row_idx, column=0, sticky="ew",
                        padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_lg"]))
        chart_frame = tk.Frame(chart_card.content, bg=C_STYLE["bg_card"], height=460)
        chart_frame.pack(fill=tk.BOTH, expand=True)
        chart_frame.pack_propagate(False)
        _sr = sweep_result if sweep_result.get("cases") else {"cases": cases, "model": row.get("model_name", "")}
        if self._matplotlib_available() and cases:
            try:
                from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
                fig = self._build_sweep_analysis_figure(_sr)
                if fig is not None:
                    mpl_canvas = FigureCanvasTkAgg(fig, master=chart_frame)
                    mpl_canvas.draw()
                    mpl_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
                else:
                    raise RuntimeError("no figure")
            except Exception as e:
                tk.Label(chart_frame, text=f"图表不可用: {e}",
                         font=C_STYLE["font_body"], bg=C_STYLE["bg_card"],
                         fg=C_STYLE["warning_text"], wraplength=700).pack(expand=True)
        else:
            tk.Label(chart_frame, text="matplotlib 未安装或暂无数据",
                     font=C_STYLE["font_body"], bg=C_STYLE["bg_card"],
                     fg=C_STYLE["warning_text"]).pack(expand=True)
        row_idx += 1

        # Expert analysis
        expert_card = SectionCard(inner, "专家分析简评" if zh else "Expert Summary",
                                  collapsible=True, expanded=True)
        expert_card.grid(row=row_idx, column=0, sticky="ew",
                         padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_lg"]))
        analysis_lines = sweep_result.get("analysis_summary", [])
        commentary = self._generate_expert_commentary(cases, analysis_lines) if cases else ""
        expert_txt = "\n".join([*analysis_lines, "", commentary]).strip() or "未生成专家分析。"
        tk.Label(expert_card.content, text=expert_txt, justify=tk.LEFT, anchor="w",
                 font=C_STYLE["font_body"], bg=C_STYLE["bg_card"],
                 fg=C_STYLE["text_primary"], wraplength=820).pack(fill=tk.X)
        row_idx += 1

        # Output files — resolved via priority-based helper
        files_card = SectionCard(inner, "输出文件" if zh else "Output Files",
                                 collapsible=True, expanded=True)
        files_card.grid(row=row_idx, column=0, sticky="ew",
                        padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_lg"]))
        _arts     = self._resolve_history_artifacts(ref, row, sweep_result)
        json_path = _arts["result_json"]
        md_path   = _arts["report_md"]
        png_path  = _arts["chart_png"]
        cust_pdf  = _arts["customer_pdf"]
        cust_docx = _arts["customer_docx"]
        _rdir     = _arts["report_dir"] or report_dir
        files_lines = [
            f"Report Dir: {_rdir or '未保存'}",
            f"JSON:       {json_path or '未保存'}",
            f"Markdown:   {md_path or '未保存'}",
            f"PNG:        {png_path or '未保存'}",
        ]
        if cust_pdf:
            files_lines.append(f"PDF 报告:   {cust_pdf}")
        if cust_docx:
            files_lines.append(f"DOCX 报告:  {cust_docx}")
        tk.Label(files_card.content, text="\n".join(files_lines),
                 justify=tk.LEFT, anchor="w",
                 font=C_STYLE["font_body"], bg=C_STYLE["bg_card"],
                 fg=C_STYLE["text_primary"]).pack(fill=tk.X)
        row_idx += 1

        # Bottom button bar: PDF report + close
        btn_bar = tk.Frame(inner, bg=C_STYLE["bg_main"])
        btn_bar.grid(row=row_idx, column=0, sticky="e",
                     padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["pad_lg"]))
        pdf_btn_txt = "生成客户报告" if zh else "Generate Client Report"
        _j, _m, _p, _rd = json_path, md_path, png_path, _rdir
        pdf_btn = ttk.Button(btn_bar, text=pdf_btn_txt, style="Primary.TButton",
                             command=lambda: self._generate_customer_report_for_history_record(
                                 ref, row, sweep_result, _j, _m, _p, _rd, btn_bar))
        pdf_btn.pack(side=tk.LEFT, padx=(0, C_STYLE["gap_md"]))
        close_txt = "关闭" if zh else "Close"
        ttk.Button(btn_bar, text=close_txt, style="Secondary.TButton",
                   command=self.history_detail_window.destroy).pack(side=tk.LEFT)

    # ── Result DB single detail ───────────────────────────────────────────
    def _render_rdb_single_detail(self, parent: tk.Frame, ref: dict):
        zh = (self.lang_code == "zh_CN")
        run_id = ref["run_id"]
        db_path = getattr(self, "result_db_path_var",
                          tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
        row = {}
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            r = conn.execute(
                """SELECT br.*, bm.output_token_throughput, bm.ttft_avg, bm.ttft_p95,
                          bm.e2el_avg, bm.e2el_p95, bm.tpot_avg,
                          bm.request_throughput, bm.per_request_output_tps_avg
                   FROM benchmark_runs br
                   LEFT JOIN benchmark_metrics bm ON bm.run_id = br.run_id
                   WHERE br.run_id=?""", (run_id,)).fetchone()
            row = dict(r) if r else {}
            conn.close()
        except Exception:
            pass

        env_lines = self._build_env_summary_lines(row, zh)

        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)
        top = parent.winfo_toplevel()
        top.grid_columnconfigure(1, weight=1)
        top.grid_rowconfigure(0, weight=1)

        scroll = ScrollableFrame(parent, bg=C_STYLE["bg_main"])
        scroll.grid(row=0, column=0, sticky="nsew")
        inner = scroll.content
        inner.configure(bg=C_STYLE["bg_main"])
        inner.grid_columnconfigure(0, weight=1)

        row_i = 0

        # Env card
        env_card = SectionCard(inner, "被测环境 / Environment" if zh else "Test Environment",
                               collapsible=True, expanded=True)
        env_card.grid(row=row_i, column=0, sticky="ew",
                      padx=C_STYLE["pad_lg"], pady=(C_STYLE["pad_lg"], C_STYLE["gap_lg"]))
        tk.Label(env_card.content, text="\n".join(env_lines), justify=tk.LEFT, anchor="w",
                 font=C_STYLE["font_body"], bg=C_STYLE["bg_card"],
                 fg=C_STYLE["text_primary"], wraplength=780).pack(fill=tk.X)
        row_i += 1

        # Summary card
        card = SectionCard(inner, "测试摘要" if zh else "Benchmark Summary",
                           collapsible=True, expanded=True)
        card.grid(row=row_i, column=0, sticky="ew",
                  padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_lg"]))
        succ  = row.get("success", 0) or 0
        fail  = row.get("fail", 0) or 0
        total = row.get("total_requests", 0) or 1
        conc  = row.get("concurrency", 0) or 0
        dur   = row.get("duration_sec", 0) or 0
        out_tps = row.get("output_token_throughput")
        ttft_avg = row.get("ttft_avg")
        ttft_p95 = row.get("ttft_p95")
        e2el_avg = row.get("e2el_avg")
        e2el_p95 = row.get("e2el_p95")
        tpot_avg = row.get("tpot_avg")
        req_rps  = row.get("request_throughput")
        sr = (succ / total * 100) if total else 0
        metrics_lines = [
            f"模型: {row.get('model_name', '-')}    并发: C{conc}    请求: {total}",
            f"成功: {succ}    失败: {fail}    成功率: {sr:.1f}%    耗时: {dur:.1f}s",
            (f"E2E avg: {e2el_avg:.3f}s" if e2el_avg is not None else "E2E avg: —") +
            "    " + (f"E2E P95: {e2el_p95:.3f}s" if e2el_p95 is not None else "E2E P95: —"),
            (f"TTFT avg: {ttft_avg:.3f}s" if ttft_avg is not None else "TTFT avg: —") +
            "    " + (f"TTFT P95: {ttft_p95:.3f}s" if ttft_p95 is not None else "TTFT P95: —"),
            (f"TPOT avg: {tpot_avg:.3f}s" if tpot_avg is not None else "TPOT avg: —") +
            "    " + (f"Output TPS: {out_tps:.1f}" if out_tps is not None else "Output TPS: —") +
            "    " + (f"RPS: {req_rps:.2f}" if req_rps is not None else "RPS: —"),
            f"Run ID: {run_id}",
            f"API: {row.get('api_url', '-')}",
            f"Status: {row.get('status', '-')}",
            f"Report Dir: {row.get('report_dir', '未保存')}",
        ]
        tk.Label(card.content, text="\n".join(metrics_lines), justify=tk.LEFT, anchor="w",
                 font=C_STYLE["font_body"], bg=C_STYLE["bg_card"],
                 fg=C_STYLE["text_primary"]).pack(fill=tk.X)
        row_i += 1

        # Histogram placeholder (E2E latency data not stored per-request in result DB)
        hist_card = SectionCard(inner, "E2E Latency 分布" if zh else "E2E Latency Distribution",
                                collapsible=True, expanded=True)
        hist_card.grid(row=row_i, column=0, sticky="ew",
                       padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_lg"]))
        hist_frame = tk.Frame(hist_card.content, bg=C_STYLE["bg_card"], height=200)
        hist_frame.pack(fill=tk.BOTH, expand=True)
        hist_frame.pack_propagate(False)
        hist_canvas = tk.Canvas(hist_frame, bg=C_STYLE["bg_card"], highlightthickness=0)
        hist_canvas.pack(fill=tk.BOTH, expand=True)
        note = ("Result DB 仅存储聚合指标，逐请求延迟分布数据请查看对应旧版历史记录。"
                if zh else
                "Result DB stores aggregate metrics only. Per-request latency histogram "
                "is available in the matching legacy history record.")
        hist_canvas.create_text(400, 90, text=note, fill=C_STYLE["text_muted"],
                                font=C_STYLE["font_small"], width=700)
        row_i += 1

        # Close button
        btn_bar = tk.Frame(inner, bg=C_STYLE["bg_main"])
        btn_bar.grid(row=row_i, column=0, sticky="e",
                     padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["pad_lg"]))
        ttk.Button(btn_bar, text="关闭" if zh else "Close",
                   style="Secondary.TButton",
                   command=self.history_detail_window.destroy).pack()

    # ── Legacy sweep detail ───────────────────────────────────────────────
    def _render_legacy_sweep_detail(self, parent: tk.Frame, ref: dict):
        zh = (self.lang_code == "zh_CN")
        rid = ref["rid"]
        row = self._load_history_row(rid)
        if not row:
            tk.Label(parent, text="历史记录不存在或已删除",
                     font=C_STYLE["font_body"], bg=C_STYLE["bg_main"],
                     fg=C_STYLE["error"]).pack(expand=True)
            return
        try:
            sweep_result = json.loads(self._row_get(row, "summary_json") or "{}")
        except Exception:
            sweep_result = {}
        cases = sweep_result.get("cases", [])

        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)
        scroll = ScrollableFrame(parent, bg=C_STYLE["bg_main"])
        scroll.grid(row=0, column=0, sticky="nsew")
        inner = scroll.content
        inner.configure(bg=C_STYLE["bg_main"])
        inner.grid_columnconfigure(0, weight=1)

        row_i = 0
        # Legacy env warning
        warn_card = SectionCard(inner, "被测环境" if zh else "Environment",
                                collapsible=True, expanded=True)
        warn_card.grid(row=row_i, column=0, sticky="ew",
                       padx=C_STYLE["pad_lg"], pady=(C_STYLE["pad_lg"], C_STYLE["gap_lg"]))
        tk.Label(warn_card.content,
                 text="⚠ 未绑定环境档案，本次结果不建议用于长期横向对比。",
                 font=C_STYLE["font_body"], bg=C_STYLE["bg_card"],
                 fg=C_STYLE["warning_text"]).pack(anchor="w")
        row_i += 1

        overview = SectionCard(inner, "扫测概览" if zh else "Sweep Overview",
                               collapsible=True, expanded=True)
        overview.grid(row=row_i, column=0, sticky="ew",
                      padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_lg"]))
        ov_txt = (
            f"模型: {self._row_get(row, 'model', '-')}\n"
            f"API: {self._row_get(row, 'api_url', '-')}\n"
            f"配置: {self._row_get(row, 'config_summary', '-')}\n"
            f"核心结果: {self._row_get(row, 'primary_metric', '-')}\n"
            f"状态: {self._row_get(row, 'status', '-')}"
        )
        tk.Label(overview.content, text=ov_txt, justify=tk.LEFT, anchor="w",
                 font=C_STYLE["font_body"], bg=C_STYLE["bg_card"],
                 fg=C_STYLE["text_primary"]).pack(fill=tk.X)
        row_i += 1

        tbl_card = SectionCard(inner, "并发档位明细" if zh else "Case Details",
                               collapsible=True, expanded=True)
        tbl_card.grid(row=row_i, column=0, sticky="ew",
                      padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_lg"]))
        cols = ("C", "Req", "Success", "Fail", "Rate%", "E2E P95", "Out TPS", "RPS")
        tv = ttk.Treeview(tbl_card.content, columns=cols, show="headings",
                          height=min(max(len(cases), 3), 8), style="App.Treeview")
        for col in cols:
            tv.heading(col, text=col)
            tv.column(col, anchor="center", width=95)
        tv.pack(fill=tk.X)
        for case in cases:
            s = case.get("benchmark_summary", {})
            tv.insert("", tk.END, values=(
                case.get("concurrency", "-"), case.get("total_requests", "-"),
                s.get("success", 0), s.get("fail", 0),
                f"{s.get('success_rate', 0) or 0:.0f}%",
                f"{s.get('e2e_latency_p95', 0) or 0:.3f}s",
                f"{s.get('system_output_tps', 0) or 0:.1f}",
                f"{s.get('request_throughput_rps', 0) or 0:.2f}",
            ))
        row_i += 1

        chart_card = SectionCard(inner, "图形分析" if zh else "Chart Analysis",
                                 collapsible=True, expanded=True)
        chart_card.grid(row=row_i, column=0, sticky="ew",
                        padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_lg"]))
        cf = tk.Frame(chart_card.content, bg=C_STYLE["bg_card"], height=460)
        cf.pack(fill=tk.BOTH, expand=True)
        cf.pack_propagate(False)
        if self._matplotlib_available() and cases:
            try:
                from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
                fig = self._build_sweep_analysis_figure(sweep_result)
                if fig is not None:
                    c2 = FigureCanvasTkAgg(fig, master=cf)
                    c2.draw()
                    c2.get_tk_widget().pack(fill=tk.BOTH, expand=True)
                else:
                    raise RuntimeError("no figure")
            except Exception as ex:
                tk.Label(cf, text=f"图表不可用: {ex}",
                         font=C_STYLE["font_body"], bg=C_STYLE["bg_card"],
                         fg=C_STYLE["warning_text"]).pack(expand=True)
        else:
            tk.Label(cf, text="matplotlib 未安装或暂无数据",
                     font=C_STYLE["font_body"], bg=C_STYLE["bg_card"],
                     fg=C_STYLE["warning_text"]).pack(expand=True)
        row_i += 1

        expert_card = SectionCard(inner, "专家分析" if zh else "Expert Analysis",
                                  collapsible=True, expanded=True)
        expert_card.grid(row=row_i, column=0, sticky="ew",
                         padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_lg"]))
        al = sweep_result.get("analysis_summary", [])
        commentary = self._generate_expert_commentary(cases, al) if cases else ""
        tk.Label(expert_card.content,
                 text=("\n".join([*al, "", commentary]).strip() or "未生成专家分析。"),
                 justify=tk.LEFT, anchor="w", font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"],
                 wraplength=820).pack(fill=tk.X)
        row_i += 1

        paths_card = SectionCard(inner, "输出文件" if zh else "Output Files",
                                 collapsible=True, expanded=True)
        paths_card.grid(row=row_i, column=0, sticky="ew",
                        padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_lg"]))
        _arts_leg  = self._resolve_history_artifacts(ref, dict(row), sweep_result)
        json_path  = _arts_leg["result_json"]
        md_path    = _arts_leg["report_md"]
        png_path   = _arts_leg["chart_png"]
        cust_pdf   = _arts_leg["customer_pdf"]
        cust_docx  = _arts_leg["customer_docx"]
        report_dir = _arts_leg["report_dir"]
        path_lines = [
            f"JSON:      {json_path or '未保存'}",
            f"Markdown:  {md_path or '未保存'}",
            f"PNG:       {png_path or '未保存'}",
        ]
        if report_dir:
            path_lines.insert(0, f"Report Dir: {report_dir}")
        if cust_pdf:
            path_lines.append(f"PDF 报告:  {cust_pdf}")
        if cust_docx:
            path_lines.append(f"DOCX 报告: {cust_docx}")
        tk.Label(paths_card.content, justify=tk.LEFT, anchor="w",
                 text="\n".join(path_lines),
                 font=C_STYLE["font_body"], bg=C_STYLE["bg_card"],
                 fg=C_STYLE["text_primary"]).pack(fill=tk.X)
        row_i += 1

        raw_card = SectionCard(inner, "Raw JSON", collapsible=True, expanded=False)
        raw_card.grid(row=row_i, column=0, sticky="ew",
                      padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_lg"]))
        txt_w = tk.Text(raw_card.content, height=10, wrap=tk.WORD,
                        font=C_STYLE["font_code"], bg=C_STYLE["bg_input"],
                        fg=C_STYLE["text_primary"], relief=tk.FLAT, borderwidth=0)
        txt_w.insert(tk.END, json.dumps(sweep_result, ensure_ascii=False,
                                        indent=2, default=str))
        txt_w.config(state=tk.DISABLED)
        txt_w.pack(fill=tk.BOTH, expand=True)
        row_i += 1

        btn_bar = tk.Frame(inner, bg=C_STYLE["bg_main"])
        btn_bar.grid(row=row_i, column=0, sticky="e",
                     padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["pad_lg"]))
        pdf_txt = "生成客户报告" if zh else "Generate Client Report"
        _j, _m, _p, _rd = json_path, md_path, png_path, report_dir
        pdf_btn = ttk.Button(btn_bar, text=pdf_txt, style="Primary.TButton",
                             command=lambda: self._generate_customer_report_for_history_record(
                                 ref, dict(row), sweep_result,
                                 _j, _m, _p, _rd, btn_bar))
        pdf_btn.pack(side=tk.LEFT, padx=(0, C_STYLE["gap_md"]))
        ttk.Button(btn_bar, text="关闭" if zh else "Close",
                   style="Secondary.TButton",
                   command=self.history_detail_window.destroy).pack(side=tk.LEFT)

    # ── Legacy single detail ──────────────────────────────────────────────
    def _render_legacy_single_detail(self, parent: tk.Frame, ref: dict):
        zh = (self.lang_code == "zh_CN")
        rid = ref["rid"]
        row = self._load_history_row(rid)
        if not row:
            tk.Label(parent, text="历史记录不存在或已删除",
                     font=C_STYLE["font_body"], bg=C_STYLE["bg_main"],
                     fg=C_STYLE["error"]).pack(expand=True)
            return
        detail = []
        try:
            detail = json.loads(self._row_get(row, "detail_json") or "[]")
        except Exception:
            pass
        latencies = [
            r.get("e2e_latency", r.get("latency", 0))
            for r in detail if r.get("ok")
        ] if detail else []
        metric_warnings = []
        try:
            metric_warnings = json.loads(self._row_get(row, "metric_warnings_json") or "[]")
        except Exception:
            pass

        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)
        scroll = ScrollableFrame(parent, bg=C_STYLE["bg_main"])
        scroll.grid(row=0, column=0, sticky="nsew")
        inner = scroll.content
        inner.configure(bg=C_STYLE["bg_main"])
        inner.grid_columnconfigure(0, weight=1)

        row_i = 0
        # Env warning card
        warn_card = SectionCard(inner, "被测环境" if zh else "Environment",
                                collapsible=True, expanded=True)
        warn_card.grid(row=row_i, column=0, sticky="ew",
                       padx=C_STYLE["pad_lg"], pady=(C_STYLE["pad_lg"], C_STYLE["gap_lg"]))
        tk.Label(warn_card.content,
                 text="⚠ 未绑定环境档案，本次结果不建议用于长期横向对比。",
                 font=C_STYLE["font_body"], bg=C_STYLE["bg_card"],
                 fg=C_STYLE["warning_text"]).pack(anchor="w")
        row_i += 1

        # Summary card
        card = SectionCard(inner, "测试摘要" if zh else "Summary",
                           collapsible=True, expanded=True)
        card.grid(row=row_i, column=0, sticky="ew",
                  padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_lg"]))
        succ  = self._row_get(row, "success", 0) or 0
        fail  = self._row_get(row, "fail", 0) or 0
        total = self._row_get(row, "total", 0) or 1
        conc  = self._row_get(row, "concurrency", 0)
        dur   = self._row_get(row, "duration_sec")
        e2e_avg  = self._row_get(row, "e2e_latency_avg")
        e2e_p95  = self._row_get(row, "e2e_latency_p95")
        ttft_avg = self._row_get(row, "ttft_avg")
        out_tps  = self._row_get(row, "system_output_tps")
        req_rps  = self._row_get(row, "request_throughput_rps")
        sr = (succ / total * 100) if total else 0
        metrics_lines = [
            f"模型: {self._row_get(row, 'model', '-')}    并发: C{conc}    请求: {total}",
            f"成功: {succ}    失败: {fail}    成功率: {sr:.1f}%    " +
            (f"耗时: {dur:.1f}s" if dur is not None else "耗时: —"),
            (f"E2E avg: {e2e_avg:.3f}s" if e2e_avg is not None else "E2E avg: —") +
            "    " + (f"E2E P95: {e2e_p95:.3f}s" if e2e_p95 is not None else "E2E P95: —"),
            (f"TTFT avg: {ttft_avg:.3f}s" if ttft_avg is not None else "TTFT avg: —") +
            "    " + (f"Output TPS: {out_tps:.1f}" if out_tps is not None else "Output TPS: —") +
            "    " + (f"RPS: {req_rps:.2f}" if req_rps is not None else "RPS: —"),
        ]
        if metric_warnings:
            metrics_lines.append(f"⚠ {len(metric_warnings)} 条指标警告")
            for w in metric_warnings[:3]:
                metrics_lines.append(f"  • {w[:100]}")
        tk.Label(card.content, text="\n".join(metrics_lines), justify=tk.LEFT, anchor="w",
                 font=C_STYLE["font_body"], bg=C_STYLE["bg_card"],
                 fg=C_STYLE["text_primary"]).pack(fill=tk.X)
        row_i += 1

        # E2E Histogram
        hist_card = SectionCard(inner, "E2E Latency 分布" if zh else "E2E Latency Distribution",
                                collapsible=True, expanded=True)
        hist_card.grid(row=row_i, column=0, sticky="ew",
                       padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_lg"]))
        hist_frame = tk.Frame(hist_card.content, bg=C_STYLE["bg_card"], height=200)
        hist_frame.pack(fill=tk.BOTH, expand=True)
        hist_frame.pack_propagate(False)
        hist_canvas = tk.Canvas(hist_frame, bg=C_STYLE["bg_card"],
                                highlightthickness=0, bd=0)
        hist_canvas.pack(fill=tk.BOTH, expand=True)

        def _redraw_hist(event=None):
            if latencies:
                self._draw_popup_histogram(hist_canvas, latencies)
        hist_canvas.bind("<Configure>", _redraw_hist, add="+")
        hist_card.content.after(120, _redraw_hist)
        row_i += 1

        # Close button
        btn_bar = tk.Frame(inner, bg=C_STYLE["bg_main"])
        btn_bar.grid(row=row_i, column=0, sticky="e",
                     padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["pad_lg"]))
        ttk.Button(btn_bar, text="关闭" if zh else "Close",
                   style="Secondary.TButton",
                   command=self.history_detail_window.destroy).pack()

    # ── Env summary helper ────────────────────────────────────────────────
    def _build_env_summary_lines(self, row: dict, zh: bool) -> list:
        """Build environment summary text lines from a benchmark_runs dict."""
        env_name  = row.get("environment_profile_name_snapshot") or ""
        hw_name   = row.get("hardware_profile_name_snapshot") or "-"
        sw_name   = row.get("software_stack_profile_name_snapshot") or "-"
        mod_name  = row.get("model_profile_name_snapshot") or "-"
        gpu_model = row.get("gpu_model_snapshot") or "-"
        gpu_count = row.get("gpu_count_snapshot") or "-"
        backend   = row.get("backend_snapshot") or "-"
        bk_ver    = row.get("backend_version_snapshot") or "-"
        api_type  = row.get("api_type_snapshot") or "-"
        deploy    = row.get("deployment_type_snapshot") or "-"
        parser    = row.get("reasoning_parser_snapshot") or "-"
        mfamily   = row.get("model_family_snapshot") or "-"
        msize     = row.get("model_size_snapshot") or "-"
        quant     = row.get("quantization_snapshot") or "-"
        mtype     = row.get("model_type_snapshot") or "-"
        lines = [
            f"  Environment Profile: {env_name or '未指定环境'}",
            f"  Hardware Profile:    {hw_name}",
            f"  GPU:                 {gpu_model}  x{gpu_count}",
            f"  Software Stack:      {sw_name}",
            f"  Backend:             {backend} {bk_ver}",
            f"  API Type:            {api_type}",
            f"  Deployment:          {deploy}",
            f"  Reasoning Parser:    {parser}",
            f"  Model Profile:       {mod_name}",
            f"  Model:               {row.get('model_name', '-')}",
            f"  Family/Size:         {mfamily} {msize}",
            f"  Quantization:        {quant}",
            f"  Model Type:          {mtype}",
        ]
        if not env_name:
            lines.append("  ⚠ 未绑定环境档案，本次结果不建议用于长期横向对比。")
        return lines

    # ── Artifact resolution helpers ───────────────────────────────────────

    def _scan_report_dir_for_artifacts(self, report_dir: str) -> dict:
        """Glob-scan *report_dir* and return best-guess artifact paths.

        Returns a dict with keys: result_json, report_md, chart_png,
        customer_pdf, customer_docx.  Values are absolute path strings or ''.
        """
        from pathlib import Path as _Path
        result = {
            "result_json": "", "report_txt": "", "report_md": "",
            "chart_png": "", "customer_pdf": "", "customer_docx": "",
        }
        if not report_dir:
            return result
        p = _Path(report_dir)
        if not p.is_dir():
            return result

        # ── report.txt ────────────────────────────────────────────────────
        txt_cands = sorted(
            p.glob("report*.txt"),
            key=lambda f: f.stat().st_mtime, reverse=True,
        )
        if txt_cands:
            result["report_txt"] = str(txt_cands[0])

        # ── result JSON ───────────────────────────────────────────────────
        # Prefer files whose stem starts with "result" or "sweep"; exclude
        # generation-summary files and pdf_report_summary files.
        def _is_data_json(f):
            stem = f.stem.lower()
            return ("summary" not in stem and "pdf_report" not in stem
                    and "generation" not in stem)

        pref_json = sorted(
            (f for f in p.glob("result*.json") if _is_data_json(f)),
            key=lambda f: f.stat().st_mtime, reverse=True,
        )
        if not pref_json:
            pref_json = sorted(
                (f for f in p.glob("*.json") if _is_data_json(f)),
                key=lambda f: f.stat().st_mtime, reverse=True,
            )
        if pref_json:
            result["result_json"] = str(pref_json[0])

        # ── Markdown ──────────────────────────────────────────────────────
        report_kws = ("report", "sweep", "analysis", "benchmark")
        md_cands = sorted(
            p.glob("*.md"),
            key=lambda f: (
                not any(kw in f.stem.lower() for kw in report_kws),
                -f.stat().st_mtime,
            ),
        )
        if md_cands:
            result["report_md"] = str(md_cands[0])

        # ── PNG chart — scan root dir and charts/ subdir ──────────────────
        chart_kws = ("chart", "analysis", "sweep", "benchmark", "histogram", "e2e")
        png_search_dirs = [p]
        _charts_sub = p / "charts"
        if _charts_sub.is_dir():
            png_search_dirs.append(_charts_sub)
        _all_pngs: list = []
        for _sd in png_search_dirs:
            _all_pngs.extend(_sd.glob("*.png"))
        png_cands = sorted(
            _all_pngs,
            key=lambda f: (
                not any(kw in f.stem.lower() for kw in chart_kws),
                -f.stat().st_mtime,
            ),
        )
        if png_cands:
            result["chart_png"] = str(png_cands[0])

        # ── Customer PDF / DOCX ───────────────────────────────────────────
        for ext, key in [("pdf", "customer_pdf"), ("docx", "customer_docx")]:
            preferred = sorted(
                (f for f in p.glob(f"*.{ext}")
                 if "customer" in f.stem.lower() or "jisuman" in f.stem.lower()
                 or "acceptance" in f.stem.lower()),
                key=lambda f: f.stat().st_mtime, reverse=True,
            )
            fallback = sorted(
                p.glob(f"*.{ext}"),
                key=lambda f: f.stat().st_mtime, reverse=True,
            )
            cands = preferred or fallback
            if cands:
                result[key] = str(cands[0])

        return result

    def _resolve_history_artifacts(
            self, ref: dict, row: dict = None,
            sweep_result: dict = None) -> dict:
        """Resolve artifact paths for a history record using 5-tier priority.

        Priority (highest → lowest):
          1. benchmark_artifacts table (result_db records)
          2. sweep_result embedded fields (json_path, report_md, report_png)
          3. Legacy DB row columns (json_path, markdown_path, png_path)
          4. Derive report_dir from any found path
          5. Scan report_dir with glob

        Returns dict with keys:
          report_dir, result_json, report_md, chart_png,
          customer_pdf, customer_docx, pdf_log
        """
        if row is None:
            row = {}
        if sweep_result is None:
            sweep_result = {}

        arts = {
            "report_dir":   "",
            "result_json":  "",
            "report_txt":   "",
            "report_md":    "",
            "chart_png":    "",
            "customer_pdf": "",
            "customer_docx": "",
            "pdf_log":      "",
        }

        # Seed report_dir from row / sweep_result / ref.run_dir
        arts["report_dir"] = (
            ref.get("run_dir", "")
            or (row.get("report_dir") if isinstance(row, dict) else "")
            or sweep_result.get("report_dir", "")
            or ""
        )

        # ── Tier 0.5: ResultStore source — direct path resolution ─────────
        if ref.get("source") == "result_store":
            _rs_dir = ref.get("run_dir", "") or arts["report_dir"]
            if _rs_dir and os.path.isdir(_rs_dir):
                # report.txt candidates
                for _rname in ("report.txt", "report_v2.txt"):
                    _rp = os.path.join(_rs_dir, _rname)
                    if os.path.isfile(_rp):
                        arts["report_txt"] = _rp
                        break
                if not arts["report_txt"]:
                    _txts = sorted(
                        (p for p in __import__("pathlib").Path(_rs_dir).glob("report*.txt")
                         if p.is_file()),
                        key=lambda f: f.stat().st_mtime, reverse=True,
                    )
                    if _txts:
                        arts["report_txt"] = str(_txts[0])
                # result.json candidates
                for _jname in ("result.json",):
                    _jp = os.path.join(_rs_dir, _jname)
                    if os.path.isfile(_jp):
                        arts["result_json"] = _jp
                        break
                # chart PNG — check root dir and charts/ subdir
                _charts_dirs = [_rs_dir, os.path.join(_rs_dir, "charts")]
                for _cdir in _charts_dirs:
                    if not os.path.isdir(_cdir):
                        continue
                    _pngs = sorted(
                        (p for p in __import__("pathlib").Path(_cdir).glob("*.png")
                         if p.is_file()),
                        key=lambda f: f.stat().st_mtime, reverse=True,
                    )
                    if _pngs:
                        arts["chart_png"] = str(_pngs[0])
                        break

        # ── Tier 1: benchmark_artifacts table (result_db only) ────────────
        if ref.get("source") == "result_db":
            run_id = ref.get("run_id", "")
            db_path = (
                getattr(self, "result_db_path_var", None) and
                self.result_db_path_var.get()
            ) or RESULT_DB_PATH
            try:
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                rows_art = conn.execute(
                    "SELECT artifact_type, path FROM benchmark_artifacts"
                    " WHERE run_id=?", (run_id,)
                ).fetchall()
                conn.close()
                for ar in rows_art:
                    atype = ar["artifact_type"]
                    apath = ar["path"] or ""
                    if not apath or not os.path.isfile(apath):
                        continue
                    if atype == "customer_pdf_report":
                        arts["customer_pdf"] = apath
                    elif atype == "customer_docx_report":
                        arts["customer_docx"] = apath
                    elif atype == "pdf_generation_log":
                        arts["pdf_log"] = apath
            except Exception:
                pass

        # ── Tier 2: sweep_result embedded fields ──────────────────────────
        for src_key, art_key in [
            ("json_path",   "result_json"),
            ("result_json", "result_json"),
            ("report_md",   "report_md"),
            ("report_png",  "chart_png"),
        ]:
            if not arts[art_key]:
                p = sweep_result.get(src_key, "") or ""
                if p and os.path.isfile(p):
                    arts[art_key] = p

        # ── Tier 3: legacy DB row columns ────────────────────────────────
        for col, art_key in [
            ("json_path",      "result_json"),
            ("markdown_path",  "report_md"),
            ("png_path",       "chart_png"),
        ]:
            if not arts[art_key]:
                p = (row.get(col, "") if isinstance(row, dict)
                     else self._row_get(row, col, "")) or ""
                if p and os.path.isfile(p):
                    arts[art_key] = p

        # ── Tier 4: derive report_dir from any found path ─────────────────
        if not arts["report_dir"]:
            for k in ("result_json", "report_md", "chart_png",
                      "customer_pdf", "customer_docx"):
                if arts[k]:
                    arts["report_dir"] = os.path.dirname(arts[k])
                    break

        # ── Tier 5: scan report_dir ────────────────────────────────────────
        if arts["report_dir"] and os.path.isdir(arts["report_dir"]):
            scanned = self._scan_report_dir_for_artifacts(arts["report_dir"])
            for k in ("result_json", "report_txt", "report_md", "chart_png",
                      "customer_pdf", "customer_docx"):
                if not arts[k] and scanned.get(k):
                    arts[k] = scanned[k]

        return arts

    # ── PDF report integration ────────────────────────────────────────────

    def _get_pdf_report_generator_path(self) -> str:
        """Return path to generate_pdf_report.py. Configurable, defaults to /opt/..."""
        default = "/opt/jisuman-pdf-report-generator/generate_pdf_report.py"
        val = getattr(self, "pdf_generator_path_var", None)
        if val and val.get():
            return val.get()
        return default

    def _get_pdf_python_executable(self) -> str:
        default = "python3"
        val = getattr(self, "pdf_python_var", None)
        if val and val.get():
            return val.get()
        return default

    def _generate_customer_report_for_current_sweep(self):
        """Called from sweep tab "生成客户报告" button."""
        zh = (self.lang_code == "zh_CN")
        sweep_result = getattr(self, "_sweep_result", None)
        if not sweep_result or not sweep_result.get("cases"):
            messagebox.showwarning(
                "提示" if zh else "Warning",
                "请先完成扫测并保存结果文件。\n(No completed sweep result found.)"
            )
            return
        json_path  = sweep_result.get("json_path", "")
        md_path    = sweep_result.get("report_md", "")
        png_path   = sweep_result.get("report_png", "")
        report_dir = getattr(self, "_current_sweep_run_dir", "") or ""
        if not report_dir and json_path:
            report_dir = os.path.dirname(json_path)
        missing = []
        if not json_path or not os.path.isfile(json_path):
            missing.append("result.json")
        if not md_path or not os.path.isfile(md_path):
            missing.append("report.md/report.txt")
        if not png_path or not os.path.isfile(png_path):
            missing.append("chart.png")
        if missing:
            msg = ("缺少生成报告所需文件：\n" + "\n".join(f"  - {m}" for m in missing) +
                   "\n\n请先完成扫测并启用保存报告/图片。")
            messagebox.showwarning("缺少文件" if zh else "Missing Files", msg)
            return
        self._run_pdf_report_generator_async(
            json_path, md_path, png_path, report_dir,
            parent_btn=getattr(self, "_sweep_pdf_btn", None))

    def _generate_customer_report_for_history_record(
            self, ref, row, sweep_result, json_path, md_path, png_path, report_dir, btn_bar):
        """Called from history detail "生成客户报告" button.

        Re-resolves artifacts so paths are always fresh, then validates
        mandatory files before launching the async generator.
        """
        zh = (self.lang_code == "zh_CN")

        # Re-resolve artifacts to catch any files written after the window opened
        _arts = self._resolve_history_artifacts(ref, row, sweep_result)
        # Caller-supplied paths take precedence if they are valid files
        _json = (json_path if json_path and os.path.isfile(json_path)
                 else _arts["result_json"])
        _md   = (md_path if md_path and os.path.isfile(md_path)
                 else _arts["report_md"])
        _png  = (png_path if png_path and os.path.isfile(png_path)
                 else _arts["chart_png"])
        _rd   = report_dir or _arts["report_dir"]
        if not _rd and _json:
            _rd = os.path.dirname(_json)

        missing = []
        if not _json or not os.path.isfile(_json):
            missing.append(
                f"result.json  {'(未找到)' if not _json else '(文件不存在: ' + _json + ')'}"
            )
        if not _md or not os.path.isfile(_md):
            missing.append(
                f"report.md  {'(未找到)' if not _md else '(文件不存在: ' + _md + ')'}"
            )
        # PNG is non-fatal; warn but do not block
        png_warn = ""
        if not _png or not os.path.isfile(_png):
            png_warn = (
                "\n⚠ chart.png 未找到，将生成无图表版报告。"
                if zh else
                "\n⚠ chart.png not found — report will be generated without a chart."
            )

        if missing:
            msg = (
                ("缺少生成报告所需文件：\n" if zh else "Missing required files:\n")
                + "\n".join(f"  · {m}" for m in missing)
                + (("\n\n报告目录: " + (_rd or "未知")) if _rd else "")
                + "\n\n请确认扫测时已启用保存 JSON 结果。"
            )
            messagebox.showwarning("缺少文件" if zh else "Missing Files", msg)
            return

        if png_warn:
            if not messagebox.askyesno(
                "确认" if zh else "Confirm",
                png_warn + ("\n\n是否继续生成报告？" if zh else "\n\nContinue generating report?"),
            ):
                return

        self._run_pdf_report_generator_async(
            _json, _md, _png, _rd, parent_btn=None)

    def _run_pdf_report_generator_async(
            self, json_path: str, md_path: str, png_path: str,
            report_dir: str, parent_btn=None):
        """Invoke generate_pdf_report.py in a background thread."""
        import threading as _threading
        zh = (self.lang_code == "zh_CN")
        gen_path = self._get_pdf_report_generator_path()
        python_exe = self._get_pdf_python_executable()

        if not os.path.isfile(gen_path):
            messagebox.showerror(
                "错误" if zh else "Error",
                f"未找到 PDF 报告生成器，请检查路径：\n{gen_path}"
            )
            return

        lang_arg = "zh_CN" if zh else "en_US"
        lang_suffix = lang_arg
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if not report_dir:
            report_dir = os.path.dirname(json_path) if json_path else "."
        os.makedirs(report_dir, exist_ok=True)
        out_pdf  = os.path.join(report_dir, f"customer_acceptance_report_{lang_suffix}.pdf")
        out_docx = os.path.join(report_dir, f"customer_acceptance_report_{lang_suffix}.docx")
        log_path = os.path.join(report_dir, f"pdf_report_generation.log")

        cmd = [python_exe, gen_path,
               "--result-json", json_path,
               "--output", out_pdf,
               "--output-docx", out_docx,
               "--report-type", "customer_acceptance",
               "--lang", lang_arg]
        if md_path and os.path.isfile(md_path):
            cmd += ["--report-md", md_path]
        if png_path and os.path.isfile(png_path):
            cmd += ["--chart-png", png_path]

        # Disable button while running
        if parent_btn:
            try:
                parent_btn.config(state=tk.DISABLED)
            except Exception:
                pass

        # Status popup
        status_win = tk.Toplevel(self.root)
        status_win.title("正在生成报告..." if zh else "Generating Report...")
        status_win.geometry("420x120")
        status_win.resizable(False, False)
        status_win.configure(bg=C_STYLE["bg_card"])
        status_var = tk.StringVar(value="正在生成客户报告..." if zh else "Generating client report...")
        tk.Label(status_win, textvariable=status_var, font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"],
                 wraplength=380).pack(expand=True)

        def _bg_run():
            import subprocess as _sp
            try:
                result = _sp.run(cmd, capture_output=True, text=True, timeout=180)
                stdout = result.stdout
                stderr = result.stderr
                rc = result.returncode
                with open(log_path, "w", encoding="utf-8") as lf:
                    lf.write(f"CMD: {' '.join(cmd)}\nRC: {rc}\n\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}\n")
                success = (rc == 0)
            except Exception as ex:
                success = False
                rc = -1
                stderr = str(ex)
                stdout = ""
                try:
                    with open(log_path, "w", encoding="utf-8") as lf:
                        lf.write(f"CMD: {' '.join(cmd)}\nEXCEPTION: {ex}\n")
                except Exception:
                    pass
            self.root.after(0, lambda: self._on_pdf_report_done(
                success, out_pdf, out_docx, log_path, json_path,
                stderr, status_win, status_var, parent_btn))

        t = _threading.Thread(target=_bg_run, daemon=True)
        t.start()

    def _on_pdf_report_done(self, success, out_pdf, out_docx, log_path,
                             json_path, stderr, status_win, status_var, parent_btn):
        zh = (self.lang_code == "zh_CN")
        try:
            status_win.destroy()
        except Exception:
            pass
        if parent_btn:
            try:
                parent_btn.config(state=tk.NORMAL)
            except Exception:
                pass

        if success:
            msg = (f"客户报告生成完成！\n\nPDF:  {out_pdf}\nDOCX: {out_docx}"
                   if zh else
                   f"Client report generated!\n\nPDF:  {out_pdf}\nDOCX: {out_docx}")
            messagebox.showinfo("完成" if zh else "Done", msg)
            # Try to write pdf_report_summary.json
            try:
                summary = {
                    "status": "passed",
                    "language": "zh_CN" if zh else "en_US",
                    "output_pdf": out_pdf,
                    "output_docx": out_docx,
                    "log_path": log_path,
                    "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                summary_json_path = os.path.join(
                    os.path.dirname(out_pdf), "pdf_report_summary.json")
                with open(summary_json_path, "w", encoding="utf-8") as f:
                    json.dump(summary, f, ensure_ascii=False, indent=2)
                self._record_pdf_artifacts(json_path, out_pdf, out_docx, log_path)
            except Exception:
                pass
        else:
            err_summary = (stderr or "").strip()[:300]
            # Friendly error messages
            if "soffice" in err_summary or "LibreOffice" in err_summary:
                hint = "未找到 LibreOffice / soffice，无法转换 DOCX 到 PDF。"
            elif "python-docx" in err_summary or "import" in err_summary.lower():
                hint = "PDF 生成器依赖未安装，请执行: pip install python-docx pypdf pillow"
            else:
                hint = f"错误摘要: {err_summary}"
            msg = (f"客户报告生成失败。\n\n{hint}\n\n日志: {log_path}"
                   if zh else
                   f"Client report generation failed.\n\n{hint}\n\nLog: {log_path}")
            messagebox.showerror("生成失败" if zh else "Failed", msg)

    def _record_pdf_artifacts(self, json_path, out_pdf, out_docx, log_path):
        """Record generated PDF/DOCX as benchmark_artifacts in result DB (best-effort)."""
        try:
            db_path = getattr(self, "result_db_path_var",
                              tk.StringVar(value=RESULT_DB_PATH)).get() or RESULT_DB_PATH
            if not os.path.exists(db_path):
                return
            conn = sqlite3.connect(db_path)
            # Find run_id by report_dir proximity
            report_dir = os.path.dirname(out_pdf)
            rows = conn.execute(
                "SELECT run_id FROM benchmark_runs WHERE report_dir=?",
                (report_dir,)).fetchall()
            for r in rows:
                run_id = r[0]
                for atype, apath in [
                    ("customer_pdf_report", out_pdf),
                    ("customer_docx_report", out_docx),
                    ("pdf_generation_log", log_path),
                ]:
                    try:
                        conn.execute(
                            """INSERT OR REPLACE INTO benchmark_artifacts
                               (run_id, artifact_type, path, created_at)
                               VALUES (?,?,?,?)""",
                            (run_id, atype, apath,
                             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                    except Exception:
                        pass
            conn.commit()
            conn.close()
        except Exception:
            pass

# ============================================================
# Entry point
# ============================================================
def _show_crash(title: str, msg: str):
    """Last-resort error display — tries GUI, falls back to log file."""
    try:
        import tkinter.messagebox as mb
        root = tk.Tk()
        root.withdraw()
        mb.showerror(title, msg)
        root.destroy()
    except Exception:
        with open(CRASH_LOG, "w", encoding="utf-8") as f:
            f.write(f"{title}\n{msg}\n")

def main():
    global DEBUG_MODE
    if "-debug" in sys.argv:
        DEBUG_MODE = True
        _runner_module.DEBUG_MODE = True
        _history_db_module.DEBUG_MODE = True
        setup_logging()
        print(f"[debug] 日志已启用，输出到 {LOG_PATH}")
    root = tk.Tk()
    LLMBenchmarkApp(root)
    root.mainloop()

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        detail = traceback.format_exc()
        _show_crash("LLM Benchmark GUI 启动失败",
                    f"程序启动时发生错误:\n\n{type(e).__name__}: {e}\n\n"
                    f"详细信息已写入 llm_benchmark_crash.log")
        with open(CRASH_LOG, "w", encoding="utf-8") as f:
            f.write(detail)
