#!/usr/bin/env python3
"""install_deps.py — Cross-platform dependency installer for JISUMAN LLM Benchmark GUI.

Detects the current platform and delegates to the appropriate native installer,
or performs a Python-level pip install when running on an unsupported platform.

Usage:
  python scripts/install_deps.py              # interactive
  python scripts/install_deps.py --yes        # non-interactive (CI/CD)
  python scripts/install_deps.py --no-venv    # skip venv creation
  python scripts/install_deps.py --no-system  # skip system packages (pip only)
  python scripts/install_deps.py --verify-only # run verify_deps.py and exit

Platform dispatch:
  Linux   → bash scripts/install_deps_linux.sh
  Windows → powershell scripts/install_deps_windows.ps1
  macOS   → bash scripts/install_deps_macos.sh
  Other   → pip install -r requirements.txt (fallback)

DO NOT:
  - Force-delete existing Python environments
  - Bundle proprietary fonts
  - Modify benchmark result directories
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).resolve().parent
PROJECT_DIR  = SCRIPT_DIR.parent
REQUIREMENTS = PROJECT_DIR / "requirements.txt"
REQUIREMENTS_TOKENIZER = PROJECT_DIR / "requirements-tokenizer.txt"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _banner(msg: str) -> None:
    line = "=" * 68
    print(f"\n{line}\n  {msg}\n{line}")


def _run(cmd: list[str], check: bool = True) -> int:
    """Run a command, print it, and return the exit code."""
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd)
    if check and result.returncode != 0:
        raise SystemExit(f"\nCommand failed with exit code {result.returncode}: {cmd[0]}")
    return result.returncode


def _confirm(prompt: str, auto_yes: bool) -> bool:
    if auto_yes:
        return True
    reply = input(f"{prompt} [y/N] ").strip().lower()
    return reply in {"y", "yes"}


def _python_exe() -> str:
    """Return path to current interpreter (venv-aware)."""
    return sys.executable


def _pip_install(packages_or_req: str | list[str], quiet: bool = False) -> None:
    """pip install one requirements file or a list of package specs."""
    cmd = [_python_exe(), "-m", "pip", "install"]
    if quiet:
        cmd.append("--quiet")
    if isinstance(packages_or_req, str):
        cmd += ["-r", packages_or_req]
    else:
        cmd += packages_or_req
    _run(cmd)


# ── Fallback pure-Python installer ───────────────────────────────────────────

def _install_pip_fallback(args: argparse.Namespace) -> int:
    """Fallback install path: pip only, no system packages."""
    print("\n── Upgrading pip / setuptools / wheel ───────────────────────────────")
    _run([_python_exe(), "-m", "pip", "install", "-U",
          "pip", "setuptools", "wheel", "--quiet"])

    print("\n── Installing Python dependencies ───────────────────────────────────")
    if REQUIREMENTS.exists():
        _pip_install(str(REQUIREMENTS))
    else:
        print("  WARNING: requirements.txt not found — installing known packages")
        _pip_install([
            "requests", "aiohttp>=3.9", "Pillow>=9.0",
            "matplotlib>=3.5", "python-docx>=1.1",
            "reportlab>=3.6", "pypdf>=3.0",
        ])

    if not args.no_tokenizer:
        print("\n── Optional: tokenizer calibration ──────────────────────────────────")
        if _confirm("Install transformers/sentencepiece/tiktoken (~500 MB+)?", args.yes):
            if REQUIREMENTS_TOKENIZER.exists():
                _pip_install(str(REQUIREMENTS_TOKENIZER))
            else:
                _pip_install(["transformers", "sentencepiece", "tiktoken"])
        else:
            print("  Skipped.")

    return 0


# ── Platform dispatch ─────────────────────────────────────────────────────────

def _build_extra_flags(args: argparse.Namespace) -> list[str]:
    flags: list[str] = []
    if args.yes:
        flags.append("--yes")
    if args.no_venv:
        flags.append("--no-venv")
    if args.no_system:
        flags.append("--no-system")
    return flags


def _install_linux(args: argparse.Namespace) -> int:
    script = SCRIPT_DIR / "install_deps_linux.sh"
    if not script.exists():
        print(f"  WARNING: {script} not found — falling back to pip-only install")
        return _install_pip_fallback(args)
    flags = _build_extra_flags(args)
    return _run(["bash", str(script)] + flags, check=False)


def _install_macos(args: argparse.Namespace) -> int:
    script = SCRIPT_DIR / "install_deps_macos.sh"
    if not script.exists():
        print(f"  WARNING: {script} not found — falling back to pip-only install")
        return _install_pip_fallback(args)
    flags = _build_extra_flags(args)
    return _run(["bash", str(script)] + flags, check=False)


def _install_windows(args: argparse.Namespace) -> int:
    script = SCRIPT_DIR / "install_deps_windows.ps1"
    if not script.exists():
        print(f"  WARNING: {script} not found — falling back to pip-only install")
        return _install_pip_fallback(args)
    # Build PowerShell flag equivalents
    ps_flags: list[str] = []
    if args.yes:
        ps_flags.append("-Yes")
    if args.no_venv:
        ps_flags.append("-NoVenv")
    if args.no_system:
        ps_flags.append("-NoSystem")
    return _run(
        ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)] + ps_flags,
        check=False,
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Non-interactive: skip all confirmation prompts")
    parser.add_argument("--no-venv", action="store_true",
                        help="Skip virtual environment creation")
    parser.add_argument("--no-system", action="store_true",
                        help="Skip system package installation (pip only)")
    parser.add_argument("--no-tokenizer", action="store_true",
                        help="Skip tokenizer calibration prompt")
    parser.add_argument("--verify-only", action="store_true",
                        help="Run verify_deps.py and exit (no install)")
    args = parser.parse_args()

    # Change to project root so relative paths work
    os.chdir(PROJECT_DIR)

    _banner("JISUMAN LLM Benchmark GUI — dependency installer")
    print(f"  Platform:    {sys.platform}")
    print(f"  Python:      {sys.version}")
    print(f"  Executable:  {sys.executable}")
    print(f"  Project dir: {PROJECT_DIR}")

    # ── Verify-only mode ──────────────────────────────────────────────────────
    if args.verify_only:
        verify_script = SCRIPT_DIR / "verify_deps.py"
        return _run([_python_exe(), str(verify_script)], check=False)

    # ── Platform dispatch ─────────────────────────────────────────────────────
    platform = sys.platform

    if platform.startswith("linux"):
        rc = _install_linux(args)
    elif platform == "darwin":
        rc = _install_macos(args)
    elif platform.startswith("win"):
        rc = _install_windows(args)
    else:
        print(f"\n  Unsupported platform '{platform}' — performing pip-only install")
        rc = _install_pip_fallback(args)

    # ── Run verify (if not already run by the shell script) ──────────────────
    if rc == 0 and not platform.startswith("linux") \
            and not platform == "darwin" \
            and not platform.startswith("win"):
        # Shell scripts run verify internally; only run it here for fallback path
        verify_script = SCRIPT_DIR / "verify_deps.py"
        rc = _run([_python_exe(), str(verify_script)], check=False)

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
