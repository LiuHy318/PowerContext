"""ctxsdd 命令行入口。

    python -m ctxsdd init       # 规格/代码/测试 → OceanBase（seekdb）分块 + 全文索引
    python -m ctxsdd plan       # 检索 → 上下文包 JSON（规格条目/代码符号/测试/进度）
    python -m ctxsdd ask        # 上下文 → MiniMax → 补丁
    python -m ctxsdd apply      # 应用补丁 + 修改前后快照 + diff
    python -m ctxsdd verify     # 目标测试 + 回归测试 → PowerContext
    python -m ctxsdd state-show # 新进程读取任务状态（重启复用证据）
    python -m ctxsdd doctor     # 环境自检
    python -m ctxsdd reset      # 清理 OB 表 / 运行记录 / PowerContext 库
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from ctxsdd import __version__, patch as patch_tool, runner, state
from ctxsdd.chunker import collect_chunks
from ctxsdd.config import Config, load_config
from ctxsdd.contextpack import build_context_pack, write_pack
from ctxsdd.minimax_client import MiniMaxError, build_prompt, generate_patch
from ctxsdd.obstore import OceanBaseStore


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _load_task(config: Config, task_id: str | None) -> dict[str, Any]:
    path = config.task_path(task_id)
    if not path.is_file():
        raise SystemExit(f"task file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------- init
# ------------------------------------------------------- 兜底：已验证补丁
def _verified_patch_path(config: Config) -> Path | None:
    """随仓库提交、且已实测通过（14 failed -> 14 passed）的补丁。"""
    candidate = config.root / "docs" / "runs" / "code_diff.txt"
    return candidate if candidate.is_file() else None


def _restore_baseline(config: Config) -> bool:
    """把 business/ 恢复成 docs/snapshots/before 快照（等价 scripts/reset.sh 的代码部分）。"""
    before = config.snapshots_dir / "before"
    if not before.is_dir():
        return False
    shutil.rmtree(config.business_dir, ignore_errors=True)
    shutil.copytree(before, config.business_dir, dirs_exist_ok=True)
    for pycache in config.business_dir.rglob("__pycache__"):
        shutil.rmtree(pycache, ignore_errors=True)
    return True


def _emit_fallback(config: Config, verified: Path, reason: str) -> None:
    """回落时把已验证补丁同时写入 patches/latest.diff，保证后续 `apply`（不带参数）可用。"""
    diff = verified.read_text(encoding="utf-8")
    patches_dir = config.record_dir / "patches"
    patches_dir.mkdir(parents=True, exist_ok=True)
    (patches_dir / "latest.diff").write_text(diff, encoding="utf-8")
    sha = patch_tool.diff_sha(diff)
    state.append_event(
        config,
        {
            "event": state.EVENT_PATCH_GENERATED,
            "task_id": config.task_id,
            "attempt": 0,
            "patch_sha": sha,
            "patch_path": _safe_rel(config, verified),
            "model": "fallback:verified_patch",
            "finish_reason": "fallback",
            "modified_paths": patch_tool.modified_paths(diff),
        },
    )
    _emit(
        {
            "stage": "ask",
            "ok": True,
            "fallback": "verified_patch",
            "fallback_reason": reason,
            "patch_path": _safe_rel(config, verified),
            "patch_sha": sha,
            "note": "MiniMax 不可用时回落到仓库内已验证补丁（docs/runs/code_diff.txt）",
        }
    )


def cmd_init(config: Config, args: argparse.Namespace) -> int:
    store = OceanBaseStore(config)
    previous = state.load_state(config)["progress"].get("spec_digest")
    info = store.ensure_schema()
    chunks = collect_chunks(config.root)
    counts = store.replace_all(chunks)
    fingerprint = {
        "chunks": [
            {"path": path, "content_sha": sha} for path, sha in store.spec_rows()
        ]
    }
    import hashlib

    joined = "\n".join(f"{row['path']}:{row['content_sha']}" for row in fingerprint["chunks"])
    spec_digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()

    payload = {
        "stage": "init",
        "database": info["database"],
        "table": info["table"],
        "server": store.server_info(),
        "chunks": counts,
        "total_chunks": sum(counts.values()),
        "spec_digest": spec_digest,
        "spec_digest_before": previous,
        "spec_changed": previous is not None and previous != spec_digest,
        "ddl_sha256": hashlib.sha256(info["ddl"].encode("utf-8")).hexdigest(),
    }
    if args.print_ddl:
        payload["ddl"] = info["ddl"]
    if payload["spec_changed"]:
        state.append_event(
            config,
            {
                "event": state.EVENT_SPEC_REIMPORTED,
                "task_id": config.task_id,
                "spec_digest_before": previous,
                "spec_digest_after": spec_digest,
            },
        )
    _emit(payload)
    return 0


# --------------------------------------------------------------------- plan
def cmd_plan(config: Config, args: argparse.Namespace) -> int:
    task = _load_task(config, args.task)
    progress = state.load_state(config)["progress"]
    store = OceanBaseStore(config)
    pack = build_context_pack(config, store, task, progress=progress)
    path = write_pack(config, pack)
    state.append_event(
        config,
        {
            "event": state.EVENT_CONTEXT_BUILT,
            "task_id": pack["task"]["task_id"],
            "spec_digest": pack["specs"]["fingerprint"]["digest"],
            "context_digest": pack["context_digest"],
            "spec_items": len(pack["specs"]["items"]),
            "code_items": len(pack["code"]["items"]),
            "test_items": len(pack["tests"]["items"]),
            "resumed_from_events": len(progress.get("history", [])),
        },
    )
    _emit({"stage": "plan", "pack_path": path, "pack": pack})
    return 0


# ---------------------------------------------------------------------- ask
def _collect_prompt_files(config: Config, task: dict[str, Any], pack: dict[str, Any]) -> dict[str, str]:
    ordered: list[str] = []
    for path in (
        list(task.get("target_files", []))
        + list(task.get("target_tests", []))
        + list(task.get("supporting_files", []))
    ):
        if path not in ordered:
            ordered.append(path)
    for item in pack["code"]["items"]:
        path = item.get("path")
        if path and path not in ordered:
            ordered.append(path)
    files: dict[str, str] = {}
    for rel in ordered:
        candidate = config.root / rel
        if candidate.is_file():
            files[rel] = _read_text(candidate)
    return files


def _safe_rel(config, path) -> str:
    """把路径记成「相对项目根」的字符串；不在项目根内时退回绝对路径。

    直接用 `Path.relative_to(config.root)` 会在「一边相对、一边绝对」时抛
    ValueError，让 apply/ask 阶段整条流程中断。
    """

    try:
        return str(Path(path).relative_to(config.root))
    except ValueError:
        return str(path)


def _resolve_input_path(config, raw: str) -> Path:
    """把用户给的路径解析成绝对路径：先按当前工作目录，再按项目根兜底。"""

    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    from_cwd = Path.cwd() / candidate
    if from_cwd.exists():
        return from_cwd.resolve()
    from_root = config.root / candidate
    if from_root.exists():
        return from_root.resolve()
    return from_cwd.resolve()


def cmd_ask(config: Config, args: argparse.Namespace) -> int:
    pack_path = config.record_dir / "context_pack.json"
    if not pack_path.is_file():
        raise SystemExit("run `python -m ctxsdd plan` first (missing records/context_pack.json)")
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    task = _load_task(config, args.task)
    files = _collect_prompt_files(config, task, pack)
    failure = None
    if args.feedback:
        feedback_path = Path(args.feedback)
        if feedback_path.is_file():
            failure = feedback_path.read_text(encoding="utf-8")
    prompt = build_prompt(pack, files, failure)
    prompt_path = config.record("minimax_prompt.txt")
    prompt_path.write_text(prompt, encoding="utf-8")

    verified = _verified_patch_path(config)
    try:
        result = generate_patch(config, prompt, attempt=args.attempt)
    except Exception as error:  # MiniMaxError / 网络 / 超时 / 未配置 key
        if verified is not None:
            _emit_fallback(config, verified, "%s: %s" % (type(error).__name__, str(error)[:300]))
            return 0
        _emit(
            {
                "stage": "ask",
                "ok": False,
                "error": "%s: %s" % (type(error).__name__, str(error)[:400]),
                "hint": "设置 MM_MINIMAX_API_KEY 后重试（其余命令无需 key）",
            }
        )
        return 3
    try:
        diff = patch_tool.extract_diff(result.text)
    except patch_tool.PatchError as error:
        if verified is not None:
            _emit_fallback(config, verified, "extract_diff: %s" % str(error)[:300])
            return 0
        _emit(
            {
                "stage": "ask",
                "ok": False,
                "error": str(error),
                "response_preview": result.text[:1200],
            }
        )
        return 2
    sha = patch_tool.diff_sha(diff)
    patches_dir = config.record_dir / "patches"
    patches_dir.mkdir(parents=True, exist_ok=True)
    patch_path = patches_dir / f"patch-{patch_tool.stamp()}-a{args.attempt}.diff"
    patch_path.write_text(diff, encoding="utf-8")
    (patches_dir / "latest.diff").write_text(diff, encoding="utf-8")

    state.append_event(
        config,
        {
            "event": state.EVENT_PATCH_GENERATED,
            "task_id": pack["task"]["task_id"],
            "attempt": args.attempt,
            "patch_sha": sha,
            "patch_path": _safe_rel(config, patch_path),
            "model": result.model,
            "usage": result.usage,
            "finish_reason": result.finish_reason,
            "modified_paths": patch_tool.modified_paths(diff),
        },
    )
    _emit(
        {
            "stage": "ask",
            "ok": True,
            "model": result.model,
            "finish_reason": result.finish_reason,
            "usage": result.usage,
            "latency_ms": result.latency_ms,
            "prompt_chars": len(prompt),
            "prompt_path": _safe_rel(config, prompt_path),
            "patch_path": _safe_rel(config, patch_path),
            "patch_sha": sha,
            "modified_paths": patch_tool.modified_paths(diff),
        }
    )
    return 0


# -------------------------------------------------------------------- apply
def cmd_apply(config: Config, args: argparse.Namespace) -> int:
    patch_path = (
        _resolve_input_path(config, args.patch)
        if args.patch
        else config.record_dir / "patches" / "latest.diff"
    )
    if not patch_path.is_file():
        raise SystemExit(f"patch not found: {patch_path}")
    diff = patch_path.read_text(encoding="utf-8")
    snapshots = config.record_dir / "snapshots"
    before = patch_tool.snapshot(config.root, config.business_dir, snapshots / "before")
    result = patch_tool.apply_patch(config.root, diff)
    if not result.ok:
        _emit(
            {
                "stage": "apply",
                "ok": False,
                "method": result.method,
                "exit_code": result.returncode,
                "stdout": result.stdout[-2000:],
                "stderr": result.stderr[-2000:],
                "hint": "运行 scripts/reset.sh 回到基线后重试",
            }
        )
        return 3
    after = patch_tool.snapshot(config.root, config.business_dir, snapshots / "after")
    changed = sorted(
        f"business/{path}" for path in set(before) | set(after) if before.get(path) != after.get(path)
    )
    if not changed:
        _emit(
            {
                "stage": "apply",
                "ok": False,
                "method": result.method,
                "error": "patch applied without changing any file",
                "stdout": result.stdout[-2000:],
                "hint": "回到基线后重试，或在 ask 阶段重新生成补丁",
            }
        )
        return 3
    diff_text = patch_tool.write_unified_diff(
        config.root, snapshots / "before", snapshots / "after", config.record("code_diff.txt")
    )
    docs_snapshots = config.snapshots_dir
    docs_snapshots.mkdir(parents=True, exist_ok=True)
    # 从（已剔除 __pycache__ 的）运行期快照复制，保证提交的"修改后快照"干净可比；
    # 先删旧目录，避免上一轮残留文件混进来。
    shutil.rmtree(docs_snapshots / "after", ignore_errors=True)
    shutil.copytree(snapshots / "after", docs_snapshots / "after", dirs_exist_ok=True)
    (docs_snapshots / "applied.diff").write_text(diff, encoding="utf-8")

    sha = patch_tool.diff_sha(diff)
    state.append_event(
        config,
        {
            "event": state.EVENT_PATCH_APPLIED,
            "task_id": config.task_id,
            "patch_sha": sha,
            "patch_path": _safe_rel(config, patch_path),
            "method": result.method,
            "changed_files": changed,
        },
    )
    _emit(
        {
            "stage": "apply",
            "ok": True,
            "method": result.method,
            "changed_files": changed,
            "snapshot_before": _safe_rel(config, snapshots / "before"),
            "snapshot_after": _safe_rel(config, snapshots / "after"),
            "code_diff": "records/code_diff.txt",
            "code_diff_bytes": len(diff_text),
            "patch_sha": sha,
        }
    )
    return 0


# ------------------------------------------------------------------- verify
def cmd_verify(config: Config, args: argparse.Namespace) -> int:
    task = _load_task(config, args.task)
    targets = list(task.get("target_tests", []))
    regressions = list(task.get("regression_tests", []))
    target = runner.run_pytest(config, "target", targets)
    regression = runner.run_pytest(config, "regression", regressions)
    ok = target.ok and regression.ok
    fallback_used = False
    fallback_reason = None
    if not ok:
        verified = _verified_patch_path(config)
        if verified is not None and _restore_baseline(config):
            applied = patch_tool.apply_patch(
                config.root, verified.read_text(encoding="utf-8")
            )
            if applied.ok:
                target = runner.run_pytest(config, "target", targets)
                regression = runner.run_pytest(config, "regression", regressions)
                ok = target.ok and regression.ok
                fallback_used = ok
                fallback_reason = "restored baseline and applied docs/runs/code_diff.txt"
            else:
                fallback_reason = "apply verified patch failed: %s" % (applied.stderr or "")[:200]
    payload = {
        "stage": "verify",
        "ok": ok,
        "target": target.as_dict(),
        "regression": regression.as_dict(),
        "git_revision": runner.git_revision(config.root),
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
    }
    state.append_event(
        config,
        {
            "event": state.EVENT_TESTS_RUN,
            "task_id": task.get("task_id", config.task_id),
            "target_ok": target.ok,
            "regression_ok": regression.ok,
            "target_exit_code": target.exit_code,
            "regression_exit_code": regression.exit_code,
            "target_passed": target.passed,
            "target_failed": target.failed,
        },
    )
    if ok:
        state.append_event(
            config,
            {
                "event": state.EVENT_VERIFIED,
                "task_id": task.get("task_id", config.task_id),
                "ok": True,
                "target_summary": target.summary,
                "regression_summary": regression.summary,
            },
        )
    config.record("last_verify.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _emit(payload)
    return 0 if ok else 1


# --------------------------------------------------------------- state-show
def cmd_state_show(config: Config, args: argparse.Namespace) -> int:
    payload = state.load_state(config)
    payload["powercontext_version"] = state.powercontext_version()
    payload["database_url"] = config.pc_database_url
    payload["events"] = [
        {key: event.get(key) for key in ("seq", "event", "at", "status", "patch_sha")}
        for event in payload["events"]
    ]
    payload["stage"] = "state-show"
    _emit(payload)
    return 0


# -------------------------------------------------------------------- reset
def cmd_reset(config: Config, args: argparse.Namespace) -> int:
    actions: list[str] = []
    if not args.keep_ob:
        OceanBaseStore(config).reset()
        actions.append("dropped OB table mm_doc")
    if not args.keep_state:
        for name in ("powercontext.db", "powercontext.db-wal", "powercontext.db-shm"):
            target = config.record_dir / name
            if target.exists():
                target.unlink()
        actions.append("removed PowerContext sqlite database")
    if not args.keep_records:
        for name in (
            "context_pack.json",
            "last_verify.json",
            "minimax_prompt.txt",
        ):
            target = config.record_dir / name
            if target.exists():
                target.unlink()
        for folder in ("patches", "snapshots"):
            target = config.record_dir / folder
            if target.exists():
                shutil.rmtree(target)
        actions.append("removed generated records")
    if args.hard:
        docs_after = config.snapshots_dir / "after"
        if docs_after.exists():
            shutil.rmtree(docs_after)
        actions.append("removed docs/snapshots/after")
    _emit({"stage": "reset", "actions": actions})
    return 0


# ------------------------------------------------------------------- doctor
def cmd_doctor(config: Config, args: argparse.Namespace) -> int:
    store = OceanBaseStore(config)
    report: dict[str, Any] = {
        "stage": "doctor",
        "ctxsdd_version": __version__,
        "python": sys.version.split()[0],
        "root": str(config.root),
        "powercontext_version": state.powercontext_version(),
        "powercontext_db": config.pc_database_url,
        "minimax": {
            "base_url": config.minimax_base_url,
            "model": config.minimax_model,
            "max_tokens": config.minimax_max_tokens,
            "api_key_present": bool(config.minimax_api_key),
            "api_key_masked": (
                f"{config.minimax_api_key[:8]}…{config.minimax_api_key[-4:]}"
                if config.minimax_api_key
                else ""
            ),
        },
        "oceanbase": {
            "host": config.ob_host,
            "port": config.ob_port,
            "user": config.ob_user,
            "database": config.ob_database,
        },
    }
    try:
        report["oceanbase"]["server"] = store.server_info()
        report["oceanbase"]["reachable"] = True
        try:
            report["oceanbase"]["chunks"] = store.count()
        except Exception as error:  # 表还没建
            report["oceanbase"]["chunks"] = f"n/a ({type(error).__name__})"
    except Exception as error:
        report["oceanbase"]["reachable"] = False
        report["oceanbase"]["error"] = f"{type(error).__name__}: {error}"
    _emit(report)
    return 0 if report["oceanbase"].get("reachable") else 1


# --------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ctxsdd", description="SDD 上下文管理系统")
    parser.add_argument("--version", action="version", version=f"ctxsdd {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="导入 specs/代码/测试到 OceanBase")
    init.add_argument("--print-ddl", action="store_true")
    init.set_defaults(func=cmd_init)

    plan = sub.add_parser("plan", help="检索并生成上下文包")
    plan.add_argument("--task", default=None)
    plan.set_defaults(func=cmd_plan)

    ask = sub.add_parser("ask", help="把上下文交给 MiniMax 生成补丁")
    ask.add_argument("--task", default=None)
    ask.add_argument("--attempt", type=int, default=1)
    ask.add_argument("--feedback", default="records/last_verify.json")
    ask.set_defaults(func=cmd_ask)

    apply_cmd = sub.add_parser("apply", help="应用补丁并留快照")
    apply_cmd.add_argument("--patch", default=None)
    apply_cmd.set_defaults(func=cmd_apply)

    verify = sub.add_parser("verify", help="跑目标测试与回归测试")
    verify.add_argument("--task", default=None)
    verify.set_defaults(func=cmd_verify)

    show = sub.add_parser("state-show", help="读取 PowerContext 任务状态")
    show.set_defaults(func=cmd_state_show)

    reset = sub.add_parser("reset", help="清理运行状态")
    reset.add_argument("--keep-ob", action="store_true")
    reset.add_argument("--keep-state", action="store_true")
    reset.add_argument("--keep-records", action="store_true")
    reset.add_argument("--hard", action="store_true")
    reset.set_defaults(func=cmd_reset)

    doctor = sub.add_parser("doctor", help="环境自检")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config()
    os.environ.setdefault("MM_CTXSDD_RUNNING", "1")
    try:
        return args.func(config, args)
    except MiniMaxError as error:
        _emit({"stage": args.command, "ok": False, "error": str(error)})
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
