#!/bin/sh
# Thin wrapper: install packages, config and the LaunchDaemon, then start.
# The real work is mpm_macos.lifecycle.install.
set -eu
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/_common.sh"
require_root install
run_cli install "$@"
