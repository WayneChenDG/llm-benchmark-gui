#!/usr/bin/env python3
"""验证：中英双语下主题切换都正常（含 语言↔主题 往返组合）。

用户报的缺陷：切成 English 后主题切换不正常。
断言点：
  1. 切换语言后，主题下拉显示名随之切换到该语言；
  2. 在下拉里选另一主题 → 真的切过去（ACTIVE_THEME 变化 + ini 记录）；
  3. 语言↔主题交叉往返 4 步后仍正确。
"""
import os
import sys
import tkinter as tk

sys.path.insert(0, "/opt/llm-benchmark")
import llm_benchmark as L
from llm_benchmark_app import ui_theme

_INI = L.INI_PATH
_backup = open(_INI, "rb").read() if os.path.exists(_INI) else None

app = L.LLMBenchmarkApp(tk.Tk())
app.root.update()
failures = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")
    if not ok:
        failures.append(label)


def select_theme_by_index(i):
    """模拟用户在下拉里点第 i 项（该语言下的显示名）。"""
    values = app.theme_combo["values"]
    app.theme_combo.current(i)
    app.theme_var.set(values[i])
    app.theme_combo.event_generate("<<ComboboxSelected>>")
    app.root.update()


def switch_lang(code):
    app.language_var.set(L.I18N[code][
        "language.en" if code == "en_US" else "language.zh"])
    app._on_language_selected()
    app.root.update()


print("0) 起点固定为中文（避免上一轮探针残留的 ini 语言影响判定）")
switch_lang("zh_CN")

print("1) 中文下主题下拉标签")
check("combo values", tuple(app.theme_combo["values"]), ("深海军蓝", "浅色"))

print("2) 切 English → 标签应随之英文化（原缺陷：仍是中文）")
switch_lang("en_US")
check("combo values", tuple(app.theme_combo["values"]), ("Navy Blue", "Light"))
check("combo 当前值", app.theme_var.get(), "Navy Blue")

print("3) 英文下切主题（原缺陷：点了没反应）")
select_theme_by_index(1)
check("ACTIVE_THEME", ui_theme.ACTIVE_THEME, "light")
check("combo 当前值", app.theme_var.get(), "Light")

print("4) 英文下切回深海军蓝")
select_theme_by_index(0)
check("ACTIVE_THEME", ui_theme.ACTIVE_THEME, "navy")

print("5) 切回中文（主题应保持 navy，标签回中文）")
switch_lang("zh_CN")
check("combo values", tuple(app.theme_combo["values"]), ("深海军蓝", "浅色"))
check("combo 当前值", app.theme_var.get(), "深海军蓝")
check("ACTIVE_THEME 保持", ui_theme.ACTIVE_THEME, "navy")

print("6) 中文下切浅色")
select_theme_by_index(1)
check("ACTIVE_THEME", ui_theme.ACTIVE_THEME, "light")

print("7) ini 落盘")
import configparser
cfg = configparser.ConfigParser(); cfg.read(_INI, encoding="utf-8")
check("ini theme", cfg.get("ui", "theme", fallback=None), "light")
check("ini language", cfg.get("ui", "language", fallback=None), "zh_CN")

app.root.destroy()
if _backup is not None:
    with open(_INI, "wb") as f:
        f.write(_backup)

print("\n结果:", "全部通过" if not failures else f"{len(failures)} 项失败: {failures}")
sys.exit(1 if failures else 0)
