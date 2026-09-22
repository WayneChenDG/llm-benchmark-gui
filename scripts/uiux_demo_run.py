#!/usr/bin/env python3
"""UI/UX v2 端到端演示驱动 —— 用真实 GUI 代码路径跑测试并逐阶段截图。

用途：验收「创建测试 → 观看进度 → 查看结果 → 失败原因 → 对比 → 导出」。
数据来源：本地 mock 端点（scripts/demo_mock_openai_server.py），属演示数据。

用法：
  xvfb-run -a -s "-screen 0 1920x1080x24" python3 scripts/uiux_demo_run.py \
      --out docs/uiux-v2/AFTER --prefix demo --url http://127.0.0.1:8123/v1 \
      --model demo-qwen3-32b-fp8

实现说明：Tkinter 的工作线程通过 root.after 回主线程更新界面，因此驱动必须运行在
真正的 mainloop 中（不能自己 update() 轮询，否则报 "main thread is not in main loop"）。
这里用 after 链构成状态机推进各阶段。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import tkinter as tk
from tkinter import messagebox

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import llm_benchmark as app_mod  # noqa: E402


class DemoDriver:
    def __init__(self, app, root, out: str, prefix: str):
        self.app = app
        self.root = root
        self.out = out
        self.prefix = prefix
        self.dialogs: list = []
        self.run_started_at = 0.0

    # ── 基础设施 ──
    def snap(self, name: str):
        path = os.path.join(self.out, f"{self.prefix}_{name}.png")
        raw = "/tmp/_uiux_demo_raw.png"
        subprocess.run(["import", "-window", "root", raw], check=True)
        from PIL import Image
        im = Image.open(raw)
        w = min(self.root.winfo_width(), im.width)
        h = min(max(self.root.winfo_height(), 880), im.height)
        im.crop((0, 0, w, h)).save(path)
        os.remove(raw)
        print(f"[demo] shot {os.path.basename(path)}", flush=True)

    def wait(self, cond, then, timeout=300.0, every=250):
        t0 = time.time()

        def poll():
            if cond() or (time.time() - t0) > timeout:
                then()
            else:
                self.root.after(every, poll)

        self.root.after(every, poll)

    # ── 阶段 ──
    def phase_new_test(self):
        app = self.app
        app.url_var.set(self.url)
        app.key_var.set("demo-key")
        app.model_var.set(self.model)
        app.system_var.set("你是一个有帮助的助手。")
        app.prompt_var.set("请用300字左右介绍机器学习。")
        app.output_length_mode_var.set("fixed")
        app._select_tab(1)
        self.root.after(900, self._after_new_test)

    def _after_new_test(self):
        self.snap("01_new_test_filled")
        self.start_run(preset="快速校准 — C1/N5", total=6, max_tokens=256,
                       temperature=0.0, stream=True, tag="runA",
                       marks=(1.0, 4.0))

    def start_run(self, *, preset, total, max_tokens, temperature, stream, tag,
                  marks=()):
        app = self.app
        app.concurrency_var.set(preset)
        app.total_var.set(str(total))
        app.max_tokens_var.set(int(max_tokens))
        try:
            app.temp_var.set(float(temperature))
        except Exception:
            pass
        app.stream_var.set("是" if stream else "否")
        app._start_benchmark()
        self.run_started_at = time.time()
        for m in marks:
            self.root.after(int(m * 1000), lambda mm=m, t=tag: self.snap(
                f"{t}_progress_{int(mm)}s"))
        self.wait(lambda: not app._benchmark_running,
                  lambda: self._run_done(tag), timeout=420)

    def _run_done(self, tag):
        print(f"[demo] {tag} finished", flush=True)
        self.root.after(1200, lambda: self.snap(f"{tag}_result"))
        if tag == "runA":
            self.root.after(2500, lambda: self.start_run(
                preset="标准基线 — C8/N80（默认）", total=40, max_tokens=256,
                temperature=0.0, stream=True, tag="runB", marks=(2.0,)))
        else:
            self.root.after(2500, self.phase_compare)

    def phase_compare(self):
        app = self.app
        app._select_tab(4)

        def do_compare():
            from llm_benchmark_app import ui_pages
            runs = getattr(app, "_cmp_runs", []) or []
            labels = [ui_pages.run_label(s) for s in runs]
            if len(labels) >= 2:
                app.cmp_var_a.set(labels[1])   # 较早一次为基准
                app.cmp_var_b.set(labels[0])
                ui_pages.run_compare(app)
            self.root.after(800, lambda: self.snap("05_history_compare"))

            def next_phase():
                self.phase_export()
            self.root.after(1800, next_phase)

        self.root.after(900, do_compare)

    def phase_export(self):
        self.app._select_tab(5)
        self.root.after(1200, lambda: self.snap("06_reports_evidence"))
        self.root.after(1800, self.phase_overview)

    def phase_overview(self):
        self.app._select_tab(0)
        self.root.after(1200, lambda: self.snap("00_overview_with_data"))
        self.root.after(1900, self.phase_final)

    def phase_final(self):
        self.app._select_tab(2)
        self.root.after(1000, lambda: self.snap("03_result_final"))
        self.root.after(1600, self.finish)

    def finish(self):
        print("[demo] 对话框记录：", flush=True)
        for kind, title, msg in self.dialogs:
            print(f"   - {kind}: {title} | {msg!r}", flush=True)
        self.root.quit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--prefix", default="demo")
    ap.add_argument("--url", default="http://127.0.0.1:8123/v1")
    ap.add_argument("--model", default="demo-qwen3-32b-fp8")
    args = ap.parse_args()

    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)

    root = tk.Tk()
    app = app_mod.LLMBenchmarkApp(root)
    drv = DemoDriver(app, root, out, args.prefix)
    drv.url, drv.model = args.url, args.model

    # 无人值守：自动应答对话框，并记录内容（用于验证失败原因提示是否够清楚）
    def showinfo(title=None, message=None, **kw):
        drv.dialogs.append(("info", title, str(message)[:300]))
        return "ok"

    def showerror(title=None, message=None, **kw):
        drv.dialogs.append(("error", title, str(message)[:300]))
        return "ok"

    def askyesno(title=None, message=None, **kw):
        drv.dialogs.append(("askyesno", title, str(message)[:300]))
        return True

    messagebox.showinfo = showinfo
    messagebox.showerror = showerror
    messagebox.askyesno = askyesno

    root.after(600, drv.phase_new_test)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
