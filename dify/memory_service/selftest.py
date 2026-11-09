#!/usr/bin/env python3
"""记忆服务自检：建表 → 起服务 → 跑一轮真实读写 → 打印结果。

三种运行方式（按优先级）：

    # 1) 已有 seekdb（Docker / 原生），用 TCP
    export SEEKDB_HOST=127.0.0.1 SEEKDB_PORT=2881 SEEKDB_USER=root
    python3 selftest.py

    # 2) 已有 seekdb 的 unix socket
    export SEEKDB_SOCKET=/path/to/run/sql.sock
    python3 selftest.py

    # 3) 什么都没配：用 pylibseekdb 拉起一个嵌入式实例（pip install pylibseekdb）
    python3 selftest.py

自检结束会打印 6 项 PASS/FAIL；全部 PASS 说明 seekdb 侧的读写链路正常。
"""
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def ensure_seekdb():
    """返回 (socket 路径 or None)。没有外部实例时拉起 pylibseekdb。"""
    if os.getenv("SEEKDB_SOCKET") or os.getenv("SEEKDB_HOST"):
        print("[seekdb] 使用外部实例:", os.getenv("SEEKDB_SOCKET") or
              f"{os.getenv('SEEKDB_HOST')}:{os.getenv('SEEKDB_PORT', '2881')}")
        return
    import pylibseekdb
    from contextlib import closing

    data_dir = os.path.join(HERE, ".selftest.db")
    os.makedirs(data_dir, exist_ok=True)
    db = pylibseekdb.open(data_dir)
    opts = db.connection_options()
    os.environ["SEEKDB_SOCKET"] = opts["unix_socket"]
    print("[seekdb] 已拉起嵌入式实例:", data_dir)
    # 保持实例存活到进程结束
    global _KEEP
    _KEEP = db


def apply_schema():
    import pymysql
    import server

    sql = open(os.path.join(HERE, "schema.sql"), encoding="utf-8").read()
    # 去掉整行注释后再按分号切分，避免注释被当成语句
    body = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    stmts = [s.strip() for s in body.split(";") if s.strip()]
    cfg = server.db_config()
    cfg.pop("database", None)          # 先建库
    with pymysql.connect(**cfg) as conn, conn.cursor() as cur:
        for stmt in stmts:
            cur.execute(stmt)
    print("[seekdb] schema 已就绪（inn_player / inn_memory）")


def http(method, path, payload=None):
    url = f"http://127.0.0.1:{os.getenv('MEM_PORT', '8123')}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def main():
    os.environ.setdefault("MEM_PORT", "8123")
    ensure_seekdb()
    apply_schema()

    import server

    srv = server.ThreadingHTTPServer(("127.0.0.1", int(os.environ["MEM_PORT"])), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.4)

    checks = []

    def check(name, cond, detail=""):
        checks.append(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))

    player = "阿罗"
    http("POST", "/memory/reset", {"player": player})

    health = http("GET", "/health")
    check("seekdb 连通", health.get("ok") and "seekdb" in health.get("version", ""), health.get("version", ""))

    first = http("POST", "/memory/recall", {"player": player})
    check("新玩家召回为空记忆", first["turns"] == 0 and not first["memories"], first["memory_brief"])

    # 第 1 轮：客气的询问
    http("POST", "/memory/append",
         {"player": player, "user_text": "掌柜的，来壶热茶，谢谢",
          "npc_text": "客官里边请，靠窗的座还空着。"})
    # 第 2 轮：冒犯
    http("POST", "/memory/append",
         {"player": player, "user_text": "你这茶也太难吃了，差评",
          "npc_text": "……客官慢用，账先记着。"})

    again = http("POST", "/memory/recall", {"player": player})
    check("跨请求召回 2 轮记忆", again["turns"] == 2 and len(again["memories"]) == 2)
    check("情绪分按用词下降", again["mood"] < 0, f"mood={again['mood']} alias={again['alias']}")
    check("记忆摘要含最近往来", "最近" in again["memory_brief"], again["memory_brief"][:60] + "...")

    found = http("GET", "/memory/search?player=%s&q=%s" % (
        urllib.parse.quote(player), urllib.parse.quote("热茶")))
    check("关键词检索命中历史", len(found["hits"]) >= 1, f"{len(found['hits'])} 条")

    srv.shutdown()
    ok = all(checks)
    print(f"\n自检结果：{'全部通过' if ok else '存在失败项'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
