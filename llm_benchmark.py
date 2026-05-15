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
    """卡片容器：白色背景 + 1px浅灰边框 + 标题分隔线 + 内边距"""
    def __init__(self, parent, title: str = "", **kw):
        super().__init__(parent, bg=C_STYLE["bg_card"],
                         highlightbackground=C_STYLE["border"],
                         highlightthickness=1, bd=0, **kw)
        self._title = title
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
            hdr = tk.Frame(inner, bg=C_STYLE["bg_card"])
            hdr.grid(row=0, column=0, sticky="ew", pady=(0, C_STYLE["pad_md"]))
            self.title_lbl = ttk.Label(hdr, text=self._title, style="Section.TLabel")
            self.title_lbl.pack(side=tk.LEFT)
            sep = tk.Frame(inner, height=1, bg=C_STYLE["border"])
            sep.grid(row=1, column=0, sticky="ew", pady=(0, C_STYLE["pad_md"]))
            self.content = tk.Frame(inner, bg=C_STYLE["bg_card"])
            self.content.grid(row=2, column=0, sticky="nsew")
        else:
            self.content = tk.Frame(inner, bg=C_STYLE["bg_card"])
            self.content.grid(row=0, column=0, sticky="nsew")
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(0, weight=1)
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
        summary_json TEXT
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
        conn.close()
        return

    # DB exists — check version
    try:
        conn = sqlite3.connect(DB_PATH)
        meta = dict(conn.execute("SELECT key, value FROM benchmark_meta").fetchall())
        version = int(meta.get("schema_version", 0))
        conn.close()
        if version == DB_SCHEMA_VERSION:
            return  # already latest
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
    conn.close()

def save_result(d: dict):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
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
    conn.commit()
    conn.close()

def load_history(limit=50):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, created_at, model, concurrency, total, "
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


