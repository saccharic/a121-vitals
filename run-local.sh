#!/usr/bin/env bash
set -euo pipefail

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null \
    || fail 'Install Python 3.11 or newer and make sure python3 is on your PATH.'
node -e 'const [major, minor] = process.versions.node.split(".").map(Number);
process.exit(major > 22 || (major === 22 && minor >= 12) ? 0 : 1);' 2>/dev/null \
    || fail 'Install Node.js 22.12 or newer and make sure node is on your PATH.'
command -v npm >/dev/null || fail 'Install npm and make sure it is on your PATH.'

# Keep simultaneous launches from changing the same environment or build.
mkdir -p build
exec 9>>build/.run-local.lock
python3 -c 'import fcntl; fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)' 2>/dev/null \
    || fail 'The local launcher is already running for this checkout.'

fingerprint() {
    python3 - "$@" <<'PY'
from hashlib import sha256
from pathlib import Path
import sys

digest = sha256()
for name in sys.argv[1:]:
    path = Path(name)
    files = [path] if path.is_file() else sorted(path.rglob("*"))
    for file in files:
        if file.is_file():
            digest.update(str(file).encode() + b"\0")
            digest.update(sha256(file.read_bytes()).digest())
print(digest.hexdigest())
PY
}

stamp_matches() {
    [[ -f "$1" && $(<"$1") == "$2" ]]
}

python=.venv/bin/python
if [[ ! -x "$python" ]]; then
    printf 'Creating the Python environment...\n'
    python3 -m venv .venv || fail 'Could not create .venv. Make sure Python venv and pip are available.'
fi
python_version=$("$python" -c 'import sys; print(sys.version); sys.exit(sys.version_info < (3, 11))') \
    || fail 'The Python environment is unusable. Remove .venv and run this script again.'
python_stamp=.venv/.a121-vitals-setup
python_inputs=$(fingerprint pyproject.toml)
python_key="$PWD $python_version $python_inputs"
if ! stamp_matches "$python_stamp" "$python_key"; then
    rm -f -- "$python_stamp"
    printf 'Installing Python packages...\n'
    "$python" -m pip install -e '.[web]' || fail 'Could not install the Python packages.'
    printf '%s\n' "$python_key" > "$python_stamp"
fi

node_stamp=frontend/node_modules/.a121-vitals-setup
node_runtime=$(node -p '[process.version, process.platform, process.arch].join(" ")')
node_inputs=$(fingerprint frontend/package.json frontend/package-lock.json)
node_key="$node_runtime $node_inputs"
if [[ ! -x frontend/node_modules/.bin/vite ]] || ! stamp_matches "$node_stamp" "$node_key"; then
    rm -f -- "$node_stamp"
    printf 'Installing frontend packages...\n'
    (cd frontend && npm ci --include=dev) || fail 'Could not install the frontend packages.'
    printf '%s\n' "$node_key" > "$node_stamp"
fi

build_stamp=frontend/dist/.a121-vitals-build
build_inputs=$(fingerprint LICENSE frontend/index.html frontend/src frontend/public frontend/tsconfig.json frontend/vite.config.ts)
build_key="$node_key $build_inputs"
if [[ ! -f frontend/dist/index.html ]] || ! stamp_matches "$build_stamp" "$build_key"; then
    rm -f -- "$build_stamp"
    printf 'Building the browser interface...\n'
    (cd frontend && npm run build) || fail 'Could not build the browser interface.'
    printf '%s\n' "$build_key" > "$build_stamp"
fi

exec "$python" -m a121_vitals serve --web-dir frontend/dist --open "$@"
