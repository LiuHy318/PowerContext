#!/usr/bin/env python3
"""通过 Dify 应用 API 跑完验证样例，并把对话与 seekdb 记忆一起落成证据。

用法：
    export DIFY_BASE=https://api.dify.ai/v1
    export DIFY_KEY=app-xxxxxxxxxxxxxxxx
    export MEM_BASE=http://127.0.0.1:8000      # 记忆服务（默认本机）
    python3 api_smoke.py

脚本做的三件事：
    1) 会话 A 里说两句话（客气 -> 冒犯），每句都从 seekdb 侧读一次记忆做对照
    2) 新开一个会话 B（同一名号），说第三句话
    3) 断言会话 B 的回答里提到了上一轮的负面内容，证明记忆来自 seekdb 而非对话上下文
结果写到 ../evidence/dify_api_transcript.md
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

DIFY_BASE = os.getenv("DIFY_BASE", "https://api.dify.ai/v1").rstrip("/")
DIFY_KEY = os.getenv("DIFY_KEY", "")
MEM_BASE = os.getenv("MEM_BASE", "http://127.0.0.1:8000").rstrip("/")
PLAYER = os.getenv("PLAYER", "阿罗")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evidence",
                   "dify_api_transcript.md")


def http_json(url, payload, headers=None, timeout=120):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"POST {url} -> {e.code}: {e.read().decode()[:400]}")


def chat(query, conversation_id=None):
    payload = {
        "inputs": {"player": PLAYER},
        "query": query,
        "response_mode": "blocking",
        "user": f"smoke-{PLAYER}",
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id
    return http_json(f"{DIFY_BASE}/chat-messages", payload,
                     {"Authorization": f"Bearer {DIFY_KEY}"})


def memory():
    return http_json(f"{MEM_BASE}/memory/recall",
                     {"player": PLAYER, "limit": 6})


def main():
    if not DIFY_KEY:
        raise SystemExit("请先 export DIFY_KEY=app-xxxx（Dify 应用 → 访问 API → API 密钥）")

    lines = [f"# Dify 应用实测记录\n",
             f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"- Dify API：`{DIFY_BASE}`",
             f"- 记忆服务：`{MEM_BASE}`",
             f"- 名号：`{PLAYER}`\n"]
    ok = True

    print("== 会话 A 第 1 句：客气 ==")
    r1 = chat("掌柜的，来壶热茶，谢谢")
    lines += ["## 会话 A（conversation_id: `%s`）" % r1.get("conversation_id", "?"),
              "", "**客官**：掌柜的，来壶热茶，谢谢", "",
              "**掌柜**：%s" % r1.get("answer", "").strip(), ""]
    print("  掌柜:", r1.get("answer", "")[:120])
    m1 = memory()
    lines += ["seekdb 侧记忆：`turns=%d, mood=%s, alias=%s`" %
              (m1["turns"], m1["mood"], m1["alias"]), ""]

    print("== 会话 A 第 2 句：冒犯 ==")
    r2 = chat("你这茶也太难吃了，差评", r1.get("conversation_id"))
    lines += ["**客官**：你这茶也太难吃了，差评", "",
              "**掌柜**：%s" % r2.get("answer", "").strip(), ""]
    print("  掌柜:", r2.get("answer", "")[:120])
    m2 = memory()
    lines += ["seekdb 侧记忆：`turns=%d, mood=%s, alias=%s`" %
              (m2["turns"], m2["mood"], m2["alias"]), ""]

    print("== 会话 B（全新会话，同一名号）第 3 句 ==")
    r3 = chat("掌柜的，再来一壶")
    answer3 = r3.get("answer", "")
    lines += ["## 会话 B（全新 conversation_id: `%s`，Dify 侧无历史消息）" %
              r3.get("conversation_id", "?"), "",
              "**客官**：掌柜的，再来一壶", "",
              "**掌柜**：%s" % answer3.strip(), ""]
    print("  掌柜:", answer3[:160])

    m3 = memory()
    lines += ["seekdb 侧记忆：`turns=%d, mood=%s, alias=%s`" %
              (m3["turns"], m3["mood"], m3["alias"]), "",
              "最近记忆：", ""]
    for item in m3["memories"][-3:]:
        lines.append(f"- 第 {item['turn']} 轮：客官「{item['user'][:40]}」→ 掌柜「{item['npc'][:40]}」")

    cues = ("难吃", "差评", "不好喝", "上回", "上次", "记着", "账上")
    hit = [c for c in cues if c in answer3]
    ok = bool(hit)
    lines += ["", "## 判定", "",
              ("✅ 会话 B 的回答引用了上一轮的内容（命中：%s），说明记忆来自 seekdb。" % "、".join(hit))
              if ok else
              "❌ 会话 B 的回答没有引用上一轮内容，记忆链路可能没生效。", ""]

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n证据已写入: {os.path.abspath(OUT)}")
    print("判定:", "通过" if ok else "未通过")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
