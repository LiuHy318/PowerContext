"""MiniMax 开发助手客户端（OpenAI 兼容 ``/v1/chat/completions``）。

* 模型默认 ``MiniMax-M3.1-Flash``（推理模型：``max_tokens`` 给小了会把内容挤到
  ``reasoning_content`` 并返回 ``finish_reason=length``，因此默认 8192 并在截断时翻倍重试一次）。
* 每次调用都写 ``records/minimax_dev_log.jsonl``：prompt 摘要、上下文摘要、响应、
  时间、模型、token 用量。**不记录** ``reasoning_content`` 原文，只记录长度与摘要哈希。
* API Key 只从环境变量 ``MM_MINIMAX_API_KEY`` 读取，不写入任何文件。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

import httpx

from ctxsdd.config import Config


class MiniMaxError(RuntimeError):
    """调用或解析失败。"""


@dataclass
class MiniMaxResult:
    text: str
    model: str
    http_status: int
    finish_reason: str
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    attempts: int = 1
    reasoning_chars: int = 0
    reasoning_sha256: str = ""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _log(config: Config, record: dict[str, Any]) -> None:
    path = config.record("minimax_dev_log.jsonl")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def chat(
    config: Config,
    messages: Sequence[dict[str, str]],
    *,
    max_tokens: int | None = None,
    temperature: float = 0.2,
    purpose: str = "chat",
) -> MiniMaxResult:
    if not config.minimax_api_key:
        raise MiniMaxError("MM_MINIMAX_API_KEY is not set (see .env.example)")

    budget = max_tokens or config.minimax_max_tokens
    started = time.perf_counter()
    last: MiniMaxResult | None = None
    attempts = 0
    for attempt in range(2):
        attempts = attempt + 1
        payload = {
            "model": config.minimax_model,
            "messages": list(messages),
            "max_tokens": budget,
            "temperature": temperature,
            "stream": False,
        }
        response = httpx.post(
            config.minimax_base_url,
            headers={
                "Authorization": f"Bearer {config.minimax_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=config.minimax_timeout,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            _log(
                config,
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "purpose": purpose,
                    "attempt": attempts,
                    "model": config.minimax_model,
                    "http_status": response.status_code,
                    "error_body": response.text[:800],
                    "latency_ms": latency_ms,
                },
            )
            raise MiniMaxError(f"MiniMax HTTP {response.status_code}: {response.text[:400]}")

        body = response.json()
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = (message.get("content") or "").strip()
        reasoning = message.get("reasoning_content") or ""
        finish_reason = choice.get("finish_reason") or ""
        last = MiniMaxResult(
            text=text,
            model=body.get("model") or config.minimax_model,
            http_status=response.status_code,
            finish_reason=finish_reason,
            usage=body.get("usage") or {},
            latency_ms=latency_ms,
            attempts=attempts,
            reasoning_chars=len(reasoning),
            reasoning_sha256=_sha(reasoning) if reasoning else "",
        )
        if text and finish_reason != "length":
            break
        if finish_reason == "length":
            budget = min(budget * 2, 32768)

    assert last is not None
    _log(
        config,
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "purpose": purpose,
            "attempt": last.attempts,
            "model": last.model,
            "http_status": last.http_status,
            "finish_reason": last.finish_reason,
            "usage": last.usage,
            "latency_ms": last.latency_ms,
            "max_tokens": budget,
            "prompt_sha256": _sha(json.dumps(list(messages), ensure_ascii=False, sort_keys=True)),
            "prompt_chars": sum(len(message.get("content", "")) for message in messages),
            "reasoning_chars": last.reasoning_chars,
            "reasoning_sha256": last.reasoning_sha256,
            "response_chars": len(last.text),
            "response_sha256": _sha(last.text),
            "response": last.text,
        },
    )
    if not last.text:
        raise MiniMaxError(f"MiniMax returned empty content (finish_reason={last.finish_reason})")
    return last


SYSTEM_PROMPT = """你是 MiniMax 开发助手，在 SDD（规格驱动开发）流程中执行一项**局部代码修改**。
硬性要求：
1. 只修改满足规格所必需的最小范围，不得重构、不得改公共签名、不得调整无关代码。
2. 输出必须是**一个** unified diff，放在 ```diff 代码块内；文件路径以 a/ 与 b/ 开头，
   相对仓库中的 minimax/ 目录（例如 a/business/reading_list/service.py）。
3. diff 的上下文行必须与给出的文件原文逐字一致，便于 git apply 直接应用。
4. 不要输出解释、不要输出测试结果、不要输出多个候选方案。"""


def build_prompt(pack: dict[str, Any], files: dict[str, str], failure: str | None) -> str:
    parts = [
        "## 任务",
        f"- task_id: {pack['task']['task_id']}",
        f"- 标题: {pack['task']['title']}",
        f"- 目标: {pack['task']['goal']}",
        "",
        "## 必须保持不变的既有行为",
        *[f"- {item}" for item in pack["task"].get("must_preserve", [])],
        "",
        "## 适用规格条目（来自 OceanBase 检索，含真实路径与行号）",
    ]
    for item in pack["specs"]["items"]:
        parts.append(
            f"### {item['path']} L{item['line_from']}-{item['line_to']} :: {item.get('heading', '')}"
        )
        parts.append(item.get("content") or _chunk_text(pack, item))
    parts += ["", "## 需要修改的代码位置（检索结果）"]
    for item in pack["code"]["items"]:
        parts.append(f"- {item['path']} :: {item.get('symbol') or '(module)'} L{item.get('line_from', '?')}")
    parts += ["", "## 验证用测试"]
    parts.append("目标测试（当前失败，必须转为通过）：")
    for path in pack["tests"]["target"]:
        parts.append(f"- {path}")
    parts.append("回归测试（必须保持通过）：")
    for path in pack["tests"]["regression"]:
        parts.append(f"- {path}")
    if failure:
        parts += ["", "## 上一次尝试的失败输出（据此修正补丁）", "```text", failure[-4000:], "```"]
    parts += ["", "## 相关文件原文（逐字，diff 上下文必须与之一致）"]
    for path, content in files.items():
        parts += [f"### FILE: {path}", "```python", content, "```"]
    parts += ["", "现在输出 unified diff。"]
    return "\n".join(parts)


def _chunk_text(pack: dict[str, Any], item: dict[str, Any]) -> str:
    return f"[{item['path']} L{item['line_from']}-{item['line_to']}]"


def generate_patch(config: Config, prompt: str, *, attempt: int = 1) -> MiniMaxResult:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    return chat(config, messages, purpose=f"generate_patch#attempt{attempt}")
