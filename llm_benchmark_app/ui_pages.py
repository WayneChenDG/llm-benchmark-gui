"""ui_pages — UI/UX v2 新增页面：总览 / 历史对比 / 报告与证据。

原则：
  - 只读取既有产物（ResultStore results/runs/*、artifacts_manifest.json）与主程序运行时配置；
    不修改任何测试逻辑、API 调用、原始 JSON 或报告生成路径。
  - 页面构造签名统一为 build_xxx_tab(app, frame)，app 为 LLMBenchmarkApp 实例。
  - 指标格式统一走 ui_theme.fmt_*（同口径、同单位、同小数位）。
"""
from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime

import tkinter as tk
from tkinter import messagebox, ttk

from . import ui_theme as U
from .result_db import RESULTS_ROOT
from .result_store import rs_compare_runs, rs_list_runs, rs_load_summary

T = U.TOKENS
TY = U.TYPE
SP = U.SPACE

# Demo 端点识别（真值由端点/模型名判定；正式版应由后端写入 provenance，见 BACKEND_GAPS）
DEMO_MODEL_PREFIXES = ("demo-", "mock-")
DEMO_URL_MARKERS = ("8123", "demo-mock")


# ─────────────────────────────────────────────────────────────────────────────
# 共用工具
# ─────────────────────────────────────────────────────────────────────────────

def _scroll_area(parent, bg_key: str = "bg_main"):
    """返回 (canvas, inner) —— 纵向滚动容器，主题化滚动条。"""
    bg = T[bg_key]
    canvas = tk.Canvas(parent, bg=bg, highlightthickness=0, bd=0)
    vsb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas, bg=bg)
    win = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=vsb.set)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    vsb.pack(side=tk.RIGHT, fill=tk.Y)

    def _on_config(_e=None):
        canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.itemconfigure(win, width=canvas.winfo_width())

    inner.bind("<Configure>", _on_config)
    canvas.bind("<Configure>", _on_config)
    for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        canvas.bind_all(seq, lambda e, c=canvas: _wheel(c, e))
    return canvas, inner


def _wheel(canvas, event):
    try:
        delta = -1 if getattr(event, "num", 0) == 5 else (
            1 if getattr(event, "num", 0) == 4 else (-1 if event.delta > 0 else 1))
        canvas.yview_scroll(delta * 2, "units")
    except Exception:
        pass


def detect_data_source(summary: dict) -> str:
    """返回 'demo' 或 'real'。演示判定：模型名前缀或端点特征。"""
    s = summary or {}
    model = (s.get("model") or "").lower()
    url = (s.get("api_url") or "").lower()
    if model.startswith(DEMO_MODEL_PREFIXES) or any(m in url for m in DEMO_URL_MARKERS):
        return "demo"
    return "real"


def run_label(summary: dict) -> str:
    ts = summary.get("created_at") or ""
    model = summary.get("model") or "(未记录模型)"
    wl = summary.get("workload") or ""
    tag = " [演示]" if detect_data_source(summary) == "demo" else ""
    return f"{ts} · {model} · {wl}{tag}".strip(" ·")


def _tone_badge(parent, summary: dict, bg_key: str = "bg_card"):
    demo = detect_data_source(summary) == "demo"
    key = "common.demo_badge" if demo else "ov.source_real"
    default = "演示数据" if demo else "真实跑测"
    txt = _tr(parent, key, default)
    return U.badge(parent, txt, tone="warning" if demo else "neutral", bg=bg_key)


def _tr(widget, key: str, default: str | None = None) -> str:
    """安全取 i18n 文案：优先主程序 tr()，失败则用默认值。"""
    try:
        app = getattr(widget, "_app", None)
        if app is None:
            app = _find_app(widget)
        if app is not None and hasattr(app, "tr"):
            val = app.tr(key)
            if val and val != key:
                return val
    except Exception:
        pass
    return default if default is not None else key


def _find_app(widget):
    w = widget
    while w is not None:
        app = getattr(w, "_app", None)
        if app is not None:
            return app
        w = getattr(w, "master", None)
    return None


def _app_attr(app, name, default=None):
    v = getattr(app, name, None)
    try:
        return v.get() if hasattr(v, "get") else (v if v is not None else default)
    except Exception:
        return default


# ─────────────────────────────────────────────────────────────────────────────
# 总览
# ─────────────────────────────────────────────────────────────────────────────

