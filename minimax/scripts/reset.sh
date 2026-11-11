#!/usr/bin/env bash
# 回到基线：把 business/ 恢复成 docs/snapshots/before/ 的快照内容。
# 同时清理运行产物（OB 表 / PowerContext 库 / patches / 上下文包），参数原样透传给 ctxsdd reset。
# 注意：默认**保留** docs/snapshots/after（修改后快照是提交材料的一部分）；
#      需要连它一起删时显式加 --hard。
set -uo pipefail
cd "$(dirname "$0")/.."
. scripts/_env.sh

if [ ! -d docs/snapshots/before ]; then
  echo "ERROR: docs/snapshots/before missing" >&2
  exit 1
fi

"$PY" -m ctxsdd reset "$@" >/dev/null 2>&1 || true
rm -rf business
cp -a docs/snapshots/before business
find business docs/snapshots/before -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
echo "reset: business/ restored from docs/snapshots/before (files: $(find business -type f | wc -l))"
