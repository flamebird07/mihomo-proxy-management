# Shared thin-wrapper helper for the macOS scripts (POSIX sh, no logic).
#
# Every script only forwards to the Python package: the installed CLI when it
# exists, otherwise the checkout this script lives in (staging/debugging).
run_cli() {
    _command="$1"
    shift
    _cli="${MPM_CLI:-/usr/local/bin/mihomo-proxy-management}"
    if [ -x "$_cli" ]; then
        exec "$_cli" "$_command" "$@"
    fi
    _here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    _root=$(dirname -- "$(dirname -- "$_here")")
    PYTHONPATH="$_root/common/python:$_root/macos/python"
    export PYTHONPATH
    exec /usr/bin/python3 -m mpm_macos.cli "$_command" "$@"
}

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "$1 requires root: sudo $0" >&2
        exit 1
    fi
}
