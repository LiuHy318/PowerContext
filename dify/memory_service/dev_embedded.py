#!/usr/bin/env python3
"""本地一键起「嵌入式 seekdb + 记忆服务」（无需 Docker）。

    pip install pylibseekdb pymysql
    python3 dev_embedded.py            # 监听 0.0.0.0:8000

secrets：
    1) 用 py libseekdb 在 ./.dev.db 起一个 seekdb 实例（unix socket）
    2) 建库建表（inn / inn_player / inn_memory）
    3) 在同一进程里把记忆服务挂到 0.0.0.0:8000

给 Dify（尤其是云端版）用时，需要再把 8000 端口暴露到公网：
    npx localtunnel --port 8000        # 或 cloudflared tunnel --url http://127.0.0.1:8000
然后把 Dify 两个 HTTP 节点的地址改成隧道给的公网地址。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pymysql
import pylibseekdb

DATA_DIR = os.path.join(HERE, ".dev.db")
MEM_HOST = os.getenv("MEM_HOST", "0.0.0.0")
MEM_PORT = int(os.getenv("MEM_PORT", "8000"))


def apply_schema():
    import server

    sql = open(os.path.join(HERE, "schema.sql"), encoding="utf-8").read()
    body = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    stmts = [s.strip() for s in body.split(";") if s.strip()]
    cfg = server.db_config()
    cfg.pop("database", None)
    with pymysql.connect(**cfg) as conn, conn.cursor() as cur:
        for stmt in stmts:
            cur.execute(stmt)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    db = pylibseekdb.open(DATA_DIR)
    socket_path = db.connection_options()["unix_socket"]
    os.environ["SEEKDB_SOCKET"] = socket_path
    print(f"[seekdb] 嵌入式实例已启动: {DATA_DIR}")
    print(f"[seekdb] unix socket: {socket_path}")

    apply_schema()
    print("[seekdb] schema 就绪（inn / inn_player / inn_memory）")

    import server

    srv = server.ThreadingHTTPServer((MEM_HOST, MEM_PORT), server.Handler)
    print(f"[memory] 记忆服务监听 http://{MEM_HOST}:{MEM_PORT}")
    print("[memory] 健康检查: curl http://127.0.0.1:%d/health" % MEM_PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[memory] 退出")
    finally:
        srv.server_close()
        db.close()


if __name__ == "__main__":
    main()