def build_overview_tab(app, frame):
    frame._app = app
    outer = tk.Frame(frame, bg=T["bg_main"])
    outer.pack(fill=tk.BOTH, expand=True, padx=SP["xl"], pady=(SP["lg"], 0))
    canvas, body = _scroll_area(outer)

    # 标题行
    head = tk.Frame(body, bg=T["bg_main"])
    head.pack(fill=tk.X)
    tk.Label(head, text=_tr(frame, "tab.overview", "总览"), font=TY["metric_lg"],
             bg=T["bg_main"], fg=T["text_primary"]).pack(side=tk.LEFT)
    tk.Label(head, text="  " + _tr(frame, "ov.subtitle", "最近一轮关键指标、当前配置与环境、待处理事项"),
             font=TY["meta"], bg=T["bg_main"], fg=T["text_muted"]).pack(side=tk.LEFT)
    ttk.Button(head, text=_tr(frame, "common.refresh", "刷新"), style="Ghost.TButton",
               command=lambda: refresh_overview(app)).pack(side=tk.RIGHT)

    app.ov_demo_slot = tk.Frame(body, bg=T["bg_main"])
    app.ov_demo_slot.pack(fill=tk.X, pady=(SP["md"], 0))

    # 关键指标
    kpi_card, kpi_body = U.card(body, title=_tr(frame, "ov.kpi_title", "最近一轮关键指标"))
    kpi_card.pack(fill=tk.X, pady=(SP["md"], SP["md"]))
    app.ov_kpi_slot = tk.Frame(kpi_body, bg=T["bg_card"])
    app.ov_kpi_slot.pack(fill=tk.X)
    app.ov_kpi_note = U.hint(kpi_body, "")
    app.ov_kpi_note.pack(fill=tk.X, pady=(SP["sm"], 0))

    # 中部：当前配置与环境 + 待处理事项
    mid = tk.Frame(body, bg=T["bg_main"])
    mid.pack(fill=tk.X, pady=(0, SP["md"]))
    mid.grid_columnconfigure(0, weight=3, uniform="ov")
    mid.grid_columnconfigure(1, weight=2, uniform="ov")

    cfg_card, cfg_body = U.card(mid, title=_tr(frame, "ov.cfg_title", "当前配置与环境"))
    cfg_card.grid(row=0, column=0, sticky="nsew", padx=(0, SP["md"]))
    app.ov_cfg_slot = tk.Frame(cfg_body, bg=T["bg_card"])
    app.ov_cfg_slot.pack(fill=tk.X)
    acts = tk.Frame(cfg_body, bg=T["bg_card"])
    acts.pack(fill=tk.X, pady=(SP["md"], 0))
    ttk.Button(acts, text=_tr(frame, "ov.act_new", "新建测试"), style="Primary.TButton",
               command=lambda: app._select_tab(1)).pack(side=tk.LEFT)
    ttk.Button(acts, text=_tr(frame, "ov.act_import_demo", "载入演示配置"),
               style="Secondary.TButton",
               command=lambda: _load_demo_config(app)).pack(side=tk.LEFT, padx=SP["sm"])
    ttk.Button(acts, text=_tr(frame, "ov.act_last_report", "查看最近报告"),
               style="Ghost.TButton",
               command=lambda: _open_latest_report(app)).pack(side=tk.LEFT)

    pend_card, pend_body = U.card(mid, title=_tr(frame, "ov.pending_title", "待处理事项"))
    pend_card.grid(row=0, column=1, sticky="nsew")
    app.ov_pending_slot = tk.Frame(pend_body, bg=T["bg_card"])
    app.ov_pending_slot.pack(fill=tk.X)

    # 最近运行
    recent_card, recent_body = U.card(body, title=_tr(frame, "ov.recent_title", "最近运行"))
    recent_card.pack(fill=tk.BOTH, expand=True, pady=(0, SP["xl"]))
    cols = ("time", "model", "workload", "ttft", "tps", "succ", "source")
    tree = ttk.Treeview(recent_body, columns=cols, show="headings", height=6,
                        style="App.Treeview")
    heads = {
        "time": (_tr(frame, "ov.col_time", "时间"), 150, "w"),
        "model": (_tr(frame, "ov.col_model", "模型"), 240, "w"),
        "workload": (_tr(frame, "ov.col_workload", "负载"), 160, "w"),
        "ttft": ("TTFT (ms)", 110, "e"),
        "tps": ("Output TPS (tok/s)", 182, "e"),
        "succ": ("成功率 (%)", 112, "e"),
        "source": (_tr(frame, "ov.col_source", "数据来源"), 110, "center"),
    }
    for c, (txt, w, anchor) in heads.items():
        tree.heading(c, text=txt)
        tree.column(c, width=w, minwidth=min(w, 110), anchor=anchor,
                    stretch=(c == "model"))
    tree.pack(fill=tk.BOTH, expand=True)
    tree.bind("<Double-1>", lambda e: app._select_tab(4))
    app.ov_tree = tree

    app._refresh_overview = lambda: refresh_overview(app)
    refresh_overview(app)


