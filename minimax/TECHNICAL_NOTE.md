# 一页技术说明：规有所依的 SDD 上下文管理系统

## 问题

规格（spec）与代码会持续演进，但"这次改动该看哪几条规格、动哪个符号、跑哪些测试"通常靠人记忆。
本系统把这件事做成可重复的机器流程：**工程资料落库 → 检索装配上下文 → 生成补丁 → 用测试裁决 → 进展沉淀**。

## 架构（三层，四个真实依赖）

```
┌────────────────────────── 上下文层 ctxsdd ──────────────────────────┐
│  chunker.py   规格按 ## 切、代码按 AST 顶层定义切，每块带 path+行号  │
│  obstore.py   OceanBase/seekdb：mm_doc + ngram 全文索引 + LIKE 兜底  │
│  contextpack  MATCH 分数 ⊕ LIKE 命中 → 上下文包（含 spec 指纹）      │
│  minimax_client  OpenAI 兼容 /v1/chat/completions，补丁规范化       │
│  patch.py     内置 unified-diff 引擎（-p1、上下文偏移搜索、no-op 拒绝）│
│  state.py     PowerContext journal：追加事件 → 折叠成当前进度        │
└──────────────────────────────────────────────────────────────────────┘
```

**为什么"规有所依"能成立**：上下文包里的每条规格条目都带 `path / line_from / line_to / content_sha`，
代码条目带 `path / symbol / line_from / line_to`，测试是任务声明的真实文件。
`plan` 的规格部分**只来自数据库检索**；规格文件一改，`content_sha` 与 `spec_digest` 就变，
同一任务的 `plan` 输出随之变化（`scripts/spec_update_check.sh` 用 diff 证明这一点）。
没有任何固定答案被写进代码。

## 三个技术选型的关键实测

1. **全文索引 parser**：seekdb `5.7.25-OceanBase seekdb-v1.4.0.0` 默认 `WITH PARSER space`，
   中文按空格切词，`MATCH(body) AGAINST('筛选')` 返回 0 行；
   改 `WITH PARSER ngram (ngram_token_size=2)` 后中英文均命中。因此选 ngram。
2. **混合打分**：只靠 `MATCH` 会漏掉 `min_rating` 这类被 ngram 切碎的标识符，
   只靠 `LIKE` 又没有相关性排序。故两条 SQL 分别取分数与命中数，在进程内合并为
   `score = ft_score + 0.05 × like_hits`，并把两条 SQL、参数、行数、耗时全部写日志。
3. **MiniMax 是推理模型**：`MiniMax-M3.1-Flash` 的 `reasoning_tokens` 远大于可见输出
   （实测一次 17462 字符推理 vs 1436 字符补丁）。`max_tokens` 给小了会 `content` 为空且
   `finish_reason=length`，故默认 8192，并在截断时翻倍重试；`reasoning_content` 只记录长度与
   哈希，不入库原文。

## 状态语义（为什么"重启后还能用上"）

PowerContext 的 `sources.journal` 是**追加式**的：每次 `plan/ask/apply/verify` 都追加一条事件
（`context_built / patch_generated / patch_applied / tests_run / verified / spec_reimported`），
读取时按序折叠成 `progress`。因为状态在外部库里而不在进程内存里，
`state-show` 与第二次 `plan` 都是**新的 Python 进程**，却能看到 `verified=true`、`attempts=1`、
上一轮的 `patch_sha`——这就是"保存任务状态，重启模块后读取并使用"的可验证形态，
也是"重试时带上一次失败输出"（`ask --feedback`）的基础。

## 质量护栏

- `extract_diff`：必须同时含 `+` 与 `-` 行，hunk 行数按实际内容重算（模型常写错一位）；
- `apply`：补丁必须真的改动文件，否则判失败（早期用 `git apply` 出现过"退出码 0 但没改文件"的假成功）；
- `verify`：目标测试与回归测试**分开跑、分别记退出码**，两者都过才写 `verified`；
- `e2e_flow.sh`：先证明"改前失败"（14 failed）与"基线回归全过"（20 passed），再进入改动流程，
  失败自动回滚基线并重试，最后再跑一次全量（34 passed）。

## 规模与成本

- 分块：77 块（spec 17 / code 25 / test 35），单次检索命中 17（全文）+ 16（LIKE）行、约 0.1–0.5 s；
- MiniMax：prompt 固定 5010 tokens，单次 16–103 s（`reasoning_tokens` 占大头），
  归档运行里第 1 轮补丁上下文不匹配、第 2 轮成功，两轮合计约 47 s；
- 全流程（建库、检索、生成、应用、双测、状态读写）约 1–2 分钟。

## 演进方向

向量检索（seekdb `VECTOR`）与规格条目级影响分析；PowerContext 换 `OceanBaseConfig` 与 OB 同库；
补丁改为多文件 + 结构化 AST 变更描述；把 `plan` 暴露成 MCP 工具供 IDE 直接调用。
