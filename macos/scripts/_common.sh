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
    # install/bootstrap must come from this checkout when one is present: the
    # installed CLI may predate a command (a fresh machine's very first
    # bootstrap has no installed CLI at all).  Ordinary management commands
    # keep using the installed CLI when it exists.
    _from_checkout=0
    case "$_command" in
        install|bootstrap)
            if [ -z "${MPM_CLI:-}" ] && [ -f "$_root/macos/python/mpm_macos/cli.py" ]; then
                _from_checkout=1
            fi
            ;;
    esac
    if [ "$_from_checkout" -eq 0 ] && [ -x "$_cli" ]; then
        exec "$_cli" "$_command" "$@"
    fi
    _python="${MPM_PYTHON:-}"
    if [ -z "$_python" ] && [ -x /usr/local/libexec/mpm-python-venv/bin/python3 ]; then
        _python=/usr/local/libexec/mpm-python-venv/bin/python3
    fi
    if [ -z "$_python" ] && [ -n "${MPM_BASE_PYTHON:-}" ] && [ -x "${MPM_BASE_PYTHON}" ]; then
        _python="${MPM_BASE_PYTHON}"
    fi
    if [ -z "$_python" ]; then
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