def refresh_overview(app):
    try:
        runs = rs_list_runs(RESULTS_ROOT)
    except Exception:
        runs = []
    latest = runs[0] if runs else None

    # 演示数据横幅
    for w in app.ov_demo_slot.winfo_children():
        w.destroy()
    if latest is not None and detect_data_source(latest) == "demo":
        U.demo_banner(app.ov_demo_slot).pack(fill=tk.X, pady=(0, SP["md"]))

    # KPI
    for w in app.ov_kpi_slot.winfo_children():
        w.destroy()
    tiles = [
        ("TTFT 平均", "mean_ttft_ms", "ms", "首 token 延迟（单请求）"),
        ("TPOT 平均", "mean_tpot_ms", "ms", "每输出 token 耗时（单请求）"),
        ("Output 吞吐", "output_token_throughput_tok_s", "tok/s", "系统级：输出 token 总量 / 测试时长"),
        ("成功率", "success_rate", "%", "成功请求 / 总请求"),
    ]
    for i, (label, key, unit, note) in enumerate(tiles):
        val = None if latest is None else latest.get(key)
        if key == "success_rate":
            text_v = U.fmt_pct(val) if val is not None else "—"
        elif unit == "ms":
            text_v = U.fmt_ms(val)
        else:
            text_v = U.fmt_tps(val)
        tone = "neutral"
        if key == "success_rate" and val is not None and float(val) < 100:
            tone = "warning"
        tile = U.metric_tile(app.ov_kpi_slot, label, text_v, unit, note, tone=tone)
        tile.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else SP["sm"], 0))
        app.ov_kpi_slot.grid_columnconfigure(i, weight=1, uniform="kpi")

    if latest is None:
        app.ov_kpi_note.configure(
            text=_tr(app.ov_kpi_slot, "ov.kpi_empty_body",
                     "完成一次测试后，这里会显示 TTFT、吞吐、成功率与失败原因。"))
    else:
        src = _tr(app.ov_kpi_slot, "ov.source_demo", "演示数据") \
            if detect_data_source(latest) == "demo" else _tr(app.ov_kpi_slot, "ov.source_real", "真实跑测")
        app.ov_kpi_note.configure(
            text=f"{latest.get('created_at', '')} · {latest.get('model', '')} · "
                 f"{latest.get('workload', '')} · 数据来源：{src}")

    # 当前配置与环境
    for w in app.ov_cfg_slot.winfo_children():
        w.destroy()
    cfg_rows = [
        ("端点地址", _app_attr(app, "url_var", "—")),
        ("模型", _app_attr(app, "model_var", "—")),
        ("并发预设", _app_attr(app, "concurrency_var", "—")),
        ("请求数", _app_attr(app, "total_var", "—")),
        ("最大 Token", _app_attr(app, "max_tokens_var", "—")),
        ("流式", _app_attr(app, "stream_var", "—")),
        ("环境档案", _app_attr(app, "env_profile_var", "") or "未绑定"),
        ("最近环境", (latest or {}).get("environment_profile_name") or "未记录"),
    ]
    U.kv_grid(app.ov_cfg_slot, cfg_rows, key_width=10, columns=2).pack(fill=tk.X)

    # 待处理事项
    for w in app.ov_pending_slot.winfo_children():
        w.destroy()
    items = []
    if latest is None:
        items.append(("info", _tr(app, "ov.kpi_empty_title", "还没有任何测试记录")))
    else:
        fail = latest.get("failed_requests") or 0
        if fail:
            items.append(("error", _tr(app, "ov.pending_fail",
                                       "最近一轮存在失败请求：{n} 条（可在结果详情按错误分类查看）"
                                       ).replace("{n}", str(fail))))
        if detect_data_source(latest) == "demo":
            items.append(("warning", _tr(app, "ov.pending_demo",
                                         "当前数据含演示数据，不可用于对外性能结论")))
        if not latest.get("environment_profile_name"):
            items.append(("info", _tr(app, "ov.pending_no_env",
                                      "尚未绑定环境档案，结果缺少硬件/软件栈快照")))
    if not items:
        items.append(("success", _tr(app, "ov.no_pending", "无待处理事项")))
    for tone, text in items:
        row = tk.Frame(app.ov_pending_slot, bg=T["bg_card"])
        row.pack(fill=tk.X, pady=(0, SP["sm"]))
        U.badge(row, "●", tone=tone).pack(side=tk.LEFT, padx=(0, SP["sm"]))
        tk.Label(row, text=text, font=TY["meta"], bg=T["bg_card"], fg=T["text_primary"],
                 anchor="w", justify="left", wraplength=380).pack(side=tk.LEFT)

    # 最近运行表
    tree = getattr(app, "ov_tree", None)
    if tree is not None:
        tree.delete(*tree.get_children())
        for s in runs[:8]:
            tree.insert("", "end", values=(
                s.get("created_at", ""),
                s.get("model", ""),
                s.get("workload", ""),
                U.fmt_ms(s.get("mean_ttft_ms")),
                U.fmt_tps(s.get("output_token_throughput_tok_s")),
                U.fmt_pct(s.get("success_rate")),
                _tr(app.ov_tree, "ov.source_demo", "演示数据")
                if detect_data_source(s) == "demo"
                else _tr(app.ov_tree, "ov.source_real", "真实跑测"),
            ))


