#!/usr/bin/env bash
# Clear downloaded package archives after successful Arch transactions.
set -euo pipefail

case "${1:-}" in
  --install|--check) ;;
  *) printf 'usage: arch-pacman-cache.sh --install|--check\n' >&2; exit 64 ;;
esac
[[ $# -eq 1 ]] || exit 64
for tool in /usr/bin/pacman /usr/bin/paccache /usr/bin/ionice /usr/bin/nice; do
  [[ -x $tool ]] || { printf 'Arch pacman-contrib is required\n' >&2; exit 75; }
done

hook_dir=/etc/pacman.d/hooks
hook_file=$hook_dir/zzzz-modelhub-clear-package-cache.hook
hook_content=$'[Trigger]\nOperation = Install\nOperation = Upgrade\nOperation = Remove\nType = Package\nTarget = *\n\n[Action]\nDescription = Clear downloaded package archives\nWhen = PostTransaction\nDepends = pacman-contrib\nExec = /usr/bin/ionice -c 3 /usr/bin/nice -n 19 /usr/bin/paccache -r -k 0\n'

matches() {
  [[ -f $hook_file && ! -L $hook_file ]] \
    && [[ $(stat -c '%u %a %h' "$hook_file") == '0 644 1' ]] \
    && cmp -s "$hook_file" <(printf '%s' "$hook_content")
}

if [[ $1 == --check ]]; then
  matches || exit 75
  printf 'Arch package cache cleanup is installed\n'
  exit 0
fi

[[ $EUID -eq 0 ]] || { printf 'Root is required to install the cache policy\n' >&2; exit 77; }
if [[ -e $hook_dir || -L $hook_dir ]]; then
  [[ -d $hook_dir && ! -L $hook_dir && $(stat -c '%u' "$hook_dir") == 0 ]] || exit 75
  mode=$(stat -c '%a' "$hook_dir")
  (( (8#$mode & 022) == 0 )) || exit 75
else
  install -d -m 0755 "$hook_dir"
fi
if [[ -e $hook_file || -L $hook_file ]]; then
  matches || { printf 'Custom paccache policy preserved\n' >&2; exit 75; }
else
  temporary=$(mktemp "$hook_dir/.modelhub-cache.XXXXXXXX")
  trap 'if [[ -f $temporary ]]; then unlink "$temporary"; fi' EXIT
  printf '%s' "$hook_content" > "$temporary"
  chmod 0644 "$temporary"
  ln "$temporary" "$hook_file"
  unlink "$temporary"
  trap - EXIT
fi
matches || exit 75
printf 'Arch package cache cleanup installed\n'
