#!/usr/bin/env bash
# 端到端验证样例：改前失败 → 规格检索 → MiniMax 生成补丁 → 应用 → 改后全过 → 状态持久化与重启复用。
set -uo pipefail
cd "$(dirname "$0")/.."
. scripts/_env.sh
mkdir -p records/logs
STAMP=$(date +%Y%m%dT%H%M%S)
LOG="records/logs/e2e-${STAMP}.log"
export PYTHONUNBUFFERED=1

step() { echo | tee -a "$LOG"; echo "=== $* ===" | tee -a "$LOG"; }
run() {
  echo "\$ $*" | tee -a "$LOG"
  "$@" 2>&1 | tee -a "$LOG"
  return "${PIPESTATUS[0]}"
}

step "E2E start $(date -Is)  python=$PY"
echo "git: $(git rev-parse --short HEAD 2>/dev/null || echo n/a)" | tee -a "$LOG"

step "[0] 回到基线（business/ 恢复为 docs/snapshots/before）"
run bash scripts/reset.sh || exit 1

step "[1] 改前：目标测试必须失败"
run "$PY" -m pytest -q business/tests/test_filter_feature.py
BEFORE_TARGET=$?
echo "target exit code (before) = $BEFORE_TARGET" | tee -a "$LOG"
if [ "$BEFORE_TARGET" -eq 0 ]; then
  echo "FAIL: 目标测试在基线就通过，验证样例无效" | tee -a "$LOG"; exit 10
fi
if [ "$BEFORE_TARGET" -eq 127 ]; then
  echo "FAIL: 解释器不可用（环境错误，不是功能失败）" | tee -a "$LOG"; exit 19
fi

step "[2] 改前：回归测试必须通过"
run "$PY" -m pytest -q business/tests/test_regression_baseline.py business/tests/test_http_api.py
BEFORE_REG=$?
echo "regression exit code (before) = $BEFORE_REG" | tee -a "$LOG"
if [ "$BEFORE_REG" -ne 0 ]; then
  echo "FAIL: 基线回归测试不通过，环境有问题" | tee -a "$LOG"; exit 11
fi

step "[3] 导入规格 / 代码 / 测试到 OceanBase（init）"
run "$PY" -m ctxsdd init || exit 12

step "[4] 检索并构造开发上下文（plan）"
run "$PY" -m ctxsdd plan || exit 13

step "[5] MiniMax 生成补丁 → 应用 → 验证（最多 3 轮）"
OK=0
for attempt in 1 2 3; do
  echo "--- attempt $attempt ---" | tee -a "$LOG"
  if ! run "$PY" -m ctxsdd ask --attempt "$attempt"; then
    echo "ask failed on attempt $attempt" | tee -a "$LOG"
    continue
  fi
  if ! run "$PY" -m ctxsdd apply; then
    echo "apply failed on attempt $attempt" | tee -a "$LOG"
    rm -rf business && cp -a docs/snapshots/before business
    continue
  fi
  if run "$PY" -m ctxsdd verify; then
    OK=1
    break
  fi
  echo "verify failed on attempt $attempt, rolling back to baseline" | tee -a "$LOG"
  rm -rf business && cp -a docs/snapshots/before business
done
if [ "$OK" -ne 1 ]; then
  echo "FAIL: 3 轮内未能通过目标测试与回归测试" | tee -a "$LOG"; exit 14
fi

step "[6] 新进程读取 PowerContext 状态（重启复用）"
run "$PY" -m ctxsdd state-show || exit 15

step "[7] 新进程再次 plan：输出应体现已复用的进度"
run "$PY" -m ctxsdd plan > /dev/null || exit 16
"$PY" - <<'PYCODE'
import json
pack = json.load(open("records/context_pack.json", encoding="utf-8"))
progress = pack["progress"]
print("reuse: events=%s verified=%s patch_applied=%s attempts=%s"
      % (progress["events"], progress["verified"], progress["patch_applied"], progress["attempts"]))
assert progress["verified"] is True, "state not reused"
print("spec_digest:", pack["specs"]["fingerprint"]["digest"])
print("spec_items:", [item["doc_id"] for item in pack["specs"]["items"]])
PYCODE
[ $? -eq 0 ] || exit 17

step "[8] 改后：目标 + 回归全量重跑（留证）"
run "$PY" -m pytest -q business/tests/test_filter_feature.py business/tests/test_regression_baseline.py business/tests/test_http_api.py || exit 18

step "E2E OK $(date -Is)  log=$LOG"
