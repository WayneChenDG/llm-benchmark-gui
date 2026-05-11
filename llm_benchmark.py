#!/usr/bin/env python3
"""LLM 并发性能测试工具 — GUI 版
纯标准库实现：tkinter + sqlite3 + urllib + threading，无需额外安装依赖。
支持 OpenAI 兼容 API（通义千问 / DeepSeek / GLM / GPT 等）。
"""
import json
import logging
import sqlite3
import statistics
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from tkinter import messagebox, ttk
from typing import Optional
from urllib import request, error
from urllib.parse import urlparse, urlunparse
DB_PATH = "llm_benchmark_history.db"
LOG_PATH = "llm_benchmark.log"
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
    logging.info("=== LLM Benchmark debug session started ===")
# ---------- 并发建议 ----------
CONCURRENCY_PRESETS = {
    "1   (基线测试 — 测单次延迟)": 1,
    "4   (轻度 — 适合个人开发者 API)": 4,
    "8   (中度 — 推荐，接近大多数 API 限额)": 8,
    "16  (重度 — 团队级 API Key)": 16,
    "32  (压力 — 企业级 Key)": 32,
    "64  (极限 — 需确认服务端 QPS 上限)": 64,
}
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
    """指标卡片：彩色左边条 + 大数值优先 + 小标签在下（Datadog风格）"""
    COLORS = {
        "ttft": C_STYLE["info"], "tps": C_STYLE["accent"],
        "total_tokens": C_STYLE["warning"], "agg_tps": C_STYLE["success"],
    }
    def __init__(self, parent, label: str, value: str = "—", metric_key: str = "", **kw):
        super().__init__(parent, bg=C_STYLE["bg_card"],
                         highlightbackground=C_STYLE["border"],
                         highlightthickness=1, bd=0, **kw)
        self._label = label
        self._value = value
        self._key = metric_key
        self._bar_color = self.COLORS.get(metric_key, C_STYLE["accent"])
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
        tk.Label(inner, text=self._label, font=C_STYLE["font_small"],
                 bg=C_STYLE["bg_card"],
                 fg=C_STYLE["text_secondary"],
                 anchor="w").pack(fill=tk.X)
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
# Database
# ============================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS benchmarks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT NOT NULL,
            api_url     TEXT NOT NULL,
            model       TEXT NOT NULL,
            prompt      TEXT NOT NULL,
            max_tokens  INTEGER,
            temperature REAL,
            concurrency INTEGER NOT NULL,
            total       INTEGER NOT NULL,
            success     INTEGER NOT NULL,
            fail        INTEGER NOT NULL,
            latency_min REAL,
            latency_avg REAL,
            latency_max REAL,
            latency_p50 REAL,
            latency_p95 REAL,
            latency_p99 REAL,
            ttft_avg    REAL,
            tokens_per_sec REAL,
            total_tokens   INTEGER,
            duration_sec   REAL,
            detail_json    TEXT
        )"""
    )
    conn.commit()
    conn.close()
def save_result(d: dict):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO benchmarks
           (created_at, api_url, model, prompt, max_tokens, temperature,
            concurrency, total, success, fail,
            latency_min, latency_avg, latency_max, latency_p50, latency_p95, latency_p99,
            ttft_avg, tokens_per_sec, total_tokens, duration_sec, detail_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            d["api_url"], d["model"], d["prompt"], d["max_tokens"], d["temperature"],
            d["concurrency"], d["total"], d["success"], d["fail"],
            d.get("latency_min"), d.get("latency_avg"), d.get("latency_max"),
            d.get("latency_p50"), d.get("latency_p95"), d.get("latency_p99"),
            d.get("ttft_avg"), d.get("tokens_per_sec"), d.get("total_tokens"),
            d.get("duration_sec"), json.dumps(d.get("detail", []), ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()
def load_history(limit=50):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, created_at, model, concurrency, total, success, fail, "
        "latency_avg, latency_p95, tokens_per_sec, duration_sec "
        "FROM benchmarks ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return rows
# ============================================================
# API caller
# ============================================================
def call_llm(api_url: str, api_key: str, model: str, messages: list[dict],
             max_tokens: int, temperature: float, timeout: int = 60) -> dict:
    """Send one chat-completion request. Returns {ok, latency, ttft, tokens, ...}."""
    body = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }).encode("utf-8")
    req = request.Request(api_url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    if DEBUG_MODE:
        logging.debug("POST %s | model=%s max_tokens=%d temp=%.2f msg_len=%d",
                       api_url, model, max_tokens, temperature, len(body))
    t0 = time.perf_counter()
    try:
        resp = request.urlopen(req, timeout=timeout)
        raw = resp.read()
        latency = time.perf_counter() - t0
        data = json.loads(raw)
        choice = data.get("choices", [{}])[0]
        usage = data.get("usage", {})
        completion_tokens = usage.get("completion_tokens", 0)
        prompt_tokens = usage.get("prompt_tokens", 0)
        total_tokens = usage.get("total_tokens", 0)
        finish = choice.get("finish_reason", "unknown")
        ttft = latency  # non-streaming: TTFT ≈ total latency
        tps = completion_tokens / latency if latency > 0 and completion_tokens > 0 else 0
        if DEBUG_MODE:
            logging.debug("OK latency=%.3fs tokens=%d finish=%s", latency, total_tokens, finish)
        return {
            "ok": True,
            "latency": latency,
            "ttft": ttft,
            "completion_tokens": completion_tokens,
            "prompt_tokens": prompt_tokens,
            "total_tokens": total_tokens,
            "finish_reason": finish,
            "tokens_per_sec": tps,
        }
    except Exception as e:
        latency = time.perf_counter() - t0
        err_msg = str(e)
        # categorize error for GUI troubleshooting advice
        if isinstance(e, error.HTTPError):
            err_type = f"HTTP {e.code}"
        elif isinstance(e, error.URLError):
            err_type = "网络连接失败"
        elif isinstance(e, json.JSONDecodeError):
            err_type = "响应格式错误"
        else:
            err_type = type(e).__name__
        if DEBUG_MODE:
            logging.warning("FAIL latency=%.3fs type=%s error=%s", latency, err_type, err_msg)
        return {"ok": False, "latency": latency, "error": err_msg, "error_type": err_type}
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
def run_benchmark(api_url: str, api_key: str, model: str, messages: list[dict],
                  max_tokens: int, temperature: float,
                  concurrency: int, num_requests: int,
                  progress_cb, done_cb) -> None:
    """Run benchmark in a background thread; call progress_cb(completed, total)
    and done_cb(results_dict) on the main thread."""
    if DEBUG_MODE:
        logging.info("benchmark start: concurrency=%d total=%d", concurrency, num_requests)
    results = []
    lock = threading.Lock()
    completed = [0]  # boxed for mutation in closure
    def worker():
        r = call_llm(api_url, api_key, model, messages, max_tokens, temperature)
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
    # --- aggregate ---
    ok_results = [r for r in results if r["ok"]]
    fail_results = [r for r in results if not r["ok"]]
    latencies = sorted([r["latency"] for r in ok_results])
    ttfts = [r.get("ttft", r["latency"]) for r in ok_results]
    total_tokens = sum(r.get("total_tokens", 0) for r in ok_results)
    tps_vals = [r.get("tokens_per_sec", 0) for r in ok_results]
    def percentile(data, p):
        if not data:
            return 0
        k = (len(data) - 1) * p / 100
        f = int(k)
        c = min(f + 1, len(data) - 1)
        return data[f] + (k - f) * (data[c] - data[f]) if c > f else data[f]
    summary = {
        "api_url": api_url,
        "model": model,
        "prompt": messages[-1]["content"][:100] if messages else "",
        "max_tokens": max_tokens,
        "temperature": temperature,
        "concurrency": concurrency,
        "total": num_requests,
        "success": len(ok_results),
        "fail": len(fail_results),
        "latency_min": round(min(latencies), 3) if latencies else 0,
        "latency_avg": round(statistics.mean(latencies), 3) if latencies else 0,
        "latency_max": round(max(latencies), 3) if latencies else 0,
        "latency_p50": round(percentile(latencies, 50), 3),
        "latency_p95": round(percentile(latencies, 95), 3),
        "latency_p99": round(percentile(latencies, 99), 3),
        "ttft_avg": round(statistics.mean(ttfts), 3) if ttfts else 0,
        "tokens_per_sec": round(statistics.mean(tps_vals), 2) if tps_vals else 0,
        "total_tokens": total_tokens,
        "duration_sec": round(duration, 2),
        "detail": ok_results[:200],
        "fail_detail": fail_results[:50],
    }
    done_cb(summary)
# ============================================================
# GUI
# ============================================================
class LLMBenchmarkApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("LLM 并发性能测试工具")
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"1100x{sh}")  # fill screen height
        self.root.minsize(1024, 680)
        self.root.configure(bg=C_STYLE["bg_main"])
        self._benchmark_running = False
        self._smoke_latency = 0.0
        init_db()
        self._setup_styles()
        self._build_header()
        self._build_body()
        self._build_statusbar()
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
        ttk.Label(title_row, text="LLM Benchmark", style="Title.TLabel").pack(side=tk.LEFT)
        ttk.Label(left, text="OpenAI 兼容接口并发性能测试", style="Subtitle.TLabel").pack(anchor="w")
        right = tk.Frame(inner, bg=C_STYLE["bg_header"])
        right.pack(side=tk.RIGHT)
        # status pill
        pill = tk.Frame(right, bg=C_STYLE["bg_stripe"], highlightbackground=C_STYLE["border"],
                        highlightthickness=1, bd=0)
        pill.pack(side=tk.RIGHT, padx=(C_STYLE["pad_sm"], 0))
        pill_inner = tk.Frame(pill, bg=C_STYLE["bg_stripe"])
        pill_inner.pack(padx=C_STYLE["pad_sm"], pady=3)
        self._status_dot = tk.Label(pill_inner, text=" ●", font=(FONT_FAMILY, 9),
                                    bg=C_STYLE["bg_stripe"], fg=C_STYLE["text_muted"])
        self._status_dot.pack(side=tk.LEFT)
        self._status_badge_lbl = tk.Label(pill_inner, text="空闲",
                                         font=C_STYLE["font_small"],
                                         bg=C_STYLE["bg_stripe"],
                                         fg=C_STYLE["text_secondary"])
        self._status_badge_lbl.pack(side=tk.LEFT)
        self._status_badge_color = {"空闲": C_STYLE["text_muted"],
                                    "测试中": C_STYLE["accent"],
                                    "已完成": C_STYLE["success"],
                                    "失败": C_STYLE["error"]}
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
        # Bind canvas resize to update inner window width
        def _on_canvas_resize(event):
            canvas.itemconfig("inner", width=event.width)
        canvas.bind("<Configure>", _on_canvas_resize, add="+")

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
        self._labeled_input(card_a.content, "模型名称", self.model_var, 2, width=40)
        self.system_var = tk.StringVar(value="你是一个有帮助的助手。")
        self._labeled_text(card_a.content, "系统提示词", self.system_var, 3, height=2)
        self.prompt_var = tk.StringVar(value="请用300字左右介绍机器学习。")
        self._labeled_text(card_a.content, "用户提示词", self.prompt_var, 4, height=2)
        card_b = SectionCard(col, "测试参数")
        card_b.pack(fill=tk.X, pady=(0, C_STYLE["gap_lg"]))
        param_grid = tk.Frame(card_b.content, bg=C_STYLE["bg_card"])
        param_grid.pack(fill=tk.X)
        self.max_tokens_var = tk.IntVar(value=512)
        self._labeled_spin(param_grid, "最大 Token 数", self.max_tokens_var,
                           16, 8192, 0, 0)
        self.temp_var = tk.DoubleVar(value=0.7)
        self._labeled_spin(param_grid, "温度参数", self.temp_var,
                           0.0, 2.0, 0, 1, step=0.1)
        self.total_var = tk.IntVar(value=20)
        self._labeled_spin(param_grid, "请求总数", self.total_var,
                           1, 500, 1, 0, step=5)
        tk.Label(param_grid, text="并发数", font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"]).grid(
            row=1, column=2, sticky="w",
            padx=(C_STYLE["gap_lg"], C_STYLE["pad_sm"]),
            pady=(C_STYLE["gap_sm"], 0))
        self.concurrency_var = tk.StringVar(value=list(CONCURRENCY_PRESETS.keys())[2])
        cb = ttk.Combobox(param_grid, textvariable=self.concurrency_var,
                          values=list(CONCURRENCY_PRESETS.keys()),
                          width=28, state="readonly")
        cb.grid(row=1, column=3, sticky="ew", padx=(0, 0), pady=(C_STYLE["gap_sm"], 0))
        param_grid.columnconfigure(1, weight=1)
        param_grid.columnconfigure(3, weight=1)
        card_c = SectionCard(col, "操作")
        card_c.pack(fill=tk.X)
        btn_row = tk.Frame(card_c.content, bg=C_STYLE["bg_card"])
        btn_row.pack(fill=tk.X, pady=(0, C_STYLE["gap_sm"]))
        self.start_btn = ttk.Button(btn_row, text="开始测试",
                                    style="Primary.TButton",
                                    command=self._start_benchmark)
        self.start_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(btn_row, text="重置配置", style="Secondary.TButton",
                   command=self._reset_config).pack(side=tk.LEFT, fill=tk.X,
                   expand=True, padx=(C_STYLE["pad_sm"], 0))
        self._action_status = tk.Label(card_c.content,
                                       text="就绪 — 请配置参数后开始测试",
                                       font=C_STYLE["font_small"],
                                       bg=C_STYLE["bg_card"],
                                       fg=C_STYLE["text_secondary"])
        self._action_status.pack(anchor="w", pady=(C_STYLE["pad_sm"], 0))
        self.progress = ttk.Progressbar(card_c.content, mode="determinate")
        self.progress.pack(fill=tk.X, pady=(C_STYLE["pad_sm"], 0))
        tip = tk.Label(col, text="提示：先用并发数=1 测基线延迟，再逐步提高并发数测试吞吐上限。",
                       font=C_STYLE["font_small"], bg=C_STYLE["bg_main"],
                       fg=C_STYLE["text_muted"])
        tip.pack(anchor="w", pady=(C_STYLE["pad_sm"], 0))
    def _build_results_tab(self):
        bf = self.bench_frame
        bf.grid_columnconfigure(0, weight=1)
        bf.grid_rowconfigure(0, weight=0)  # status cards — fixed
        bf.grid_rowconfigure(1, weight=0)  # metrics — fixed
        bf.grid_rowconfigure(2, weight=0)  # notice — fixed
        bf.grid_rowconfigure(3, weight=6)  # histogram — most height
        bf.grid_rowconfigure(4, weight=1)  # report — compact
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
        for i, (key, label) in enumerate([
            ("ttft", "平均 TTFT"), ("tps", "单请求 Token/s"),
            ("total_tokens", "总输出 Token 数"), ("agg_tps", "估算总吞吐"),
        ]):
            mi = MetricItem(metric_grid, label, metric_key=key)
            mi.grid(row=0, column=i, sticky="nsew",
                    padx=(0 if i == 0 else C_STYLE["gap_md"], 0))
            self.metrics[key] = mi
        # make metric columns equal-width
        for i in range(4):
            metric_grid.columnconfigure(i, weight=1)
        self.notice_banner = NoticeBanner(bf, "info")
        self.notice_banner.grid(row=2, column=0, sticky="ew",
                                pady=(0, C_STYLE["gap_lg"]))
        hist_card = SectionCard(bf, "延迟分布直方图")
        hist_card.grid(row=3, column=0, sticky="nsew",
                       pady=(0, C_STYLE["gap_lg"]))
        hist_card.columnconfigure(0, weight=1)
        self.hist_canvas = tk.Canvas(hist_card.content,
                                     bg=C_STYLE["bg_card"],
                                     highlightthickness=0, bd=0,
                                     height=250)
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
        t.bind("<FocusOut>", lambda e, v=var, w=t: v.set(w.get("1.0", "end-1c")))
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
    def _set_status_badge(self, status: str):
        color = self._status_badge_color.get(status, C_STYLE["text_secondary"])
        self._status_dot.config(foreground=color)
        self._status_badge_lbl.config(text=status, foreground=color)
    def _reset_config(self):
        self.url_var.set("http://192.168.1.12:8000/v1")
        self.key_var.set("change-me-before-production")
        self.model_var.set("qwen3.5-122b-a10b-fp8")
        self.system_var.set("你是一个有帮助的助手。")
        self.prompt_var.set("请用300字左右介绍机器学习。")
        self.max_tokens_var.set(512)
        self.temp_var.set(0.7)
        self.total_var.set(20)
        self.concurrency_var.set(list(CONCURRENCY_PRESETS.keys())[2])
        for label, t in getattr(self, '_text_widgets', {}).items():
            if "系统" in label:
                t.delete("1.0", tk.END); t.insert("1.0", self.system_var.get())
            elif "用户" in label:
                t.delete("1.0", tk.END); t.insert("1.0", self.prompt_var.get())
        self._action_status.config(text="已重置 — 请配置参数后开始测试")
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
    def _on_preflight_fail(self, step: str, payload):
        self._benchmark_running = False
        self.start_btn.config(state=tk.NORMAL, text="开始测试")
        self.progress["value"] = 0
        self._set_status_badge("失败")
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
        total = self.total_var.get()
        concurrency_label = self.concurrency_var.get()
        concurrency = CONCURRENCY_PRESETS.get(concurrency_label, 8)
        if not api_url:
            messagebox.showerror("错误", "请输入 API 地址")
            return
        if not user_prompt:
            messagebox.showerror("错误", "请输入用户提示词")
            return
        api_url = normalize_api_url(api_url)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        if DEBUG_MODE:
            logging.info("benchmark requested: url=%s model=%s concurrency=%d total=%d",
                         api_url, model, concurrency, total)
        self._benchmark_running = True
        self.start_btn.config(state=tk.DISABLED, text="测试中...")
        self._set_status_badge("测试中")
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
                  concurrency, total),
            daemon=True,
        )
        t.start()
    def _run_preflight_and_benchmark(self, api_url, api_key, model, messages,
                                      max_tokens, temperature, concurrency, total):
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
        if DEBUG_MODE:
            logging.info("preflight: step 2 — smoke test")
        self.root.after(0, lambda: self._set_indicator("smoke", "checking"))
        self.root.after(0, lambda: self.status_label.config(
            text="正在执行基线测试 (1 次请求)...", fg=C_STYLE["accent"]))
        smoke = call_llm(api_url, api_key, model, messages, max_tokens, temperature)
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
        run_benchmark(api_url, api_key, model, messages, max_tokens, temperature,
                      concurrency, total, self._on_progress, self._on_done)
    def _on_progress(self, completed, total):
        self.root.after(0, lambda: self._update_progress(completed, total))
    def _update_progress(self, completed, total):
        self.progress["value"] = completed
        self.status_label.config(text=f"进度: {completed}/{total}")
        self._action_status.config(text=f"测试中... {completed}/{total}")
        self._set_indicator("benchmark", "checking", f"{completed}/{total}")
    def _on_done(self, summary: dict):
        self.root.after(0, lambda: self._show_results(summary))
    def _show_results(self, summary: dict):
        self._benchmark_running = False
        self.start_btn.config(state=tk.NORMAL, text="开始测试")
        self._action_status.config(text="测试完成")
        total = max(summary["total"], 1)
        success_rate = summary["success"] / total * 100
        if summary["fail"] == 0:
            self._set_indicator("benchmark", "pass", f"{success_rate:.0f}% 通过")
            self._set_status_badge("已完成")
            self.status_label.config(text="测试完成")
        elif summary["success"] > 0:
            self._set_indicator("benchmark", "fail", f"{success_rate:.0f}% 通过")
            self._set_status_badge("已完成")
            self.status_label.config(text="测试完成（部分失败）")
        else:
            self._set_indicator("benchmark", "fail", "全部失败")
            self._set_status_badge("失败")
            self.status_label.config(text="测试完成（全部失败）")
        self._set_indicator("duration", "idle", f"{summary['duration_sec']:.1f}s")
        self.metrics["ttft"].set_value(f"{summary['ttft_avg']:.3f}s")
        self.metrics["tps"].set_value(f"{summary['tokens_per_sec']:.2f}")
        self.metrics["total_tokens"].set_value(str(summary["total_tokens"]))
        if summary["duration_sec"] > 0:
            agg = summary["total_tokens"] / summary["duration_sec"]
            self.metrics["agg_tps"].set_value(f"~{agg:.1f} tok/s")
        diag = self._diagnose(summary)
        level = "success" if summary["fail"] == 0 and len(diag) == 1 else \
                "warn" if summary["fail"] == 0 else "error"
        self.notice_banner._level = level
        self.notice_banner.set_text("\n".join(diag) if diag else "")
        if DEBUG_MODE:
            logging.info("result: success=%d fail=%d rate=%.1f%% avg_lat=%.3fs p95=%.3fs tps=%.2f",
                         summary["success"], summary["fail"], success_rate,
                         summary["latency_avg"], summary["latency_p95"],
                         summary["tokens_per_sec"])
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
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            report_path = f"llm_benchmark_report_{ts}.txt"
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
            ratio = summary["latency_avg"] / max(self._smoke_latency, 0.001)
            if ratio > 2.0 and concurrency > 1:
                tips.append(
                    f"  ⚠ 并发延迟放大: 平均延迟 ({summary['latency_avg']:.2f}s)"
                    f" 是基线 ({self._smoke_latency:.2f}s) 的 {ratio:.1f}x。")
                if ratio > 5:
                    tips.append("    服务端可能已达并发上限，建议降低并发数。")
                elif ratio > 3:
                    tips.append("    服务端负载较高，可适当降低并发数以获更低延迟。")
                else:
                    tips.append("    并发带来了可接受的延迟增加。")
        if concurrency >= 16 and summary['latency_p95'] > summary['latency_avg'] * 1.5:
            tips.append(
                f"  ⚠ P95 延迟 ({summary['latency_p95']:.2f}s) 显著高于平均"
                f" ({summary['latency_avg']:.2f}s)，表明高并发下存在排队等待。")
            tips.append("    建议: 检查服务端 `--max-num-seqs` 等并发限制参数。")
        if summary["success"] > 0 and summary["duration_sec"] > 0:
            aggregate_tps = summary["total_tokens"] / summary["duration_sec"]
            if concurrency >= 32 and aggregate_tps < summary["tokens_per_sec"] * concurrency * 0.5:
                tips.append(
                    f"  ⚠ 总吞吐 (~{aggregate_tps:.0f} tok/s) 远低于"
                    f" 理论值 ({summary['tokens_per_sec'] * concurrency:.0f} tok/s)，"
                    f"服务端可能已达吞吐上限。")
        if summary["fail"] == 0 and not tips:
            tips.append("  ✓ 所有检查通过，未发现异常。")
        return tips
    def _generate_report(self, summary: dict) -> str:
        total = max(summary["total"], 1)
        success_rate = summary["success"] / total * 100
        fail_rate = summary["fail"] / total * 100
        sep = "─" * 58
        r = []
        r.append("=" * 60)
        r.append("  LLM 并发性能测试报告")
        r.append("=" * 60)
        r.append(f"  测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        r.append(f"  API 地址: {summary['api_url']}")
        r.append(f"  模型:     {summary['model']}")
        r.append(f"  提示词:   {summary.get('prompt', '')[:60]}")
        r.append("")
        r.append(sep)
        r.append("  1. 请求成功率")
        r.append(sep)
        r.append(f"  总请求数:   {summary['total']}")
        r.append(f"  成功:       {summary['success']}  ({success_rate:.1f}%)")
        r.append(f"  失败:       {summary['fail']}  ({fail_rate:.1f}%)")
        if summary["fail"] == 0:
            r.append("  状态:       PASS — 全部通过")
        elif success_rate >= 90:
            r.append("  状态:       WARN — 部分失败")
        else:
            r.append("  状态:       FAIL — 成功率过低")
        r.append("")
        r.append(sep)
        r.append("  2. 延迟分析")
        r.append(sep)
        r.append(f"  并发数:     {summary['concurrency']}")
        r.append(f"  总耗时:     {summary['duration_sec']:.2f}s")
        r.append(f"  基线延迟:   {self._smoke_latency:.3f}s" if self._smoke_latency > 0 else
                 f"  基线延迟:   (未记录)")
        r.append(f"  最小延迟:   {summary['latency_min']:.3f}s")
        r.append(f"  平均延迟:   {summary['latency_avg']:.3f}s")
        r.append(f"  最大延迟:   {summary['latency_max']:.3f}s")
        r.append(f"  P50 延迟:   {summary['latency_p50']:.3f}s")
        r.append(f"  P95 延迟:   {summary['latency_p95']:.3f}s")
        r.append(f"  P99 延迟:   {summary['latency_p99']:.3f}s")
        if self._smoke_latency > 0 and summary['concurrency'] > 1:
            amp = summary['latency_avg'] / max(self._smoke_latency, 0.001)
            r.append(f"  延迟放大:   {amp:.1f}x (平均 / 基线)")
        r.append("")
        r.append(sep)
        r.append("  3. 吞吐量")
        r.append(sep)
        r.append(f"  平均 TTFT:          {summary['ttft_avg']:.3f}s")
        r.append(f"  单请求 Token/s:     {summary['tokens_per_sec']:.2f}")
        r.append(f"  总输出 Token 数:    {summary['total_tokens']}")
        if summary["duration_sec"] > 0:
            agg = summary["total_tokens"] / summary["duration_sec"]
            r.append(f"  估算总吞吐:         ~{agg:.1f} token/s")
        r.append("")
        r.append(sep)
        r.append("  4. 诊断与建议")
        r.append(sep)
        diag = self._diagnose(summary)
        if diag:
            r.extend(diag)
        else:
            r.append("  (无特殊建议)")
        r.append("")
        r.append(sep)
        r.append("  5. 结论")
        r.append(sep)
        if summary["fail"] == 0:
            if self._smoke_latency > 0 and summary["concurrency"] > 1:
                amp = summary["latency_avg"] / max(self._smoke_latency, 0.001)
                if amp > 3:
                    r.append(f"  全部请求成功。{summary['concurrency']} 并发下平均延迟是基线的 {amp:.1f}x，")
                    r.append(f"  服务端可能存在排队瓶颈，建议降低并发数以获更稳定延迟。")
                else:
                    r.append(f"  全部请求成功，{summary['concurrency']} 并发下性能表现稳定。")
            else:
                r.append(f"  全部请求成功，性能表现正常。")
        elif summary["success"] > 0:
            r.append(f"  成功率 {success_rate:.1f}%，部分请求失败。")
            r.append(f"  请根据上方诊断建议排查问题后重试。")
        else:
            r.append(f"  所有请求均失败。请检查 API 地址、Key 和模型名称后重试。")
        r.append("=" * 60)
        return "\n".join(r)
    def _draw_histogram(self, summary: dict):
        """Draw histogram on the main benchmark canvas."""
        detail = summary.get("detail", [])
        if not detail:
            self.hist_canvas.delete("all")
            self.hist_canvas.create_text(300, 100, text="暂无数据",
                                         font=C_STYLE["font_body"],
                                         fill=C_STYLE["text_secondary"])
            return
        latencies = [r["latency"] for r in detail if r["ok"]]
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
        cols = ("id", "时间", "模型", "并发", "总数", "成功", "失败", "平均延迟", "P95", "Token/s", "耗时")
        self.hist_tree = ttk.Treeview(table_card, columns=cols,
                                      show="headings", selectmode="browse",
                                      style="App.Treeview")
        for c in cols:
            self.hist_tree.heading(c, text=c)
            self.hist_tree.column(c, width=80, anchor="center")
        self.hist_tree.column("id", width=40)
        self.hist_tree.column("时间", width=150)
        self.hist_tree.column("模型", width=120)
        self.hist_tree.column("平均延迟", width=85)
        self.hist_tree.column("P95", width=85)
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
            rid, created, model, conc, total, ok, fail, avg_lat, p95, tps, dur = row
            self.hist_tree.insert("", tk.END, values=(
                rid, created, model, conc, total, ok, fail,
                f"{avg_lat:.2f}s" if avg_lat else "-",
                f"{p95:.2f}s" if p95 else "-",
                f"{tps:.1f}" if tps else "-",
                f"{dur:.1f}s" if dur else "-",
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
        row = conn.execute("SELECT * FROM benchmarks WHERE id=?",
                           (rid,)).fetchone()
        conn.close()
        if not row:
            return
        detail = json.loads(row[21]) if row[21] else []
        latencies = [r["latency"] for r in detail if r["ok"]]
        fail_detail = [r for r in detail if not r["ok"]]

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
        tk.Label(r1, text=f"模型: {row[3]}", font=C_STYLE["font_section"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"]).pack(side=tk.LEFT)
        tk.Label(r1, text=f"  并发: {row[7]}", font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_secondary"]).pack(
            side=tk.LEFT, padx=(C_STYLE["pad_md"], 0))
        tk.Label(r1, text=f"请求: {row[8]}", font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_secondary"]).pack(
            side=tk.LEFT, padx=(C_STYLE["pad_md"], 0))
        succ_color = C_STYLE["success"] if row[9] == row[8] else \
                     C_STYLE["warning"] if row[9] > 0 else C_STYLE["error"]
        tk.Label(r1, text=f"成功: {row[9]}", font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=succ_color).pack(
            side=tk.LEFT, padx=(C_STYLE["pad_md"], 0))
        if row[10] > 0:
            tk.Label(r1, text=f"失败: {row[10]}", font=C_STYLE["font_body"],
                     bg=C_STYLE["bg_card"], fg=C_STYLE["error"]).pack(
                side=tk.LEFT, padx=(C_STYLE["pad_sm"], 0))
        tk.Label(r1, text=f"耗时: {row[20]:.1f}s" if row[20] else "耗时: —",
                 font=C_STYLE["font_body"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_secondary"]).pack(
            side=tk.RIGHT)
        # row 2: latency metrics
        r2 = tk.Frame(card_inner, bg=C_STYLE["bg_card"])
        r2.pack(fill=tk.X, pady=(C_STYLE["gap_sm"], 0))
        metrics_text = (
            f"最小: {row[11]:.3f}s" if row[11] else "最小: —"
        ) + "    " + (
            f"平均: {row[12]:.3f}s" if row[12] else "平均: —"
        ) + "    " + (
            f"最大: {row[13]:.3f}s" if row[13] else "最大: —"
        )
        tk.Label(r2, text=metrics_text, font=C_STYLE["font_small"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_primary"]).pack(side=tk.LEFT)
        pct_text = (
            f"P50: {row[14]:.3f}s" if row[14] else "P50: —"
        ) + "    " + (
            f"P95: {row[15]:.3f}s" if row[15] else "P95: —"
        ) + "    " + (
            f"P99: {row[16]:.3f}s" if row[16] else "P99: —"
        )
        tk.Label(r2, text=pct_text, font=C_STYLE["font_small"],
                 bg=C_STYLE["bg_card"], fg=C_STYLE["text_secondary"]).pack(
            side=tk.LEFT, padx=(C_STYLE["gap_lg"], 0))

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
    main()
