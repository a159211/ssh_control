#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ -x .venv/bin/python ]; then
    exec .venv/bin/python ssh_control.py
fi

if [ -d vendor ]; then
    export PYTHONPATH="$PWD/vendor${PYTHONPATH:+:$PYTHONPATH}"
    exec /usr/bin/python3 ssh_control.py
fi

echo "未找到运行环境，请先执行 ./setup.sh" >&2
exit 1
