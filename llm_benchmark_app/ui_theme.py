"""ui_theme — JISUMAN LLM Benchmark GUI 设计令牌与组件权威实现。

规范文档：docs/uiux-v2/DESIGN_SPEC.md（本文件是令牌权威源，冲突以本文件为准）

包含：
  TOKENS / SPACE / FONTS / TYPE — 设计令牌
  apply_ttk_styles(root)        — ttk 主题（clam 基线 + 企业级中性观感）
  card / metric_tile / badge / empty_state / kv_grid / demo_banner / toolbar — 组件工厂
  contrast_ratio / luminance    — WCAG 对比度计算（供审计脚本与测试复用）

设计约束（见规范 §1）：
  - 颜色只承载语义，不做装饰；指标卡默认中性色
  - 每个数字都带单位与口径
  - 空态必须给出下一步动作
  - 演示数据必须可见标注
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# ─────────────────────────────────────────────────────────────────────────────
# 字体检测（不依赖外部模块，避免与主程序循环导入）
# ─────────────────────────────────────────────────────────────────────────────

_UI_CANDIDATES = [
    "Noto Sans CJK SC", "Microsoft YaHei UI", "Microsoft YaHei",
    "PingFang SC", "Source Han Sans SC", "Droid Sans Fallback", "DejaVu Sans",
]
_MONO_CANDIDATES = [
    "Noto Sans Mono CJK SC", "JetBrains Mono", "Cascadia Mono", "Consolas",
    "Menlo", "Noto Sans Mono", "DejaVu Sans Mono", "Liberation Mono",
]


def _families() -> set:
    try:
        import tkinter.font as tkfont
        root = tk.Tk()
        root.withdraw()
        fams = set(tkfont.families())
        root.destroy()
        return fams
    except Exception:
        return set()


def detect_font(families: set | None = None) -> str:
    fams = families if families is not None else _families()
    for c in _UI_CANDIDATES:
        if c in fams:
            return c
    return "TkDefaultFont" if not fams else "sans-serif"


def detect_mono(families: set | None = None) -> str:
    fams = families if families is not None else _families()
    for c in _MONO_CANDIDATES:
        if c in fams:
            return c
    return "TkFixedFont" if not fams else "monospace"


_FAMS = _families()
FONT_UI = detect_font(_FAMS)
FONT_MONO = detect_mono(_FAMS)

# ─────────────────────────────────────────────────────────────────────────────
# 令牌
# ─────────────────────────────────────────────────────────────────────────────

# ── 浅色主题（内容区白底 + 浅灰蓝背景；保留品牌蓝）──
_LIGHT: dict[str, str] = {
    # surface
    "bg_main": "#F4F6FA",
    "bg_card": "#FFFFFF",
    "bg_header": "#FFFFFF",
    "bg_input": "#FFFFFF",
    "bg_inset": "#F1F5F9",
    "bg_hover": "#EEF2F8",
    "bg_stripe": "#F8FAFD",
    # text
    "text_primary": "#0F172A",
    "text_secondary": "#475569",
    "text_muted": "#5F6C80",
    "text_inverse": "#FFFFFF",
    "text_disabled": "#94A3B8",
    # border
    "border": "#DCE3EC",
    "border_light": "#EDF1F6",
    "border_strong": "#C3CDDB",
    "border_focus": "#0060F0",
    # brand / accent
    "accent": "#0060F0",
    "accent_hover": "#0052CF",
    "accent_pressed": "#003FA8",
    "accent_disabled": "#9DBEF5",
    "accent_light": "#E8F0FE",
    "accent_soft": "#D3E4FD",
    "accent_text": "#0B4FB0",
    # semantic
    "success": "#2FAF63",
    "success_bg": "#E9F7EF",
    "success_text": "#146C3B",
    "warning": "#D29922",
    "warning_bg": "#FDF6E7",
    "warning_text": "#8A5A00",
    "error": "#E5534B",
    "error_bg": "#FDECEB",
    "error_text": "#B3261E",
    "info": "#4A9EDA",
    "info_bg": "#EAF4FB",
    "info_text": "#0B5FA5",
    "neutral": "#8A92A1",
    "neutral_bg": "#F1F3F6",
    "neutral_text": "#5B6472",
    # data-viz series (仅图表系列 identity)
    "series_1": "#0060F0",
    "series_2": "#14B8A6",
    "series_3": "#D97706",
    "series_4": "#7C3AED",
    "series_baseline": "#64748B",
    # nav（导航条 / 表头 / 状态栏条带；浅色主题＝白底深字）
    "nav_bg": "#FFFFFF",
    "nav_bg_active": "#F1F4F9",
    "nav_fg": "#0F172A",
    "nav_fg_muted": "#475569",
    "nav_border": "#DCE3EC",
    "nav_accent": "#0060F0",
}

# ── 商务深海军蓝（默认）：导航/表头/状态栏深底 + 白色内容区 ──
_NAVY: dict[str, str] = {
    # surface（内容区保持白/浅灰，保证数据可读性）
    "bg_main": "#EFF3F9",
    "bg_card": "#FFFFFF",
    "bg_header": "#FFFFFF",
    "bg_input": "#FFFFFF",
    "bg_inset": "#EDF1F8",
    "bg_hover": "#E9EFF7",
    "bg_stripe": "#F7F9FC",
    # text
    "text_primary": "#0F172A",
    "text_secondary": "#475569",
    "text_muted": "#5F6C80",
    "text_inverse": "#FFFFFF",
    "text_disabled": "#94A3B8",
    # border
    "border": "#D9E1EC",
    "border_light": "#E9EEF5",
    "border_strong": "#BFCADA",
    "border_focus": "#0060F0",
    # brand / accent
    "accent": "#0060F0",
    "accent_hover": "#0052CF",
    "accent_pressed": "#003FA8",
    "accent_disabled": "#9DBEF5",
    "accent_light": "#E8F0FE",
    "accent_soft": "#D3E4FD",
    "accent_text": "#0B4FB0",
    # semantic
    "success": "#2FAF63",
    "success_bg": "#E9F7EF",
    "success_text": "#146C3B",
    "warning": "#D29922",
    "warning_bg": "#FDF6E7",
    "warning_text": "#8A5A00",
    "error": "#E5534B",
    "error_bg": "#FDECEB",
    "error_text": "#B3261E",
    "info": "#4A9EDA",
    "info_bg": "#EAF4FB",
    "info_text": "#0B5FA5",
    "neutral": "#8A92A1",
    "neutral_bg": "#F1F3F6",
    "neutral_text": "#5B6472",
    # data-viz series (仅图表系列 identity)
    "series_1": "#0060F0",
    "series_2": "#14B8A6",
    "series_3": "#D97706",
    "series_4": "#7C3AED",
    "series_baseline": "#64748B",
    # nav（深海军蓝条带）
    "nav_bg": "#14263D",
    "nav_bg_active": "#1D3454",
    "nav_fg": "#F4F7FC",
    "nav_fg_muted": "#A6B8D3",
    "nav_border": "#0D1B2D",
    "nav_accent": "#6BA6FF",
}

THEMES: dict[str, dict[str, str]] = {"navy": _NAVY, "light": _LIGHT}
DEFAULT_THEME = "navy"
ACTIVE_THEME = DEFAULT_THEME
# 兼容既有调用点：TOKENS 恒为"当前主题"的字典（切换时原地更新，引用不失效）
TOKENS: dict[str, str] = dict(THEMES[ACTIVE_THEME])


def theme_keys() -> tuple[str, ...]:
    return tuple(THEMES.keys())


def set_theme(name: str) -> dict[str, str]:
    """切换主题（原地更新 TOKENS，使 TOKENS[...] 的所有读取点立即生效）。"""
    global ACTIVE_THEME
    if name not in THEMES:
        raise KeyError(f"未知主题: {name}（可选: {', '.join(THEMES)}）")
    TOKENS.clear()
    TOKENS.update(THEMES[name])
    ACTIVE_THEME = name
    return TOKENS

SPACE = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24, "xxl": 32}

RADIUS = {"card": 8, "btn": 6, "input": 6}

TYPE: dict[str, tuple] = {
    "display":    (FONT_UI, 26, "bold"),
    "metric_lg":  (FONT_UI, 20, "bold"),
    "metric_md":  (FONT_UI, 15, "bold"),
    "metric_num": (FONT_MONO, 20, "bold"),   # 数值等宽对齐
    "num":        (FONT_MONO, 12, "normal"),
    "section":    (FONT_UI, 13, "bold"),
    "body":       (FONT_UI, 12, "normal"),
    "body_bold":  (FONT_UI, 12, "bold"),
    "label":      (FONT_UI, 12, "normal"),
    "meta":       (FONT_UI, 11, "normal"),
    "caption":    (FONT_UI, 10, "normal"),
}

# 语义色调 → (前景, 背景) 供 metric_tile / badge 使用
TONE_COLORS = {
    "neutral": ("text_primary", "bg_card"),
    "accent":  ("accent_text", "accent_light"),
    "success": ("success_text", "success_bg"),
    "warning": ("warning_text", "warning_bg"),
    "error":   ("error_text", "error_bg"),
    "info":    ("info_text", "info_bg"),
    "muted":   ("text_muted", "bg_card"),
}

DEMO_LABEL = "演示数据（本地 mock 端点，非真实硬件/模型性能）"


# ─────────────────────────────────────────────────────────────────────────────
# WCAG 对比度
# ─────────────────────────────────────────────────────────────────────────────

def _srgb_to_lin(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return 0.2126 * _srgb_to_lin(r) + 0.7152 * _srgb_to_lin(g) + 0.0722 * _srgb_to_lin(b)


def contrast_ratio(fg: str, bg: str) -> float:
    l1, l2 = luminance(fg), luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return round((hi + 0.05) / (lo + 0.05), 2)


# ─────────────────────────────────────────────────────────────────────────────
# ttk 主题
# ─────────────────────────────────────────────────────────────────────────────

def apply_ttk_styles(root) -> ttk.Style:
    """企业级中性观感；风格名沿用旧代码（Primary/Secondary/App.* 等）以保持兼容。"""
    T = TOKENS
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except Exception:
        pass

    st.configure(".", font=TYPE["body"], background=T["bg_main"],
                 foreground=T["text_primary"], focuscolor=T["border_focus"])

    # ── 容器 ──
    st.configure("Card.TFrame", background=T["bg_card"])
    # 顶栏 / 状态栏走 nav 令牌：深海军蓝主题下为深底白字，浅色主题下为白底深字
    st.configure("Header.TFrame", background=T["nav_bg"])
    st.configure("StatusBar.TFrame", background=T["nav_bg"])

    # ── 标签 ──
    for name, spec, bg, fg in [
        ("Title.TLabel",    TYPE["section"],   "nav_bg",    "nav_fg"),
        ("Subtitle.TLabel", TYPE["meta"],      "nav_bg",    "nav_fg_muted"),
        ("NavLabel.TLabel", TYPE["meta"],      "nav_bg",    "nav_fg_muted"),
        ("NavStrong.TLabel", TYPE["body_bold"], "nav_bg",   "nav_fg"),
        ("Section.TLabel",  TYPE["section"],   "bg_card",   "text_primary"),
        ("Body.TLabel",     TYPE["body"],      "bg_card",   "text_primary"),
        ("Small.TLabel",    TYPE["meta"],      "bg_card",   "text_muted"),
        ("Metric.TLabel",   TYPE["metric_num"], "bg_card",  "text_primary"),
        ("MetricSmall.TLabel", TYPE["metric_md"], "bg_card", "text_primary"),
        ("StatusBar.TLabel", TYPE["caption"],  "nav_bg",    "nav_fg_muted"),
        # 新增语义标签
        ("Muted.TLabel",    TYPE["meta"],      "bg_card",   "text_muted"),
        ("Inset.TLabel",    TYPE["meta"],      "bg_inset",  "text_secondary"),
        ("Num.TLabel",      TYPE["num"],       "bg_card",   "text_primary"),
        ("SectionInsetTitle.TLabel", TYPE["section"], "bg_inset", "text_primary"),
    ]:
        st.configure(name, font=spec, background=T[bg], foreground=T[fg])

    # ── 按钮 ──
    st.configure("Primary.TButton", font=TYPE["body_bold"],
                 background=T["accent"], foreground=T["text_inverse"],
                 borderwidth=0, focuscolor=T["accent"], padding=(16, SPACE["sm"]))
    st.map("Primary.TButton",
           background=[("disabled", T["accent_disabled"]), ("pressed", T["accent_pressed"]),
                       ("active", T["accent_hover"])],
           foreground=[("disabled", T["text_inverse"])])

    st.configure("Secondary.TButton", font=TYPE["label"],
                 background=T["bg_card"], foreground=T["text_primary"],
                 bordercolor=T["border_strong"], borderwidth=1,
                 focuscolor=T["border_focus"], padding=(12, SPACE["sm"]), relief="solid")
    st.map("Secondary.TButton",
           background=[("disabled", T["bg_inset"]), ("pressed", T["bg_inset"]),
                       ("active", T["bg_hover"])],
           foreground=[("disabled", T["text_disabled"])])

    st.configure("Ghost.TButton", font=TYPE["label"],
                 background=T["bg_card"], foreground=T["text_secondary"],
                 borderwidth=0, focuscolor=T["border_focus"], padding=(8, SPACE["xs"]))
    st.map("Ghost.TButton",
           background=[("pressed", T["bg_inset"]), ("active", T["bg_hover"])],
           foreground=[("disabled", T["text_disabled"]), ("active", T["text_primary"])])

    st.configure("Danger.TButton", font=TYPE["label"],
                 background=T["bg_card"], foreground=T["error_text"],
                 bordercolor=T["error"], borderwidth=1, relief="solid",
                 focuscolor=T["border_focus"], padding=(12, SPACE["sm"]))
    st.map("Danger.TButton",
           background=[("pressed", T["error_bg"]), ("active", T["error_bg"])])

    # ── 输入控件 ──
    st.configure("App.TEntry", fieldbackground=T["bg_input"], foreground=T["text_primary"],
                 bordercolor=T["border_strong"], lightcolor=T["border_strong"],
                 darkcolor=T["border_strong"], borderwidth=1, padding=6,
                 insertcolor=T["text_primary"])
    st.map("App.TEntry",
           fieldbackground=[("disabled", T["bg_inset"]), ("focus", T["bg_input"])],
           bordercolor=[("focus", T["border_focus"])],
           foreground=[("disabled", T["text_disabled"])])

    st.configure("App.TCombobox", fieldbackground=T["bg_input"], background=T["bg_card"],
                 foreground=T["text_primary"], bordercolor=T["border_strong"],
                 lightcolor=T["border_strong"], darkcolor=T["border_strong"],
                 arrowcolor=T["text_secondary"], borderwidth=1, padding=4)
    st.map("App.TCombobox",
           fieldbackground=[("disabled", T["bg_inset"]), ("readonly", T["bg_input"])],
           bordercolor=[("focus", T["border_focus"])],
           foreground=[("disabled", T["text_disabled"])])

    st.configure("App.TCheckbutton", font=TYPE["label"], background=T["bg_card"],
                 foreground=T["text_primary"], focuscolor=T["border_focus"])
    st.map("App.TCheckbutton",
           background=[("active", T["bg_card"])],
           foreground=[("disabled", T["text_disabled"])])
    st.configure("Inset.TCheckbutton", font=TYPE["label"], background=T["bg_inset"],
                 foreground=T["text_primary"], focuscolor=T["border_focus"])
    st.map("Inset.TCheckbutton", background=[("active", T["bg_inset"])])

    st.configure("App.TRadiobutton", font=TYPE["label"], background=T["bg_card"],
                 foreground=T["text_primary"], focuscolor=T["border_focus"])
    st.map("App.TRadiobutton", background=[("active", T["bg_card"])])

    # ── 表格 ──
    for style_name, row_h in (("App.Treeview", 28), ("Dense.App.Treeview", 24)):
        st.configure(style_name, rowheight=row_h, font=TYPE["body"],
                     background=T["bg_card"], fieldbackground=T["bg_card"],
                     foreground=T["text_primary"], borderwidth=0)
        st.map(style_name,
               background=[("selected", T["accent_light"])],
               foreground=[("selected", T["text_primary"])])
        st.configure(f"{style_name}.Heading", font=TYPE["label"],
                     background=T["nav_bg"], foreground=T["nav_fg"],
                     relief="flat", padding=(SPACE["sm"], SPACE["sm"]),
                     borderwidth=0)
        st.map(f"{style_name}.Heading",
               background=[("active", T["nav_bg_active"])],
               foreground=[("active", T["nav_fg"])])

    # ── 进度条 ──
    st.configure("Accent.Horizontal.TProgressbar",
                 troughcolor=T["bg_inset"], background=T["accent"],
                 bordercolor=T["bg_inset"], lightcolor=T["accent"],
                 darkcolor=T["accent"], thickness=8)
    st.configure("Success.Horizontal.TProgressbar",
                 troughcolor=T["bg_inset"], background=T["success"],
                 bordercolor=T["bg_inset"], lightcolor=T["success"],
                 darkcolor=T["success"], thickness=8)

    # ── Notebook：页签条＝深色导航带（nav 令牌），选中页签回到内容面 ──
    st.configure("App.TNotebook", background=T["nav_bg"], borderwidth=0,
                 tabmargins=(SPACE["xl"], 0, 0, 0))
    st.configure("App.TNotebook.Tab", font=TYPE["label"], padding=(SPACE["lg"], 9),
                 background=T["nav_bg"], foreground=T["nav_fg_muted"],
                 bordercolor=T["nav_bg"], borderwidth=0, focuscolor=T["border_focus"])
    st.map("App.TNotebook.Tab",
           background=[("selected", T["bg_card"]), ("active", T["nav_bg_active"])],
           foreground=[("selected", T["accent_text"]), ("active", T["nav_fg"])],
           expand=[("selected", (0, 0, 0, 0))])

    # ── 深色导航条上的下拉框（语言 / 主题）──
    st.configure("Nav.TCombobox", font=TYPE["label"],
                 fieldbackground=T["nav_bg_active"], background=T["nav_bg_active"],
                 foreground=T["nav_fg"], arrowcolor=T["nav_fg"],
                 bordercolor=T["nav_border"], lightcolor=T["nav_bg_active"],
                 darkcolor=T["nav_bg_active"], borderwidth=0, padding=(6, 2))
    st.map("Nav.TCombobox",
           fieldbackground=[("readonly", T["nav_bg_active"]),
                            ("disabled", T["nav_bg"])],
           foreground=[("readonly", T["nav_fg"]), ("disabled", T["nav_fg_muted"])])

    # ── 滚动条：细、低对比 ──
    for orient in ("Vertical", "Horizontal"):
        st.configure(f"{orient}.TScrollbar", background=T["border_strong"],
                     troughcolor=T["bg_main"], bordercolor=T["bg_main"],
                     arrowcolor=T["text_muted"], borderwidth=0, width=10)
        st.map(f"{orient}.TScrollbar", background=[("active", T["text_muted"])])

    st.configure("TLabelframe", background=T["bg_card"], bordercolor=T["border"],
                 borderwidth=1)
    st.configure("TLabelframe.Label", font=TYPE["label"], background=T["bg_card"],
                 foreground=T["text_secondary"])

    return st


# ─────────────────────────────────────────────────────────────────────────────
# 组件工厂（全部基于 tk，保证背景色可控）
# ─────────────────────────────────────────────────────────────────────────────

def card(parent, title: str | None = None, subtitle: str | None = None,
         bg: str = "bg_card", padx: int = SPACE["lg"], pady: int = SPACE["lg"]):
    """返回 (外层容器, 内容区)。内容区已放好标题行，调用方往 content 里塞控件。"""
    T = TOKENS
    outer = tk.Frame(parent, bg=T[bg], highlightbackground=T["border"],
                     highlightthickness=1, bd=0)
    content = tk.Frame(outer, bg=T[bg])
    content.pack(fill=tk.BOTH, expand=True, padx=padx, pady=pady)
    if title:
        head = tk.Frame(content, bg=T[bg])
        head.pack(fill=tk.X, anchor="w")
        tk.Label(head, text=title, font=TYPE["section"], bg=T[bg],
                 fg=T["text_primary"]).pack(side=tk.LEFT)
        if subtitle:
            tk.Label(head, text=subtitle, font=TYPE["meta"], bg=T[bg],
                     fg=T["text_muted"]).pack(side=tk.LEFT, padx=(SPACE["sm"], 0))
        sub = tk.Frame(content, bg=T["border_light"], height=1)
        sub.pack(fill=tk.X, pady=(SPACE["sm"], SPACE["md"]))
    return outer, content


class MetricTile(tk.Frame):
    """指标卡容器，带可更新数值标签。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.value_label: tk.Label | None = None