def _load_demo_config(app):
    """载入演示端点配置（本地 mock）。明确提示这是演示数据。"""
    try:
        app.url_var.set("http://127.0.0.1:8123/v1")
        app.model_var.set("demo-qwen3-32b-fp8")
        app.key_var.set("demo-key")
    except Exception:
        pass
    app._select_tab(1)
    messagebox.showinfo(
        "演示配置",
        "已填入本地演示端点（scripts/demo_mock_openai_server.py 启动）：\n"
        "  http://127.0.0.1:8123/v1\n"
        "  模型 demo-qwen3-32b-fp8\n\n"
        "该端点为本机 mock，不代表任何真实硬件/模型性能；\n"
        "产生的数据在总览、结果、历史、导出报告中都会标注「演示数据」。")


def _open_latest_report(app):
    runs = rs_list_runs(RESULTS_ROOT)
    if not runs:
        messagebox.showinfo("最近报告", "还没有任何运行记录。")
        return
    latest = runs[0]
    for key in ("report_txt_path", "report_md_path"):
        p = latest.get(key)
        if p and os.path.isfile(p):
            try:
                mod = _app_module(app)
                if mod and hasattr(mod, "open_directory"):
                    mod.open_directory(os.path.dirname(p))
                else:
                    os.startfile(os.path.dirname(p))  # type: ignore[attr-defined]
            except Exception as e:
                messagebox.showinfo("最近报告", f"报告文件：{p}\n（打开失败：{e}）")
            return
    messagebox.showinfo("最近报告", "最近一轮没有生成报告文件。")


def _app_module(app):
    import sys
    return sys.modules.get(app.__class__.__module__)


# ─────────────────────────────────────────────────────────────────────────────
# 历史对比
# ─────────────────────────────────────────────────────────────────────────────

CMP_META_KEYS = [
    ("model", "模型"),
    ("api_url", "端点"),
    ("benchmark_mode_label_zh", "测试模式"),
    ("benchmark_objective_label_zh", "测试目标"),
    ("concurrency", "并发"),
    ("total_requests", "请求数"),
    ("workload", "负载 (C/N/I/O)"),
    ("environment_profile_name", "环境档案"),
    ("hardware", "硬件"),
    ("backend", "推理框架"),
    ("model_profile", "模型档案"),
    ("parser_profile", "解析器档案"),
]


def build_compare_panel(app, parent):
    """在「历史对比」页内追加对比面板（不改动既有历史表格）。

    parent 使用 grid 布局，因此这里先建一个用 pack 的容器再 grid 进去，避免混用。
    """
    parent._app = app
    outer = tk.Frame(parent, bg=T["bg_main"])
    outer.grid(row=3, column=0, sticky="nsew", pady=(SP["md"], 0))
    parent.grid_rowconfigure(3, weight=1)
    card, body = U.card(outer, title=_tr(parent, "cmp.btn_compare", "开始对比"),
                        subtitle=_tr(parent, "cmp.subtitle",
                                     "选择两次运行比较；配置或口径不同时会显式标注"))
    card.pack(fill=tk.BOTH, expand=True)

    pick = tk.Frame(body, bg=T["bg_card"])
    pick.pack(fill=tk.X)
    tk.Label(pick, text=_tr(parent, "cmp.pick_a", "运行 A（基准）"), font=TY["label"],
             bg=T["bg_card"], fg=T["text_secondary"]).grid(row=0, column=0, sticky="w")
    tk.Label(pick, text=_tr(parent, "cmp.pick_b", "运行 B（对照）"), font=TY["label"],
             bg=T["bg_card"], fg=T["text_secondary"]).grid(row=0, column=1, sticky="w",
                                                           padx=(SP["md"], 0))
    app.cmp_var_a = tk.StringVar()
    app.cmp_var_b = tk.StringVar()
    app.cmp_combo_a = ttk.Combobox(pick, textvariable=app.cmp_var_a, state="readonly",
                                  style="App.TCombobox", width=52, font=TY["body"])
    app.cmp_combo_b = ttk.Combobox(pick, textvariable=app.cmp_var_b, state="readonly",
                                  style="App.TCombobox", width=52, font=TY["body"])
    app.cmp_combo_a.grid(row=1, column=0, sticky="w", pady=(SP["xs"], 0))
    app.cmp_combo_b.grid(row=1, column=1, sticky="w", pady=(SP["xs"], 0), padx=(SP["md"], 0))
    btn_row = tk.Frame(pick, bg=T["bg_card"])
    btn_row.grid(row=0, column=2, rowspan=2, padx=(SP["md"], 0), sticky="s")
    ttk.Button(btn_row, text=_tr(parent, "cmp.btn_compare", "开始对比"),
               style="Primary.TButton",
               command=lambda: run_compare(app)).pack(side=tk.LEFT)
    ttk.Button(btn_row, text=_tr(parent, "cmp.btn_clear", "清除选择"),
               style="Ghost.TButton",
               command=lambda: (app.cmp_var_a.set(""), app.cmp_var_b.set(""),
                                _clear_compare(app))).pack(side=tk.LEFT)

    app.cmp_banner_slot = tk.Frame(body, bg=T["bg_card"])
    app.cmp_banner_slot.pack(fill=tk.X, pady=(SP["md"], 0))
    app.cmp_diff_slot = tk.Frame(body, bg=T["bg_card"])
    app.cmp_diff_slot.pack(fill=tk.X, pady=(SP["sm"], 0))
    app.cmp_metric_slot = tk.Frame(body, bg=T["bg_card"])
    app.cmp_metric_slot.pack(fill=tk.BOTH, expand=True, pady=(SP["sm"], 0))

    app._refresh_compare_options = lambda: refresh_compare_options(app)
    refresh_compare_options(app)


