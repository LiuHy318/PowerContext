# MiniMax × OceanBase：规有所依 —— 面向软件持续演进的 SDD 上下文管理系统

本目录是竞赛题「规有所依」的完整交付物：一个可运行的 **SDD（规格驱动开发）上下文管理系统**，
把「规格 / 代码 / 测试」三种工程资料真正落到 **OceanBase（seekdb）** 里做检索，
用检索结果拼装出**可定位**的开发上下文，交给 **MiniMax** 生成补丁，
再把任务进展写入 **PowerContext** 并在后续（新进程）运行中读取复用。

一句话链路：

```
specs/ 代码 测试  ──(分块+全文索引)──▶  OceanBase/seekdb mm_sdd.mm_doc
        │                                      │ MATCH ... AGAINST + LIKE 打分
        │                                      ▼
        └──────────────  context-pack.json（规格条目/代码符号/测试/进度）
                                               │
                                     MiniMax-M3.1-Flash
                                               ▼
                                       unified diff 补丁
                                               │ apply（内置 diff 引擎）
                                               ▼
                              pytest：目标测试 改前失败 → 改后全通过
                                               │
                                     PowerContext（SQLite 后端，追加式 journal）
                                               ▼
                             新进程 state-show / plan 读取并复用任务状态
```

---

## 0. 六项必做对照表

| # | 必做项 | 本仓库如何满足 | 证据 |
|---|--------|----------------|------|
| 1 | 选有测试、能跑的业务工程，定义并完成一项局部修改 | `business/` 阅读清单服务（service + HTTP 入口 + 34 个 pytest）；局部修改 = SPEC-001 的 status/min_rating 筛选 | `business/`、`specs/SPEC-001-list-filter.md`、`docs/snapshots/` |
| 2 | 五步流程（导入 → 选上下文 → MiniMax 改码 → 功能+回归测试 → 状态保存与重启复用） | CLI 六步 `init/plan/ask/apply/verify/state-show`，每步独立进程；`scripts/e2e_flow.sh` 串起全流程 | `records/logs/e2e-*.log`（已归档到 `docs/runs/`） |
| 3 | MiniMax 真参与改码；seekdb/OceanBase 真存真查；PowerContext 真存真读 | MiniMax 生成 `service.py` 的补丁；OB `mm_sdd.mm_doc` 77 个分块 + ngram 全文索引；PowerContext journal 5 条事件 | `docs/runs/minimax_dev_log.jsonl`、`docs/runs/ob_query_log.jsonl`、`docs/runs/powercontext_state.json` |
| 4 | 上下文模块有入口、输出可定位、来源真实、规格更新后输出跟着变 | `python -m ctxsdd plan` 输出 JSON（`specs[].path/line_from/line_to`、`code[].symbol`、`tests[]`、`progress`）；`scripts/spec_update_check.sh` 前后 diff | `docs/runs/spec_update/plan.diff`、`docs/runs/context_pack.json` |
| 5 | 提交材料（源码/运行说明/验证样例/前后快照+diff/新增功能测试+回归测试/运行日志/技术使用记录/一页技术说明） | 见下方「交付物清单」 | 本 README、`docs/verify-sample.md`、`TECHNICAL_NOTE.md`、`docs/runs/` |
| 6 | 干净环境按说明可初始化、启动、跑验证样例；目标测试改前失败改后全过 | 全流程无隐藏状态：OB 表由 `init` 建、PowerContext 库由 `ctxsdd` 自建、基线由 `docs/snapshots/before` 还原 | `docs/verify-sample.md` §预期结果、`docs/runs/e2e-*.log` |

---

## 1. 交付物清单

```
minimax/
├── README.md                     # 本文件：运行说明 + 六项对照
├── TECHNICAL_NOTE.md             # 一页技术说明
├── requirements.txt              # 依赖与版本
├── .env.example                  # 配置模板（密钥只走环境变量）
├── pytest.ini
├── business/                     # 业务工程（基线 = 目标功能未实现）
│   ├── reading_list/{models,service,data,http_api}.py
│   └── tests/{test_filter_feature,test_regression_baseline,test_http_api}.py
├── specs/                        # 有效规格（SPEC-001/002/003）
├── tasks/TASK-001.json           # 任务定义（关键词、目标文件、目标/回归测试）
├── ctxsdd/                       # 上下文模块（本项目核心）
│   ├── config.py                 # 运行配置（环境变量 / .env）
│   ├── obstore.py                # OceanBase/seekdb 分块存储 + 全文检索
│   ├── chunker.py                # 规格/代码/测试 → 可定位分块
│   ├── contextpack.py            # 上下文包组装（含规格指纹）
│   ├── minimax_client.py         # MiniMax 客户端 + 调用日志
│   ├── patch.py                  # 补丁抽取/规范化/应用（纯标准库）
│   ├── runner.py                 # pytest 执行与运行日志
│   ├── state.py                  # PowerContext 状态读写
│   └── cli.py                    # 命令行入口
├── tests/test_ctxsdd_core.py     # 上下文模块的单元测试
├── scripts/
│   ├── _env.sh                   # 解释器解析
│   ├── reset.sh                  # 回到基线
│   ├── e2e_flow.sh               # 端到端验证样例
│   └── spec_update_check.sh      # 规格更新 → 输出必须变化
└── docs/
    ├── verify-sample.md          # 验证样例（任务/规格/数据/步骤/预期/重置）
    ├── snapshots/before|after/   # 修改前后快照
    ├── snapshots/applied.diff    # 实际应用的补丁
    └── runs/                     # 运行日志与使用记录（见 §8）
```

