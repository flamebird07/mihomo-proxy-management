#!/bin/sh
# Thin wrapper: stop (confirmed) then start.
set -eu
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/_common.sh"
require_root restart
run_cli restart "$@"
