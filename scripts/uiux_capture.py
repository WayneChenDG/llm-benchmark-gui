#!/usr/bin/env python3
"""UI/UX 截图探针 — 在虚拟显示（Xvfb）下逐页截图，用于改版前/改版后对比与回归。

用法:
  xvfb-run -s "-screen 0 1920x1080x24" python3 scripts/uiux_capture.py \
      --out docs/uiux-v2/BEFORE --prefix before

说明:
  - 不修改应用逻辑，只实例化 LLMBenchmarkApp 并切换 Notebook 页后截图。
  - 截图通过 ImageMagick `import -window root` 抓取整个虚拟屏幕，再用 PIL
    按应用窗口尺寸裁剪，保证每张图只含应用窗口。
  - 页面清单在 PAGES 中定义；新增页面只需加一行。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import tkinter as tk  # noqa: E402

import llm_benchmark as app_mod  # noqa: E402


# (文件名, 顶层页索引, 子页索引或 None) —— 与 _build_body 的页签顺序一致
PAGES = [
    ("01_overview", 0, None),
    ("02_new_test", 1, None),
    ("03_test_results", 2, None),
    ("04_sweep", 3, None),
    ("05_history_compare", 4, None),
    ("06_reports_evidence", 5, None),
    ("07_env_profiles", 6, None),
    ("08_env_hw", 6, 0),
    ("09_env_sw", 6, 1),
    ("10_env_model", 6, 2),
    ("11_env_profile", 6, 3),
    ("12_env_db", 6, 4),
]


def shot(win_w: int, win_h: int, path: str) -> bool:
    raw = "/tmp/_uiux_raw.png"
    subprocess.run(["import", "-window", "root", raw], check=True)
    from PIL import Image
    im = Image.open(raw)
    im.crop((0, 0, min(win_w, im.width), min(win_h, im.height))).save(path)
    os.remove(raw)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--prefix", default="page")
    ap.add_argument("--settle", type=float, default=1.2)
    args = ap.parse_args()

    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)

    root = tk.Tk()
    app = app_mod.LLMBenchmarkApp(root)
    root.update()
    time.sleep(args.settle)
    root.update()

    win_w, win_h = root.winfo_width(), root.winfo_height()
    print(f"[capture] window={win_w}x{win_h} screen="
          f"{root.winfo_screenwidth()}x{root.winfo_screenheight()}")

    nb = app.nb
    n_tabs = nb.index("end")
    print(f"[capture] notebook tabs = {n_tabs}")

    made, skipped = [], []
    for name, tab_i, sub_i in PAGES:
        path = os.path.join(out, f"{args.prefix}_{name}.png")
        if tab_i is None or tab_i >= n_tabs:
            skipped.append((name, "no such tab"))
            continue
        try:
            nb.select(tab_i)
            root.update()
            time.sleep(0.5)
            if sub_i is not None:
                frame = nb.nametowidget(nb.tabs()[tab_i])
                sub = _find_sub_notebook(frame)
                if sub is None or sub_i >= sub.index("end"):
                    skipped.append((name, "no such sub-tab"))
                    continue
                sub.select(sub_i)
            root.update()
            time.sleep(args.settle)
            shot(win_w, win_h, path)
            made.append(path)
            print(f"[capture] OK  {os.path.basename(path)}")
        except Exception as e:  # noqa: BLE001
            skipped.append((name, f"{type(e).__name__}: {e}"))

    print(f"[capture] made={len(made)} skipped={len(skipped)}")
    for n, why in skipped:
        print(f"[capture] SKIP {n}: {why}")
    root.destroy()
    return 0


def _find_sub_notebook(widget):
    """深度优先找第一个 ttk.Notebook 子页容器。"""
    import tkinter.ttk as ttk
    for child in widget.winfo_children():
        if isinstance(child, ttk.Notebook):
            return child
    for child in widget.winfo_children():
        found = _find_sub_notebook(child)
        if found is not None:
            return found
    return None


if __name__ == "__main__":
    raise SystemExit(main())
