#!/usr/bin/env python3
"""Windows double-click launcher for LLM Benchmark (no console window).
On Windows, .pyw files run with pythonw.exe — no terminal popup."""
import os
import sys

# Ensure the script directory is on the path and set as working directory
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(_SCRIPT_DIR)
sys.path.insert(0, _SCRIPT_DIR)

CRASH_LOG = os.path.join(_SCRIPT_DIR, "llm_benchmark_crash.log")

try:
    import llm_benchmark
    llm_benchmark.main()
except Exception as e:
    import traceback
    detail = traceback.format_exc()
    try:
        import tkinter.messagebox as mb
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        mb.showerror("LLM Benchmark 启动失败",
                     f"程序启动时发生错误:\n\n{type(e).__name__}: {e}\n\n"
                     f"详细信息已写入 llm_benchmark_crash.log")
        root.destroy()
    except Exception:
        pass
    with open(CRASH_LOG, "w", encoding="utf-8") as f:
        f.write(detail)
