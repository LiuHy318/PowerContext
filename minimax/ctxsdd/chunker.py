"""把规格 / 代码 / 测试切成可定位（path + 行号区间）的分块。"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from ctxsdd.obstore import DocChunk

SPEC_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


# --------------------------------------------------------------------- 规格
def chunk_spec(path: Path, root: Path) -> list[DocChunk]:
    """按 ``## `` 二级标题切分规格文件；文件头作为第 0 块。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    rel = _rel(path, root)
    chunks: list[DocChunk] = []
    blocks: list[tuple[str, int, int]] = []
    current_heading = ""
    current_start = 1
    for index, line in enumerate(lines, start=1):
        match = SPEC_HEADING.match(line)
        if match and len(match.group(1)) == 2:
            if current_start <= index - 1:
                blocks.append((current_heading, current_start, index - 1))
            current_heading = match.group(2).strip()
            current_start = index
    if current_start <= len(lines):
        blocks.append((current_heading, current_start, len(lines)))
    if not blocks:
        blocks.append(("", 1, len(lines)))

    for order, (heading, line_from, line_to) in enumerate(blocks):
        text = "\n".join(lines[line_from - 1 : line_to]).strip()
        if not text:
            continue
        label = heading or path.stem
        chunks.append(
            DocChunk(
                doc_id=f"spec:{rel}#{order}",
                kind="spec",
                path=rel,
                heading=label,
                symbol=path.stem if not heading else heading,
                line_from=line_from,
                line_to=line_to,
                content=f"[{rel} L{line_from}-{line_to}] {label}\n{text}",
            )
        )
    return chunks


# --------------------------------------------------------------------- 代码
def _dotted_names(tree: ast.AST) -> list[tuple[str, ast.AST]]:
    """收集模块顶层的函数/类，以及类的方法（symbol 用点号连接）。"""
    found: list[tuple[str, ast.AST]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in getattr(node, "body", []):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append((f"{prefix}{child.name}", child))
            elif isinstance(child, ast.ClassDef):
                found.append((f"{prefix}{child.name}", child))
                walk(child, f"{prefix}{child.name}.")

    walk(tree, "")
    return found


def chunk_python(path: Path, root: Path, *, kind: str) -> list[DocChunk]:
    """按顶层定义切分 Python 源文件，行号区间精确到 ``end_lineno``。"""
    rel = _rel(path, root)
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [
            DocChunk(
                doc_id=f"{kind}:{rel}#raw",
                kind=kind,
                path=rel,
                symbol=path.stem,
                line_from=1,
                line_to=len(lines),
                content=f"[{rel} L1-{len(lines)}]\n{source}",
            )
        ]

    defs = _dotted_names(tree)
    chunks: list[DocChunk] = []
    if defs:
        first_line = min(getattr(node, "lineno", 1) for _, node in defs)
        header = "\n".join(lines[: max(0, first_line - 1)]).strip()
        if header:
            chunks.append(
                DocChunk(
                    doc_id=f"{kind}:{rel}#header",
                    kind=kind,
                    path=rel,
                    symbol=f"{path.stem}:<module-header>",
                    heading="module header",
                    line_from=1,
                    line_to=first_line - 1,
                    content=f"[{rel} L1-{first_line - 1}] module header\n{header}",
                )
            )
    for name, node in defs:
        line_from = getattr(node, "lineno", 1)
        line_to = getattr(node, "end_lineno", line_from)
        text = "\n".join(lines[line_from - 1 : line_to]).strip()
        if not text:
            continue
        chunks.append(
            DocChunk(
                doc_id=f"{kind}:{rel}#{name}",
                kind=kind,
                path=rel,
                symbol=name,
                heading=name.split(".")[-1],
                line_from=line_from,
                line_to=line_to,
                content=f"[{rel} L{line_from}-{line_to}] {name}\n{text}",
            )
        )
    return chunks


def collect_chunks(root: Path) -> list[DocChunk]:
    """收集 specs/*.md、business/**/*.py（区分 code/test）。"""
    chunks: list[DocChunk] = []
    for spec in sorted((root / "specs").glob("*.md")):
        chunks.extend(chunk_spec(spec, root))
    tests_dir = root / "business" / "tests"
    for py in sorted((root / "business").rglob("*.py")):
        if py.name.startswith(".") or "__pycache__" in py.parts:
            continue
        kind = "test" if tests_dir in py.parents or py.parent == tests_dir else "code"
        chunks.extend(chunk_python(py, root, kind=kind))
    return chunks
