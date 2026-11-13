#!/bin/sh
# Thin wrapper: remove the LaunchDaemon and program files (--purge also state).
set -eu
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/_common.sh"
require_root uninstall
run_cli uninstall "$@"
