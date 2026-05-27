#!/usr/bin/env bash
# install_deps_linux.sh — JISUMAN LLM Benchmark GUI Linux dependency installer
#
# Supports: Debian/Ubuntu (apt), Fedora/RHEL 8+ (dnf), CentOS/RHEL 7 (yum)
#
# Usage:
#   bash scripts/install_deps_linux.sh           # interactive (asks before sudo)
#   bash scripts/install_deps_linux.sh --yes      # non-interactive (CI/CD)
#   bash scripts/install_deps_linux.sh --no-venv  # skip venv creation
#   bash scripts/install_deps_linux.sh --no-system # skip system packages
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
echo "  JISUMAN LLM Benchmark GUI — Linux dependency installer"
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

# ── System package installation ───────────────────────────────────────────────
if $INSTALL_SYSTEM; then
    echo "── System packages ─────────────────────────────────────────────────"
    echo "The following system packages will be installed (requires sudo):"
    echo ""

    if command -v apt-get >/dev/null 2>&1; then
        PKG_MANAGER="apt"
        echo "  python3 python3-pip python3-venv python3-tk"
        echo "  git curl ca-certificates"
        echo "  libreoffice"
        echo "  fonts-noto-cjk fonts-wqy-microhei"
        echo "  xdg-utils"
        echo ""
        confirm "Install via apt-get?" || { echo "Skipping system packages."; INSTALL_SYSTEM=false; }
        if $INSTALL_SYSTEM; then
            sudo apt-get update -qq
            sudo apt-get install -y \
                python3 python3-pip python3-venv python3-tk \
                git curl ca-certificates \
                libreoffice \
                fonts-noto-cjk fonts-wqy-microhei \
                xdg-utils
        fi

    elif command -v dnf >/dev/null 2>&1; then
        PKG_MANAGER="dnf"
        echo "  python3 python3-pip python3-tkinter"
        echo "  git curl ca-certificates"
        echo "  libreoffice"
        echo "  google-noto-sans-cjk-fonts"
        echo "  xdg-utils"
        echo ""
        confirm "Install via dnf?" || { echo "Skipping system packages."; INSTALL_SYSTEM=false; }
        if $INSTALL_SYSTEM; then
            sudo dnf install -y \
                python3 python3-pip python3-tkinter \
                git curl ca-certificates \
                libreoffice \
                google-noto-sans-cjk-fonts \
                xdg-utils
        fi

    elif command -v yum >/dev/null 2>&1; then
        PKG_MANAGER="yum"
        echo "  python3 python3-pip tkinter"
        echo "  git curl ca-certificates"
        echo "  libreoffice"
        echo "  google-noto-sans-cjk-fonts"
        echo "  xdg-utils"
        echo ""
        confirm "Install via yum?" || { echo "Skipping system packages."; INSTALL_SYSTEM=false; }
        if $INSTALL_SYSTEM; then
            sudo yum install -y \
                python3 python3-pip tkinter \
                git curl ca-certificates \
                libreoffice \
                google-noto-sans-cjk-fonts \
                xdg-utils
        fi

    else
        echo "WARNING: No supported package manager (apt/dnf/yum) found."
        echo "Please install the following manually:"
        echo "  - Python 3.10+ with tkinter support"
        echo "  - LibreOffice (headless)"
        echo "  - CJK fonts (Noto CJK or WQY Microhei)"
        echo "  - xdg-utils"
        INSTALL_SYSTEM=false
    fi
    echo ""
fi

# ── Verify Python ─────────────────────────────────────────────────────────────
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found after system package install."
    echo "Please install Python 3.10+ and re-run this script."
    exit 1
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "Python $PYTHON_VERSION found: $(command -v python3)"

# ── Virtual environment ───────────────────────────────────────────────────────
if $CREATE_VENV; then
    echo ""
    echo "── Virtual environment ──────────────────────────────────────────────"
    if [[ -d ".venv" ]]; then
        echo "  .venv already exists — skipping creation"
    else
        echo "  Creating .venv ..."
        python3 -m venv .venv
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
PYTHON_CMD="${PYTHON_CMD:-python3}"
PIP_CMD="${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/pip}"
PIP_CMD="${PIP_CMD:-python3 -m pip}"

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

# ── Verify ────────────────────────────────────────────────────────────────────
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
