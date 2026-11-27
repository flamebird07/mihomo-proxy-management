#!/bin/sh
# Thin wrapper: prepare the dedicated Python venv and bootstrap this Mac from
# the Feishu Bitable.  The real work is mpm_macos.lifecycle.bootstrap, which
# hands the checkout to scripts/deploy/__main__.py --platform macos.
set -eu
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/_common.sh"
require_root bootstrap
run_cli bootstrap "$@"
