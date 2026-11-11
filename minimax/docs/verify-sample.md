# 验证样例：TASK-001 为阅读清单服务新增按状态与最低评分筛选

本文件是题目要求的「验证样例」：任务描述 / 有效规格 / 初始数据 / 操作步骤 / 预期结果 / 重置方法。
所有命令都在 `minimax/` 目录下执行。

---

## 1. 任务描述

- **task_id**：`TASK-001`（定义在 `tasks/TASK-001.json`）
- **要改什么**：在 `ReadingListService.list_items` 上实现两个筛选参数
  - `status`：按阅读状态筛选，接受 `Status` 枚举或字符串值（`to_read` / `reading` / `finished`）；
  - `min_rating`：只保留 `rating >= min_rating` 的条目，**未评分（`rating is None`）必须排除**；
  - 两者同时给出时按 **AND** 组合；
  - `min_rating` 越界（不在 1..5）或 `status` 非法时抛 `ValueError`。
- **哪些原有行为必须保持**（回归红线）
  1. 三种 `sort`（`added_at_desc` / `added_at_asc` / `title_asc`）与"主键相同按 `id` 升序"的稳定次序；
  2. `page` / `page_size` 校验与越界分页语义（越界返回空 `items` 但 `total`/`total_pages` 不变）；
  3. `total` / `total_pages` 描述**筛选之后**的全集，`total_pages=0` 当且仅当 `total=0`；
  4. `get_item` 缺失返回 `None`；
  5. HTTP 层：`/items` 参数 1:1 透传、`ValueError → 400 {"error": ...}`、未知路径 `404`。
- **不做的事**：不改公共签名、不改 HTTP 契约、不重构、不动排序/分页实现。

## 2. 有效规格（`specs/`，共 3 条）

| 规格 | 覆盖内容 |
|------|----------|
| `SPEC-001-list-filter.md` | 筛选能力声明、未评分条目处理、参数校验、与分页/排序的关系、验收标准（6 节） |
| `SPEC-002-ordering-and-pagination.md` | 三种排序、稳定次序、分页参数、分页边界、验收标准（5 节） |
| `SPEC-003-error-contract.md` | 单条读取、错误表达、HTTP 契约、验收标准（4 节） |

这三条规格在 `init` 时被切成 17 个条目写入 OceanBase，`plan` 时按任务关键词检索出其中 6 条进入上下文。

## 3. 初始数据

`business/reading_list/data.py` 里 12 条确定性记录（`added_at` 从 2026-01-01 起逐日递增）：

| id | 状态 | 评分 | `added_at` 日 |
|----|------|------|---------------|
| r01 | finished | 5 | 1 |
| r02 | finished | 5 | 2 |
| r03 | reading | 4 | 3 |
| r04 | finished | 4 | 4 |
| r05 | reading | **None** | 5 |
| r06 | to_read | **None** | 6 |
| r07 | finished | 3 | 7 |
| r08 | reading | 5 | 8 |
| r09 | to_read | **None** | 9 |
| r10 | to_read | **None** | 10 |
| r11 | finished | 4 | 11 |
| r12 | reading | 3 | 12 |

由此可推出的验收事实（SPEC-001 §5 已写明）：
`status=finished` → 5 条（r11,r07,r04,r02,r01）；`min_rating=4` → 6 条；
`status=finished & min_rating=4` → 4 条；`status=to_read & min_rating=5` → 0 条。

## 4. 操作步骤

前置：`.env` 填好 `MM_MINIMAX_API_KEY`，OceanBase/seekdb 可连（`python -m ctxsdd doctor` 应为 `reachable: true`）。

### 4.1 一条命令跑完（推荐）

```bash
bash scripts/e2e_flow.sh
```

它按顺序做 8 步：回到基线 → 改前目标测试（应失败）→ 改前回归测试（应通过）→ `init` → `plan`
→ （`ask` → `apply` → `verify`）×最多 3 轮 → `state-show`（新进程）→ 再 `plan`（新进程，验证复用）
→ 改后全量重跑。退出码 0 表示全部成立，日志在 `records/logs/e2e-*.log`。

### 4.2 分步手工验证（每步都是独立进程，便于观察状态复用）

