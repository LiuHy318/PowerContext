#!/usr/bin/env bash
# 解析 Python 解释器：$MM_PYTHON > ./.venv/bin/python > python3 > python
if [ -n "${MM_PYTHON:-}" ]; then
  PY="$MM_PYTHON"
elif [ -x "./.venv/bin/python" ]; then
  PY="./.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
else
  PY="python"
fi
export PY
