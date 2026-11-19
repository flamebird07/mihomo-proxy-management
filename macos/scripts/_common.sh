# Shared thin-wrapper helper for the macOS scripts (POSIX sh, no logic).
#
# Management uses the installed CLI. Installing from a checkout uses that
# checkout's package so an existing installation can actually be upgraded.
run_cli() {
    _command="$1"
    shift
    _here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    _root=$(dirname -- "$(dirname -- "$_here")")
    _cli="${MPM_CLI:-/usr/local/bin/mihomo-proxy-management}"
    if [ -x "$_cli" ] && { [ -n "${MPM_CLI:-}" ] || [ "$_command" != install ] ||
        [ ! -f "$_root/macos/python/mpm_macos/cli.py" ]; }; then
        exec "$_cli" "$_command" "$@"
    fi
    _python="${MPM_PYTHON:-/usr/local/libexec/mpm-python-venv/bin/python3}"
    if [ -z "${MPM_PYTHON:-}" ] && [ ! -x "$_python" ]; then
        _python=/usr/bin/python3
    fi
    PYTHONPATH="$_root/common/python:$_root/macos/python"
    export PYTHONPATH
    exec "$_python" -m mpm_macos.cli "$_command" "$@"
}

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "$1 requires root: sudo $0" >&2
        exit 1
    fi
}