class EmptyStateBox(tk.Frame):
    """空态容器。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.inner: tk.Frame | None = None


def metric_tile(parent, label: str, value: str = "—", unit: str | None = None,
                meta: str | None = None, tone: str = "neutral",
                width: int | None = None, bg: str = "bg_card"):
    """指标卡：标签 → 数值(+单位) → 口径备注。默认中性色，tone 只在语义确实好坏时使用。"""
    T = TOKENS
    fg_key, _ = TONE_COLORS.get(tone, TONE_COLORS["neutral"])
    frame = MetricTile(parent, bg=T[bg], highlightbackground=T["border"],
                       highlightthickness=1, bd=0)
    if width:
        frame.configure(width=width)
    inner = tk.Frame(frame, bg=T[bg])
    inner.pack(fill=tk.BOTH, expand=True, padx=SPACE["md"], pady=SPACE["md"])
    tk.Label(inner, text=label, font=TYPE["meta"], bg=T[bg],
             fg=T["text_secondary"], anchor="w").pack(fill=tk.X)
    row = tk.Frame(inner, bg=T[bg])
    row.pack(fill=tk.X, pady=(2, 0))
    v = tk.Label(row, text=value, font=TYPE["metric_num"], bg=T[bg],
                 fg=T[fg_key], anchor="w")
    v.pack(side=tk.LEFT)
    if unit:
        tk.Label(row, text=" " + unit, font=TYPE["meta"], bg=T[bg],
                 fg=T["text_muted"], anchor="w").pack(side=tk.LEFT, pady=(6, 0))
    if meta:
        tk.Label(inner, text=meta, font=TYPE["caption"], bg=T[bg],
                 fg=T["text_muted"], anchor="w", justify="left").pack(fill=tk.X, pady=(2, 0))
    frame.value_label = v
    return frame


def set_metric_value(tile, value: str, unit: str | None = None, tone: str | None = None):
    """更新指标卡数值（保持单位与色调一致）。"""
    T = TOKENS
    if tile is None:
        return
    try:
        lbl = getattr(tile, "value_label", None)
        if lbl is not None:
            lbl.configure(text=value)
            if tone:
                fg_key, _ = TONE_COLORS.get(tone, TONE_COLORS["neutral"])
                lbl.configure(fg=T[fg_key])
    except Exception:
        pass


def badge(parent, text: str, tone: str = "neutral", bg: str = "bg_card"):
    T = TOKENS
    fg_key, bg_key = TONE_COLORS.get(tone, TONE_COLORS["neutral"])
    lbl = tk.Label(parent, text=text, font=TYPE["caption"], bg=T[bg_key], fg=T[fg_key],
                   padx=SPACE["sm"], pady=1)
    return lbl


def empty_state(parent, title: str, body: str, action_text: str | None = None,
                command=None, bg: str = "bg_card", min_height: int = 120):
    """空态：发生了什么 → 能得到什么 → 主操作。禁止只写「暂无数据」。"""
    T = TOKENS
    box = EmptyStateBox(parent, bg=T[bg], highlightbackground=T["border_light"],
                        highlightthickness=1, bd=0)
    inner = tk.Frame(box, bg=T[bg])
    inner.pack(expand=True, pady=SPACE["xl"])
    tk.Label(inner, text=title, font=TYPE["body_bold"], bg=T[bg],
             fg=T["text_primary"]).pack()
    tk.Label(inner, text=body, font=TYPE["meta"], bg=T[bg], fg=T["text_muted"],
             justify="center", wraplength=520).pack(pady=(SPACE["xs"], SPACE["md"]))
    if action_text:
        ttk.Button(inner, text=action_text, style="Primary.TButton",
                   command=command if command is not None else (lambda: None)).pack()
    box.inner = inner
    return box


def kv_grid(parent, rows, key_width: int = 12, bg: str = "bg_card",
            columns: int = 2):
    """键值网格：用于运行证据（Provenance）。缺失值显示「未记录」。"""
    T = TOKENS
    grid = tk.Frame(parent, bg=T[bg])
    for i, (k, v) in enumerate(rows):
        r, c = divmod(i, columns)
        cell = tk.Frame(grid, bg=T[bg])
        cell.grid(row=r, column=c, sticky="w", padx=(0, SPACE["xl"]),
                  pady=(0, SPACE["sm"]))
        tk.Label(cell, text=k, font=TYPE["meta"], bg=T[bg], fg=T["text_muted"],
                 width=key_width, anchor="w").pack(side=tk.LEFT)
        val = "未记录" if v in (None, "", "—") else str(v)
        fg = T["text_muted"] if val == "未记录" else T["text_primary"]
        tk.Label(cell, text=val, font=TYPE["num"], bg=T[bg], fg=fg,
                 anchor="w").pack(side=tk.LEFT)
    for c in range(columns):
        grid.grid_columnconfigure(c, weight=1)
    return grid


def demo_banner(parent, bg: str = "warning_bg"):
    """演示数据横幅（规范 §6：必须在总览可见）。

    对比度：文字用 warning_text on warning_bg（≥4.5:1），色带用 warning 填充，
    不使用白字压琥珀底（白 on #D29922 仅 2.1:1，不达标）。
    """
    T = TOKENS
    bar = tk.Frame(parent, bg=T[bg], highlightbackground=T["warning"],
                   highlightthickness=1, bd=0)
    tk.Frame(bar, bg=T["warning"], width=3).pack(side=tk.LEFT, fill=tk.Y)
    tk.Label(bar, text="演示数据", font=TYPE["caption"], bg=T[bg],
             fg=T["warning_text"], padx=SPACE["sm"],
             pady=SPACE["sm"]).pack(side=tk.LEFT)
    tk.Label(bar, text="当前数据来自本地 mock 端点，非真实硬件/模型性能",
             font=TYPE["meta"], bg=T[bg], fg=T["warning_text"]).pack(
        side=tk.LEFT, pady=SPACE["sm"])
    return bar


def toolbar(parent, bg: str = "bg_header"):
    T = TOKENS
    return tk.Frame(parent, bg=T[bg])


def section_label(parent, text: str, bg: str = "bg_card", fg: str = "text_secondary"):
    T = TOKENS
    return tk.Label(parent, text=text, font=TYPE["section"], bg=T[bg], fg=T[fg],
                    anchor="w")


def hint(parent, text: str, bg: str = "bg_card", tone: str = "muted"):
    T = TOKENS
    fg = T["text_muted"] if tone == "muted" else T[TONE_COLORS[tone][0]]
    return tk.Label(parent, text=text, font=TYPE["meta"], bg=T[bg], fg=fg,
                    anchor="w", justify="left")


# ─────────────────────────────────────────────────────────────────────────────
# 指标格式化（跨页面同口径、同格式）—— 规范 §1.5
# ─────────────────────────────────────────────────────────────────────────────

def fmt_num(v, digits: int = 2, dash: str = "—") -> str:
    if v is None or v == "":
        return dash
    try:
        f = float(v)
    except (TypeError, ValueError):
        return dash
    if digits == 0:
        return f"{f:,.0f}"
    return f"{f:,.{digits}f}"


def fmt_ms(v, dash: str = "—") -> str:
    """毫秒：≥100 取整，<100 保留 1 位。"""
    if v is None or v == "":
        return dash
    try:
        f = float(v)
    except (TypeError, ValueError):
        return dash
    return f"{f:,.0f}" if abs(f) >= 100 else f"{f:,.1f}"


def fmt_pct(v, digits: int = 1, dash: str = "—") -> str:
    if v is None or v == "":
        return dash
    try:
        return f"{float(v):,.{digits}f}%"
    except (TypeError, ValueError):
        return dash


def fmt_tps(v, dash: str = "—") -> str:
    return fmt_num(v, 1, dash)


def fmt_duration(seconds, dash: str = "—") -> str:
    """秒 → 人性化时长：1h2m3s / 2m3s / 3.4s。"""
    if seconds in (None, ""):
        return dash
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return dash
    if s < 60:
        return f"{s:.1f} 秒"
    m, sec = divmod(int(round(s)), 60)
    if m < 60:
        return f"{m} 分 {sec} 秒"
    h, m = divmod(m, 60)
    return f"{h} 时 {m} 分 {sec} 秒"


def data_source_label(summary: dict) -> str:
    """数据来源标注（规范 §6）。"""
    src = (summary or {}).get("data_source") or ""
    return src if src else "真实跑测"


def is_demo(summary: dict) -> bool:
    src = ((summary or {}).get("data_source") or "").lower()
    return "demo" in src or "mock" in src
