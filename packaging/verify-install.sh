#!/usr/bin/env bash
# 安装后自检 —— JISUMEN LLM Benchmark GUI
#
# 用法：
#   bash verify-install.sh                       # 自动定位安装目录
#   bash verify-install.sh --install-dir /opt/xx
#   bash verify-install.sh --install-dir /opt/xx --smoke   # 额外做一次 GUI 冒烟启动
#
# 安装目录定位顺序（显式参数 → 启动器 → 默认路径），并打印实际来源。
set -uo pipefail

EXPLICIT_DIR=""
SMOKE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --install-dir) EXPLICIT_DIR="${2:?}"; shift 2 ;;
    --smoke) SMOKE=1; shift ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

resolve_dir() {
  if [ -n "$EXPLICIT_DIR" ]; then echo "$EXPLICIT_DIR|--install-dir"; return; fi
  if [ -x "$HOME/jisumen-llm-benchmark/bin/jisumen-benchmark" ]; then
    echo "$HOME/jisumen-llm-benchmark|启动器 (~/jisumen-llm-benchmark)"; return; fi
  if [ -f "$SELF/llm_benchmark.py" ]; then echo "$SELF|脚本所在目录（包内直跑）"; return; fi
  echo "|未找到"
}
IFS='|' read -r DIR SRCUSED <<< "$(resolve_dir)"
echo "==> 安装目录: ${DIR:-<未找到>}   [来源: $SRCUSED]"
[ -n "$DIR" ] || { echo "✗ 未找到安装目录，请用 --install-dir 指定"; exit 1; }

PASS=0; FAIL=0
ok()   { echo "  ✓ $1"; PASS=$((PASS+1)); }
bad()  { echo "  ✗ $1"; FAIL=$((FAIL+1)); }

# ── 1. 文件完整性 ─────────────────────────────────────────────────────────
[ -f "$DIR/llm_benchmark.py" ] && ok "主程序 llm_benchmark.py" || bad "缺少 llm_benchmark.py"
[ -d "$DIR/llm_benchmark_app" ] && ok "包目录 llm_benchmark_app/" || bad "缺少 llm_benchmark_app/"
[ -f "$DIR/VERSION" ] && ok "版本戳 VERSION（$(tr '\n' ' ' < "$DIR/VERSION" | sed 's/  */ /g')）" \
                      || bad "缺少 VERSION"
if [ -f "$DIR/VERSION" ]; then
  PV="$(sed -n 's/^version=//p' "$DIR/VERSION")"
  APV="$(grep -m1 '^APP_VERSION' "$DIR/llm_benchmark.py" | sed -E 's/.*"([^"]+)".*/\1/')"
  [ "$PV" = "$APV" ] && ok "版本一致（${APV}）" || bad "版本不一致：VERSION=${PV} APP_VERSION=${APV}"
fi
# SHA256SUMS 校验（包内清单存在时）
if [ -f "$DIR/SHA256SUMS" ]; then
  if ( cd "$DIR" && sha256sum -c --quiet SHA256SUMS ) 2>/dev/null; then
    ok "SHA256SUMS 全部校验通过"
  else
    bad "SHA256SUMS 校验失败（文件被改动或缺失）"
  fi
else
  echo "  · SHA256SUMS 不存在（解包后手动安装属正常）"
fi

# ── 2. Python 运行时 ──────────────────────────────────────────────────────
PY="$DIR/.venv/bin/python"
if [ -x "$PY" ]; then ok "venv Python ($("$PY" -V 2>&1))"
else PY="$(command -v python3)"; echo "  · 未使用 venv，退回系统 python3 ($("$PY" -V 2>&1))"; fi
"$PY" -c 'import tkinter' >/dev/null 2>&1 && ok "tkinter 可用" \
  || bad "tkinter 不可用（apt-get install python3-tk）"

for mod in requests aiohttp; do
  "$PY" -c "import $mod" >/dev/null 2>&1 && ok "依赖 $mod" || bad "依赖缺失: $mod"
done
for mod in matplotlib docx reportlab pypdf PIL; do
  if "$PY" -c "import $mod" >/dev/null 2>&1; then ok "可选依赖 $mod"; else echo "  · 可选依赖缺失: $mod（对应导出/图表功能降级）"; fi
done

# ── 3. 主程序可导入 ───────────────────────────────────────────────────────
( cd "$DIR" && "$PY" -m py_compile llm_benchmark.py llm_benchmark_app/*.py ) 2>/dev/null \
  && ok "py_compile 通过" || bad "py_compile 失败"
( cd "$DIR" && "$PY" -c 'import llm_benchmark as m; print(m.APP_VERSION)' >/dev/null 2>&1 ) \
  && ok "主程序可导入（APP_VERSION=$((cd "$DIR" && "$PY" -c 'import llm_benchmark as m;print(m.APP_VERSION)') ))" \
  || bad "主程序导入失败"

# ── 4. 可选：GUI 冒烟启动 ─────────────────────────────────────────────────
if [ "$SMOKE" = "1" ]; then
  LAUNCH="$DIR/bin/jisumen-benchmark"
  [ -x "$LAUNCH" ] || bad "启动器不存在: $LAUNCH"
  if [ -n "${DISPLAY:-}" ]; then
    "$LAUNCH" >/tmp/_verify_gui.log 2>&1 & PID=$!
  elif command -v xvfb-run >/dev/null 2>&1; then
    xvfb-run -a "$LAUNCH" >/tmp/_verify_gui.log 2>&1 & PID=$!
  else
    echo "  · 无 DISPLAY 且无 xvfb-run，跳过 GUI 冒烟"; PID=""
  fi
  if [ -n "$PID" ]; then
    sleep 12
    if kill -0 "$PID" 2>/dev/null; then
      ok "GUI 启动后 12s 仍在运行"
      kill "$PID" 2>/dev/null || true; sleep 1; pkill -f "llm_benchmark.py" 2>/dev/null || true
    else
      bad "GUI 启动后退出，日志尾部："; tail -5 /tmp/_verify_gui.log | sed 's/^/      /'
    fi
  fi
fi

echo
echo "==> 自检结果: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = "0" ] || exit 1
