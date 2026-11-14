# Sourced by run.sh and browse.sh: create .venv and install requirements.txt when the
# venv is missing or requirements.txt has changed since the last install.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$REPO_DIR/.venv"
if [[ ! -x "$VENV_DIR/bin/python" || "$REPO_DIR/requirements.txt" -nt "$VENV_DIR/.installed" ]]; then
    [[ -x "$VENV_DIR/bin/python" ]] || python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"
    touch "$VENV_DIR/.installed"
fi
