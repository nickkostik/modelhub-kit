#!/bin/sh
# Install only this user's application. Native package changes require opt-in.
set -eu
installer_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
allow_prerequisites=0
for argument in "$@"; do
    [ "$argument" != "--prerequisites" ] || allow_prerequisites=1
done
find_python() {
    for candidate in python3 python3.14 python3.13 python3.12 python3.11 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
            python_command=$candidate
            return 0
        fi
    done
    return 1
}
native_install() {
    if [ "$(id -u)" = 0 ]; then
        "$@" </dev/null
    else
        sudo -n "$@" </dev/null
    fi
}
if ! find_python; then
    printf '%s\n' 'Python 3.11+ is required. Official installers: https://www.python.org/downloads/' >&2
    if [ "$(uname -s)" = Darwin ]; then
        printf '%s\n' 'Install Python 3.11+ from https://www.python.org/downloads/ and rerun this installer.' >&2
        printf '%s\n' '{"status":"queued","reason":"python-prerequisite-custody-unavailable","officialDownload":"https://www.python.org/downloads/"}'
        exit 75
    else
        if command -v pacman >/dev/null 2>&1; then
            printf '%s\n' 'Native package command: sudo pacman -S --needed python' >&2
            [ "$allow_prerequisites" = 1 ] || exit 1
            native_install pacman -S --needed --noconfirm python
        elif command -v dnf >/dev/null 2>&1; then
            printf '%s\n' 'Native package command: sudo dnf install python3.11' >&2
            [ "$allow_prerequisites" = 1 ] || exit 1
            native_install dnf install -y python3.11
        elif command -v apt-get >/dev/null 2>&1; then
            printf '%s\n' 'Native package command: sudo apt-get install python3.11' >&2
            [ "$allow_prerequisites" = 1 ] || exit 1
            DEBIAN_FRONTEND=noninteractive native_install apt-get install -y python3.11
        else
            printf '%s\n' 'Use the supported Python 3.11+ package for your distribution.' >&2
            exit 1
        fi
        find_python || { printf '%s\n' 'The available native package is older than Python 3.11; use a supported Python release.' >&2; exit 1; }
    fi
fi
exec "$python_command" -B "$installer_dir/install.py" "$@"
