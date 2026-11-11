"""补丁抽取与应用（保留修改前/后快照与 diff 产物）。"""

from __future__ import annotations

import hashlib
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

DIFF_FENCE = re.compile(r"```(?:diff|patch)?\s*\n(.*?)```", re.DOTALL)


class PatchError(RuntimeError):
    """补丁无法抽取或应用。"""


@dataclass
class ApplyResult:
    ok: bool
    method: str
    returncode: int
    stdout: str
    stderr: str


HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def _count_hunk(lines: list[str]) -> tuple[int, int]:
    """按行前缀重新统计 hunk 的 old/new 行数。

    模型偶尔把空上下文行写成空字符串而不是单空格，或把 hunk 计数写错一位，
    这里按实际内容重算，属于补丁规范化而非改写模型意图。
    """
    old = new = 0
    for line in lines:
        if line.startswith("@@ ") or line.startswith("diff --git "):
            break
        if line.startswith("\\"):  # \ No newline at end of file
            continue
        if line.startswith("+"):
            new += 1
        elif line.startswith("-"):
            old += 1
        else:
            old += 1
            new += 1
    return old, new


def normalize_diff(diff: str) -> str:
    """去掉行尾 \\r，并按实际内容重算每个 hunk 的行数。"""
    text = diff.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]

    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = HUNK_HEADER.match(line)
        if not match:
            out.append(line)
            index += 1
            continue
        start = index + 1
        end = start
        while end < len(lines) and not HUNK_HEADER.match(lines[end]) and not lines[end].startswith("diff --git "):
            end += 1
        body = lines[start:end]
        for offset, body_line in enumerate(body):
            if body_line == "":
                body[offset] = " "  # 空行按上下文行处理
            elif not body_line.startswith(("+", "-", " ", "\\")):
                body[offset] = " " + body_line
        old, new = _count_hunk(body)
        tail = match.group(5) or ""
        header = f"@@ -{match.group(1)},{old} +{match.group(3)},{new} @@{tail}"
        out.append(header)
        out.extend(body)
        index = end
    return "\n".join(out).strip("\n") + "\n"


def extract_diff(text: str) -> str:
    """从模型输出里取出 unified diff，并做行数与行尾规范化。"""
    candidate = ""
    for match in DIFF_FENCE.finditer(text):
        block = match.group(1)
        if "--- " in block and "+++ " in block:
            candidate = block
            break
    if not candidate:
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if line.startswith("--- ") or line.startswith("diff --git"):
                candidate = "\n".join(lines[index:])
                break
    if not candidate:
        raise PatchError("no unified diff found in model response")
    candidate = normalize_diff(candidate)
    if not candidate.startswith("diff --git") and not candidate.startswith("--- "):
        raise PatchError("extracted block is not a unified diff")
    removed = sum(1 for line in candidate.splitlines() if line.startswith("-") and not line.startswith("--- "))
    added = sum(1 for line in candidate.splitlines() if line.startswith("+") and not line.startswith("+++ "))
    if removed == 0 or added == 0:
        raise PatchError(f"diff has no added/removed lines (added={added}, removed={removed})")
    return candidate


def diff_sha(diff: str) -> str:
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()


def modified_paths(diff: str) -> list[str]:
    """从 diff 里解析出被修改的相对路径（去掉 a/ b/ 前缀）。"""
    paths: list[str] = []
    for line in diff.splitlines():
        for prefix in ("--- ", "+++ "):
            if line.startswith(prefix):
                raw = line[len(prefix) :].strip()
                if raw in ("/dev/null", ""):
                    continue
                raw = raw.split("\t")[0]
                if raw.startswith(("a/", "b/")):
                    raw = raw[2:]
                if raw not in paths:
                    paths.append(raw)
    return paths


def _split_file_sections(diff: str) -> list[tuple[str, list[str]]]:
    """把 unified diff 拆成 (目标相对路径, hunk 行列表)。

    兼容 ``diff --git a/x b/x`` 头部与只有 ``--- `` / ``+++ `` 的精简形式。
    """
    lines = diff.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]  # 文件末尾换行不是上下文行
    sections: list[tuple[str, list[str]]] = []
    index = 0
    while index < len(lines):
        current_target: str | None = None
        if lines[index].startswith("--- "):
            raw = lines[index][4:].strip().split("\t")[0]
            if raw.startswith(("a/", "b/")):
                raw = raw[2:]
            current_target = raw
            index += 1
            if index < len(lines) and lines[index].startswith("+++ "):
                index += 1
        elif lines[index].startswith("diff --git "):
            index += 1
            continue
        else:
            index += 1
            continue
        body: list[str] = []
        while index < len(lines) and not lines[index].startswith("diff --git ") and not lines[index].startswith("--- "):
            body.append(lines[index])
            index += 1
        if current_target:
            sections.append((current_target, body))
    return sections


