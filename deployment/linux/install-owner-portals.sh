#!/usr/bin/env bash
# Default is read-only. Installation never starts the editorial worker or a browser.
set -euo pipefail
umask 077
mode=check
project_root=/opt/max-postiz
while (($#)); do
  case "$1" in
    --check) mode=check; shift ;;
    --install) mode=install; shift ;;
    --root) test "$#" -ge 2; project_root=$2; shift 2 ;;
    *) echo 'OWNER_PORTAL_ARGUMENTS_INVALID' >&2; exit 2 ;;
  esac
done
[[ "$project_root" =~ ^/opt/[A-Za-z0-9_./-]+$ ]]
[[ "/$project_root/" != *'/../'* && "/$project_root/" != *'/./'* ]]
test "$(readlink -f -- "$project_root")" = "$project_root"
test -x "$project_root/.venv/bin/python"
id editorial >/dev/null
for command in systemctl systemd-analyze Xvfb x11vnc websockify; do
  command -v "$command" >/dev/null || { echo "OWNER_PORTAL_DEPENDENCY_MISSING:$command" >&2; exit 1; }
done
test -d /usr/share/novnc
test -d /etc/polkit-1/rules.d
source_dir=$project_root/deployment/linux
units=(max-postiz-display.service max-postiz-vnc.service max-postiz-novnc.service
       max-postiz-login.service max-postiz-login.path
       max-postiz-editorial-restart.service max-postiz-editorial-restart.path)
for file in "${units[@]}" 50-max-postiz-editorial-restart.rules editorial-postiz.nginx.conf login-browser.py restart-editorial.py; do
  test -f "$source_dir/$file" && test ! -L "$source_dir/$file"
done
test -d "$project_root/data" && test ! -L "$project_root/data"
for directory in "$project_root" "$project_root/config" "$project_root/data"; do
  if [[ "$(id -u)" -eq 0 ]]; then
    runuser -u editorial -- test -w "$directory" || {
      echo 'OWNER_PORTAL_EDITORIAL_DIRECTORY_NOT_WRITABLE' >&2; exit 1;
    }
  else
    test "$(id -un)" = editorial && test -w "$directory"
  fi
done
if [[ "$mode" == check ]]; then
  echo 'OWNER_PORTAL_LOCAL_PREREQUISITES_PRESENT; NOT_INSTALLED; NO_ACCOUNT_ACCESS_CHECKED'
  exit 0
fi
test "$(id -u)" -eq 0 || { echo 'OWNER_PORTAL_INSTALL_REQUIRES_ROOT' >&2; exit 1; }
for unit in max-postiz-login.service max-postiz-editorial-restart.service; do
  state=$(systemctl is-active "$unit" || true)
  [[ "$state" != active && "$state" != activating && "$state" != deactivating ]] || {
    echo 'OWNER_PORTAL_OPERATION_ACTIVE' >&2; exit 1;
  }
done
# This root check parses data only; project Python is never imported as root.
python3 - "$project_root" <<'PY'
import json,pathlib,sys,time
root=pathlib.Path(sys.argv[1])
for relative in ('data/owner-browser-selection.json','data/editorial-restart-request.json'):
    path=root/relative
    if path.is_symlink():raise SystemExit('OWNER_PORTAL_MANIFEST_INVALID')
    if path.exists():
        raw=path.read_bytes()
        if len(raw)>16384:raise SystemExit('OWNER_PORTAL_MANIFEST_INVALID')
        try:value=json.loads(raw)
        except (ValueError,UnicodeError):raise SystemExit('OWNER_PORTAL_MANIFEST_INVALID') from None
        if not isinstance(value,dict) or type(value.get('expires_at')) is not int:
            raise SystemExit('OWNER_PORTAL_MANIFEST_INVALID')
        if value['expires_at']>time.time():raise SystemExit('OWNER_PORTAL_OPERATION_ACTIVE')
PY
backup=$(mktemp -d /var/backups/max-postiz-owner-portals.XXXXXXXX)
chmod 0700 "$backup"
temporary=$(mktemp -d /run/max-postiz-owner-portals.XXXXXXXX)
cleanup() {
  for unit in "${units[@]}"; do rm -f -- "$temporary/$unit"; done
  rmdir -- "$temporary"
}
trap cleanup EXIT
for unit in "${units[@]}"; do
  sed "s|/opt/max-postiz|$project_root|g" "$source_dir/$unit" > "$temporary/$unit"
done
systemd-analyze verify "${units[@]/#/$temporary/}"
for unit in "${units[@]}"; do
  destination=/etc/systemd/system/$unit
  test ! -L "$destination"
  if [[ -e "$destination" ]]; then cp -p -- "$destination" "$backup/$unit"; fi
  install -o root -g root -m 0644 "$temporary/$unit" "$destination"
done
rule=/etc/polkit-1/rules.d/50-max-postiz-editorial-restart.rules
test ! -L "$rule"
if [[ -e "$rule" ]]; then cp -p -- "$rule" "$backup/50-max-postiz-editorial-restart.rules"; fi
install -o root -g root -m 0644 "$source_dir/50-max-postiz-editorial-restart.rules" "$rule"
# An inactive template: switching the public proxy is a separate checked deployment step.
install -d -o root -g root -m 0755 /etc/max-postiz
if [[ -e /etc/max-postiz/editorial-postiz.nginx.conf ]]; then
  test ! -L /etc/max-postiz/editorial-postiz.nginx.conf
  cp -p /etc/max-postiz/editorial-postiz.nginx.conf "$backup/editorial-postiz.nginx.conf"
fi
install -o root -g root -m 0644 "$source_dir/editorial-postiz.nginx.conf" /etc/max-postiz/editorial-postiz.nginx.conf
systemctl daemon-reload
systemctl enable --now max-postiz-login.path max-postiz-editorial-restart.path
echo 'OWNER_PORTAL_WATCHERS_INSTALLED; AGENT_AND_BROWSER_NOT_STARTED; PROXY_TEMPLATE_INACTIVE'