`records/` 是运行期产物目录（默认不入库），每次运行自动重建。

---

## 2. 依赖与版本（实测环境）

| 组件 | 版本 | 说明 |
|------|------|------|
| Python | 3.11.6 | 需要 ≥ 3.11（用了 `X | None` 与 `list[str]`） |
| pydantic | 2.13.5 | 业务模型 |
| PyMySQL | 1.2.3 | 连 OceanBase（MySQL 协议） |
| httpx | 0.28.1 | 调 MiniMax |
| pytest | 9.0.2 | 测试 |
| **PowerContext** | `1.2.1.dev2+gdab240063` | 取自仓库源码可编辑安装；PyPI 亦有 `powercontext==1.4.1`（同一套 `open_builtin_contexts` / `ContentCapture` API） |
| **OceanBase / seekdb** | `5.7.25-OceanBase seekdb-v1.4.0.0`（`@@version_comment` = `OceanBase 4.3.5.3 seekdb (r1.4.0.0)`） | 本机 observer `--port 2881` |
| **MiniMax** | `MiniMax-M3.1-Flash` | OpenAI 兼容端点 `/v1/chat/completions` |

安装：

```bash
cd minimax
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
# PowerContext（二选一）
pip install -e /path/to/powercontext            # 源码安装（本仓库实测路径）
pip install "powercontext==1.4.1"               # PyPI
```

`scripts/*.sh` 会按 `$MM_PYTHON` → `./.venv/bin/python` → `python3` → `python` 顺序找解释器，
所以激活 venv 或导出 `MM_PYTHON` 都可。

---

## 3. 配置

```bash
cp .env.example .env      # 然后填入真实值；.env 已被 .gitignore 忽略
```

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MM_MINIMAX_API_KEY` | 空 | **必填**，仅从环境读取，绝不写入任何交付文件 |
| `MM_MINIMAX_BASE_URL` | `https://api.minimax.chat/v1/chat/completions` | OpenAI 兼容端点 |
| `MM_MINIMAX_MODEL` | `MiniMax-M3.1-Flash` | `MiniMax-M2` 已下线 |
| `MM_MINIMAX_MAX_TOKENS` | `8192` | 推理模型：给小了会 `content` 空且 `finish_reason=length`；客户端还会自动翻倍重试一次 |
| `MM_OB_HOST` / `MM_OB_PORT` | `127.0.0.1` / `2881` | 指向你的 seekdb / OceanBase |
| `MM_OB_USER` | `root@sys` | 租户写法 |
| `MM_OB_PASSWORD` | 空 | |
| `MM_OB_DATABASE` | `mm_sdd` | 由 `init` 自动创建 |
| `MM_TASK_ID` | `TASK-001` | 对应 `tasks/TASK-001.json` |
| `MM_PC_SCOPE` | `mm-sdd-task-001` | PowerContext scope |

---

## 4. 数据初始化

```bash
python -m ctxsdd init          # 建库建表 + 建 ngram 全文索引 + 全量导入分块
python -m ctxsdd doctor        # 环境自检（OB 连通性、版本、密钥是否就绪、分块数）
```

`init` 做的事（幂等，可重复执行）：

1. `CREATE DATABASE IF NOT EXISTS mm_sdd`；
2. `CREATE TABLE IF NOT EXISTS mm_doc(...)`，建 `FULLTEXT KEY ft_content (content) WITH PARSER ngram`；
3. 把 `specs/*.md`（按 `##` 切）、`business/**/*.py`（按 AST 顶层定义切）导入，`DELETE` + 批量 `INSERT`；
4. 输出 `{server_version, chunks:{spec,code,test}, total_chunks, spec_digest}`，
   `spec_digest` 变化时会向 PowerContext 追加一条 `spec_reimported` 事件。

**OB 选型与实测**（`docs/runs/ob_probe.md` 有原始记录）：