def refresh_compare_options(app):
    try:
        runs = rs_list_runs(RESULTS_ROOT)
    except Exception:
        runs = []
    app._cmp_runs = runs
    labels = [run_label(s) for s in runs]
    for combo in (getattr(app, "cmp_combo_a", None), getattr(app, "cmp_combo_b", None)):
        if combo is not None:
            combo.configure(values=labels)
    _clear_compare(app, initial=True)


def _clear_compare(app, initial: bool = False):
    for slot in ("cmp_banner_slot", "cmp_diff_slot", "cmp_metric_slot"):
        holder = getattr(app, slot, None)
        if holder is not None:
            for w in holder.winfo_children():
                w.destroy()
    if initial:
        holder = getattr(app, "cmp_banner_slot", None)
        if holder is not None:
            U.hint(holder, _tr(holder, "cmp.need_two",
                               "请在上方选择两条记录（A 为基准，B 为对照）后开始对比。")
                   ).pack(anchor="w")


def run_compare(app):
    runs = getattr(app, "_cmp_runs", []) or []
    labels = [run_label(s) for s in runs]
    try:
        ia = labels.index(app.cmp_var_a.get())
        ib = labels.index(app.cmp_var_b.get())
    except ValueError:
        messagebox.showinfo("历史对比", _tr(app.cmp_metric_slot, "cmp.need_two",
                                           "请在上方选择两条记录（A 为基准，B 为对照）后开始对比。"))
        return
    if ia == ib:
        messagebox.showinfo("历史对比", "两次选择的是同一条记录，请选择不同的运行。")
        return

    a, b = runs[ia], runs[ib]
    for slot in ("cmp_banner_slot", "cmp_diff_slot", "cmp_metric_slot"):
        holder = getattr(app, slot, None)
        if holder is not None:
            for w in holder.winfo_children():
                w.destroy()

    # 差异判定：配置/口径差异必须显式标注（规范 §1.3）
    diffs = []
    for key, label in CMP_META_KEYS:
        va, vb = a.get(key), b.get(key)
        if (va or "") != (vb or ""):
            diffs.append((label, va if va not in (None, "") else "未记录",
                          vb if vb not in (None, "") else "未记录"))
    demo_a, demo_b = detect_data_source(a), detect_data_source(b)
    if demo_a != demo_b:
        diffs.append(("数据来源", "演示数据" if demo_a == "demo" else "真实跑测",
                      "演示数据" if demo_b == "demo" else "真实跑测"))

    banner = tk.Frame(app.cmp_banner_slot, bg=T["bg_card"])
    banner.pack(fill=tk.X)
    if diffs:
        tone, bg_key = "warning", "warning_bg"
        U.badge(banner, "⚠", tone=tone).pack(side=tk.LEFT, padx=(0, SP["sm"]))
        tk.Label(banner,
                 text=_tr(banner, "cmp.diff_warn",
                          "不可直接比较：存在 {n} 项配置/口径差异（见下表）"
                          ).replace("{n}", str(len(diffs))),
                 font=TY["body_bold"], bg=T[bg_key], fg=T["warning_text"],
                 padx=SP["sm"], pady=SP["xs"]).pack(side=tk.LEFT)
    else:
        U.badge(banner, "✓", tone="success").pack(side=tk.LEFT, padx=(0, SP["sm"]))
        tk.Label(banner, text=_tr(banner, "cmp.same_note",
                                  "两次运行的配置与口径一致，可直接比较。"),
                 font=TY["meta"], bg=T["bg_card"], fg=T["success_text"],
                 padx=SP["sm"]).pack(side=tk.LEFT)

    # 差异表
    if diffs:
        tv = ttk.Treeview(app.cmp_diff_slot, columns=("k", "a", "b"), show="headings",
                          height=min(len(diffs), 7), style="Dense.App.Treeview")
        for c, txt, w, anchor in (("k", _tr(app.cmp_diff_slot, "cmp.diff_col", "差异项"), 160, "w"),
                                  ("a", "A（基准）", 300, "w"),
                                  ("b", "B（对照）", 300, "w")):
            tv.heading(c, text=txt)
            tv.column(c, width=w, anchor=anchor, stretch=(c != "k"))
        for label, va, vb in diffs:
            tv.insert("", "end", values=(label, str(va), str(vb)))
        tv.pack(fill=tk.X)

    # 指标对比（复用 result_store.rs_compare_runs，口径判定不重写）
    rows = rs_compare_runs([a, b])
    tv2 = ttk.Treeview(app.cmp_metric_slot,
                       columns=("m", "unit", "a", "b", "d", "p"), show="headings",
                       height=min(len(rows) + 1, 16), style="Dense.App.Treeview")
    for c, txt, w, anchor in (
        ("m", _tr(app.cmp_metric_slot, "cmp.metric", "指标"), 230, "w"),
        ("unit", _tr(app.cmp_metric_slot, "cmp.unit", "单位"), 90, "w"),
        ("a", "A（基准）", 110, "e"),
        ("b", "B（对照）", 110, "e"),
        ("d", _tr(app.cmp_metric_slot, "cmp.delta", "差值"), 110, "e"),
        ("p", _tr(app.cmp_metric_slot, "cmp.delta_pct", "变化"), 100, "e"),
    ):
        tv2.heading(c, text=txt)
        tv2.column(c, width=w, anchor=anchor, stretch=(c == "m"))
    tv2.tag_configure("better", foreground=T["success_text"])
    tv2.tag_configure("worse", foreground=T["error_text"])
    tv2.tag_configure("neutral", foreground=T["text_primary"])
    for r in rows:
        tv2.insert("", "end", values=(
            r["label"], r["unit"],
            U.fmt_num(r["baseline"]), U.fmt_num(r["current"]),
            U.fmt_num(r["delta"]), (U.fmt_pct(r["pct_change"]) if r["pct_change"] is not None else "—"),
        ), tags=(r["direction"],))
    tv2.pack(fill=tk.BOTH, expand=True)
    U.hint(app.cmp_metric_slot,
           "颜色含义：绿色为该指标方向更优，红色为更差；差值 = B − A。"
           "不同并发/输入输出长度/温度下的数值不可直接当作同条件性能对比。").pack(
        anchor="w", pady=(SP["xs"], 0))


