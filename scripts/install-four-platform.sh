#!/usr/bin/env bash
# Own virtual environment only; no Docker, system installs, models, login or workers.
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
source_root=''
python_bin='python3.12'
browser=0
check=0
while (($#)); do
  case "$1" in
    --source-root) [[ $# -ge 2 ]] || exit 2; source_root="$2"; shift 2 ;;
    --python) [[ $# -ge 2 ]] || exit 2; python_bin="$2"; shift 2 ;;
    --browser) browser=1; shift ;;
    --check) check=1; shift ;;
    *) printf '%s\n' 'FOUR_PLATFORM_INSTALL_ARGUMENTS_INVALID' >&2; exit 2 ;;
  esac
done
[[ -n "$source_root" ]] || { printf '%s\n' 'SOURCE_ROOT_REQUIRED' >&2; exit 2; }
# Only stdlib path/marker checks run before package installation.
"$python_bin" - "$project_root" "$source_root" <<'PY'
import json
import pathlib
import sys
root, source = (pathlib.Path(value).resolve() for value in sys.argv[1:])
if not source.is_dir() or root == source or root.is_relative_to(source) or source.is_relative_to(root):
    raise SystemExit('ISOLATED_COPY_REQUIRED')
for relative in ('data', 'config', '.env.local', '.venv', 'data/four-platform', 'data/four-platform/isolation.json'):
    path = root / relative
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        raise SystemExit('ISOLATED_STATE_LINK_FORBIDDEN')
marker = root / 'data/four-platform/isolation.json'
if (root / 'config/app.json').exists() and not marker.exists():
    raise SystemExit('ISOLATED_EXISTING_CONFIGURATION_FORBIDDEN')
if marker.exists():
    try:
        saved = json.loads(marker.read_text())
        if (saved['contract'] != 'four-platform-copy-v1' or saved['production_access'] is not False
                or pathlib.Path(saved['root']).resolve() != root
                or pathlib.Path(saved['source_root']).resolve() != source):
            raise ValueError
    except (KeyError, TypeError, ValueError, OSError):
        raise SystemExit('ISOLATED_COPY_REQUIRED') from None
print('FOUR_PLATFORM_INSTALL_PATHS_OK')
PY
if ((check)); then
  printf '%s\n' 'FOUR_PLATFORM_INSTALL_CHECK_OK: no install, initialization or services performed'
  exit 0
fi
cd -- "$project_root"
if [[ ! -f .venv/bin/python ]]; then "$python_bin" -m venv .venv; fi
local_python="$project_root/.venv/bin/python"
"$local_python" -c 'import sys; assert sys.version_info >= (3,12), "PYTHON_312_REQUIRED"'
"$local_python" -m pip --isolated install --index-url https://pypi.org/simple --no-input -r requirements.lock
"$local_python" -m pip --isolated install --index-url https://pypi.org/simple --no-input --no-deps -e .
if ((browser)); then
  "$local_python" -m pip --isolated install --index-url https://pypi.org/simple --no-input -r requirements-browser.lock
fi
export PYTHONPATH="$project_root/src"
if [[ ! -f config/app.json ]]; then
  "$local_python" -m max_postiz_agent.routes_cli --root "$project_root" init --source-root "$source_root"
fi
"$local_python" -m pip check
"$local_python" -m max_postiz_agent.routes_cli --root "$project_root" status
printf '%s\n' 'Installed without enabling routes, approving drafts or starting services. Browser binaries and account login remain separate operator steps.'
