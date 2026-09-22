#!/usr/bin/env python3
"""主题探针：navy / light 两套主题分别构建截图，并验证界面内切换（重建）路径。"""
import os
import subprocess
import sys
import time
import tkinter as tk

sys.path.insert(0, "/opt/llm-benchmark")
import llm_benchmark as L
from llm_benchmark_app import ui_theme

OUT = "/opt/llm-benchmark/docs/uiux-v2/theme"
os.makedirs(OUT, exist_ok=True)

# 探针会写 llm_benchmark.ini（记住主题），跑完还原，避免污染用户配置
_INI = L.INI_PATH
_ini_backup = open(_INI, "rb").read() if os.path.exists(_INI) else None


def shot(app, path):
    for _ in range(6):
        app.root.update()
        time.sleep(0.2)
    w, h = app.root.winfo_width(), app.root.winfo_height()
    subprocess.run(["import", "-window", "root", "-crop", f"{w}x{h}+0+0", "+repage",
                    path], check=True)
    return w, h


def report(tag, app):
    print(f"[{tag}] theme={ui_theme.ACTIVE_THEME} tabs={app.nb.index('end')} "
          f"bg_main={L.C_STYLE['bg_main']} nav_bg={L.C_STYLE['nav_bg']} "
          f"combo={app.theme_var.get()}")


app = L.LLMBenchmarkApp(tk.Tk())
app.root.update()
report("build", app)
w, h = shot(app, f"{OUT}/01_navy_overview.png")
print("navy 窗口:", w, "x", h)

# 界面内切换（走 _on_theme_selected → 重建界面）
app.theme_var.set(app._theme_label("light"))
app._on_theme_selected()
app.root.update()
report("switch→light", app)
w2, h2 = shot(app, f"{OUT}/02_light_overview.png")
print("light 窗口:", w2, "x", h2)

# 切回
app.theme_var.set(app._theme_label("navy"))
app._on_theme_selected()
app.root.update()
report("switch→navy", app)
w3, h3 = shot(app, f"{OUT}/03_navy_back.png")
print("navy 窗口:", w3, "x", h3)

# 切换后功能自检：页签可切、配置项仍在
app.nb.select(4)
app.root.update()
shot(app, f"{OUT}/04_navy_history_tab.png")
print("切到页签4:", app.nb.index(app.nb.select()), "| url_var:", app.url_var.get())

app.root.destroy()

if _ini_backup is not None:
    with open(_INI, "wb") as f:
        f.write(_ini_backup)
    print("INI 已还原")
print("OK")
