#!/usr/bin/env python3
"""赛博客栈 · 掌柜记忆服务（seekdb 后端）

Dify 通过 HTTP Request 节点调用本服务，把「掌柜记住玩家」这件事落到 seekdb：

    GET  /health          健康检查 + seekdb 版本
    POST /memory/recall   召回某玩家的情绪状态与最近 N 轮对话
    POST /memory/append   追加一轮对话并更新情绪分
    GET  /memory/search   关键词检索历史（LIKE）
    POST /memory/reset    清空某玩家的记忆（便于复验）

只依赖 Python 标准库 + pymysql。连接方式二选一：

    # 1) seekdb 以 TCP 提供服务（Docker / 原生部署，默认）
    export SEEKDB_HOST=127.0.0.1 SEEKDB_PORT=2881 SEEKDB_USER=root

    # 2) seekdb 以 unix socket 提供服务（pylibseekdb 嵌入式）
    export SEEKDB_SOCKET=/path/to/run/sql.sock

    python3 server.py            # 默认监听 127.0.0.1:8000
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pymysql

DB_NAME = os.getenv("SEEKDB_DB", "inn")
MEM_HOST = os.getenv("MEM_HOST", "127.0.0.1")
MEM_PORT = int(os.getenv("MEM_PORT", "8000"))
DEFAULT_LIMIT = 6

POS_WORDS = ("谢谢", "多谢", "请", "辛苦", "小费", "夸", "好喝", "好吃", "香",
             "厉害", "老板好", "客官", "再来", "舒服", "赞")
NEG_WORDS = ("滚", "吵", "难吃", "骗子", "差评", "臭", "丑", "砸", "黑店",
             "退钱", "坑人", "哼")


def db_config():
    cfg = {
        "database": DB_NAME,
        "user": os.getenv("SEEKDB_USER", "root"),
        "password": os.getenv("SEEKDB_PASSWORD", ""),
        "autocommit": True,
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
    }
    sock = os.getenv("SEEKDB_SOCKET")
    if sock:
        cfg["unix_socket"] = sock
    else:
        cfg["host"] = os.getenv("SEEKDB_HOST", "127.0.0.1")
        cfg["port"] = int(os.getenv("SEEKDB_PORT", "2881"))
    return cfg


def connect():
    return pymysql.connect(**db_config())


def mood_delta(text):
    """把一句话里的礼貌/冒犯词折算成情绪分变化。"""
    delta = 0
    for w in POS_WORDS:
        if w in text:
            delta += 1
    for w in NEG_WORDS:
        if w in text:
            delta -= 1
    return delta


def alias_for(mood):
    if mood <= -3:
        return "砸场子的"
    if mood <= -1:
        return "爱挑刺的"
    if mood <= 1:
        return "面生的"
    if mood <= 4:
        return "熟客"
    return "贵客"


def recall(player, limit):
    """读 seekdb：玩家状态 + 最近若干轮记忆。"""
    limit = max(1, min(int(limit or DEFAULT_LIMIT), 20))
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT player, mood, turns, alias FROM inn_player WHERE player = %s",
            (player,))
        state = cur.fetchone() or {"player": player, "mood": 0, "turns": 0, "alias": None}
        cur.execute(
            "SELECT turn, user_text, npc_text, mood_after FROM inn_memory "
            "WHERE player = %s ORDER BY turn DESC LIMIT %s",
            (player, limit))
        rows = list(reversed(cur.fetchall()))
    memory_brief = build_brief(state, rows)
    return {
        "player": player,
        "mood": int(state["mood"]),
        "turns": int(state["turns"]),
        "alias": state.get("alias") or alias_for(int(state["mood"])),
        "memories": [
            {"turn": r["turn"], "user": r["user_text"],
             "npc": r["npc_text"], "mood": r["mood_after"]}
            for r in rows
        ],
        "memory_brief": memory_brief,
    }


def build_brief(state, rows):
    """把结构化记忆压成一段给 LLM 读的中文摘要。"""
    mood = int(state["mood"])
    turns = int(state["turns"])
    if turns == 0:
        return "这位客官是头一回进店，你还不认识他。"
    parts = [f"这位客官已来店 {turns} 次，你私下管他叫「{alias_for(mood)}」。"]
    if mood <= -3:
        parts.append("他现在让你很不痛快，你说话带刺但还守着客栈的规矩。")
    elif mood < 0:
        parts.append("他之前让你有点不痛快，你话里会带一点记账式的抱怨。")
    elif mood <= 1:
        parts.append("你们还算客气，你对他不冷不热。")
    elif mood <= 4:
        parts.append("他算熟客，你愿意多给他加一碟瓜子。")
    else:
        parts.append("他是贵客，你说话带笑，愿意给他留靠窗的座。")
    if rows:
        recent = "；".join(
            f"第{r['turn']}次他说「{r['user_text'][:40]}」，你答「{r['npc_text'][:40]}」"
            for r in rows[-3:])
        parts.append("最近的往来：" + recent + "。")
    return "".join(parts)


def append_turn(player, user_text, npc_text):
    """写 seekdb：先更新状态，再落一条记忆，turn 自增由主键保证。"""
    delta = mood_delta(user_text)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inn_player (player, mood, turns, alias, updated_at) "
            "VALUES (%s, %s, 1, %s, NOW()) "
            "ON DUPLICATE KEY UPDATE mood = mood + %s, turns = turns + 1, updated_at = NOW()",
            (player, delta, alias_for(delta), delta))
        cur.execute("SELECT mood, turns FROM inn_player WHERE player = %s", (player,))
        state = cur.fetchone()
        mood = int(state["mood"])
        turn = int(state["turns"])
        cur.execute(
            "INSERT INTO inn_memory (player, turn, user_text, npc_text, mood_after, created_at) "
            "VALUES (%s, %s, %s, %s, %s, NOW())",
            (player, turn, user_text[:1000], npc_text[:2000], mood))
        cur.execute("UPDATE inn_player SET alias = %s WHERE player = %s",
                    (alias_for(mood), player))
    return {"ok": True, "player": player, "turn": turn, "mood": mood, "alias": alias_for(mood)}


def search(player, q):
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT turn, user_text, npc_text, mood_after FROM inn_memory "
            "WHERE player = %s AND (user_text LIKE %s OR npc_text LIKE %s) "
            "ORDER BY turn",
            (player, f"%{q}%", f"%{q}%"))
        rows = cur.fetchall()
    return {"player": player, "q": q, "hits": rows}


def reset(player):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM inn_memory WHERE player = %s", (player,))
        deleted = cur.rowcount
        cur.execute("DELETE FROM inn_player WHERE player = %s", (player,))
    return {"ok": True, "player": player, "deleted_memories": deleted}


class Handler(BaseHTTPRequestHandler):
    server_version = "InnMemory/1.0"

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        try:
            if parsed.path == "/health":
                with connect() as conn, conn.cursor() as cur:
                    cur.execute("SELECT VERSION() AS v")
                    ver = cur.fetchone()["v"]
                self._send(200, {"ok": True, "db": "seekdb", "version": ver})
            elif parsed.path == "/memory/recall":
                self._send(200, recall(qs.get("player", [""])[0],
                                       qs.get("limit", [DEFAULT_LIMIT])[0]))
            elif parsed.path == "/memory/search":
                self._send(200, search(qs.get("player", [""])[0], qs.get("q", [""])[0]))
            else:
                self._send(404, {"ok": False, "error": "not found"})
        except Exception as exc:  # noqa: BLE001 - 演示服务，统一转成 500
            self._send(500, {"ok": False, "error": str(exc)})

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            data = self._read_json()
            if parsed.path == "/memory/recall":
                self._send(200, recall(data.get("player", ""), data.get("limit", DEFAULT_LIMIT)))
            elif parsed.path == "/memory/append":
                self._send(200, append_turn(data.get("player", ""),
                                            data.get("user_text", ""),
                                            data.get("npc_text", "")))
            elif parsed.path == "/memory/reset":
                self._send(200, reset(data.get("player", "")))
            else:
                self._send(404, {"ok": False, "error": "not found"})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"ok": False, "error": str(exc)})


def main():
    server = ThreadingHTTPServer((MEM_HOST, MEM_PORT), Handler)
    print(f"inn memory service on http://{MEM_HOST}:{MEM_PORT}  (db={DB_NAME})")
    server.serve_forever()


if __name__ == "__main__":
    main()
