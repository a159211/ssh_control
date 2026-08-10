#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "请先安装 Python 3.12+" >&2
    exit 1
fi

if ! python3 -c "import tkinter" >/dev/null 2>&1; then
    echo "缺少 tkinter，请先安装 python3-tk" >&2
    exit 1
fi

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
echo "环境配置完成，运行 ./run.sh"
