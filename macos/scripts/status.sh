#!/bin/sh
# Thin wrapper: read-only status (launchd, mode, proxy, controller).
set -eu
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/_common.sh"
run_cli status "$@"
