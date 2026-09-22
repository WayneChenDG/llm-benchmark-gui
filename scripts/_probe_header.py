#!/usr/bin/env python3
"""顶栏侦察：抓左上角放大图 + 量出各控件文本与宽度（定位"显示不全"）。"""
import os
import subprocess
import sys

sys.path.insert(0, "/opt/llm-benchmark")
import tkinter as tk
from tkinter import font as tkfont
import llm_benchmark as L

OUT = "/opt/llm-benchmark/docs/uiux-v2/header_probe.png"


def main():
    root = tk.Tk()
    root.geometry("1432x950")
    app = L.LLMBenchmarkApp(root)
    for _ in range(4):
        root.update()
        root.update_idletasks()

    print("=== 顶栏内所有 Label 的文本与尺寸 ===")
    hdr = getattr(app, "header", None) or getattr(app, "header_frame", None)
    print("header 属性:", [a for a in dir(app) if "head" in a.lower() or "logo" in a.lower()][:12])

    def walk(w, depth=0, limit=[0]):
        if limit[0] > 40:
            return
        for ch in w.winfo_children():
            cls = type(ch).__name__
            txt = ""
            try:
                txt = ch.cget("text")
            except Exception:
                pass
            if txt or cls in ("Label", "Frame"):
                try:
                    w_w, req_w = ch.winfo_width(), ch.winfo_reqwidth()
                    fnt = ch.cget("font") if cls == "Label" else ""
                    measured = tkfont.Font(font=fnt).measure(txt) if (txt and fnt) else ""
                    print(f"{'  '*depth}{cls}({ch.winfo_name()}) "
                          f"text={txt!r} 宽={w_w} 需宽={req_w} 文本长={measured} "
                          f"x={ch.winfo_x()} y={ch.winfo_y()}")
                    limit[0] += 1
                except Exception as e:
                    print(f"{'  '*depth}{cls} err={e}")
            walk(ch, depth + 1, limit)

    top = root.winfo_children()[0]
    print(f"顶层: {type(top).__name__}")
    walk(top)

    # 截图 + 左上角放大
    raw = "/tmp/_header_raw.png"
    subprocess.run(["import", "-window", "root", raw], check=True)
    from PIL import Image
    im = Image.open(raw)
    w = min(root.winfo_width(), im.width)
    h = min(max(root.winfo_height(), 880), im.height)
    full = im.crop((0, 0, w, h))
    full.save("/opt/llm-benchmark/docs/uiux-v2/AFTER/after_00_header_full.png")
    crop = full.crop((0, 0, 720, 74)).resize((720 * 2, 74 * 2), Image.LANCZOS)
    crop.save(OUT)
    print(f"\n顶栏放大图: {OUT}")
    print(f"整窗截图: /opt/llm-benchmark/docs/uiux-v2/AFTER/after_00_header_full.png")
    root.destroy()


if __name__ == "__main__":
    main()
