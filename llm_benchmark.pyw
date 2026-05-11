#!/usr/bin/env python3
"""Windows double-click launcher for LLM Benchmark (no console window).
On Windows, .pyw files run with pythonw.exe — no terminal popup.

Does NOT use import — executes llm_benchmark.py directly in-process
to avoid path/module resolution issues with pythonw.exe.
"""
import os
import sys
import traceback

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(_SCRIPT_DIR)

CRASH_LOG = os.path.join(_SCRIPT_DIR, "llm_benchmark_crash.log")

try:
    main_script = os.path.join(_SCRIPT_DIR, "llm_benchmark.py")
    with open(main_script, encoding="utf-8") as f:
        code = compile(f.read(), main_script, "exec")
    # Inject __name__ so the if __name__ == "__main__" block runs
    exec(code, {"__name__": "__main__"})
except Exception as e:
    detail = traceback.format_exc()
    # Try GUI error dialog first
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
    # Always write crash log
    with open(CRASH_LOG, "w", encoding="utf-8") as f:
        f.write(detail)