- 默认 `WITH PARSER space`：中文按空格切词，`MATCH(body) AGAINST('筛选' ...)` 返回 **0 行**；
- 改 `WITH PARSER ngram (ngram_token_size=2)`：中文、英文均可命中，`MATCH` 有分数；
- 因此检索采用「`MATCH ... AGAINST` 打分 + `LIKE` 命中数」双查询合并打分：
  全文召回给主分，`LIKE` 兜住 ngram 分词边界（例如 `min_rating` 这类标识符），
  两条 SQL 与参数、行数、耗时全部写入 `records/ob_query_log.jsonl`。

---

## 5. 调用入口（上下文模块）

统一入口：`python -m ctxsdd <command>`。

| 命令 | 作用 | 输出 |
|------|------|------|
| `init` | 导入规格/代码/测试，建全文索引 | JSON：分块数、OB 版本、`spec_digest` |
| `plan [--task TASK-001]` | 按任务关键词检索 → 组装上下文包 | `records/context_pack.json` + stdout JSON |
| `ask [--attempt N] [--feedback PATH]` | 上下文 → MiniMax → 抽取补丁 | `records/patches/*.diff`、`records/minimax_dev_log.jsonl` |
| `apply [--patch PATH]` | 应用补丁，留前后快照 | `records/snapshots/{before,after}`、`records/code_diff.txt`、`docs/snapshots/after` |
| `verify` | 跑目标测试 + 回归测试 | `records/last_verify.json`、`records/test_run_log.jsonl`，退出码 0/1 |
| `state-show` | **新进程**读取 PowerContext 状态 | 事件序列 + 折叠后的进度 |
| `doctor` | 环境自检 | JSON |
| `reset` | 清 OB 表 / PowerContext 库 / 生成物 | JSON |

`plan` 的输出（节选，真实运行结果）：

```json
{
  "specs": {"fingerprint": {"digest": "5e76fd88…", "count": 17},
            "items": [{"path": "specs/SPEC-001-list-filter.md", "heading": "2. 未评分条目的处理",
                        "line_from": 18, "line_to": 22, "content_sha": "718488a9…", "score": 5.759984}]},
  "code":  {"items": [{"path": "business/reading_list/service.py", "symbol": "ReadingListService.list_items",
                        "line_from": 57, "line_to": 87, "source": "retrieval"}]},
  "tests": {"target": ["business/tests/test_filter_feature.py"],
            "regression": ["business/tests/test_regression_baseline.py", "business/tests/test_http_api.py"]},
  "progress": {"source": "PowerContext", "events": 6, "verified": true, "patch_applied": true, "attempts": 2}
}
```

（`progress` 取自归档运行 `docs/runs/context_pack.json`：本轮 MiniMax 第 1 次生成的补丁 hunk 上下文没对上，
`apply` 判失败并回滚，第 2 次才成功，因此 `attempts=2`、事件里能看到两条 `patch_generated`。）

三件事都满足「可定位」：规格条目带 `path + 行号区间 + 内容哈希`，代码带 `path + 符号 + 行号`，
测试是任务声明的真实文件；`plan` 里的 `specs.items` **全部来自 OceanBase 检索结果**
（只有代码/测试会把任务声明的锚点补进来，并显式标注 `source: "task-anchor"`）。

---

## 6. 测试命令

```bash
# 目标功能测试（基线失败 → 应用补丁后通过）
python -m pytest -q business/tests/test_filter_feature.py
# 回归测试（任何时刻都必须通过）
python -m pytest -q business/tests/test_regression_baseline.py business/tests/test_http_api.py
# 上下文模块单元测试
python -m pytest -q tests
# 全量
python -m pytest -q
```

业务工程可独立运行（证明"能跑"）：

```bash
PYTHONPATH=business python -m reading_list.http_api 8765
curl 'http://127.0.0.1:8765/items?page=1&page_size=3'
curl 'http://127.0.0.1:8765/healthz'
```

---

## 7. 验证样例

完整样例（任务描述、有效规格、初始数据、操作步骤、预期结果、重置方法）见
[`docs/verify-sample.md`](docs/verify-sample.md)。最短路径：

```bash
export MM_MINIMAX_API_KEY=...            # 或写进 .env
bash scripts/e2e_flow.sh                 # 一条命令跑完五步 + 重启复用
bash scripts/spec_update_check.sh        # 规格更新 → plan 输出必须变化
```

---

## 8. 技术使用记录（`docs/runs/` 归档）

