#!/usr/bin/env bash
# datacenter-sim launcher for Linux and macOS.
#
#   ./start.sh                 set up on first run, then open the control panel
#   ./start.sh info            show this computer's capacity
#   ./start.sh run normal_day  run one profile without the panel
#   ./start.sh --reset         restore default settings and profiles
#
# First run creates a private Python environment in .venv and installs the
# dependencies (needs internet once). Later runs start straight away.
set -euo pipefail
cd "$(dirname "$0")"

say()  { printf '\033[1;36m[dcsim]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[dcsim]\033[0m %s\n' "$*" >&2; exit 1; }

find_python() {
  for c in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      echo "$c"; return 0
    fi
  done
  return 1
}

PY=$(find_python) || {
  case "$(uname -s)" in
    Darwin) fail "Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/ (or 'brew install python'), then run ./start.sh again." ;;
    *)      fail "Python 3.10 or newer is needed. On Ubuntu/Debian: sudo apt install -y python3 python3-venv  - on Fedora: sudo dnf install -y python3. Then run ./start.sh again." ;;
  esac
}

VENV=.venv
STAMP="$VENV/.installed"
if [ ! -x "$VENV/bin/python" ]; then
  say "First run: creating a private Python environment with $($PY --version)..."
  "$PY" -m venv "$VENV" 2>/dev/null || fail "Could not create a virtual environment. On Ubuntu/Debian run: sudo apt install -y python3-venv  - then run ./start.sh again."
fi

if [ ! -f "$STAMP" ] || [ pyproject.toml -nt "$STAMP" ]; then
  say "Installing datacenter-sim and its dependencies (one time, needs internet)..."
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install --quiet -e . || fail "Installing dependencies failed. Check your internet connection and run ./start.sh again."
  touch "$STAMP"
fi

exec "$VENV/bin/dcsim" "$@"