def _apply_hunks(path: Path, body: list[str]) -> tuple[bool, str]:
    """把一个文件的 hunks 应用到 path（内置实现，不依赖 git / patch 命令）。"""
    if not path.is_file():
        return False, f"target file not found: {path}"
    original = path.read_text(encoding="utf-8").split("\n")
    if original and original[-1] == "":
        original = original[:-1]
    result = list(original)
    offset = 0
    index = 0
    applied = 0
    while index < len(body):
        match = HUNK_HEADER.match(body[index])
        if not match:
            index += 1
            continue
        old_start = int(match.group(1))
        old_count = int(match.group(2) or 1)
        new_count = int(match.group(4) or 1)
        index += 1
        old_block: list[str] = []
        new_block: list[str] = []
        # 用 hunk 头里的行数界定本 hunk，避免把文件尾部换行或注释行误当上下文。
        while index < len(body) and not HUNK_HEADER.match(body[index]):
            if len(old_block) >= old_count and len(new_block) >= new_count:
                break
            line = body[index]
            if line.startswith("\\"):
                index += 1
                continue
            if line.startswith("+"):
                new_block.append(line[1:])
            elif line.startswith("-"):
                old_block.append(line[1:])
            else:
                content = line[1:] if line.startswith(" ") else line
                old_block.append(content)
                new_block.append(content)
            index += 1
        anchor = max(0, old_start - 1 + offset)
        position = _locate(result, old_block, anchor)
        if position is None:
            return False, f"hunk context not found in {path.name} (expected old start {old_start})"
        result[position : position + len(old_block)] = new_block
        offset += len(new_block) - len(old_block)
        applied += 1
    if applied == 0:
        return False, f"no applicable hunk for {path.name}"
    path.write_text("\n".join(result) + "\n", encoding="utf-8")
    return True, f"{applied} hunk(s) applied"


def _locate(lines: list[str], block: list[str], anchor: int) -> int | None:
    """优先在基准位置附近找完全匹配的旧块，找不到再全文件搜索。"""
    if not block:
        return min(anchor, len(lines))
    limit = min(len(lines) - len(block), anchor + 20)
    for start in range(max(0, anchor - 20), max(0, limit) + 1):
        if lines[start : start + len(block)] == block:
            return start
    for start in range(0, len(lines) - len(block) + 1):
        if lines[start : start + len(block)] == block:
            return start
    return None


def apply_patch(root: Path, diff: str) -> ApplyResult:
    """应用补丁。内置 Python 实现，只依赖标准库。"""
    patch_file = Path(root) / "records" / "patches" / "_pending.diff"
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.write_text(diff, encoding="utf-8")

    sections = _split_file_sections(diff)
    if not sections:
        return ApplyResult(
            ok=False,
            method="builtin",
            returncode=1,
            stdout="",
            stderr="no file section found in diff",
        )
    messages: list[str] = []
    for relative, body in sections:
        ok, message = _apply_hunks(Path(root) / relative, body)
        messages.append(f"{relative}: {message}")
        if not ok:
            return ApplyResult(
                ok=False,
                method="builtin",
                returncode=1,
                stdout="\n".join(messages),
                stderr=message,
            )
    return ApplyResult(ok=True, method="builtin", returncode=0, stdout="\n".join(messages), stderr="")


def snapshot(root: Path, source: Path, destination: Path) -> dict[str, str]:
    """把 ``source`` 树复制到 ``destination``，返回 {相对路径: sha256}。"""
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
    )
    digests: dict[str, str] = {}
    for path in sorted(destination.rglob("*")):
        if path.is_file():
            rel = path.relative_to(destination).as_posix()
            digests[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def write_unified_diff(root: Path, before: Path, after: Path, output: Path) -> str:
    """生成修改前后两份快照的 unified diff（difflib 实现，不依赖 git）。"""
    import difflib

    chunks: list[str] = []
    names = sorted({path.relative_to(before).as_posix() for path in before.rglob("*") if path.is_file()} |
                   {path.relative_to(after).as_posix() for path in after.rglob("*") if path.is_file()})
    for name in names:
        left = before / name
        right = after / name
        if left.is_file() and right.is_file() and left.read_bytes() == right.read_bytes():
            continue
        old_lines = left.read_text(encoding="utf-8").splitlines(keepends=True) if left.is_file() else []
        new_lines = right.read_text(encoding="utf-8").splitlines(keepends=True) if right.is_file() else []
        chunks.extend(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/business/{name}",
                tofile=f"b/business/{name}",
                n=3,
            )
        )
    text = "".join(chunks)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    return text


def stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%S")