# ─────────────────────────────────────────────────────────────────────────────
# 报告与证据
# ─────────────────────────────────────────────────────────────────────────────

ARTIFACT_LABELS = {
    "result.json": "原始结果 JSON", "summary.json": "汇总 JSON",
    "config.json": "运行配置", "report.txt": "文字报告 (TXT)",
    "report.md": "文字报告 (Markdown)", "artifacts_manifest.json": "交付清单",
    "e2e_latency_histogram.png": "E2E 延迟分布图", "e2e_latency_histogram.json": "延迟分布数据",
    "sweep_analysis.png": "扫测分析图", "sweep_result.json": "扫测结果 JSON",
    "report_sweep.md": "扫测报告 (Markdown)",
}


def build_export_tab(app, frame):
    frame._app = app
    outer = tk.Frame(frame, bg=T["bg_main"])
    outer.pack(fill=tk.BOTH, expand=True, padx=SP["xl"], pady=(SP["lg"], 0))
    canvas, body = _scroll_area(outer)

    head = tk.Frame(body, bg=T["bg_main"])
    head.pack(fill=tk.X)
    tk.Label(head, text=_tr(frame, "tab.export", "报告与证据"), font=TY["metric_lg"],
             bg=T["bg_main"], fg=T["text_primary"]).pack(side=tk.LEFT)
    tk.Label(head, text="  " + _tr(frame, "exp.subtitle",
                                   "选择一次运行，查看并导出交付件"),
             font=TY["meta"], bg=T["bg_main"], fg=T["text_muted"]).pack(side=tk.LEFT)
    ttk.Button(head, text=_tr(frame, "exp.btn_refresh", "刷新"), style="Ghost.TButton",
               command=lambda: refresh_export(app)).pack(side=tk.RIGHT)

    pick_card, pick_body = U.card(body, title=_tr(frame, "exp.pick_run", "选择运行"))
    pick_card.pack(fill=tk.X, pady=(SP["md"], SP["md"]))
    app.exp_var = tk.StringVar()
    app.exp_combo = ttk.Combobox(pick_body, textvariable=app.exp_var, state="readonly",
                                 style="App.TCombobox", font=TY["body"], width=80)
    app.exp_combo.pack(side=tk.LEFT)
    app.exp_combo.bind("<<ComboboxSelected>>", lambda e: refresh_export(app, keep=True))
    app.exp_btn_open = ttk.Button(pick_body, text=_tr(frame, "exp.btn_open", "打开所在目录"),
                                 style="Secondary.TButton",
                                 command=lambda: _open_run_dir(app))
    app.exp_btn_open.pack(side=tk.LEFT, padx=SP["sm"])
    app.exp_btn_bundle = ttk.Button(pick_body, text=_tr(frame, "exp.btn_bundle", "打包证据（ZIP）"),
                                   style="Primary.TButton",
                                   command=lambda: _bundle_run(app))
    app.exp_btn_bundle.pack(side=tk.LEFT)

    app.exp_warn_slot = tk.Frame(body, bg=T["bg_main"])
    app.exp_warn_slot.pack(fill=tk.X, pady=(0, SP["md"]))

    prov_card, prov_body = U.card(body, title=_tr(frame, "exp.provenance_title", "运行证据（Run Provenance）"))
    prov_card.pack(fill=tk.X, pady=(0, SP["md"]))
    app.exp_prov_slot = tk.Frame(prov_body, bg=T["bg_card"])
    app.exp_prov_slot.pack(fill=tk.X)

    art_card, art_body = U.card(body, title=_tr(frame, "exp.artifacts_title", "交付件清单"))
    art_card.pack(fill=tk.BOTH, expand=True, pady=(0, SP["xl"]))
    app.exp_art_slot = tk.Frame(art_body, bg=T["bg_card"])
    app.exp_art_slot.pack(fill=tk.BOTH, expand=True)

    app._refresh_export = lambda: refresh_export(app)
    refresh_export(app)


