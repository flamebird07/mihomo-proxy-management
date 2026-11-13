#!/bin/sh
# Thin wrapper: start the core and verify TUN (falls back to mixed).
set -eu
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/_common.sh"
require_root start
run_cli start "$@"
