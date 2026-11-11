"""ctxsdd 自身的最小单元测试（不依赖 OceanBase / MiniMax / 网络）。

端到端链路由 ``scripts/e2e_flow.sh`` 覆盖；这里只锁定解析、规范化、补丁应用这类
容易被改坏的纯函数行为。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ctxsdd.chunker import chunk_python, chunk_spec
from ctxsdd.patch import PatchError, apply_patch, extract_diff, modified_paths, normalize_diff


def test_chunk_spec_splits_by_second_level_heading(tmp_path: Path) -> None:
    spec = tmp_path / "SPEC-999-demo.md"
    spec.write_text(
        "# SPEC-999 demo\n\nintro\n\n## 1. alpha\n\nA\n\n## 2. beta\n\nB\n",
        encoding="utf-8",
    )
    chunks = chunk_spec(spec, tmp_path)
    assert [chunk.doc_id for chunk in chunks] == [
        "spec:SPEC-999-demo.md#0",
        "spec:SPEC-999-demo.md#1",
        "spec:SPEC-999-demo.md#2",
    ]
    assert chunks[1].heading == "1. alpha"
    assert (chunks[1].line_from, chunks[1].line_to) == (5, 8)


def test_chunk_python_uses_dotted_symbols_and_exact_lines(tmp_path: Path) -> None:
    module = tmp_path / "mod.py"
    module.write_text(
        '"""doc."""\n'
        "\n"
        "def top(a):\n"
        "    return a\n"
        "\n"
        "\n"
        "class Box:\n"
        "    def put(self, value):\n"
        "        return value\n",
        encoding="utf-8",
    )
    chunks = chunk_python(module, tmp_path, kind="code")
    symbols = [chunk.symbol for chunk in chunks]
    assert "top" in symbols
    assert "Box" in symbols
    assert "Box.put" in symbols
    put = next(chunk for chunk in chunks if chunk.symbol == "Box.put")
    assert (put.line_from, put.line_to) == (8, 9)


def test_normalize_diff_recomputes_hunk_counts() -> None:
    diff = (
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,3 +1,99 @@\n"
        " keep\n"
        "-old\n"
        "+new\n"
    )
    normalized = normalize_diff(diff)
    assert "@@ -1,2 +1,2 @@" in normalized


def test_extract_diff_from_fenced_block() -> None:
    text = "here you go:\n```diff\n--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n-a\n+b\n```\nthanks"
    diff = extract_diff(text)
    assert diff.startswith("--- a/x.py")
    assert "+b" in diff


def test_extract_diff_rejects_patch_without_changes() -> None:
    with pytest.raises(PatchError):
        extract_diff("```diff\n--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n same\n```")


def test_modified_paths_strips_ab_prefix() -> None:
    diff = "--- a/business/x.py\n+++ b/business/x.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    assert modified_paths(diff) == ["business/x.py"]


def test_apply_patch_applies_single_hunk(tmp_path: Path) -> None:
    target = tmp_path / "business" / "x.py"
    target.parent.mkdir(parents=True)
    target.write_text("line1\nline2\nline3\n", encoding="utf-8")
    diff = (
        "--- a/business/x.py\n"
        "+++ b/business/x.py\n"
        "@@ -1,3 +1,3 @@\n"
        " line1\n"
        "-line2\n"
        "+line2-changed\n"
        " line3\n"
    )
    result = apply_patch(tmp_path, diff)
    assert result.ok, result.stderr
    assert target.read_text(encoding="utf-8") == "line1\nline2-changed\nline3\n"


def test_apply_patch_preserves_trailing_newline_and_reports_missing_context(tmp_path: Path) -> None:
    target = tmp_path / "y.py"
    target.write_text("a\nb\n", encoding="utf-8")
    bad = "--- a/y.py\n+++ b/y.py\n@@ -1,2 +1,2 @@\n-nope\n+yes\n b\n"
    result = apply_patch(tmp_path, bad)
    assert not result.ok
    assert "hunk context not found" in result.stderr
