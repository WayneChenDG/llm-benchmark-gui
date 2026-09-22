#!/usr/bin/env bash
# 发行包验收 —— 在隔离目录安装并跑真实负载，不触碰本机任何运行中的服务
#
# 用法：
#   bash packaging/accept-release.sh <tarball> [mock_base_url]
#   例: bash packaging/accept-release.sh dist/jisumen-llm-benchmark-2.0.0-linux-x86_64.tar.gz \
#           http://127.0.0.1:8123/v1
#
# 覆盖：包内不得含站点配置/运行时产物 → sha256 校验 → 离线安装 → 自检 → GUI 冒烟
#       → 用安装副本跑一次真实 C1/N2 基准（走 OpenAI 兼容端点）
set -uo pipefail

TARBALL="${1:?用法: bash packaging/accept-release.sh <tarball> [mock_base_url]}"
URL="${2:-http://127.0.0.1:8123/v1/chat/completions}"
TARBALL="$(cd "$(dirname "$TARBALL")" && pwd)/$(basename "$TARBALL")"
WORK="$(mktemp -d /tmp/acc-release-XXXXXX)"
LOG="$WORK/accept.log"
PASS=0; FAIL=0
ok()  { echo "  ✓ $1" | tee -a "$LOG"; PASS=$((PASS+1)); }
bad() { echo "  ✗ $1" | tee -a "$LOG"; FAIL=$((FAIL+1)); }

echo "==> 验收目录: $WORK" | tee -a "$LOG"
echo "==> 发行包  : $TARBALL" | tee -a "$LOG"

# ── 1. 包本身：校验和 + 不得含站点配置/运行时产物 ──────────────────────────
( cd "$(dirname "$TARBALL")" && sha256sum -c "$(basename "$TARBALL").sha256" >/dev/null 2>&1 ) \
  && ok "tarball sha256 校验通过" || bad "tarball sha256 校验失败"

LISTING="$(tar tzf "$TARBALL")"
for pat in 'llm_benchmark.ini' 'results/' 'backups/' '__pycache__'; do
  if printf '%s\n' "$LISTING" | grep -qF -- "$pat"; then
    bad "包内不应包含: $pat"
  else
    ok "包内不含 $pat"
  fi
done
# 运行时数据库：按扩展名精确匹配（'.db' 用固定字符串，避免 . 通配误命中 xxx_db.py）
if printf '%s\n' "$LISTING" | grep -qF '.db'; then
  bad "包内不应包含 .db 数据库文件"
else
  ok "包内不含 .db 数据库文件"
fi
printf '%s\n' "$LISTING" | grep -q 'VERSION$' && ok "包含 VERSION 版本戳" || bad "缺少 VERSION"
printf '%s\n' "$LISTING" | grep -q 'wheels/' && ok "包含离线 wheels" || echo "  · 无 wheels（联网安装包）"

# ── 2. 安装（隔离目录）────────────────────────────────────────────────────
tar -xzf "$TARBALL" -C "$WORK"
PKG="$WORK/$(ls -1 "$WORK" | grep '^jisumen-llm-benchmark-' | head -1)"
INST="$WORK/install"
echo "==> 安装到: $INST" | tee -a "$LOG"
if bash "$PKG/install.sh" --install-dir "$INST" >>"$LOG" 2>&1; then
  ok "install.sh 成功"
else
  bad "install.sh 失败（日志见 $LOG）"
  tail -15 "$LOG" | sed 's/^/      /'
fi

# ── 3. 自检（含 GUI 冒烟）─────────────────────────────────────────────────
if bash "$PKG/verify-install.sh" --install-dir "$INST" --smoke >>"$LOG" 2>&1; then
  ok "verify-install.sh 全通过（含 GUI 冒烟）"
  grep -E 'PASS=|仍在运行' "$LOG" | tail -2 | sed 's/^/      /'
else
  bad "verify-install.sh 有 FAIL"
  grep -E '✗' "$LOG" | tail -5 | sed 's/^/      /'
fi

# ── 4. 真实负载：用安装副本跑 C1/N2（OpenAI 兼容端点）────────────────────
echo "==> 真实负载测试（$URL）" | tee -a "$LOG"
( cd "$INST" && "$INST/.venv/bin/python" - "$URL" <<'PY' >>"$LOG" 2>&1
import json, sys
sys.path.insert(0, ".")
from llm_benchmark_app.runner import run_benchmark
url = sys.argv[1]
out = {}
msgs = [{"role": "system", "content": "你是助手。"},
        {"role": "user", "content": "用一句话介绍基准测试。"}]
run_benchmark(url, "demo-key", "demo-qwen3-32b-fp8", msgs, 64, 0.0, 1, 2,
              lambda c, t, f: None,
              lambda *a: out.update(a[0] if a and isinstance(a[0], dict) else {}),
              stream=True)
print("BENCH_OK", json.dumps({k: out.get(k) for k in
      ("success", "fail", "success_rate", "ttft_avg", "output_tok_per_s")},
      ensure_ascii=False))
PY
) >/dev/null 2>&1
RESULT_LINE="$(grep -o 'BENCH_OK.*' "$LOG" | tail -1)"
BENCH_PASSED=0
if [ -n "$RESULT_LINE" ]; then
  python3 - "$RESULT_LINE" <<'PY' && BENCH_PASSED=1
import json, sys
payload = json.loads(sys.argv[1].split("BENCH_OK", 1)[1].strip())
ok = (payload.get("success") or payload.get("successful_requests") or 0) >= 1
sys.exit(0 if ok else 1)
PY
fi
if [ "$BENCH_PASSED" = "1" ]; then
  ok "安装副本跑通真实基准: ${RESULT_LINE#BENCH_OK }"
else
  bad "安装副本基准未取得任何成功请求: ${RESULT_LINE:-无输出}"
  tail -8 "$LOG" | sed 's/^/      /'
fi

# ── 5. 收尾 ───────────────────────────────────────────────────────────────
echo
echo "==> 验收结果: PASS=$PASS FAIL=$FAIL  （日志：$LOG）"
echo "    隔离安装位于 $INST（可整体删除；本机既有服务未受影响）"
[ "$FAIL" = "0" ]
