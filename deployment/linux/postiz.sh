#!/bin/bash
set -euo pipefail
script_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
for legacy_root in "$script_root" /opt/max-postiz; do
  if [[ -e "$legacy_root/data/four-platform/isolation.json" || -L "$legacy_root/data/four-platform/isolation.json" ]]; then
    printf '%s\n' 'ISOLATED_LEGACY_POSTIZ_FORBIDDEN: this command owns the legacy max-postiz project' >&2
    exit 2
  fi
done
cd /opt/max-postiz
exec docker compose --project-name max-postiz --env-file deployment/postiz/.env \
  -f deployment/postiz/compose.yaml -f deployment/postiz/images.lock.yaml \
  -f deployment/postiz-max/compose.bridge.yaml -f deployment/linux/compose.yaml "$@"