```bash
bash scripts/reset.sh                                        # 0) 回到基线
python -m pytest -q business/tests/test_filter_feature.py    # 1) 应 14 failed
python -m pytest -q business/tests/test_regression_baseline.py business/tests/test_http_api.py  # 2) 应 20 passed

python -m ctxsdd init                                        # 3) 建库建表 + 导入 77 块
python -m ctxsdd plan                                        # 4) 生成 records/context_pack.json
python -m ctxsdd ask --attempt 1                             # 5) MiniMax 生成补丁
python -m ctxsdd apply                                       # 6) 应用补丁 + 快照 + diff
python -m ctxsdd verify                                      # 7) 目标 14 passed + 回归 20 passed → 写 verified

python -m ctxsdd state-show                                  # 8) 新进程读取状态：verified=true
python -m ctxsdd plan | tail -20                             # 9) 再 plan：progress.events 增长、verified=true
```

### 4.3 规格更新 → 输出必须变化

```bash
bash scripts/spec_update_check.sh
```

它备份 `specs/`，追加 `SPEC-001 §6` 并新增 `SPEC-004-export-csv.md`，重新 `init` + `plan`，
断言两次的 `specs.fingerprint.digest` 与 `specs.items` 都变化、且新规格被检索进来，
然后打印 `diff -u` 并恢复 `specs/`。产物在 `records/spec_update/{plan_a,plan_b,plan.diff}`。

## 5. 预期结果

| 阶段 | 预期 |
|------|------|
| 改前 目标测试 | `14 failed`，失败原因是断言不符（不是 ImportError / 环境错误） |
| 改前 回归测试 | `20 passed` |
| `init` | `total_chunks=77`（spec 17 / code 25 / test 35），`server_version` 含 `seekdb` |
| `plan` | 规格条目 ≥ 6 条，均带真实 `path` 与行号；代码条目含 `business/reading_list/service.py :: ReadingListService.list_items`；`tests.target/regression` 为声明文件；`progress.source = "PowerContext"` |
| `ask` | 返回 `ok:true`、`finish_reason: stop`、`modified_paths` 含 `business/reading_list/service.py`，`records/minimax_dev_log.jsonl` 新增一条 |
| `apply` | `ok:true`、`changed_files` 非空；`docs/snapshots/after` 与 `records/code_diff.txt` 生成 |
| `verify` | `target: 14 passed`、`regression: 20 passed`，`ok:true`，退出码 0 |
| `state-show`（新进程） | 事件序列含 `context_built→patch_generated→patch_applied→tests_run→verified`，`progress.verified=true` |
| 再 `plan`（新进程） | `progress.events` 比第一次 `plan` 时增加，`verified=true`，`attempts` 反映实际轮数 |
| 改后 全量 | `34 passed`（14 目标 + 20 回归） |
| 规格更新验证 | `spec_digest` 变化、`specs.items` 变化、`SPEC-004` 出现在检索结果里 |

补充说明（来自归档运行 `docs/runs/e2e-*.log`）：

- 本次归档的运行里 `ask` **第 1 轮失败、第 2 轮成功**：第 1 轮补丁的 hunk 上下文与文件不完全一致，
  `apply` 明确报 `hunk context not found ... (expected old start 70)` 并返回非 0；
  脚本按设计回滚基线并重试，第 2 轮 `apply` + `verify` 全过。因此 `progress.attempts=2`，
  `state-show` 里能看到两条 `patch_generated` 事件——这正是"状态被复用、重试有据可查"的证据。
- 一次成功也是可能的（更早的运行里出现过 `attempts=1`）；重点是**无论几轮，最终都必须
  目标 14 passed + 回归 20 passed**，而且失败轮不能被当成成功。

## 6. 重置方法

```bash
bash scripts/reset.sh          # 恢复 business/ 为 docs/snapshots/before，并清 patches/快照/上下文包
python -m ctxsdd reset         # 额外删除 OceanBase 表 + PowerContext 库（回到"干净环境"）
```

`reset.sh` 不带参数会同时清掉 PowerContext 状态（等价于全新部署），
想保留状态、只回滚代码用 `bash scripts/reset.sh --keep-state --keep-ob`。

## 7. 失败时的排查顺序

1. `python -m ctxsdd doctor` → OB 是否 `reachable`、`api_key_present` 是否为 true；
2. 改前目标测试若"通过"→ 说明 `business/` 不是基线，执行 `bash scripts/reset.sh`；
3. 改前回归测试若失败 → 环境问题，先修环境（缺 pydantic / pytest 版本）；
4. `apply` 报 `hunk context not found` → 补丁与当前文件不匹配，先 `reset.sh` 再重新 `ask`；
5. `ask` 报 `finish_reason=length` → 提高 `MM_MINIMAX_MAX_TOKENS`（默认 8192 已含一次翻倍重试）。