def call_llm(api_url: str, api_key: str, model: str, messages: list[dict],
             max_tokens: int, temperature: float, timeout: int = 120,
             stream: bool = True) -> dict:
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
    body_dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": stream,
    }
    if stream:
        # Some OpenAI-compatible servers require stream_options to get usage
        body_dict["stream_options"] = {"include_usage": True}

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

        first_data_line_time = None
        first_json_chunk_time = None
        first_non_empty_time = None
        token_timestamps = []
        completion_tokens = 0
        prompt_tokens = 0
        total_tokens = 0
        finish_reason = "unknown"
        content_pieces = 0
        raw_data_lines = 0

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

                choices = obj.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    text_piece = delta.get("content") or delta.get("reasoning_content") or ""
                    if text_piece:
                        content_pieces += 1
                        token_timestamps.append(now)
                        if first_non_empty_time is None:
                            first_non_empty_time = now
                    fr = choices[0].get("finish_reason") or ""
                    if fr:
                        finish_reason = fr

                usage_chunk = obj.get("usage") or {}
                if usage_chunk:
                    prompt_tokens = usage_chunk.get("prompt_tokens", prompt_tokens)
                    completion_tokens = usage_chunk.get("completion_tokens", completion_tokens)
                    total_tokens = usage_chunk.get("total_tokens", prompt_tokens + completion_tokens)

        request_end = time.perf_counter()
        e2e_latency = request_end - request_start

        # If server didn't return usage, we don't have real token counts
        token_source = "usage" if completion_tokens > 0 else "missing_usage"
        if completion_tokens == 0 and DEBUG_MODE:
            logging.debug("stream: no usage in response, token_source=%s content_pieces=%d",
                          token_source, content_pieces)

        # ── split TTFT: benchmark-aligned (first stream chunk) vs user-visible (first non-empty) ──
        first_data_line_s  = round(first_data_line_time  - request_start, 6) if first_data_line_time  else None
        first_json_chunk_s = round(first_json_chunk_time - request_start, 6) if first_json_chunk_time else None
        first_non_empty_s  = round(first_non_empty_time  - request_start, 6) if first_non_empty_time  else None

        # vLLM-compatible TTFT = first JSON chunk (or first data line as fallback)
        ttft = first_json_chunk_s if first_json_chunk_s is not None else first_data_line_s
        # user-visible TTFT = first non-empty content
        visible_ttft = first_non_empty_s

        first_visible_gap_s = None
        if first_data_line_s is not None and first_non_empty_s is not None:
            first_visible_gap_s = round(first_non_empty_s - first_data_line_s, 6)

        # Compute TPOT using benchmark-aligned TTFT
        tpot = None
        if ttft is not None and completion_tokens >= 2 and (e2e_latency - ttft) > 0:
            tpot = (e2e_latency - ttft) / max(completion_tokens - 1, 1)

        # Visible TPOT (debug only — uses first visible token)
        visible_tpot = None
        if visible_ttft is not None and completion_tokens >= 2 and (e2e_latency - visible_ttft) > 0:
            visible_tpot = (e2e_latency - visible_ttft) / max(completion_tokens - 1, 1)

        # Compute ITL
        itl_values = []
        itl_avg = None
        if len(token_timestamps) >= 2:
            itl_values = [
                token_timestamps[i] - token_timestamps[i - 1]
                for i in range(1, len(token_timestamps))
            ]
            itl_avg = statistics.mean(itl_values)

        if DEBUG_MODE:
            logging.debug("OK (stream) e2e=%.3fs ttft=%.3fs visible_ttft=%.3fs tpot=%.3fs tokens=%d content_pieces=%d finish=%s",
                          e2e_latency, ttft or -1, visible_ttft or -1, tpot or -1, total_tokens, content_pieces, finish_reason)

        debug_fields = {
            "first_data_line_s": first_data_line_s,
            "first_json_chunk_s": first_json_chunk_s,
            "first_non_empty_s": first_non_empty_s,
            "first_visible_gap_s": first_visible_gap_s,
            "content_pieces": content_pieces,
            "raw_data_lines": raw_data_lines,
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
        "first_visible_token_avg": round(statistics.mean(first_visible_tokens), 3) if first_visible_tokens else 0,
        "first_visible_token_p50": _p(first_visible_tokens, 50),
        "first_visible_token_p95": _p(first_visible_tokens, 95),
        "first_visible_token_p99": _p(first_visible_tokens, 99),

        "visible_ttft_avg": round(statistics.mean(visible_ttfts), 3) if visible_ttfts else 0,
        "visible_ttft_p50": _p(visible_ttfts, 50),
        "visible_ttft_p95": _p(visible_ttfts, 95),
        "visible_ttft_p99": _p(visible_ttfts, 99),

        # ── first visible gap ──
        "first_visible_gap_avg": round(statistics.mean(first_visible_gaps), 3) if first_visible_gaps else 0,
        "first_visible_gap_p50": _p(first_visible_gaps, 50),
        "first_visible_gap_p95": _p(first_visible_gaps, 95),
        "first_visible_gap_p99": _p(first_visible_gaps, 99),

        # ── visible TPOT (debug only) ──
        "visible_tpot_avg": round(statistics.mean(visible_tpots), 3) if visible_tpots else 0,
        "visible_tpot_p50": _p(visible_tpots, 50),
        "visible_tpot_p95": _p(visible_tpots, 95),
        "visible_tpot_p99": _p(visible_tpots, 99),

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
    summary["metric_warnings"] = warnings

    return summary


def run_benchmark(api_url: str, api_key: str, model: str, messages: list[dict],
                  max_tokens: int, temperature: float,
                  concurrency: int, num_requests: int,
                  progress_cb, done_cb,
                  stream: bool = True,
                  preset_name: str = "") -> None:
    """Run benchmark in a background thread; call progress_cb(completed, total)
    and done_cb(summary_dict) on the main thread."""
    if DEBUG_MODE:
        logging.info("benchmark start: concurrency=%d total=%d stream=%s preset=%s",
                     concurrency, num_requests, stream, preset_name)
    results = []
    lock = threading.Lock()
    completed = [0]  # boxed for mutation in closure

    def worker():
        r = call_llm(api_url, api_key, model, messages, max_tokens, temperature,
                     stream=stream)
        with lock:
            results.append(r)
            completed[0] += 1
            progress_cb(completed[0], num_requests)
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
        self.root.title("LLM Benchmark GUI")
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = int(sw * 1099 / 1920)
        h = int(sh * 1018 / 1080)
        self.root.geometry(f"{w}x{h}")
        self.root.resizable(False, False)
        self.root.minsize(1024, 680)
        self.root.configure(bg=C_STYLE["bg_main"])
        self._benchmark_running = False
        self._smoke_latency = 0.0
        # ── animated status state ──
        self._run_started_at = None
        self._run_completed = 0
        self._run_total = 0
        self._run_fail = 0
        self._run_phase = "idle"
        self._spinner_index = 0
        self._spinner_after_id = None
        init_db()
        self._setup_styles()
        self._build_header()
        self._build_body()
        self._build_statusbar()
        self._load_config()
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
                   padx=C_STYLE["pad_lg"], pady=C_STYLE["pad_sm"])
        left = tk.Frame(inner, bg=C_STYLE["bg_header"])
        left.pack(side=tk.LEFT)
        # icon + title in one line
        title_row = tk.Frame(left, bg=C_STYLE["bg_header"])
        title_row.pack(anchor="w")
        icon_lbl = tk.Label(title_row, text="⚡", font=(FONT_FAMILY, 16),
                            bg=C_STYLE["bg_header"], fg=C_STYLE["accent"])
        icon_lbl.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(title_row, text="LLM Benchmark GUI", style="Title.TLabel").pack(side=tk.LEFT)
        ttk.Label(left, text="OpenAI 兼容接口并发性能测试", style="Subtitle.TLabel").pack(anchor="w")
        right = tk.Frame(inner, bg=C_STYLE["bg_header"])
        right.pack(side=tk.RIGHT)
        # status pill (fixed min-width to prevent overflow)
        pill = tk.Frame(right, bg=C_STYLE["bg_stripe"], highlightbackground=C_STYLE["border"],
                        highlightthickness=1, bd=0, width=240)
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
        self.history_frame = tk.Frame(self.nb, bg=C_STYLE["bg_main"])
        self.nb.add(self.settings_frame, text="  参数设置  ")
        self.nb.add(self.bench_frame, text="  基准测试  ")
        self.nb.add(self.history_frame, text="  历史记录  ")
        self._build_settings_tab()
        self._build_results_tab()
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

        card_a = SectionCard(col, "API 配置")
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
        card_b = SectionCard(col, "测试参数")
        card_b.pack(fill=tk.X, pady=(0, C_STYLE["gap_lg"]))
        self._build_test_params(card_b.content)


        card_c = SectionCard(col, "操作")
        card_c.pack(fill=tk.X)
        btn_row = tk.Frame(card_c.content, bg=C_STYLE["bg_card"])
        btn_row.pack(fill=tk.X, pady=(0, C_STYLE["gap_sm"]))
        self.start_btn = ttk.Button(btn_row, text="开始测试",
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
                  self.stream_var, self.warmup_var, self.auto_save_var):
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
        hist_card = SectionCard(bf, "E2E Latency Distribution (e2el)")
        hist_card.grid(row=3, column=0, sticky="nsew",
                       pady=(0, C_STYLE["gap_lg"]))
        hist_card.columnconfigure(0, weight=1)
        self.hist_canvas = tk.Canvas(hist_card.content, height=260,
                                     bg=C_STYLE["bg_card"],
                                     highlightthickness=0, bd=0)
        self.hist_canvas.grid(row=0, column=0, sticky="nsew")
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
    # ── animated status badge (spinner + progress + elapsed + fail) ──
    SPINNER_FRAMES = ["|", "/", "-", "\\"]

    def _start_status_animation(self, phase: str, total: int = 0):
        self._run_started_at = time.perf_counter()
        self._run_completed = 0
        self._run_total = total
        self._run_fail = 0
        self._run_phase = phase
        self._spinner_index = 0
        self._animate_status_badge()

    def _stop_status_animation(self, final_status: str, completed=None, total=None, fail=None):
        if self._spinner_after_id:
            try:
                self.root.after_cancel(self._spinner_after_id)
            except Exception:
                pass
            self._spinner_after_id = None
        if completed is not None:
            self._run_completed = completed
        if total is not None:
            self._run_total = total
        if fail is not None:
            self._run_fail = fail
        elapsed = self._format_elapsed()
        if final_status == "completed":
            self._status_dot.config(text="✓", fg=C_STYLE["success"],
                                    font=C_STYLE["font_status"])
            self._status_badge_lbl.config(
                text=f"已完成 · {self._run_completed}/{self._run_total} · ✕{self._run_fail} · {elapsed}",
                fg=C_STYLE["success"])
        elif final_status == "failed":
            self._status_dot.config(text="✕", fg=C_STYLE["error"],
                                    font=C_STYLE["font_status"])
            self._status_badge_lbl.config(
                text=f"失败 · fail={self._run_fail} · {elapsed}",
                fg=C_STYLE["error"])
        else:
            self._status_dot.config(text="●", fg=C_STYLE["text_muted"], font=(FONT_FAMILY, 9))
            self._status_badge_lbl.config(text="空闲", fg=C_STYLE["text_secondary"])

    def _animate_status_badge(self):
        if not self._benchmark_running:
            return
        frame = self.SPINNER_FRAMES[self._spinner_index % len(self.SPINNER_FRAMES)]
        self._spinner_index += 1
        elapsed = self._format_elapsed()
        progress = ""
        if self._run_total:
            progress = f" · {self._run_completed}/{self._run_total}"
        fail_part = f" · ✕{self._run_fail}" if self._run_fail else ""
        phase_text = self._phase_display_name(self._run_phase)
        self._status_dot.config(text=frame, fg=C_STYLE["accent"], font=(FONT_FAMILY, 9))
        self._status_badge_lbl.config(
            text=f"{phase_text}{progress}{fail_part} · {elapsed}",
            fg=C_STYLE["accent"])
        self._spinner_after_id = self.root.after(250, self._animate_status_badge)

    def _format_elapsed(self) -> str:
        if self._run_started_at is None:
            return "00:00"
        sec = int(time.perf_counter() - self._run_started_at)
        return f"{sec // 60:02d}:{sec % 60:02d}"

    def _phase_display_name(self, phase: str) -> str:
        return {
            "idle": "空闲",
            "connectivity": "连接检测中",
            "models": "获取模型中",
            "smoke": "基础测试中",
            "warmup": "预热中",
            "benchmark": "测试中",
            "saving": "保存结果中",
        }.get(phase, phase)
    # ── end animated status badge ──

    def _reset_config(self):
        self.url_var.set("http://192.168.1.12:8000/v1")
        self.key_var.set("change-me-before-production")
        self.model_var.set("qwen3.5-122b-a10b-fp8")
        self.system_var.set("你是一个有帮助的助手。")
        self.prompt_var.set("请用300字左右介绍机器学习。")
        self.max_tokens_var.set(512)
        self.temp_var.set(0.0)
        self.total_var.set("80")
        self.concurrency_var.set(DEFAULT_PRESET_KEY)
        self.stream_var.set("是")
        self.warmup_var.set(2)
        self._load_config()  # overlay INI values if available
        self._action_status.config(text="已重置 — 请配置参数后开始测试")
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
            "warmup": str(self.warmup_var.get()),
            "auto_save": self.auto_save_var.get(),
        }
        with open(INI_PATH, "w", encoding="utf-8") as f:
            cfg.write(f)
        if not silent:
            self._action_status.config(text="配置已保存到 llm_benchmark.ini")
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
        self.status_label = tk.Label(inner, text="就绪",
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
        self._benchmark_running = False
        self.start_btn.config(state=tk.NORMAL, text="开始测试")
        self.progress["value"] = 0
        self._stop_status_animation("failed", fail=max(self._run_fail, 1))
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
        if self._benchmark_running:
            return
        api_url = self.url_var.get().strip()
        api_key = self.key_var.get().strip()
        model = self.model_var.get().strip()
        system_prompt = self.system_var.get().strip()
        user_prompt = self.prompt_var.get().strip()
        max_tokens = self.max_tokens_var.get()
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
            messagebox.showerror("错误", "请输入 API 地址")
            return
        if not user_prompt:
            messagebox.showerror("错误", "请输入用户提示词")
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
        self._benchmark_running = True
        self.start_btn.config(state=tk.DISABLED, text="测试中...")
        self._start_status_animation("connectivity", total=total)
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
            target=self._run_preflight_and_benchmark,
            args=(api_url, api_key, model, messages, max_tokens, temperature,
                  concurrency, total, stream, warmup, preset_name),
            daemon=True,
        )
        t.start()
    def _run_preflight_and_benchmark(self, api_url, api_key, model, messages,
                                      max_tokens, temperature, concurrency, total, stream,
                                      warmup=0, preset_name=""):
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
        self.root.after(0, lambda: setattr(self, '_run_phase', 'models'))
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
            self.root.after(0, lambda: setattr(self, '_run_phase', 'warmup'))
            self.root.after(0, lambda: self.status_label.config(
                text=f"预热中 ({warmup} 次请求)...", fg=C_STYLE["accent"]))
            for i in range(warmup):
                call_llm(api_url, api_key, model, messages, max_tokens, temperature,
                        stream=stream)
                self.root.after(0, lambda c=i+1: self._action_status.config(
                    text=f"预热中 — {c}/{warmup}"))

        self.root.after(0, lambda: setattr(self, '_run_phase', 'smoke'))

        if DEBUG_MODE:
            logging.info("preflight: step 2 — smoke test")
        self.root.after(0, lambda: self._set_indicator("smoke", "checking"))
        self.root.after(0, lambda: self.status_label.config(
            text="正在执行基线测试 (1 次请求)...", fg=C_STYLE["accent"]))
        smoke = call_llm(api_url, api_key, model, messages, max_tokens, temperature,
                        stream=stream)
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
        self.root.after(0, lambda: setattr(self, '_run_phase', 'benchmark'))

        def _done_with_warmup(s):
            s["warmup_requests"] = warmup
            self._on_done(s)

        run_benchmark(api_url, api_key, model, messages, max_tokens, temperature,
                      concurrency, total, self._on_progress, _done_with_warmup,
                      stream=stream, preset_name=preset_name)
    def _on_progress(self, completed, total):
        self.root.after(0, lambda: self._update_progress(completed, total))
    def _update_progress(self, completed, total):
        self._run_completed = completed
        self._run_total = total
        self.progress["value"] = completed
        elapsed = self._format_elapsed()
        self.status_label.config(text=f"进度: {completed}/{total}")
        self._action_status.config(
            text=f"测试中 — {completed}/{total} · fail={self._run_fail} · {elapsed}")
        self._set_indicator("benchmark", "checking", f"{completed}/{total}")
    def _on_done(self, summary: dict):
        self.root.after(0, lambda: self._show_results(summary))
    def _show_results(self, summary: dict):
        self._benchmark_running = False
        self.start_btn.config(state=tk.NORMAL, text="开始测试")
        elapsed = self._format_elapsed()
        self._action_status.config(
            text=f"已完成 — {summary['total']} 请求 · success={summary['success']} · fail={summary['fail']} · {elapsed}")
        total = max(summary["total"], 1)
        success_rate = summary["success"] / total * 100
        if summary["fail"] == 0:
            self._set_indicator("benchmark", "pass", f"{success_rate:.0f}% 通过")
            self._stop_status_animation("completed",
                completed=summary["success"], total=summary["total"], fail=0)
            self.status_label.config(text="测试完成")
        elif summary["success"] > 0:
            self._set_indicator("benchmark", "fail", f"{success_rate:.0f}% 通过")
            self._stop_status_animation("completed",
                completed=summary["success"], total=summary["total"], fail=summary["fail"])
            self.status_label.config(text="测试完成（部分失败）")
        else:
            self._set_indicator("benchmark", "fail", "全部失败")
            self._stop_status_animation("failed", fail=summary["fail"])
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
        vt = summary.get("visible_ttft_avg", 0)
        self.metrics["visible_ttft"].set_value(
            f"{vt:.3f}s" if summary.get("stream_mode") and vt > 0 else "N/A")
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
            self._run_phase = "saving"
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
        gap_avg = summary.get("first_visible_gap_avg", 0)
        gap_p95 = summary.get("first_visible_gap_p95", 0)
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

        visible_tpot_avg = summary.get("visible_tpot_avg", 0)
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
            # ── First Visible Token Latency（首字延迟）──
            r.append(f"  First Visible Token Latency（首字延迟）:")
            r.append(f"    请求发出 → 首个非空 delta.content / delta.reasoning_content。")
            r.append(f"    用于衡量用户首字体验。")
            r.append(f"    avg: {summary.get('visible_ttft_avg', 0):.3f}s  p50: {summary.get('visible_ttft_p50', 0):.3f}s  p95: {summary.get('visible_ttft_p95', 0):.3f}s  p99: {summary.get('visible_ttft_p99', 0):.3f}s")
            # ── First Visible Gap（首包到首字间隔）──
            r.append(f"  First Visible Gap（首包到首字间隔）:")
            r.append(f"    First Visible Token − First Stream Chunk。")
            r.append(f"    用于观察服务端已开始流式响应但可见内容延迟出现的情况。")
            r.append(f"    avg: {summary.get('first_visible_gap_avg', 0):.3f}s  p50: {summary.get('first_visible_gap_p50', 0):.3f}s  p95: {summary.get('first_visible_gap_p95', 0):.3f}s  p99: {summary.get('first_visible_gap_p99', 0):.3f}s")
            # ── TPOT / Time Per Output Token（单 Token 耗时）──
            r.append(f"  TPOT / Time Per Output Token（单 Token 耗时）:")
            r.append(f"    (E2E − TTFT) / (completion_tokens − 1)，使用 First Stream Chunk 作为 TTFT。")
            r.append(f"    avg: {summary.get('tpot_avg', 0):.3f}s  p50: {summary.get('tpot_p50', 0):.3f}s  p95: {summary.get('tpot_p95', 0):.3f}s  p99: {summary.get('tpot_p99', 0):.3f}s")
            # ── Visible TPOT（仅供诊断）──
            r.append(f"  Visible TPOT（仅供诊断）:")
            r.append(f"    (E2E − First Visible Token) / (completion_tokens − 1)。")
            r.append(f"    受首字延迟影响，不作为主 benchmark TPOT。")
            r.append(f"    avg: {summary.get('visible_tpot_avg', 0):.3f}s  p50: {summary.get('visible_tpot_p50', 0):.3f}s  p95: {summary.get('visible_tpot_p95', 0):.3f}s  p99: {summary.get('visible_tpot_p99', 0):.3f}s")
            # ── ITL / Inter-Token Latency（Token 间隔）──
            r.append(f"  ITL / Inter-Token Latency（Token 间隔）:")
            r.append(f"    相邻流式响应 chunk 的时间间隔。")
            r.append(f"    avg: {summary.get('itl_avg', 0):.3f}s  p50: {summary.get('itl_p50', 0):.3f}s  p95: {summary.get('itl_p95', 0):.3f}s  p99: {summary.get('itl_p99', 0):.3f}s")
        else:
            r.append("  TTFT / TPOT / ITL: N/A（非流式模式无法真实测量）")

        # ── TTFT Debug ──
        ok_count_rpt = summary.get("success", 0)
        if stream_mode and ok_count_rpt > 0:
            r.append("")
            r.append("  TTFT Debug (校准参考):")
            r.append(f"    first_data_line  avg: {summary.get('first_data_line_avg', 0):.4f}s  p50: {summary.get('first_data_line_p50', 0):.4f}s")
            r.append(f"    first_json_chunk avg: {summary.get('first_json_chunk_avg', 0):.4f}s  p50: {summary.get('first_json_chunk_p50', 0):.4f}s")
            r.append(f"    first_visible_token avg: {summary.get('first_visible_token_avg', 0):.4f}s  p50: {summary.get('first_visible_token_p50', 0):.4f}s")
            r.append(f"    first_visible_gap avg: {summary.get('first_visible_gap_avg', 0):.4f}s")

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
        r.append("  TTFT (Time to First Token):")
        r.append("    请求发出 → 首个非空输出 chunk (delta.content / delta.reasoning_content)")
        r.append("    对应 vLLM bench serve --percentile-metrics ttft")
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
        """Draw E2E latency histogram on the main benchmark canvas."""
        detail = summary.get("detail", [])
        if not detail:
            self.hist_canvas.delete("all")
            self.hist_canvas.create_text(300, 100, text="暂无数据",
                                         font=C_STYLE["font_body"],
                                         fill=C_STYLE["text_secondary"])
            return
        latencies = [r["e2e_latency"] for r in detail if r["ok"]]
        if not latencies:
            self.hist_canvas.delete("all")
            self.hist_canvas.create_text(300, 100, text="暂无成功请求",
                                         font=C_STYLE["font_body"],
                                         fill=C_STYLE["text_secondary"])
            return
        self._draw_popup_histogram(self.hist_canvas, latencies)
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
        # toolbar with subtle background
        toolbar = tk.Frame(hf, bg=C_STYLE["bg_card"],
                           highlightbackground=C_STYLE["border"],
                           highlightthickness=1, bd=0)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, C_STYLE["gap_lg"]))
        toolbar_inner = tk.Frame(toolbar, bg=C_STYLE["bg_card"])
        toolbar_inner.pack(fill=tk.X, padx=C_STYLE["pad_lg"], pady=C_STYLE["pad_sm"])
        ttk.Button(toolbar_inner, text="↻ 刷新", style="Secondary.TButton",
                   command=self._refresh_history).pack(side=tk.LEFT)
        ttk.Button(toolbar_inner, text="✕ 清空记录", style="Secondary.TButton",
                   command=self._clear_history).pack(side=tk.LEFT, padx=C_STYLE["pad_sm"])
        lbl = tk.Label(toolbar_inner, text="双击行查看详情",
                       font=C_STYLE["font_small"],
                       bg=C_STYLE["bg_card"], fg=C_STYLE["text_muted"])
        lbl.pack(side=tk.RIGHT)
        # table card
        table_card = tk.Frame(hf, bg=C_STYLE["bg_card"],
                              highlightbackground=C_STYLE["border"],
                              highlightthickness=1, bd=0)
        table_card.grid(row=1, column=0, sticky="nsew")
        table_card.grid_columnconfigure(0, weight=1)
        table_card.grid_rowconfigure(0, weight=1)
        cols = ("id", "时间", "模型", "并发", "请求", "成功率", "E2E P95", "TTFT Avg", "Output TPS", "Req/s", "耗时", "标准")
        self.hist_tree = ttk.Treeview(table_card, columns=cols,
                                      show="headings", selectmode="browse",
                                      style="App.Treeview")
        for c in cols:
            self.hist_tree.heading(c, text=c)
            self.hist_tree.column(c, width=80, anchor="center")
        self.hist_tree.column("id", width=40)
        self.hist_tree.column("时间", width=130)
        self.hist_tree.column("模型", width=100)
        self.hist_tree.column("成功率", width=65)
        self.hist_tree.column("E2E P95", width=80)
        self.hist_tree.column("TTFT Avg", width=75)
        self.hist_tree.column("Output TPS", width=80)
        self.hist_tree.column("Req/s", width=60)
        self.hist_tree.column("耗时", width=65)
        self.hist_tree.column("标准", width=60)
        scrollbar = ttk.Scrollbar(table_card, orient=tk.VERTICAL,
                                  command=self.hist_tree.yview)
        self.hist_tree.configure(yscrollcommand=scrollbar.set)
        self.hist_tree.grid(row=0, column=0, sticky="nsew",
                            padx=(C_STYLE["pad_lg"], 0),
                            pady=C_STYLE["pad_lg"])
        scrollbar.grid(row=0, column=1, sticky="ns",
                       padx=(0, C_STYLE["pad_lg"]),
                       pady=C_STYLE["pad_lg"])
        self.hist_tree.bind("<Double-1>", self._on_history_double_click)
        self._refresh_history()
    def _refresh_history(self):
        for item in self.hist_tree.get_children():
            self.hist_tree.delete(item)
        for row in load_history():
            rid = row["id"]
            created = row["created_at"]
            model = row["model"]
            conc = row["concurrency"]
            total = row["total"]
            success_rate = row["success_rate"] or 0.0
            e2e_p95 = row["e2e_latency_p95"] or 0.0
            ttft_avg = row["ttft_avg"]
            sys_tps = row["system_output_tps"] or 0.0
            rps = row["request_throughput_rps"] or 0.0
            dur = row["duration_sec"]
            std = "v1" if (row["metric_standard"] or "").startswith("JISUMAN") else "-"
            self.hist_tree.insert("", tk.END, values=(
                rid, created, model, conc, total,
                f"{success_rate:.0f}%" if success_rate else "-",
                f"{e2e_p95:.2f}s" if e2e_p95 else "-",
                f"{ttft_avg:.3f}s" if ttft_avg else "-",
                f"{sys_tps:.1f}" if sys_tps else "-",
                f"{rps:.2f}" if rps else "-",
                f"{dur:.1f}s" if dur else "-",
                std,
            ))
    def _clear_history(self):
        if not messagebox.askyesno("确认", "确定要清空所有历史记录吗？"):
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
            canvas.create_text(200, 80, text="暂无延迟数据",
                               font=C_STYLE["font_body"],
                               fill=C_STYLE["text_secondary"])
            return
        canvas.update_idletasks()
        w = canvas.winfo_width() or 580
        h = canvas.winfo_height() or 220
        margin_l, margin_r, margin_t, margin_b = 55, 25, 20, 35
        plot_w = w - margin_l - margin_r
        plot_h = h - margin_t - margin_b
        min_l, max_l = min(latencies), max(latencies)
        if max_l == min_l:
            max_l = min_l + 0.001
        bin_count = min(25, max(6, len(latencies) // 2))
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
            bar_h = count / max_bin * plot_h
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
        # x-axis labels
        for i in range(0, bin_count + 1, max(1, bin_count // 5)):
            x = margin_l + i * plot_w / bin_count
            val = min_l + i * bin_w
            canvas.create_text(x, margin_t + plot_h + 14,
                               text=f"{val:.2f}s",
                               font=C_STYLE["font_small"],
                               fill=C_STYLE["text_secondary"])
        canvas.create_text(w / 2, h - 8, text="延迟 (秒)",
                           font=C_STYLE["font_small"],
                           fill=C_STYLE["text_secondary"])
        canvas.create_text(14, h / 2, text="请求数", angle=90,
                           font=C_STYLE["font_small"],
                           fill=C_STYLE["text_secondary"])

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
