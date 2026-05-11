#!/usr/bin/env python3
"""Windows double-click launcher for LLM Benchmark (no console window).
On Windows, .pyw files run with pythonw.exe — no terminal popup."""
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
    with open("llm_benchmark_crash.log", "w", encoding="utf-8") as f:
        f.write(detail)
