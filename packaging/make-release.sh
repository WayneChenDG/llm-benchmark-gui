#!/usr/bin/env bash
# 构建发行包 —— JISUMEN LLM Benchmark GUI
#
# 用法：
#   bash packaging/make-release.sh [--no-wheels]
#
# 产出：
#   dist/jisumen-llm-benchmark-<version>-linux-<arch>.tar.gz
#   dist/jisumen-llm-benchmark-<version>-linux-<arch>.tar.gz.sha256
#
# 包内布局（解包即可用，无需联网）：
#   jisumen-llm-benchmark-<ver>/          应用 + VERSION + SHA256SUMS
#     ├── install.sh                      安装（离线优先：优先用 wheels/）
#     ├── verify-install.sh               安装后自检
#     ├── wheels/py<X.Y>-<arch>/          预取 wheels（离线安装用）
#     └── ...（应用文件、docs、tests）
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

WITH_WHEELS=1
for arg in "$@"; do
  case "$arg" in
    --no-wheels) WITH_WHEELS=0 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

VERSION="$(grep -m1 '^APP_VERSION' llm_benchmark.py | sed -E 's/.*"([^"]+)".*/\1/')"
[ -n "$VERSION" ] || { echo "无法从 llm_benchmark.py 读取 APP_VERSION" >&2; exit 1; }
ARCH="$(uname -m)"
PYVER="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
GIT_REV="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
BUILT_AT="$(date -Iseconds)"
NAME="jisumen-llm-benchmark-${VERSION}-linux-${ARCH}"
[ "$WITH_WHEELS" = "1" ] || NAME="${NAME}-slim"

DIST="$REPO/dist"
STAGE="$DIST/_stage/$NAME"
rm -rf "$STAGE"
mkdir -p "$STAGE"

echo "==> 版本 ${VERSION} / ${ARCH} / py${PYVER} / git ${GIT_REV}"

# ── 1. 拷贝应用负载（排除开发/运行时产物）──────────────────────────────────
if command -v rsync >/dev/null 2>&1; then
  rsync -a \
    --exclude '.git' --exclude 'dist' --exclude 'backups' --exclude 'results' \
    --exclude 'data' --exclude '*.db' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude '.agents' --exclude 'design-system' --exclude '.pytest_cache' \
    --exclude 'llm_benchmark.ini' --exclude 'llm_benchmark_history.db' \
    --exclude 'docs/uiux-v2/BEFORE' --exclude 'docs/uiux-v2/AFTER' \
    --exclude 'docs/uiux-v2/COMPARE' --exclude 'docs/uiux-v2/theme' \
    --exclude 'scripts/_probe_*.py' \
    ./ "$STAGE"/
else
  tar --exclude='./.git' --exclude='./dist' --exclude='./backups' --exclude='./results' \
      --exclude='./data' --exclude='*.db' --exclude='__pycache__' --exclude='*.pyc' \
      --exclude='./.agents' --exclude='./design-system' \
      --exclude='./docs/uiux-v2/BEFORE' --exclude='./docs/uiux-v2/AFTER' \
      --exclude='./docs/uiux-v2/COMPARE' --exclude='./docs/uiux-v2/theme' \
      --exclude='./scripts/_probe_*.py' -cf - . | tar -xf - -C "$STAGE"
fi
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# ── 2. 预取 wheels（离线安装）──────────────────────────────────────────────
if [ "$WITH_WHEELS" = "1" ]; then
  WD="$STAGE/wheels/py${PYVER}-${ARCH}"
  mkdir -p "$WD"
  echo "==> 预取 wheels → wheels/py${PYVER}-${ARCH}/"
  python3 -m pip download --only-binary=:all: --python-version "$PYVER" \
      --implementation cp --abi "cp${PYVER//./}" -r requirements.txt -d "$WD" \
      >/dev/null || python3 -m pip download --only-binary=:all: -r requirements.txt -d "$WD"
  if [ -f requirements-tokenizer.txt ]; then
    WD2="$STAGE/wheels/py${PYVER}-${ARCH}-tokenizer"
    mkdir -p "$WD2"
    python3 -m pip download --only-binary=:all: -r requirements-tokenizer.txt -d "$WD2" \
      >/dev/null 2>&1 || { rmdir "$WD2"; echo "    （tokenizer 依赖预取跳过）"; }
  fi
else
  echo "==> 跳过 wheels（--no-wheels）"
fi

# ── 3. 安装器 / 自检脚本 / VERSION / 校验和 ────────────────────────────────
install -m 0755 packaging/install.sh       "$STAGE/install.sh"
install -m 0755 packaging/verify-install.sh "$STAGE/verify-install.sh"
cat > "$STAGE/VERSION" <<EOF
version=${VERSION}
arch=${ARCH}
python=${PYVER}
git_rev=${GIT_REV}
built_at=${BUILT_AT}
builder=packaging/make-release.sh
EOF

( cd "$STAGE" && find . -type f ! -name 'SHA256SUMS' -print0 \
    | sort -z | xargs -0 sha256sum > SHA256SUMS )

# ── 4. 打包 ────────────────────────────────────────────────────────────────
mkdir -p "$DIST"
TARBALL="$DIST/${NAME}.tar.gz"
rm -f "$TARBALL"
tar -czf "$TARBALL" -C "$DIST/_stage" "$NAME"
sha256sum "$TARBALL" | awk '{print $1"  '"${NAME}.tar.gz"'"}' > "${TARBALL}.sha256"
rm -rf "$DIST/_stage"

SIZE="$(du -h "$TARBALL" | cut -f1)"
echo
echo "==> 发行包: $TARBALL  (${SIZE})"
echo "==> 校验和: $(cat "${TARBALL}.sha256")"
echo "==> 包内文件数: $(tar tzf "$TARBALL" | grep -vc '/$')"