def refresh_export(app, keep: bool = False):
    try:
        runs = rs_list_runs(RESULTS_ROOT)
    except Exception:
        runs = []
    app._exp_runs = runs
    labels = [run_label(s) for s in runs]
    if hasattr(app, "exp_combo"):
        app.exp_combo.configure(values=labels)
        if not keep and labels:
            app.exp_var.set(labels[0])

    for slot in ("exp_warn_slot", "exp_prov_slot", "exp_art_slot"):
        holder = getattr(app, slot, None)
        if holder is not None:
            for w in holder.winfo_children():
                w.destroy()

    if not runs:
        app.exp_var.set("")
        _set_export_enabled(app, False)
        U.hint(app.exp_prov_slot, "选择一次运行后显示证据快照与交付件清单。").pack(anchor="w")
        U.empty_state(app.exp_art_slot,
                      _tr(app.exp_art_slot, "exp.empty_title", "还没有可交付的运行记录"),
                      _tr(app.exp_art_slot, "exp.empty_body",
                          "完成一次测试后，报告、图表、原始 JSON 与交付清单会汇总在这里。"),
                      action_text=_tr(app.exp_art_slot, "ov.act_new", "新建测试"),
                      command=lambda: app._select_tab(1)).pack(fill=tk.X)
        return

    try:
        sel = runs[labels.index(app.exp_var.get())]
    except ValueError:
        sel = runs[0]
    _set_export_enabled(app, True)

    if detect_data_source(sel) == "demo":
        bar = U.demo_banner(app.exp_warn_slot)
        bar.pack(fill=tk.X)
        tk.Label(app.exp_warn_slot,
                 text=_tr(app.exp_warn_slot, "exp.demo_warn",
                          "该运行含演示数据，导出件已标注，不可作为真实性能结论交付"),
                 font=TY["meta"], bg=T["bg_main"], fg=T["warning_text"]).pack(anchor="w",
                                                                             pady=(2, 0))

    # Provenance
    miss = _tr(app.exp_prov_slot, "exp.missing", "未记录")
    rows = [
        ("运行 ID", sel.get("run_id", miss)),
        ("时间", sel.get("created_at", miss)),
        ("类型", "并发扫测" if sel.get("run_type") == "sweep" else "单次基准"),
        ("模型", sel.get("model", miss)),
        ("端点", sel.get("api_url", miss)),
        ("环境档案", sel.get("environment_profile_name") or miss),
        ("硬件", sel.get("hardware") or miss),
        ("推理框架", sel.get("backend") or miss),
        ("模型档案", sel.get("model_profile") or miss),
        ("负载", sel.get("workload", miss)),
        ("并发", sel.get("concurrency", miss)),
        ("请求数", sel.get("total_requests", miss)),
        ("数据来源", "演示数据（本地 mock 端点）" if detect_data_source(sel) == "demo" else "真实跑测"),
        ("run 目录", sel.get("_run_dir") or sel.get("run_dir") or miss),
    ]
    U.kv_grid(app.exp_prov_slot, rows, key_width=10, columns=2).pack(fill=tk.X)

    # 交付件
    arts = _collect_artifacts(sel)
    if not arts:
        U.hint(app.exp_art_slot, _tr(app.exp_art_slot, "exp.no_artifacts", "该运行没有可用交付件")).pack(
            anchor="w")
        return
    tv = ttk.Treeview(app.exp_art_slot, columns=("n", "k", "s"), show="headings",
                      height=min(len(arts), 12), style="Dense.App.Treeview")
    for c, txt, w, anchor in (
        ("n", _tr(app.exp_art_slot, "exp.col_artifact", "交付件"), 320, "w"),
        ("k", _tr(app.exp_art_slot, "exp.col_kind", "类型"), 140, "w"),
        ("s", _tr(app.exp_art_slot, "exp.col_size", "大小"), 110, "e"),
    ):
        tv.heading(c, text=txt)
        tv.column(c, width=w, anchor=anchor, stretch=(c == "n"))
    for name, kind, size in arts:
        tv.insert("", "end", values=(name, kind, _fmt_size(size)))
    tv.pack(fill=tk.BOTH, expand=True)


