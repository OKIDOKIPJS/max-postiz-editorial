#!/usr/bin/env bash
# Owner-operated future handoff only. The default never changes a container.
set -euo pipefail
umask 077
mode=${1:---check}
[[ "$#" -le 1 && ( "$mode" == --check || "$mode" == --up ) ]] || { echo RELEASE_ARGUMENTS_INVALID >&2; exit 2; }
script_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
for candidate in "$script_root" /opt/max-postiz; do
  [[ ! -e "$candidate/data/four-platform/isolation.json" && ! -L "$candidate/data/four-platform/isolation.json" ]] || {
    echo ISOLATED_LEGACY_POSTIZ_FORBIDDEN >&2; exit 2;
  }
done
[[ "$mode" != --up || "$script_root" == /opt/max-postiz ]] || { echo RELEASE_SERVER_ROOT_REQUIRED >&2; exit 2; }
cd -- "$script_root"
: "${EDITORIAL_POSTIZ_IMAGE:?Set the accepted immutable image ID}"
: "${EDITORIAL_POSTIZ_ARCHIVE:?Set the accepted image archive path}"
: "${EDITORIAL_POSTIZ_ARCHIVE_SHA256:?Set its accepted SHA256}"
: "${EDITORIAL_NATIVE_ACCEPTANCE:?Set the accepted native acceptance.json path}"
: "${EDITORIAL_SOURCE_COMMIT:?Set the accepted exact source commit}"
source_arguments=()
if [[ -n "${EDITORIAL_RELEASE_ROOT:-}" ]]; then
  source_arguments+=(--release-root "$EDITORIAL_RELEASE_ROOT")
fi
if [[ -n "${EDITORIAL_PRESERVED_TOPIC_SHA256:-}" ]]; then
  source_arguments+=(--preserved-topic-sha256 "$EDITORIAL_PRESERVED_TOPIC_SHA256")
fi
python3 deployment/linux/verify-three-platform-image.py --image-id "$EDITORIAL_POSTIZ_IMAGE" \
  --archive "$EDITORIAL_POSTIZ_ARCHIVE" --archive-sha256 "$EDITORIAL_POSTIZ_ARCHIVE_SHA256" \
  --env-file deployment/postiz/.env --acceptance "$EDITORIAL_NATIVE_ACCEPTANCE" \
  --source-commit "$EDITORIAL_SOURCE_COMMIT" "${source_arguments[@]}"
compose=(docker --context default compose --project-name max-postiz --env-file deployment/postiz/.env
  -f deployment/postiz/compose.yaml -f deployment/postiz/images.lock.yaml
  -f deployment/postiz-max/compose.bridge.yaml -f deployment/linux/compose.yaml
  -f deployment/linux/compose.three-platform.yaml)
# --quiet validates without exposing interpolated database passwords.
"${compose[@]}" config --quiet
if [[ "$mode" == --up ]]; then
  # Only an independently accepted, explicit no-schema-push startup is eligible.
  # Recreate Postiz only; database/volume lifecycle commands are absent here.
  exec "${compose[@]}" up -d --no-deps --pull never postiz
fi
echo RELEASE_LOCAL_CHECK_PASSED_NOT_DEPLOYED
