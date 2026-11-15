#!/bin/sh
# Thin wrapper: read-only health check (controller, provider, live nodes).
set -eu
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/_common.sh"
run_cli health "$@"
