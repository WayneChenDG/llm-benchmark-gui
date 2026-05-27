# install_deps_windows.ps1 — JISUMAN LLM Benchmark GUI Windows dependency installer
#
# Usage:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\scripts\install_deps_windows.ps1
#   .\scripts\install_deps_windows.ps1 -Yes          # non-interactive
#   .\scripts\install_deps_windows.ps1 -NoVenv        # skip venv creation
#   .\scripts\install_deps_windows.ps1 -NoSystem      # skip winget installs
#
# Requires: Windows 10 1903+ (winget available) or manual pre-install of Python/LibreOffice
#
# DO NOT:
#   - Force-delete existing Python environments
#   - Bundle proprietary fonts
#   - Modify benchmark result directories

param(
    [switch]$Yes,       # Non-interactive: skip all confirmation prompts
    [switch]$NoVenv,    # Skip virtual environment creation
    [switch]$NoSystem   # Skip winget package installation
)

$ErrorActionPreference = "Stop"

# ── Paths ─────────────────────────────────────────────────────────────────────
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
Set-Location $ProjectDir

Write-Host "=====================================================================" -ForegroundColor Cyan
Write-Host "  JISUMAN LLM Benchmark GUI - Windows dependency installer" -ForegroundColor Cyan
Write-Host "=====================================================================" -ForegroundColor Cyan
Write-Host "Project dir: $ProjectDir"
Write-Host ""

# ── Confirmation helper ───────────────────────────────────────────────────────
function Confirm-Action([string]$Prompt) {
    if ($Yes) { return $true }
    $reply = Read-Host "$Prompt [y/N]"
    return ($reply -match '^[Yy]')
}

# ── System package installation (winget) ──────────────────────────────────────
if (-not $NoSystem) {
    Write-Host "-- System packages (winget) -----------------------------------------"

    # Python
    $pythonOk = $null -ne (Get-Command python -ErrorAction SilentlyContinue)
    if (-not $pythonOk) {
        Write-Host "Python not found."
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            if (Confirm-Action "Install Python 3.12 via winget?") {
                winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
                # Refresh PATH in this session
                $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" +
                             [System.Environment]::GetEnvironmentVariable("PATH", "User")
            } else {
                Write-Warning "Python not installed. Please install Python 3.10+ manually and re-run."
            }
        } else {
            Write-Warning "winget not available. Please install Python 3.10+ from https://python.org and re-run."
        }
    } else {
        $pyVer = (python --version 2>&1)
        Write-Host "  OK        Python: $pyVer"
    }

    # LibreOffice
    $sofficeOk = ($null -ne (Get-Command soffice -ErrorAction SilentlyContinue)) -or
                  ($null -ne (Get-Command libreoffice -ErrorAction SilentlyContinue)) -or
                  (Test-Path "C:\Program Files\LibreOffice\program\soffice.exe")
    if (-not $sofficeOk) {
        Write-Host "LibreOffice not found (needed for DOCX -> PDF conversion)."
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            if (Confirm-Action "Install LibreOffice via winget?") {
                winget install -e --id TheDocumentFoundation.LibreOffice --accept-package-agreements --accept-source-agreements
            } else {
                Write-Warning "LibreOffice not installed. DOCX->PDF conversion will be unavailable."
            }
        } else {
            Write-Warning "winget not available. Install LibreOffice from https://libreoffice.org"
        }
    } else {
        Write-Host "  OK        LibreOffice found"
    }

    # CJK font check (Windows ships with limited CJK coverage; Noto CJK not on winget)
    Write-Host ""
    Write-Host "  NOTE: For correct Chinese character rendering in PDF reports," -ForegroundColor Yellow
    Write-Host "        ensure a CJK font is installed (e.g. Noto Sans CJK, Microsoft YaHei," -ForegroundColor Yellow
    Write-Host "        or SimHei). Windows 10/11 typically includes basic CJK support." -ForegroundColor Yellow
    Write-Host ""
}

# ── Verify Python exists ──────────────────────────────────────────────────────
$pythonCmd = $null
foreach ($candidate in @("python", "python3", "py")) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) {
        $pythonCmd = $candidate
        break
    }
}
if (-not $pythonCmd) {
    throw "Python not found. Please install Python 3.10+ and add it to PATH."
}
$pyVerStr = (& $pythonCmd --version 2>&1)
Write-Host "Using: $pythonCmd ($pyVerStr)"

# ── Virtual environment ───────────────────────────────────────────────────────
$venvPython = $null
$venvPip    = $null

if (-not $NoVenv) {
    Write-Host ""
    Write-Host "-- Virtual environment -----------------------------------------------"
    if (Test-Path ".venv") {
        Write-Host "  .venv already exists - skipping creation"
    } else {
        Write-Host "  Creating .venv ..."
        & $pythonCmd -m venv .venv
        Write-Host "  .venv created"
    }
    $venvPython = ".\.venv\Scripts\python.exe"
    $venvPip    = ".\.venv\Scripts\pip.exe"
    Write-Host "  Venv Python: $venvPython"
} else {
    $venvPython = $pythonCmd
    $venvPip    = "$pythonCmd -m pip"
}

# ── Python dependencies ───────────────────────────────────────────────────────
Write-Host ""
Write-Host "-- Python packages ---------------------------------------------------"
Write-Host "  Upgrading pip / setuptools / wheel ..."
& $venvPython -m pip install -U pip setuptools wheel --quiet

if (Test-Path "requirements.txt") {
    Write-Host "  Installing requirements.txt ..."
    & $venvPython -m pip install -r requirements.txt
} else {
    Write-Warning "requirements.txt not found - installing known packages inline"
    & $venvPython -m pip install requests "aiohttp>=3.9" "Pillow>=9.0" `
        "matplotlib>=3.5" "python-docx>=1.1" "reportlab>=3.6" "pypdf>=3.0"
}

# ── Optional: tokenizer calibration ──────────────────────────────────────────
Write-Host ""
Write-Host "-- Optional: local tokenizer calibration ----------------------------"
Write-Host "  Install transformers/sentencepiece/tiktoken for token-count calibration?"
Write-Host "  (Large download ~500 MB+; safe to skip)"
if (Confirm-Action "Install tokenizer deps?") {
    if (Test-Path "requirements-tokenizer.txt") {
        & $venvPython -m pip install -r requirements-tokenizer.txt
    } else {
        & $venvPython -m pip install transformers sentencepiece tiktoken
    }
} else {
    Write-Host "  Skipped."
}

# ── Dependency verification ───────────────────────────────────────────────────
Write-Host ""
Write-Host "-- Dependency verification -------------------------------------------"
& $venvPython scripts\verify_deps.py
$verifyExit = $LASTEXITCODE

Write-Host ""
if ($verifyExit -eq 0) {
    Write-Host "=====================================================================" -ForegroundColor Green
    Write-Host "  DONE - all required dependencies installed successfully" -ForegroundColor Green
    if (-not $NoVenv) {
        Write-Host ""
        Write-Host "  To activate the virtual environment in a new shell:" -ForegroundColor Cyan
        Write-Host "    .\.venv\Scripts\Activate.ps1"
        Write-Host ""
        Write-Host "  To run the benchmark GUI:" -ForegroundColor Cyan
        Write-Host "    python llm_benchmark.py"
    }
    Write-Host "=====================================================================" -ForegroundColor Green
} else {
    Write-Warning "Some dependencies are missing (see output above)."
    Write-Host "Run:  $venvPython -m pip install -r requirements.txt"
    exit $verifyExit
}
