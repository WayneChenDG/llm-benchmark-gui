#!/usr/bin/env python3
"""LLM Benchmark GUI — 并发性能测试工具
纯标准库实现：tkinter + sqlite3 + urllib + threading，无需额外安装依赖。
支持 OpenAI 兼容 API（通义千问 / DeepSeek / GLM / GPT 等）。

Metrics aligned with:
  • vLLM bench serve:  ttft, tpot, itl, e2el
  • NVIDIA GenAI-Perf: output_token_throughput, request_throughput, ttft, itl
  • NIM Benchmark:      output_token_throughput, request_throughput
"""
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

# Resolve paths relative to script location (fixes double-click on Windows)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_SCRIPT_DIR, "llm_benchmark_history.db")
INI_PATH = os.path.join(_SCRIPT_DIR, "llm_benchmark.ini")
LOG_PATH = os.path.join(_SCRIPT_DIR, "llm_benchmark.log")
CRASH_LOG = os.path.join(_SCRIPT_DIR, "llm_benchmark_crash.log")
DEBUG_MODE = False
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
    "自定义":                    {"concurrency": 64, "total": 640, "desc": "自定义并发与请求数，可手动修改"},
}
DEFAULT_PRESET_KEY = "标准基线 — C8/N80（默认）"
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
        "app.title": "JISUMAN LLM Benchmark GUI",
        "app.subtitle": "OpenAI 兼容接口并发性能测试",
        "language.label": "语言",
        "language.zh": "简体中文",
        "language.en": "English",
        "tab.settings": "参数设置",
        "tab.benchmark": "基准测试",
        "tab.sweep": "并发扫测",
        "tab.history": "历史记录",
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
        "history.type_all": "All",
        "history.type_single": "Single",
        "history.type_sweep": "Sweep",
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
    },
    "en_US": {
        "app.title": "JISUMAN LLM Benchmark GUI",
        "app.subtitle": "OpenAI-compatible API concurrency benchmark",
        "language.label": "Language",
        "language.zh": "简体中文",
        "language.en": "English",
        "tab.settings": "Settings",
        "tab.benchmark": "Benchmark",
        "tab.sweep": "Concurrency Sweep",
        "tab.history": "History",
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
# Design: Modern SaaS dashboard — warm gray bg, white cards, left-accent metrics,
#         Stripe purple accent, Datadog/Linear-inspired clean hierarchy.
C_STYLE = {
    # ── surfaces ──
    "bg_main": "#F5F6FA",        # warm dashboard background
    "bg_card": "#FFFFFF",        # elevated cards
    "bg_header": "#FFFFFF",      # top bar
    "bg_input": "#FFFFFF",
    "bg_hover": "#EEF0F6",
    "bg_stripe": "#F8F7FF",      # subtle purple-tinted surface
    # ── text ──
    "text_primary": "#1E293B",   # slate-800 — sharp but not black
    "text_secondary": "#64748B", # slate-500 — body / labels
    "text_muted": "#94A3B8",     # slate-400 — hints
    "text_inverse": "#FFFFFF",
    # ── borders ──
    "border": "#E2E8F0",         # slate-200 — card edges
    "border_light": "#F1F5F9",   # subtle separators
    "border_focus": "#533AFD",
    # ── accent (Stripe purple) ──
    "accent": "#533AFD",
    "accent_hover": "#4434D4",
    "accent_light": "#F0EEFF",   # tinted bg for accent areas
    "accent_soft": "#E8E4FF",    # slightly stronger tint
    # ── semantic ──
    "success": "#10B981",        # emerald green
    "success_bg": "#ECFDF5",
    "success_text": "#065F46",
    "warning": "#F59E0B",        # amber
    "warning_bg": "#FFFBEB",
    "warning_text": "#92400E",
    "error": "#EF4444",          # red
    "error_bg": "#FEF2F2",
    "error_text": "#991B1B",
    "info": "#3B82F6",           # blue
    "info_bg": "#EFF6FF",
    "info_text": "#1E40AF",
    # ── fonts: 10→12→13→15→18→26 (Segoe UI, proportional scale) ──
    "font_title": (FONT_FAMILY, 18, "bold"),
    "font_subtitle": (FONT_FAMILY, 12),
    "font_section": (FONT_FAMILY, 13, "bold"),
    "font_label": (FONT_FAMILY, 12),
    "font_body": (FONT_FAMILY, 12),
    "font_status": (FONT_FAMILY, 15, "bold"),
    "font_metric": (FONT_FAMILY, 26, "bold"),
    "font_small": (FONT_FAMILY, 10),
    "font_code": ("Consolas", 10),
    # ── spacing ──
    "radius_card": 8,
    "radius_btn": 6,
    "radius_input": 6,
    "pad_lg": 24,
    "pad_md": 16,
    "pad_sm": 10,
    "gap_lg": 20,
    "gap_md": 14,
    "gap_sm": 10,
    # ── accent bars ──
    "bar_width": 4,              # left accent strip width
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
    COLORS = {
        "ttft": C_STYLE["info"], "tps": C_STYLE["accent"],
        "total_tokens": C_STYLE["warning"], "agg_tps": C_STYLE["success"],
        "e2e_p95": C_STYLE["error"], "rps": C_STYLE["info"],
        "system_output_tps": C_STYLE["success"], "output_tokens": C_STYLE["warning"],
        "tpot": C_STYLE["accent"], "itl": C_STYLE["accent"],
        "success_rate": C_STYLE["success"],
        "visible_ttft": C_STYLE["warning"],
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
        # position below the widget
        tw.update_idletasks()
        x = self.winfo_rootx() + 4
        y = self.winfo_rooty() + self.winfo_height() + 2
        tw.geometry(f"+{x}+{y}")
    def _hide_tooltip(self, event=None):
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
    def set_text(self, text: str):
        self.text_lbl.config(text=text)
        if text:
            self.grid()
        else:
            self.grid_remove()
# ============================================================
# Database — JISUMAN LLM Benchmark Standard v1 schema
# ============================================================
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
# ============================================================
# API caller
# ============================================================
# ---------- utility: percentile & filter ----------
def clean_numbers(values):
    """Return a list with only non-None numeric values."""
    return [v for v in values if isinstance(v, (int, float)) and v is not None]

def percentile(data, p):
    """Compute the p-th percentile (0-100) of a list of numbers.
    Uses linear interpolation. Returns 0 for empty data."""
    data = sorted(clean_numbers(data))
    if not data:
        return 0
    k = (len(data) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(data) - 1)
    return data[f] + (k - f) * (data[c] - data[f]) if c > f else data[f]

# ============================================================
# JISUMAN LLM Benchmark Standard v1 — metric definitions & aliases
# ============================================================
DB_SCHEMA_VERSION = 2

STANDARD_METRIC_ALIASES = {
    "ttft": "ttft",
    "tpot": "tpot",
    "itl": "itl_avg",
    "e2el": "e2e_latency",
    "output_token_throughput": "system_output_tps",
    "request_throughput": "request_throughput_rps",
}

def get_metric_standard_definitions() -> dict:
    """Return JISUMAN LLM Benchmark Standard v1 metric definitions.

    The metric set aligns with common LLM serving benchmark terminology:
    - vLLM bench serve: ttft, tpot, itl, e2el
    - NVIDIA GenAI-Perf / NIM Benchmark style: output token throughput,
      request throughput, time to first token, inter token latency
    """
    return {
        "standard_name": "JISUMAN LLM Benchmark Standard v1",
        "references": [
            "vLLM bench serve: ttft / tpot / itl / e2el",
            "NVIDIA GenAI-Perf / NIM-style metrics: output token throughput / request throughput / time to first token / inter token latency",
        ],
        "metrics": {
            "ttft": {
                "display_name": "TTFT",
                "full_name": "Time To First Token",
                "definition": "request_start 到首个非空输出 chunk/token 的时间",
                "formula": "first_output_time - request_start",
                "unit": "seconds",
                "source": "streaming response timestamps",
                "note": "当前工具无 tokenizer，按首个非空流式 chunk 估算 first token",
            },
            "e2el": {
                "display_name": "E2E Latency",
                "full_name": "End-to-End Latency",
                "definition": "request_start 到完整响应结束的时间",
                "formula": "response_end - request_start",
                "unit": "seconds",
                "source": "client-side timer",
            },
            "tpot": {
                "display_name": "TPOT",
                "full_name": "Time Per Output Token",
                "definition": "首 token 之后每个输出 token 的平均耗时",
                "formula": "(e2e_latency - ttft) / max(output_tokens - 1, 1)",
                "unit": "seconds/token",
                "source": "usage.completion_tokens + streaming timestamps",
            },
            "itl": {
                "display_name": "ITL",
                "full_name": "Inter Token Latency",
                "definition": "相邻流式输出 chunk/token 的时间间隔",
                "formula": "timestamp[i] - timestamp[i-1]",
                "unit": "seconds",
                "source": "streaming chunk timestamps",
                "note": "当前工具无 tokenizer，ITL 为 chunk-level approximation，不得声称严格 token-level",
            },
            "system_output_tps": {
                "display_name": "System Output TPS",
                "full_name": "System Output Token Throughput",
                "definition": "整轮 benchmark 的输出 token 吞吐",
                "formula": "sum(completion_tokens) / benchmark_duration",
                "unit": "tokens/s",
                "source": "usage.completion_tokens",
            },
            "system_total_tps": {
                "display_name": "System Total TPS",
                "full_name": "System Total Token Throughput",
                "definition": "整轮 benchmark 的输入+输出 token 总吞吐",
                "formula": "sum(prompt_tokens + completion_tokens) / benchmark_duration",
                "unit": "tokens/s",
                "source": "usage.total_tokens or prompt_tokens + completion_tokens",
            },
            "request_throughput_rps": {
                "display_name": "Request Throughput",
                "full_name": "Requests Per Second",
                "definition": "成功请求数除以整轮 benchmark 持续时间",
                "formula": "successful_requests / benchmark_duration",
                "unit": "req/s",
                "source": "benchmark summary",
            },
            "per_request_output_tps": {
                "display_name": "Per-request Output TPS",
                "full_name": "Per-request Output Token Speed",
                "definition": "单请求维度的输出速度",
                "formula": "completion_tokens / e2e_latency",
                "unit": "tokens/s",
                "source": "per-request usage + e2e latency",
                "note": "这是用户侧单请求体验速度，不等于系统总吞吐",
            },
        },
    }

def validate_metric_consistency(summary: dict) -> list[str]:
    """Return warnings when benchmark metrics violate expected relationships.

    Tolerance: relative 3%, absolute 0.05
    """
    warnings = []
    eps_rel = 0.03
    eps_abs = 0.05

    def _close(a, b):
        if a == 0 and b == 0:
            return True
        return abs(a - b) <= max(abs(a), abs(b)) * eps_rel + eps_abs

    dur = max(summary.get("duration_sec", 0.001), 0.001)
    ok_count = summary.get("success", 0)
    out_tok = summary.get("total_output_tokens", 0)
    in_tok = summary.get("total_input_tokens", 0)
    tot_tok = summary.get("total_tokens", 0)

    # 1-3: token sums consistency
    if not _close(tot_tok, in_tok + out_tok):
        warnings.append(f"total_tokens ({tot_tok}) != input ({in_tok}) + output ({out_tok})")

    # 4-6: throughput consistency
    if not _close(summary.get("system_output_tps", 0), out_tok / dur):
        warnings.append("system_output_tps != total_output_tokens / duration_sec")
    if not _close(summary.get("system_total_tps", 0), tot_tok / dur):
        warnings.append("system_total_tps != total_tokens / duration_sec")
    if not _close(summary.get("request_throughput_rps", 0), ok_count / dur):
        warnings.append("request_throughput_rps != success / duration_sec")

    # 7-8: standard aliases consistency
    if not _close(summary.get("output_token_throughput", 0), summary.get("system_output_tps", 0)):
        warnings.append("output_token_throughput != system_output_tps")
    if not _close(summary.get("request_throughput", 0), summary.get("request_throughput_rps", 0)):
        warnings.append("request_throughput != request_throughput_rps")

    # 9: e2el alias consistency
    for p in ["avg", "p50", "p95", "p99"]:
        e2e_key = f"e2e_latency_{p}"
        e2el_key = f"e2el_{p}"
        if e2el_key in summary:
            if not _close(summary.get(e2el_key, 0), summary.get(e2e_key, 0)):
                warnings.append(f"{e2el_key} != {e2e_key}")

    # 10: TTFT should not all equal E2E latency when streaming with success
    if summary.get("stream_mode") and ok_count > 0:
        ttft_vals_ok = [r.get("ttft") for r in summary.get("detail", []) if r.get("ok") and r.get("ttft") is not None]
        e2e_vals_ok = [r.get("e2e_latency") for r in summary.get("detail", []) if r.get("ok")]
        if ttft_vals_ok and e2e_vals_ok and len(ttft_vals_ok) == len(e2e_vals_ok):
            all_equal = all(abs(t - e) < 0.001 for t, e in zip(ttft_vals_ok, e2e_vals_ok))
            if all_equal:
                warnings.append("流式模式下所有 TTFT ≈ E2E Latency — 疑似未正确采集 first token 时间")

    # 11: non-streaming must not fake TTFT/TPOT/ITL
    if not summary.get("stream_mode"):
        if summary.get("ttft_avg", 0) > 0 and summary.get("ttft_avg") != 0:
            warnings.append("非流式模式不应有非零 TTFT 值")

    # 12: per_request_output_tps_avg is per-request, must not equal system throughput
    #     Only warn when concurrency > 1 — at concurrency=1 they are expected to be close.
    pr_tps = summary.get("per_request_output_tps_avg", 0)
    sys_tps = summary.get("system_output_tps", 0)
    concurrency = summary.get("concurrency", 1)
    if concurrency > 1 and ok_count > 1 and pr_tps > 0 and sys_tps > 0:
        if _close(pr_tps, sys_tps):
            warnings.append(f"per_request_output_tps_avg ({pr_tps:.2f}) ≈ system_output_tps ({sys_tps:.2f}) — 单请求均速不应等于系统吞吐（除非并发=1）")

    return warnings


# ── Centralized SSE stream chunk parser ─────────────────────────────────────
# TASK-LLM-BENCHMARK-STREAM-PARSER-ROOT-FIX-002: model-agnostic parser
# Supports: delta.content / delta.reasoning_content / delta.reasoning /
#           choices[].text / tool_calls / function_call / unknown-key diagnostics

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
        if rc:
            chunk.reasoning_text = rc
            # track which field was used for generated_field labeling
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
            # tool text used as fallback generated_text only when no content/reasoning
            chunk.generated_text = chunk.tool_text
            chunk.generated_field = "tool_calls"

    elif not choices:
        # No choices — mark as stream event only if there's a usage chunk
        if chunk.is_usage_chunk:
            chunk.has_stream_event = False  # usage-only chunk is not a stream event

    return chunk


def run_stream_parser_tests() -> list[dict]:
    """Synthetic tests for _parse_stream_chunk (TASK-LLM-BENCHMARK-STREAM-PARSER-ROOT-FIX-002).

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

    # Test 1: role-only empty chunk
    _t("T1_role_only_empty", {"choices": [{"delta": {"role": "assistant", "content": ""}}]}, {
        "has_stream_event": True, "generated_text": None, "answer_text": None, "reasoning_text": None,
    })

    # Test 2: standard answer content (delta.content)
    _t("T2_content", {"choices": [{"delta": {"content": "hello"}}]}, {
        "generated_text": "hello", "generated_field": "content", "answer_text": "hello",
    })

    # Test 3: delta.reasoning_content
    _t("T3_reasoning_content", {"choices": [{"delta": {"reasoning_content": "think"}}]}, {
        "generated_text": "think", "generated_field": "reasoning_content", "reasoning_text": "think",
    })

    # Test 4: delta.reasoning (Qwen3 format)
    _t("T4_reasoning", {"choices": [{"delta": {"reasoning": "Here"}}]}, {
        "generated_text": "Here", "generated_field": "reasoning", "reasoning_text": "Here",
    })

    # Test 5: choices[].text
    _t("T5_choices_text", {"choices": [{"text": "hello"}]}, {
        "generated_text": "hello", "generated_field": "text",
    })

    # Test 6: usage-only chunk
    _t("T6_usage_only", {"choices": [], "usage": {"completion_tokens": 512}}, {
        "is_usage_chunk": True, "generated_text": None, "has_stream_event": False,
    })

    # Test 7: tool/function delta
    obj7 = {"choices": [{"delta": {"tool_calls": [{"function": {"arguments": '{"x":1}'}}]}}]}
    chunk7 = _parse_stream_chunk(obj7)
    passed7 = (chunk7.tool_text is not None and chunk7.answer_text is None)
    results.append({"name": "T7_tool_delta", "passed": passed7,
                    "failures": [] if passed7 else ["tool_text should be set, answer_text should be None"]})

    # Test 8: qwen3.6 synthetic stream sequence
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

    # Test 9: missing generated field — no fake 0.000s
    chunk9 = _parse_stream_chunk({"choices": [{"delta": {"role": "assistant"}}]})
    p9 = (chunk9.generated_text is None and chunk9.answer_text is None and chunk9.reasoning_text is None)
    results.append({"name": "T9_missing_generated_none", "passed": p9,
                    "failures": [] if p9 else ["all content fields must be None when no content present"]})

    # Test 10: real zero gap — 0.0 must display as 0.000s not N/A
    _gap = round(0.06 - 0.06, 6)  # == 0.0 exactly
    p10 = (_gap == 0.0 and _gap is not None)
    results.append({"name": "T10_real_zero_gap", "passed": p10,
                    "failures": [] if p10 else [f"gap={_gap!r} should be 0.0 not None"]})

    # Test 11: unknown delta key diagnostic
    chunk11 = _parse_stream_chunk({"choices": [{"delta": {"thinking": "abc"}}]})
    p11 = ("thinking" in chunk11.unknown_delta_keys and chunk11.generated_text is None)
    results.append({"name": "T11_unknown_delta_key", "passed": p11,
                    "failures": [] if p11 else [
                        f"unknown_delta_keys={chunk11.unknown_delta_keys} generated_text={chunk11.generated_text!r}"]})

    return results


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


def _test_stream_parser_fixtures():
    """Golden fixture tests for _parse_stream_chunk.

    Wraps run_stream_parser_tests() and raises AssertionError on any failure.
    Called directly by CI validation scripts.
    """
    results = run_stream_parser_tests()
    failures = [r for r in results if not r["passed"]]
    if failures:
        details = "; ".join(
            f"{r['name']}: {r['failures']}" for r in failures
        )
        raise AssertionError(f"Stream parser fixture failures ({len(failures)}/{len(results)}): {details}")
    print(f"STREAM_FIXTURE_TESTS_PASS ({len(results)}/{len(results)} passed)")


def call_llm(api_url: str, api_key: str, model: str, messages: list[dict],
             max_tokens: int, temperature: float, timeout: int = 120,
             stream: bool = True, output_length_mode: str = "normal") -> dict:
    """Send one chat-completion request.

    When stream=True: reads SSE chunks, records first-token time,
    inter-chunk timestamps, and computes TTFT / TPOT / ITL accurately.

    When stream=False: returns e2e_latency only; TTFT/TPOT/ITL are None.
    The caller/UI MUST indicate that non-streaming cannot measure true TTFT.

    Returns a dict with keys:
      ok, e2e_latency, latency, ttft, tpot, itl_avg, itl_values,
      prompt_tokens, completion_tokens, total_tokens,
      per_request_output_tps_e2e, per_request_decode_tps,
      finish_reason, stream, [error, error_type on failure]
    """
    body_dict = build_chat_payload(model, messages, max_tokens, temperature,
                                   stream=stream,
                                   output_length_mode=output_length_mode)

    body = json.dumps(body_dict).encode("utf-8")
    req = request.Request(api_url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")

    if DEBUG_MODE:
        logging.debug("POST %s | model=%s max_tokens=%d temp=%.2f stream=%s msg_len=%d",
                       api_url, model, max_tokens, temperature, stream, len(body))

    request_start = time.perf_counter()

    # ── helper: build success result ──
    def _success_result(e2e_latency, ttft, tpot, itl_avg, itl_values,
                        prompt_tokens, completion_tokens, total_tokens,
                        finish_reason, used_stream,
                        visible_ttft=None, visible_tpot=None,
                        debug_fields=None):
        latency = e2e_latency
        per_req_out_tps = completion_tokens / e2e_latency if e2e_latency > 0 and completion_tokens > 0 else 0.0
        per_req_decode_tps = None
        if ttft is not None and completion_tokens >= 2 and (e2e_latency - ttft) > 0:
            per_req_decode_tps = (completion_tokens - 1) / (e2e_latency - ttft)
        result = {
            "ok": True,
            "e2e_latency": round(e2e_latency, 6),
            "latency": round(latency, 6),
            "ttft": round(ttft, 6) if ttft is not None else None,
            "visible_ttft": round(visible_ttft, 6) if visible_ttft is not None else None,
            "tpot": round(tpot, 6) if tpot is not None else None,
            "visible_tpot": round(visible_tpot, 6) if visible_tpot is not None else None,
            "itl_avg": round(itl_avg, 6) if itl_avg is not None else None,
            "itl_values": [round(v, 6) for v in itl_values],
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "per_request_output_tps_e2e": round(per_req_out_tps, 2),
            "per_request_decode_tps": round(per_req_decode_tps, 2) if per_req_decode_tps is not None else None,
            "finish_reason": finish_reason,
            "stream": used_stream,
        }
        if debug_fields:
            result.update(debug_fields)
        return result

    # ── helper: build failure result ──
    def _fail_result(e2e_latency, err_msg, err_type, used_stream):
        return {
            "ok": False,
            "e2e_latency": round(e2e_latency, 6),
            "latency": round(e2e_latency, 6),
            "ttft": None, "tpot": None, "itl_avg": None, "itl_values": [],
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "per_request_output_tps_e2e": 0.0, "per_request_decode_tps": None,
            "finish_reason": "",
            "stream": used_stream,
            "error": err_msg,
            "error_type": err_type,
        }

    try:
        if not stream:
            # ── non-streaming path ──
            resp = request.urlopen(req, timeout=timeout)
            raw = resp.read()
            e2e_latency = time.perf_counter() - request_start
            data = json.loads(raw)
            choice = data.get("choices", [{}])[0]
            usage = data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)
            finish = choice.get("finish_reason", "unknown")
            if DEBUG_MODE:
                logging.debug("OK (non-stream) e2e=%.3fs tokens=%d finish=%s",
                              e2e_latency, total_tokens, finish)
            return _success_result(e2e_latency, None, None, None, [],
                                   prompt_tokens, completion_tokens, total_tokens,
                                   finish, False)

        # ── streaming path ──
        try:
            resp = request.urlopen(req, timeout=timeout)
        except error.HTTPError as e:
            # Try fallback: remove stream_options and retry
            if "stream_options" in body_dict:
                if DEBUG_MODE:
                    logging.debug("stream_options rejected (%s), retrying without", e)
                body_dict.pop("stream_options", None)
                body2 = json.dumps(body_dict).encode("utf-8")
                req2 = request.Request(api_url, data=body2, method="POST")
                req2.add_header("Content-Type", "application/json")
                if api_key:
                    req2.add_header("Authorization", f"Bearer {api_key}")
                request_start = time.perf_counter()  # RESET timer after fallback
                resp = request.urlopen(req2, timeout=timeout)
            else:
                raise

        # ── streaming timing variables ──────────────────────────────────
        first_data_line_time = None
        first_json_chunk_time = None
        # transport / stream-event level
        first_stream_event_time = None
        stream_event_timestamps = []
        # generated content (any field: content / reasoning / text / tool)
        first_generated_token_time = None
        generated_token_timestamps = []
        # answer content (delta.content only)
        first_answer_token_time = None
        answer_token_timestamps = []
        # reasoning content (delta.reasoning_content / delta.reasoning)
        first_reasoning_token_time = None
        reasoning_token_timestamps = []
        # counters / token totals
        completion_tokens = 0
        prompt_tokens = 0
        total_tokens = 0
        finish_reason = "unknown"
        raw_data_lines = 0
        # diagnostics
        _obs_delta_keys: set = set()
        _unk_delta_keys: set = set()
        _gen_field_counts: dict = {}
        _stream_debug: dict = {"first_delta_samples": [], "first_generated_samples": []}

        # Read SSE line by line for accurate per-event timing
        with resp:
            while True:
                line_bytes = resp.readline()
                if not line_bytes:
                    break
                now = time.perf_counter()
                raw_data_lines += 1
                line = line_bytes.decode("utf-8", errors="replace").strip()

                if not line:
                    continue
                if not line.startswith("data:"):
                    continue

                if first_data_line_time is None:
                    first_data_line_time = now

                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break

                try:
                    obj = json.loads(data_str)
                except json.JSONDecodeError:
                    if DEBUG_MODE:
                        logging.warning("SSE JSON decode error: %s", data_str[:100])
                    continue

                if first_json_chunk_time is None:
                    first_json_chunk_time = now

                # ── centralized parser ──
                _chunk = _parse_stream_chunk(obj)

                # transport: stream event (any choices chunk, including role-only)
                if _chunk.has_stream_event:
                    stream_event_timestamps.append(now)
                    if first_stream_event_time is None:
                        first_stream_event_time = now

                # diagnostics
                _obs_delta_keys.update(_chunk.raw_delta_keys)
                _unk_delta_keys.update(_chunk.unknown_delta_keys)
                if len(_stream_debug["first_delta_samples"]) < 5 and _chunk.raw_delta_keys:
                    _stream_debug["first_delta_samples"].append({
                        "keys": list(_chunk.raw_delta_keys[:10]),
                        "generated": (_chunk.generated_text or "")[:200] if _chunk.generated_text else None,
                    })

                # generated content (any non-empty supported field)
                if _chunk.generated_text:
                    generated_token_timestamps.append(now)
                    if _chunk.generated_field:
                        _gen_field_counts[_chunk.generated_field] = (
                            _gen_field_counts.get(_chunk.generated_field, 0) + 1)
                    if first_generated_token_time is None:
                        first_generated_token_time = now
                        if len(_stream_debug["first_generated_samples"]) < 3:
                            _stream_debug["first_generated_samples"].append({
                                "field": _chunk.generated_field,
                                "text": _chunk.generated_text[:200],
                            })

                # answer content (delta.content only)
                if _chunk.answer_text:
                    answer_token_timestamps.append(now)
                    if first_answer_token_time is None:
                        first_answer_token_time = now

                # reasoning content
                if _chunk.reasoning_text:
                    reasoning_token_timestamps.append(now)
                    if first_reasoning_token_time is None:
                        first_reasoning_token_time = now

                # finish reason
                if _chunk.finish_reason:
                    finish_reason = _chunk.finish_reason

                # usage
                if _chunk.is_usage_chunk:
                    usage_chunk = obj.get("usage") or {}
                    prompt_tokens = usage_chunk.get("prompt_tokens", prompt_tokens)
                    completion_tokens = usage_chunk.get("completion_tokens", completion_tokens)
                    total_tokens = usage_chunk.get("total_tokens", prompt_tokens + completion_tokens)

        request_end = time.perf_counter()
        e2e_latency = request_end - request_start

        content_pieces = len(generated_token_timestamps)
        token_source = "usage" if completion_tokens > 0 else "missing_usage"
        if completion_tokens == 0 and DEBUG_MODE:
            logging.debug("stream: no usage in response, token_source=%s content_pieces=%d",
                          token_source, content_pieces)

        # ── transport / benchmark-aligned timing ──
        first_data_line_s    = round(first_data_line_time    - request_start, 6) if first_data_line_time    else None
        first_json_chunk_s   = round(first_json_chunk_time   - request_start, 6) if first_json_chunk_time   else None
        first_stream_event_s = round(first_stream_event_time - request_start, 6) if first_stream_event_time else None

        # ── generated content timing ──
        first_generated_token_s = round(first_generated_token_time - request_start, 6) if first_generated_token_time else None
        first_answer_token_s    = round(first_answer_token_time    - request_start, 6) if first_answer_token_time    else None
        first_reasoning_token_s = round(first_reasoning_token_time - request_start, 6) if first_reasoning_token_time else None

        # ── gaps (first_json_chunk → first generated/answer) ──
        first_generated_gap_s = None
        if first_json_chunk_s is not None and first_generated_token_s is not None:
            first_generated_gap_s = round(first_generated_token_s - first_json_chunk_s, 6)
        first_answer_gap_s = None
        if first_json_chunk_s is not None and first_answer_token_s is not None:
            first_answer_gap_s = round(first_answer_token_s - first_json_chunk_s, 6)

        # ── backward compat aliases ──
        first_non_empty_s   = first_generated_token_s      # was: first chunk with content/reasoning_content
        first_visible_gap_s = first_generated_gap_s        # old name

        # ── TTFT: benchmark-aligned = first JSON chunk ──
        ttft = first_json_chunk_s if first_json_chunk_s is not None else first_data_line_s
        # visible TTFT backward compat = first generated token
        visible_ttft = first_generated_token_s

        # ── TPOT (benchmark-aligned, formula unchanged) ──
        tpot = None
        if ttft is not None and completion_tokens >= 2 and (e2e_latency - ttft) > 0:
            tpot = (e2e_latency - ttft) / max(completion_tokens - 1, 1)

        # ── Visible TPOT (debug only, uses first_generated_token_s) ──
        visible_tpot = None
        if first_generated_token_s is not None and completion_tokens >= 2 and (e2e_latency - first_generated_token_s) > 0:
            visible_tpot = (e2e_latency - first_generated_token_s) / max(completion_tokens - 1, 1)

        # ── ITL: generated_itl is primary; stream_event_itl is debug fallback ──
        generated_itl_values = []
        generated_itl_avg = None
        if len(generated_token_timestamps) >= 2:
            generated_itl_values = [generated_token_timestamps[i] - generated_token_timestamps[i - 1]
                                    for i in range(1, len(generated_token_timestamps))]
            generated_itl_avg = statistics.mean(generated_itl_values)

        answer_itl_values = []
        answer_itl_avg = None
        if len(answer_token_timestamps) >= 2:
            answer_itl_values = [answer_token_timestamps[i] - answer_token_timestamps[i - 1]
                                 for i in range(1, len(answer_token_timestamps))]
            answer_itl_avg = statistics.mean(answer_itl_values)

        reasoning_itl_values = []
        reasoning_itl_avg = None
        if len(reasoning_token_timestamps) >= 2:
            reasoning_itl_values = [reasoning_token_timestamps[i] - reasoning_token_timestamps[i - 1]
                                    for i in range(1, len(reasoning_token_timestamps))]
            reasoning_itl_avg = statistics.mean(reasoning_itl_values)

        stream_event_itl_values = []
        stream_event_itl_avg = None
        if len(stream_event_timestamps) >= 2:
            stream_event_itl_values = [stream_event_timestamps[i] - stream_event_timestamps[i - 1]
                                       for i in range(1, len(stream_event_timestamps))]
            stream_event_itl_avg = statistics.mean(stream_event_itl_values)

        # Primary ITL: generated ITL preferred; stream_event ITL as calibration fallback
        if generated_itl_values:
            itl_values = generated_itl_values
            itl_avg = generated_itl_avg
        elif stream_event_itl_values:
            itl_values = stream_event_itl_values
            itl_avg = stream_event_itl_avg
        else:
            itl_values = []
            itl_avg = None

        # ── diagnostic warnings ──
        _stream_warnings = []
        if completion_tokens > 0 and not generated_token_timestamps:
            _stream_warnings.append(
                "服务端返回了 completion_tokens，但工具没有捕获到任何生成内容字段。"
                f"请检查流式 chunk 字段格式。observed_delta_keys={sorted(_obs_delta_keys)}"
            )
        if first_answer_token_s is None and first_reasoning_token_s is not None:
            _stream_warnings.append(
                "该模型本次流式输出包含 reasoning 字段，但未捕获到回答正文 content。"
                "若前端隐藏 reasoning，用户首字体验应参考 First Answer Token。"
            )
        if _unk_delta_keys:
            _stream_warnings.append(
                f"检测到未识别的流式 delta 字段：{sorted(_unk_delta_keys)}。"
                "已保存 stream_debug 供兼容性分析。"
            )
        if _gen_field_counts and not _gen_field_counts.get("content") and (
                _gen_field_counts.get("reasoning") or _gen_field_counts.get("reasoning_content")):
            _stream_warnings.append(
                "本次输出主要来自 reasoning 字段，最终回答正文 content 可能未流式输出或被服务端隐藏。"
            )

        if DEBUG_MODE:
            logging.debug(
                "OK (stream) e2e=%.3fs ttft=%.3fs gen=%.3fs answer=%.3fs reason=%.3fs tpot=%.3fs"
                " tokens=%d pieces=%d finish=%s",
                e2e_latency, ttft or -1, first_generated_token_s or -1,
                first_answer_token_s or -1, first_reasoning_token_s or -1,
                tpot or -1, total_tokens, content_pieces, finish_reason,
            )
            for _w in _stream_warnings:
                logging.warning("stream_diag: %s", _w)

        debug_fields = {
            # transport timing
            "first_data_line_s":    first_data_line_s,
            "first_json_chunk_s":   first_json_chunk_s,
            "first_stream_event_s": first_stream_event_s,
            # generated content timing
            "first_generated_token":  first_generated_token_s,
            "first_answer_token":     first_answer_token_s,
            "first_reasoning_token":  first_reasoning_token_s,
            "first_generated_gap":    first_generated_gap_s,
            "first_answer_gap":       first_answer_gap_s,
            # backward compat
            "first_non_empty_s":    first_non_empty_s,
            "first_visible_gap_s":  first_visible_gap_s,
            "content_pieces":       content_pieces,
            "raw_data_lines":       raw_data_lines,
            # ITL debug
            "generated_itl_avg":    generated_itl_avg,
            "answer_itl_avg":       answer_itl_avg,
            "reasoning_itl_avg":    reasoning_itl_avg,
            "stream_event_itl_avg": stream_event_itl_avg,
            # diagnostics
            "observed_delta_keys":    sorted(_obs_delta_keys),
            "unknown_delta_keys":     sorted(_unk_delta_keys),
            "generated_field_counts": dict(_gen_field_counts),
            "stream_debug":           _stream_debug,
            "stream_warnings":        _stream_warnings,
        }
        return _success_result(e2e_latency, ttft, tpot, itl_avg, itl_values,
                               prompt_tokens, completion_tokens, total_tokens,
                               finish_reason, True, visible_ttft=visible_ttft,
                               visible_tpot=visible_tpot, debug_fields=debug_fields)

    except Exception as e:
        e2e_latency = time.perf_counter() - request_start
        err_msg = str(e)
        used_stream = stream
        if isinstance(e, error.HTTPError):
            err_type = f"HTTP {e.code}"
        elif isinstance(e, error.URLError):
            err_type = "网络连接失败"
        elif isinstance(e, json.JSONDecodeError):
            err_type = "响应格式错误"
        else:
            err_type = type(e).__name__
        if DEBUG_MODE:
            logging.warning("FAIL e2e=%.3fs type=%s error=%s", e2e_latency, err_type, err_msg)
        return _fail_result(e2e_latency, err_msg, err_type, used_stream)
def normalize_api_url(url: str) -> str:
    """Ensure the URL points to a /chat/completions endpoint.
    Uses urlparse to correctly handle any host:port combination.
    Input:  http://host:8000/v1     → http://host:8000/v1/chat/completions
    Input:  http://host:9099/v1/    → http://host:9099/v1/chat/completions
    Input:  https://api.x.com/v1/chat/completions → unchanged
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    return urlunparse((parsed.scheme, parsed.netloc, path,
                        parsed.params, parsed.query, parsed.fragment))
def check_server_reachable(api_url: str, timeout: int = 5) -> tuple[bool, str]:
    """Quick preflight: try a minimal POST to the API endpoint.
    Returns (True, "") if the server responds (even with an error status).
    Returns (False, error_msg) only on connection-level failures.
    """
    if DEBUG_MODE:
        logging.debug("connectivity check → %s", api_url)
    try:
        req = request.Request(api_url, data=b"{}", method="POST")
        req.add_header("Content-Type", "application/json")
        request.urlopen(req, timeout=timeout)
        if DEBUG_MODE:
            logging.debug("connectivity OK")
        return True, ""
    except error.HTTPError as e:
        # 4xx/5xx responses still mean the server is reachable
        if DEBUG_MODE:
            logging.debug("connectivity OK (server responded HTTP %d)", e.code)
        return True, ""
    except Exception as e:
        if DEBUG_MODE:
            logging.warning("connectivity FAIL: %s", e)
        return False, str(e)
def fetch_models(api_url: str, api_key: str, timeout: int = 10) -> tuple[list[str], str]:
    """Fetch available model list from the API's /v1/models endpoint.
    Returns (model_ids, error_msg). model_ids is empty on failure.
    Strips /chat/completions from api_url to get the base URL."""
    # Derive base URL by removing /chat/completions suffix
    parsed = urlparse(api_url)
    path = parsed.path
    if path.endswith("/chat/completions"):
        path = path[:-len("/chat/completions")]
    models_url = urlunparse((parsed.scheme, parsed.netloc, path + "/models",
                             parsed.params, parsed.query, parsed.fragment))
    if DEBUG_MODE:
        logging.debug("fetch models → %s", models_url)
    try:
        req = request.Request(models_url, method="GET")
        req.add_header("Content-Type", "application/json")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        resp = request.urlopen(req, timeout=timeout)
        data = json.loads(resp.read())
        models = [m.get("id", "") for m in data.get("data", [])]
        models = [m for m in models if m]  # filter empty
        if DEBUG_MODE:
            logging.info("fetched %d models", len(models))
        return models, ""
    except Exception as e:
        if DEBUG_MODE:
            logging.warning("fetch models failed: %s", e)
        return [], str(e)


def build_chat_payload(model: str, messages: list[dict], max_tokens: int,
                       temperature: float, stream: bool = True,
                       output_length_mode: str = "normal") -> dict:
    """Build an OpenAI-compatible chat/completions payload.

    Normal mode preserves existing behavior. Fixed mode is vLLM-bench compatible:
    min_tokens=max_tokens and ignore_eos=true.
    """
    mode = output_length_mode if output_length_mode in ("normal", "fixed") else "normal"
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": stream,
    }
    if mode == "fixed":
        payload["min_tokens"] = max_tokens
        payload["ignore_eos"] = True
    if stream:
        # Some OpenAI-compatible servers require stream_options to get usage.
        payload["stream_options"] = {"include_usage": True}
    return payload


def aggregate_results(results: list[dict], duration: float, config: dict) -> dict:
    """Aggregate benchmark results into a summary dict.

    Args:
        results: list of per-request result dicts from call_llm()
        duration: wall-clock time from first request start to last response end
        config: dict with keys api_url, model, prompt, max_tokens, temperature,
                concurrency, total, stream_mode

    Returns:
        Summary dict with all required metrics (TTFT, TPOT, ITL, E2E, throughput, etc.)
    """
    ok_results = [r for r in results if r["ok"]]
    fail_results = [r for r in results if not r["ok"]]
    num_total = len(results)
    num_ok = len(ok_results)
    num_fail = len(fail_results)

    # ── latencies ──
    e2e_latencies = [r["e2e_latency"] for r in ok_results]
    ttfts = [
        r.get("ttft")
        for r in ok_results
        if isinstance(r.get("ttft"), (int, float)) and r.get("ttft") is not None
    ]
    tpots = [r.get("tpot") for r in ok_results if isinstance(r.get("tpot"), (int, float))]
    itl_values_all = []
    for r in ok_results:
        itl_values_all.extend(r.get("itl_values", []))

    # ── split TTFT: first_stream / first_visible ──
    first_data_lines    = [r.get("first_data_line_s")  for r in ok_results if isinstance(r.get("first_data_line_s"),  (int, float))]
    first_json_chunks   = [r.get("first_json_chunk_s") for r in ok_results if isinstance(r.get("first_json_chunk_s"), (int, float))]
    first_visible_tokens = [r.get("first_non_empty_s") for r in ok_results if isinstance(r.get("first_non_empty_s"), (int, float))]
    visible_ttfts       = [r.get("visible_ttft")       for r in ok_results if isinstance(r.get("visible_ttft"),       (int, float))]
    visible_tpots       = [r.get("visible_tpot")       for r in ok_results if isinstance(r.get("visible_tpot"),       (int, float))]
    first_visible_gaps  = [r.get("first_visible_gap_s") for r in ok_results if isinstance(r.get("first_visible_gap_s"), (int, float))]

    # ── new: generated/answer/reasoning timing aggregates ──
    def _clean(key):
        return [r.get(key) for r in ok_results if isinstance(r.get(key), (int, float))]

    first_generated_tokens   = _clean("first_generated_token")
    first_answer_tokens      = _clean("first_answer_token")
    first_reasoning_tokens   = _clean("first_reasoning_token")
    first_generated_gaps     = _clean("first_generated_gap")
    first_answer_gaps        = _clean("first_answer_gap")
    generated_itl_avgs       = _clean("generated_itl_avg")
    answer_itl_avgs          = _clean("answer_itl_avg")
    reasoning_itl_avgs       = _clean("reasoning_itl_avg")
    stream_event_itl_avgs    = _clean("stream_event_itl_avg")

    # ── tokens ──
    total_input_tokens = sum(r.get("prompt_tokens", 0) for r in ok_results)
    total_output_tokens = sum(r.get("completion_tokens", 0) for r in ok_results)
    total_tokens = total_input_tokens + total_output_tokens

    # ── throughput ──
    dur = max(duration, 0.001)
    request_throughput_rps = num_ok / dur
    system_output_tps = total_output_tokens / dur
    system_total_tps = total_tokens / dur

    # ── per-request output TPS ──
    per_req_tps = [r.get("per_request_output_tps_e2e", 0) for r in ok_results]

    # ── success rate ──
    success_rate = (num_ok / num_total * 100) if num_total > 0 else 0.0

    # ── stream mode flag ──
    stream_mode = any(r.get("stream") for r in ok_results)

    def _p(data, p):
        return round(percentile(data, p), 3)

    summary = {
        "api_url": config["api_url"],
        "model": config["model"],
        "prompt": config["prompt"],
        "max_tokens": config["max_tokens"],
        "output_length_mode": config.get("output_length_mode", "normal"),
        "fixed_output_tokens": config["max_tokens"] if config.get("output_length_mode") == "fixed" else None,
        "min_tokens_sent": config["max_tokens"] if config.get("output_length_mode") == "fixed" else None,
        "ignore_eos": config.get("output_length_mode") == "fixed",
        "temperature": config["temperature"],
        "concurrency": config["concurrency"],
        "total": num_total,
        "success": num_ok,
        "fail": num_fail,
        "success_rate": round(success_rate, 1),
        "duration_sec": round(duration, 2),
        "stream_mode": stream_mode,

        # ── standard metadata ──
        "metric_standard": "JISUMAN LLM Benchmark Standard v1",
        "metric_references": [
            "vLLM bench serve compatible terminology: ttft/tpot/itl/e2el",
            "NVIDIA GenAI-Perf/NIM-style terminology: output token throughput/request throughput/TTFT/ITL",
        ],

        # E2E latency
        "e2e_latency_min": round(min(e2e_latencies), 3) if e2e_latencies else 0,
        "e2e_latency_avg": round(statistics.mean(e2e_latencies), 3) if e2e_latencies else 0,
        "e2e_latency_max": round(max(e2e_latencies), 3) if e2e_latencies else 0,
        "e2e_latency_p50": _p(e2e_latencies, 50),
        "e2e_latency_p95": _p(e2e_latencies, 95),
        "e2e_latency_p99": _p(e2e_latencies, 99),

        # ── standard e2el aliases (vLLM: e2el) ──
        "e2el_min": round(min(e2e_latencies), 3) if e2e_latencies else 0,
        "e2el_avg": round(statistics.mean(e2e_latencies), 3) if e2e_latencies else 0,
        "e2el_max": round(max(e2e_latencies), 3) if e2e_latencies else 0,
        "e2el_p50": _p(e2e_latencies, 50),
        "e2el_p95": _p(e2e_latencies, 95),
        "e2el_p99": _p(e2e_latencies, 99),

        # backward compat: latency_* = e2e_latency_*
        "latency_min": round(min(e2e_latencies), 3) if e2e_latencies else 0,
        "latency_avg": round(statistics.mean(e2e_latencies), 3) if e2e_latencies else 0,
        "latency_max": round(max(e2e_latencies), 3) if e2e_latencies else 0,
        "latency_p50": _p(e2e_latencies, 50),
        "latency_p95": _p(e2e_latencies, 95),
        "latency_p99": _p(e2e_latencies, 99),

        # TTFT
        "ttft_avg": round(statistics.mean(ttfts), 3) if ttfts else 0,
        "ttft_p50": _p(ttfts, 50),
        "ttft_p95": _p(ttfts, 95),
        "ttft_p99": _p(ttfts, 99),

        # TPOT
        "tpot_avg": round(statistics.mean(tpots), 3) if tpots else 0,
        "tpot_p50": _p(tpots, 50),
        "tpot_p95": _p(tpots, 95),
        "tpot_p99": _p(tpots, 99),

        # ITL
        "itl_avg": round(statistics.mean(itl_values_all), 3) if itl_values_all else 0,
        "itl_p50": _p(itl_values_all, 50),
        "itl_p95": _p(itl_values_all, 95),
        "itl_p99": _p(itl_values_all, 99),

        # ── split TTFT: first stream chunk (vLLM-aligned) ──
        "first_data_line_avg":  round(statistics.mean(first_data_lines),  3) if first_data_lines  else 0,
        "first_data_line_p50":  _p(first_data_lines, 50),
        "first_data_line_p95":  _p(first_data_lines, 95),
        "first_data_line_p99":  _p(first_data_lines, 99),

        "first_json_chunk_avg": round(statistics.mean(first_json_chunks), 3) if first_json_chunks else 0,
        "first_json_chunk_p50": _p(first_json_chunks, 50),
        "first_json_chunk_p95": _p(first_json_chunks, 95),
        "first_json_chunk_p99": _p(first_json_chunks, 99),

        # ── split TTFT: first visible token (user-perceived) ──
        "first_visible_token_avg": round(statistics.mean(first_visible_tokens), 3) if first_visible_tokens else None,
        "first_visible_token_p50": _p(first_visible_tokens, 50) if first_visible_tokens else None,
        "first_visible_token_p95": _p(first_visible_tokens, 95) if first_visible_tokens else None,
        "first_visible_token_p99": _p(first_visible_tokens, 99) if first_visible_tokens else None,

        "visible_ttft_avg": round(statistics.mean(visible_ttfts), 3) if visible_ttfts else None,
        "visible_ttft_p50": _p(visible_ttfts, 50) if visible_ttfts else None,
        "visible_ttft_p95": _p(visible_ttfts, 95) if visible_ttfts else None,
        "visible_ttft_p99": _p(visible_ttfts, 99) if visible_ttfts else None,

        # ── first visible gap ──
        "first_visible_gap_avg": round(statistics.mean(first_visible_gaps), 3) if first_visible_gaps else None,
        "first_visible_gap_p50": _p(first_visible_gaps, 50) if first_visible_gaps else None,
        "first_visible_gap_p95": _p(first_visible_gaps, 95) if first_visible_gaps else None,
        "first_visible_gap_p99": _p(first_visible_gaps, 99) if first_visible_gaps else None,

        # ── visible TPOT (debug only) ──
        "visible_tpot_avg": round(statistics.mean(visible_tpots), 3) if visible_tpots else None,
        "visible_tpot_p50": _p(visible_tpots, 50) if visible_tpots else None,
        "visible_tpot_p95": _p(visible_tpots, 95) if visible_tpots else None,
        "visible_tpot_p99": _p(visible_tpots, 99) if visible_tpots else None,

        # ── new: generated / answer / reasoning timing ──
        # None when no samples (never faked as 0)
        "first_generated_token_avg": round(statistics.mean(first_generated_tokens), 3) if first_generated_tokens else None,
        "first_generated_token_p50": _p(first_generated_tokens, 50) if first_generated_tokens else None,
        "first_generated_token_p95": _p(first_generated_tokens, 95) if first_generated_tokens else None,
        "first_generated_token_p99": _p(first_generated_tokens, 99) if first_generated_tokens else None,

        "first_answer_token_avg": round(statistics.mean(first_answer_tokens), 3) if first_answer_tokens else None,
        "first_answer_token_p50": _p(first_answer_tokens, 50) if first_answer_tokens else None,
        "first_answer_token_p95": _p(first_answer_tokens, 95) if first_answer_tokens else None,
        "first_answer_token_p99": _p(first_answer_tokens, 99) if first_answer_tokens else None,

        "first_reasoning_token_avg": round(statistics.mean(first_reasoning_tokens), 3) if first_reasoning_tokens else None,
        "first_reasoning_token_p50": _p(first_reasoning_tokens, 50) if first_reasoning_tokens else None,
        "first_reasoning_token_p95": _p(first_reasoning_tokens, 95) if first_reasoning_tokens else None,
        "first_reasoning_token_p99": _p(first_reasoning_tokens, 99) if first_reasoning_tokens else None,

        "first_generated_gap_avg": round(statistics.mean(first_generated_gaps), 3) if first_generated_gaps else None,
        "first_generated_gap_p50": _p(first_generated_gaps, 50) if first_generated_gaps else None,
        "first_generated_gap_p95": _p(first_generated_gaps, 95) if first_generated_gaps else None,
        "first_generated_gap_p99": _p(first_generated_gaps, 99) if first_generated_gaps else None,

        "first_answer_gap_avg": round(statistics.mean(first_answer_gaps), 3) if first_answer_gaps else None,
        "first_answer_gap_p50": _p(first_answer_gaps, 50) if first_answer_gaps else None,
        "first_answer_gap_p95": _p(first_answer_gaps, 95) if first_answer_gaps else None,
        "first_answer_gap_p99": _p(first_answer_gaps, 99) if first_answer_gaps else None,

        "generated_itl_avg_agg": round(statistics.mean(generated_itl_avgs), 3) if generated_itl_avgs else None,
        "answer_itl_avg_agg":    round(statistics.mean(answer_itl_avgs),    3) if answer_itl_avgs    else None,
        "reasoning_itl_avg_agg": round(statistics.mean(reasoning_itl_avgs), 3) if reasoning_itl_avgs else None,
        "stream_event_itl_avg_agg": round(statistics.mean(stream_event_itl_avgs), 3) if stream_event_itl_avgs else None,

        # tokens
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_tokens": total_tokens,

        # throughput
        "request_throughput_rps": round(request_throughput_rps, 2),
        "system_output_tps": round(system_output_tps, 1),
        "system_total_tps": round(system_total_tps, 1),

        # ── standard throughput aliases (NVIDIA: output_token_throughput / request_throughput) ──
        "output_token_throughput": round(system_output_tps, 1),
        "request_throughput": round(request_throughput_rps, 2),

        # per-request TPS (backward compat: tokens_per_sec)
        "per_request_output_tps_avg": round(statistics.mean(per_req_tps), 2) if per_req_tps else 0,
        "per_request_output_tps_p50": _p(per_req_tps, 50),
        "per_request_output_tps_p95": _p(per_req_tps, 95),
        "tokens_per_sec": round(statistics.mean(per_req_tps), 2) if per_req_tps else 0,

        # detail
        "detail": ok_results[:200],
        "fail_detail": fail_results[:50],
    }

    # ── run metric consistency validation ──
    warnings = validate_metric_consistency(summary)
    if summary.get("output_length_mode") == "fixed":
        avg_completion = total_output_tokens / num_ok if num_ok else 0
        target_output_tokens = config["max_tokens"]
        summary["fixed_output_avg_completion_tokens"] = round(avg_completion, 2)
        summary["fixed_output_validation_passed"] = avg_completion >= target_output_tokens * 0.9
        if not summary["fixed_output_validation_passed"]:
            summary["fixed_output_validation_warning"] = (
                "Fixed output mode is enabled, but actual completion tokens are far below the target. "
                "The server may not support min_tokens / ignore_eos, or context/stop constraints may apply.")
            warnings.append(summary["fixed_output_validation_warning"])
    else:
        summary["fixed_output_avg_completion_tokens"] = None
        summary["fixed_output_validation_passed"] = None
        summary["fixed_output_validation_warning"] = ""
    summary["metric_warnings"] = warnings

    # ── parser_profile: stream capability profile from per-request results ──
    summary["parser_profile"] = _build_parser_profile(ok_results, config)

    return summary


def run_benchmark(api_url: str, api_key: str, model: str, messages: list[dict],
                  max_tokens: int, temperature: float,
                  concurrency: int, num_requests: int,
                  progress_cb, done_cb,
                  stream: bool = True,
                  preset_name: str = "",
                  output_length_mode: str = "normal") -> None:
    """Run benchmark in a background thread; call progress_cb(completed, total, fail)
    and done_cb(summary_dict) on the main thread."""
    if DEBUG_MODE:
        logging.info("benchmark start: concurrency=%d total=%d stream=%s preset=%s",
                     concurrency, num_requests, stream, preset_name)
    results = []
    lock = threading.Lock()
    completed = [0]  # boxed for mutation in closure
    failed = [0]     # boxed for mutation in closure

    def worker():
        r = call_llm(api_url, api_key, model, messages, max_tokens, temperature,
                     stream=stream, output_length_mode=output_length_mode)
        with lock:
            results.append(r)
            completed[0] += 1
            if not r["ok"]:
                failed[0] += 1
            progress_cb(completed[0], num_requests, failed[0])
        return r

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(worker) for _ in range(num_requests)]
        for f in as_completed(futures):
            pass  # results collected in worker via closure
    duration = time.perf_counter() - t0

    if DEBUG_MODE:
        ok_count = sum(1 for r in results if r["ok"])
        fail_count = len(results) - ok_count
        logging.info("benchmark done: ok=%d fail=%d duration=%.2fs", ok_count, fail_count, duration)

    config = {
        "api_url": api_url,
        "model": model,
        "prompt": messages[-1]["content"][:100] if messages else "",
        "max_tokens": max_tokens,
        "temperature": temperature,
        "concurrency": concurrency,
        "total": num_requests,
        "stream_mode": stream,
        "benchmark_preset_name": preset_name,
        "benchmark_preset_type": "fixed_concurrency",
        "output_length_mode": output_length_mode,
    }
    summary = aggregate_results(results, duration, config)
    # Also add preset info to output
    summary["benchmark_preset_name"] = preset_name
    summary["benchmark_preset_type"] = "fixed_concurrency"
    done_cb(summary)
# ============================================================
# GUI
# ============================================================
class LLMBenchmarkApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("JISUMAN LLM Benchmark GUI")
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = int(sw * 1099 / 1920)
        h = int(sh * 1018 / 1080)
        self.root.geometry(f"{w}x{h}")
        self.root.resizable(False, False)
        self.root.minsize(1024, 680)
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
        self.lang_code = self._load_language_config()
        self.language_var = tk.StringVar(
            value=I18N[self.lang_code]["language.en"]
            if self.lang_code == "en_US" else I18N["zh_CN"]["language.zh"])
        self._i18n_widgets = []
        self._i18n_callbacks = []
        init_db()
        self._setup_styles()
        self._build_header()
        self._build_body()
        self._build_statusbar()
        self._load_config()
        self._refresh_ui_language()

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
            self.nb.tab(self.settings_frame, text=f"  {self.tr('tab.settings')}  ")
            self.nb.tab(self.bench_frame, text=f"  {self.tr('tab.benchmark')}  ")
            self.nb.tab(self.sweep_frame, text=f"  {self.tr('tab.sweep')}  ")
            self.nb.tab(self.history_frame, text=f"  {self.tr('tab.history')}  ")
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

    def _load_header_logo(self, path="/home/jisuman/logo.png", max_height=28, max_width=160):
        paths = [
            path,
            "/opt/llm-benchmark/logo.png",
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
        st = ttk.Style()
        st.theme_use("clam")
        st.configure(".", font=C_STYLE["font_body"],
                     background=C_STYLE["bg_main"], foreground=C_STYLE["text_primary"])
        st.configure("Card.TFrame", background=C_STYLE["bg_card"])
        st.configure("Header.TFrame", background=C_STYLE["bg_header"])
        st.configure("StatusBar.TFrame", background=C_STYLE["bg_header"])
        st.configure("Title.TLabel", font=C_STYLE["font_title"],
                     background=C_STYLE["bg_header"], foreground=C_STYLE["text_primary"])
        st.configure("Subtitle.TLabel", font=C_STYLE["font_subtitle"],
                     background=C_STYLE["bg_header"], foreground=C_STYLE["text_secondary"])
        st.configure("Section.TLabel", font=C_STYLE["font_section"],
                     background=C_STYLE["bg_card"], foreground=C_STYLE["text_primary"])
        st.configure("Body.TLabel", font=C_STYLE["font_body"],
                     background=C_STYLE["bg_card"], foreground=C_STYLE["text_primary"])
        st.configure("Small.TLabel", font=C_STYLE["font_small"],
                     background=C_STYLE["bg_card"], foreground=C_STYLE["text_secondary"])
        st.configure("Metric.TLabel", font=C_STYLE["font_metric"],
                     background=C_STYLE["bg_card"], foreground=C_STYLE["text_primary"])
        st.configure("MetricSmall.TLabel", font=C_STYLE["font_status"],
                     background=C_STYLE["bg_card"], foreground=C_STYLE["text_primary"])
        st.configure("StatusBar.TLabel", font=C_STYLE["font_small"],
                     background=C_STYLE["bg_header"], foreground=C_STYLE["text_secondary"])
        st.configure("Primary.TButton", font=C_STYLE["font_label"],
                     background=C_STYLE["accent"], foreground="white",
                     borderwidth=0, padding=(20, C_STYLE["pad_sm"]))
        st.map("Primary.TButton",
               background=[("disabled", "#B8B0F9"), ("active", C_STYLE["accent_hover"])])
        st.configure("Secondary.TButton", font=C_STYLE["font_label"],
                     background=C_STYLE["bg_card"], foreground=C_STYLE["text_primary"],
                     borderwidth=1, padding=(16, C_STYLE["pad_sm"]))
        st.map("Secondary.TButton",
               background=[("disabled", "#F1F5F9")])
        st.configure("App.TEntry", fieldbackground=C_STYLE["bg_input"],
                     borderwidth=1, padding=10, font=C_STYLE["font_body"])
        st.map("App.TEntry",
               fieldbackground=[("disabled", "#F1F5F9"), ("focus", C_STYLE["accent_light"])])
        st.configure("App.Treeview", rowheight=40, font=C_STYLE["font_body"],
                     background=C_STYLE["bg_card"], fieldbackground=C_STYLE["bg_card"],
                     foreground=C_STYLE["text_primary"])
        st.configure("App.Treeview.Heading", font=C_STYLE["font_label"],
                     background=C_STYLE["bg_main"], foreground=C_STYLE["text_primary"],
                     padding=(C_STYLE["pad_sm"], C_STYLE["pad_sm"]))
        st.map("App.Treeview",
               background=[("selected", C_STYLE["accent"])],
               foreground=[("selected", "white")])
        # vibrant progress bar
        st.configure("Accent.Horizontal.TProgressbar",
                     troughcolor=C_STYLE["border_light"],
                     background=C_STYLE["accent"],
                     bordercolor=C_STYLE["border"],
                     lightcolor=C_STYLE["accent"],
                     darkcolor=C_STYLE["accent_hover"])
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
        self.logo_image = self._load_header_logo("/home/jisuman/logo.png",
                                                 max_height=28, max_width=160)
        if self.logo_image is not None:
            icon_lbl = tk.Label(title_row, image=self.logo_image,
                                bg=C_STYLE["bg_header"])
            icon_lbl.pack(side=tk.LEFT, padx=(0, 10))
            self._icon_lbl = None
        else:
            icon_lbl = tk.Label(title_row, text="⚡", font=(FONT_FAMILY, 16),
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
        self.nb = ttk.Notebook(body)
        self.nb.grid(row=0, column=0, sticky="nsew")
        self.settings_frame = tk.Frame(self.nb, bg=C_STYLE["bg_main"])
        self.bench_frame = tk.Frame(self.nb, bg=C_STYLE["bg_main"])
        self.sweep_frame = tk.Frame(self.nb, bg=C_STYLE["bg_main"])
        self.history_frame = tk.Frame(self.nb, bg=C_STYLE["bg_main"])
        self.nb.add(self.settings_frame, text="  参数设置  ")
        self.nb.add(self.bench_frame, text="  基准测试  ")
        self.nb.add(self.sweep_frame, text="  并发扫测  ")
        self.nb.add(self.history_frame, text="  历史记录  ")
        self._build_settings_tab()
        self._build_results_tab()
        self._build_sweep_tab()
        self._build_history_tab()
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
        self.prompt_var = tk.StringVar(value="请用300字左右介绍机器学习。")
        self._labeled_text(card_a.content, "用户提示词", self.prompt_var, 4, height=2)
        card_b = SectionCard(col, "测试参数", collapsible=True, expanded=True)
        card_b.pack(fill=tk.X, pady=(0, C_STYLE["gap_lg"]))
        self._build_test_params(card_b.content)


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
        ttk.Spinbox(gen, from_=16, to=8192, increment=16,
                    textvariable=self.max_tokens_var, width=SPIN_W).grid(
            row=0, column=1, sticky="w", pady=(C_STYLE["gap_sm"], 0))

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
        # Row 0: 4 cards
        row0 = tk.Frame(metric_grid, bg=C_STYLE["bg_card"])
        row0.pack(fill=tk.X, pady=(0, C_STYLE["gap_md"]))
        for i, (key, label) in enumerate([
            ("ttft", "首包延迟 TTFT"), ("visible_ttft", "首字延迟 FVT"),
            ("e2e_p95", "E2E P95"), ("system_output_tps", "输出吞吐 TPS"),
        ]):
            mi = MetricItem(row0, label, metric_key=key)
            mi.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                    padx=(0 if i == 0 else C_STYLE["gap_md"], 0))
            self.metrics[key] = mi
        # Row 1: 4 cards
        row1 = tk.Frame(metric_grid, bg=C_STYLE["bg_card"])
        row1.pack(fill=tk.X)
        for i, (key, label) in enumerate([
            ("rps", "请求吞吐 RPS"), ("tpot", "单 Token 耗时 TPOT"),
            ("itl", "Token 间隔 ITL"), ("success_rate", "成功率"),
        ]):
            mi = MetricItem(row1, label, metric_key=key)
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

        def _done_with_warmup(s):
            s["warmup_requests"] = warmup
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
        report = self._generate_report(summary)
        fail_detail = summary.get("fail_detail", [])
        if fail_detail:
            error_summary, advice = self._analyze_failures(fail_detail)
            report += "\n\n  ═══════════ 失败请求分析 ═══════════\n\n"
            report += error_summary + "\n\n"
            report += advice
        self.result_text.config(state=tk.NORMAL)
        self.result_text.delete("1.0", tk.END)
        self.result_text.insert(tk.END, report)
        self.result_text.config(state=tk.DISABLED)

        # Auto-expand report to show results
        if self._report_collapsed:
            self._report_card.content.grid()
            self._report_card.title_lbl.config(text="▼ 详细报告")
            self._report_collapsed = False
        # save report to file if enabled
        if self.save_report_var.get() == "是":
            try:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                report_path = os.path.join(_SCRIPT_DIR, f"llm_benchmark_report_{ts}.txt")
                with open(report_path, "w", encoding="utf-8") as f:
                    f.write(report)
                if DEBUG_MODE:
                    logging.info("report saved to %s", report_path)
            except Exception as e:
                if DEBUG_MODE:
                    logging.warning("failed to save report: %s", e)
        self._draw_histogram(summary)
        self._refresh_history()
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

        r.append("")
        r.append("=" * 60)
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
                                   values=["All", "Single", "Sweep"],
                                   width=10, state="readonly")
        self.history_type_filter = type_filter
        type_filter.pack(side=tk.LEFT)
        type_filter.bind("<<ComboboxSelected>>", lambda e: self._refresh_history())
        ttk.Button(toolbar_inner, text="✕ 清空记录", style="Secondary.TButton",
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
        cols = ("id", "Time", "Type", "Model", "Config", "Key Result", "Status")
        self.hist_tree = ttk.Treeview(table_card, columns=cols,
                                      show="headings", selectmode="browse",
                                      style="App.Treeview")
        for c in cols:
            self.hist_tree.heading(c, text=c)
            self.hist_tree.column(c, width=80, anchor="center")
        self.hist_tree.column("id", width=40)
        self.hist_tree.column("Time", width=140)
        self.hist_tree.column("Type", width=80)
        self.hist_tree.column("Model", width=120)
        self.hist_tree.column("Config", width=220)
        self.hist_tree.column("Key Result", width=300)
        self.hist_tree.column("Status", width=100)
        scrollbar = ttk.Scrollbar(table_card, orient=tk.VERTICAL,
                                  command=self.hist_tree.yview)
        self.hist_tree.configure(yscrollcommand=scrollbar.set)
        self.hist_tree.grid(row=0, column=0, sticky="nsew",
                            padx=(C_STYLE["pad_lg"], 0),
                            pady=C_STYLE["pad_lg"])
        scrollbar.grid(row=0, column=1, sticky="ns",
                       padx=(0, C_STYLE["pad_lg"]),
                       pady=C_STYLE["pad_lg"])
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
        # Row 0: Concurrency levels
        tk.Label(cfg, text="并发级别", font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"]).grid(
            row=0, column=0, sticky="w", padx=(0, C_STYLE["pad_sm"]), pady=(0, C_STYLE["gap_sm"]))
        self.sweep_conc_var = tk.StringVar(value="1,5,10,20,40")
        ttk.Entry(cfg, textvariable=self.sweep_conc_var, width=40).grid(
            row=0, column=1, sticky="ew", pady=(0, C_STYLE["gap_sm"]))
        tk.Label(cfg, text="例如: 1,5,10,20,40", font=C_STYLE["font_small"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_muted"]).grid(
            row=0, column=2, sticky="w", padx=(C_STYLE["pad_sm"], 0), pady=(0, C_STYLE["gap_sm"]))

        # Row 1: Requests multiplier
        tk.Label(cfg, text="请求倍数", font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"]).grid(
            row=1, column=0, sticky="w", padx=(0, C_STYLE["pad_sm"]), pady=(0, C_STYLE["gap_sm"]))
        self.sweep_mult_var = tk.IntVar(value=10)
        ttk.Spinbox(cfg, from_=1, to=100, increment=1,
                    textvariable=self.sweep_mult_var, width=8).grid(
            row=1, column=1, sticky="w", pady=(0, C_STYLE["gap_sm"]))
        tk.Label(cfg, text="每个并发级别: 请求数 = 并发数 × 倍数",
                 font=C_STYLE["font_small"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_muted"]).grid(
            row=1, column=2, sticky="w", padx=(C_STYLE["pad_sm"], 0), pady=(0, C_STYLE["gap_sm"]))

        # Row 2: Resource mode (always disabled for MVP)
        tk.Label(cfg, text="资源监测", font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"]).grid(
            row=2, column=0, sticky="w", padx=(0, C_STYLE["pad_sm"]), pady=(0, C_STYLE["gap_sm"]))
        self.sweep_resource_var = tk.StringVar(value="disabled")
        res_combo = ttk.Combobox(cfg, textvariable=self.sweep_resource_var,
                                 values=["disabled"], width=12, state="readonly")
        res_combo.grid(row=2, column=1, sticky="w", pady=(0, C_STYLE["gap_sm"]))
        res_combo.current(0)
        tk.Label(cfg, text="MVP 阶段资源监测暂不可用",
                 font=C_STYLE["font_small"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_muted"]).grid(
            row=2, column=2, sticky="w", padx=(C_STYLE["pad_sm"], 0), pady=(0, C_STYLE["gap_sm"]))

        # Row 3: Save options
        save_lbl = tk.Label(cfg, text="保存选项", font=C_STYLE["font_body"],
                            bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"])
        save_lbl.grid(row=3, column=0, sticky="w",
                      padx=(0, C_STYLE["pad_sm"]), pady=(C_STYLE["gap_sm"], 0))
        save_row = tk.Frame(cfg, bg=C_STYLE["bg_card"])
        save_row.grid(row=3, column=1, columnspan=2, sticky="ew",
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

        # Row 4: Start button
        btn_row = tk.Frame(cfg, bg=C_STYLE["bg_card"])
        btn_row.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(C_STYLE["gap_sm"], 0))
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
    def _parse_concurrency_levels(self, text: str) -> list[int]:
        """Parse comma-separated concurrency levels. Returns sorted unique ints.
        Raises ValueError for invalid input."""
        parts = [p.strip() for p in text.split(",") if p.strip()]
        if not parts:
            raise ValueError("并发级别不能为空")
        levels = []
        for p in parts:
            try:
                v = int(p)
            except ValueError:
                raise ValueError(f"无效的并发值: '{p}'，请输入逗号分隔的整数，例如: 1,5,10")
            if v < 1:
                raise ValueError(f"并发数必须大于 0，收到: {v}")
            if v > 512:
                raise ValueError(f"并发数不能超过 512，收到: {v}")
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

        multiplier = self.sweep_mult_var.get()
        if multiplier < 1:
            messagebox.showerror(self.tr("msg.input_error"), self.tr("msg.multiplier_positive"))
            return

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

        # Update UI
        if not self._begin_run("sweep"):
            self._show_run_busy("sweep")
            return
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

        t = threading.Thread(
            target=self._run_sweep_thread,
            args=(api_url, api_key, model, messages, max_tokens, temperature,
                  concurrency_levels, multiplier, stream, warmup,
                  output_length_mode),
            daemon=True,
        )
        t.start()

    def _run_sweep_thread(self, api_url, api_key, model, messages,
                          max_tokens, temperature,
                          concurrency_levels, multiplier, stream, warmup,
                          output_length_mode="normal"):
        try:
            self._run_sweep(api_url, api_key, model, messages, max_tokens,
                            temperature, concurrency_levels, multiplier, stream,
                            warmup, output_length_mode)
        except Exception as e:
            self._append_sweep_status(f"\n✕ 扫测异常: {e}\n")
            self.root.after(0, lambda err=str(e): self._sweep_done(None, err))
        finally:
            self.root.after(0, lambda: self._set_sweep_running_state(False))
            self.root.after(0, lambda: self._end_run("sweep"))

    def _run_sweep(self, api_url, api_key, model, messages,
                   max_tokens, temperature,
                   concurrency_levels, multiplier, stream, warmup,
                   output_length_mode="normal"):
        """Run a concurrency sweep in the current (background) thread."""
        sweep_id = datetime.now().strftime("sweep_%Y%m%d_%H%M%S")
        started_at = datetime.now().isoformat()

        self._append_sweep_status(f"扫测开始 — {sweep_id}\n")
        self._append_sweep_status(f"API: {api_url}\n")
        self._append_sweep_status(f"Model: {model}\n")
        self._append_sweep_status(f"并发级别: {concurrency_levels}\n")
        self._append_sweep_status(f"请求倍数: {multiplier}\n\n")

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
            num_requests = c * multiplier
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
                }
                cases.append(case)
                sweep_fail_count += summary.get("fail", 0) or 0
                self.root.after(0, lambda i=idx + 1, total=total_levels, cc=c, fail=sweep_fail_count:
                                self._update_status_animation(
                                    completed=i, total=total, fail=fail,
                                    phase="sweep", current_label=f"C={cc}"))
                self._append_sweep_status(
                    f"✓ success={summary['success']} fail={summary['fail']} "
                    f"E2E_avg={summary['e2e_latency_avg']:.3f}s "
                    f"Output_TPS={summary['system_output_tps']:.1f} tok/s\n")
            else:
                sweep_fail_count += 1
                self.root.after(0, lambda i=idx + 1, total=total_levels, cc=c, fail=sweep_fail_count:
                                self._update_status_animation(
                                    completed=i, total=total, fail=fail,
                                    phase="sweep", current_label=f"C={cc}"))
                self._append_sweep_status(f"✕ 未返回结果\n")

        finished_at = datetime.now().isoformat()
        analysis_summary = self._generate_analysis_summary(cases)
        sweep_diagnostics = {
            "detail_truncation_notes": self._detect_detail_truncation_notes(cases),
            "non_monotonic_anomalies": self._detect_non_monotonic_sweep_anomalies(cases),
            "recommendation": self._recommend_sweep_concurrency_range(cases),
        }
        sweep_result = {
            "sweep_id": sweep_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "api_url": api_url,
            "model": model,
            "concurrency_levels": concurrency_levels,
            "requests_multiplier": multiplier,
            "output_length_mode": output_length_mode,
            "fixed_output_tokens": max_tokens if output_length_mode == "fixed" else None,
            "min_tokens_sent": max_tokens if output_length_mode == "fixed" else None,
            "ignore_eos": output_length_mode == "fixed",
            "cases": cases,
            "analysis_summary": analysis_summary,
            "sweep_diagnostics": sweep_diagnostics,
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

    def _export_sweep_json(self, sweep_result: dict) -> str:
        """Export sweep result to JSON file. Returns the file path."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = os.path.join(_SCRIPT_DIR, f"sweep_result_{ts}.json")
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
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        md_path = os.path.join(_SCRIPT_DIR, f"sweep_analysis_{ts}.md")
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

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        png_path = os.path.join(_SCRIPT_DIR, f"sweep_report_{ts}.png")
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

    def _refresh_history(self):
        for item in self.hist_tree.get_children():
            self.hist_tree.delete(item)
        rows = load_history()
        selected_filter = getattr(self, "history_type_filter_var", tk.StringVar(value="All")).get()
        visible_count = 0
        for row in rows:
            rid = row["id"]
            created = row["created_at"]
            record_type = row["record_type"] or "single"
            if selected_filter == "Single" and record_type != "single":
                continue
            if selected_filter == "Sweep" and record_type != "sweep":
                continue
            model = row["model"]
            conc = row["concurrency"]
            total = row["total"]
            success_rate = row["success_rate"] or 0.0
            e2e_p95 = row["e2e_latency_p95"] or 0.0
            sys_tps = row["system_output_tps"] or 0.0
            status = row["status"] or "completed"
            if record_type == "sweep":
                type_label = self.tr("history.sweep")
                config_summary = row["config_summary"] or "-"
                primary_metric = row["primary_metric"] or "-"
            else:
                type_label = self.tr("history.single")
                config_summary = row["config_summary"] or (
                    f"C{conc} / N{total} / max_tokens=-")
                primary_metric = row["primary_metric"] or (
                    f"Output TPS {sys_tps:.1f} | E2E P95 {e2e_p95:.3f}s | "
                    f"Success {success_rate:.0f}%")
            self.hist_tree.insert("", tk.END, values=(
                rid, created, type_label, model, config_summary,
                primary_metric, status,
            ))
            visible_count += 1
        if visible_count:
            self.history_status_var.set(self.tr("status.history_count").format(count=visible_count))
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
        rid = self.hist_tree.item(sel[0], "values")[0]
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

        title = self.tr("section.e2e_distribution")
        canvas.create_text(w / 2, 16, text=title,
                           font=C_STYLE["font_section"],
                           fill=C_STYLE["text_primary"])

        margin_l, margin_r, margin_t, margin_b = 58, 26, 48, 48
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
            if ratio < 0.25:
                r_, g_, b_ = 200, 195, 253
            elif ratio < 0.5:
                r_, g_, b_ = 160, 148, 252
            elif ratio < 0.75:
                r_, g_, b_ = 120, 100, 250
            else:
                r_, g_, b_ = 83, 58, 253
            color = f"#{r_:02x}{g_:02x}{b_:02x}"
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
        canvas.create_text(margin_l, 32, text=stat_text, anchor="w",
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
        rid = self.hist_tree.item(sel[0], "values")[0]
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM benchmarks WHERE id=?",
                           (rid,)).fetchone()
        conn.close()
        if not row:
            return
        if (row["record_type"] or "single") == "sweep":
            self._show_sweep_history_detail(rid, row)
            return
        detail = json.loads(row["detail_json"]) if row["detail_json"] else []
        # backward compat: old records use "latency", new records use "e2e_latency"
        latencies = [
            r.get("e2e_latency", r.get("latency", 0))
            for r in detail if r.get("ok")
        ] if detail else []
        fail_detail = [r for r in detail if not r.get("ok")]
        # load warnings from DB
        metric_warnings = []
        try:
            metric_warnings = json.loads(row["metric_warnings_json"] or "[]")
        except Exception:
            pass

        top = tk.Toplevel(self.root)
        top.title(f"测试详情  #{rid}")
        top.geometry("800x620")
        top.configure(bg=C_STYLE["bg_main"])
        top.minsize(600, 450)
        top.grid_columnconfigure(0, weight=1)
        top.grid_rowconfigure(0, weight=0)  # summary card
        top.grid_rowconfigure(1, weight=1)  # histogram
        top.grid_rowconfigure(2, weight=0)  # close button

        # ── summary card ──
        card = tk.Frame(top, bg=C_STYLE["bg_card"],
                        highlightbackground=C_STYLE["border"],
                        highlightthickness=1, bd=0)
        card.grid(row=0, column=0, sticky="ew",
                  padx=C_STYLE["pad_lg"], pady=(C_STYLE["pad_lg"], C_STYLE["gap_md"]))
        card_inner = tk.Frame(card, bg=C_STYLE["bg_card"])
        card_inner.pack(fill=tk.X, padx=C_STYLE["pad_md"], pady=C_STYLE["pad_md"])
        # row 1: model + concurrency + duration
        r1 = tk.Frame(card_inner, bg=C_STYLE["bg_card"])
        r1.pack(fill=tk.X)
        tk.Label(r1, text=f"模型: {row['model']}", font=C_STYLE["font_section"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"]).pack(side=tk.LEFT)
        tk.Label(r1, text=f"  并发: {row['concurrency']}", font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_secondary"]).pack(
            side=tk.LEFT, padx=(C_STYLE["pad_md"], 0))
        tk.Label(r1, text=f"请求: {row['total']}", font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_secondary"]).pack(
            side=tk.LEFT, padx=(C_STYLE["pad_md"], 0))
        succ = row["success"] or 0
        fail = row["fail"] or 0
        total = row["total"] or 1
        succ_color = C_STYLE["success"] if succ == total else \
                     C_STYLE["warning"] if succ > 0 else C_STYLE["error"]
        tk.Label(r1, text=f"成功: {succ}", font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=succ_color).pack(
            side=tk.LEFT, padx=(C_STYLE["pad_md"], 0))
        if fail > 0:
            tk.Label(r1, text=f"失败: {fail}", font=C_STYLE["font_body"],
                     bg=C_STYLE["bg_card"], fg=C_STYLE["error"]).pack(
                side=tk.LEFT, padx=(C_STYLE["pad_sm"], 0))
        dur = row["duration_sec"]
        tk.Label(r1, text=f"耗时: {dur:.1f}s" if dur else "耗时: —",
                 font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_secondary"]).pack(
            side=tk.RIGHT)
        # row 2: latency + TTFT/TPOT metrics
        r2 = tk.Frame(card_inner, bg=C_STYLE["bg_card"])
        r2.pack(fill=tk.X, pady=(C_STYLE["gap_sm"], 0))
        metrics_text = (
            f"E2E avg: {row['e2e_latency_avg']:.3f}s" if row["e2e_latency_avg"] else "E2E avg: —"
        ) + "    " + (
            f"E2E P95: {row['e2e_latency_p95']:.3f}s" if row["e2e_latency_p95"] else "E2E P95: —"
        ) + "    " + (
            f"TTFT: {row['ttft_avg']:.3f}s" if row["ttft_avg"] else "TTFT: —"
        )
        tk.Label(r2, text=metrics_text, font=C_STYLE["font_small"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"]).pack(side=tk.LEFT)
        pct_text = (
            f"Output TPS: {row['system_output_tps']:.1f}" if row["system_output_tps"] else "Output TPS: —"
        ) + "    " + (
            f"Req/s: {row['request_throughput_rps']:.2f}" if row["request_throughput_rps"] else "Req/s: —"
        ) + "    " + (
            f"Success: {row['success_rate']:.0f}%" if row["success_rate"] else "Success: —"
        )
        tk.Label(r2, text=pct_text, font=C_STYLE["font_small"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_secondary"]).pack(
            side=tk.LEFT, padx=(C_STYLE["gap_lg"], 0))

        # row 3: metric standard + warnings
        if row["metric_standard"] or metric_warnings:
            r3 = tk.Frame(card_inner, bg=C_STYLE["bg_card"])
            r3.pack(fill=tk.X, pady=(C_STYLE["gap_sm"], 0))
            std_label = f"标准: {row['metric_standard']}" if row["metric_standard"] else "标准: —"
            warn_label = f"  |  警告: {len(metric_warnings)} 条" if metric_warnings else ""
            tk.Label(r3, text=std_label + warn_label, font=C_STYLE["font_small"],
                     bg=C_STYLE["bg_card"], fg=C_STYLE["text_muted"] if not metric_warnings else C_STYLE["warning"]).pack(side=tk.LEFT)
            if metric_warnings:
                r3w = tk.Frame(card_inner, bg=C_STYLE["bg_card"])
                r3w.pack(fill=tk.X, pady=(2, 0))
                for w in metric_warnings[:5]:
                    tk.Label(r3w, text=f"  • {w[:100]}", font=C_STYLE["font_small"],
                             bg=C_STYLE["bg_card"], fg=C_STYLE["warning_text"]).pack(anchor="w")

        # ── histogram ──
        hist_frame = tk.Frame(top, bg=C_STYLE["bg_card"],
                              highlightbackground=C_STYLE["border"],
                              highlightthickness=1, bd=0)
        hist_frame.grid(row=1, column=0, sticky="nsew",
                        padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["gap_md"]))
        hist_frame.grid_columnconfigure(0, weight=1)
        hist_frame.grid_rowconfigure(0, weight=1)
        hist_inner = tk.Frame(hist_frame, bg=C_STYLE["bg_card"])
        hist_inner.grid(row=0, column=0, sticky="nsew",
                        padx=C_STYLE["pad_md"], pady=C_STYLE["pad_md"])
        hist_inner.grid_columnconfigure(0, weight=1)
        hist_inner.grid_rowconfigure(0, weight=1)

        canvas = tk.Canvas(hist_inner, bg=C_STYLE["bg_card"],
                           highlightthickness=0, bd=0)
        canvas.grid(row=0, column=0, sticky="nsew")

        # bind resize to redraw
        def _redraw(event=None):
            if latencies:
                self._draw_popup_histogram(canvas, latencies)
        canvas.bind("<Configure>", _redraw, add="+")
        # initial draw after layout
        top.after(100, _redraw)

        # ── close button ──
        btn_frame = tk.Frame(top, bg=C_STYLE["bg_main"])
        btn_frame.grid(row=2, column=0, sticky="e",
                       padx=C_STYLE["pad_lg"], pady=(0, C_STYLE["pad_lg"]))
        ttk.Button(btn_frame, text="关闭", style="Secondary.TButton",
                   command=top.destroy).pack()
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
