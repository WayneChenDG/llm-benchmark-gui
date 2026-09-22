#!/usr/bin/env bash
# 安装器 —— JISUMEN LLM Benchmark GUI（离线优先）
#
# 用法：
#   bash install.sh                          # 默认装到 ~/jisumen-llm-benchmark
#   bash install.sh --install-dir /opt/xxx   # 指定目录
#   bash install.sh --no-venv                # 用系统 Python（依赖需已装好）
#
# 说明：
#   * 本工具是桌面 GUI 客户端，不装系统服务、不占端口、不改系统配置；
#   * 依赖优先从包内 wheels/py<X.Y>-<arch>/ 离线安装，缺失时才联网 PyPI；
#   * 结束时生成启动器 <install-dir>/bin/jisumen-benchmark。
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/jisumen-llm-benchmark"
USE_VENV=1
while [ $# -gt 0 ]; do
  case "$1" in
    --install-dir) INSTALL_DIR="${2:?--install-dir 需要参数}"; shift 2 ;;
    --no-venv)     USE_VENV=0; shift ;;
    -h|--help)     sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "✗ 未找到 python3，请先安装 Python 3.9+" >&2; exit 1; }
PYVER="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
ARCH="$(uname -m)"

echo "==> 源目录   : $SRC"
echo "==> 安装目录 : $INSTALL_DIR"
echo "==> Python   : $PY ($PYVER / $ARCH)"

# ── 1. GUI 运行时的系统前置：tkinter ───────────────────────────────────────
if ! "$PY" -c 'import tkinter' >/dev/null 2>&1; then
  echo "✗ 缺少 tkinter（GUI 必需）。请安装后重试："
  echo "    Debian/Ubuntu : sudo apt-get install -y python3-tk"
  echo "    RHEL/CentOS   : sudo dnf install -y python3-tkinter"
  echo "    macOS         : brew install python-tk"
  exit 1
fi
echo "==> tkinter   : OK"

# ── 2. 拷贝应用文件 ────────────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR"
if command -v rsync >/dev/null 2>&1; then
  rsync -a --exclude 'wheels' --exclude 'SHA256SUMS' --exclude 'install.sh' \
        --exclude 'verify-install.sh' \
        "$SRC"/ "$INSTALL_DIR"/
else
  tar --exclude='./wheels' --exclude='./SHA256SUMS' --exclude='./install.sh' \
      --exclude='./verify-install.sh' -cf - -C "$SRC" . \
    | tar -xf - -C "$INSTALL_DIR"
fi
echo "==> 应用文件 : 已拷贝 $(find "$INSTALL_DIR" -type f ! -path '*/bin/*' | wc -l) 个文件"

# ── 3. Python 依赖（离线优先）─────────────────────────────────────────────
WHEELS="$SRC/wheels/py${PYVER}-${ARCH}"
if [ "$USE_VENV" = "1" ]; then
  VENV="$INSTALL_DIR/.venv"
  echo "==> 创建 venv: $VENV"
  "$PY" -m venv "$VENV" 2>/dev/null || {
    echo "✗ venv 创建失败（可能需要 python3-venv）。可改用 --no-venv" >&2; exit 1; }
  PIP="$VENV/bin/pip"; RUNPY="$VENV/bin/python"
else
  PIP="$PY -m pip"; RUNPY="$PY"
fi

if [ -d "$WHEELS" ]; then
  echo "==> 依赖安装 : 离线（$WHEELS）"
  $PIP install --quiet --no-index --find-links "$WHEELS" -r "$INSTALL_DIR/requirements.txt" \
    || { echo "✗ 离线安装失败：该包可能不含 py${PYVER}/${ARCH} 的 wheels。" >&2
         echo "  请改用联网安装（重跑本脚本时删掉 wheels 目录，或直接 pip install -r requirements.txt）" >&2
         exit 1; }
else
  echo "==> 依赖安装 : 联网 PyPI（包内无 wheels/py${PYVER}-${ARCH}）"
  $PIP install --quiet -r "$INSTALL_DIR/requirements.txt"
fi
echo "==> 依赖检查 :"
$RUNPY "$INSTALL_DIR/scripts/verify_deps.py" 2>/dev/null || true

# ── 4. 启动器 ─────────────────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR/bin"
cat > "$INSTALL_DIR/bin/jisumen-benchmark" <<EOF
#!/usr/bin/env bash
# 启动 JISUMEN LLM Benchmark GUI（由 install.sh 生成）
cd "$INSTALL_DIR"
exec "$RUNPY" llm_benchmark.py "\$@"
EOF
chmod 0755 "$INSTALL_DIR/bin/jisumen-benchmark"

echo
echo "==> 安装完成"
echo "    启动    : $INSTALL_DIR/bin/jisumen-benchmark"
echo "    自检    : bash $SRC/verify-install.sh --install-dir $INSTALL_DIR"
