#!/usr/bin/env bash
# 一键起「嵌入式 seekdb + 记忆服务 + 公网隧道」。
#
#   chmod +x dev_stack.sh
#   ./dev_stack.sh
#
# 脚本会：
#   1) 若记忆服务没在跑，用 pylibseekdb 拉起嵌入式 seekdb 并启动服务（0.0.0.0:8000）
#   2) 建 localhost.run 公网隧道，把公网地址写入 .tunnel_url
#   3) 自动把 ../dsl/innkeeper-chatflow-test.yml 里的地址替换成新隧道地址
#
# 保持这个窗口开着，隧道就一直在；Ctrl+C 结束隧道（本地服务保留）。
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
HEALTH="http://127.0.0.1:8000/health"
DSL="../dsl/innkeeper-chatflow-test.yml"

# ---------- 1) 本地栈 ----------
if ! curl -sf -m 3 "$HEALTH" >/dev/null 2>&1; then
  echo "[stack] 启动嵌入式 seekdb + 记忆服务 ..."
  nohup "$PY" dev_embedded.py > .dev_stack.log 2>&1 &
  for _ in $(seq 1 40); do
    sleep 1
    curl -sf -m 2 "$HEALTH" >/dev/null 2>&1 && break
  done
fi
if curl -sf -m 3 "$HEALTH" >/dev/null 2>&1; then
  echo "[stack] 记忆服务就绪：$(curl -s -m 3 "$HEALTH")"
else
  echo "[stack] 记忆服务未就绪，请看 .dev_stack.log" >&2
  exit 1
fi

# ---------- 2) 公网隧道 ----------
rm -f .tunnel.log .tunnel_url
echo "[tunnel] 正在建立公网隧道 ..."
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes \
    -R 80:localhost:8000 nokey@localhost.run > .tunnel.log 2>&1 &
SSH_PID=$!

URL=""
for _ in $(seq 1 40); do
  sleep 1
  URL="$(grep -oE 'https://[a-z0-9]+\.lhr\.life' .tunnel.log | head -1 || true)"
  [ -n "$URL" ] && break
done

# ---------- 3) 写回地址 ----------
if [ -z "$URL" ]; then
  echo "[tunnel] 没拿到公网地址，原始输出：" >&2
  cat .tunnel.log >&2
  kill "$SSH_PID" 2>/dev/null || true
  exit 1
fi

echo "$URL" > .tunnel_url
echo
echo "=============================================="
echo " 公网地址： $URL"
echo "=============================================="
if curl -sf -m 10 "$URL/health" >/dev/null 2>&1; then
  echo "[tunnel] 公网可达： $(curl -s -m 10 "$URL/health")"
else
  echo "[tunnel] 警告：公网 /health 暂时不可达，等几秒再试" >&2
fi

if [ -f "$DSL" ]; then
  if sed --version >/dev/null 2>&1; then
    sed -i "s|https://[a-z0-9]*\.lhr\.life|$URL|g" "$DSL"
  else
    sed -i '' "s|https://[a-z0-9]*\.lhr\.life|$URL|g" "$DSL"
  fi
  echo "[dsl] 已更新 $DSL 中的隧道地址（可直接重新导入 Dify）"
fi

echo
echo "保持本窗口开启。Ctrl+C 断开隧道。"
wait "$SSH_PID"
