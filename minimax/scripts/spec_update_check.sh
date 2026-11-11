#!/usr/bin/env bash
# 规格更新验证：改一条规格 + 新增一条规格 → 重新 init → plan 输出必须变化。
set -uo pipefail
cd "$(dirname "$0")/.."
. scripts/_env.sh
OUT="records/spec_update"
mkdir -p "$OUT"
BACKUP=$(mktemp -d)
cp -a specs/. "$BACKUP/"

cleanup() {
  rm -rf specs
  cp -a "$BACKUP" specs
  rm -rf "$BACKUP"
  echo "specs/ restored"
}
trap cleanup EXIT

echo "=== [1] 基线 init + plan ==="
"$PY" -m ctxsdd init > "$OUT/init_a.json" 2>&1 || { cat "$OUT/init_a.json"; exit 1; }
"$PY" -m ctxsdd plan > "$OUT/plan_a.json" 2>&1 || { cat "$OUT/plan_a.json"; exit 1; }

echo "=== [2] 修改规格：SPEC-001 增补一条验收，新增 SPEC-004（导出复用筛选语义）==="
cat >> specs/SPEC-001-list-filter.md <<'EOF'

## 6. 增补条款（规格演进验证用）

`min_rating` 与 `status` 同时给出时，必须先按状态筛选再按评分筛选，
任何实现都不得改变 `total` 的口径；导出等下游能力复用同一筛选语义。
EOF
cat > specs/SPEC-004-export-csv.md <<'EOF'
# SPEC-004 导出能力复用筛选语义

- 状态：draft
- 归属：reading_list 服务
- 关联任务：TASK-001

## 1. 能力声明

导出（CSV）必须复用 `list_items` 的筛选语义：`status` 与 `min_rating` 的作用范围、
未评分条目在给出 `min_rating` 时被排除、分页与排序语义均与列表查询一致。

## 2. 验收标准

- 导出结果集的条目集合等于同参数下 `list_items` 全量分页结果的并集。
EOF

echo "=== [3] 重新 init + plan ==="
"$PY" -m ctxsdd init > "$OUT/init_b.json" 2>&1 || { cat "$OUT/init_b.json"; exit 1; }
"$PY" -m ctxsdd plan > "$OUT/plan_b.json" 2>&1 || { cat "$OUT/plan_b.json"; exit 1; }

echo "=== [4] 比对 spec 指纹与上下文包 ==="
"$PY" - "$OUT" <<'PY'
import json, sys
from pathlib import Path

out = Path(sys.argv[1])


def load(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    start = text.find("{")
    return json.loads(text[start:])


a = load(out / "plan_a.json")
b = load(out / "plan_b.json")
fa = a["pack"]["specs"]["fingerprint"]["digest"]
fb = b["pack"]["specs"]["fingerprint"]["digest"]
ida = [item["doc_id"] for item in a["pack"]["specs"]["items"]]
idb = [item["doc_id"] for item in b["pack"]["specs"]["items"]]
print("spec_digest A:", fa)
print("spec_digest B:", fb)
print("spec items A:", ida)
print("spec items B:", idb)
assert fa != fb, "spec fingerprint did not change"
assert ida != idb, "retrieved spec items did not change"
assert any("SPEC-004" in item for item in idb), "new spec not retrieved"
print("OK: spec update changed the plan output")
PY
RC=$?
diff -u "$OUT/plan_a.json" "$OUT/plan_b.json" > "$OUT/plan.diff"
echo "diff written: $OUT/plan.diff ($(wc -l < "$OUT/plan.diff") lines)"
exit $RC
