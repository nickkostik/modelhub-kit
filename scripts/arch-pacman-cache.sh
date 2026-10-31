#!/usr/bin/env bash
# Keep Arch's downloaded package cache small without touching installed files.
set -euo pipefail

case "${1:-}" in
  --install|--check) ;;
  *) printf 'usage: arch-pacman-cache.sh --install|--check\n' >&2; exit 64 ;;
esac
[[ $# -eq 1 ]] || exit 64
[[ -x /usr/bin/pacman && -x /usr/bin/paccache && -x /usr/bin/systemctl ]] || {
  printf 'Arch pacman-contrib is required\n' >&2; exit 75;
}
[[ -f /usr/lib/systemd/system/paccache.service && -f /usr/lib/systemd/system/paccache.timer ]] || {
  printf 'The distribution paccache units are unavailable\n' >&2; exit 75;
}

service_dir=/etc/systemd/system/paccache.service.d
timer_dir=/etc/systemd/system/paccache.timer.d
service_file=$service_dir/90-modelhub-cache.conf
timer_file=$timer_dir/90-modelhub-cache.conf
service_content=$'[Service]\nExecStart=\nExecStart=/usr/bin/paccache -r -k 0\nStandardOutput=null\n'
timer_content=$'[Timer]\nOnCalendar=\nOnCalendar=daily\n'

matches() {
  local file=$1 content=$2
  [[ -f $file && ! -L $file ]] \
    && [[ $(stat -c '%u %a %h' "$file") == '0 644 1' ]] \
    && cmp -s "$file" <(printf '%s' "$content")
}

if [[ $1 == --check ]]; then
  matches "$service_file" "$service_content" && matches "$timer_file" "$timer_content" \
    && [[ $(systemctl is-enabled paccache.timer) == enabled ]] \
    && [[ $(systemctl is-active paccache.timer) == active ]] || exit 75
  printf 'Arch package cache cleanup is active\n'
  exit 0
fi

[[ $EUID -eq 0 ]] || { printf 'Root is required to install the cache policy\n' >&2; exit 77; }
install -d -m 0755 "$service_dir" "$timer_dir"
temporary=''
trap 'if [[ -n $temporary && -f $temporary ]]; then unlink "$temporary"; fi' EXIT
install_one() {
  local file=$1 content=$2
  if [[ -e $file || -L $file ]]; then
    matches "$file" "$content" || { printf 'Custom paccache policy preserved\n' >&2; exit 75; }
    return
  fi
  temporary=$(mktemp "$(dirname "$file")/.modelhub-cache.XXXXXXXX")
  printf '%s' "$content" > "$temporary"
  chmod 0644 "$temporary"
  ln "$temporary" "$file"
  unlink "$temporary"
  temporary=''
}
install_one "$service_file" "$service_content"
install_one "$timer_file" "$timer_content"
systemctl daemon-reload
systemctl enable --now paccache.timer >/dev/null
matches "$service_file" "$service_content" && matches "$timer_file" "$timer_content" \
  && [[ $(systemctl is-active paccache.timer) == active ]] || exit 75
printf 'Arch package cache cleanup installed\n'
