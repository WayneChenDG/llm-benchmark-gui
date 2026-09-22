#!/usr/bin/env python3
"""几何探针：切到目标页后测量真实高度/宽度与列宽，定位「列表看不见行」与「表头截断」。"""
import os
import sys

os.environ.setdefault("DISPLAY", os.environ.get("DISPLAY", ":99"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk
from tkinter import font as tkfont
import llm_benchmark as L

TARGETS = (("总览", "_refresh_overview", "ov_tree"),
           ("历史对比", "_refresh_history", "hist_tree"))


def report(app, label, tree):
    print(f"\n[{label}] 高度={tree.winfo_height()} 宽度={tree.winfo_width()} "
          f"map={tree.winfo_ismapped()} 行数={len(tree.get_children())} "
          f"树自身请求高度={tree.winfo_reqheight()}")
    total = 0
    for col in tree["columns"]:
        w = tree.column(col, "width")
        total += w
        txt = tree.heading(col, "text")
        tw = tkfont.Font().measure(txt)
        flag = "  ← 表头超宽" if tw + 18 > w else ""
        print(f"    {col:<11} width={w:<5} 文本宽={tw:<4}{flag}")
    print(f"  列宽合计={total}  可用宽={tree.winfo_width()}")
    chain = []
    w = tree
    for _ in range(4):
        w = w.master
        if w is None:
            break
        chain.append(f"{type(w).__name__}(h={w.winfo_height()},w={w.winfo_width()},"
                     f"reqh={w.winfo_reqheight()},mgr={w.winfo_manager()})")
    print("  父链: " + " <- ".join(chain))


def main():
    root = tk.Tk()
    app = L.LLMBenchmarkApp(root)
    root.update()
    nb = getattr(app, "notebook", None) or getattr(app, "nb", None)
    tabs = nb.tabs() if nb else []
    names = [nb.tab(t, "text").strip() for t in tabs] if nb else []
    print("页签:", names)

    for label, refresh, attr in TARGETS:
        if nb and label in names:
            nb.select(tabs[names.index(label)])
            root.update()
        fn = getattr(app, refresh, None)
        if callable(fn):
            try:
                fn()
            except Exception as e:
                print(f"  {refresh} err: {type(e).__name__}: {e}")
        root.update()
        root.update_idletasks()
        tree = getattr(app, attr, None)
        if tree is None:
            print(f"[{label}] 未找到 {attr}")
            continue
        report(app, label, tree)
    root.destroy()


if __name__ == "__main__":
    main()
