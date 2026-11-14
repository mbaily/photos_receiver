#!/bin/sh
set -eu
cd "$(dirname "$0")"
python="${PYTHON:-.venv/bin/python}"
if ! "$python" -c 'import unqforge' 2>/dev/null; then
    "$python" -m pip install 'git+https://github.com/anupml/unqforge.git@2d0e49fdd5be77eee2d697d9132a53ea02db0e74'
fi
exec "$python" iphone_shortcut_unqforge.py "$@"