def _set_export_enabled(app, enabled: bool):
    state = "normal" if enabled else "disabled"
    for name in ("exp_btn_open", "exp_btn_bundle"):
        btn = getattr(app, name, None)
        if btn is not None:
            try:
                btn.configure(state=state)
            except Exception:
                pass


def _collect_artifacts(summary: dict):
    run_dir = summary.get("_run_dir") or summary.get("run_dir") or ""
    if not run_dir or not os.path.isdir(run_dir):
        return []
    manifest_path = os.path.join(run_dir, "artifacts_manifest.json")
    out = []
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as f:
                for a in json.load(f):
                    rel = a.get("relative_path") or a.get("name") or ""
                    full = os.path.join(run_dir, rel)
                    size = os.path.getsize(full) if os.path.isfile(full) else 0
                    out.append((rel, a.get("file_type") or "-", size))
        except Exception:
            pass
    if not out:  # 清单缺失时直接扫描目录（兼容旧运行）
        for root, _dirs, files in os.walk(run_dir):
            for fn in files:
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, run_dir)
                out.append((rel, os.path.splitext(fn)[1].lstrip(".") or "-",
                            os.path.getsize(full)))
    out.sort(key=lambda x: (x[1], x[0]))
    return [(n, ARTIFACT_LABELS.get(os.path.basename(n), k), s) for n, k, s in out]


def _fmt_size(n: int) -> str:
    try:
        n = float(n)
    except Exception:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} GB"


def _current_export_run(app):
    runs = getattr(app, "_exp_runs", []) or []
    labels = [run_label(s) for s in runs]
    try:
        return runs[labels.index(app.exp_var.get())]
    except Exception:
        return None


def _open_run_dir(app):
    sel = _current_export_run(app)
    if sel is None:
        messagebox.showinfo("报告与证据", _tr(app.exp_art_slot, "exp.need_run", "请先选择一次运行"))
        return
    path = sel.get("_run_dir") or sel.get("run_dir") or ""
    try:
        mod = _app_module(app)
        if mod and hasattr(mod, "open_directory"):
            mod.open_directory(path)
        else:
            os.startfile(path)  # type: ignore[attr-defined]
    except Exception as e:
        messagebox.showinfo("报告与证据", f"目录：{path}\n（打开失败：{e}）")


def _bundle_run(app):
    sel = _current_export_run(app)
    if sel is None:
        messagebox.showinfo("报告与证据", _tr(app.exp_art_slot, "exp.need_run", "请先选择一次运行"))
        return
    run_dir = sel.get("_run_dir") or sel.get("run_dir") or ""
    if not os.path.isdir(run_dir):
        messagebox.showerror("报告与证据", f"运行目录不存在：{run_dir}")
        return
    out_dir = os.path.join(RESULTS_ROOT, "exports")
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = os.path.join(out_dir, f"{os.path.basename(run_dir)}_evidence_{ts}.zip")
    demo = detect_data_source(sel) == "demo"
    notice = (
        "JISUMAN LLM Benchmark — 交付说明\n"
        f"运行 ID: {sel.get('run_id', '')}\n生成时间: {ts}\n"
        f"模型: {sel.get('model', '')}\n端点: {sel.get('api_url', '')}\n"
        f"数据来源: {'演示数据（本地 mock 端点，非真实硬件/模型性能）' if demo else '真实跑测'}\n\n"
        "包含文件：本运行目录下的 report/report.md、charts、result.json、summary.json、"
        "artifacts_manifest.json 等全部交付件。\n"
    )
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(run_dir):
            for fn in files:
                full = os.path.join(root, fn)
                z.write(full, os.path.relpath(full, run_dir))
        z.writestr("DELIVERY_NOTICE.txt", notice)
    messagebox.showinfo("报告与证据",
                        _tr(app.exp_art_slot, "exp.bundle_done", "证据包已生成：{path}"
                           ).replace("{path}", zip_path))
