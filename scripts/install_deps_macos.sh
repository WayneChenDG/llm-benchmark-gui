#!/usr/bin/env bash
# install_deps_macos.sh — JISUMAN LLM Benchmark GUI macOS dependency installer
#
# Requires: Homebrew (https://brew.sh)
#
# Usage:
#   bash scripts/install_deps_macos.sh
#   bash scripts/install_deps_macos.sh --yes       # non-interactive
#   bash scripts/install_deps_macos.sh --no-venv   # skip venv creation
#   bash scripts/install_deps_macos.sh --no-system # skip brew installs
#
# DO NOT:
#   - Force-delete existing Python environments
#   - Bundle proprietary fonts
#   - Modify benchmark result directories

set -Eeuo pipefail

# ── Argument parsing ──────────────────────────────────────────────────────────
AUTO_YES=false
CREATE_VENV=true
INSTALL_SYSTEM=true

for arg in "$@"; do
    case "$arg" in
        --yes|-y)       AUTO_YES=true ;;
        --no-venv)      CREATE_VENV=false ;;
        --no-system)    INSTALL_SYSTEM=false ;;
        --help|-h)
            sed -n '2,20p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
    esac
done

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

echo "====================================================================="
echo "  JISUMAN LLM Benchmark GUI — macOS dependency installer"
echo "====================================================================="
echo "Project dir: $PROJECT_DIR"
echo ""

# ── Confirmation helper ───────────────────────────────────────────────────────
confirm() {
    if $AUTO_YES; then return 0; fi
    local prompt="${1:-Continue?} [y/N] "
    read -r -p "$prompt" reply
    [[ "${reply,,}" =~ ^y(es)?$ ]]
}

# ── System package installation (Homebrew) ────────────────────────────────────
if $INSTALL_SYSTEM; then
    echo "── Homebrew packages ────────────────────────────────────────────────"

    if ! command -v brew >/dev/null 2>&1; then
        echo "ERROR: Homebrew not found."
        echo "Install Homebrew from https://brew.sh then re-run this script."
        exit 1
    fi
    echo "  Homebrew found: $(brew --version | head -1)"
    echo ""
    echo "  Packages to install:"
    echo "    python@3.12  git"
    echo "    libreoffice  (cask — ~300 MB)"
    echo "    font-noto-sans-cjk-sc  (cask — CJK fonts for PDF/chart rendering)"
    echo ""
    confirm "Install via Homebrew?" || { echo "Skipping Homebrew packages."; INSTALL_SYSTEM=false; }

    if $INSTALL_SYSTEM; then
        brew install python@3.12 git

        echo ""
        echo "  Installing LibreOffice cask ..."
        brew install --cask libreoffice || {
            echo "  WARNING: LibreOffice cask install failed."
            echo "  DOCX->PDF conversion may be unavailable."
            echo "  Install manually from https://libreoffice.org"
        }

        echo ""
        echo "  Installing Noto CJK font cask ..."
        brew tap homebrew/cask-fonts 2>/dev/null || true
        brew install --cask font-noto-sans-cjk-sc || {
            echo "  WARNING: Noto CJK font cask install failed."
            echo "  CJK rendering in PDF/matplotlib may use fallback fonts."
        }
    fi
    echo ""
fi

# ── Verify Python ─────────────────────────────────────────────────────────────
PYTHON3=""
for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON3="$candidate"
        break
    fi
done

if [[ -z "$PYTHON3" ]]; then
    echo "ERROR: python3 not found. Please install Python 3.10+ and re-run."
    exit 1
fi
echo "Python: $($PYTHON3 --version) at $(command -v $PYTHON3)"

# ── tkinter check ─────────────────────────────────────────────────────────────
if ! $PYTHON3 -c "import tkinter" 2>/dev/null; then
    echo ""
    echo "WARNING: tkinter not available for $PYTHON3"
    echo "  If you installed Python via Homebrew, tkinter is included."
    echo "  If using system Python on macOS, install python-tk:"
    echo "    brew install python-tk@3.12"
fi

# ── Virtual environment ───────────────────────────────────────────────────────
if $CREATE_VENV; then
    echo ""
    echo "── Virtual environment ──────────────────────────────────────────────"
    if [[ -d ".venv" ]]; then
        echo "  .venv already exists — skipping creation"
    else
        echo "  Creating .venv ..."
        $PYTHON3 -m venv .venv
        echo "  .venv created"
    fi
    # shellcheck disable=SC1091
    source .venv/bin/activate
    echo "  Activated: $VIRTUAL_ENV"
fi

# ── Python dependencies ───────────────────────────────────────────────────────
echo ""
echo "── Python packages ──────────────────────────────────────────────────"
PYTHON_CMD="${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/python3}"
PYTHON_CMD="${PYTHON_CMD:-$PYTHON3}"

echo "  Upgrading pip / setuptools / wheel ..."
$PYTHON_CMD -m pip install -U pip setuptools wheel --quiet

if [[ -f "requirements.txt" ]]; then
    echo "  Installing requirements.txt ..."
    $PYTHON_CMD -m pip install -r requirements.txt
else
    echo "  WARNING: requirements.txt not found — installing known packages inline"
    $PYTHON_CMD -m pip install requests "aiohttp>=3.9" "Pillow>=9.0" \
        "matplotlib>=3.5" "python-docx>=1.1" "reportlab>=3.6" "pypdf>=3.0"
fi

# ── Optional: tokenizer calibration ──────────────────────────────────────────
echo ""
echo "── Optional: local tokenizer calibration ────────────────────────────"
echo "  Install transformers/sentencepiece/tiktoken for token-count calibration?"
echo "  (Large download ~500 MB+; safe to skip)"
if confirm "Install tokenizer deps?"; then
    if [[ -f "requirements-tokenizer.txt" ]]; then
        $PYTHON_CMD -m pip install -r requirements-tokenizer.txt
    else
        $PYTHON_CMD -m pip install transformers sentencepiece tiktoken
    fi
else
    echo "  Skipped."
fi

# ── Dependency verification ───────────────────────────────────────────────────
echo ""
echo "── Dependency verification ──────────────────────────────────────────"
$PYTHON_CMD scripts/verify_deps.py
VERIFY_EXIT=$?

echo ""
if [[ $VERIFY_EXIT -eq 0 ]]; then
    echo "====================================================================="
    echo "  DONE — all required dependencies installed successfully"
    if $CREATE_VENV; then
        echo ""
        echo "  To activate the virtual environment:"
        echo "    source .venv/bin/activate"
        echo ""
        echo "  To run the benchmark GUI:"
        echo "    python llm_benchmark.py"
    fi
    echo "====================================================================="
else
    echo "====================================================================="
    echo "  WARN — some dependencies are missing (see output above)"
    echo "====================================================================="
    exit $VERIFY_EXIT
fi
