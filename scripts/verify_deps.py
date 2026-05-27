#!/usr/bin/env python3
"""verify_deps.py — Dependency verification for JISUMAN LLM Benchmark GUI.

Checks Python modules, external commands, and local font directory.
Exit code 0 = all required deps present; 1 = one or more missing.

Usage:
  python scripts/verify_deps.py
  python scripts/verify_deps.py --strict    # treat optional as required too
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
from pathlib import Path

# ── Module lists ──────────────────────────────────────────────────────────────

# Required: missing any of these will prevent a feature from working at all.
REQUIRED_MODULES: list[tuple[str, str]] = [
    ("tkinter",    "GUI framework (system package: python3-tk / python3-tkinter)"),
    ("requests",   "HTTP client for single-run benchmark (pip install requests)"),
    ("aiohttp",    "Async HTTP for high-concurrency sweep (pip install aiohttp)"),
]

# Recommended: degraded experience without these but core benchmark still runs.
RECOMMENDED_MODULES: list[tuple[str, str]] = [
    ("matplotlib", "Sweep charts and E2E histogram PNG (pip install matplotlib)"),
    ("PIL",        "Logo display and image thumbnails (pip install Pillow)"),
    ("docx",       "Customer DOCX report generation (pip install python-docx)"),
    ("reportlab",  "PDF rendering fallback (pip install reportlab)"),
    ("pypdf",      "PDF page operations (pip install pypdf)"),
]

# Optional: heavier or situational dependencies.
OPTIONAL_MODULES: list[tuple[str, str]] = [
    ("fitz",         "Faster PDF rendering — PyMuPDF (pip install PyMuPDF)"),
    ("transformers", "Local tokenizer calibration (pip install transformers)"),
    ("sentencepiece","BPE tokenizer support (pip install sentencepiece)"),
    ("tiktoken",     "OpenAI-style tokenizer (pip install tiktoken)"),
]

# ── Command list ──────────────────────────────────────────────────────────────

OPTIONAL_COMMANDS: list[tuple[str, str]] = [
    ("soffice",     "LibreOffice headless — DOCX→PDF conversion"),
    ("libreoffice", "LibreOffice headless — alternate binary name"),
    ("xdg-open",    "Open results directory on Linux"),
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _check_module(name: str, description: str, level: str) -> bool:
    """Return True if importable."""
    ok = importlib.util.find_spec(name) is not None
    tag = "OK      " if ok else f"{level.upper():8s}"
    print(f"  {tag}  {name:<20s}  {description}")
    return ok


def _check_cmd(name: str, description: str) -> bool:
    path = shutil.which(name)
    ok = path is not None
    tag = "OK      " if ok else "MISSING "
    detail = f"({path})" if path else "(not in PATH)"
    print(f"  {tag}  {name:<20s}  {description}  {detail}")
    return ok


# ── Main ──────────────────────────────────────────────────────────────────────

def main(strict: bool = False) -> int:
    print("=" * 68)
    print("  JISUMAN LLM Benchmark GUI — dependency check")
    print("=" * 68)

    print(f"\nPython {sys.version}")
    print(f"Executable: {sys.executable}")

    all_ok = True
    warnings: list[str] = []

    # ── Required modules ──────────────────────────────────────────────────────
    print("\n── Required modules ─────────────────────────────────────────────")
    for name, desc in REQUIRED_MODULES:
        if not _check_module(name, desc, "MISSING"):
            all_ok = False

    # ── Recommended modules ───────────────────────────────────────────────────
    print("\n── Recommended modules (degraded without these) ─────────────────")
    for name, desc in RECOMMENDED_MODULES:
        ok = _check_module(name, desc, "WARNING")
        if not ok:
            warnings.append(f"pip install {_pip_name(name)}")

    # ── Optional modules ──────────────────────────────────────────────────────
    print("\n── Optional modules ─────────────────────────────────────────────")
    for name, desc in OPTIONAL_MODULES:
        ok = _check_module(name, desc, "OPTIONAL")
        if strict and not ok:
            warnings.append(f"pip install {_pip_name(name)}")

    # ── External commands ─────────────────────────────────────────────────────
    print("\n── External commands ────────────────────────────────────────────")
    soffice_ok = any(
        _check_cmd(cmd, desc)
        for cmd, desc in OPTIONAL_COMMANDS
        if "soffice" in cmd or "libreoffice" in cmd
    )
    _check_cmd("xdg-open", "Open results directory on Linux")

    if not soffice_ok:
        warnings.append(
            "LibreOffice not found — DOCX→PDF conversion unavailable.\n"
            "    Install: sudo apt-get install libreoffice  (Linux)\n"
            "             winget install TheDocumentFoundation.LibreOffice  (Windows)\n"
            "             brew install --cask libreoffice  (macOS)"
        )

    # ── Local fonts directory ─────────────────────────────────────────────────
    print("\n── Local fonts directory ────────────────────────────────────────")
    # Resolve relative to this script's parent (project root)
    project_root = Path(__file__).resolve().parent.parent
    fonts_dir = project_root / "fonts"
    print(f"  Checking: {fonts_dir}")
    if fonts_dir.is_dir():
        font_files = [
            p for p in fonts_dir.iterdir()
            if p.suffix.lower() in {".ttf", ".ttc", ".otf"}
        ]
        if font_files:
            for p in sorted(font_files):
                print(f"  FONT      {p.name}")
        else:
            print("  INFO      fonts/ directory exists but contains no TTF/TTC/OTF files")
    else:
        print("  INFO      No local fonts/ directory — system fonts will be used")
        print("            CJK rendering in PDF/matplotlib requires CJK fonts installed")
        print("            on the system (fonts-noto-cjk / fonts-wqy-microhei).")

    # ── aiohttp version check ─────────────────────────────────────────────────
    print("\n── Runtime version notes ────────────────────────────────────────")
    try:
        import aiohttp
        print(f"  aiohttp {aiohttp.__version__}")
    except ImportError:
        pass
    try:
        import matplotlib
        print(f"  matplotlib {matplotlib.__version__}")
    except ImportError:
        pass

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 68)
    if warnings:
        print("Warnings / install suggestions:")
        for w in warnings:
            print(f"  • {w}")
        print()

    if all_ok:
        print("VERIFY_DEPS_PASS  (all required dependencies found)")
        return 0
    else:
        print("VERIFY_DEPS_FAIL  (one or more REQUIRED dependencies missing)")
        print("Run:  pip install -r requirements.txt")
        return 1


def _pip_name(module_name: str) -> str:
    """Map import name → pip package name for common renames."""
    _MAP = {
        "PIL":          "Pillow",
        "docx":         "python-docx",
        "fitz":         "PyMuPDF",
        "sentencepiece":"sentencepiece",
    }
    return _MAP.get(module_name, module_name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict", action="store_true",
        help="Treat optional modules as required (exit 1 if any optional is missing)")
    args = parser.parse_args()
    raise SystemExit(main(strict=args.strict))