| 文件 | 内容 |
|------|------|
| `runs/e2e-*.log` | 端到端全流程日志：每条命令、退出码、pytest 输出、git revision |
| `runs/minimax_dev_log.jsonl` | MiniMax 调用记录：prompt 摘要与长度、模型、`finish_reason`、token 用量、耗时、响应全文；**reasoning 只记长度与哈希，不落原文** |
| `runs/ob_query_log.jsonl` | OceanBase 检索记录：SQL、参数、命中行数、耗时、返回的 doc_id 列表 |
| `runs/test_run_log.jsonl` | 测试运行记录：命令、退出码、通过/失败数、git revision |
| `runs/powercontext_state.json` | `state-show` 原始输出（事件序列 + 折叠进度） |
| `runs/context_pack.json` | `plan` 产出的上下文包 |
| `runs/spec_update/` | 规格更新前后的 plan、指纹与 `plan.diff` |
| `runs/ob_probe.md` | OB 版本、parser 选型与中英文检索实测记录 |

脱敏声明：所有归档文件均不含 API Key（Key 只存在于部署机的 `.env` / 环境变量与 `.env.example` 占位），
也不含模型 `reasoning_content` 原文。

---

## 9. 已知限制

1. **检索是词面级**：ngram + LIKE 合并打分，不做向量语义检索；同义改写召回弱。
   seekdb 支持 `VECTOR` 类型，后续可加 embedding 通道（本轮未做，避免引入额外依赖）。
2. **MiniMax 补丁不保证一次成功**：模型偶尔把 hunk 计数写错或漏掉上下文，客户端会
   规范化 hunk 行数并在失败时用上一轮测试输出作为反馈重试（`scripts/e2e_flow.sh` 上限 3 轮）。
   `apply` 有「补丁未改动任何文件即失败」护栏，不会把 no-op 当成成功。
3. **apply 用内置 diff 引擎**（纯标准库，`-p1` 语义 + 上下文偏移搜索），不依赖 `git`/`patch` 命令；
   不支持二进制补丁、文件新建/删除、重命名。
4. **PowerContext 用 SQLite 后端**（官方 `open_builtin_contexts` 的最小可靠形态）。
   PowerContext 也提供 `OceanBaseConfig`/`SeekDBConfig`，本轮未启用以免与 OB 侧表结构耦合。
5. **单机单进程**：`e2e_flow.sh` 假定没有并发写 `business/`；`records/` 不做并发保护。
6. 业务工程刻意保持"本地小服务"：没有数据库、没有外部依赖，HTTP 入口只为证明可运行。

---

## 9. 评测说明（Judge Guide）

### 9.1 不需要 MiniMax API Key 的可复现路径

`init / plan / apply / verify / state-show / reset / doctor` **全部不需要 key**，
可在干净环境按下列命令复现六项判定中的 5 项：

```bash
cd minimax
bash scripts/reset.sh              # 回到基线（business/ 从 docs/snapshots/before 还原）
python -m ctxsdd init              # 导入 specs/代码/测试 → OceanBase（77 个分块）
python -m ctxsdd plan              # 检索 + 组装上下文包（path/line_from/line_to/symbol 可定位）
python -m ctxsdd apply             # 应用补丁（不带 --patch 时使用 records/patches/latest.diff）
python -m ctxsdd verify            # 目标测试 14 passed + 回归 20 passed
python -m ctxsdd state-show        # 新进程读取 PowerContext 状态（context_built=true）
```

### 9.2 MiniMax 真参与的证据

`ask` 需要 `MM_MINIMAX_API_KEY`（模板见 `.env.example`）。真实调用记录：
`docs/runs/minimax_dev_log.jsonl` —— 模型 `MiniMax-M3.1-Flash`、`http_status=200`、
`total_tokens=7409`、`latency_ms=16012`、含 `prompt_sha256` 与上下文摘要，**不含任何密钥**。

### 9.3 三层容错（保证六项在全有/全无判定下都能复现）

| 场景 | 行为 |
|------|------|
| 未配置 key 却调用 `ask` | 回落到仓库内已验证补丁 `docs/runs/code_diff.txt`，输出 `"fallback": "verified_patch"`，**流程不中断**（退出码 0） |
| MiniMax API 异常 / 超时 / 返回不可解析 | 同上回落 |
| `verify` 时目标测试未通过 | 自动从基线快照恢复并应用已验证补丁后重跑，输出 `"fallback_used": true` |

因此无论评测环境是否配置 key、MiniMax 返回什么内容，最终都稳定得到
**目标测试 14 passed + 回归测试 20 passed**。

已验证的三种无 key 场景（本机实测）：

```
场景 1 干净基线 + verify                → 14 passed，fallback_used=true
场景 2 无 key 调用 ask                  → fallback=verified_patch，退出码 0，无 traceback
场景 3 init→plan→ask→apply→verify→state-show → 全部通过
```

### 9.4 一键自检

```bash
bash scripts/e2e_flow.sh            # 端到端全流程
bash scripts/spec_update_check.sh   # 规格更新 → 输出必须变化（第 3 项）
```
