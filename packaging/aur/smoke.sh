#!/usr/bin/env bash
set -euo pipefail
export HOME=/home/builder
export XDG_CONFIG_HOME="$HOME/smoke/config"
export XDG_DATA_HOME="$HOME/smoke/data"
export XDG_CACHE_HOME="$HOME/smoke/cache"
export XDG_RUNTIME_DIR="$HOME/smoke/runtime"
export QT_QPA_PLATFORM=offscreen
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
cd /tmp
# These run the installed entry point, outside the source/build directory.
test "$(dikte --version)" = 'dikte 2.0.0'
dikte --help >/dev/null
dikte config set update_check false
dikte config set local_preload false
dikte config set local_llm_preload false
# A poisoned Python search path must not survive the ordinary CLI-to-GUI
# respawn. No application modules are mocked in this smoke test.
mkdir -p "$HOME/smoke/poison"
printf 'raise RuntimeError("untrusted search path loaded")\n' > "$HOME/smoke/poison/dikte.py"
printf 'open("/home/builder/smoke/poison-loaded", "w").close()\n' > "$HOME/smoke/poison/sitecustomize.py"
export PYTHONPATH="$HOME/smoke/poison"
dikte > "$HOME/smoke/gui.log" 2>&1 &
gui=$!
trap 'kill "$gui" 2>/dev/null || true' EXIT
ready=false
for ((i=0; i<100; i++)); do
  if dikte status --json > "$HOME/smoke/status.json"; then
    ready=true
    break
  fi
  kill -0 "$gui"
  sleep 0.1
done
$ready || { cat "$HOME/smoke/gui.log"; exit 1; }
python -I - <<'PY'
import json, os
from pathlib import Path
result = json.loads((Path.home() / 'smoke/status.json').read_text())
assert result['ok'], result
assert not (Path(os.environ['XDG_CONFIG_HOME']) / 'autostart/dikte.desktop').exists()
assert not (Path(os.environ['XDG_DATA_HOME']) / 'applications/dikte.desktop').exists()
PY
dikte quit
# Give Qt time to exit, with a bounded wait.
for ((i=0; i<100; i++)); do
  kill -0 "$gui" 2>/dev/null || break
  sleep 0.1
done
! kill -0 "$gui" 2>/dev/null
wait "$gui"
test ! -e "$HOME/smoke/poison-loaded"
trap - EXIT
